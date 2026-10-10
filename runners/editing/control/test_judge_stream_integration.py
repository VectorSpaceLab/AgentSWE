"""Real local SSE -> owned broker worker -> judge; no external provider."""
import http.client
import http.server
import json
from pathlib import Path
import socket
import tempfile
import time
import unittest
from unittest.mock import patch, Mock

import requests
import judge_broker_runtime as runtime
import result_judge as judge
import responses_stream as stream
import test_judge_broker_xhigh as fixtures
from test_responses_stream import body, event


class SSE(fixtures.Upstream):
    def do_POST(self):
        payload = json.loads(self.rfile.read(int(self.headers['Content-Length'])))
        self.server.requests.append({'body': payload, 'auth': self.headers.get('Authorization')})
        mode = self.server.mode
        try:
            if mode == 'retry' and len(self.server.requests) == 1:
                self.send_response(502); self.send_header('Content-Length', '0'); self.send_header('Retry-After', '0')
                self.end_headers(); return
            self.send_response(200); self.send_header('Content-Type', 'text/event-stream')
            self.send_header('Connection', 'close'); self.end_headers()
            self.wfile.write(event('response.created', response=body('in_progress'))); self.wfile.flush()
            if mode in ('hang', 'drop'):
                self.wfile.write(event('response.output_text.delta', delta=self.server.test_key, sequence=1)); self.wfile.flush()
                if mode == 'drop':
                    self.connection.shutdown(socket.SHUT_RDWR); return
                until = time.monotonic()+3
                while time.monotonic() < until:
                    self.wfile.write(b': heartbeat\n\n'); self.wfile.flush(); time.sleep(.02)
                return
            response = body('incomplete') if mode == 'incomplete' else body()
            if mode == 'wrong_model': response['model'] = 'wrong'
            if mode == 'echo': response['output'][0]['content'][0]['text'] = self.server.test_key
            self.wfile.write(event('response.'+response['status'], response=response, sequence=1)); self.wfile.flush()
            if mode == 'terminal_without_eof': time.sleep(1)
        except OSError:
            pass
        finally:
            self.close_connection = True


class IntegrationTests(unittest.TestCase):
    def call(self, endpoint, output, timeout=2):
        return judge.call_judge('synthetic', 'broker-only-placeholder', timeout, 4, endpoint=endpoint,
            response_path=output/'provider.json', transport_mode='stream')

    def stats(self, path, calls):
        value = fixtures.BrokerTests().wait_stats(path, calls)
        self.assertEqual(value['runtime']['in_flight_calls'], 0)
        return value

    def test_complete_stream_projected_to_exact_json_and_raw_bound(self):
        with fixtures.servers('terminal_without_eof') as (upstream, proxy, path, endpoint), tempfile.TemporaryDirectory() as tmp:
            upstream.RequestHandlerClass = SSE
            start = time.monotonic(); result = self.call(endpoint, Path(tmp))
            self.assertLess(time.monotonic()-start, .8)
            self.assertEqual(json.loads(result[0]), {'中文': 1})
            value = self.stats(path, 1); observed = value['attempts'][0]
            self.assertTrue(upstream.requests[0]['body']['stream'])
            self.assertEqual(value['runtime']['tokens'], 13); self.assertEqual(value['runtime']['usage_unknown_calls'], 0)
            self.assertIn(b'response.completed', Path(observed['provider_response_path']).read_bytes())
            self.assertEqual(observed['upstream_http_status'], 200)
            self.assertEqual(observed['downstream_http_status'], 200)
            self.assertEqual(value['protocol']['downstream_response_format'], 'terminal_json')

    def test_bad_or_incomplete_stream_is_not_resampled_and_usage_is_honest(self):
        for mode in ('incomplete', 'wrong_model', 'drop'):
            with self.subTest(mode=mode), fixtures.servers(mode) as (upstream, proxy, path, endpoint), tempfile.TemporaryDirectory() as tmp:
                upstream.RequestHandlerClass = SSE
                with self.assertRaises(judge.TransportFailure): self.call(endpoint, Path(tmp))
                value = self.stats(path, 1)
                self.assertEqual(len(upstream.requests), 1)
                self.assertEqual(value['runtime']['successful_calls'], 0)
                self.assertEqual(value['runtime']['usage_unknown_calls'], int(mode == 'drop'))
                self.assertEqual(value['runtime']['tokens'], 0 if mode == 'drop' else 13)

    def test_owned_deadline_kill_preserves_redacted_partial_bytes_and_http_headers(self):
        with fixtures.servers('hang', timeout=.3) as (upstream, proxy, path, endpoint), tempfile.TemporaryDirectory() as tmp:
            upstream.RequestHandlerClass = SSE
            with self.assertRaisesRegex(judge.TransportFailure, 'HTTP 598'): self.call(endpoint, Path(tmp))
            value = self.stats(path, 1); record = value['attempts'][0]
            captured = Path(record['provider_response_path']).read_bytes()
            self.assertIn(b'response.created', captured); self.assertNotIn(upstream.test_key.encode(), captured)
            self.assertEqual(record['upstream_http_status'], 200)
            self.assertEqual(value['runtime']['usage_unknown_calls'], 1)
            self.assertFalse(record['worker_transport_complete'])

    def test_one_outer_502_retry_means_two_actual_requests_and_same_mode(self):
        with fixtures.servers('retry') as (upstream, proxy, path, endpoint), tempfile.TemporaryDirectory() as tmp:
            upstream.RequestHandlerClass = SSE
            self.assertEqual(self.call(endpoint, Path(tmp))[1], 2)
            value = self.stats(path, 2)
            self.assertEqual([r['upstream_attempts'] for r in value['attempts']], [1, 1])
            self.assertEqual([r['upstream_http_status'] for r in value['attempts']], [502, 200])
            self.assertTrue(all(r['body']['stream'] for r in upstream.requests))

    def test_client_loss_reaps_worker_and_keeps_partial_response(self):
        with fixtures.servers('hang') as (upstream, proxy, path, endpoint):
            upstream.RequestHandlerClass = SSE
            payload = json.dumps({**fixtures.payload(), 'stream': True}).encode()
            connection = socket.create_connection(('127.0.0.1', proxy.server_port))
            connection.sendall(b'POST /v1/responses HTTP/1.1\r\nHost: localhost\r\nAuthorization: Bearer broker-only-placeholder\r\nContent-Length: '
                               + str(len(payload)).encode()+b'\r\n\r\n'+payload)
            until = time.monotonic()+2
            while time.monotonic() < until:
                observed = json.loads(path.read_text())['attempts']
                # The broker records an attempt in two steps (State.begin, then State.update adds the capture paths),
                # so a snapshot taken in between has no provider_response_path yet.
                capture = observed[0].get('provider_response_path') if observed else None
                if capture and Path(capture).is_file() and Path(capture).stat().st_size:
                    break
                time.sleep(.01)
            else: self.fail('no durable partial stream before disconnect')
            connection.shutdown(socket.SHUT_RDWR); connection.close()
            value = self.stats(path, 1)
            self.assertIn('client_disconnected', value['attempts'][0]['transport_abort_reason'])
            self.assertEqual(value['runtime']['upstream_attempts'], 1)

    def test_stream_source_and_fresh_protocol_are_required(self):
        with fixtures.servers() as (upstream, proxy, path, endpoint):
            value = json.loads(path.read_text())
            self.assertTrue(runtime.fresh_judge_stats(value))
            for key in ('response_transport_modes', 'downstream_response_format', 'connect_timeout_seconds_max'):
                changed = json.loads(json.dumps(value)); changed['protocol'].pop(key)
                self.assertFalse(runtime.fresh_judge_stats(changed))

    def test_connect_cap_restored_to_remaining_body_budget(self):
        for cls, parent in ((stream.BudgetHTTPConnection, http.client.HTTPConnection),
                            (stream.BudgetHTTPSConnection, http.client.HTTPSConnection)):
            for budget in (2, 900):
                observed = []
                def connected(connection):
                    observed.append(connection.timeout); connection.sock = Mock()
                connection = cls('example.invalid', timeout=budget)
                with patch.object(parent, 'connect', connected): connection.connect()
                self.assertEqual(observed, [min(30, budget)])
                self.assertEqual(connection.timeout, budget); connection.sock.settimeout.assert_called_once_with(budget)
                with patch.object(parent, 'connect', side_effect=TimeoutError()), self.assertRaises(stream.SafeConnectTimeout):
                    connection.connect()


if __name__ == '__main__': unittest.main()
