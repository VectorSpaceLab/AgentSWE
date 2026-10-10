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
# Fallbacks only: both values are normally read back out of the evaluator's own
# deepcode_config.json, so evaluator/harness/deepcode_lower_agent.py:44 stays the
# single source of truth for the budget.
MAX_OUTPUT_TOKENS = 32000
CATALOG_CONTEXT_WINDOW = 128_000


def _connection_revision(api_base):
    """ConnectionResolver.connection_revision() for the legacy 'custom' connection.

    Product side: core/providers/profiles.py:259-275 -- sha256 over the executable,
    non-credential connection fields, first 16 hex characters.  The 'custom' spec is
    id/providerName 'custom', backend 'openai_compat' (core/providers/registry.py:64-73),
    with no extra headers and enabled=True (profiles.py:327-349).
    """
    payload = json.dumps({'id': 'custom', 'providerName': 'custom', 'adapter': 'openai_compat',
                          'apiBase': api_base, 'extraHeaders': {}, 'enabled': True},
                         sort_keys=True, separators=(',', ':'), ensure_ascii=False).encode()
    return hashlib.sha256(payload).hexdigest()[:16]


def write_model_catalog_cache(home, api_base, config):
    """Seed the product's own model catalog so the configured maxTokens survives the clamp.

    ExecutionProfile.max_tokens = min(settings.max_tokens, max_output_tokens)
    (core/providers/profiles.py:197); without this file max_output_tokens for
    'deepseek-flash' is the deepseek-v3 family value 8192 (core/providers/catalog.py:100,131)
    and the evaluator's 32000 is clamped straight back to 8192.  With it,
    resolve_execution_profile() passes (contextWindow, maxOutputTokens) from the cache
    (core/compat/runtime.py:172-181) and the product resolves 32000.

    'reasoning' is null on purpose: CatalogModel.from_dict then falls back to
    infer_reasoning_capabilities(), which returns None for this model exactly as it does
    today, so reasoning_effort='high' resolves unchanged (core/providers/reasoning.py:125-126).
    The sandbox has no route to a /models endpoint, so this entry is never refreshed away.
    """
    defaults = ((config.get('agents') or {}).get('defaults') or {}) if isinstance(config, dict) else {}
    model = defaults.get('model') or 'deepseek-flash'
    max_output_tokens = int(defaults.get('maxTokens') or defaults.get('max_tokens') or MAX_OUTPUT_TOKENS)
    (Path(home) / 'model_catalog_cache.json').write_text(json.dumps({
        'version': 1,
        'connections': {'custom': {'connectionRevision': _connection_revision(api_base),
                                   'source': 'manual', 'refreshedAt': time.time(),
                                   'models': [{'id': model, 'name': model,
                                               'contextWindow': CATALOG_CONTEXT_WINDOW,
                                               'maxOutputTokens': max_output_tokens,
                                               'supportedParameters': [], 'reasoning': None}]}},
    }, indent=2) + '\n', encoding='utf-8')


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
    def __init__(self, endpoint, context_identity=None):
        parsed = urllib.parse.urlsplit(endpoint)
        if parsed.scheme != 'http' or parsed.hostname != '127.0.0.1' or not parsed.port:
            raise ValueError('relay requires one explicitly selected loopback HTTP broker')
        self.host, self.port = parsed.hostname, parsed.port
        self.context_identity = context_identity or "synthetic-transport-probe"
        self.temp = tempfile.TemporaryDirectory(prefix='agentswe-deepcode-uds-')
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
                if self.path not in ALLOWED_POST_PATHS:
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
                        connection.request('POST', self.path, body, headers={
                            'Content-Type': 'application/json', 'Authorization': 'Bearer broker-only-placeholder', 'X-AgentSWE-Context': owner.context_identity})
                        response = connection.getresponse()
                        raw = response.read()
                        status, content_type = response.status, response.getheader('Content-Type', 'application/json')
                    finally:
                        connection.close()
                        owner.connections.discard(connection)
                    owner.events.append({'path': self.path, 'status': status, 'request_bytes': len(body), 'response_bytes': len(raw)})
                    if status >= 400:
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


def sandbox_command(command, *, writable=(), readonly=(), cwd=None, relay=None, trusted_observer_orchestrator=False):
    """Mount only explicit run-local state, source/dependency inputs and UDS."""
    argv = ['bwrap', '--die-with-parent', '--new-session', '--unshare-pid',
        '--unshare-ipc', '--unshare-uts', '--unshare-net', '--cap-drop', 'ALL']
    if trusted_observer_orchestrator:
        # Only evaluator-owned observer orchestration gets namespace setup
        # capability. The nested Candidate app server drops every capability.
        argv += ['--cap-add', 'CAP_SYS_ADMIN']
    for directory in ('/usr', '/bin', '/lib', '/lib64'):
        if Path(directory).exists():
            argv += ['--ro-bind', directory, directory]
    argv += ['--proc', '/proc', '--dev', '/dev', '--tmpfs', '/tmp']
    # Local product HTTP services use localhost; no DNS resolver or broad
    # /etc mount is needed. These contain no evaluator credentials.
    for name in ('hosts', 'nsswitch.conf', 'localtime'):
        source = Path('/etc') / name
        if source.exists():
            argv += ['--ro-bind', str(source), str(source)]
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
        config_path = Path('/deepcode-home/deepcode_config.json')
        if config_path.is_file():
            config = json.loads(config_path.read_text())
            config['providers']['custom']['apiBase'] = endpoint
            config_path.write_text(json.dumps(config, indent=2)+'\n')
            # Same endpoint, same moment: the catalog entry is fingerprinted over
            # this apiBase, so it can only be written after the rewrite above.
            write_model_catalog_cache(config_path.parent, endpoint, config)
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
