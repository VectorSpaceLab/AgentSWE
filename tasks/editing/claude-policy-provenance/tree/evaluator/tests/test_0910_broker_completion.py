"""Actual broker HTTP request path, one local synthetic upstream, no provider."""
import http.server
import json
import threading
import unittest
import urllib.error
import urllib.request
from agentloop.evaluator import broker


class BrokerCompletionTests(unittest.TestCase):
    def measure(self,payload,status=200):
        attempts=[]
        class Upstream(http.server.BaseHTTPRequestHandler):
            def log_message(self,*_): pass
            def do_POST(self):
                attempts.append(json.loads(self.rfile.read(int(self.headers['Content-Length']))))
                raw=json.dumps(payload).encode();self.send_response(status)
                self.send_header('Content-Length',str(len(raw)));self.end_headers();self.wfile.write(raw)
        upstream=http.server.ThreadingHTTPServer(('127.0.0.1',0),Upstream)
        endpoint=http.server.ThreadingHTTPServer(('127.0.0.1',0),broker.Handler)
        endpoint.state=broker.BrokerState();endpoint.provider_url=f'http://127.0.0.1:{upstream.server_port}/v1/responses'
        endpoint.provider_key='synthetic-evaluator-only-placeholder'
        for server in (upstream,endpoint):threading.Thread(target=server.serve_forever,daemon=True).start()
        try:
            req=urllib.request.Request(f'http://127.0.0.1:{endpoint.server_port}/v1/responses',data=b'{"input":"synthetic prompt"}',headers={'Authorization':'Bearer broker-only-placeholder'})
            try:
                with urllib.request.urlopen(req,timeout=10) as response: code=response.status;response.read()
            except urllib.error.HTTPError as exc: code=exc.code
            return code,attempts,endpoint.state.stats()
        finally:
            for server in (upstream,endpoint):server.shutdown();server.server_close()

    def test_parseable_partial_is_not_success_or_known_zero_usage(self):
        code,attempts,stats=self.measure({'object':'response','status':'in_progress','output_text':'partial'})
        self.assertEqual(code,502);self.assertEqual(len(attempts),1)
        self.assertEqual(stats['runtime']['completed_responses'],0)
        self.assertEqual(stats['runtime']['unknown_usage_requests'],1)
        self.assertFalse(stats['runtime']['usage_complete'])
        self.assertIsNone(stats['request_ledger'][0]['usage'])

    def test_gateway_failure_is_one_unknown_attempt_without_retry(self):
        code,attempts,stats=self.measure({'error':'synthetic gateway timeout'},524)
        self.assertEqual(code,502);self.assertEqual(len(attempts),1)
        self.assertEqual(stats['runtime']['upstream_transport_attempts'],1)
        self.assertEqual(stats['runtime']['unknown_usage_requests'],1)

    def test_completed_response_retains_forced_protocol_and_actual_usage(self):
        code,attempts,stats=self.measure({'object':'response','status':'completed','id':'synthetic-response',
            'output_text':'authored synthetic result','usage':{'input_tokens':7,'output_tokens':3,'total_tokens':10}})
        self.assertEqual(code,200);self.assertEqual(len(attempts),1)
        self.assertEqual(attempts[0]['model'],'gpt-5.6-sol');self.assertEqual(attempts[0]['reasoning']['effort'],'high')
        self.assertEqual(stats['runtime']['completed_responses'],1)
        self.assertEqual(stats['runtime']['known_tokens'],10)
        self.assertTrue(stats['runtime']['usage_complete'])


if __name__=='__main__':unittest.main()
