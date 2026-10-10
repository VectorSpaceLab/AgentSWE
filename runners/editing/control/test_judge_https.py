"""Local TLS with certificate verification and real subprocess HTTP workers.

No external requests or real API credentials. OpenSSL generates ephemeral test
certificates. Never disable SSL verification to make the positive path pass.
"""
import contextlib
import http.server
import json
import os
from pathlib import Path
import socket
import ssl
import subprocess
import tempfile
import threading
import unittest
from unittest.mock import patch

import judge_broker_xhigh as broker
import responses_stream as stream
import result_judge as judge
from test_judge_stream_integration import SSE
import test_judge_broker_xhigh as fixtures


def certificate(directory, name, san):
    cert, key = directory/(name+'.pem'), directory/(name+'.key')
    subprocess.run(['openssl', 'req', '-x509', '-newkey', 'rsa:2048', '-nodes',
        '-keyout', str(key), '-out', str(cert), '-days', '1', '-subj', '/CN='+name,
        '-addext', 'subjectAltName='+san], check=True, stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL, timeout=15)
    return cert, key


@contextlib.contextmanager
def tls_server(cert, key, mode='valid'):
    server = http.server.ThreadingHTTPServer(('127.0.0.1', 0), SSE if mode != 'redirect' else fixtures.Upstream)
    server.daemon_threads = True
    server.requests, server.mode, server.test_key = [], mode, 'synthetic-tls-only-key'
    server.url = f'https://localhost:{server.server_port}/v1/responses'
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    context.load_cert_chain(cert, key)
    server.socket = context.wrap_socket(server.socket, server_side=True)
    thread = threading.Thread(target=server.serve_forever, kwargs={'poll_interval': .02}, daemon=True)
    thread.start()
    try:
        yield server
    finally:
        server.shutdown(); server.server_close(); thread.join(2)


@contextlib.contextmanager
def proxy_server(endpoint, directory):
    path = directory/'stats.json'
    proxy = broker.Server(('127.0.0.1', 0), key='synthetic-tls-only-key',
        endpoint=endpoint, stats_path=path, timeout=4, keepalive=.05)
    thread = threading.Thread(target=proxy.serve_forever, kwargs={'poll_interval': .02}, daemon=True)
    thread.start()
    try:
        yield f'http://127.0.0.1:{proxy.server_port}/v1/responses', path
    finally:
        proxy.shutdown(); proxy.stop_owned_workers(); proxy.server_close(); thread.join(2)


class HTTPSTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        cls.directory = Path(cls.tmp.name)
        cls.cert, cls.key = certificate(cls.directory, 'localhost', 'DNS:localhost')
        cls.other_cert, _ = certificate(cls.directory, 'other', 'DNS:other.invalid')

    @classmethod
    def tearDownClass(cls):
        cls.tmp.cleanup()

    def call(self, endpoint, directory):
        return judge.call_judge('synthetic TLS test', 'broker-only-placeholder', 4, 4,
            endpoint=endpoint, response_path=directory/'provider.json', transport_mode='stream')

    def test_secure_context_has_no_removed_handler_attribute_dependency(self):
        handler = stream._HTTPS()
        self.assertTrue(handler._context.check_hostname)
        self.assertEqual(handler._context.verify_mode, ssl.CERT_REQUIRED)
        with patch.object(handler, 'do_open', return_value='ok') as opened:
            self.assertEqual(handler.https_open('synthetic-request'), 'ok')
        self.assertNotIn('check_hostname', opened.call_args.kwargs)
        self.assertIs(opened.call_args.kwargs['context'], handler._context)

    def test_direct_tls_stream_retains_exact_terminal(self):
        with patch.dict(os.environ, {'SSL_CERT_FILE': str(self.cert)}), tls_server(self.cert, self.key) as upstream, tempfile.TemporaryDirectory() as tmp:
            value = self.call(upstream.url, Path(tmp))
            self.assertEqual(json.loads(value[0]), {'中文': 1})
            self.assertEqual(len(upstream.requests), 1)
            self.assertTrue(upstream.requests[0]['body']['stream'])

    def test_real_worker_tls_stream_and_usage(self):
        with patch.dict(os.environ, {'SSL_CERT_FILE': str(self.cert)}), tls_server(self.cert, self.key) as upstream, tempfile.TemporaryDirectory() as tmp:
            directory = Path(tmp)
            with proxy_server(upstream.url, directory) as (endpoint, path):
                self.assertEqual(json.loads(self.call(endpoint, directory)[0]), {'中文': 1})
                value = fixtures.BrokerTests().wait_stats(path)
                self.assertEqual(len(upstream.requests), 1)
                self.assertEqual(value['runtime']['upstream_attempts'], 1)
                self.assertEqual(value['runtime']['usage_unknown_calls'], 0)
                self.assertEqual(value['runtime']['tokens'], 13)
                self.assertTrue(value['attempts'][0]['request_start_observed'])

    def rejected_certificate(self, trusted, hostname):
        with patch.dict(os.environ, {'SSL_CERT_FILE': str(trusted)}), tls_server(self.cert, self.key) as upstream, tempfile.TemporaryDirectory() as tmp:
            directory = Path(tmp)
            target = upstream.url.replace('localhost', hostname)
            with proxy_server(target, directory) as (endpoint, path):
                with self.assertRaisesRegex(judge.TransportFailure, 'HTTP 598'):
                    self.call(endpoint, directory)
                value = fixtures.BrokerTests().wait_stats(path)
                self.assertEqual(upstream.requests, [])
                self.assertEqual(value['runtime']['calls'], 1)
                self.assertEqual(value['runtime']['upstream_attempts'], 0)
                self.assertEqual(value['runtime']['usage_unknown_calls'], 0)
                self.assertEqual(value['runtime']['tokens'], 0)
                self.assertTrue(value['attempts'][0]['upstream_submission_proven_absent'])
                self.assertEqual(value['attempts'][0]['failure_phase'], 'before_http_send')

    def test_untrusted_certificate_rejected_before_http(self):
        self.rejected_certificate(self.other_cert, 'localhost')

    def test_wrong_hostname_rejected_before_http(self):
        self.rejected_certificate(self.cert, '127.0.0.1')

    def test_tls_response_loss_remains_unknown_and_is_not_retried(self):
        with patch.dict(os.environ, {'SSL_CERT_FILE': str(self.cert)}), tls_server(self.cert, self.key, 'drop') as upstream, tempfile.TemporaryDirectory() as tmp:
            directory = Path(tmp)
            with proxy_server(upstream.url, directory) as (endpoint, path):
                with self.assertRaises(judge.TransportFailure): self.call(endpoint, directory)
                value = fixtures.BrokerTests().wait_stats(path)
                self.assertEqual(len(upstream.requests), 1)
                self.assertEqual(value['runtime']['upstream_attempts'], 1)
                self.assertEqual(value['runtime']['usage_unknown_calls'], 1)
                self.assertTrue(value['attempts'][0]['request_start_observed'])

    def test_https_redirect_never_sends_a_second_request(self):
        with patch.dict(os.environ, {'SSL_CERT_FILE': str(self.cert)}), tls_server(self.cert, self.key, 'redirect') as upstream, tempfile.TemporaryDirectory() as tmp:
            directory = Path(tmp)
            with proxy_server(upstream.url, directory) as (endpoint, path):
                with self.assertRaisesRegex(judge.TransportFailure, 'HTTP 307'):
                    self.call(endpoint, directory)
                value = fixtures.BrokerTests().wait_stats(path)
                self.assertEqual(len(upstream.requests), 1)
                self.assertEqual(value['runtime']['upstream_attempts'], 1)

    def test_https_502_retries_only_at_outer_layer(self):
        with patch.dict(os.environ, {'SSL_CERT_FILE': str(self.cert)}), tls_server(self.cert, self.key, 'retry') as upstream, tempfile.TemporaryDirectory() as tmp:
            directory = Path(tmp)
            with proxy_server(upstream.url, directory) as (endpoint, path):
                result = self.call(endpoint, directory)
                self.assertEqual(result[1], 2)
                value = fixtures.BrokerTests().wait_stats(path, 2)
                self.assertEqual(len(upstream.requests), 2)
                self.assertEqual(value['runtime']['upstream_attempts'], 2)
                self.assertEqual([r['upstream_http_status'] for r in value['attempts']], [502, 200])

    def test_send_marker_is_after_tls_connect_and_only_once(self):
        observed = []
        class FakeSocket:
            def sendall(self, data): observed.append(('bytes', data))
        def connect(connection):
            observed.append('connected'); connection.sock = FakeSocket()
        connection = stream.BudgetHTTPSConnection('example.invalid', timeout=4,
            on_request_start=lambda: observed.append('marker'))
        with patch.object(stream.BudgetHTTPSConnection, 'connect', connect):
            connection.send(b'headers'); connection.send(b'body')
        self.assertEqual(observed, ['connected', 'marker', ('bytes', b'headers'), ('bytes', b'body')])
        observed.clear()
        connection = stream.BudgetHTTPSConnection('example.invalid', timeout=4,
            on_request_start=lambda: observed.append('marker'))
        with patch.object(stream.BudgetHTTPSConnection, 'connect', side_effect=AttributeError('synthetic-before-network')):
            with self.assertRaises(AttributeError): connection.send(b'headers')
        self.assertEqual(observed, [])


if __name__ == '__main__': unittest.main()
