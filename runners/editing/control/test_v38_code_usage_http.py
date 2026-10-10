"""Actual localhost HTTP through the shared transport; no real credentials."""
import json
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
import tempfile
import threading
from types import SimpleNamespace
import unittest

import code_judge_entry as entry


class JudgeTransportError(RuntimeError):
    def __init__(self, message, attempts, endpoints):
        super().__init__(message)


class CodeUsageHttpTests(unittest.TestCase):
    def exercise(self, responses):
        observed = []

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def do_POST(self):
                observed.append(json.loads(self.rfile.read(int(self.headers['Content-Length']))))
                status, value = responses[len(observed) - 1]
                data = json.dumps(value).encode()
                self.send_response(status)
                self.send_header('Content-Type', 'application/json')
                self.send_header('Content-Length', str(len(data)))
                self.send_header('Retry-After', '0')
                self.end_headers()
                self.wfile.write(data)

        server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
        worker = threading.Thread(target=server.serve_forever, daemon=True)
        worker.start()
        try:
            with tempfile.TemporaryDirectory() as directory:
                create = SimpleNamespace(ENDPOINTS=(f'http://127.0.0.1:{server.server_port}/v1/responses',),
                    MODEL='deepseek-flash', REASONING_EFFORT='max', MAX_ATTEMPTS=2,
                    JudgeTransportError=JudgeTransportError)
                entry.install_transport(create, Path(directory))
                failed = False
                try:
                    create.call_judge('synthetic transport accounting check', 'not-a-real-key', 10)
                except JudgeTransportError:
                    failed = True
                ledger = json.loads((Path(directory) / 'code_transport_ledger.json').read_text())
                attempts = json.loads((Path(directory) / 'code_provider_response-attempts.json').read_text())
                with self.assertRaisesRegex(RuntimeError, 'second logical'):
                    create.call_judge('must not generate again', 'not-a-real-key', 10)
                return failed, ledger, attempts, len(observed)
        finally:
            server.shutdown()
            server.server_close()
            worker.join(timeout=2)

    def completion(self, usage):
        return {'object': 'response', 'id': 'synthetic-code-accounting',
                'status': 'completed', 'model': 'deepseek-flash',
                'output_text': 'synthetic text', 'usage': usage}

    def test_real_http_403_does_not_report_zero_or_retry(self):
        failed, ledger, attempts, calls = self.exercise([(403, {'error': 'synthetic access denied'})])
        self.assertTrue(failed)
        self.assertEqual(calls, 1)
        self.assertEqual(attempts['attempts'][0]['http_status'], 403)
        self.assertIsNone(ledger['total_tokens'])
        self.assertEqual(ledger['unknown_usage_attempts'], 1)

    def test_completed_last_response_does_not_erase_unknown_429_prefix(self):
        usage = {'input_tokens': 30, 'output_tokens': 12, 'total_tokens': 42}
        failed, ledger, attempts, calls = self.exercise([
            (429, {'error': 'synthetic throttling'}), (200, self.completion(usage))])
        self.assertFalse(failed)
        self.assertEqual(calls, 2)
        self.assertEqual(ledger['completed_responses'], 1)
        self.assertEqual(ledger['known_total_tokens'], 42)
        self.assertIsNone(ledger['total_tokens'])
        self.assertEqual(ledger['unknown_usage_attempts'], 1)

    def test_valid_completed_usage_is_exact(self):
        failed, ledger, attempts, calls = self.exercise([
            (200, self.completion({'input_tokens': 30, 'output_tokens': 12, 'total_tokens': 42}))])
        self.assertFalse(failed)
        self.assertEqual(calls, 1)
        self.assertEqual(ledger['total_tokens'], 42)
        self.assertEqual(ledger['known_total_tokens'], 42)
        self.assertTrue(ledger['usage_complete'])

    def test_partial_usage_does_not_fill_missing_fields_with_zero(self):
        failed, ledger, attempts, calls = self.exercise([
            (200, self.completion({'input_tokens': 30}))])
        self.assertFalse(failed)
        self.assertEqual(calls, 1)
        for field in entry.TOKEN_FIELDS:
            self.assertIsNone(ledger[field])
        self.assertEqual(ledger['unknown_usage_attempts'], 1)


if __name__ == '__main__':
    unittest.main()
