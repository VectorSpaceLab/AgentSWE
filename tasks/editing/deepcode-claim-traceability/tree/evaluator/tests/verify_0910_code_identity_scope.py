"""Offline interface regression only: mock Code completion, no model evidence.

Uses the actual old frozen tree read-only and disposable copies. Synthetic fatal
records exercise Result identity plumbing; they are not genuine Candidate zeros.
"""
import argparse,hashlib,json,runpy,shutil,sys
from pathlib import Path
from unittest.mock import patch

ROOT=Path(__file__).resolve().parents[2]
OLD=Path('@@AGENTSWE_EDITING_RUNS@@/smoke/deepcode/0905-smoke-current-004/lifecycle')
EE='ee47091920929eb12a39ce687d67b3f8a8cb794e0de95b733e49bc0e8aaf4999'
FF='ffab63067c7b6c7da0f00db0fa965268d7963ea222f86fb283a039fa6e07d7b3'

def main():
    p=argparse.ArgumentParser();p.add_argument('--output',type=Path,required=True);a=p.parse_args()
    a.output.mkdir(parents=True,exist_ok=False)
    wrapper=runpy.run_path(str(ROOT/'evaluator/formal_axes.py'))
    shared=wrapper['SHARED'];hooks=wrapper['IDENTITY_HOOKS']
    native,create=hooks['algorithms']()
    original=(OLD/'freeze_manifest.json').read_bytes();old=json.loads(original)
    rows=[];commands=[]
    def check(name,condition,details=None):
        row={'name':name,'passed':bool(condition),'details':details or {}}
        rows.append(row);print(json.dumps(row),flush=True)
    def write(path,value):
        path.parent.mkdir(parents=True,exist_ok=True);path.write_text(json.dumps(value,indent=2)+'\n')
    bridge=hooks['code_frozen_identity'](old,OLD)
    check('actual_original_freeze_two_digests',bridge['lifecycle_candidate_digest']==EE and bridge['code_candidate_digest']==FF and bridge['freeze_sha256']==hashlib.sha256(original).hexdigest() and shared['checked_code_frozen_identity'](bridge,old,OLD,Path(old['candidate_path']))==FF,bridge)
    scope=hooks['prepare_code_evidence_scope'](Path(old['candidate_path']),FF,a.output)
    write(a.output/'actual-scope.json',scope)
    included=set(scope['mandatory_included_paths'])
    check('actual_scope_complete_change_dependency_and_cap',scope['valid'] and scope['complete_change_coverage'] and scope['source_pack_bytes']<6000000 and set(scope['changed_paths']+scope['added_paths'])<=included and all(p in included for p in ('tests/test_desktop_release_scripts.py','desktop/scripts/audit-licenses.py','desktop/scripts/create-release-config.py','desktop/sidecar-requirements.lock','desktop/package.json','desktop/src/generated/app-server.ts','docs/PRIVACY_AND_DIAGNOSTICS.md')),{'bytes':scope['source_pack_bytes'],'modified':scope['changed_paths'],'added':scope['added_paths']})
    check('wrapper_live_namespace_hooks',all(shared[name] is hooks[name] for name in ('frozen_identity_errors','code_frozen_identity','prepare_code_evidence_scope')))
    def fixture(name):
        run=a.output/name;run.mkdir()
        candidate=run/'candidate';shutil.copytree(Path(old['candidate_path']),candidate,symlinks=True)
        freeze={**old,'candidate_path':str(candidate)};write(run/'freeze_manifest.json',freeze)
        task=run/'task.md';task.write_text('Synthetic offline interface fixture. No model execution or score claim.\n')
        private=run/'private.json';write(private,{'case_id':'test_001','candidate_visible':False,'executed_task_sha256':hashlib.sha256(task.read_bytes()).hexdigest()})
        failure=run/'synthetic-build.json';write(failure,{'test_fixture':True,'purpose':'interface-only fatal Result contract identity regression'})
        record={'case_id':'test_001','candidate_digest':EE,'classification':'candidate_build_failure','execution_attempted':True,'environment_preflight':{'valid':True},'failure_attribution':{'party':'candidate','observed_by':'evaluator','fatal':True,'reason':'TEST-ONLY synthetic build failure; not genuine Candidate evidence','evidence_paths':[str(failure)]},'executed_task_path':str(task),'executed_task_sha256':hashlib.sha256(task.read_bytes()).hexdigest(),'private_oracle_comparison_path':str(private),'private_oracle_comparison_sha256':hashlib.sha256(private.read_bytes()).hexdigest()}
        write(run/'hidden-after-freeze-attestation.json',{'expected_cases':['test_001'],'executed_cases':['test_001'],'cases':[record],'all_cases_started_after_freeze':True,'frozen_digest_stable':True})
        (run/'placeholder').write_text('not-a-credential')
        return run
    fake_judge=a.output/'fake-code-entry.py';fake_judge.write_text('# test-only, all invocation intercepted before execution\n')
    def code(command,**kwargs):
        commands.append(command)
        assert command[command.index('--expected-candidate-digest')+1]==FF
        out=Path(command[command.index('--output-dir')+1]);out.mkdir(parents=True,exist_ok=True)
        contract={'candidate_digest':FF,'contract_valid':True,'code_score_publishable':True,'code_score':0,'code_raw_score':0,'code_dimensions':{n:{'score':0,'max':v} for n,v in shared['CODE_MAXIMA'].items()},'judge':{'model':'gpt-5.6-sol','reasoning_effort':'max'},'provider_usage':{'logical_requests':1,'completed_responses':1,'transport_attempts':1,'input_tokens':10,'output_tokens':5,'total_tokens':15},'test_fixture':True}
        write(out/'code_score_contract.json',contract)
        return type('Completed',(),{'returncode':0,'stdout':'mock Code interface only','stderr':''})()
    def invoke(run,extra=None):
        values={'CODE_JUDGE':fake_judge,**(extra or {})}
        with patch.dict(shared,values),patch.object(shared['subprocess'],'run',side_effect=code) as proc,patch.dict(shared,{'judge_broker_stats':lambda *_: (_ for _ in ()).throw(AssertionError('Result API forbidden'))}),patch('builtins.print'):
            ret=wrapper['main'](['--run-dir',str(run),'--credential-file',str(run/'placeholder'),'--acceptance-cases','test_001'])
        return ret,json.loads((run/'acceptance_aggregation.json').read_text()),proc.call_count
    run=fixture('mock-interface')
    ret,result,count=invoke(run)
    intent=json.loads((run/'formal_scoring/code_axis/scoring_intent.json').read_text())
    result_contract=json.loads(Path(result['result_judge_contracts']['test_001']).read_text())
    check('Code_argv_intent_contract_ff_Result_and_freeze_ee',ret==0 and count==1 and intent['candidate_digest']==FF and result_contract['candidate_digest']==EE and json.loads((run/'freeze_manifest.json').read_text())['candidate_digest']==EE and json.loads((run/'formal_scoring/code_axis/code_score_contract.json').read_text())['candidate_digest']==FF and bool(intent['frozen_identity_sha256']),{'code_intent':intent,'result_digest':result_contract.get('candidate_digest'),'mock_code_calls':count})
    ret,result,count=invoke(run)
    check('repeat_cache_zero_extra_Code_or_Result',ret==0 and count==0 and result['fresh_semantic_judge_cases']==[])
    bridge_path=run/'formal_scoring/code_axis/code_frozen_identity.json'
    cached_bridge=bridge_path.read_bytes();bad=json.loads(cached_bridge);bad['algorithms']['code']['sha256']='0'*64;write(bridge_path,bad)
    ret,result,count=invoke(run);check('altered_bridge_rejected_without_request',ret==2 and count==0 and result['code_score_publishable'] is False and result['result_axis']=='N/A')
    bridge_path.write_bytes(cached_bridge)
    freeze_path=run/'freeze_manifest.json';frozen=freeze_path.read_bytes();bad=json.loads(frozen);bad['frozen_at']='altered original freeze';write(freeze_path,bad)
    ret,result,count=invoke(run);check('altered_original_freeze_rejected_without_request',ret==2 and count==0 and result['code_score_publishable'] is False and result['result_axis']=='N/A')
    freeze_path.write_bytes(frozen)
    source=run/'candidate/workflows/traceability.py';data=source.read_bytes();source.chmod(0o644);source.write_bytes(data+b'\n# offline tamper control\n')
    ret,result,count=invoke(run);check('altered_frozen_source_rejected_without_request',ret==2 and count==0 and result['code_score_publishable'] is False)
    source.write_bytes(data);source.chmod(0o444)
    # The lifecycle ignores pycache, whereas Code must still protect those bytes.
    cache=next((run/'candidate').rglob('*.pyc'));cache_bytes=cache.read_bytes();cache.chmod(0o644);cache.write_bytes(cache_bytes+b'changed-cache')
    ret,result,count=invoke(run);check('changed_cache_bytes_rejected_under_same_lifecycle',native['tree_digest'](run/'candidate')==EE and ret==2 and count==0)
    cache.write_bytes(cache_bytes);cache.chmod(0o444)
    candidate=run/'candidate';candidate.chmod(0o755)
    future=candidate/'future_candidate_module.py';future.write_text('from core.domain.artifact import Artifact\n')
    future_digest=create['tree_digest'](candidate);future_scope=hooks['prepare_code_evidence_scope'](candidate,future_digest,a.output)
    check('future_candidate_changes_recomputed_not_hardcoded',future_scope['valid'] and 'future_candidate_module.py' in future_scope['added_paths'] and 'future_candidate_module.py' in future_scope['mandatory_included_paths'])
    future.unlink()
    dot=candidate/'.gitignore';prior=dot.read_bytes();dot.chmod(0o644);dot.write_bytes(prior+b'\nfuture-candidate-policy\n')
    try:
        s=hooks['prepare_code_evidence_scope'](candidate,create['tree_digest'](candidate),a.output);rejected=not s['valid'] and not s['complete_change_coverage']
    except ValueError:rejected=True
    check('new_unrepresentable_dot_change_fails_closed',rejected);dot.write_bytes(prior);dot.chmod(0o444)
    lock=candidate/'desktop/package-lock.json';prior=lock.read_bytes();lock.chmod(0o644);lock.write_bytes(prior+b'\n')
    try:hooks['prepare_code_evidence_scope'](candidate,create['tree_digest'](candidate),a.output);rejected=False
    except ValueError as exc:rejected='unchanged Create policy' in str(exc)
    check('modified_optional_lock_disables_exclusion_and_obeys_cap',rejected);lock.write_bytes(prior);lock.chmod(0o444)
    external=a.output/'private-canary';external.write_text('private synthetic canary');link=candidate/'external-link';link.symlink_to(external)
    try:hooks['code_frozen_identity'](json.loads(frozen),run);rejected=False
    except ValueError as exc:rejected='symlink' in str(exc)
    check('external_symlink_rejected_before_hash_read',rejected);link.unlink()
    # End-of-request equality rejects a bridge which changes even while both
    # digest strings remain valid; only mocked completion occurs in this test.
    final_run=fixture('mock-end-bridge-change');calls=[0]
    def moving_bridge(freeze,root):
        b=hooks['code_frozen_identity'](freeze,root);calls[0]+=1
        if calls[0]>=2:b['algorithms']['code']['offline_mutation']=True
        return b
    ret,result,count=invoke(final_run,{'code_frozen_identity':moving_bridge})
    check('bridge_end_recheck_rejects_midrequest_change',ret==2 and count==1 and any('bridge changed during scoring' in r for r in result['reasons']))
    check('historical_freeze_and_actual_source_unchanged',(OLD/'freeze_manifest.json').read_bytes()==original and native['tree_digest'](Path(old['candidate_path']))==EE and create['tree_digest'](Path(old['candidate_path']))==FF)
    report={'passed':all(r['passed'] for r in rows),'checks':rows,'external_provider_calls':0,'actual_lower_calls':0,'actual_Result_calls':0,'mock_Code_completions':len(commands),'synthetic_Result_zero_fixtures_not_candidate_evidence':True,'task_ready_claim':False,'stage':str(ROOT),'shared_implementation':str(wrapper['IMPLEMENTATION']),'shared_sha256':hashlib.sha256(Path(wrapper['IMPLEMENTATION']).read_bytes()).hexdigest()}
    write(a.output/'verification.json',report)
    return 0 if report['passed'] else 2

if __name__=='__main__':raise SystemExit(main())
