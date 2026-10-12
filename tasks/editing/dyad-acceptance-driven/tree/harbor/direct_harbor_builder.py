"""Use Harbor's native auth.json support for a direct GATEWAY Builder connection."""
from contextlib import contextmanager
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import tempfile
import select
import socket
import socketserver
import threading
import subprocess
import time
from urllib.parse import urlsplit


def write_provider(path, base_url='https://api.deepseek.com/v1'):
    Path(path).write_text(
        'model_provider="gateway_direct"\nmodel="deepseek-flash"\n'
        'model_reasoning_effort="max"\ndisable_response_storage=true\n'
        '[model_providers.gateway_direct]\nname="GATEWAY direct native Codex"\n'
        'base_url=' + json.dumps(base_url) + '\nwire_api="responses"\n'
        'requires_openai_auth=true\n')


class RelayTelemetry:
    """Persist timing and byte counts without recording tunnel contents."""

    def __init__(self, path):
        self.path = Path(path)
        if self.path.exists() or self.path.is_symlink():
            raise FileExistsError('refusing to replace existing relay telemetry')
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.lock = threading.Lock()
        self.started = time.monotonic()
        self.value = {
            'schema_version': 'agentswe-builder-relay-directional-telemetry/v1',
            'content_recorded': False,
            'headers_recorded': False,
            'credentials_recorded': False,
            'created_at': datetime.now(timezone.utc).isoformat(),
            'context_closed': False,
            'connections': [],
        }
        with self.lock:
            self._write_locked()

    def _offset(self):
        return round(time.monotonic() - self.started, 6)

    def _write_locked(self):
        temporary = self.path.with_name(self.path.name + '.tmp')
        temporary.write_text(json.dumps(self.value, indent=2, sort_keys=True) + '\n')
        temporary.replace(self.path)

    def start_connection(self):
        with self.lock:
            connection_id = len(self.value['connections']) + 1
            self.value['connections'].append({
                'connection_id': connection_id,
                'start_s': self._offset(),
                'connect_head_complete_s': None,
                'connect_head_bytes': 0,
                'upstream_connected_s': None,
                'directions': {
                    'client_to_target': {
                        'bytes': 0, 'first_byte_s': None, 'last_byte_s': None,
                        'eof_s': None,
                    },
                    'target_to_client': {
                        'bytes': 0, 'first_byte_s': None, 'last_byte_s': None,
                        'eof_s': None,
                    },
                },
                'terminal_reason': None,
                'exception_type': None,
                'end_s': None,
            })
            self._write_locked()
            return connection_id

    def _connection(self, connection_id):
        return self.value['connections'][connection_id - 1]

    def head_complete(self, connection_id, size):
        with self.lock:
            row = self._connection(connection_id)
            row['connect_head_complete_s'] = self._offset()
            row['connect_head_bytes'] = size
            self._write_locked()

    def upstream_connected(self, connection_id):
        with self.lock:
            self._connection(connection_id)['upstream_connected_s'] = self._offset()
            self._write_locked()

    def transferred(self, connection_id, direction, size):
        with self.lock:
            row = self._connection(connection_id)['directions'][direction]
            now = self._offset()
            first = row['bytes'] == 0
            row['bytes'] += size
            if first:
                row['first_byte_s'] = now
            row['last_byte_s'] = now
            if first:
                self._write_locked()

    def eof(self, connection_id, direction):
        with self.lock:
            connection = self._connection(connection_id)
            connection['directions'][direction]['eof_s'] = self._offset()
            connection['terminal_reason'] = direction + '_eof'
            connection['end_s'] = self._offset()
            self._write_locked()

    def exception(self, connection_id, exception):
        with self.lock:
            connection = self._connection(connection_id)
            connection['terminal_reason'] = 'exception'
            connection['exception_type'] = type(exception).__name__
            connection['end_s'] = self._offset()
            self._write_locked()

    def terminal(self, connection_id, reason):
        with self.lock:
            connection = self._connection(connection_id)
            connection['terminal_reason'] = reason
            connection['end_s'] = self._offset()
            self._write_locked()

    def finish(self, connection_id):
        with self.lock:
            connection = self._connection(connection_id)
            if connection['terminal_reason'] is None:
                connection['terminal_reason'] = 'context_stop'
                connection['end_s'] = self._offset()
            self._write_locked()

    def close(self):
        with self.lock:
            self.value['context_closed'] = True
            self.value['closed_at'] = datetime.now(timezone.utc).isoformat()
            self.value['closed_s'] = self._offset()
            self._write_locked()


@contextmanager
def existing_proxy_for_builder(config_path, compose_path, upstream='http://127.0.0.1:7890', *,
                               provider_url='https://api.deepseek.com/v1'):
    """Expose the user's existing loopback proxy to the Builder bridge network.

    This is an opaque TCP relay: native Codex retains TLS and HTTP protocol
    ownership. It does not reconstruct requests or hold API credentials.
    """
    parsed = urlsplit(upstream)
    if parsed.scheme != 'http' or parsed.hostname != '127.0.0.1' or not parsed.port:
        raise ValueError('Expected the observed user loopback HTTP proxy')
    provider = urlsplit(provider_url)
    if (provider.scheme != 'https' or not provider.hostname or provider.username or provider.password
            or provider.hostname not in ('api.deepseek.com',)):
        raise ValueError('Expected a configured GATEWAY HTTPS provider')
    allowed_authority = provider.hostname.lower() + ':' + str(provider.port or 443)
    addresses = json.loads(subprocess.check_output(['ip', '-j', 'address', 'show', 'docker0']))
    bridge = next(x['local'] for entry in addresses for x in entry['addr_info'] if x['family'] == 'inet')
    stop = threading.Event()
    telemetry = RelayTelemetry(Path(config_path).resolve().parent / 'builder_relay_telemetry.json')

    class Relay(socketserver.BaseRequestHandler):
        def handle(self):
            connection_id = telemetry.start_connection()
            try:
                self.request.settimeout(15)
                head = b''
                while b'\r\n\r\n' not in head and len(head) < 8192:
                    block = self.request.recv(min(4096, 8192 - len(head)))
                    if not block:
                        telemetry.eof(connection_id, 'client_to_target')
                        return
                    head += block
                telemetry.head_complete(connection_id, len(head))
                first = head.partition(b'\r\n')[0].split(b' ')
                if (b'\r\n\r\n' not in head or len(first) != 3 or first[0] != b'CONNECT'
                        or first[1].lower() != allowed_authority.encode('ascii')
                        or first[2] not in (b'HTTP/1.0', b'HTTP/1.1')):
                    self.request.sendall(b'HTTP/1.1 403 Forbidden\r\nContent-Length: 0\r\nConnection: close\r\n\r\n')
                    telemetry.terminal(connection_id, 'connect_rejected')
                    return
                with socket.create_connection((parsed.hostname, parsed.port), timeout=15) as target:
                    telemetry.upstream_connected(connection_id)
                    # The 15 s limit is handshake-only; keep tunnel sockets alive for SSE.
                    self.request.settimeout(None)
                    target.settimeout(None)
                    target.sendall(head)
                    telemetry.transferred(connection_id, 'client_to_target', len(head))
                    sockets = (self.request, target)
                    while not stop.is_set():
                        ready, _, _ = select.select(sockets, [], [], 0.5)
                        for source in ready:
                            direction = ('client_to_target' if source is self.request
                                         else 'target_to_client')
                            data = source.recv(65536)
                            if not data:
                                telemetry.eof(connection_id, direction)
                                return
                            (target if source is self.request else self.request).sendall(data)
                            telemetry.transferred(connection_id, direction, len(data))
            except Exception as error:
                telemetry.exception(connection_id, error)
                raise
            finally:
                telemetry.finish(connection_id)

    class Server(socketserver.ThreadingTCPServer):
        daemon_threads = True
        allow_reuse_address = False

    server = Server((bridge, 0), Relay)
    worker = threading.Thread(target=server.serve_forever, daemon=True)
    worker.start()
    url = 'http://' + bridge + ':' + str(server.server_address[1])
    compose_path = Path(compose_path)
    compose = json.loads(compose_path.read_text())
    environment = compose['services']['main'].setdefault('environment', {})
    for name in ('HTTP_PROXY', 'HTTPS_PROXY', 'http_proxy', 'https_proxy'):
        environment[name] = url
    compose_path.write_text(json.dumps(compose, indent=2) + '\n')
    try:
        yield {'upstream': upstream, 'builder_proxy_url': url, 'relay_protocol': 'opaque_tcp',
               'allowed_connect_authority': allowed_authority,
               'private_destinations_rejected': True, 'builder_network_mode_changed': False,
               'relay_telemetry_path': str(telemetry.path),
               'relay_telemetry_content_recorded': False}
    finally:
        stop.set()
        server.shutdown()
        server.server_close()
        worker.join(timeout=3)
        telemetry.close()


@contextmanager
def direct_auth(config_path, credential_path):
    values = {}
    for raw in Path(credential_path).read_text().splitlines():
        if raw.strip() and not raw.lstrip().startswith('#') and '=' in raw:
            key, value = raw.split('=', 1)
            values[key.strip()] = value.strip().strip('\"\x27')
    key = values.get('DEEPSEEK_API_KEY')
    if not key:
        raise RuntimeError('DEEPSEEK_API_KEY is missing')
    del values
    config_path = Path(config_path)
    # /dev/shm is host tmpfs. Harbor uploads only this auth file to its owned
    # Builder container; neither the original .env nor its other keys is copied.
    with tempfile.TemporaryDirectory(prefix='agentswe-builder-auth-', dir='/dev/shm') as private:
        auth = Path(private) / 'auth.json'
        fd = os.open(auth, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, 'w') as out:
            json.dump({'auth_mode': 'apikey', 'OPENAI_API_KEY': key}, out)
        config = json.loads(config_path.read_text())
        for agent in config['agents']:
            agent.setdefault('env', {})['CODEX_AUTH_JSON_PATH'] = str(auth)
            agent['env'].pop('AGENTSWE_BUILDER_BROKER_TOKEN', None)
        config_path.write_text(json.dumps(config, indent=2) + '\n')
        try:
            yield {'transport': 'native_codex_direct', 'credential_method': 'Harbor CODEX_AUTH_JSON_PATH',
                   'credential_persisted_in_report': False, 'builder_broker_started': False}
        finally:
            # Keep the completed config reviewable without an obsolete auth path.
            for agent in config['agents']:
                agent['env'].pop('CODEX_AUTH_JSON_PATH', None)
            config_path.write_text(json.dumps(config, indent=2) + '\n')


def native_stats(run):
    turns = []
    sessions = set()
    sources = []
    for path in Path(run).glob('jobs/**/agent/codex.txt'):
        sources.append(str(path))
        for line in path.read_text(errors='replace').splitlines():
            try:
                event = json.loads(line)
            except ValueError:
                continue
            # Native logs can contain scalar JSON emitted by the merged stream.
            # Only object envelopes carry typed Codex events.
            if not isinstance(event, dict):
                continue
            if event.get('type') == 'thread.started' and event.get('thread_id'):
                sessions.add(event['thread_id'])
            if event.get('type') == 'turn.completed':
                turns.append(event.get('usage'))
    return {'transport': 'native_codex_direct', 'builder_broker_started': False,
            'native_sessions': sorted(sessions), 'native_completed_turns': len(turns),
            'native_reported_usage': turns, 'sources': sources,
            'actual_upstream_requests': None, 'complete_provider_billing_claimed': False}
