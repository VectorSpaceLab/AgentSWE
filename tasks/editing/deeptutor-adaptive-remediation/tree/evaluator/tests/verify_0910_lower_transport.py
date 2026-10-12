"""Actual local TLS/proxy and fixed-image controls; never a paid model call."""
import argparse,hashlib,json,os,select,socket,ssl,subprocess,sys,threading,time,urllib.error,urllib.request
from http.server import BaseHTTPRequestHandler,ThreadingHTTPServer
from pathlib import Path

def main():
 p=argparse.ArgumentParser();p.add_argument('--source',type=Path,required=True);p.add_argument('--run',type=Path,required=True);a=p.parse_args()
 a.run.mkdir(parents=True,exist_ok=False);sys.path[:0]=[str(a.source),str(a.source/'harbor'),'@@AGENTSWE_EDITING_CONTROL@@']
 from validate_formal_config import tree_digest
 from harbor import formal_one_stop as formal
 before=tree_digest(a.source);checks=[];posts=[];connects=[];pending=threading.Event();release=threading.Event();mode=['completed'];owned=[]
 opener=urllib.request.build_opener(urllib.request.ProxyHandler({}))
 def check(name,condition):
  checks.append({'name':name,'valid':bool(condition)});assert condition,name
 def sha(path):return hashlib.sha256(path.read_bytes()).hexdigest()
 key=a.run/'synthetic.env';key.write_text('GATEWAY_API_KEY=synthetic-no-real-provider-key\n');key.chmod(0o600)
 cert=a.run/'fixture.crt';private=a.run/'fixture.key'
 subprocess.run(['openssl','req','-x509','-newkey','rsa:2048','-nodes','-keyout',str(private),'-out',str(cert),'-days','1','-subj','/CN=fixture.invalid','-addext','subjectAltName=DNS:fixture.invalid'],check=True,stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL);private.chmod(0o600)
 response={'id':'fixture_completed','status':'completed','output':[{'type':'message','role':'assistant','content':[{'type':'output_text','text':'synthetic transport only'}]}],'usage':{'input_tokens':3,'output_tokens':2,'total_tokens':5}}
 class Upstream(BaseHTTPRequestHandler):
  def log_message(self,*_):pass
  def do_POST(self):
   body=json.loads(self.rfile.read(int(self.headers['Content-Length'])));posts.append({'path':self.path,'model':body.get('model'),'reasoning':body.get('reasoning'),'input':body.get('input')})
   if mode[0]=='pending':pending.set();release.wait(10)
   data=(b'data: {"type":"response.output_text.delta","delta":"partial"}\n\n' if mode[0]=='partial' else ('event: response.completed\ndata: '+json.dumps({'type':'response.completed','response':response})+'\n\n').encode())
   self.send_response(200);self.send_header('Content-Type','text/event-stream');self.send_header('Content-Length',str(len(data)));self.end_headers();self.wfile.write(data)
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
  proc=subprocess.Popen([sys.executable,'-E','-s','-B',str(a.source/'agentloop/broker.py'),'--port',str(port),'--upstream','https://fixture.invalid/v1/responses','--credential-file',str(key),'--stats-file',str(stats)],stdout=log,stderr=log,start_new_session=True,env={'PATH':'/usr/bin:/bin','LANG':'C.UTF-8','SSL_CERT_FILE':str(cert),'AGENTSWE_EVALUATOR_PROXY_URL':proxy_url})
  owned.append((proc,log));base=f'http://127.0.0.1:{port}'
  for _ in range(100):
   try:opener.open(base+'/healthz',timeout=.2).close();return base
   except (OSError,urllib.error.URLError):
    if proc.poll() is not None:raise RuntimeError(stats.with_suffix('.log').read_text())
    time.sleep(.03)
  raise RuntimeError('broker startup timeout')
 def stop():
  proc,log=owned.pop();proc.terminate();proc.wait(timeout=5);log.close()
 def request(base,ctx='known',body=None,path='/v1/responses'):
  headers={'Authorization':'Bearer broker-only-placeholder','Content-Type':'application/json'}
  if ctx is not None:headers['X-AgentSWE-Context']=ctx
  req=urllib.request.Request(base+path,data=json.dumps(body or {'input':'local TLS fixture'}).encode(),headers=headers)
  try:
   with opener.open(req,timeout=12) as r:return r.status,json.loads(r.read())
  except urllib.error.HTTPError as e:return e.code,json.loads(e.read())
 def stats(base):
  with opener.open(urllib.request.Request(base+'/stats',headers={'Authorization':'Bearer stats-only-placeholder'}),timeout=3) as r:
   value=json.loads(r.read());return {**value,'logical_intent_count':len(value.get('logical_requests',{}))}
 result={'valid':False,'source_before':before,'external_API_calls':0,'checks':checks}
 try:
  base=start(a.run/'host.json')
  check('actual_TLS_POST_completed',request(base)==(200,response) and len(posts)==1)
  check('typed_completed_cache_no_extra_POST',request(base)==(200,response) and len(posts)==1)
  check('first_measured_POST',stats(base)['calls']==1 and stats(base)['total_tokens']==5 and stats(base)['logical_intent_count']==1)
  stop();base=start(a.run/'host.json');check('restart_completed_cache_no_extra_POST',request(base)==(200,response) and len(posts)==1)
  mode[0]='pending';response_box=[];worker=threading.Thread(target=lambda:response_box.append(request(base,'pending')));worker.start();check('actual_POST_inflight',pending.wait(5))
  inflight=stats(base);check('inflight_persisted_A_and_unknown_null',inflight['calls']==2 and inflight['pending_calls']==1 and inflight['total_tokens'] is None and inflight['known_usage_subtotal']['total_tokens']==5)
  persisted=json.loads((a.run/'host.json').read_text());check('request_start_durable_before_completion',persisted['calls']==2 and sum(v.get('transport_attempts',0) for v in persisted['logical_requests'].values())==2)
  release.set();worker.join(10);mode[0]='completed';check('inflight_typed_completion',response_box==[(200,response)] and stats(base)['calls']==2 and stats(base)['total_tokens']==10)
  mode[0]='partial';check('partial_is_not_completed',request(base,'partial')[0]==502)
  unknown=stats(base);check('partial_unknown_null_preserves_subtotal',unknown['calls']==3 and unknown['unknown_usage_calls']==1 and unknown['total_tokens'] is None and unknown['known_usage_subtotal']['total_tokens']==10)
  check('partial_same_and_changed_not_resent',request(base,'partial')[0]==409 and request(base,'partial',{'input':'changed'})[0]==409 and len(posts)==3)
  check('missing_context_before_POST',request(base,None)[0]==400 and stats(base)['calls']==3 and len(posts)==3)
  stop();base=start(a.run/'host.json');check('restart_unknown_not_resent',request(base,'partial')[0]==409 and len(posts)==3);stop()
  # A disposable legacy-format copy is read-only. No historical run is touched.
  legacy=json.loads((a.run/'host.json').read_text());legacy.pop('transport_measurement');legacy['schema_version']='agentswe-deeptutor-single-attempt/v2'
  legacy_path=a.run/'legacy.json';legacy_path.write_text(json.dumps(legacy)+'\n');legacy_hash=sha(legacy_path)
  base=start(a.run/'legacy.json');check('legacy_completed_cache_preserved',request(base)==(200,response) and len(posts)==3)
  check('legacy_unknown_and_new_identity_refused',request(base,'partial')[0]==409 and request(base,'new-source')[0]==502 and len(posts)==3)
  stop();check('legacy_ledger_bytes_unchanged',sha(legacy_path)==legacy_hash)
  # The actual default task lifecycle Docker argv carries the explicit proxy.
  deny[0]=True;before_connect=len(connects)
  containers={};docker_run=a.run/'docker-denied';docker_run.mkdir()
  try:
   formal.EVALUATOR_PROXY_URL=proxy_url
   broker=formal.BrokerProcess(run_dir=docker_run,role='public',credential=key,python='/usr/bin/python3',max_calls=10,max_tokens=10000)
   cid=broker.container_id;endpoint=broker.endpoint_local;containers[broker.container_name]=cid
   inspect=json.loads(subprocess.check_output(['docker','inspect',cid],text=True))[0]
   check('Docker_explicit_proxy_and_host_network',inspect['HostConfig']['NetworkMode']=='host' and 'AGENTSWE_EVALUATOR_PROXY_URL='+proxy_url in inspect['Config']['Env'])
   check('Docker_private_loopback_listener',inspect['Config']['Cmd'][inspect['Config']['Cmd'].index('--bind')+1]=='127.0.0.1')
   base=endpoint.removesuffix('/v1/responses');check('controlled_CONNECT_denied',request(base,'denied')[0]==502)
   denial=stats(base);check('CONNECT_failure_zero_model_POST',denial['calls']==0 and denial['successful_calls']==0 and denial['unknown_usage_calls']==0 and denial['pre_transport_failures']==1 and denial['provider_failures']==0 and denial['total_tokens']==0 and denial['logical_intent_count']==1 and denial['requests'][0]['transport_attempts']==0)
   check('known_unsent_not_automatically_retried',request(base,'denied')[0]==409 and request(base,'denied',{'input':'changed'})[0]==409 and len(connects)==before_connect+1)
  finally:
   cleanup=[broker.stop()] if containers else []
  check('owned_Docker_cleanup',bool(cleanup) and all(row['absent_after_cleanup'] for row in cleanup))
  from agentloop.run_hidden import stats_delta
  known={'calls':1,'successful_calls':1,'failures':0,'provider_failures':0,'delivery_failures':0,'input_tokens':6,'output_tokens':4,'total_tokens':10,'known_usage_subtotal':{'input_tokens':6,'output_tokens':4,'total_tokens':10}}
  later={**known,'calls':2,'failures':1,'provider_failures':1,'input_tokens':None,'output_tokens':None,'total_tokens':None}
  delta=stats_delta(known,later)
  check('consumer_unknown_tokens_not_zero_or_negative',delta['calls']==1 and delta['total_tokens'] is None and delta['input_tokens'] is None and delta['known_usage_subtotal']['total_tokens']==0)
  completed={**known,'calls':2,'successful_calls':2,'input_tokens':9,'output_tokens':6,'total_tokens':15,'known_usage_subtotal':{'input_tokens':9,'output_tokens':6,'total_tokens':15}}
  delta=stats_delta(known,completed)
  check('consumer_completed_tokens_and_subtotal_consistent',delta['calls']==1 and delta['total_tokens']==5 and delta['known_usage_subtotal']['total_tokens']==5)
  check('fixture_only_destination_and_no_CONNECT_credential',all(v['authority'] in ('fixture.invalid:443','gateway.example.com:443') and not v['authorization_header_present'] for v in connects) and len(posts)==3)
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
