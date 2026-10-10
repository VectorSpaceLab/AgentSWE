"""Actual HTTP handler tests with the upstream transport replaced by a fixture."""
import io
import json
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer
from pathlib import Path
from unittest.mock import patch
from evaluator.broker import responses_broker as broker


class BrokerTests(unittest.TestCase):
    def test_gateway_credential_priority(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'fixture.env'
            path.write_text('OPENAI_API_KEY=wrong-fixture\nDEEPSEEK_API_KEY=gateway-fixture\n')
            self.assertEqual(broker.Ledger(path, 'https://api.deepseek.com/v1').credential(), 'gateway-fixture')

    def test_real_handler_forces_both_wire_protocols_and_placeholder(self):
        ledger = broker.Ledger(None, 'https://api.deepseek.com/v1')
        server = ThreadingHTTPServer(('127.0.0.1', 0), broker.make_handler(ledger))
        threading.Thread(target=server.serve_forever, daemon=True).start()
        original = urllib.request.urlopen
        captured = []
        class Response(io.BytesIO):
            status = 200
        def transport(request, *args, **kwargs):
            if request.full_url.startswith('http://127.0.0.1:'):
                return original(request, *args, **kwargs)
            captured.append({'url': request.full_url, 'body': json.loads(request.data),
                             'authorization': request.get_header('Authorization')})
            return Response(b'{"usage":{"input_tokens":7,"output_tokens":3,"total_tokens":10}}')
        try:
            with patch.object(urllib.request, 'urlopen', side_effect=transport), \
                 patch.object(ledger, 'credential', return_value='evaluator-fixture-key'):
                for path in ('/v1/responses', '/v1/chat/completions'):
                    request = urllib.request.Request(f'http://127.0.0.1:{server.server_port}{path}',
                        data=json.dumps({'model': 'wrong', 'reasoning': {'effort': 'high'},
                                         'reasoning_effort': 'max', 'stream': True}).encode(),
                        headers={'Authorization': 'Bearer broker-only-placeholder'})
                    with urllib.request.urlopen(request) as response:
                        self.assertEqual(response.status, 200)
                invalid = urllib.request.Request(f'http://127.0.0.1:{server.server_port}/v1/responses',
                    data=b'{}', headers={'Authorization': 'Bearer candidate-key'})
                with self.assertRaises(urllib.error.HTTPError) as error:
                    urllib.request.urlopen(invalid)
                self.assertEqual(error.exception.code, 401)
            self.assertEqual(len(captured), 2)
            self.assertEqual(captured[0]['url'], 'https://api.deepseek.com/v1/responses')
            self.assertEqual(captured[1]['url'], 'https://api.deepseek.com/v1/chat/completions')
            self.assertEqual(captured[0]['body']['reasoning'], {'effort': 'high'})
            self.assertNotIn('reasoning_effort', captured[0]['body'])
            self.assertEqual(captured[1]['body']['reasoning_effort'], 'high')
            self.assertNotIn('reasoning', captured[1]['body'])
            for item in captured:
                self.assertEqual(item['body']['model'], 'deepseek-flash')
                self.assertFalse(item['body']['stream'])
                self.assertEqual(item['authorization'], 'Bearer evaluator-fixture-key')
            self.assertEqual(ledger.stats()['runtime']['successful_calls'], 2)
        finally:
            server.shutdown()
            server.server_close()


if __name__ == '__main__':
    unittest.main()
