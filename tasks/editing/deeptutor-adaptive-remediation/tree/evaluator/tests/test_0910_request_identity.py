"""Real local HTTP/SSE transport; no provider mocks inside the native adapter."""
import asyncio,http.server,json,sys,tempfile,threading,unittest,urllib.request,urllib.error
from pathlib import Path
ROOT=Path(__file__).resolve().parents[2];sys.path.insert(0,str(ROOT))
from agentloop.broker import BrokerState,make_handler
from agentloop.responses_client_adapter import ResponsesChatAdapter
class RequestIdentityTests(unittest.TestCase):
 def setUp(self):
  self.temp=tempfile.TemporaryDirectory();self.root=Path(self.temp.name);self.requests=[];self.mode='json'
  owner=self
  class Upstream(http.server.BaseHTTPRequestHandler):
   def log_message(self,*_):pass
   def do_POST(self):
    body=json.loads(self.rfile.read(int(self.headers['Content-Length'])));owner.requests.append(body);owner.last_headers=dict(self.headers)
    output=[{'type':'message','role':'assistant','content':[{'type':'output_text','text':'final'}]}]
    if owner.mode=='tool':output=[{'type':'function_call','call_id':'call_native','name':'mastery_remediation_status','arguments':'{}'}]
    if owner.mode=='empty':output=[]
    value={'id':'resp_local_'+str(len(owner.requests)),'status':'completed','output':output,'usage':{'input_tokens':4,'output_tokens':2,'total_tokens':6}}
    status=503 if owner.mode=='http503' else 200;kind='application/json'
    if owner.mode=='no_id':value.pop('id')
    raw=json.dumps(value).encode()
    if owner.mode in ('sse','partial'):
     kind='text/event-stream';raw=b'event: response.created\ndata: '+json.dumps({"type":"response.created","response":{"id":value["id"],"status":"in_progress"}}).encode()+b'\n\n'
     if owner.mode=='sse':raw+=b'event: response.completed\ndata: '+json.dumps({'type':'response.completed','response':value}).encode()+b'\n\n'
     else:raw+=b'event: response.output_text.delta\ndata: {"type":"response.output_text.delta","delta":"partial must not become final"}\n\n'
    self.send_response(status);self.send_header('Content-Type',kind);self.send_header('Content-Length',str(len(raw)));self.end_headers();self.wfile.write(raw)
  self.upstream=http.server.ThreadingHTTPServer(('127.0.0.1',0),Upstream);threading.Thread(target=self.upstream.serve_forever,daemon=True).start();self.start_broker()
 def start_broker(self):
  self.state=BrokerState(self.root/'stats.json',0,0)
  self.broker=http.server.ThreadingHTTPServer(('127.0.0.1',0),make_handler(self.state,f'http://127.0.0.1:{self.upstream.server_port}/v1/responses','test-secret'))
  threading.Thread(target=self.broker.serve_forever,daemon=True).start();self.endpoint=f'http://127.0.0.1:{self.broker.server_port}/v1/responses'
 def tearDown(self):
  self.broker.shutdown();self.broker.server_close();self.upstream.shutdown();self.upstream.server_close();self.temp.cleanup()
 def post(self,context='context-one',text='hello'):
  body={'model':'gpt-5.6-sol','reasoning':{'effort':'high'},'input':[{'role':'user','content':text}],'stream':False}
  req=urllib.request.Request(self.endpoint,data=json.dumps(body).encode(),headers={'Authorization':'Bearer broker-only-placeholder','Content-Type':'application/json','X-AgentSWE-Context':context})
  try:
   with urllib.request.urlopen(req,timeout=5) as response:return response.status,json.loads(response.read())
  except urllib.error.HTTPError as exc:return exc.code,json.loads(exc.read())
 def test_explicit_api_client_headers_reach_upstream(self):
  self.assertEqual(self.post()[0],200)
  self.assertEqual(self.last_headers['User-Agent'],'AgentSWE-Edit-Lower-Broker/1.0')
  self.assertEqual(self.last_headers['Accept'],'text/event-stream, application/json')
  self.assertEqual(self.last_headers['Accept-Encoding'],'identity')
 def test_completed_restart_reuses_identical_terminal_response(self):
  first=self.post();self.assertEqual(first[0],200);self.broker.shutdown();self.broker.server_close();self.start_broker()
  self.assertEqual(self.post(),first);self.assertEqual(len(self.requests),1);self.assertEqual(self.state.public()['calls'],1)
 def test_typed_sse_completion_is_parsed(self):
  self.mode='sse';status,value=self.post();self.assertEqual(status,200);self.assertEqual(value['status'],'completed');self.assertEqual(self.state.public()['total_tokens'],6)
 def test_partial_stream_blocks_same_or_changed_context_without_resampling(self):
  self.mode='partial';self.assertEqual(self.post()[0],502);self.assertEqual(self.post()[0],409);self.assertEqual(self.post(text='changed')[0],409)
  self.assertEqual(len(self.requests),1);self.assertIsNone(self.state.public()['requests'][0]['total_tokens']);self.assertEqual(self.state.public()['unknown_usage_calls'],1)
 def test_http_error_has_one_upstream_attempt(self):
  self.mode='http503';self.assertEqual(self.post()[0],502);self.assertEqual(self.post()[0],409);self.assertEqual(len(self.requests),1)
 def test_missing_typed_identity_cannot_be_completed(self):
  self.mode='no_id';self.assertEqual(self.post()[0],502);self.assertEqual(self.state.public()['successful_calls'],0);self.assertIsNone(self.state.public()['requests'][0]['total_tokens'])
 def test_durable_incomplete_intent_survives_broker_restart(self):
  self.assertIsNone(self.state.reserve('a'*64,'context-one',{'input':'persisted before provider dispatch'}))
  self.broker.shutdown();self.broker.server_close();self.start_broker()
  self.assertEqual(self.post(text='changed-after-crash')[0],409);self.assertEqual(len(self.requests),0)
  self.assertEqual(self.state.public()['pending_calls'],0)
  self.assertEqual(self.state.public()['calls'],0)
  self.assertEqual(self.state.public()['total_tokens'],0)
 def test_possible_POST_intent_survives_broker_restart_as_unknown(self):
  self.assertIsNone(self.state.reserve('b'*64,'context-one',{'input':'possible request'}))
  self.state.transport_started('b'*64)
  self.broker.shutdown();self.broker.server_close();self.start_broker()
  self.assertEqual(self.post(text='changed-after-crash')[0],409);self.assertEqual(len(self.requests),0)
  self.assertEqual(self.state.public()['pending_calls'],1)
  self.assertEqual(self.state.public()['calls'],1)
  self.assertIsNone(self.state.public()['total_tokens'])
 def test_empty_completed_stays_empty(self):
  self.mode='empty';status,value=self.post();self.assertEqual(status,200);self.assertEqual(value['output'],[]);self.assertNotIn('output_text',value)
 def test_native_adapter_tool_return_is_carried_to_next_real_http_request(self):
  # Adapter's production _post is preserved; the fixed evaluator relay adds the
  # trusted context header as it does outside the actual product sandbox.
  from agentloop.lower_transport import UnixHTTPRelay
  import os
  relay=UnixHTTPRelay(self.endpoint,context_id='native-adapter-context');previous=os.environ.get('AGENTSWE_BROKER_UDS');os.environ['AGENTSWE_BROKER_UDS']=str(relay.path)
  try:
   adapter=ResponsesChatAdapter(self.endpoint)
   async def collect(messages):
    stream=await adapter.create(messages=messages,tools=[{'type':'function','function':{'name':'mastery_remediation_status','parameters':{'type':'object'}}}]);return [x async for x in stream]
   self.mode='tool';chunks=asyncio.run(collect([{'role':'user','content':'status'}]));calls=[call for chunk in chunks for call in (chunk.choices[0].delta.tool_calls or [])]
   self.assertEqual(calls[0].id,'call_native');self.assertEqual(calls[0].function.name,'mastery_remediation_status')
   self.mode='json';chunks=asyncio.run(collect([{'role':'user','content':'status'},{'role':'assistant','tool_calls':[{'id':'call_native','function':{'name':'mastery_remediation_status','arguments':'{}'}}]},{'role':'tool','tool_call_id':'call_native','content':'{"status":"observed"}'}]))
   self.assertTrue(any(x.choices[0].delta.content=='final' for x in chunks));self.assertTrue(any(x.get('type')=='function_call_output' and x['call_id']=='call_native' for x in self.requests[-1]['input']))
  finally:
   relay.close()
   if previous is None:os.environ.pop('AGENTSWE_BROKER_UDS',None)
   else:os.environ['AGENTSWE_BROKER_UDS']=previous
if __name__=='__main__':unittest.main()
