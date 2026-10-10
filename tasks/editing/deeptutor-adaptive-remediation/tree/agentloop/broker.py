#!/usr/bin/env python3
"""Evaluator-owned, durable single-attempt lower Responses transport."""
from __future__ import annotations
import argparse,hashlib,http.server,json,os,sys,threading,time,urllib.error,urllib.request
from pathlib import Path
from typing import Any
try:
 from .protocol import LOWER_MODEL,LOWER_EFFORT,PLACEHOLDER_TOKEN,STATS_TOKEN,utc_now
except ImportError:
 from protocol import LOWER_MODEL,LOWER_EFFORT,PLACEHOLDER_TOKEN,STATS_TOKEN,utc_now
sys.path.insert(0,'@@AGENTSWE_EDITING_CONTROL@@')
sys.path.insert(0,'/')
from responses_stream import direct_opener,read_response,strict_json,RedactedCapture
DEFAULT_UPSTREAM='https://api.deepseek.com/v1/responses'
MAX_BODY_BYTES=8*1024*1024
MAX_ATTEMPTS=1
# Retries for a transport that never reached the provider. 34 of the 50 recorded
# lower failures on 2026-09-15 were transport_attempts=0/not_submitted: nothing was
# sent, billed or generated, so resending is not a replay and the single-upstream
# guarantee for submitted requests is untouched.
UNSENT_MAX_RETRIES=3
UNSENT_RETRY_BACKOFF_SECONDS=1.0
MAX_CALL_SECONDS=180
MEASUREMENT="http-request-start-after-connect-and-tls/v1"
MAX_ERROR_CAPTURE_BYTES=64*1024
ERROR_HEADERS=('content-type','content-length','server','cf-ray','x-request-id','request-id')
# Public per-case defaults.  These exact numbers are disclosed verbatim in
# input/04_resources.md and README.md and are mirrored by the public/hidden
# BrokerProcess calls in harbor/formal_one_stop.py; they must never disagree.
#
# The ledger counts every token the provider bills, and DeepTutor re-sends the
# whole prompt (system prompt + 26 tool schemas + accumulated turn history) on
# every call, so a turn is charged in full rather than as an increment.  The
# 0919 formal run measured 11.0k-18.1k tokens per turn and still rising at turn
# 12; at that size the old 80000 ceiling admitted only 6 calls, which made the
# disclosed 12-call budget unreachable by construction and turned honest
# mid-task work into a "budget exhausted" zero (hidden test_001/test_002).
# 240000 = 12 turns x 20000 tokens keeps the call budget the binding limit and
# leaves the token budget as an outer safety rail.
PUBLIC_CASE_MAX_CALLS=12
PUBLIC_CASE_MAX_TOKENS=240000


def _completed_or_budget_limited(value):
    """Accept a completed response, or one finished inside the Candidate's own
    output budget.

    The latter arrives as HTTP 200 with its own id, complete usage and no error
    -- exactly what a real API client receives when it sets max_output_tokens --
    so treating it as a provider fault invents a failure production cannot
    produce. Nothing else about a response is admitted here.
    """
    if not isinstance(value, dict):
        return False
    if value.get('status') == 'completed':
        return True
    details = value.get('incomplete_details')
    return (value.get('status') == 'incomplete' and isinstance(details, dict)
            and details.get('reason') == 'max_output_tokens'
            and value.get('error') is None)

def capture_http_error(exc,capture,api_key):
 """Retain bounded private denial evidence without changing transport or retry policy."""
 headers={k:str(exc.headers.get(k))[:1024].replace(api_key,'[REDACTED]')
          for k in ERROR_HEADERS if exc.headers and exc.headers.get(k) is not None}
 evidence={'http_status':exc.code,'headers':headers,'capture_limit_bytes':MAX_ERROR_CAPTURE_BYTES,
           'received_bytes':0,'body_complete':False,'read_error':None,'retry_allowed':False,
           'client_identity_changed':False}
 try:
  # A single read1 keeps error diagnostics from waiting for a large/streaming
  # response to finish. Partial bytes are useful evidence, never a full response.
  raw=exc.read1(MAX_ERROR_CAPTURE_BYTES)
  evidence['received_bytes']=len(raw);capture.write(raw)
  length=headers.get('content-length','')
  evidence['body_complete']=length.isdecimal() and int(length)==len(raw) and int(length)<=MAX_ERROR_CAPTURE_BYTES
 except Exception as read_error:
  evidence['read_error']=type(read_error).__name__
 finally:exc.close()
 return evidence

def dotenv(path):
 result={}
 for raw in Path(path).read_text().splitlines():
  line=raw.strip()
  if line and not line.startswith('#') and '=' in line:
   k,v=line.split('=',1);result[k.strip()]=v.strip().strip('\'"')
 return result

def atomic_write(path,value):
 path.parent.mkdir(parents=True,exist_ok=True)
 temp=path.with_suffix(path.suffix+'.tmp')
 with temp.open('w') as handle:
  json.dump(value,handle,ensure_ascii=False,indent=2);handle.write('\n');handle.flush();os.fsync(handle.fileno())
 os.replace(temp,path)
 fd=os.open(path.parent,os.O_RDONLY)
 try:os.fsync(fd)
 finally:os.close(fd)

def response_usage(value):
 u=value.get('usage')
 names=('input_tokens','output_tokens','total_tokens')
 return {n:u[n] if isinstance(u,dict) and type(u.get(n)) is int and u[n]>=0 else None for n in names}

class BrokerState:
 def __init__(self,stats_file,max_calls,max_tokens,*,model=LOWER_MODEL,reasoning_effort=LOWER_EFFORT,role='lower'):
  if (model,reasoning_effort,role)!=(LOWER_MODEL,LOWER_EFFORT,'lower'):
   raise ValueError('task broker is exclusively the native medium lower transport')
  # Historical zero means the disclosed default, never an unlimited case.
  if max_calls == 0 and type(max_calls) is int:max_calls=PUBLIC_CASE_MAX_CALLS
  if max_tokens == 0 and type(max_tokens) is int:max_tokens=PUBLIC_CASE_MAX_TOKENS
  if type(max_calls) is not int or type(max_tokens) is not int or not 1<=max_calls<=PUBLIC_CASE_MAX_CALLS or not 1<=max_tokens<=PUBLIC_CASE_MAX_TOKENS:
   raise ValueError('case budget must be positive and cannot exceed public defaults %d calls / %d tokens'%(PUBLIC_CASE_MAX_CALLS,PUBLIC_CASE_MAX_TOKENS))
  self.stats_file=Path(stats_file);self.max_calls=max_calls;self.max_tokens=max_tokens
  self.model=model;self.reasoning_effort=reasoning_effort;self.lock=threading.Lock()
  self.stats={'schema_version':'agentswe-deeptutor-single-attempt/v4','budget_scope':'case-context/v1','configured_case_limits':{'max_calls':max_calls,'max_tokens':max_tokens},'context_budget_exceeded':{},'transport_measurement':MEASUREMENT,'role':role,'model':model,'reasoning_effort':reasoning_effort,
   'calls':0,'successful_calls':0,'failures':0,'provider_failures':0,'delivery_failures':0,'input_tokens':0,'output_tokens':0,'total_tokens':0,
   'unknown_usage_calls':0,'forced_model_overrides':0,'forced_effort_overrides':0,'budget_exceeded':False,'last_failure_classification':None,'logical_requests':{},'requests':[]}
  if self.stats_file.exists():
   saved=json.loads(self.stats_file.read_text())
   if saved.get('schema_version') not in {'agentswe-deeptutor-single-attempt/v2','agentswe-deeptutor-single-attempt/v3','agentswe-deeptutor-single-attempt/v4'}:raise RuntimeError('existing lower ledger schema mismatch')
   for key in ('role','model','reasoning_effort'):
    if saved.get(key)!=self.stats[key]:raise RuntimeError('existing lower ledger identity mismatch; refusing reset')
   if saved.get('schema_version')=='agentswe-deeptutor-single-attempt/v4' and (saved.get('budget_scope')!='case-context/v1' or saved.get('configured_case_limits')!={'max_calls':max_calls,'max_tokens':max_tokens}):
    raise RuntimeError('existing lower ledger case budget binding mismatch; refusing reset')
   self.stats=saved
  else:self.flush()
 def writable(self):
  return self.stats.get('schema_version')=='agentswe-deeptutor-single-attempt/v4' and self.stats.get('transport_measurement')==MEASUREMENT and self.stats.get('budget_scope')=='case-context/v1'
 def flush(self):
  if not self.writable():return
  self.stats['last_updated_at']=utc_now();atomic_write(self.stats_file,self.stats)
 def context_budget(self,context_id):
  intents=[v for v in self.stats['logical_requests'].values() if v['context_id']==context_id]
  calls=sum(v.get('transport_attempts',0) for v in intents)
  known=sum((v.get('usage') or {}).get('total_tokens') or 0 for v in intents)
  unknown=sum(bool(v.get('transport_attempts')) and (v.get('usage') or {}).get('total_tokens') is None for v in intents)
  return {'context_id':context_id,'calls':calls,'known_total_tokens':known,
          'unknown_usage_calls':unknown,'total_tokens':None if unknown else known,
          'max_calls':self.max_calls,'max_tokens':self.max_tokens,
          'budget_exceeded':bool(self.stats.get('context_budget_exceeded',{}).get(context_id)) or
              bool(self.max_calls and calls>self.max_calls) or bool(self.max_tokens and known>self.max_tokens),
          'token_limit_kind':'measured request-admission threshold; final response may cross it, overrun is explicit',
          'hard_token_ceiling_proven':False}
 def reserve(self,request_id,context_id,request):
  with self.lock:
   intents=self.stats['logical_requests']
   if request_id in intents:return dict(intents[request_id])
   if self.stats.get('transport_measurement')!=MEASUREMENT or self.stats.get('budget_scope')!='case-context/v1':
    raise RuntimeError('legacy lower ledger is read-only; no new request allowed')
   budget=self.context_budget(context_id)
   if any(v['context_id']==context_id and v['state']!='completed' for v in intents.values()) or budget['unknown_usage_calls']:
    return {'state':'blocked_by_prior_unknown_context'}
   if (self.max_calls and budget['calls']>=self.max_calls) or (self.max_tokens and budget['known_total_tokens']>=self.max_tokens):
    self.stats['context_budget_exceeded'][context_id]=True
    self.stats['budget_exceeded']=True;self.flush();return {'state':'budget_exceeded'}
   intents[request_id]={'state':'reserved_not_submitted','request_sha256':request_id,'context_id':context_id,'reserved_at':utc_now(),'transport_attempts':0,'usage':None}
   # Persist the normalized request alongside the intent before any HTTP call.
   request_dir=self.stats_file.parent/(self.stats_file.stem+'-requests');request_dir.mkdir(exist_ok=True)
   request_path=request_dir/(request_id+'.json')
   with request_path.open('x') as handle:json.dump(request,handle);handle.flush();os.fsync(handle.fileno())
   self.flush();return None
 def record_unsent_retry(self,error_type):
  """Count an attempt that failed before the provider received anything.

  These never reach transport_started, so the logical request keeps
  transport_attempts=0 until one attempt actually starts and no provider work
  is duplicated. Counted separately so the evidence shows recovery happened.
  """
  with self.lock:
   self.stats.setdefault('unsent_transport_retries',[]).append({'at':utc_now(),'error_type':error_type})
   self.flush()

 def transport_started(self,request_id):
  with self.lock:
   if not self.writable():raise RuntimeError('legacy lower ledger is read-only')
   intent=self.stats['logical_requests'][request_id]
   if intent['state']!='reserved_not_submitted' or intent['transport_attempts']!=0:raise RuntimeError('logical request may send only once')
   budget=self.context_budget(intent['context_id'])
   if self.max_calls and budget['calls']>=self.max_calls:raise RuntimeError('actual case lower POST budget exhausted')
   intent.update(state='submitted_or_unknown',submitted_at=utc_now(),transport_attempts=1)
   self.stats['calls']+=1;self.flush()
 def override(self,model,effort):
  with self.lock:
   if not self.writable():return
   self.stats['forced_model_overrides']+=int(model);self.stats['forced_effort_overrides']+=int(effort);self.flush()
 def finish(self,request_id,*,response=None,error=None,http_error_evidence=None):
  with self.lock:
   if not self.writable():raise RuntimeError('legacy lower ledger is read-only')
   intent=self.stats['logical_requests'][request_id]
   if intent['state'] not in {'submitted_or_unknown','reserved_not_submitted'}:raise RuntimeError('lower terminal request cannot be overwritten')
   attempts=intent['transport_attempts']
   ok=error is None
   if ok and not attempts:raise RuntimeError('completed response has no observed model POST')
   usage=response_usage(response) if ok and isinstance(response,dict) else {n:None for n in ('input_tokens','output_tokens','total_tokens')}
   if not attempts:usage={n:0 for n in ('input_tokens','output_tokens','total_tokens')}
   row={'request_sha256':request_id,'transport_attempts':attempts,'upstream_completion':('completed' if ok else 'unknown_or_failed') if attempts else 'not_submitted','error':error,**usage,'usage_state':('known' if all(v is not None for v in usage.values()) else 'unknown') if attempts else 'not_submitted'}
   if http_error_evidence is not None:row['http_error_evidence']=http_error_evidence
   if row['usage_state']=='unknown':self.stats['unknown_usage_calls']+=1
   if ok:
    response_dir=self.stats_file.parent/(self.stats_file.stem+'-responses');response_dir.mkdir(exist_ok=True)
    response_path=response_dir/(request_id+'.json')
    with response_path.open('x') as handle:json.dump(response,handle);handle.flush();os.fsync(handle.fileno())
    intent.update(response_path=str(response_path),response_sha256=hashlib.sha256(response_path.read_bytes()).hexdigest())
    self.stats['successful_calls']+=1
   else:
    self.stats['failures']+=1;self.stats['provider_failures']+=int(bool(attempts))
    if not attempts:self.stats['pre_transport_failures']=self.stats.get('pre_transport_failures',0)+1
   for k,v in usage.items():
    if v is not None:self.stats[k]+=v
   intent.update(state=('completed' if ok else 'unknown_or_failed') if attempts else 'pre_transport_failed',usage=usage,result=row)
   if self.context_budget(intent['context_id'])['budget_exceeded']:
    self.stats['context_budget_exceeded'][intent['context_id']]=True;self.stats['budget_exceeded']=True
   self.stats['requests'].append(row);self.stats['last_failure_classification']=None if ok else ('provider_infrastructure_error' if attempts else 'broker_infrastructure_error');self.flush()
 def completed_response(self,intent):
  if intent.get('state')!='completed':return None
  p=Path(intent['response_path'])
  if hashlib.sha256(p.read_bytes()).hexdigest()!=intent['response_sha256']:raise RuntimeError('completed response digest mismatch')
  return json.loads(p.read_text())
 def delivery_failure(self):
  with self.lock:
   if not self.writable():return
   self.stats['delivery_failures']+=1;self.stats['last_failure_classification']='broker_delivery_failure';self.flush()
 def public(self):
  with self.lock:
   result=json.loads(json.dumps(self.stats))
   if self.stats.get('budget_scope')=='case-context/v1':
    result['context_budgets']={key:self.context_budget(key) for key in sorted({v['context_id'] for v in self.stats['logical_requests'].values()})}
  result.update(max_calls=self.max_calls,max_tokens=self.max_tokens,pending_calls=sum(v['state']=='submitted_or_unknown' for v in result['logical_requests'].values()),unknown_calls=sum(v['state']!='completed' for v in result['logical_requests'].values()),max_upstream_attempts_per_logical_request=1)
  if result.get('transport_measurement')==MEASUREMENT:
   result['known_usage_subtotal']={k:result[k] for k in ('input_tokens','output_tokens','total_tokens')}
   result['unknown_usage_calls']+=result['pending_calls']
   if result['unknown_usage_calls']:
    for k in ('input_tokens','output_tokens','total_tokens'):result[k]=None
  return result

def make_handler(state,upstream,api_key):
 class Handler(http.server.BaseHTTPRequestHandler):
  protocol_version='HTTP/1.1'
  def log_message(self,*_):pass
  def send_json(self,status,value):
   raw=json.dumps(value,ensure_ascii=False).encode();self.send_response(status);self.send_header('Content-Type','application/json');self.send_header('Content-Length',str(len(raw)));self.end_headers()
   try:self.wfile.write(raw)
   except (BrokenPipeError,ConnectionResetError,OSError):state.delivery_failure()
  def do_GET(self):
   if self.path=='/healthz':return self.send_json(200,{'ok':True,'model':state.model,'reasoning_effort':state.reasoning_effort})
   if self.path=='/stats' and self.headers.get('Authorization')==f'Bearer {STATS_TOKEN}':return self.send_json(200,state.public())
   self.send_json(404,{'error':'not_found'})
  def do_POST(self):
   if self.path.rstrip('/')!='/v1/responses':return self.send_json(404,{'error':{'type':'unsupported_endpoint'}})
   if self.headers.get('Authorization')!=f'Bearer {PLACEHOLDER_TOKEN}':return self.send_json(401,{'error':{'type':'candidate_placeholder_required'}})
   try:
    length=int(self.headers.get('Content-Length','0'))
    if not 0<length<=MAX_BODY_BYTES:raise ValueError('request size')
    body=strict_json(self.rfile.read(length))
    if not isinstance(body,dict):raise ValueError('request object required')
    context=self.headers.get('X-AgentSWE-Context','')
    if not context or len(context)>256:raise ValueError('evaluator-owned context identity required')
    if body.get('stream') is True:raise ValueError('native adapter requires terminal JSON')
   except (ValueError,UnicodeError) as exc:return self.send_json(400,{'error':{'type':'invalid_lower_request','message':str(exc)}})
   reasoning=body.get('reasoning') or {}
   state.override(body.get('model')!=state.model,not isinstance(reasoning,dict) or reasoning.get('effort')!=state.reasoning_effort)
   body.update(model=state.model,reasoning={'effort':state.reasoning_effort},stream=True);body.pop('reasoning_effort',None)
   data=json.dumps(body,sort_keys=True,separators=(',',':')).encode();request_id=hashlib.sha256(context.encode()+b'\0'+data).hexdigest()
   try:
    prior=state.reserve(request_id,context,body)
    if prior is not None:
     if prior['state']=='budget_exceeded':return self.send_json(429,{'error':{'type':'budget_exceeded'},'retry_allowed':False})
     completed=state.completed_response(prior)
     if completed is None:return self.send_json(409,{'error':{'type':'prior_request_result_unknown'},'retry_allowed':False})
     return self.send_json(200,completed)
   except Exception:return self.send_json(502,{'error':{'type':'evaluator_ledger_failure'},'retry_allowed':False})
   parsed=None;failure=None;http_error_evidence=None
   submitted={'v':False}
   def _started():
    state.transport_started(request_id);submitted['v']=True
   unsent=0
   try:
    req=urllib.request.Request(upstream,data=data,method='POST',headers={'Authorization':f'Bearer {api_key}','Content-Type':'application/json','Accept':'text/event-stream, application/json','Accept-Encoding':'identity','User-Agent':'AgentSWE-Edit-Lower-Broker/1.0'})
    raw_dir=state.stats_file.parent/(state.stats_file.stem+'-raw');raw_dir.mkdir(exist_ok=True)
    while True:
     # A transport that never reached the provider is retried: nothing was sent,
     # billed or produced, so no result is replayed. transport_started stays
     # uncalled for those attempts, so the durable intent keeps
     # transport_attempts=0 until one attempt actually starts.
     capture_name=request_id+('' if not unsent else '.unsent%03d'%unsent)
     try:
      with (raw_dir/(capture_name+'.bin')).open('xb') as handle:
       capture=RedactedCapture(handle,api_key)
       try:
        with direct_opener(on_request_start=_started).open(req,timeout=MAX_CALL_SECONDS) as response:
         raw,streamed=read_response(response.read1,content_type=response.headers.get('Content-Type',''),is_success=True,capture=capture.write)
         parsed=streamed if streamed is not None else strict_json(raw)
       except urllib.error.HTTPError as exc:
        http_error_evidence=capture_http_error(exc,capture,api_key)
        raise
       finally:capture.finish();handle.flush();os.fsync(handle.fileno())
      break
     except (urllib.error.URLError,OSError) as exc:
      if submitted['v'] or isinstance(exc,urllib.error.HTTPError) or unsent>=UNSENT_MAX_RETRIES:raise
      unsent+=1;state.record_unsent_retry(type(exc).__name__);time.sleep(UNSENT_RETRY_BACKOFF_SECONDS)
    if not isinstance(parsed,dict) or not _completed_or_budget_limited(parsed) or not isinstance(parsed.get('id'),str) or not parsed['id']:raise ValueError('response_not_typed_completed')
   except Exception as exc:failure=(type(exc).__name__+':'+str(exc)[:160]).replace(api_key,'[REDACTED]')
   if http_error_evidence is not None:
    raw_path=raw_dir/(capture_name+'.bin')
    http_error_evidence.update(raw_capture_sha256=hashlib.sha256(raw_path.read_bytes()).hexdigest(),
                              redacted_capture_bytes=raw_path.stat().st_size)
    atomic_write(raw_dir/(request_id+'.http-error.json'),http_error_evidence)
   # A completed provider result is durable before delivery, even if the client went away.
   try:state.finish(request_id,response=parsed,error=failure,http_error_evidence=http_error_evidence)
   except Exception:return self.send_json(502,{'error':{'type':'evaluator_ledger_failure'},'retry_allowed':False})
   if failure:return self.send_json(502,{'error':{'type':'provider_infrastructure_error','message':failure},'retry_allowed':False})
   self.send_json(200,parsed)
 return Handler

def main():
 p=argparse.ArgumentParser(description=__doc__);p.add_argument('--credential-file',type=Path,required=True);p.add_argument('--stats-file',type=Path,required=True);p.add_argument('--bind',default='127.0.0.1');p.add_argument('--port',type=int,required=True);p.add_argument('--upstream',default=os.environ.get('AGENTSWE_RESPONSES_UPSTREAM',DEFAULT_UPSTREAM));p.add_argument('--max-calls',type=int,default=PUBLIC_CASE_MAX_CALLS);p.add_argument('--max-tokens',type=int,default=PUBLIC_CASE_MAX_TOKENS);p.add_argument('--builder',action='store_true');a=p.parse_args()
 if a.builder:raise SystemExit('Builder must use the evaluator shared xhigh runtime')
 credentials=dotenv(a.credential_file);key=credentials.get('DEEPSEEK_API_KEY') or credentials.get('OPENAI_API_KEY')
 if not key or key==PLACEHOLDER_TOKEN:raise SystemExit('evaluator-owned upstream credential unavailable')
 state=BrokerState(a.stats_file,a.max_calls,a.max_tokens);server=http.server.ThreadingHTTPServer((a.bind,a.port),make_handler(state,a.upstream,key))
 try:server.serve_forever()
 finally:server.server_close()
if __name__=='__main__':main()
