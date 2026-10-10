"""All eight native dev/hidden entrypoints, scripted local provider, no external API.

Only independent Result is withheld after its exact native scoring inputs have
been assembled. This probe does not produce a benchmark score or Builder round.
"""
import argparse,http.server,importlib.util,json,sys,threading,time
from pathlib import Path
from unittest.mock import patch
ROOT=Path(__file__).resolve().parents[2];sys.path.insert(0,str(ROOT));sys.path.insert(0,str(ROOT/"harbor"));sys.path.insert(0,'@@AGENTSWE_EDITING_CONTROL@@')
from agentloop.broker import BrokerState,make_handler
from agentloop.run_hidden import _case
from agentloop.protocol import tree_digest
from execution_contract import classify_candidate_execution
import execution_scoring

def main():
 p=argparse.ArgumentParser();p.add_argument('--repository',type=Path,required=True);p.add_argument('--python',required=True);p.add_argument('--output',type=Path,required=True);a=p.parse_args();a.output.mkdir(parents=True,exist_ok=False)
 spec=importlib.util.spec_from_file_location('deeptutor_native_entry_probe',ROOT/'harbor/formal_one_stop.py');formal=importlib.util.module_from_spec(spec);sys.modules[spec.name]=formal;spec.loader.exec_module(formal)
 active={};requests=[];prepared=[];rows=[];before_digest=tree_digest(a.repository)
 class Provider(http.server.BaseHTTPRequestHandler):
  def log_message(self,*_):pass
  def do_POST(self):
   body=json.loads(self.rfile.read(int(self.headers['Content-Length'])));requests.append({'case_id':active['case_id'],'body':body})
   tools=body.get('tools',[])
   if not active.get('tool_sent') and any(t.get('name')=='mastery_remediation_status' for t in tools):
    active['tool_sent']=True;output=[{'type':'function_call','call_id':'scripted_status_'+active['case_id'],'name':'mastery_remediation_status','arguments':'{}'}]
   else:
    artifact={'schema_version':'v1','case_id':active['case_id'],'status':'incomplete','summary':'Scripted local transport probe; no benchmark task success claimed.','artifacts':{'scripted_provider':True,'no_semantic_score_claimed':True}}
    output=[{'type':'message','role':'assistant','content':[{'type':'output_text','text':json.dumps(artifact)}]}]
   raw=json.dumps({'id':'resp_native_'+str(len(requests)),'status':'completed','output':output,'usage':{'input_tokens':8,'output_tokens':4,'total_tokens':12}}).encode()
   self.send_response(200);self.send_header('Content-Type','application/json');self.send_header('Content-Length',str(len(raw)));self.end_headers();self.wfile.write(raw)
 upstream=http.server.ThreadingHTTPServer(('127.0.0.1',0),Provider);threading.Thread(target=upstream.serve_forever,daemon=True).start()
 state=BrokerState(a.output/'broker-stats.json',0,0);broker=http.server.ThreadingHTTPServer(('127.0.0.1',0),make_handler(state,f'http://127.0.0.1:{upstream.server_port}/v1/responses','scripted-not-a-real-secret'));threading.Thread(target=broker.serve_forever,daemon=True).start();endpoint=f'http://127.0.0.1:{broker.server_port}/v1/responses'
 def prepare_result(**kwargs):
  record=kwargs['execution_record'];v=classify_candidate_execution(record,case_id=kwargs['case_id'],candidate_digest=kwargs['candidate_digest']);prepared.append({'case_id':kwargs['case_id'],'verdict':v,'artifact':str(kwargs['artifact']),'native_evidence':str(kwargs['native_evidence']),'private_oracle':str(kwargs['private_oracle'])})
  return {'score':None,'classification':v['classification'],'contract_valid':v['contract_valid'],'reason':'Independent Result deliberately withheld in provider-free native-entry verification','result_request_sent':False}
 try:
  for case_id in ('dev_001','dev_002',*(f'test_{i:03d}' for i in range(1,7))):
   active.clear();active['case_id']=case_id;offset=len(requests);out=a.output/case_id
   if case_id.startswith('dev_'):
    with patch.object(execution_scoring,'judge_execution_case',prepare_result):
     record=formal.run_public_case(case_id=case_id,repository=a.repository,output=out,broker_endpoint=endpoint,runtime_python=a.python)
    native=json.loads((out/'public_execution_evidence.json').read_text())
   else:
    record=_case(case_id=case_id,frozen=a.repository,endpoint=endpoint,output_root=a.output,python_executable=a.python,launcher_path=ROOT/'agentloop/lower_agent_launcher.py');native=record
   launch=json.loads((out/'launcher_result.json').read_text());resource=launch.get('case_resource_contract',{});context=json.loads((out/'logical-context.json').read_text());copy=json.loads((out/'source-copy-observation.json').read_text());percase=requests[offset:]
   verdict=classify_candidate_execution(native,case_id=case_id,candidate_digest=before_digest)
   checks={'native_product_and_tool_consumed':bool(active.get('tool_sent')) and any(any(item.get('type')=='function_call_output' for item in q['body'].get('input',[]) if isinstance(item,dict)) for q in percase),
    'partial_native_artifact_scoreable':verdict['classification']=='scoreable','product_authored_terminal':native.get('artifact_validation',{}).get('valid') is True,
    'copy_in_same_scope':resource.get('cgroup') in copy.get('cgroup','') and copy.get('copy_completed_inside_case_scope') is True,
    'aggregate_600s_4GiB':resource.get('timeout_seconds')==600 and resource.get('memory_bytes')==4294967296 and resource.get('elapsed_seconds',601)<=600,
    'cleanup_complete':resource.get('cleanup',{}).get('complete') is True,
    'fixture_product_observer_inside_scope':launch.get('oracle_observation_exit_code')==0 and (out/'fixture/fixture-observation.json').is_file(),
    'source_world_candidate_request_binding':all(context.get(k) for k in ('task_source_digest','candidate_digest','executed_task_sha256','runtime_nonce','context_id')),
    'network_namespace_isolated':launch.get('sandbox',{}).get('network_namespace')=='isolated'}
   row={'case_id':case_id,'entrypoint':'private Builder public-case handler' if case_id.startswith('dev_') else 'hidden runner _case','checks':checks,'passed':all(checks.values()),'scripted_provider_requests':len(percase),'lower_classification':record.get('classification'),'semantic_score':None,'result_request_sent':False,'resources':resource};rows.append(row);(out/'native-entry-verification.json').write_text(json.dumps(row,indent=2)+'\n');print(json.dumps({k:row[k] for k in ('case_id','passed','checks','scripted_provider_requests')}),flush=True)
  ledger=state.public();report={'passed':all(row['passed'] for row in rows) and tree_digest(a.repository)==before_digest,'cases':rows,'provider_calls':0,'scripted_provider_requests':len(requests),'ledger_requests':ledger['requests'],'result_preparation':prepared,'original_candidate_digest':before_digest,'original_candidate_unchanged':tree_digest(a.repository)==before_digest,'mock_boundary':'scripted upstream response + deliberately withheld independent Result only; native product tools/fixtures/observer/isolation execute unchanged','semantic_score_claimed':False,'builder_round_claimed':False}
  (a.output/'verification.json').write_text(json.dumps(report,indent=2)+'\n');return 0 if report['passed'] else 2
 finally:
  broker.shutdown();broker.server_close();upstream.shutdown();upstream.server_close()
if __name__=='__main__':raise SystemExit(main())
