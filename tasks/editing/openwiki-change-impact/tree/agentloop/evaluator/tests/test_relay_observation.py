"""Real local socket tests: observation must not change broker delivery."""
import http.server
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import patch

from agentloop.evaluator.transport_sandbox import FixedLowerRelay, UnixConnection


class FailingStore:
    mode = 'record'

    def __init__(self, *args, **kwargs):
        self.records = []

    def begin(self, body):
        if self.mode == 'begin':
            raise OSError('synthetic evidence storage failure')
        return self

    def feed_response(self, raw):
        if self.mode == 'feed':
            raise ValueError('synthetic observer parse failure')

    def record(self, *args, **kwargs):
        raise OSError('synthetic evidence write failure')

    def close(self, **kwargs):
        return {'complete': False, 'sealed': True, 'records': [],
                'errors': ['synthetic evidence failure']}


class RelayObservationTests(unittest.TestCase):
    def test_observer_faults_preserve_actual_http_status_body_and_content_type(self):
        raw = b'data: {"type":"response.completed","response":{"id":"r","status":"completed","output":[]}}\n\n'
        requests = []

        class Broker(http.server.BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def do_POST(self):
                requests.append((self.path, self.rfile.read(int(self.headers['Content-Length']))))
                self.send_response(200)
                self.send_header('Content-Type', 'text/event-stream')
                self.send_header('Content-Length', str(len(raw)))
                self.end_headers()
                self.wfile.write(raw)

        broker = http.server.ThreadingHTTPServer(('127.0.0.1', 0), Broker)
        worker = threading.Thread(target=broker.serve_forever, daemon=True)
        worker.start()
        try:
            for failure in ('begin', 'feed', 'record'):
                with self.subTest(failure=failure), tempfile.TemporaryDirectory() as directory:
                    FailingStore.mode = failure
                    with patch('agentloop.evaluator.broker_observation.ObservationStore', FailingStore):
                        relay = FixedLowerRelay(f'http://127.0.0.1:{broker.server_port}/v1/responses',
                            context_id='synthetic-no-provider', observation_dir=Path(directory)).start()
                    try:
                        client = UnixConnection(relay.socket_path, timeout=5)
                        client.request('POST', '/v1/responses', b'{"input":"test"}')
                        response = client.getresponse()
                        self.assertEqual(response.status, 200)
                        self.assertEqual(response.getheader('Content-Type'), 'text/event-stream')
                        self.assertEqual(response.read(), raw)
                        client.close()
                    finally:
                        summary = relay.close()
                    self.assertFalse(summary['complete'])
                    self.assertTrue(relay.errors)
                    self.assertEqual(summary['relay_pending_handlers'], 0)
            self.assertEqual(len(requests), 3)
            self.assertEqual({body for _, body in requests}, {b'{"input":"test"}'})
        finally:
            broker.shutdown()
            broker.server_close()
            worker.join(3)


if __name__ == '__main__':
    unittest.main()
