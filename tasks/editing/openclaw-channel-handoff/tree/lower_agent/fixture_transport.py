"""Fixed native-service transport; no Gateway operations or recovery policy.

Only the evaluator-side relay holds synthetic provider credentials. Products
see role-specific placeholders, loopback URLs and individual relay sockets.
This is deliberately separate from the lower-model/Result transport.
"""
from __future__ import annotations

import argparse
import hashlib
import http.client
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import math
import os
from pathlib import Path
import signal
import socket
import socketserver
import subprocess
import tempfile
import threading
import time

ROUTES = {
    'effect': frozenset({'/effects'}),
    'channel': frozenset({'/v1/messages', '/v1/receipts'}),
    'media': frozenset({'/v1/uploads', '/v1/upload-chunks', '/v1/upload-finalize', '/v1/upload-status'}),
    'interaction': frozenset({'/v1/interactions/verify', '/v1/interactions/status'}),
}
PREFIXES = {'effect': 'EFFECT_SINK', 'channel': 'CHANNEL_CONNECTOR',
            'media': 'MEDIA_CONNECTOR', 'interaction': 'INTERACTION_CONNECTOR'}
HEALTH = '/agentswe/native-transport-health'
ORIGIN_HEADER = 'X-AgentSWE-Native-Origin'
MAX_BODY = 256 * 1024  # Above every frozen native request; not an Agent budget.


def placeholder(role):
    if role not in ROUTES:
        raise ValueError('unknown native service')
    return 'native-' + role + '-placeholder'


class UnixConnection(http.client.HTTPConnection):
    def __init__(self, path, timeout):
        super().__init__('localhost', timeout=timeout)
        self.path = str(path)

    def connect(self):
        self.sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.sock.settimeout(self.timeout)
        self.sock.connect(self.path)


class UnixServer(socketserver.ThreadingMixIn, socketserver.UnixStreamServer):
    daemon_threads = True
    block_on_close = False


class Handler(BaseHTTPRequestHandler):
    protocol_version = 'HTTP/1.1'

    def log_message(self, *_):
        pass

    def setup(self):
        super().setup()
        self.connection.settimeout(max(.001, self.server.deadline - time.monotonic()))
        with self.server.lock:
            self.server.clients.add(self.connection)

    def finish(self):
        try:
            super().finish()
        finally:
            with self.server.lock:
                self.server.clients.discard(self.connection)

    def reply(self, status, value):
        data = json.dumps(value).encode()
        self.send_response(status)
        self.send_header('Content-Type', 'application/json')
        self.send_header('Content-Length', str(len(data)))
        self.send_header('Connection', 'close')
        self.end_headers()
        self.wfile.write(data)
        self.close_connection = True

    def do_GET(self):
        if self.path != HEALTH:
            return self.reply(404, {'error': 'native_route_not_exposed'})
        # Open only the fixed UDS, without a business request. It confirms
        # listener reachability, not product correctness or provider auth.
        check = UnixConnection(self.server.upstream, self.remaining())
        try:
            if self.server.is_host:
                check.connect()
            else:
                check.request('GET', HEALTH)
                response = check.getresponse()
                value = json.loads(response.read())
                if response.status != 200 or value.get('role') != self.server.role:
                    raise RuntimeError('native relay role preflight failed')
            self.reply(200, {'role': self.server.role, 'business_request': False})
        except (OSError, ValueError, http.client.HTTPException, RuntimeError):
            self.reply(503, {'error': 'fixed_native_listener_unavailable'})
        finally:
            check.close()

    def remaining(self):
        value = self.server.deadline - time.monotonic()
        if value <= 0:
            raise TimeoutError('native case deadline exhausted')
        return value

    def do_POST(self):
        self.close_connection = True
        role = self.server.role
        if self.path not in ROUTES[role]:
            return self.reply(404, {'error': 'native_route_not_exposed'})
        if role != 'effect' and self.headers.get('Authorization') != 'Bearer ' + placeholder(role):
            return self.reply(401, {'error': 'native_role_placeholder_required'})
        lengths = self.headers.get_all('Content-Length', [])
        if len(lengths) != 1 or self.headers.get('Transfer-Encoding'):
            return self.reply(400, {'error': 'one_content_length_required'})
        try:
            length = int(lengths[0])
            if not 0 <= length <= MAX_BODY:
                return self.reply(413, {'error': 'native_request_size_invalid'})
            body = self.rfile.read(length)
            if len(body) != length:
                return self.reply(400, {'error': 'native_request_incomplete'})
        except (ValueError, OSError):
            return self.reply(400, {'error': 'native_request_invalid'})
        event = {'role': role, 'method': 'POST', 'path': self.path,
                 'request_sha256': hashlib.sha256(body).hexdigest(),
                 'attempts': 0, 'complete': False, 'status': None,
                 'gateway_origin_verified': False}
        connection = UnixConnection(self.server.upstream, self.remaining())
        with self.server.lock:
            self.server.upstreams.add(connection)
        try:
            headers = {'Content-Type': self.headers.get('Content-Type', 'application/json')}
            if role != 'effect':
                token = self.server.token if self.server.is_host else placeholder(role)
                headers['Authorization'] = 'Bearer ' + token
            # Ignore any client-provided origin header. Only this host-side
            # relay has the evaluator credential for its private namespace.
            if self.server.is_host and self.server.origin_credential is not None:
                headers[ORIGIN_HEADER] = self.server.origin_credential
            # Preserve native idempotency exactly; never create/rewrite IDs,
            # bodies, task routes, capability/grant or recovery instructions.
            idem = self.headers.get_all('Idempotency-Key', [])
            if len(idem) > 1:
                return self.reply(400, {'error': 'ambiguous_idempotency_header'})
            if idem:
                headers['Idempotency-Key'] = idem[0]
                event['idempotency_sha256'] = hashlib.sha256(idem[0].encode()).hexdigest()
            event['attempts'] = 1
            connection.request('POST', self.path, body=body, headers=headers)
            response = connection.getresponse()
            event['status'] = response.status
            data = response.read(MAX_BODY + 1)
            if len(data) > MAX_BODY or response.length not in (None, 0):
                raise http.client.IncompleteRead(data)
            self.send_response(response.status)
            self.send_header('Content-Type', response.getheader('Content-Type', 'application/json'))
            self.send_header('Content-Length', str(len(data)))
            self.send_header('Connection', 'close')
            self.end_headers()
            self.wfile.write(data)
            event['response_sha256'] = hashlib.sha256(data).hexdigest()
            event['complete'] = True
        except (OSError, TimeoutError, http.client.HTTPException):
            # Acceptance-with-lost-response is an actual native case fault.
            # Preserve disconnection; don't synthesize a retryable response,
            # retry, repair, or automatically classify it as infrastructure.
            event['connection_interrupted'] = True
        finally:
            connection.close()
            with self.server.lock:
                self.server.upstreams.discard(connection)
                self.server.events.append(event)


class NativeRelay:
    def __init__(self, *, role, upstream, deadline, token=None, port=None, origin_credential=None):
        if role not in ROUTES or not math.isfinite(deadline) or deadline <= time.monotonic():
            raise ValueError('invalid native role/deadline')
        upstream = Path(upstream)
        if not upstream.is_socket():
            raise ValueError('native service must be an existing Unix socket')
        if port is None and role != 'effect' and (not isinstance(token, str) or not token):
            raise ValueError('evaluator synthetic service token required')
        if origin_credential is not None and (port is not None or not isinstance(origin_credential,str)
                                               or len(origin_credential) < 32):
            raise ValueError('native origin credential belongs only to the evaluator host relay')
        self.temporary = None
        if port is None:
            self.temporary = tempfile.TemporaryDirectory(prefix='ocn-')
            self.socket_path = Path(self.temporary.name) / 'relay.sock'
            server = UnixServer(str(self.socket_path), Handler)
        else:
            server = ThreadingHTTPServer(('127.0.0.1', port), Handler)
            server.daemon_threads = True
            server.block_on_close = False
        self.server = server
        server.role, server.upstream, server.deadline = role, upstream, deadline
        server.is_host, server.token = port is None, token
        server.origin_credential = origin_credential
        server.lock = threading.Lock()
        server.clients, server.upstreams, server.events = set(), set(), []
        self.stop_event = threading.Event()
        self.closed = False
        self.thread = threading.Thread(target=server.serve_forever,
            kwargs={'poll_interval': .05}, daemon=True)
        self.thread.start()
        self.watchdog = threading.Thread(target=self.expire, daemon=True)
        self.watchdog.start()

    def cancel_connections(self):
        with self.server.lock:
            sockets = list(self.server.clients) + [c.sock for c in self.server.upstreams if c.sock]
        for item in sockets:
            try:
                item.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass

    def expire(self):
        if not self.stop_event.wait(max(0, self.server.deadline - time.monotonic())):
            self.cancel_connections()

    def snapshot(self):
        with self.server.lock:
            return {'role': self.server.role, 'events': list(self.server.events),
                    'active_clients': len(self.server.clients),
                    'active_upstreams': len(self.server.upstreams),
                    'retries': 0, 'redirects_followed': 0, 'gateway_operations': 0,
                    'closed': self.closed}

    def close(self):
        if self.closed:
            return self.snapshot()
        self.stop_event.set()
        self.cancel_connections()
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=1)
        self.watchdog.join(timeout=1)
        # Handler completion is tracked independently of listener shutdown.
        end = time.monotonic() + 1
        while time.monotonic() < end:
            with self.server.lock:
                active = len(self.server.clients) + len(self.server.upstreams)
            if not active:
                break
            time.sleep(.01)
        if self.temporary:
            self.temporary.cleanup()
        self.closed = True
        return self.snapshot()


def inside_main(argv=None):
    parser = argparse.ArgumentParser()
    parser.add_argument('--deadline', type=float, required=True)
    parser.add_argument('--service', nargs=3, action='append', default=[], metavar=('ROLE', 'SOCKET', 'PORT'))
    parser.add_argument('command', nargs=argparse.REMAINDER)
    args = parser.parse_args(argv)
    command = args.command[1:] if args.command[:1] == ['--'] else args.command
    if not command or len({s[0] for s in args.service}) != len(args.service):
        raise ValueError('native wrapper requires a command and unique service roles')
    bridges, process = [], None
    previous = None
    try:
        for role, path, port in args.service:
            check = UnixConnection(path, min(5, args.deadline - time.monotonic()))
            try:
                check.request('GET', HEALTH)
                response = check.getresponse()
                if response.status != 200 or json.loads(response.read()).get('role') != role:
                    raise RuntimeError('native fixed service preflight failed')
            finally:
                check.close()
            bridges.append(NativeRelay(role=role, upstream=path, deadline=args.deadline, port=int(port)))
        process = subprocess.Popen(command, close_fds=True)
        def stop(_signal, _frame):
            if process.poll() is None:
                process.terminate()
        previous = signal.signal(signal.SIGTERM, stop)
        return process.wait(timeout=max(.001, args.deadline - time.monotonic()))
    finally:
        if process is not None:
            if process.poll() is None:
                process.kill()
            process.wait(timeout=1)
        for bridge in reversed(bridges):
            bridge.close()
        if previous is not None:
            signal.signal(signal.SIGTERM, previous)


if __name__ == '__main__':
    raise SystemExit(inside_main())
