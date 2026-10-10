"""Fixed lower-only UDS relay and loopback bridge for an isolated net namespace.

The outside relay never accepts a destination URL from a Candidate. The inside
bridge lets an unmodified product HTTP SDK use localhost while bwrap/Docker
isolates every other host/network endpoint. No provider credential is present.
"""
from __future__ import annotations
import argparse
import http.client
import http.server
import json
import os
import socket
import socketserver
import subprocess
import tempfile
import re
import threading
import urllib.parse
from pathlib import Path

ALLOWED_POST_PATHS = frozenset({'/v1/responses', '/v1/chat/completions'})
# Case-fixture paths: a 4xx there (409 action_not_allowed, 404 not_found) is the fixture
# answering the product by design, so it is Candidate-visible protocol traffic, not a
# relay/provider fault. Fixture 5xx and every model-broker error stay transport errors.
FIXTURE_PATHS = frozenset({'/action', '/record'})
MAX_BODY = 16_000_000


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
    def __init__(self, endpoint, allowed_paths=ALLOWED_POST_PATHS, *, evaluation_scope=None):
        parsed = urllib.parse.urlsplit(endpoint)
        if parsed.scheme != 'http' or parsed.hostname != '127.0.0.1' or not parsed.port:
            raise ValueError('relay requires one explicitly selected loopback HTTP broker')
        self.host, self.port = parsed.hostname, parsed.port
        # Evaluator-owned per-evaluation identity scope. The candidate product never
        # sees or sets it: the relay adds it on the way to the broker, so two
        # independent Candidates cannot share one logical-request identity.
        if evaluation_scope is not None and not re.fullmatch(r'[0-9a-f]{8,64}', evaluation_scope):
            raise ValueError('evaluation scope must be an evaluator hex digest')
        self.evaluation_scope = evaluation_scope
        self.allowed_paths = frozenset(allowed_paths)
        if not self.allowed_paths <= (ALLOWED_POST_PATHS | {'/action', '/record'}):
            raise ValueError('unexpected evaluator relay surface')
        self.temp = tempfile.TemporaryDirectory(prefix='agentswe-aider-uds-')
        self.socket_path = Path(self.temp.name) / 'lower.sock'
        self.events, self.errors, self.connections = [], [], set()
        owner = self

        class Handler(http.server.BaseHTTPRequestHandler):
            def log_message(self, *_args):
                pass

            def do_GET(self):
                if self.path != '/transport-health':
                    _respond(self, 404, b'{"error":"lower_endpoint_only"}')
                    return
                _respond(self, 200, b'{"valid":true,"scope":"fixed_lower_broker"}')

            def do_POST(self):
                if self.path not in owner.allowed_paths:
                    owner.errors.append({'path': self.path, 'error': 'unmapped_product_endpoint'})
                    _respond(self, 404, b'{"error":"lower_endpoint_only"}')
                    return
                try:
                    size = int(self.headers.get('Content-Length', '0'))
                    if not 0 < size <= MAX_BODY:
                        raise ValueError('invalid bounded content length')
                    body = self.rfile.read(size)
                    connection = http.client.HTTPConnection(owner.host, owner.port, timeout=650)
                    owner.connections.add(connection)
                    try:
                        forward = {'Content-Type': 'application/json',
                                   'Authorization': 'Bearer broker-only-placeholder'}
                        if owner.evaluation_scope is not None:
                            forward['X-AgentSWE-Evaluation'] = owner.evaluation_scope
                        connection.request('POST', self.path, body, headers=forward)
                        response = connection.getresponse()
                        raw = response.read()
                        status, content_type = response.status, response.getheader('Content-Type', 'application/json')
                    finally:
                        connection.close()
                        owner.connections.discard(connection)
                    owner.events.append({'path': self.path, 'status': status, 'request_bytes': len(body), 'response_bytes': len(raw)})
                    if status >= 400 and not (self.path in FIXTURE_PATHS and status < 500):
                        owner.errors.append({'path': self.path, 'error': 'broker_http_error', 'status': status})
                    _respond(self, status, raw, content_type)
                except (OSError, ValueError, http.client.HTTPException) as exc:
                    owner.errors.append({'path': self.path, 'error': type(exc).__name__})
                    _respond(self, 502, b'{"error":"evaluator_lower_transport_failure"}')

        self.server = _UnixServer(str(self.socket_path), Handler)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)

    def start(self):
        self.thread.start()
        return self

    def close(self):
        for connection in list(self.connections):
            connection.close()
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=3)
        self.temp.cleanup()


def sandbox_command(command, *, writable=(), readonly=(), cwd=None, relay=None):
    """Mount only explicit run-local state, source/dependency inputs and UDS."""
    argv = ['bwrap', '--die-with-parent', '--new-session', '--unshare-pid',
        '--unshare-ipc', '--unshare-uts', '--unshare-net', '--cap-drop', 'ALL']
    for directory in ('/usr', '/bin', '/lib', '/lib64'):
        if Path(directory).exists():
            argv += ['--ro-bind', directory, directory]
    argv += ['--proc', '/proc', '--dev', '/dev', '--tmpfs', '/tmp']
    mounted = set()
    for mode, paths in (('--bind', writable), ('--ro-bind', readonly)):
        for path in paths:
            path = Path(path).absolute()
            if path in {Path('/'), Path('/home'), Path('/root'), Path('/data'), Path('@@AGENTSWE_LEGACY_HOME@@'), Path('@@AGENTSWE_LEGACY_DATA@@')}:
                raise ValueError('broad host mount rejected')
            if str(path) in mounted:
                continue
            if not path.exists():
                raise FileNotFoundError(path)
            argv += [mode, str(path), str(path)]
            mounted.add(str(path))
    if relay is not None:
        argv += ['--dir', '/run/agentswe', '--ro-bind', str(relay.socket_path), '/run/agentswe/lower.sock',
            '--ro-bind', str(Path(__file__).resolve()), '/run/agentswe/transport.py']
    if cwd:
        argv += ['--chdir', str(cwd)]
    return argv + list(command)


def start_inside_bridge(socket_path, allowed_paths):
    class Handler(http.server.BaseHTTPRequestHandler):
        def log_message(self, *_args):
            pass
        def do_POST(self):
            if self.path not in allowed_paths:
                _respond(self, 404, b'{"error":"fixed_endpoint_only"}')
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
                _respond(self, 502, b'{"error":"evaluator_fixed_transport_failure"}')
            finally:
                connection.close()
    connection = UnixConnection(socket_path, timeout=5)
    try:
        connection.request('GET', '/transport-health')
        response = connection.getresponse()
        if response.status != 200 or json.loads(response.read()).get('valid') is not True:
            raise RuntimeError('fixed relay failed health check')
    finally:
        connection.close()
    server = http.server.ThreadingHTTPServer(('127.0.0.1', 0), Handler)
    server.daemon_threads = True
    threading.Thread(target=server.serve_forever, daemon=True).start()
    with socket.create_connection(server.server_address, timeout=3):
        pass
    return server


def inside_main(lower_socket, fixture_socket, preflight, command):
    servers = []
    record = {'schema_version': 'agentswe-aider-isolated-transport/v1', 'valid': False,
              'network_scope': 'two fixed evaluator UDS; no host TCP', 'provider_calls': 0}
    try:
        lower = start_inside_bridge(lower_socket, ALLOWED_POST_PATHS)
        servers.append(lower)
        fixture = start_inside_bridge(fixture_socket, frozenset({'/action', '/record'}))
        servers.append(fixture)
        endpoint = f'http://127.0.0.1:{lower.server_port}/v1'
        fixture_endpoint = f'http://127.0.0.1:{fixture.server_port}'
        record.update(valid=True, endpoint=endpoint, fixture_endpoint=fixture_endpoint,
                      relay_health=True, loopback_connection_valid=True)
        Path(preflight).write_text(json.dumps(record, indent=2) + '\n')
        env = dict(os.environ)
        env.update(OPENAI_BASE_URL=endpoint, OPENAI_API_BASE=endpoint,
                   AGENTSWE_RESPONSES_BASE_URL=endpoint + '/responses',
                   AGENTSWE_CASE_SERVICE_URL=fixture_endpoint,
                   OPENAI_API_KEY='broker-only-placeholder')
        return subprocess.call(command, env=env)
    except (OSError, ValueError, RuntimeError, http.client.HTTPException) as exc:
        record['error'] = f'{type(exc).__name__}: {exc}'
        Path(preflight).write_text(json.dumps(record, indent=2) + '\n')
        return 78
    finally:
        for server in servers:
            server.shutdown()
            server.server_close()


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--inside', action='store_true', required=True)
    parser.add_argument('--lower-uds', required=True)
    parser.add_argument('--fixture-uds', required=True)
    parser.add_argument('--preflight', required=True)
    parser.add_argument('command', nargs=argparse.REMAINDER)
    args = parser.parse_args()
    command = args.command[1:] if args.command[:1] == ['--'] else args.command
    if not command:
        parser.error('product command required after --')
    raise SystemExit(inside_main(args.lower_uds, args.fixture_uds, args.preflight, command))

