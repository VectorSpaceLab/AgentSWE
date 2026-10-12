import json,os,socket,subprocess,sys,tempfile,threading,time,unittest,urllib.error,urllib.request
from http.server import BaseHTTPRequestHandler,ThreadingHTTPServer
from pathlib import Path
ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT/'lower_agent'))
import isolated_runtime as runtime
import openhands_lower_agent as lower
class IdentityTests(unittest.TestCase):
 def setUp(self):
  self.paused=threading.Event();self.release=threading.Event();self.pause=False
  self.output=Path(tempfile.mkdtemp(prefix='request-',dir=os.environ['AGENTSWE_DIAGNOSTIC_OUTPUT']));self.received=[];self.reply=(200,'application/json',json.dumps({'id':'r1','status':'completed','output_text':'{"kind":"finish"}','usage':{'input_tokens':3,'output_tokens':2,'total_tokens':5}}).encode())
  test=self
  class Upstream(BaseHTTPRequestHandler):
   def log_message(self,*args):pass
   def do_POST(self):
    test.received.append(json.loads(self.rfile.read(int(self.headers['content-length']))))
    if test.pause:test.paused.set();test.release.wait(timeout=4)
    status,ctype,data=test.reply;self.send_response(status);self.send_header('content-type',ctype);self.end_headers();self.wfile.write(data)
  self.upstream=ThreadingHTTPServer(('127.0.0.1',0),Upstream);threading.Thread(target=self.upstream.serve_forever,daemon=True).start()
  self.key=self.output/'synthetic.env';self.key.write_text('DEEPSEEK_API_KEY=synthetic-never-real\n');self.start_broker()
 def start_broker(self):
  sock=socket.socket();sock.bind(('127.0.0.1',0));port=sock.getsockname()[1];sock.close();self.url=f'http://127.0.0.1:{port}'
  self.log=(self.output/'broker.log').open('a');self.proc=subprocess.Popen(['/usr/bin/python3',str(ROOT/'evaluator/broker/candidate_broker.py'),'--port',str(port),'--upstream',f'http://127.0.0.1:{self.upstream.server_port}','--credential-file',str(self.key),'--stats-file',str(self.output/'stats.json')],stdout=self.log,stderr=self.log)
  for _ in range(100):
   try:urllib.request.urlopen(self.url+'/healthz',timeout=.2).close();return
   except Exception:time.sleep(.02)
  self.fail('broker local startup failed')
 def stop_broker(self):self.proc.terminate();self.proc.wait(timeout=5);self.log.close()
 def tearDown(self):self.stop_broker();self.upstream.shutdown();self.upstream.server_close()
 def request(self):
  req=urllib.request.Request(self.url+'/v1/responses',data=json.dumps({'input':'test exact identity'}).encode(),headers={'Content-Type':'application/json'})
  try:
   with urllib.request.urlopen(req,timeout=5) as response:return response.status,json.loads(response.read())
  except urllib.error.HTTPError as exc:return exc.code,json.loads(exc.read())
 def test_completed_stream_and_duplicate_and_reload_use_same_response(self):
  response={'id':'rstream','status':'completed','output_text':'{"kind":"finish"}','usage':{'input_tokens':3,'output_tokens':2,'total_tokens':5}}
  self.reply=(200,'text/event-stream',('event: response.completed\ndata: '+json.dumps({'type':'response.completed','response':response})+'\n\n').encode())
  self.assertEqual(self.request(),(200,response));self.assertEqual(self.request(),(200,response));self.stop_broker();self.start_broker();self.assertEqual(self.request(),(200,response));self.assertEqual(len(self.received),1);self.assertTrue(self.received[0]['stream']);self.assertEqual(self.received[0]['reasoning']['effort'],'high')
 def test_unknown_http_response_is_not_resampled_and_usage_is_unknown(self):
  self.reply=(524,'text/plain',b'upstream uncertain');self.assertEqual(self.request()[0],502);self.assertEqual(self.request()[0],409);self.assertEqual(len(self.received),1)
  stats=json.loads((self.output/'stats.json').read_text());self.assertIsNone(stats['requests'][0]['total_tokens']);self.assertEqual(stats['runtime']['unknown_usage_calls'],1)
 def test_partial_stream_is_not_a_completion(self):
  self.reply=(200,'text/event-stream',b'data: {"type":"response.output_text.delta","delta":"partial"}\n\n');self.assertEqual(self.request()[0],502);self.assertEqual(self.request()[0],409);self.assertEqual(len(self.received),1)
 def test_duplicate_status_cannot_forge_completion(self):
  self.reply=(200,'application/json',b'{"id":"r1","status":"in_progress","status":"completed","output_text":"{}"}');self.assertEqual(self.request()[0],502);self.assertEqual(self.request()[0],409)
 def test_driver_records_intent_and_never_retries_unknown_request(self):
  self.reply=(524,'text/plain',b'unknown')
  capture=self.output/'model_response.txt'
  with self.assertRaises(urllib.error.HTTPError):runtime.model_json(lower,self.url+'/v1/responses','one action','test_006','action',lambda:10,capture)
  self.assertEqual(len(self.received),1)
  with self.assertRaises(FileExistsError):runtime.model_json(lower,self.url+'/v1/responses','one action','test_006','action',lambda:10,capture)
  self.assertEqual(len(self.received),1);self.assertEqual(json.loads(Path(str(capture)+'.request.json').read_text())['state'],'submitted_or_unknown')
 def test_pending_reservation_is_durable_unknown_until_completed(self):
  self.pause=True;result=[]
  thread=threading.Thread(target=lambda:result.append(self.request()));thread.start();self.assertTrue(self.paused.wait(2))
  stats=json.loads((self.output/'stats.json').read_text());self.assertEqual(stats['runtime']['calls'],1);self.assertEqual(stats['runtime']['in_flight_calls'],1);self.assertEqual(stats['runtime']['unknown_usage_calls'],1);self.assertIsNone(stats['runtime']['total_tokens'])
  self.release.set();thread.join(3);self.assertEqual(result[0][0],200)
  stats=json.loads((self.output/'stats.json').read_text());self.assertEqual(stats['runtime']['calls'],1);self.assertEqual(stats['runtime']['unknown_usage_calls'],0);self.assertEqual(stats['runtime']['total_tokens'],5)
 def test_remaining_case_deadline_stops_wait_without_resampling(self):
  self.pause=True;capture=self.output/'short-deadline.txt';started=time.monotonic()
  try:
   with self.assertRaises((TimeoutError,urllib.error.HTTPError)):runtime.model_json(lower,self.url+'/v1/responses','bounded wait','test_005','action',lambda:max(.001,.3-(time.monotonic()-started)),capture)
  finally:self.release.set()
  self.assertLess(time.monotonic()-started,2);self.assertEqual(len(self.received),1)
  self.assertEqual(json.loads(Path(str(capture)+'.request.json').read_text())['state'],'submitted_or_unknown')
 def test_completed_response_survives_downstream_disconnect(self):
  from urllib.parse import urlsplit
  self.pause=True;url=urlsplit(self.url);body=json.dumps({'input':'test exact identity'}).encode()
  client=socket.create_connection((url.hostname,url.port));client.sendall(('POST /v1/responses HTTP/1.1\r\nHost: localhost\r\nContent-Type: application/json\r\nContent-Length: '+str(len(body))+'\r\nConnection: close\r\n\r\n').encode()+body)
  self.assertTrue(self.paused.wait(2));client.close();self.release.set()
  end=time.monotonic()+3
  while time.monotonic()<end:
   snap=json.loads((self.output/'stats.json').read_text())
   if snap['runtime']['in_flight_calls']==0:break
   time.sleep(.02)
  self.assertEqual(snap['runtime']['calls'],1);self.assertEqual(snap['runtime']['successful_calls'],1);self.assertEqual(snap['runtime']['failures'],0)
  self.assertEqual(self.request()[0],200);self.assertEqual(len(self.received),1);self.assertEqual(json.loads((self.output/'stats.json').read_text())['runtime']['unknown_usage_calls'],0)
if __name__=='__main__':unittest.main(verbosity=2)
