"""Full native public/hidden routes with local scripted Responses and no score.

Only independent Result is stopped after its real scoring inputs are assembled.
The scripted provider requests read_file, write_file and final partial JSON;
the evaluator never writes agent_result.json or performs benchmark repair work.
"""
import argparse,hashlib,http.server,json,re,sys,threading,time
from pathlib import Path
from unittest.mock import patch
ROOT=Path(__file__).resolve().parents[2]
sys.path.insert(0,str(ROOT));sys.path.insert(0,'@@AGENTSWE_EDITING_CONTROL@@')
from agentloop.evaluator.broker import BrokerState,handler
from agentloop.evaluator.controller import Controller
from agentloop.evaluator.hidden_controller import _execute_cases
from agentloop.protocol import tree_digest,write_json
from execution_contract import classify_candidate_execution
import execution_scoring
from validate_formal_config import tree_digest as task_digest

def main():
 p=argparse.ArgumentParser();p.add_argument('--output',type=Path,required=True);p.add_argument('--single',action='store_true');a=p.parse_args()
 a.output.mkdir(parents=True,exist_ok=False);repository=(ROOT/'.runtime/candidate-smoke/repository').resolve()
 before=tree_digest(repository);source=task_digest(ROOT);requests=[];prepared=[];active={};rows=[]
 def artifact(case):return {'case_id':case,'observations':[{'status':'incomplete','scripted_provider_probe':True,'note':'No benchmark solution or score claimed.'}]}
 class Provider(http.server.BaseHTTPRequestHandler):
  def log_message(self,*args):pass
  def do_POST(self):
   body=json.loads(self.rfile.read(int(self.headers['Content-Length'])))
   case=active.get('case_id')
   if not case:
    content=json.dumps(body.get('input',[]),ensure_ascii=False);matches=re.findall(r'(?:dev|test)_[0-9]{3}',content)
    case=matches[0] if matches else 'unbound'
   history=body.get('input',[]);returns=[v for v in history if isinstance(v,dict) and v.get('type')=='function_call_output']
   read=any(v.get('call_id')=='scripted_read_'+case for v in returns)
   wrote=any(v.get('call_id')=='scripted_write_'+case for v in returns)
   requests.append({'case_id':case,'read_tool_return':read,'write_tool_return':wrote,'native_write_succeeded':any(v.get('call_id')=='scripted_write_'+case and 'Refused path' not in str(v.get('output')) and 'Error' not in str(v.get('output')) for v in returns),'real_manifest_return':any('change_source' in json.dumps(v.get('output')) for v in returns),'model':body.get('model'),'effort':body.get('reasoning',{}).get('effort')})
   terminal=json.dumps(artifact(case),ensure_ascii=False)
   if not read:item={'type':'function_call','id':'fc_read_'+case,'call_id':'scripted_read_'+case,'name':'read_file','arguments':json.dumps({'file_path':'/impact-manifest.json'}),'status':'completed'}
   elif not wrote:item={'type':'function_call','id':'fc_write_'+case,'call_id':'scripted_write_'+case,'name':'write_file','arguments':json.dumps({'file_path':'/openwiki/agent_result.json','content':terminal}),'status':'completed'}
   else:item={'type':'message','id':'msg_'+case,'role':'assistant','status':'completed','content':[{'type':'output_text','text':terminal,'annotations':[]}]}
   response={'id':'resp_scripted_'+str(len(requests)),'object':'response','created_at':int(time.time()),'status':'completed','model':'gpt-5.6-sol','output':[item],'usage':{'input_tokens':10,'output_tokens':10,'total_tokens':20}}
   raw=('event: response.completed\ndata: '+json.dumps({'type':'response.completed','response':response})+'\n\n').encode()
   self.send_response(200);self.send_header('content-type','text/event-stream');self.send_header('content-length',str(len(raw)));self.end_headers();self.wfile.write(raw)
 upstream=http.server.ThreadingHTTPServer(('127.0.0.1',0),Provider);threading.Thread(target=upstream.serve_forever,daemon=True).start()
 credential=a.output/'synthetic.env';credential.write_text('OPENAI_API_KEY=scripted-no-real-provider\n')
 state=BrokerState(credential,a.output/'broker-stats.json','all-entrypoints-scripted')
 broker=http.server.ThreadingHTTPServer(('127.0.0.1',0),handler(state,f'http://127.0.0.1:{upstream.server_port}/v1/responses'))
 threading.Thread(target=broker.serve_forever,daemon=True).start();endpoint=f'http://127.0.0.1:{broker.server_port}/v1/responses'
 def result_prepare(**kw):
  verdict=classify_candidate_execution(kw['execution_record'],case_id=kw['case_id'],candidate_digest=kw['candidate_digest'])
  prepared.append({'case_id':kw['case_id'],'verdict':verdict,'artifact':str(kw['artifact']),'native_evidence':str(kw['native_evidence']),'private_oracle':str(kw['private_oracle'])})
  return {'classification':verdict['classification'],'contract_valid':verdict['contract_valid'],'score':None,'result_request_sent':False,'scripted_probe':True}
 def check(case,record,out,entry):
  launch=json.loads((out/'launcher_result.json').read_text());resources=launch.get('case_resource_contract',{});fixture=json.loads((out/'fixture-materialization.json').read_text());context=json.loads((out/'logical-context.json').read_text())
  verdict=classify_candidate_execution(record,case_id=case,candidate_digest=before);seen=[v for v in requests if v['case_id']==case]
  checks={'actual_native_read_and_write_tools':any(v['read_tool_return'] and v['write_tool_return'] and v['native_write_succeeded'] and v['real_manifest_return'] for v in seen),
   'native_partial_artifact_scoreable':verdict['classification']=='scoreable' and record.get('ordinary_partial_artifact_scoreable') is True,
   'terminal_authorship_verified':record.get('artifact_validation',{}).get('valid') is True,
   'all_heavy_work_in_600s_4GiB_scope':resources.get('memory_bytes')==4294967296 and 599<=resources.get('timeout_seconds',0)<=600 and resources.get('elapsed_seconds',601)<=600,
   'fixture_and_observer_in_same_scope':resources.get('cgroup') in fixture.get('cgroup','') and launch.get('semantic_observer_in_case_budget') is True,
   'source_world_candidate_bound':context.get('task_source_digest')==source and context.get('candidate_digest')==before,
   'network_and_private_files_isolated':launch.get('sandbox',{}).get('network_namespace_isolated') is True and launch.get('sandbox',{}).get('filesystem_isolated') is True,
   'cleanup_empty':resources.get('cleanup',{}).get('complete') is True}
  row={'case_id':case,'entrypoint':entry,'passed':all(checks.values()),'checks':checks,'resources':resources,'verdict':verdict,'scripted_provider_requests':len(seen),'execution_record':str(out/'execution_record.json')}
  rows.append(row);write_json(out/'native-entry-verification.json',row);print(json.dumps({'case_id':case,'passed':row['passed'],'checks':checks}),flush=True)
 try:
  for case in ('dev_001',) if a.single else ('dev_001','dev_002'):
   active['case_id']=case;controller=Controller(repository,ROOT,a.output/'public',endpoint,dev_cases=(case,),pilot_not_formal=True,result_judge_endpoint='http://127.0.0.1:1')
   controller.active_attempt=a.output/'public'/case
   with patch.object(execution_scoring,'judge_execution_case',result_prepare):public=controller._evaluate_dev(repository,1)
   check(case,public[case]['execution'],controller.active_attempt/'dev'/case,'Controller._evaluate_dev')
  active.clear()
  if not a.single:
   frozen=a.output/'scripted-hidden';frozen.mkdir();manifest=frozen/'diagnostic-manifest.json'
   freeze={'source_submission':0,'candidate_digest':before,'repository_digest':before,'scripted_diagnostic_only':True,'not_a_Builder_freeze':True};write_json(manifest,freeze)
   suite,_=_execute_cases(manifest,frozen/'hidden-suite.json',endpoint,state.instance_id,ROOT,600,freeze,repository,{'path':'scripted-probe-not-a-formal-gate','hidden_started_at':'scripted-native-verification'})
   for case,record in suite['cases'].items():check(case,record,frozen/'hidden'/case,'hidden_controller._execute_cases')
  public=state.stats;report={'passed':all(r['passed'] for r in rows) and tree_digest(repository)==before and source==task_digest(ROOT),'cases':rows,'provider_calls':0,'scripted_provider_requests':len(requests),'requests':requests,'stage_task_digest':source,'candidate_digest':before,'original_candidate_unchanged':tree_digest(repository)==before,'result_preparation':prepared,'durable_single_attempts':all(r['transport_attempts']==1 for r in public['requests']),'unknown_calls':public['unknown_calls'],'quality_score_claimed':False,'Builder_round_claimed':False,'formal_freeze_claimed':False}
  write_json(a.output/'verification.json',report);return 0 if report['passed'] else 2
 finally:
  broker.shutdown();broker.server_close();upstream.shutdown();upstream.server_close()
if __name__=='__main__':raise SystemExit(main())

