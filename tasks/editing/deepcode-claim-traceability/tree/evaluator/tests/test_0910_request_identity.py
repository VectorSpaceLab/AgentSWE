import json,os,socket,subprocess,sys,tempfile,threading,time,unittest,urllib.error,urllib.request
from http.server import BaseHTTPRequestHandler,ThreadingHTTPServer
from pathlib import Path
ROOT=Path(__file__).resolve().parents[2]
class IdentityTests(unittest.TestCase):
 def setUp(self):
  self.temp=tempfile.TemporaryDirectory();self.output=Path(self.temp.name);self.received=[]
  self.response={'id':'r1','status':'completed','output':[{'type':'message','role':'assistant','content':[{'type':'output_text','text':'native result'}]}],'usage':{'input_tokens':3,'output_tokens':2,'total_tokens':5}}
  self.reply=(200,'application/json',json.dumps(self.response).encode());test=self
  class Upstream(BaseHTTPRequestHandler):
   def log_message(self,*a):pass
   def do_POST(self):
    test.received.append(json.loads(self.rfile.read(int(self.headers['content-length']))));status,ct,body=test.reply
    self.send_response(status);self.send_header('content-type',ct);self.end_headers();self.wfile.write(body)
  self.upstream=ThreadingHTTPServer(('127.0.0.1',0),Upstream);threading.Thread(target=self.upstream.serve_forever,daemon=True).start()
  self.key=self.output/'synthetic.env';self.key.write_text('GATEWAY_API_KEY=synthetic-never-real\n');self.start_broker()
 def start_broker(self):
  sock=socket.socket();sock.bind(('127.0.0.1',0));port=sock.getsockname()[1];sock.close();self.url=f'http://127.0.0.1:{port}'
  self.log=(self.output/'broker.log').open('a');self.proc=subprocess.Popen([sys.executable,'-I',str(ROOT/'evaluator/harness/broker_server.py'),'--port',str(port),'--upstream',f'http://127.0.0.1:{self.upstream.server_port}','--credential-file',str(self.key),'--stats-file',str(self.output/'stats.json')],stdout=self.log,stderr=self.log)
  for _ in range(100):
   try:urllib.request.urlopen(self.url+'/healthz',timeout=.2).close();return
   except Exception:time.sleep(.02)
  self.fail('broker startup failed: '+(self.output/'broker.log').read_text())
 def stop_broker(self):self.proc.terminate();self.proc.wait(timeout=5);self.log.close()
 def tearDown(self):self.stop_broker();self.upstream.shutdown();self.upstream.server_close();self.temp.cleanup()
 def request(self,body=None,context='context-1',path='/v1/responses'):
  headers={'Content-Type':'application/json','Authorization':'Bearer broker-only-placeholder'}
  if context:headers['X-AgentSWE-Context']=context
  req=urllib.request.Request(self.url+path,data=json.dumps(body or {'input':'test exact identity'}).encode(),headers=headers)
  try:
   with urllib.request.urlopen(req,timeout=5) as r:return r.status,json.loads(r.read())
  except urllib.error.HTTPError as e:return e.code,json.loads(e.read())
 def test_completed_stream_duplicate_and_reload_reuse_response(self):
  self.reply=(200,'text/event-stream',('event: response.completed\ndata: '+json.dumps({'type':'response.completed','response':self.response})+'\n\n').encode())
  self.assertEqual(self.request(),(200,self.response));self.assertEqual(self.request(),(200,self.response));self.stop_broker();self.start_broker();self.assertEqual(self.request(),(200,self.response));self.assertEqual(len(self.received),1)
  self.assertTrue(self.received[0]['stream']);self.assertEqual(self.received[0]['reasoning']['effort'],'high')
 def test_unknown_blocks_same_and_changed_request_in_context(self):
  self.reply=(524,'text/plain',b'upstream uncertain');self.assertEqual(self.request()[0],502);self.assertEqual(self.request()[0],409);self.assertEqual(self.request({'input':'changed prompt'})[0],409);self.assertEqual(len(self.received),1)
  stats=json.loads((self.output/'stats.json').read_text());self.assertIsNone(stats['requests'][0]['total_tokens']);self.assertEqual(stats['runtime']['unknown_usage_calls'],1)
 def test_partial_stream_is_not_completed(self):
  self.reply=(200,'text/event-stream',b'data: {"type":"response.output_text.delta","delta":"partial"}\n\n');self.assertEqual(self.request()[0],400);self.assertEqual(self.request()[0],409);self.assertEqual(len(self.received),1)
 def test_duplicate_json_status_cannot_forge_completion(self):
  self.reply=(200,'application/json',b'{"id":"r1","status":"in_progress","status":"completed","output_text":"{}"}');self.assertEqual(self.request()[0],400);self.assertEqual(self.request()[0],409);self.assertEqual(len(self.received),1)
 def test_source_world_context_prevents_cross_context_cache(self):
  self.assertEqual(self.request(context='candidate-a-world-a')[0],200);self.assertEqual(self.request(context='candidate-b-world-b')[0],200);self.assertEqual(len(self.received),2)
 def test_context_identity_required_before_provider_call(self):
  self.assertEqual(self.request(context=None)[0],400);self.assertEqual(len(self.received),0)
 def test_chat_tool_history_converts_without_losing_call_identity(self):
  body={'messages':[{'role':'user','content':'inspect'},{'role':'assistant','tool_calls':[{'id':'c1','function':{'name':'read_file','arguments':'{}'}}]},{'role':'tool','tool_call_id':'c1','content':'actual bytes'}],'tools':[{'type':'function','function':{'name':'read_file','parameters':{'type':'object'}}}]}
  status,response=self.request(body,path='/v1/chat/completions');self.assertEqual(status,200);self.assertEqual(response['choices'][0]['message']['content'],'native result')
  self.assertEqual(self.received[0]['input'][1]['type'],'function_call');self.assertEqual(self.received[0]['input'][2],{'type':'function_call_output','call_id':'c1','output':'actual bytes'})
if __name__=='__main__':unittest.main(verbosity=2)
