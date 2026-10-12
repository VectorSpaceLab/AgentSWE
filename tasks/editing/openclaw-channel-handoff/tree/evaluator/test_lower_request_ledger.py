"""Actual local HTTP/SSE transport: durable same-intent replay and unknown isolation."""
from pathlib import Path
import importlib.util,json,os,socket,sys,tempfile,threading,time,unittest,urllib.request,urllib.error
from http.server import BaseHTTPRequestHandler,ThreadingHTTPServer
ROOT=Path(__file__).resolve().parents[1]
spec=importlib.util.spec_from_file_location('openclaw_test_lower_broker',ROOT/'broker/responses_broker.py');broker=importlib.util.module_from_spec(spec);sys.modules[spec.name]=broker;spec.loader.exec_module(broker)
class Provider(BaseHTTPRequestHandler):
 def log_message(self,*args):pass
 def do_POST(self):
  body=json.loads(self.rfile.read(int(self.headers['Content-Length'])));self.server.calls.append(body)
  if self.server.mode=='drop':self.connection.shutdown(socket.SHUT_RDWR);self.connection.close();return
  status='incomplete' if self.server.mode=='incomplete' else 'completed'
  response={'object':'response','id':'resp-actual-http-test','model':broker.MODEL,'status':status,'error':None,'output':[{'type':'message','role':'assistant','content':[{'type':'output_text','text':'observed'}]}],'usage':{'input_tokens':3,'output_tokens':2,'total_tokens':5}}
  if self.server.mode=='missing-id':response.pop('id')
  wire=('data: '+json.dumps({'type':'response.'+status,'response':response})+'\n\n').encode()
  self.send_response(200);self.send_header('Content-Type','text/event-stream');self.send_header('Content-Length',str(len(wire)));self.end_headers();self.wfile.write(wire)
class DurableLowerTests(unittest.TestCase):
 def setUp(self):
  self.tmp=tempfile.TemporaryDirectory(prefix='oc-ledger-',dir=os.environ.get('AGENTSWE_DIAGNOSTIC_OUTPUT'));self.root=Path(self.tmp.name)
  self.provider=ThreadingHTTPServer(('127.0.0.1',0),Provider);self.provider.mode='complete';self.provider.calls=[];threading.Thread(target=self.provider.serve_forever,daemon=True).start();self.start_lower()
 def start_lower(self):
  self.server=broker.LowerServer(('127.0.0.1',0),endpoint=f'http://127.0.0.1:{self.provider.server_port}/v1/responses',key='SYNTHETIC-LEDGER-ONLY',stats_path=self.root/'stats.json',timeout=10);threading.Thread(target=self.server.serve_forever,daemon=True).start()
 def stop_lower(self):self.server.shutdown();self.server.stop_owned_workers();self.server.server_close()
 def tearDown(self):self.stop_lower();self.provider.shutdown();self.provider.server_close();self.tmp.cleanup()
 def post(self,body=None):
  body=body or {'model':'caller-model','input':'same logical intent','stream':True};req=urllib.request.Request(f'http://127.0.0.1:{self.server.server_port}/v1/responses',data=json.dumps(body).encode(),headers={'Authorization':'Bearer '+broker.PLACEHOLDER,broker.LOWER_DEADLINE_HEADER:str(time.monotonic()+10),'Content-Type':'application/json'})
  try:
   with urllib.request.urlopen(req,timeout=12) as r:status,wire=r.status,r.read()
  except urllib.error.HTTPError as e:status,wire=e.code,e.read()
  end=time.monotonic()+2
  while self.server.state.snapshot()['runtime']['in_flight_calls'] and time.monotonic()<end:time.sleep(.01)
  return status,wire
 def test_completed_cached_exact_bytes_and_restart(self):
  first=self.post();self.assertEqual(first[0],200);second=self.post();self.assertEqual(first,second);self.assertEqual(len(self.provider.calls),1)
  self.stop_lower();self.start_lower();self.assertEqual(self.post(),first);self.assertEqual(len(self.provider.calls),1);self.assertEqual(self.server.state.snapshot()['runtime']['total_tokens'],5)
 def test_unknown_blocked_after_restart_and_usage_null(self):
  self.provider.mode='drop';first=self.post();self.assertEqual(first[0],598);self.assertEqual(self.post()[0],409);self.stop_lower();self.start_lower();self.assertEqual(self.post()[0],409);self.assertEqual(len(self.provider.calls),1)
  snap=self.server.state.snapshot();self.assertEqual(snap['runtime']['usage_unknown_calls'],1);self.assertIsNone(snap['runtime']['total_tokens']);self.assertIsNone(snap['runtime']['input_tokens'])
 def test_incomplete_is_observed_provider_failure_no_replay(self):
  self.provider.mode='incomplete';self.post();snap=self.server.state.snapshot();self.assertEqual(snap['runtime']['successful_calls'],0);self.assertEqual(snap['attempts'][0]['response_status'],'incomplete');self.assertEqual(self.post()[0],409);self.assertEqual(len(self.provider.calls),1)
 def test_completed_requires_response_identity(self):
  self.provider.mode='missing-id';self.post();snap=self.server.state.snapshot();self.assertEqual(snap['runtime']['successful_calls'],0);self.assertTrue(snap['attempts'][0]['protocol_invalid']);self.assertEqual(self.post()[0],409)
 def test_known_case_delta_after_previous_unknown(self):
  self.provider.mode='drop';self.post();before=self.server.state.snapshot();self.provider.mode='complete';self.post({'input':'new distinct logical case','stream':True});after=self.server.state.snapshot()
  sys.path.insert(0,str(ROOT));from lower_agent.launcher import broker_stats_delta
  delta=broker_stats_delta(before,after);self.assertEqual(delta['usage_unknown_calls'],0);self.assertEqual(delta['total_tokens'],5);self.assertIsNone(after['runtime']['total_tokens'])
if __name__=='__main__':unittest.main()
