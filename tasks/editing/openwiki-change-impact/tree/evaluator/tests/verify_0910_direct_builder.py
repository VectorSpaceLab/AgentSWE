"""Actual native direct consumer with network-none scripted feedback controls.

No real model or semantic score: actual Codex/Harbor, patch builds and socket
feedback; public judgments are explicitly synthetic fixtures.
"""
import argparse,hashlib,json,os,subprocess,sys,time
from pathlib import Path
from unittest.mock import patch

def main():
    p=argparse.ArgumentParser();p.add_argument('--source',type=Path,required=True);p.add_argument('--run',type=Path,required=True);p.add_argument('--negative-healthcheck',action='store_true');p.add_argument('--negative-runtime',action='store_true');p.add_argument('--reconnect-once',action='store_true');p.add_argument('--single-submission',action='store_true');p.add_argument('--no-replay',action='store_true');a=p.parse_args()
    a.run.mkdir(parents=True,exist_ok=False)
    sys.path[:0]=[str(a.source),'@@AGENTSWE_EDITING_CONTROL@@']
    from harbor import formal_one_stop as f
    from validate_formal_config import tree_digest
    from agentloop.protocol import write_json
    before=tree_digest(a.source);public=a.run/'builder_public_package'
    manifest=f.stage_public(a.source,public)
    workspace=a.run/'builder_workspace/submission';workspace.mkdir(parents=True)
    credential=a.run/'synthetic.env';credential.write_text('GATEWAY_API_KEY=synthetic-no-real-credential\n');credential.chmod(0o600)
    private=a.run/'private-canary';private.write_text('PRIVATE_HOST_CANARY_NOT_FOR_BUILDER')
    life=f.BuilderLifecycle(a.run,workspace,'http://127.0.0.1:1/v1/responses',credential,'http://127.0.0.1:1',max_dev_rounds=2)
    life.start()
    provider=a.run/'provider.toml';f.direct_builder_runtime().write_provider(provider,'http://127.0.0.1:18734/v1')
    dependencies=Path('@@AGENTSWE_EDITING_TASKS@@/openwiki-change-impact/tree/.runtime/candidate-smoke/repository/node_modules')
    config=f.stage_builder_task(a.run,public,workspace,life,provider,f.BUILDER_IMAGE)
    fixture=a.run/'fixture';fixture.mkdir()
    origin=Path('@@AGENTSWE_EDITING_CONTROL@@/owner-b-oh-native-fixture-provider.py')
    provider_text=origin.read_text().replace("cmd=f'python3 /probe/fixture_delivery.py {number}; submit_dev_candidate --wait'", "cmd=f'python3 /probe/fixture_delivery.py {number}'+(' '+ack if number==2 else '')+'; submit_dev_candidate --wait'")
    provider_text=provider_text.replace("'n':counter,", "'n':counter,'observed_epoch':__import__('time').time(),")
    if a.single_submission:
        provider_text=provider_text.replace('if number>2:', 'if number>1:')
    if a.reconnect_once:
        provider_text=provider_text.replace("  if counter>100:", "  if counter==1:\n   body=b'event: response.created\\ndata: {\"type\":\"response.created\",\"response\":{\"id\":\"fixture_partial\",\"status\":\"in_progress\",\"output\":[]}}\\n\\n';self.send_response(200);self.send_header('Content-Type','text/event-stream');self.send_header('Content-Length',str(len(body)));self.end_headers();self.wfile.write(body);self.wfile.flush();return\n  if counter>100:")
    if a.no_replay:
        provider_text=provider_text.replace('if number>2:', 'if number>3:')
        provider_text=provider_text.replace("cmd=f'python3 /probe/fixture_delivery.py {number}'+(' '+ack if number==2 else '')+'; submit_dev_candidate --wait'", "cmd=f'python3 /probe/fixture_delivery.py {number}; submit_dev_candidate --wait | tee /probe/submission-{number}.json'")
        provider_text=provider_text.replace("    if number==2:cmd+=' --feedback-digest '+ack", '')
    (fixture/'provider.py').write_text(provider_text)
    delivery_script=r'''import hashlib,json,subprocess,sys
from pathlib import Path
n=int(sys.argv[1]);status=json.loads(subprocess.check_output(['submit_dev_candidate','--status'],text=True))['payload']
if n==1:
    root=Path('/builder-package');manifest=json.loads((root/'PUBLIC_PACKAGE_MANIFEST.json').read_text())
    files=manifest['visible_file_sha256']
    visible=all((root/p).is_file() and hashlib.sha256((root/p).read_bytes()).hexdigest()==sha for p,sha in files.items())
    work=Path('/workspace/worktree');baseline=root/'input/repository'
    copied=all((work/p.relative_to(baseline)).is_file() and (work/p.relative_to(baseline)).read_bytes()==p.read_bytes() for p in baseline.rglob('*') if p.is_file())
    try:(root/'SHOULD_NOT_WRITE').write_text('bad');readonly=False
    except OSError:readonly=True
    hidden_absent=all(not Path(p).exists() for p in PRIVATE_PATHS)
    proof={'all_public_files_visible':visible,'all_baseline_files_in_worktree':copied,'public_read_only':readonly,'private_paths_absent':hidden_absent,'public_file_count':len(files),'dependency_typescript_visible':Path('/builder-dependencies/node_modules/typescript/bin/tsc').is_file()}
    Path('/probe/isolation.json').write_text(json.dumps(proof));assert all(v for k,v in proof.items() if k!='public_file_count'),proof
previous=status['records'][-1] if status['records'] else None
if n==2:assert previous['feedback_digest']==sys.argv[2]
relative='src/_agentswe_native_builder_probe.ts';work=Path('/workspace/worktree')/relative
work.write_text('export const nativeBuilderProbeRevision = '+str(n)+';\n')
p=Path('/workspace/submission')
(p/'solution.patch').write_text('diff --git a/'+relative+' b/'+relative+'\nnew file mode 100644\n--- /dev/null\n+++ b/'+relative+'\n@@ -0,0 +1 @@\n+export const nativeBuilderProbeRevision = '+str(n)+';\n')
(p/'edit_report.json').write_text(json.dumps({'summary':'Scripted interface probe only','changed_paths':[relative]}))
(p/'run_report.json').write_text(json.dumps({'builder_session_id':status['builder_session_id'],'submission_number':n,'revision_of_candidate_digest':previous['candidate_digest'] if previous else None,'feedback_digest':previous['feedback_digest'] if previous else None,'api_calls':{'gateway':0},'synthetic_provider':True}))
'''
    if a.no_replay:
        delivery_script=delivery_script.replace("n=int(sys.argv[1]);status=", "n=int(sys.argv[1])\nif n>1:\n    if n==2:Path('/workspace/submission/edit_report.json').write_text(json.dumps({'summary':'reports-only change after unknown', 'changed_paths':['src/_agentswe_native_builder_probe.ts']}))\n    raise SystemExit(0)\nstatus=")
    private_paths=[str(private),str(credential),str(a.source/'agentloop/evaluator/controller.py'),str(a.source/'test_cases'),' @@AGENTSWE_CREDENTIAL_FILE@@'.strip()]
    (fixture/'fixture_delivery.py').write_text('PRIVATE_PATHS='+repr(private_paths)+'\n'+delivery_script)
    compose=a.run/'builder_task/environment/docker-compose.yaml';spec=json.loads(compose.read_text());main=spec['services']['main']
    main.update(network_mode='none',command=['bash','-lc','python3 /probe/provider.py & exec sleep infinity'])
    main['volumes'].append({'type':'bind','source':str(fixture),'target':'/probe'})
    main['environment'].update(NO_PROXY='localhost,127.0.0.1',no_proxy='localhost,127.0.0.1')
    if a.negative_runtime:
        main['environment']['PATH']='/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin'
    if a.negative_healthcheck:
        main['cpu_quota']=0;main['cpu_period']=0
        import harbor.builder_resources as br
        original=br.BuilderResourceObserver._observe
        def record_only(self):
            records=[]
            class Recording(list):
                def append(self,item):records.append(item)
            self.proof['errors']=Recording();original(self);self.proof['polling_errors_recorded']=records
        br.BuilderResourceObserver._observe=record_only
    write_json(compose,spec)
    task=a.run/'builder_task/task.toml';task.write_text(task.read_text().replace('network_mode = "public"','network_mode = "no-network"'))
    synthetic=[]
    def dev(repository,number):
        synthetic.extend({'round':number,'case_id':case} for case in life.controller.dev_cases)
        values={case:{'classification':'valid_behavior','infrastructure_invalid':False,
            'run':{'valid':True,'broker_delta':{'calls':0,'successful_calls':0}},
            'judgement':{'contract_valid':True,'score':30,'assessment':'Synthetic feedback requests a second distinct source revision. No semantic capability result.',
            'major_errors':['Synthetic revision control.'],'dimensions':{'documentation_correctness':{'score':10,'max':30,'rationale':'Scripted interface fixture','oracle_summary':'PRIVATE_ORACLE_SENTINEL'}},
            'oracle_summary':'PRIVATE_ORACLE_SENTINEL','judge_prompt':'PRIVATE_JUDGE_SENTINEL','raw_response_path':'/data/private-synthetic'}} for case in life.controller.dev_cases}
        if a.no_replay:
            for case,value in values.items():
                unknown=case=='dev_001'
                value.update(classification='provider_failure' if unknown else 'candidate_partial',infrastructure_invalid=unknown)
                value['judgement'].update(contract_valid=not unknown,score=None if unknown else 99)
                output=life.controller.active_attempt/'dev'/case;output.mkdir(parents=True,exist_ok=True)
                write_json(output/'synthetic-private-result.json',value)
        return values
    proof={'valid':False,'external_provider_calls':0,'real_lower_calls':0,'real_Result_calls':0,'synthetic_feedback_not_acceptance':True,'source_before':before}
    try:
        with patch.object(life.controller,'_evaluate_dev',side_effect=dev):
            result=f.run_native_builder(lifecycle=life,config=config,credential=credential,harbor=Path('@@AGENTSWE_HARBOR_BIN@@'),timeout=900)
        stats=f.direct_builder_runtime().native_stats(a.run);resources=json.loads((a.run/'builder_resource_attestation.json').read_text());cleanup=json.loads((a.run/'builder_container_cleanup.json').read_text())
        requests=(fixture/'provider_requests.jsonl').read_text().splitlines() if (fixture/'provider_requests.jsonl').exists() else []
        proof.update(exit_code=result.returncode,native_stats=stats,resources=resources,cleanup=cleanup,scripted_local_POSTs=len(requests),source_after=tree_digest(a.source),shared_runtime_sha256=hashlib.sha256(f.DIRECT_BUILDER_SCRIPT.read_bytes()).hexdigest())
        gates=[json.loads(p.read_text()) for p in (a.run/'jobs').rglob('builder_resource_gate.json')]
        proof['preagent_gates']=gates
        gate_attestation=a.run/'builder_preagent_gate_attestation.json'
        proof['preagent_gate_attestation']={'path':str(gate_attestation),'sha256':hashlib.sha256(gate_attestation.read_bytes()).hexdigest(),'proof':json.loads(gate_attestation.read_text())}
        if a.negative_healthcheck or a.negative_runtime:
            trials=[json.loads(p.read_text()) for p in (a.run/'jobs').rglob('result.json') if p.parent.name.startswith('builder_task__')]
            proof['negative_trials']=trials
            gate_rejected=len(gates)==1 and not gates[0]['valid'] and (not a.negative_runtime or gates[0].get('runtime',{}).get('valid') is False)
            proof['valid']=result.returncode!=0 and gate_rejected and not proof['preagent_gate_attestation']['proof']['valid'] and (a.negative_runtime or not resources['valid']) and cleanup['complete'] and len(requests)==0 and stats['native_completed_turns']==0 and len(trials)==1 and 'Healthcheck' in str(trials[0].get('exception_info')) and trials[0].get('agent_setup') is None and trials[0].get('agent_execution') is None and before==tree_digest(a.source)
        elif a.no_replay:
            outputs=[json.loads((fixture/('submission-'+str(n)+'.json')).read_text()) for n in range(1,4)]
            ledger=life.controller.product_attempts
            roots=list(ledger.root.iterdir());retained=ledger.lookup(roots[0].name) if len(roots)==1 else {}
            before_restart={str(p):hashlib.sha256(p.read_bytes()).hexdigest() for p in ledger.root.rglob('*') if p.is_file()}
            from agentloop.evaluator.controller import Controller
            restarted=Controller(life.controller.source,life.controller.cases,life.controller.run_dir,life.controller.broker_endpoint)
            with patch.object(restarted,'_evaluate_dev',side_effect=AssertionError('restart must not execute dev')):
                # Use the actual owned materializer with no repeated product build.
                from agentloop.candidate_adapter import build_candidate
                with patch('agentloop.evaluator.controller.build_candidate',side_effect=lambda source,candidate,output,run_build:build_candidate(source,candidate,output,run_build=False)):
                    replay=restarted.submit(workspace,1)
            from harbor.native_builder_evidence import classify_native_termination
            termination={'native_completed_turns':stats['native_completed_turns']}
            feedback=[json.dumps(v['payload'],ensure_ascii=False) for v in outputs]
            cases=outputs[0]['payload']['feedback']['cases']
            proof.update(native_termination=termination,synthetic_dev_calls=synthetic,submission_statuses=[v['status'] for v in outputs],
                product_attempt_count=len(roots),retained_private_state=retained['state'],restart_replay_blocked=replay.get('replay_blocked'),
                original_ledger_unchanged=before_restart=={str(p):hashlib.sha256(p.read_bytes()).hexdigest() for p in ledger.root.rglob('*') if p.is_file()},
                retained_public_scores={k:v['result_score'] for k,v in cases.items()},
                public_feedback_private_fields_absent=all('PRIVATE_' not in v and '/data/private' not in v for v in feedback))
            proof['valid']=result.returncode==0 and resources['valid'] and cleanup['complete'] and stats['native_completed_turns']==1 and len(synthetic)==2 and not life.controller.records and proof['submission_statuses']==[503,409,409] and len(roots)==1 and replay.get('replay_blocked') and proof['original_ledger_unchanged'] and proof['retained_public_scores']=={'dev_001':None,'dev_002':99} and all(v['payload']['retry_allowed'] is False for v in outputs) and proof['public_feedback_private_fields_absent'] and all(v['payload']['feedback']==outputs[0]['payload']['feedback'] for v in outputs) and before==tree_digest(a.source)
        else:
            native=f.verify_native(a.run,life.controller.records,[e for e in life.events if e['event']=='feedback_delivered'],life.native_observations)
            proof.update(native_evidence=native,synthetic_dev_calls=synthetic,accepted_submissions=len(life.controller.records))
            if result.returncode==0 and native['valid'] and life.controller.records:proof['freeze']=life.controller.freeze()
            revisions=[(Path(r['build']['product_entry']).parent.parent/'src/_agentswe_native_builder_probe.ts').read_text() for r in life.controller.records]
            feedback=[Path(r['feedback']['json']).read_text() for r in life.controller.records]
            isolation=json.loads((fixture/'isolation.json').read_text()) if (fixture/'isolation.json').exists() else {}
            proof.update(actual_source_revisions=revisions,public_feedback_private_fields_absent=all('PRIVATE_' not in v and '/data/private' not in v for v in feedback),isolation=isolation)
            proof['runtime_gate_before_first_model_POST']=proof['preagent_gate_attestation']['proof']['valid'] and len(gates)==1 and gates[0]['valid'] and gates[0].get('runtime',{}).get('valid') and gates[0]['epoch']<min(json.loads(row)['observed_epoch'] for row in requests)
            expected_rounds=1 if a.single_submission else 2
            proof['expected_scripted_submissions']=expected_rounds
            proof['valid']=result.returncode==0 and native['valid'] and native['revision_observed']==(expected_rounds>1) and resources['valid'] and cleanup['complete'] and len(synthetic)==2*expected_rounds and len(life.controller.records)==expected_rounds and revisions==['export const nativeBuilderProbeRevision = '+str(n)+';\n' for n in range(1,expected_rounds+1)] and proof['public_feedback_private_fields_absent'] and bool(isolation) and all(v for k,v in isolation.items() if k!='public_file_count') and before==tree_digest(a.source) and proof['runtime_gate_before_first_model_POST']
            if a.reconnect_once:
                proof['valid']=proof['valid'] and native['native_termination']['recovered_in_same_turn'] and native['native_termination']['native_retry_announcements']==1
    finally:
        life.close();proof['controller_socket_absent']=not life.socket_path.exists()
        write_json(a.run/'verification.json',proof)
    print(json.dumps({'valid':proof['valid'],'exit_code':proof.get('exit_code'),'external_provider_calls':0,'scripted_POSTs':proof.get('scripted_local_POSTs'),'accepted':proof.get('accepted_submissions'),'native_errors':proof.get('native_evidence',{}).get('errors')}))
    return 0 if proof['valid'] else 2

if __name__=='__main__':raise SystemExit(main())
