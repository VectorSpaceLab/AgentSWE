"""Provider-free: an upstream transport that never reached the provider is retried.

No network, no credential, no provider call. A stub upstream serves a completed
Responses body; the first attempts are forced to fail before ``on_request_start``
fires, which is exactly the ``transport_attempts=0``/``not_submitted`` class that
made up 34 of the 50 recorded lower failures.
"""
from __future__ import annotations

import json
import sys
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.append('@@AGENTSWE_EDITING_CONTROL@@')
import candidate_broker as cb

COMPLETED = {'id': 'resp_stub_1', 'object': 'response', 'status': 'completed', 'model': cb.MODEL,
             'output': [{'type': 'message', 'role': 'assistant',
                         'content': [{'type': 'output_text', 'text': 'ok'}]}],
             'usage': {'input_tokens': 3, 'output_tokens': 2, 'total_tokens': 5}}


class Upstream(BaseHTTPRequestHandler):
    def log_message(self, *a): pass

    def do_POST(self):
        self.rfile.read(int(self.headers.get('Content-Length', '0')))
        body = json.dumps(COMPLETED).encode()
        self.send_response(200)
        self.send_header('Content-Type', 'application/json')
        self.send_header('Content-Length', str(len(body)))
        self.end_headers()
        self.wfile.write(body)


class UnsentRetryTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.upstream = ThreadingHTTPServer(('127.0.0.1', 0), Upstream)
        threading.Thread(target=self.upstream.serve_forever, daemon=True).start()
        self.addCleanup(self.upstream.shutdown)
        self.addCleanup(self.tmp.cleanup)
        url = 'http://127.0.0.1:%d' % self.upstream.server_address[1]
        ledger = cb.Stats(self.root / 'lower.json', effort=cb.EFFORT, role='lower')
        self.broker = cb.BrokerServer(('127.0.0.1', 0), url, 'test-credential-not-real', ledger)
        self.state = self.broker.state
        threading.Thread(target=self.broker.serve_forever, daemon=True).start()
        self.addCleanup(self.broker.shutdown)
        self.endpoint = 'http://127.0.0.1:%d/v1/responses' % self.broker.server_address[1]

    def _fail_before_send(self, times):
        """Force the first ``times`` attempts to fail at connect, before any send."""
        real = cb.deadline_opener
        state = {'n': 0}

        @contextmanager
        def flaky(deadline, on_request_start, deadline_killed=None):
            state['n'] += 1
            if state['n'] <= times:
                raise urllib.error.URLError('simulated connect failure before send')
            with real(deadline, on_request_start, deadline_killed) as opener:
                yield opener

        cb.deadline_opener = flaky
        self.addCleanup(lambda: setattr(cb, 'deadline_opener', real))

    def _call(self):
        request = urllib.request.Request(
            self.endpoint, method='POST',
            headers={'Authorization': 'Bearer ' + cb.INBOUND_PLACEHOLDER,
                     'Content-Type': 'application/json',
                     'X-AgentSWE-Context': 'dev_001/case-1'},
            data=json.dumps({'input': [{'role': 'user', 'content': 'hi'}]}).encode())
        try:
            response = urllib.request.urlopen(request, timeout=30)
        except urllib.error.HTTPError as exc:
            response = exc
        with response:
            return response.status, json.loads(response.read())

    def test_unsent_failures_are_retried_and_stay_one_upstream_attempt(self):
        cb.UNSENT_RETRY_BACKOFF_SECONDS = 0.01
        self._fail_before_send(2)
        status, body = self._call()
        self.assertEqual(status, 200)
        self.assertEqual(body['status'], 'completed')
        stats = self.state.stats()
        runtime = stats['runtime']
        self.assertEqual(runtime['unsent_transport_retries'], 2)
        self.assertEqual((runtime['calls'], runtime['successful_calls'], runtime['failures']), (1, 1, 0))
        self.assertEqual(runtime['unknown_usage_calls'], 0)
        row = stats['requests'][0]
        # The retried attempts never started a transport, so the durable intent
        # still records exactly one real upstream attempt.
        self.assertEqual(row['transport_attempts'], 1)
        self.assertEqual(row['upstream_completion'], 'completed')
        self.assertIs(row['ok'], True)

    def test_retry_budget_is_bounded_and_failure_still_fails_closed(self):
        cb.UNSENT_RETRY_BACKOFF_SECONDS = 0.01
        self._fail_before_send(cb.UNSENT_MAX_RETRIES + 1)
        status, _ = self._call()
        self.assertEqual(status, 502)
        stats = self.state.stats()
        runtime = stats['runtime']
        self.assertEqual(runtime['unsent_transport_retries'], cb.UNSENT_MAX_RETRIES)
        self.assertEqual(runtime['pre_transport_failures'], 1)
        self.assertEqual(runtime['successful_calls'], 0)
        row = stats['requests'][0]
        self.assertIs(row['ok'], False)
        self.assertEqual(row['transport_attempts'], 0)
        self.assertEqual(row['upstream_completion'], 'not_submitted')
        # An exhausted budget must still be inadmissible, not silently tolerated.
        from v2_extra_usage_normalizers import make_extra_lower_normalizer
        with self.assertRaises(ValueError):
            make_extra_lower_normalizer('dyad')(stats, row['request_sha256'])

    def test_recovered_ledger_still_passes_the_v2_admission_normalizer(self):
        cb.UNSENT_RETRY_BACKOFF_SECONDS = 0.01
        self._fail_before_send(2)
        self._call()
        stats = self.state.stats()
        from v2_extra_usage_normalizers import make_extra_lower_normalizer
        normalize = make_extra_lower_normalizer('dyad')
        request_id = stats['requests'][0]['request_sha256']
        record = normalize(stats, request_id)
        self.assertEqual(record['state'], 'success')
        self.assertIs(record['usage_known'], True)
        self.assertEqual(record['upstream_attempts'], 1)


if __name__ == '__main__':
    unittest.main()
