"""Fixed lower-only UDS relay and loopback bridge for an isolated net namespace.

The outside relay never accepts a destination URL from a Candidate. The inside
bridge lets an unmodified product HTTP SDK use localhost while bwrap/Docker
isolates every other host/network endpoint. No provider credential is present.
"""
from __future__ import annotations
import argparse
import hashlib
import http.client
import http.server
import json
import os
import socket
import socketserver
import subprocess
import tempfile
import threading
import time
import urllib.parse
from pathlib import Path

ALLOWED_POST_PATHS = frozenset({'/v1/responses', '/v1/chat/completions'})
MAX_BODY = 16_000_000


def load_transport_health(output, context_id, *, endpoint=None):
    path = Path(output) / 'native-transport-health.json'
    if path.is_symlink() or not path.is_file():
        raise ValueError('evaluator transport health evidence missing')
    value = json.loads(path.read_text())
    expected_source = {'path': str(Path(__file__).resolve()),
        'sha256': hashlib.sha256(Path(__file__).read_bytes()).hexdigest()}
    if (value.get('schema_version') != 'openwiki-native-transport-health/v1'
            or value.get('context_id') != context_id
            or value.get('valid') is not True
            or value.get('owner') != 'evaluator outside Candidate namespace'
            or value.get('health_request_observed') is not True
            or value.get('fixed_loopback_host') != '127.0.0.1'
            or value.get('observer_source') != expected_source):
        raise ValueError('evaluator transport health binding invalid')
    if endpoint is not None:
        parsed = urllib.parse.urlsplit(endpoint)
        if (parsed.scheme != 'http' or parsed.hostname != value['fixed_loopback_host']
                or parsed.port != value.get('fixed_loopback_port')):
            raise ValueError('evaluator transport health endpoint mismatch')
    return value


class UnixConnection(http.client.HTTPConnection):
    def __init__(self, socket_path, timeout=650):
        super().__init__('lower-only', timeout=timeout)
        self.socket_path = str(socket_path)

    def connect(self):
        self.sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.sock.settimeout(self.timeout)
        self.sock.connect(self.socket_path)


class _UnixServer(socketserver.ThreadingMixIn, socketserver.UnixStreamServer):
    daemon_threads = True
    block_on_close = False


def _respond(handler, status, body, content_type='application/json'):
    handler.send_response(status)
    handler.send_header('Content-Type', content_type)
    handler.send_header('Content-Length', str(len(body)))
    handler.end_headers()
    handler.wfile.write(body)


class FixedLowerRelay:
    """Outside namespace; fixed localhost broker, no generic proxy capability."""
    def __init__(self, endpoint, *, context_id=None, observation_dir=None):
        from .broker_observation import ObservationStore
        parsed = urllib.parse.urlsplit(endpoint)
        if parsed.scheme != 'http' or parsed.hostname != '127.0.0.1' or not parsed.port:
            raise ValueError('relay requires one explicitly selected loopback HTTP broker')
        self.host, self.port = parsed.hostname, parsed.port
        self.context_id=context_id or hashlib.sha256(os.urandom(32)).hexdigest()
        self.observation_store = ObservationStore(observation_dir, self.context_id)
        self.temp = tempfile.TemporaryDirectory(prefix='agentswe-wiki-uds-')
        self.socket_path = Path(self.temp.name) / 'lower.sock'
        self.events, self.errors, self.connections = [], [], set()
        self._condition = threading.Condition()
        self._active = 0
        self._closing = False
        self._sealed = False
        self._dropped_events = 0
        self._late_events = 0
        self.observation_summary = None
        self._health_recorded = False
        self.observation_dir = Path(observation_dir).resolve() if observation_dir is not None else None
        owner = self

        class Handler(http.server.BaseHTTPRequestHandler):
            def log_message(self, *_args):
                pass

            def do_GET(self):
                if self.path != '/transport-health':
                    _respond(self, 404, b'{"error":"lower_endpoint_only"}')
                    return
                owner._record_health()
                _respond(self, 200, b'{"valid":true,"scope":"fixed_lower_broker"}')

            def do_POST(self):
                with owner._condition:
                    if owner._closing:
                        _respond(self, 503, b'{"error":"lower_relay_closed"}')
                        return
                    owner._active += 1
                try:
                    self._forward()
                finally:
                    with owner._condition:
                        owner._active -= 1
                        owner._condition.notify_all()

            def _forward(self):
                if self.path not in ALLOWED_POST_PATHS:
                    owner._event({'path': self.path, 'error': 'unmapped_product_endpoint'}, error=True)
                    _respond(self, 404, b'{"error":"lower_endpoint_only"}')
                    return
                exchange, observation = None, None
                response_status, content_type, failure = 0, '', None
                delivery_attempted = False
                body, raw = b'', b''
                try:
                    size = int(self.headers.get('Content-Length', '0'))
                    if not 0 < size <= MAX_BODY:
                        raise ValueError('invalid bounded content length')
                    body = self.rfile.read(size)
                    if len(body) != size:
                        raise ValueError('incomplete request body')
                    try:
                        exchange = owner.observation_store.begin(body)
                    except Exception as exc:
                        owner._event({'error': 'observation_begin_' + type(exc).__name__}, error=True)
                    connection = http.client.HTTPConnection(owner.host, owner.port, timeout=650)
                    with owner._condition:
                        if owner._closing:
                            raise OSError('lower relay closed before forward')
                        owner.connections.add(connection)
                    try:
                        connection.request('POST', self.path, body, headers={
                            'Content-Type': 'application/json', 'Authorization': 'Bearer broker-only-placeholder','X-AgentSWE-Context':owner.context_id})
                        response = connection.getresponse()
                        response_status = response.status
                        content_type = response.getheader('Content-Type', 'application/json') or 'application/json'
                        raw = response.read()
                        if exchange is not None:
                            try:
                                exchange.feed_response(raw)
                            except Exception as exc:
                                failure = 'observation_feed_' + type(exc).__name__
                                owner._event({'error': failure}, error=True)
                    finally:
                        connection.close()
                        with owner._condition:
                            owner.connections.discard(connection)
                    if response_status >= 400:
                        owner._event({'path': self.path, 'error': 'broker_http_error', 'status': response_status}, error=True)
                    # Preserve the actual broker response even if observation
                    # parsing or storage fails. Such failures invalidate the
                    # evaluator evidence; they never replace a delivered 200.
                    delivery_attempted = True
                    _respond(self, response_status, raw, content_type)
                except (OSError, ValueError, http.client.HTTPException) as exc:
                    failure = type(exc).__name__
                    owner._event({'path': self.path, 'error': failure}, error=True)
                    if not delivery_attempted:
                        try:
                            _respond(self, 502, b'{"error":"evaluator_lower_transport_failure"}')
                        except OSError:
                            pass
                finally:
                    if exchange is not None:
                        try:
                            observation = owner.observation_store.record(exchange,
                                status=response_status, content_type=content_type, error=failure)
                        except Exception as exc:
                            owner._event({'error': 'observation_record_' + type(exc).__name__}, error=True)
                    observation = observation if isinstance(observation, dict) else {}
                    owner._event({'path': self.path, 'status': response_status,
                        'request_bytes': len(body), 'response_bytes': len(raw),
                        'observation_path': observation.get('path'),
                        'observation_complete': observation.get('complete')})
                    if not observation.get('complete'):
                        owner._event({'path': self.path, 'error': 'broker_observation_incomplete',
                            'observation_path': observation.get('path'),
                            'reasons': observation.get('errors', [])}, error=True)

        self.server = _UnixServer(str(self.socket_path), Handler)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)

    def start(self):
        self.thread.start()
        return self

    def _event(self, value, *, error=False):
        with self._condition:
            if self._sealed:
                self._late_events += 1
                return
            target = self.errors if error else self.events
            if len(target) < 512:
                target.append(value)
            else:
                self._dropped_events += 1

    def _record_health(self):
        with self._condition:
            if self._health_recorded or self._closing or self.observation_dir is None:
                return
            self._health_recorded = True
        try:
            path = self.observation_dir / 'native-transport-health.json'
            value = {'schema_version': 'openwiki-native-transport-health/v1',
                'valid': True, 'context_id': self.context_id,
                'observed_at': time.time(), 'owner': 'evaluator outside Candidate namespace',
                'fixed_loopback_host': self.host, 'fixed_loopback_port': self.port,
                'health_request_observed': True,
                'observer_source': {'path': str(Path(__file__).resolve()),
                    'sha256': hashlib.sha256(Path(__file__).read_bytes()).hexdigest()}}
            with path.open('x', encoding='utf-8') as handle:
                json.dump(value, handle, sort_keys=True)
                handle.flush()
                os.fsync(handle.fileno())
        except Exception as exc:
            self._event({'error': 'transport_health_observation_' + type(exc).__name__}, error=True)

    def close(self):
        if self.observation_summary is not None:
            return self.observation_summary
        with self._condition:
            self._closing = True
            connections = list(self.connections)
        for connection in connections:
            connection.close()
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=3)
        deadline = time.monotonic() + 3
        with self._condition:
            while self._active and time.monotonic() < deadline:
                self._condition.wait(timeout=max(0, deadline - time.monotonic()))
            pending = self._active
            dropped = self._dropped_events
            if pending or dropped:
                self.errors.append({'error': 'relay_evidence_incomplete',
                    'pending_handlers': pending, 'dropped_events': dropped})
            self._sealed = True
        try:
            summary = self.observation_store.close(timeout=max(0, deadline - time.monotonic()))
        except Exception as exc:
            summary = {'complete': False, 'sealed': False, 'records': [],
                'errors': ['observation_close_' + type(exc).__name__]}
        summary = dict(summary, relay_pending_handlers=pending,
            relay_dropped_events=dropped, relay_server_stopped=not self.thread.is_alive())
        if pending or dropped or self.thread.is_alive():
            summary['complete'] = False
        if summary.get('complete') is not True:
            self.errors.append({'error': 'broker_observation_store_incomplete'})
        self.observation_summary = summary
        self.temp.cleanup()
        return summary


def sandbox_command(command, *, writable=(), readonly=(), cwd=None, relay=None):
    """Mount only explicit run-local state, source/dependency inputs and UDS."""
    argv = ['bwrap', '--die-with-parent', '--new-session', '--unshare-pid',
        '--unshare-ipc', '--unshare-uts', '--unshare-net', '--cap-drop', 'ALL']
    for directory in ('/usr', '/bin', '/lib', '/lib64'):
        if Path(directory).exists():
            argv += ['--ro-bind', directory, directory]
    argv += ['--proc', '/proc', '--dev', '/dev', '--tmpfs', '/tmp']
    mounted = set()
    for mode, paths in (("--bind", writable), ("--ro-bind", readonly)):
        for path in paths:
            path = Path(path).absolute()
            if path in {Path("/"), Path("/home"), Path("/root"), Path("/data"), Path("@@AGENTSWE_LEGACY_HOME@@"), Path("@@AGENTSWE_LEGACY_DATA@@")}:
                raise ValueError("broad host mount rejected")
            seen = mounted.__contains__(str(path))
            if seen:
                continue
            if not path.exists():
                raise FileNotFoundError(path)
            argv += [mode, str(path), str(path)]
            mounted.add(str(path))
    if relay is not None:
        argv += ["--dir", "/run/agentswe", "--ro-bind", str(relay.socket_path), "/run/agentswe/lower.sock",
            "--ro-bind", str(Path(__file__).resolve()), "/run/agentswe/transport.py"]
    if cwd is not None:
        argv += ["--chdir", str(cwd)]
    return argv + list(command)


def inside_main(socket_path, preflight, command):
    class Handler(http.server.BaseHTTPRequestHandler):
        def log_message(self, *_args):
            pass

        def do_POST(self):
            if self.path not in ALLOWED_POST_PATHS:
                record.setdefault('endpoint_mapping_errors', []).append({'path': self.path, 'error': 'unmapped_product_endpoint'})
                _respond(self, 404, b'{"error":"lower_endpoint_only"}')
                return
            connection = UnixConnection(socket_path)
            try:
                size = int(self.headers.get('Content-Length', '0'))
                if not 0 < size <= MAX_BODY:
                    raise ValueError('invalid bounded content length')
                connection.request('POST', self.path, self.rfile.read(size), headers={'Content-Type': 'application/json'})
                response = connection.getresponse()
                _respond(self, response.status, response.read(), response.getheader('Content-Type', 'application/json'))
            except (OSError, ValueError, http.client.HTTPException):
                _respond(self, 502, b'{"error":"evaluator_lower_transport_failure"}')
            finally:
                connection.close()

    record = {'schema_version': 'agentswe-isolated-lower-transport/v1', 'valid': False,
        'network_namespace_required': True, 'network_scope': 'fixed lower endpoint via single UDS'}
    server = None
    try:
        connection = UnixConnection(socket_path, timeout=5)
        try:
            connection.request('GET', '/transport-health')
            response = connection.getresponse()
            if response.status != 200 or json.loads(response.read()).get('valid') is not True:
                raise RuntimeError('UDS lower relay failed health check')
        finally:
            connection.close()
        server = http.server.ThreadingHTTPServer(('127.0.0.1', 0), Handler)
        server.daemon_threads = True
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        endpoint = f'http://127.0.0.1:{server.server_port}/v1'
        # HTTP loopback itself must work in this exact namespace, not just bind.
        probe = socket.create_connection(server.server_address, timeout=3)
        probe.close()
        record.update(valid=True, endpoint=endpoint, relay_health=True, loopback_connection_valid=True)
        Path(preflight).write_text(json.dumps(record, indent=2) + '\n')
        env = dict(os.environ)
        env.update(OPENAI_BASE_URL=endpoint, OPENAI_API_BASE=endpoint)
        result = subprocess.call(command, env=env)
        Path(preflight).write_text(json.dumps(record, indent=2) + '\n')
        return result
    except (OSError, ValueError, RuntimeError, http.client.HTTPException) as exc:
        record['error'] = f'{type(exc).__name__}: {exc}'
        Path(preflight).write_text(json.dumps(record, indent=2) + '\n')
        return 78
    finally:
        if server:
            server.shutdown()
            server.server_close()


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--inside', action='store_true', required=True)
    parser.add_argument('--uds', required=True)
    parser.add_argument('--preflight', required=True)
    parser.add_argument('command', nargs=argparse.REMAINDER)
    args = parser.parse_args()
    command = args.command[1:] if args.command[:1] == ['--'] else args.command
    if not command:
        parser.error('product command required after --')
    raise SystemExit(inside_main(args.uds, args.preflight, command))
