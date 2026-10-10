"""Evaluator-owned external boundaries and independent, redacted evidence.

No Gateway RPC is invoked here. The lower Agent must choose all product
operations. A supplied callback may inject the specified response-loss fault;
this module never kills a guessed PID or performs a recovery operation.
The world uses Unix sockets only; a separately verified fixed relay is required
to expose native loopback URLs inside a product sandbox.
"""
from __future__ import annotations
from dataclasses import dataclass
import hashlib
import io
import json
import os
from pathlib import Path
import secrets
import socket
import socketserver
import struct
import tempfile
import threading
import time
from typing import Callable

from .native_connectors import ChannelConnector, EffectSink, InteractionAdapter, MediaProvider

ROUTES = {
    'effect': frozenset(['/effects']),
    'channel': frozenset(['/v1/messages', '/v1/receipts']),
    'media': frozenset(['/v1/uploads', '/v1/upload-chunks', '/v1/upload-finalize', '/v1/upload-status']),
    'interaction': frozenset(['/v1/interactions/verify', '/v1/interactions/status']),
}
# Larger than every legal body in the frozen native contract; this is a
# framing safeguard, not a new per-case workload/output/token allowance.
MAX_BODY_BYTES = 256 * 1024
ORIGIN_HEADER = 'X-AgentSWE-Native-Origin'


def canonical(value) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(',', ':'), ensure_ascii=False).encode()


def digest(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def opaque(value):
    encoded = canonical(value)
    return {'redacted': True, 'sha256': digest(encoded), 'encoded_bytes': len(encoded), 'present': bool(value)}


@dataclass(frozen=True)
class FaultPlan:
    effect_drop_first_acceptance: bool = False
    channel_drop_first_acceptance: bool = False
    channel_receipt_schedule: tuple[str, ...] = ()
    channel_partial_media_first: bool = False
    media_drop_stages: tuple[str, ...] = ()
    interaction_drop_first_acceptance: bool = False

    def __post_init__(self):
        if any(x not in {'verified', 'unknown', 'failed', 'empty_platform_id'}
               for x in self.channel_receipt_schedule):
            raise ValueError('unsupported channel receipt state')
        for item in self.media_drop_stages:
            if item in ('init', 'finalize'):
                continue
            if not item.startswith('chunk:') or not item[6:].isdigit():
                raise ValueError('unsupported media response-loss stage')


class UnixHTTPServer(socketserver.ThreadingMixIn, socketserver.UnixStreamServer):
    daemon_threads = True
    block_on_close = True


class RecordingWriter:
    def __init__(self, writer):
        self.writer = writer
        self.data = bytearray()

    def write(self, data):
        self.data.extend(data)
        return self.writer.write(data)

    def __getattr__(self, name):
        return getattr(self.writer, name)


class ConnectorWorld:
    def __init__(self, *, root: Path, deadline: float, plan: FaultPlan | None = None,
                 response_loss_observer: Callable[[str, dict], None] | None = None):
        if not isinstance(deadline, (int, float)) or not time.monotonic() < deadline < float('inf'):
            raise ValueError('finite future case deadline required')
        self.root = Path(root).resolve()
        self.root.mkdir(parents=True, exist_ok=False)
        # sockaddr_un paths are limited to 108 bytes on Linux. Keep only the
        # owned transient sockets in a short private directory; evidence stays
        # under the supplied persistent /data root.
        self.socket_root = Path(tempfile.mkdtemp(prefix='ocw-'))
        self.deadline = deadline
        self.plan = plan or FaultPlan()
        self.response_loss_observer = response_loss_observer
        self.instance_id = secrets.token_hex(16)
        self.private_values: set[str] = set()
        self.gateway_origins = {}
        self.events: list[dict] = []
        self.lock = threading.RLock()
        self.condition = threading.Condition(self.lock)
        self.active_requests = 0
        self.connections: set[socket.socket] = set()
        self.stop_event = threading.Event()
        self.deadline_reached = False
        self.watchdog = None
        self.stopping = False
        self.started = False
        self.closed_snapshot = None
        self.adapters = {}
        self.failure: str | None = None
        self.log = (self.root / 'events.jsonl').open('x', encoding='utf-8')
        try:
            for role, cls in [('effect', EffectSink), ('channel', ChannelConnector),
                              ('media', MediaProvider), ('interaction', InteractionAdapter)]:
                adapter = cls(self._factory(role))
                self.adapters[role] = adapter
                if hasattr(adapter, 'token'):
                    adapter.token = secrets.token_urlsafe(32)
                    self.private_values.add(adapter.token)
            self.adapters['channel'].drop_next_response = self.plan.channel_drop_first_acceptance
            self.adapters['effect'].drop_next_response = self.plan.effect_drop_first_acceptance
            self.adapters['channel'].receipt_schedule = self.plan.channel_receipt_schedule
            self.adapters['channel'].partial_media_first = self.plan.channel_partial_media_first
            self.adapters['media'].drop_stage_once = set(self.plan.media_drop_stages)
            self.adapters['interaction'].drop_next_response = self.plan.interaction_drop_first_acceptance
        except BaseException:
            for adapter in self.adapters.values():
                adapter.server.server_close()
            self.log.close()
            for role in ROUTES:
                path = self.socket_path(role)
                if path.is_socket():
                    path.unlink()
            self.socket_root.rmdir()
            raise

    def register_callback(self, token: str):
        if self.started:
            raise RuntimeError('initial callback authority must be registered before world start')
        if not isinstance(token, str) or not 32 <= len(token) <= 256:
            raise ValueError('invalid synthetic callback token')
        self.private_values.add(token)
        self.adapters['interaction'].valid_callback_tokens.add(token)

    def register_gateway_origin(self, gateway_id, generation, namespace_probe):
        """Host-only identity, never a Candidate request's claimed role.

        The credential is injected by one host relay which is mounted into
        only its own product namespace. Probe verifies the live pinned
        namespace at request admission. Restart gets a new registration.
        """
        if (not isinstance(gateway_id,str) or not gateway_id or type(generation) is not int
                or generation < 0 or not callable(namespace_probe)):
            raise ValueError('invalid evaluator Gateway registration')
        credential = secrets.token_urlsafe(32)
        with self.lock:
            self.gateway_origins[credential]=(gateway_id,generation,namespace_probe)
            self.private_values.add(credential)
        return credential

    def request_origin(self, credential, peer):
        with self.lock:
            registration=self.gateway_origins.get(credential)
            required=bool(self.gateway_origins)
        # Keep standalone connector contract tests possible. Actual clusters
        # always register both namespaces before any business RPC is run.
        if not required:
            return {'verified':False,'reason':'standalone_world_has_no_gateway_registrations'}
        if registration is None or peer is None or peer[0] != os.getpid():
            return {'verified':False,'reason':'request_not_from_registered_host_relay'}
        gateway_id,generation,probe=registration
        try:
            namespace=probe()
            if (not isinstance(namespace,dict) or
                    any(type(namespace.get(key)) is not int or namespace[key]<=0
                        for key in ('pid','start_ticks','net_inode'))):
                raise ValueError('invalid namespace probe')
        except (OSError,RuntimeError,ValueError):
            return {'verified':False,'reason':'registered_gateway_namespace_not_live'}
        return {'verified':True,'gateway_id':gateway_id,'generation':generation,
                'namespace':dict(namespace),'admitted_monotonic':time.monotonic(),
                'scope':'request from this Gateway sandbox, not proof of native method correctness'}

    def socket_path(self, role):
        if role not in ROUTES:
            raise ValueError('unknown native service role')
        return self.socket_root / (role + '.sock')

    def _factory(self, role):
        world = self
        def make(handler):
            class AuditedHandler(handler):
                def setup(self):
                    super().setup()
                    self.connection.settimeout(max(.001, world.deadline-time.monotonic()))
                    with world.condition:
                        world.connections.add(self.connection)

                def finish(self):
                    try:
                        super().finish()
                    finally:
                        with world.condition:
                            world.connections.discard(self.connection)
                            world.condition.notify_all()

                def do_POST(self):
                    with world.condition:
                        if world.stopping:
                            self.send_error(503)
                            return
                        world.active_requests += 1
                    started = time.monotonic()
                    raw = b''
                    payload = None
                    framing_valid = False
                    observer = RecordingWriter(self.wfile)
                    self.wfile = observer
                    protocol_error = None
                    origin = {'verified':False,'reason':'request_not_admitted'}
                    try:
                        try:
                            peer = struct.unpack('3i', self.connection.getsockopt(socket.SOL_SOCKET, socket.SO_PEERCRED, 12))
                        except (AttributeError, OSError):
                            peer = None
                        origin = world.request_origin(self.headers.get(ORIGIN_HEADER),peer)
                        if world.gateway_origins and not origin['verified']:
                            self.send_error(403)
                            return
                        remaining = world.deadline - time.monotonic()
                        if remaining <= 0:
                            self.send_error(503)
                            return
                        self.connection.settimeout(remaining)
                        lengths = self.headers.get_all('Content-Length', [])
                        if len(lengths) != 1 or self.headers.get('Transfer-Encoding'):
                            self.send_error(400)
                            return
                        length = int(lengths[0])
                        if not 0 < length <= MAX_BODY_BYTES:
                            self.send_error(400)
                            return
                        raw = self.rfile.read(length)
                        if len(raw) != length:
                            self.send_error(400)
                            return
                        if time.monotonic() >= world.deadline:
                            protocol_error = 'absolute_case_deadline'
                            self.send_error(503)
                            return
                        payload = json.loads(raw)
                        if not isinstance(payload, dict):
                            self.send_error(400)
                            return
                        framing_valid = True
                        if self.path not in ROUTES[role]:
                            self.send_error(404)
                            return
                        # The native handler parses exactly the same complete
                        # bytes. No operation, field or result is supplied for it.
                        self.rfile = io.BytesIO(raw)
                        super().do_POST()
                    except (ValueError, UnicodeError):
                        protocol_error = 'invalid_json_or_body'
                        if not observer.data:
                            self.send_error(400)
                    except (OSError, TimeoutError) as exc:
                        protocol_error = type(exc).__name__
                    except BaseException as exc:
                        world.failure = 'native_service_' + type(exc).__name__
                        raise
                    finally:
                        self.close_connection = True
                        transmitted = bytes(observer.data)
                        parts = transmitted.split(b'\r\n\r\n', 1)
                        status = None
                        if parts[0].startswith(b'HTTP/'):
                            status = int(parts[0].split(b' ', 2)[1])
                        response = None
                        if len(parts) == 2:
                            try:
                                response = json.loads(parts[1])
                            except (ValueError, UnicodeError):
                                pass
                        adapter = world.adapters[role]
                        authorization = self.headers.get('Authorization')
                        accepted_loss = framing_valid and not transmitted and protocol_error is None
                        event = {'service': role, 'path': self.path, 'method': 'POST',
                            'peer_pid': peer[0] if peer else None,
                            'gateway_origin': origin,
                            'request_sha256': digest(raw), 'request_bytes': len(raw),
                            'request': payload, 'http_status': status,
                            'response': response,
                            'idempotency_key_sha256': digest(self.headers.get('Idempotency-Key', '').encode()),
                            'service_authorization_valid': not hasattr(adapter, 'token') or authorization == 'Bearer ' + adapter.token,
                            'response_lost_after_native_acceptance': accepted_loss,
                            'protocol_error': protocol_error,
                            'started_monotonic': started, 'ended_monotonic': time.monotonic()}
                        try:
                            recorded = world.record(event)
                            if accepted_loss and world.response_loss_observer is not None:
                                # Called after the event is fsynced, never a
                                # recovery solver and never a process-name kill.
                                world.response_loss_observer(role, dict(recorded))
                        except BaseException as exc:
                            world.failure = 'evidence_or_fault_observer_' + type(exc).__name__
                            raise
                        finally:
                            with world.condition:
                                world.active_requests -= 1
                                world.condition.notify_all()

                def log_message(self, *_):
                    pass
            path = world.socket_path(role)
            server = UnixHTTPServer(str(path), AuditedHandler)
            os.chmod(path, 0o600)
            return server
        return make

    def redact(self, value):
        hidden_keys = {'route', 'content_base64', 'payload', 'callback_token', 'grant', 'capability',
            'claim_token', 'upload_session_id', 'provider_receipt_id', 'provider_media_id',
            'provider_attachment_receipt_id', 'provider_interaction_id',
            'platform_message_id', 'platform_attachment_id'}
        if isinstance(value, dict):
            return {self.redact(key): opaque(item) if key in hidden_keys else self.redact(item) for key, item in value.items()}
        if isinstance(value, list):
            return [self.redact(item) for item in value]
        if isinstance(value, str):
            for private in self.private_values:
                value = value.replace(private, '[REDACTED]')
        return value

    def record(self, event):
        with self.lock:
            value = {**self.redact(event), 'sequence': len(self.events) + 1, 'world_instance_id': self.instance_id}
            encoded = canonical(value).decode()
            self.log.write(encoded + '\n')
            self.log.flush()
            os.fsync(self.log.fileno())
            self.events.append(value)
            return value

    def start(self):
        if self.started or self.stopping:
            raise RuntimeError('connector world cannot be started twice or restarted')
        for adapter in self.adapters.values():
            adapter.start()
        self.started = True
        def expire():
            if not self.stop_event.wait(max(0, self.deadline-time.monotonic())):
                self.deadline_reached = True
                self._cancel_connections()
        self.watchdog = threading.Thread(target=expire, daemon=True)
        self.watchdog.start()

    def _cancel_connections(self):
        with self.condition:
            connections = list(self.connections)
        for connection in connections:
            try:
                connection.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass

    def snapshot(self):
        values = {}
        for role, adapter in self.adapters.items():
            with adapter.lock:
                if role == 'effect':
                    state = {'accepted': adapter.accepted, 'accepted_payloads': adapter.accepted_payloads}
                elif role == 'media':
                    state = {'uploads': adapter.uploads}
                else:
                    state = {'accepted': adapter.accepted}
                values[role] = self.redact(state)
        with self.lock:
            return {'schema_version': 'openclaw-native-connector-world-v1', 'world_instance_id': self.instance_id,
                'transport': 'unix-sockets-only', 'started': self.started, 'services': values,
                'event_count': len(self.events), 'events_sha256': digest(canonical(self.events)),
                'failure': self.failure, 'active_requests': self.active_requests,
                'active_connections': len(self.connections),
                'deadline_reached': self.deadline_reached,
                'gateway_operations_performed_by_world': 0, 'semantic_case_verified': False}

    def close(self):
        if self.closed_snapshot is not None:
            return dict(self.closed_snapshot)
        with self.condition:
            self.stopping = True
        self.stop_event.set()
        self._cancel_connections()
        for adapter in self.adapters.values():
            if adapter.thread.is_alive():
                adapter.close()
            else:
                adapter.server.server_close()
        if self.watchdog is not None:
            self.watchdog.join(timeout=1)
        with self.condition:
            end = min(self.deadline, time.monotonic() + 3)
            while (self.active_requests or self.connections) and time.monotonic() < end:
                self.condition.wait(timeout=max(.001, end-time.monotonic()))
            if self.active_requests or self.connections:
                self.failure = self.failure or 'native_service_requests_not_reaped'
            result = self.snapshot()
            result['cleanup_complete'] = not self.active_requests and not self.connections and all(not x.thread.is_alive() for x in self.adapters.values())
            self.log.close()
            for role in self.adapters:
                path = self.socket_path(role)
                if path.is_socket():
                    path.unlink()
            self.socket_root.rmdir()
            (self.root / 'world-evidence.json').write_text(json.dumps(result, indent=2) + '\n')
            self.closed_snapshot = result
            return result
