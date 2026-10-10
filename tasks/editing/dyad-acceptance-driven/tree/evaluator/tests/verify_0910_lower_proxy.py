"""Actual local TLS/proxy and fixed-image controls; never a paid model call."""
import argparse,hashlib,json,os,select,socket,ssl,subprocess,sys,threading,time,urllib.error,urllib.request
from http.server import BaseHTTPRequestHandler,ThreadingHTTPServer
from pathlib import Path

def main():
 p=argparse.ArgumentParser();p.add_argument('--source',type=Path,required=True);p.add_argument('--run',type=Path,required=True);a=p.parse_args()
 a.run.mkdir(parents=True,exist_ok=False);sys.path[:0]=[str(a.source),str(a.source/'harbor'),'@@AGENTSWE_EDITING_CONTROL@@']
 from validate_formal_config import tree_digest
 import types
 from environment.transport_sandbox import FixedLowerRelay, UnixConnection
 before=tree_digest(a.source);checks=[];posts=[];connects=[];pending=threading.Event();release=threading.Event();mode=['completed'];owned=[]
 opener=urllib.request.build_opener(urllib.request.ProxyHandler({}))
 def check(name,condition):
  checks.append({'name':name,'valid':bool(condition)});assert condition,name
 def sha(path):return hashlib.sha256(path.read_bytes()).hexdigest()
 key=a.run/'synthetic.env';key.write_text('GATEWAY_API_KEY=synthetic-no-real-provider-key\n');key.chmod(0o600)
 cert=a.run/'fixture.crt';private=a.run/'fixture.key'
 subprocess.run(['openssl','req','-x509','-newkey','rsa:2048','-nodes','-keyout',str(private),'-out',str(cert),'-days','1','-subj','/CN=fixture.invalid','-addext','subjectAltName=DNS:fixture.invalid'],check=True,stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL);private.chmod(0o600)
 response={'id':'fixture_completed','status':'completed','model':'gpt-5.6-sol','created_at':1,'output':[{'id':'msg_fixture','status':'completed','type':'message','role':'assistant','content':[{'type':'output_text','text':'synthetic transport only','annotations':[]}]}],'usage':{'input_tokens':3,'output_tokens':2,'total_tokens':5}}
 class Upstream(BaseHTTPRequestHandler):
  def log_message(self,*_):pass
  def do_POST(self):
   body=json.loads(self.rfile.read(int(self.headers['Content-Length'])));posts.append({'path':self.path,'model':body.get('model'),'reasoning':body.get('reasoning'),'input':body.get('input')})
   if mode[0]=='pending':pending.set();release.wait(10)
   data=(b'data: {"type":"response.output_text.delta","delta":"partial"}\n\n' if mode[0]=='partial' else ('event: response.completed\ndata: '+json.dumps({'type':'response.completed','response':response})+'\n\n').encode())
   self.send_response(200);self.send_header('Content-Type','text/event-stream');self.send_header('Content-Length',str(len(data)));self.end_headers()
   try:self.wfile.write(data)
   except (OSError,ssl.SSLError):pass
 tls=ThreadingHTTPServer(('127.0.0.1',0),Upstream);context=ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER);context.load_cert_chain(cert,private);tls.socket=context.wrap_socket(tls.socket,server_side=True);threading.Thread(target=tls.serve_forever,daemon=True).start()
 deny=[False]
 class Proxy(BaseHTTPRequestHandler):
  def log_message(self,*_):pass
  def do_CONNECT(self):
   connects.append({'authority':self.path,'denied':deny[0],'authorization_header_present':self.headers.get('Authorization') is not None})
   if deny[0] or self.path!='fixture.invalid:443':self.send_error(403,'synthetic controlled CONNECT rejection');return
   target=socket.create_connection(('127.0.0.1',tls.server_port),timeout=5)
   self.send_response(200,'Connection Established');self.end_headers();self.wfile.flush()
   try:
    while True:
     readers,_,_=select.select([self.connection,target],[],[],12)
     if not readers:break
     for src in readers:
      data=src.recv(65536)
      if not data:return
      (target if src is self.connection else self.connection).sendall(data)
   finally:target.close()
 proxy=ThreadingHTTPServer(('127.0.0.1',0),Proxy);threading.Thread(target=proxy.serve_forever,daemon=True).start();proxy_url=f'http://127.0.0.1:{proxy.server_port}'
 def start(stats):
  sock=socket.socket();sock.bind(('127.0.0.1',0));port=sock.getsockname()[1];sock.close()
  log=stats.with_suffix('.log').open('ab')
  proc=subprocess.Popen([sys.executable,'-I',str(a.source/'evaluator/broker/candidate_broker.py'),'--port',str(port),'--upstream','https://fixture.invalid','--credential-file',str(key),'--stats-file',str(stats)],stdout=log,stderr=log,start_new_session=True,env={'PATH':'/usr/bin:/bin','LANG':'C.UTF-8','SSL_CERT_FILE':str(cert),'AGENTSWE_EVALUATOR_PROXY_URL':proxy_url})
  owned.append((proc,log));base=f'http://127.0.0.1:{port}'
  for _ in range(100):
   try:opener.open(base+'/healthz',timeout=.2).close();return base
   except (OSError,urllib.error.URLError):
    if proc.poll() is not None:raise RuntimeError(stats.with_suffix('.log').read_text())
    time.sleep(.03)
  raise RuntimeError('broker startup timeout')
 def stop():
  proc,log=owned.pop();proc.terminate();proc.wait(timeout=5);log.close()
 def request(base,ctx='known',body=None,path='/v1/responses',extra=None):
  headers={'Authorization':'Bearer broker-only-placeholder','Content-Type':'application/json'}
  if ctx is not None:headers['X-AgentSWE-Context']=ctx
  headers.update(extra or {})
  req=urllib.request.Request(base+path,data=json.dumps(body or {'input':'local TLS fixture'}).encode(),headers=headers)
  try:
   with opener.open(req,timeout=12) as r:return r.status,json.loads(r.read())
  except urllib.error.HTTPError as e:return e.code,json.loads(e.read())
 def stats(base):
  with opener.open(base+'/stats',timeout=3) as r:
   value=json.loads(r.read());return {**value['runtime'],'requests':value['requests'],'logical_intent_count':len(value.get('logical_requests',{}))}
 result={'valid':False,'source_before':before,'external_API_calls':0,'checks':checks}
 try:
  # Main owns the lower budget even when the caller omits an absolute deadline.
  import importlib.util
  from unittest.mock import patch
  spec=importlib.util.spec_from_file_location('dyad_headless_budget',a.source/'environment/headless_chat_flow.py');headless=importlib.util.module_from_spec(spec);spec.loader.exec_module(headless)
  captured={}
  def owned_probe(command,**kwargs):
   captured.update(command=command,timeout=kwargs['timeout']);return subprocess.CompletedProcess(command,0,'',''),{'controlled':True}
  initial=time.monotonic()
  with patch.object(sys,'argv',[str(a.source/'environment/headless_chat_flow.py'),'--output',str(a.run/'budget/output.json'),'--timeout','600']),patch.dict(sys.modules,{'owned_resources':types.SimpleNamespace(run_owned=owned_probe)}):
   check('owned_child_keeps_parent_case_deadline',headless.main()==0 and '--case-deadline-monotonic' in captured['command'] and 0<float(captured['command'][captured['command'].index('--case-deadline-monotonic')+1])-initial<=600.1 and captured['timeout']<=600)
  base=start(a.run/'host.json')
  check('actual_TLS_POST_completed',request(base)==(200,response) and len(posts)==1)
  check('typed_completed_cache_no_extra_POST',request(base)==(200,response) and len(posts)==1)
  check('first_measured_POST',stats(base)['calls']==1 and stats(base)['total_tokens']==5 and stats(base)['logical_intent_count']==1)
  stop();base=start(a.run/'host.json');check('restart_completed_cache_no_extra_POST',request(base)==(200,response) and len(posts)==1)
  mode[0]='pending';response_box=[];worker=threading.Thread(target=lambda:response_box.append(request(base,'pending')));worker.start();check('actual_POST_inflight',pending.wait(5))
  inflight=stats(base);check('inflight_persisted_A_and_unknown_null',inflight['calls']==2 and inflight['pending_calls']==1 and inflight['total_tokens'] is None and inflight['known_usage_subtotal']['total_tokens']==5)
  persisted=json.loads((a.run/'host.json').read_text());check('request_start_durable_before_completion',persisted['runtime']['calls']==2 and sum(v.get('transport_attempts',0) for v in persisted['logical_requests'].values())==2)
  release.set();worker.join(10);mode[0]='completed';check('inflight_typed_completion',response_box==[(200,response)] and stats(base)['calls']==2 and stats(base)['total_tokens']==10)
  mode[0]='partial';check('partial_is_not_completed',request(base,'partial')[0]==400)
  unknown=stats(base);check('partial_unknown_null_preserves_subtotal',unknown['calls']==3 and unknown['unknown_usage_calls']==1 and unknown['total_tokens'] is None and unknown['known_usage_subtotal']['total_tokens']==10)
  check('partial_same_and_changed_not_resent',request(base,'partial')[0]==409 and request(base,'partial',{'input':'changed'})[0]==409 and len(posts)==3)
  check('missing_context_before_POST',request(base,None)[0]==400 and stats(base)['calls']==3 and len(posts)==3)
  stop();base=start(a.run/'host.json');check('restart_unknown_not_resent',request(base,'partial')[0]==409 and len(posts)==3);stop()
  # A disposable legacy-format copy is read-only. No historical run is touched.
  legacy=json.loads((a.run/'host.json').read_text());legacy.pop('transport_measurement');legacy['schema_version']='dyad-request-ledger/v1'
  legacy_path=a.run/'legacy.json';legacy_path.write_text(json.dumps(legacy)+'\n');legacy_hash=sha(legacy_path)
  base=start(a.run/'legacy.json');check('legacy_completed_cache_preserved',request(base)==(200,response) and len(posts)==3)
  check('legacy_unknown_and_new_identity_refused',request(base,'partial')[0]==409 and request(base,'new-source')[0]==400 and len(posts)==3)
  stop();check('legacy_ledger_bytes_unchanged',sha(legacy_path)==legacy_hash)
  # Exercise the exact required production mount/argv, without central formal changes.
  deny[0]=True;before_connect=len(connects)
  sock=socket.socket();sock.bind(('127.0.0.1',0));docker_port=sock.getsockname()[1];sock.close()
  docker_dir=a.run/'docker-denied';docker_dir.mkdir();image='agentswe/create-agent-builder-codex:0.144.1-node24.6.0-0812'
  command=['docker','run','-d','--rm','--network','host','--name','dyad-lower-v54-'+str(os.getpid()),
    '-v',str(a.source/'evaluator/broker/candidate_broker.py')+':/broker.py:ro','-v',str(a.source/'evaluator/broker/request_ledger.py')+':/request_ledger.py:ro',
    '-v','@@AGENTSWE_EDITING_CONTROL@@/responses_stream.py:/responses_stream.py:ro',
    '-v',str(key)+':/credential.env:ro','-v',str(docker_dir)+':/ledger:rw',
    '-e','AGENTSWE_EVALUATOR_PROXY_URL='+proxy_url,image,'python3','/broker.py','--bind','127.0.0.1','--port',str(docker_port),'--upstream','https://fixture.invalid','--credential-file','/credential.env','--stats-file','/ledger/stats.json']
  cid=subprocess.check_output(command,text=True).strip();cleanup=[]
  try:
   inspect=json.loads(subprocess.check_output(['docker','inspect',cid],text=True))[0]
   check('Docker_explicit_proxy_and_host_network',inspect['HostConfig']['NetworkMode']=='host' and 'AGENTSWE_EVALUATOR_PROXY_URL='+proxy_url in inspect['Config']['Env'])
   check('Docker_private_loopback_listener',inspect['Config']['Cmd'][inspect['Config']['Cmd'].index('--bind')+1]=='127.0.0.1')
   base=f'http://127.0.0.1:{docker_port}'
   for _ in range(100):
    try:opener.open(base+'/healthz',timeout=.2).close();break
    except OSError:time.sleep(.05)
   check('controlled_CONNECT_denied',request(base,'denied')[0]==502)
   denial=stats(base);check('CONNECT_failure_zero_model_POST',denial['calls']==0 and denial['successful_calls']==0 and denial['unknown_usage_calls']==0 and denial['total_tokens']==0 and denial['logical_intent_count']==1 and denial['requests'][0]['transport_attempts']==0)
   check('known_unsent_not_automatically_retried',request(base,'denied')[0]==409 and request(base,'denied',{'input':'changed'})[0]==409 and len(connects)==before_connect+1)
  finally:
   subprocess.run(['docker','rm','-f',cid],check=True,stdout=subprocess.DEVNULL)
   absent=subprocess.run(['docker','inspect',cid],stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL).returncode!=0
   cleanup=[{'container_id':cid,'absent_after_cleanup':absent}]
  check('owned_Docker_cleanup',cleanup[0]['absent_after_cleanup'])
  deny[0]=False;mode[0]='completed';base=start(a.run/'chat.json')
  chat_body={'model':'candidate-override','reasoning_effort':'high','messages':[{'role':'assistant','tool_calls':[{'id':'call_fixture','function':{'name':'calculate','arguments':'{}'}}]},{'role':'tool','tool_call_id':'call_fixture','content':'result'}]}
  code,chat=request(base,'chat',chat_body,'/v1/chat/completions')
  check('actual_chat_translation_completed',code==200 and chat['choices'][0]['message']['content']=='synthetic transport only' and posts[-1]['path']=='/v1/responses' and posts[-1]['input']==[{'type':'function_call','call_id':'call_fixture','name':'calculate','arguments':'{}'},{'type':'function_call_output','call_id':'call_fixture','output':'result'}])
  check('model_effort_forced_for_chat',posts[-1]['model']=='gpt-5.6-sol' and posts[-1]['reasoning']=={'effort':'high'})
  check('chat_complete_cached',request(base,'chat',chat_body,'/v1/chat/completions')==(code,chat) and len(posts)==4)
  stop()
  sdk_base=start(a.run/'sdk.json');relay=FixedLowerRelay(sdk_base+'/v1/responses',context_id='sdk-case',deadline=time.monotonic()+30,journal=a.run/'sdk-relay.json').start()
  def via_relay(body,*,action=None,explicit=None):
   conn=UnixConnection(relay.socket_path,timeout=10)
   try:
    headers={'Content-Type':'application/json'}
    if action:headers['X-AgentSWE-Action']=action
    if explicit:headers['Idempotency-Key']=explicit
    conn.request('POST','/v1/responses',json.dumps(body).encode(),headers=headers)
    result=conn.getresponse();return result.status,result.read()
   finally:conn.close()
  try:
   count=len(posts);identical={'input':'identical-action'}
   check('different_actions_identical_payload_two_POSTs',via_relay(identical,action='one')[0]==200 and via_relay(identical,action='two')[0]==200 and len(posts)==count+2)
   check('same_completed_action_cached',via_relay(identical,action='one')[0]==200 and len(posts)==count+2)
   check('explicit_request_identity_completed',via_relay({'input':'explicit'},explicit='request-1')[0]==200 and len(posts)==count+3)
   check('explicit_identity_payload_change_rejected',via_relay({'input':'changed'},explicit='request-1')[0]==400 and len(posts)==count+3)
   # Actual installed AI SDK v6 plus @ai-sdk/openai v3 through the real inside bridge.
   sdk_script=a.run/'sdk.mjs'
   sdk_script.write_text("import {streamText} from '@@AGENTSWE_ENVS@@/dyad-task-env-cycle-006/node_modules/ai/dist/index.mjs';\nimport {createOpenAI} from '@@AGENTSWE_ENVS@@/dyad-task-env-cycle-006/node_modules/@ai-sdk/openai/dist/index.mjs';\nconst rawFetch=globalThis.fetch;let action='sdk-action';let retryDelivery=true;\nconst provider=createOpenAI({apiKey:'broker-only-placeholder',baseURL:process.env.OPENAI_BASE_URL,fetch:async(input,init)=>{const headers=new Headers(init?.headers);headers.set('X-AgentSWE-Action',action);const response=await rawFetch(input,{...init,headers});if(retryDelivery){retryDelivery=false;await response.arrayBuffer();return new Response('{}',{status:503,headers:{'content-type':'application/json'}});}return response;}});\nfor(const next of ['sdk-action','sdk-action-2']){action=next;const result=streamText({model:provider.chat('candidate-override'),prompt:'same SDK prompt'});const text=await result.text;if(text!=='synthetic transport only')throw new Error('invalid actual SDK chat completion');}\nconsole.log('ACTUAL_AI_SDK_COMPLETE');\n")
   node='@@AGENTSWE_ENVS@@/runtime-deps-0910/dyad/node24.6.0/bin/node'
   start_sdk=len(posts)
   proc=subprocess.run([sys.executable,'-I',str(a.source/'environment/transport_sandbox.py'),'--inside','--uds',str(relay.socket_path),'--preflight',str(a.run/'sdk-inside-preflight.json'),'--',node,str(sdk_script)],capture_output=True,text=True,timeout=25)
   (a.run/'sdk.stdout').write_text(proc.stdout);(a.run/'sdk.stderr').write_text(proc.stderr)
   check('actual_AI_SDK_chat_stream_retry_cache_and_distinct_action',proc.returncode==0 and 'ACTUAL_AI_SDK_COMPLETE' in proc.stdout and len(posts)==start_sdk+2)
   # Execute the exact run-local AsyncLocalStorage wrapper emitted by headless_chat_flow.
   import ast
   tree=ast.parse((a.source/'environment/headless_chat_flow.py').read_text())
   wrapper=next(node.value for node in ast.walk(tree) if isinstance(node,ast.Constant) and isinstance(node.value,str) and node.value.startswith('// Evaluator transport metadata;'))
   wrapper_script=a.run/'wrapper.mts'
   wrapper_script.write_text(wrapper+"\nimport {streamText} from '@@AGENTSWE_ENVS@@/dyad-task-env-cycle-006/node_modules/ai/dist/index.mjs';\nimport {createOpenAI} from '@@AGENTSWE_ENVS@@/dyad-task-env-cycle-006/node_modules/@ai-sdk/openai/dist/index.mjs';\nconst provider=createOpenAI({apiKey:'broker-only-placeholder',baseURL:process.env.OPENAI_BASE_URL});\nconst harness={streamChat:async(prompt:string)=>{const r=streamText({model:provider.responses('candidate-override'),prompt});return r.text;}};\nfor(let i=0;i<2;i++){const text=await agentsweStreamChat(harness,'identical wrapper prompt');if(text!=='synthetic transport only')throw new Error('actual Responses SDK output missing');}\nconsole.log('ACTUAL_WRAPPER_RESPONSES_COMPLETE');\n")
   prior_wrapper=len(posts)
   proc=subprocess.run([sys.executable,'-I',str(a.source/'environment/transport_sandbox.py'),'--inside','--uds',str(relay.socket_path),'--preflight',str(a.run/'wrapper-inside-preflight.json'),'--',node,str(wrapper_script)],capture_output=True,text=True,timeout=25,env={**os.environ,'DYAD_CASE_ID':'local-wrapper','DYAD_RESTART_EPOCH':'0'})
   (a.run/'wrapper.stdout').write_text(proc.stdout);(a.run/'wrapper.stderr').write_text(proc.stderr)
   check('actual_run_local_wrapper_Responses_SDK_distinct_identical_actions',proc.returncode==0 and 'ACTUAL_WRAPPER_RESPONSES_COMPLETE' in proc.stdout and len(posts)==prior_wrapper+2)
   mode[0]='partial';unknown_count=len(posts)
   unknown_script=a.run/'unknown-sdk.mjs'
   original=sdk_script.read_text();prefix=original.split("for(const next of")[0].replace("let retryDelivery=true;","let retryDelivery=true;let fetches=0;").replace("const headers=new Headers(init?.headers);","fetches++;const headers=new Headers(init?.headers);")
   unknown_script.write_text(prefix+"action='sdk-unknown';let failed=false;try{await streamText({model:provider.chat('candidate-override'),prompt:'uncertain request'}).text;}catch{failed=true;}if(!failed||fetches!==2)throw new Error('SDK unknown retry was not refused');console.log('ACTUAL_SDK_UNKNOWN_REFUSED');\n")
   proc=subprocess.run([sys.executable,'-I',str(a.source/'environment/transport_sandbox.py'),'--inside','--uds',str(relay.socket_path),'--preflight',str(a.run/'unknown-inside-preflight.json'),'--',node,str(unknown_script)],capture_output=True,text=True,timeout=25)
   (a.run/'unknown-sdk.stdout').write_text(proc.stdout);(a.run/'unknown-sdk.stderr').write_text(proc.stderr)
   check('actual_SDK_retry_unknown_refused_one_POST',proc.returncode==0 and 'ACTUAL_SDK_UNKNOWN_REFUSED' in proc.stdout and len(posts)==unknown_count+1)
   check('relay_unknown_and_changed_action_refused',via_relay({'input':'changed'},action='new-context')[0]==400 and len(posts)==unknown_count+1)
  finally:relay.close();stop()
  relay=FixedLowerRelay(sdk_base+'/v1/responses',context_id='sdk-case',deadline=time.monotonic()+10,journal=a.run/'sdk-relay.json').start()
  try:check('relay_restart_unknown_refused_without_broker',via_relay({'input':'changed-again'},action='fresh')[0]==400)
  finally:relay.close()
  mode[0]='completed';base=start(a.run/'deadline.json');before_deadline=len(posts);mode[0]='pending';pending.clear();release.clear();deadline=time.monotonic()+.7
  started=time.monotonic();expired=request(base,'deadline',extra={'X-AgentSWE-Deadline-Monotonic':str(deadline)});elapsed=time.monotonic()-started
  check('absolute_case_deadline_terminates_inflight_POST',expired[0]==502 and elapsed<2.5 and len(posts)==before_deadline+1 and stats(base)['total_tokens'] is None)
  release.set();stop()
  check('fixture_only_destination_and_no_CONNECT_credential',all(v['authority']=='fixture.invalid:443' and not v['authorization_header_present'] for v in connects))
  check('source_unchanged',tree_digest(a.source)==before)
  result.update(valid=True,source_after=tree_digest(a.source),upstream_model_POSTs=len(posts),synthetic_proxy_CONNECTs=connects,inflight_snapshot=inflight,unknown_snapshot=unknown,denied_snapshot=denial,docker_cleanup=cleanup,shared_transport_sha256=sha(Path('@@AGENTSWE_EDITING_CONTROL@@/responses_stream.py')))
 finally:
  release.set()
  while owned:stop()
  proxy.shutdown();proxy.server_close();tls.shutdown();tls.server_close()
  result['owned_host_processes_stopped']=not owned
  (a.run/'verification.json').write_text(json.dumps(result,indent=2)+'\n')
 print(json.dumps({'valid':result['valid'],'checks':len(checks),'external_API_calls':0,'synthetic_POSTs':len(posts)}))
 return 0 if result['valid'] else 2
if __name__=='__main__':raise SystemExit(main())
