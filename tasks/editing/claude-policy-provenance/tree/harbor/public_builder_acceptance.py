"""Complete public Claude Builder acceptance; never dispatch hidden or Code."""
from __future__ import annotations
import argparse,hashlib,json,sys,time
from pathlib import Path

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT))
from harbor import formal_one_stop as formal
from harbor.direct_harbor_builder import native_stats
from agentloop.protocol import write_json
sys.path.insert(0,'@@AGENTSWE_EDITING_CONTROL@@')
from validate_formal_config import tree_digest


def main(argv=None):
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('--run',type=Path,required=True)
    p.add_argument('--credential-file',type=Path,default=Path('@@AGENTSWE_CREDENTIAL_FILE@@'))
    p.add_argument('--harbor',type=Path,default=Path('@@AGENTSWE_HARBOR_BIN@@'))
    p.add_argument('--builder-base-url',default='https://api.deepseek.com/v1')
    p.add_argument('--builder-proxy',default='http://127.0.0.1:7890')
    p.add_argument('--lower-provider-url',default='https://api.deepseek.com/v1/responses')
    p.add_argument('--builder-timeout',type=int,default=28800)
    p.add_argument('--max-dev-rounds',type=int,default=10)
    mode=p.add_mutually_exclusive_group();mode.add_argument('--prepare-only',action='store_true');mode.add_argument('--startup-only',action='store_true')
    a=p.parse_args(argv)
    if not 1<=a.max_dev_rounds<=10 or a.builder_timeout<=0:p.error('invalid Builder round/time budget')
    run=a.run.resolve();run.mkdir(parents=True,exist_ok=False)
    source_digest=tree_digest(ROOT)
    shared=Path('@@AGENTSWE_EDITING_CONTROL@@')
    paths=[ROOT/'harbor/public_builder_acceptance.py',ROOT/'harbor/builder_direct.py',ROOT/'harbor/direct_harbor_builder.py',
        *(shared/name for name in ['judge_broker_xhigh.py','judge_broker_runtime.py','responses_stream.py','execution_contract.py','execution_scoring.py','result_judge.py'])]
    sources={str(f):hashlib.sha256(f.read_bytes()).hexdigest() for f in paths}
    identity={'schema_version':'agentswe-claude-native-direct-public-acceptance/v1','task_root':str(ROOT),'task_source':source_digest,'sources':sources,
        'started_at':formal.now(),'kind':'smoke','formal_branch':False,'builder_transport':'native_codex_direct','builder_broker_started':False,
        'public_cases':list(formal.DEV_CASES),'hidden_dispatched':False,'code_judge_dispatched':False,
        'max_dev_rounds':a.max_dev_rounds,'formal_minimum_submissions':1,'acceptance_revision_requested':True,
        'historical_lower_or_judgment_replayed':False,
        'necessity':'Validate a new native Builder workspace under the complete public requirements, both public dev cases, full authoritative feedback delivery and a substantive same-session revision. Original Candidates and historical scoring requests are not resumed.'}
    write_json(run/'experiment_identity.json',identity)
    public,manifest=formal.stage_public_package(run)
    write_json(run/'public_package_manifest.json',manifest)
    if a.prepare_only:return 0
    ports=set()
    while len(ports)<2:ports.add(formal.free_port())
    lower_port,judge_port=sorted(ports)
    lower_endpoint=f'http://127.0.0.1:{lower_port}/v1/responses';judge_endpoint=f'http://127.0.0.1:{judge_port}/v1/responses'
    suffix=hashlib.sha256(str(run).encode()).hexdigest()[:10]
    names={role:'claude-public-acceptance-'+role+'-'+suffix for role in ('lower','judge')}
    cids={role:run/'brokers'/f'{role}.cid' for role in names};attempted={role:False for role in names};started={role:False for role in names}
    lifecycle=None;code=-1
    try:
        attempted['lower']=True
        formal.start_broker(name=names['lower'],script=ROOT/'agentloop/evaluator/broker.py',credential=a.credential_file,
            port=lower_port,image=formal.LOWER_IMAGE,cidfile=cids['lower'],provider_url=a.lower_provider_url)
        started['lower']=True
        attempted['judge']=True
        formal.start_broker(name=names['judge'],script=formal.JUDGE_BROKER_SCRIPT,credential=a.credential_file,
            port=judge_port,image=formal.BUILDER_IMAGE,cidfile=cids['judge'])
        started['judge']=True
        import subprocess
        for role,cid in cids.items():
            info=json.loads(subprocess.check_output(['docker','inspect',cid.read_text().strip()]))[0]
            command=info['Config']['Cmd']
            if command[command.index('--bind')+1]!='127.0.0.1':raise RuntimeError(role+' role listener is not private')
        if a.startup_only:
            stats={role:formal.broker_stats(endpoint) for role,endpoint in [('lower',lower_endpoint),('judge',judge_endpoint)]}
            if any(v.get('runtime',{}).get('calls',0) for v in stats.values()):raise RuntimeError('startup unexpectedly dispatched a model request')
            write_json(run/'startup_verification.json',{'valid':True,'sources':sources,'task_source':source_digest,'external_model_calls':0,'roles_started':list(names),'builder_broker_started':False,'stats':stats})
            return 0
        workspace=run/'builder_workspace/submission';workspace.mkdir(parents=True)
        lifecycle=formal.BuilderLifecycle(run_dir=run,workspace=workspace,public_endpoint=lower_endpoint,max_dev_rounds=a.max_dev_rounds)
        lifecycle.controller.judge_endpoint=judge_endpoint
        formal.start_submission_server(lifecycle)
        provider=run/'builder_provider.toml'
        # The execution context writes the actual native provider before launch.
        provider.write_text('model_provider="gateway_direct"\n')
        config=formal.builder_config(run_dir=run,public=public,workspace=workspace,lifecycle=lifecycle,provider_config=provider,image=formal.BUILDER_IMAGE)
        instruction=run/'builder_task/instruction.md'
        instruction.write_text(instruction.read_text()+'''\nThis invocation is non-formal public repair acceptance. Follow the complete four public input documents and both dev descriptions. After the first authoritative two-dev feedback, make a substantive product revision and submit it with the exact preceding feedback digest, then stop when the acceptance exercise is complete. Changes only to reports, formatting, or a nonce do not establish a product revision. This diagnostic revision requirement does not impose a two-submission minimum on formal runs. This entry never dispatches hidden or Code scoring.\n''')
        lifecycle.event('builder_invocation_started',acceptance=True);began=time.time_ns()
        code=formal.run_configured_builder(a.harbor,config,run,a.builder_timeout,credential=a.credential_file,
            transport='direct',base_url=a.builder_base_url,proxy=a.builder_proxy)
        ended=time.time_ns();lifecycle.event('builder_invocation_finished',exit_code=code,acceptance=True)
        lifecycle.finalize_witness(code)
        if code==0 and lifecycle.controller.rounds and not lifecycle.controller.frozen:lifecycle.controller.freeze_latest('builder_exit')
        proof=formal.builder_attestation(lifecycle,code,began,ended)
        proof.update(acceptance_complete=bool(proof['complete'] and proof['native_evidence']['revision_observed']),
            task_source_unchanged=tree_digest(ROOT)==source_digest,
            shared_source_unchanged=all(hashlib.sha256(Path(f).read_bytes()).hexdigest()==h for f,h in sources.items()),
            formal_branch=False,hidden_dispatched=False,code_judge_dispatched=False)
        write_json(run/'builder_session_attestation.json',proof)
        write_json(run/'summary.json',{'status':'builder_acceptance_complete' if proof['acceptance_complete'] else 'builder_acceptance_incomplete',
            'formal_branch':False,'formal_ready':False,'accepted_rounds':len(lifecycle.controller.rounds),
            'native_session':lifecycle.session_id,'builder':native_stats(run),
            'lower':formal.broker_stats(lower_endpoint),'result_judge':formal.broker_stats(judge_endpoint)})
        return 0 if proof['acceptance_complete'] and proof['task_source_unchanged'] and proof['shared_source_unchanged'] else 2
    finally:
        if lifecycle is not None:lifecycle.close()
        for role,endpoint in [('lower',lower_endpoint),('judge',judge_endpoint)]:formal.save_stats(run,role,endpoint if started[role] else None)
        harbor_cleanup=formal.cleanup_run_mounted_containers(run,set(names.values()))
        receipts=[formal.cleanup_owned_container(role,cids[role],attempted=attempted[role]) for role in names]
        write_json(run/'cleanup_attestation.json',{'complete':all(r['absent_after_cleanup'] for r in receipts) and all(r.get('absent_after_cleanup',True) for r in harbor_cleanup),
            'results':receipts,'harbor_run_resources':harbor_cleanup,'builder_broker_started':False,'unrelated_resources_touched':False})


if __name__=='__main__':raise SystemExit(main())
