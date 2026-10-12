"""Fixed lower-only UDS relay and loopback bridge for an isolated net namespace.

The outside relay never accepts a destination URL from a Candidate. The inside
bridge lets an unmodified product HTTP SDK use localhost while bwrap/Docker
isolates every other host/network endpoint. No provider credential is present.
"""
from __future__ import annotations
import argparse
import base64
import hashlib
import time
import uuid
import http.client
import http.server
import json
import os
import socket
import socketserver
import subprocess
import tempfile
import threading
import urllib.parse
from pathlib import Path

ALLOWED_POST_PATHS = frozenset({'/v1/responses', '/v1/chat/completions'})
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


# The lower agent runs under `// @vitest-environment happy-dom`, which enforces
# the same-origin policy on window.fetch.  A JSON POST from the app origin to
# this loopback bridge is therefore a PREFLIGHTED cross-origin request: without
# an OPTIONS route the bridge answered `501 Unsupported method ('OPTIONS')` and
# the browser blocked the real request.  That is what killed the resumed
# (DYAD_RESTART_EPOCH=1) epoch's /v1/chat/completions call -- the model response
# came back as `chat:response:error: NetworkError: Cross-Origin Request
# Blocked`, the assistant message persisted empty, and the case ended with no
# action dispatched.  Preflight is answered for EVERY path: an unmapped path
# then receives the ordinary bounded 404 below instead of a network error.
CORS_HEADERS = (
    ('Access-Control-Allow-Origin', '*'),
    ('Access-Control-Allow-Methods', 'POST, GET, OPTIONS'),
    ('Access-Control-Expose-Headers', '*'),
)
CORS_MAX_AGE = '600'


def _respond(handler, status, body, content_type='application/json'):
    handler.send_response(status)
    handler.send_header('Content-Type', content_type)
    handler.send_header('Content-Length', str(len(body)))
    for name, value in CORS_HEADERS:
        handler.send_header(name, value)
    handler.end_headers()
    handler.wfile.write(body)


def _respond_preflight(handler):
    """204 + the headers happy-dom's CORS check reads.  No body, no relay."""
    requested = handler.headers.get('Access-Control-Request-Headers')
    handler.send_response(204)
    for name, value in CORS_HEADERS:
        handler.send_header(name, value)
    handler.send_header('Access-Control-Allow-Headers', requested or '*')
    handler.send_header('Access-Control-Max-Age', CORS_MAX_AGE)
    handler.send_header('Content-Length', '0')
    handler.end_headers()


class FixedLowerRelay:
    """Outside namespace; fixed localhost broker, no generic proxy capability."""
    def __init__(self, endpoint, *, context_id=None, deadline=None, journal=None):
        parsed = urllib.parse.urlsplit(endpoint)
        if parsed.scheme != 'http' or parsed.hostname != '127.0.0.1' or not parsed.port:
            raise ValueError('relay requires one explicitly selected loopback HTTP broker')
        self.host, self.port = parsed.hostname, parsed.port
        self.temp = tempfile.TemporaryDirectory(prefix='agentswe-dyad-uds-')
        self.socket_path = Path(self.temp.name) / 'lower.sock'
        self.events, self.errors, self.connections = [], [], set()
        self.context_id=context_id or ('isolated-'+uuid.uuid4().hex)
        self.deadline=min(deadline or time.monotonic()+600,time.monotonic()+600)
        self.journal=Path(journal) if journal is not None else Path(self.temp.name)/'requests.json'
        self.intent_lock=threading.Lock()
        self.intents=json.loads(self.journal.read_text()) if self.journal.exists() else {}
        owner = self

        def save():
            owner.journal.parent.mkdir(parents=True,exist_ok=True)
            temp=owner.journal.with_suffix('.tmp')
            with temp.open('w') as f:
                json.dump(owner.intents,f);f.flush();os.fsync(f.fileno())
            os.replace(temp,owner.journal)
            fd=os.open(owner.journal.parent,os.O_RDONLY)
            try:os.fsync(fd)
            finally:os.close(fd)

        class Handler(http.server.BaseHTTPRequestHandler):
            def log_message(self, *_args):
                pass

            def do_OPTIONS(self):
                _respond_preflight(self)

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
                    payload=json.loads(body)
                    if not isinstance(payload,dict):raise ValueError('request must be an object')
                    normalized=json.dumps(payload,sort_keys=True,separators=(',',':')).encode()
                    body_hash=hashlib.sha256(self.path.encode()+b'\0'+normalized).hexdigest()
                    explicit=next((self.headers.get(name) for name in ('Idempotency-Key','X-Request-Id','X-AgentSWE-Request-Id') if self.headers.get(name)),None)
                    action=self.headers.get('X-AgentSWE-Action')
                    if any(v is not None and (len(v)>256 or not v.strip()) for v in (explicit,action)):
                        raise ValueError('invalid bounded request identity')
                    with owner.intent_lock:
                        if explicit:
                            identity='request:'+explicit
                        elif action:
                            identity='action:'+action+':'+body_hash
                        else:
                            identity='sequence:'+str(len(owner.intents)+1)
                        key=hashlib.sha256((owner.context_id+'\0'+identity).encode()).hexdigest()
                        previous=owner.intents.get(key)
                        # AI SDK retries HTTP 409. Terminal refusal uses 400 so it stops.
                        if previous is not None:
                            if previous['body_sha256']!=body_hash:
                                _respond(self,400,b'{"error":"request_identity_payload_changed"}');return
                            if previous['state']=='completed':
                                _respond(self,200,base64.b64decode(previous['response']),previous['content_type']);return
                            _respond(self,400,b'{"error":"prior_request_result_unknown","retry_allowed":false}');return
                        if any(v['state']!='completed' for v in owner.intents.values()):
                            _respond(self,400,b'{"error":"prior_case_request_unresolved","retry_allowed":false}');return
                        if owner.deadline<=time.monotonic():
                            _respond(self,400,b'{"error":"case_deadline_expired"}');return
                        owner.intents[key]={'state':'submitted_or_unknown','body_sha256':body_hash,'identity':identity}
                        save()
                    connection=http.client.HTTPConnection(owner.host,owner.port,timeout=max(.001,owner.deadline-time.monotonic()))
                    owner.connections.add(connection)
                    try:
                        connection.request('POST',self.path,body,headers={
                            'Content-Type':'application/json','Authorization':'Bearer broker-only-placeholder',
                            'X-AgentSWE-Context':key,'X-AgentSWE-Deadline-Monotonic':str(owner.deadline)})
                        response=connection.getresponse();raw=response.read()
                        status,content_type=response.status,response.getheader('Content-Type','application/json')
                    finally:
                        connection.close();owner.connections.discard(connection)
                    with owner.intent_lock:
                        owner.intents[key].update(state='completed' if status==200 else 'unknown_or_failed',status=status)
                        if status==200:owner.intents[key].update(response=base64.b64encode(raw).decode(),content_type=content_type)
                        save()
                    owner.events.append({'path':self.path,'status':status,'request_bytes':len(body),'response_bytes':len(raw),'request_identity':key})
                    if status>=400:owner.errors.append({'path':self.path,'error':'broker_http_error','status':status})
                    _respond(self,status,raw,content_type)
                except (OSError,ValueError,http.client.HTTPException) as exc:
                    owner.errors.append({'path':self.path,'error':type(exc).__name__})
                    _respond(self,502,b'{"error":"evaluator_lower_transport_failure"}')

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

        def do_OPTIONS(self):
            _respond_preflight(self)

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
                connection.request('POST', self.path, self.rfile.read(size), headers={'Content-Type':'application/json',**{key:self.headers[key] for key in ('Idempotency-Key','X-Request-Id','X-AgentSWE-Request-Id','X-AgentSWE-Action') if self.headers.get(key)}})
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
        env.update(OPENAI_BASE_URL=endpoint, OPENAI_API_BASE=endpoint,
            DYAD_ENGINE_URL=endpoint, DYAD_BROKER_ENDPOINT=endpoint + '/responses')
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
