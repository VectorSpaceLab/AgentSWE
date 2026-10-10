"""Real local HTTP/SSE integration; no external provider, credentials or scores."""
import contextlib
import http.server
import json
from pathlib import Path
import socket
import tempfile
import threading
import time
import unittest
import urllib.error
import urllib.request
from unittest.mock import patch

import lower_responses_broker as broker
from lower_request_ledger import RequestLedger


class BuilderRequestTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        self.root=Path(self.temp.name);self.upstreams=[];self.headers_seen=[];self.mode='json'
        self.proxy_requests=[];self.proxy_servers=[]
        self.release=threading.Event();self.started=threading.Event()
        owner=self
        class Provider(http.server.BaseHTTPRequestHandler):
            def log_message(self,*_):pass
            def do_POST(self):
                owner.headers_seen.append({k:self.headers.get(k) for k in ('User-Agent','Accept','Accept-Encoding')})
                owner.upstreams.append(json.loads(self.rfile.read(int(self.headers['Content-Length']))))
                owner.started.set()
                value={'id':'resp_unit','object':'response','status':'completed','model':broker.MODEL,
                       'output':[{'type':'message','role':'assistant','content':[{'type':'output_text','text':'ok'}]}],
                       'usage':{'input_tokens':11,'output_tokens':7,'total_tokens':18}}
                mode=owner.mode
                if mode=='block':owner.release.wait(3)
                if mode=='http524':self.send_error(524);return
                if mode in ('http403','slow403'):
                    error = json.dumps({'error': {'code': 'synthetic_denial', 'message': 'unit-provider-secret'}}).encode()
                    self.send_response(403);self.send_header('Content-Type','application/json')
                    self.send_header('Content-Length',str(len(error)));self.end_headers()
                    if mode=='slow403':owner.release.wait(2)
                    try:self.wfile.write(error);self.wfile.flush()
                    except OSError:pass
                    return
                if mode=='missing_usage':value.pop('usage')
                if mode=='wrong_model':value['model']='wrong'
                if mode=='delayed_headers':owner.release.wait(2)
                if mode=='secret':value['output'][0]['content'][0]['text']='unit-provider-secret'
                if mode=='incomplete':value['status']='incomplete'
                content_type='application/json';payload=json.dumps(value).encode()
                if mode=='duplicate':payload=b'{"object":"response","status":"completed","status":"incomplete"}'
                if mode=='marker':payload=b'{"text":"response.completed"}'
                if mode in ('sse','partial','slow'):
                    content_type='text/event-stream'
                    payload=('event: response.created\ndata: '+json.dumps({'type':'response.created','response':{'id':'resp_unit'}})+'\n\n').encode()
                    if mode=='sse':payload+=('event: response.completed\ndata: '+json.dumps({'type':'response.completed','response':value})+'\n\n').encode()
                self.send_response(200);self.send_header('Content-Type',content_type)
                if mode!='slow':self.send_header('Content-Length',str(len(payload)))
                self.end_headers()
                try:
                    self.wfile.write(payload);self.wfile.flush()
                    if mode=='slow':owner.release.wait(2)
                except OSError:pass
        self.provider=http.server.ThreadingHTTPServer(('127.0.0.1',0),Provider)
        threading.Thread(target=self.provider.serve_forever,daemon=True).start()
        self.addCleanup(self.provider.server_close);self.addCleanup(self.provider.shutdown)
        self.servers=[];self.ledgers=[]
        self.addCleanup(self.cleanup)
        self.launch()

    def cleanup(self):
        self.release.set()
        for server in self.servers:server.shutdown();server.server_close()
        for server in self.proxy_servers:server.shutdown();server.server_close()
        for ledger in self.ledgers:
            if not ledger.lock.closed:ledger.close()

    def launch(self):
        self.server=http.server.ThreadingHTTPServer(('127.0.0.1',0),broker.Handler)
        self.server.provider_url=f'http://127.0.0.1:{self.provider.server_port}/responses'
        self.server.provider_key='unit-provider-secret'
        self.server.ledger=RequestLedger(self.root/'requests')
        self.server.state=broker.State(self.root/'stats.json')
        self.server.state.reconcile(self.server.ledger)
        self.servers.append(self.server);self.ledgers.append(self.server.ledger)
        threading.Thread(target=self.server.serve_forever,daemon=True).start()

    def start_proxy(self, mode, *, location=None):
        owner=self
        class Proxy(http.server.BaseHTTPRequestHandler):
            def log_message(self,*_):pass
            def do_POST(self):
                body=self.rfile.read(int(self.headers['Content-Length']))
                owner.proxy_requests.append({'path':self.path,'body':json.loads(body)})
                if mode=='disconnect':
                    self.close_connection=True
                    try:self.connection.shutdown(socket.SHUT_RDWR)
                    except OSError:pass
                    return
                if mode=='redirect':
                    self.send_response(302);self.send_header('Location',location)
                    self.send_header('Content-Length','0');self.end_headers();return
                value={'id':'resp_proxy','object':'response','status':'completed','model':broker.MODEL,
                       'output':[{'type':'message','role':'assistant','content':[{'type':'output_text','text':'proxied'}]}],
                       'usage':{'input_tokens':11,'output_tokens':7,'total_tokens':18}}
                payload=json.dumps(value).encode()
                self.send_response(200);self.send_header('Content-Type','application/json')
                self.send_header('Content-Length',str(len(payload)));self.end_headers()
                self.wfile.write(payload)
        server=http.server.ThreadingHTTPServer(('127.0.0.1',0),Proxy)
        self.proxy_servers.append(server)
        threading.Thread(target=server.serve_forever,daemon=True).start()
        return server

    def post(self, *, route='/v1/responses', body=None):
        body=body or {'model':'wrong','reasoning':{'effort':'low'},'input':'synthetic','stream':True}
        req=urllib.request.Request(f'http://127.0.0.1:{self.server.server_port}'+route,
            data=json.dumps(body).encode(),headers={'Authorization':'Bearer '+broker.PLACEHOLDER,'Content-Type':'application/json'})
        try:r=urllib.request.urlopen(req,timeout=5)
        except urllib.error.HTTPError as exc:r=exc
        with r:return r.status,r.read()

    def test_transparent_api_headers_reach_real_loopback_provider(self):
        for stream,accept in [(False,'application/json'),(True,'text/event-stream, application/json')]:
            status,_=self.post(body={'model':'wrong','input':'header-control-'+str(stream),'stream':stream})
            self.assertEqual(status,200)
            self.assertEqual(self.headers_seen[-1],{'User-Agent':'AgentSWE-Codex-Lower/1','Accept':accept,'Accept-Encoding':'identity'})
            self.assertEqual(self.upstreams[-1]['stream'],stream)
        self.assertEqual(self.server.state.stats()['runtime']['actual_upstream_requests'],2)

    def test_controlled_loopback_proxy_forwards_one_absolute_uri_request(self):
        proxy=self.start_proxy('success')
        self.server.provider_url='http://provider.invalid/v1/responses'
        self.server.provider_proxy_url=f'http://127.0.0.1:{proxy.server_port}'
        status,data=self.post(body={'input':'proxy-success','stream':False})
        self.assertEqual(status,200);self.assertEqual(json.loads(data)['id'],'resp_proxy')
        self.assertEqual(len(self.proxy_requests),1)
        self.assertEqual(self.proxy_requests[0]['path'],'http://provider.invalid/v1/responses')
        self.assertEqual(self.proxy_requests[0]['body']['model'],broker.MODEL)
        runtime=self.server.state.stats()['runtime']
        self.assertEqual(runtime['actual_upstream_requests'],1)
        self.assertEqual(runtime['successful_calls'],1)
        self.assertEqual(runtime['total_tokens'],18)

    def test_proxy_disconnect_is_unknown_and_never_replayed(self):
        proxy=self.start_proxy('disconnect')
        self.server.provider_url='http://provider.invalid/v1/responses'
        self.server.provider_proxy_url=f'http://127.0.0.1:{proxy.server_port}'
        first=self.post(body={'input':'proxy-disconnect','stream':False})
        second=self.post(body={'input':'proxy-disconnect','stream':False})
        self.assertEqual(first[0],409);self.assertEqual(second[0],409)
        self.assertEqual(len(self.proxy_requests),1)
        runtime=self.server.state.stats()['runtime']
        self.assertEqual(runtime['actual_upstream_requests'],1)
        self.assertEqual(runtime['unknown_usage_requests'],1)
        self.assertEqual(runtime['cache_queries'],1)
        self.assertIsNone(runtime['total_tokens'])

    def test_proxy_redirect_is_not_followed_or_replayed(self):
        target_calls=[]
        class Target(http.server.BaseHTTPRequestHandler):
            def log_message(self,*_):pass
            def do_GET(self):target_calls.append(('GET',self.path));self.send_error(500)
            def do_POST(self):target_calls.append(('POST',self.path));self.send_error(500)
        target=http.server.ThreadingHTTPServer(('127.0.0.1',0),Target)
        self.proxy_servers.append(target)
        threading.Thread(target=target.serve_forever,daemon=True).start()
        location=f'http://127.0.0.1:{target.server_port}/must-not-follow'
        proxy=self.start_proxy('redirect',location=location)
        self.server.provider_url='http://provider.invalid/v1/responses'
        self.server.provider_proxy_url=f'http://127.0.0.1:{proxy.server_port}'
        self.assertEqual(self.post(body={'input':'proxy-redirect','stream':False})[0],409)
        self.assertEqual(self.post(body={'input':'proxy-redirect','stream':False})[0],409)
        self.assertEqual(len(self.proxy_requests),1);self.assertEqual(target_calls,[])
        runtime=self.server.state.stats()['runtime']
        self.assertEqual(runtime['actual_upstream_requests'],1)
        self.assertEqual(runtime['unknown_usage_requests'],1)
        self.assertIsNone(runtime['total_tokens'])

    def test_completed_json_is_one_logical_request_and_cached(self):
        first=self.post();second=self.post()
        self.assertEqual(first,second);self.assertEqual(first[0],200);self.assertEqual(len(self.upstreams),1)
        runtime=self.server.state.stats()['runtime']
        self.assertEqual(runtime['calls'],1);self.assertEqual(runtime['actual_upstream_requests'],1)
        self.assertEqual(runtime['total_tokens'],18);self.assertEqual(runtime['cache_queries'],1)
        self.assertEqual(self.upstreams[0]['model'],broker.MODEL)
        self.assertEqual(self.upstreams[0]['reasoning'],{'effort':'high'})

    def test_completed_sse_is_typed_and_usage_counted(self):
        self.mode='sse';status,data=self.post();self.assertEqual(status,200)
        self.assertIn(b'event: response.completed',data)
        self.assertEqual(self.server.state.stats()['runtime']['total_tokens'],18)

    def test_completed_cache_survives_broker_reload(self):
        first=self.post();self.server.shutdown();self.server.server_close();self.server.ledger.close()
        self.launch();self.assertEqual(self.post(),first);self.assertEqual(len(self.upstreams),1)
        self.assertEqual(self.server.state.stats()['runtime']['calls'],1)

    def test_unknown_524_never_retries_or_becomes_zero_usage(self):
        self.mode='http524';self.assertEqual(self.post()[0],409);self.assertEqual(self.post()[0],409)
        self.assertEqual(len(self.upstreams),1)
        runtime=self.server.state.stats()['runtime']
        self.assertIsNone(runtime['total_tokens']);self.assertEqual(runtime['known_total_tokens'],0)
        self.assertEqual(runtime['unknown_usage_requests'],1);self.assertEqual(runtime['successful_calls'],0)

    def test_unknown_state_survives_reload(self):
        self.mode='http524';self.post();self.server.shutdown();self.server.server_close();self.server.ledger.close()
        self.launch();self.assertEqual(self.post()[0],409);self.assertEqual(len(self.upstreams),1)
        self.assertIsNone(self.server.state.stats()['runtime']['total_tokens'])

    def test_403_error_sample_is_private_redacted_and_never_retried(self):
        self.mode='http403';status,wire=self.post()
        self.assertEqual(status,409);self.assertEqual(self.post()[0],409)
        self.assertEqual(len(self.upstreams),1)
        self.assertNotIn(b'synthetic_denial',wire)
        samples=list((self.root/'requests').glob('*/upstream_error_sample.redacted'))
        self.assertEqual(len(samples),1)
        self.assertIn(b'synthetic_denial',samples[0].read_bytes())
        self.assertIn(b'[REDACTED]',samples[0].read_bytes())
        for path in self.root.rglob('*'):
            if path.is_file():self.assertNotIn(b'unit-provider-secret',path.read_bytes())
        self.assertIsNone(self.server.state.stats()['runtime']['total_tokens'])

    def test_error_capture_has_its_own_bound_and_late_read_cannot_write(self):
        self.mode='slow403'
        with patch.object(broker,'ERROR_SAMPLE_SECONDS',.1):
            started=time.monotonic();status,_=self.post()
        self.assertEqual(status,409);self.assertLess(time.monotonic()-started,1.5)
        self.assertEqual(len(self.upstreams),1)
        receipt=next((self.root/'requests').glob('*/upstream_error_sample.json'))
        before=receipt.read_bytes()
        self.release.set();time.sleep(.15)
        self.assertEqual(receipt.read_bytes(),before)
        self.assertFalse(list((self.root/'requests').glob('*/upstream_error_sample.redacted')))

    def test_error_sample_storage_failure_keeps_original_terminal_outcome(self):
        self.mode='http403'
        original=Path.open
        def guarded(path,*args,**kwargs):
            if path.name.startswith('upstream_error_sample'):
                raise PermissionError('synthetic diagnostic-only write failure')
            return original(path,*args,**kwargs)
        with patch.object(Path,'open',guarded):
            self.assertEqual(self.post()[0],409)
        runtime=self.server.state.stats()['runtime']
        self.assertEqual(runtime['in_flight_calls'],0)
        self.assertEqual(runtime['provider_failures'],1)
        self.assertIsNone(runtime['total_tokens'])
        self.assertEqual(self.post()[0],409)
        self.assertEqual(len(self.upstreams),1)

    def test_partial_sse_cannot_complete_or_retry(self):
        self.mode='partial';self.assertEqual(self.post()[0],409);self.assertEqual(self.post()[0],409)
        self.assertEqual(len(self.upstreams),1)

    def test_marker_and_duplicate_json_are_not_completion(self):
        for mode in ('marker','duplicate','incomplete','wrong_model'):
            self.mode=mode
            self.assertEqual(self.post(body={'input':mode,'stream':True})[0],409)
        self.assertEqual(self.server.state.stats()['runtime']['successful_calls'],0)

    def test_concurrent_duplicate_does_not_start_upstream(self):
        self.mode='block';results=[]
        thread=threading.Thread(target=lambda:results.append(self.post()));thread.start()
        self.assertTrue(self.started.wait(2));self.assertEqual(self.post()[0],409)
        self.release.set();thread.join(3);self.assertEqual(results[0][0],200)
        self.assertEqual(len(self.upstreams),1)

    def test_full_response_missing_usage_remains_unknown(self):
        self.mode='missing_usage';self.assertEqual(self.post()[0],200)
        self.assertIsNone(self.server.state.stats()['runtime']['total_tokens'])

    def test_secret_redacted_from_wire_and_all_evidence(self):
        self.mode='secret';_,data=self.post();self.assertNotIn(b'unit-provider-secret',data)
        for path in self.root.rglob('*'):
            if path.is_file():self.assertNotIn(b'unit-provider-secret',path.read_bytes(),str(path))

    def test_completed_cache_tamper_rejected(self):
        self.post();next((self.root/'requests').glob('*/response.bin')).write_bytes(b'{}')
        self.assertEqual(self.post()[0],409);self.assertEqual(len(self.upstreams),1)

    def test_two_process_owners_rejected(self):
        with self.assertRaises(BlockingIOError):RequestLedger(self.root/'requests')

    def test_stream_deadline_bounds_trickle_and_does_not_retry(self):
        self.mode='slow'
        with patch.object(broker,'MAX_STREAM_SECONDS',.15):
            start=time.monotonic();status,_=self.post()
        self.release.set();self.assertEqual(status,409);self.assertLess(time.monotonic()-start,1.5)
        self.assertEqual(len(self.upstreams),1)

    def test_header_absolute_deadline_cannot_later_complete(self):
        self.mode='delayed_headers'
        with patch.object(broker,'MAX_CALL_SECONDS',.15):
            start=time.monotonic();status,_=self.post()
        self.assertEqual(status,409);self.assertLess(time.monotonic()-start,1.5)
        self.release.set();time.sleep(.15)
        self.assertEqual(self.post()[0],409);self.assertEqual(len(self.upstreams),1)
        self.assertFalse(list((self.root/'requests').glob('*/completed.json')))

    def test_recover_orphan_sent_request_is_unknown_and_never_replayed(self):
        body={'model':broker.MODEL,'reasoning':{'effort':broker.EFFORT},'input':'synthetic','stream':True}
        path,_,_=self.server.ledger.claim(body,'/v1/responses',self.server.provider_url)
        self.server.ledger.sent(path)
        self.server.state.reconcile(self.server.ledger)
        runtime=self.server.state.stats()['runtime']
        self.assertIsNone(runtime['total_tokens']);self.assertEqual(runtime['actual_upstream_requests'],1)
        self.assertEqual(runtime['unknown_usage_requests'],1);self.assertEqual(runtime['calls'],1)
        self.assertEqual(self.post()[0],409);self.assertEqual(len(self.upstreams),0)
        self.assertTrue((path/'recovered_interruption.json').exists())

    def test_recover_completion_before_stats_persist_restores_usage(self):
        self.post()
        clean=broker.State().stats()
        (self.root/'stats.json').write_text(json.dumps(clean))
        restored=broker.State(self.root/'stats.json');restored.reconcile(self.server.ledger)
        runtime=restored.stats()['runtime']
        self.assertEqual(runtime['total_tokens'],18);self.assertEqual(runtime['successful_calls'],1)

    def test_in_flight_usage_is_not_reported_as_zero(self):
        self.mode='block';results=[]
        thread=threading.Thread(target=lambda:results.append(self.post()));thread.start()
        self.assertTrue(self.started.wait(2))
        self.assertIsNone(self.server.state.stats()['runtime']['total_tokens'])
        self.release.set();thread.join(3);self.assertEqual(results[0][0],200)

    def test_chat_conversion_uses_typed_response(self):
        status,data=self.post(route='/v1/chat/completions',body={'messages':[{'role':'user','content':'synthetic'}]})
        self.assertEqual(status,200);self.assertEqual(json.loads(data)['choices'][0]['message']['content'],'ok')


if __name__=='__main__':unittest.main()
