"""Fresh full-public Aider native Builder acceptance; hidden and Code are absent."""
import argparse,hashlib,json,sys
from pathlib import Path
ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT))
from harbor import formal_one_stop as f
from harbor.direct_harbor_builder import native_stats
sys.path.insert(0,'@@AGENTSWE_EDITING_CONTROL@@')
from validate_formal_config import tree_digest


def main(argv=None):
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('--run',type=Path,required=True)
    p.add_argument('--credential-file',type=Path,default=Path('@@AGENTSWE_CREDENTIAL_FILE@@'))
    p.add_argument('--harbor',type=Path,default=Path('@@AGENTSWE_HARBOR_BIN@@'))
    p.add_argument('--builder-base-url',default='https://api.deepseek.com/v1')
    p.add_argument('--builder-proxy',default='http://127.0.0.1:7890')
    p.add_argument('--builder-timeout',type=int,default=28800)
    p.add_argument('--max-dev-rounds',type=int,default=10)
    p.add_argument('--lower-image',default='agentswe/edit-candidate-python311:0826')
    p.add_argument('--dependency-overlay',type=Path,default=Path('@@AGENTSWE_ENVS@@/aider-worktree-transaction-ledger-edit-v1/lib/python3.11/site-packages'))
    m=p.add_mutually_exclusive_group();m.add_argument('--prepare-only',action='store_true');m.add_argument('--startup-only',action='store_true')
    a=p.parse_args(argv)
    if not 1<=a.max_dev_rounds<=10 or a.builder_timeout<=0:p.error('invalid Builder budget')
    run=a.run.resolve();run.mkdir(parents=True,exist_ok=False)
    source=tree_digest(ROOT);shared=Path('@@AGENTSWE_EDITING_CONTROL@@')
    bindings={str(shared/n):hashlib.sha256((shared/n).read_bytes()).hexdigest() for n in ['judge_broker_runtime.py','judge_broker_xhigh.py','responses_stream.py','execution_contract.py','execution_scoring.py','result_judge.py']}
    f.write_json(run/'experiment_identity.json',{'schema_version':'agentswe-aider-native-direct-public-acceptance/v1',
        'source':source,'task_root':str(ROOT),'shared_sources':bindings,'started_at':f.now(),'kind':'smoke','formal_branch':False,
        'public_cases':['dev_001','dev_002'],'hidden_dispatched':False,'code_judge_dispatched':False,
        'historical_requests_replayed':False,'builder_transport':'native_codex_direct','max_dev_rounds':a.max_dev_rounds,
        'necessity':'Fresh native Builder using all four public input documents and both public dev cases, then a substantive same-session product revision based on complete authoritative feedback.'})
    public=f.public_package(run)
    if a.prepare_only:return 0
    lower_port=f.port();judge_port=f.port()
    while judge_port==lower_port:judge_port=f.port()
    endpoints={'lower':f'http://127.0.0.1:{lower_port}/v1/responses','result_judge':f'http://127.0.0.1:{judge_port}/v1/responses'}
    suffix=hashlib.sha256(str(run).encode()).hexdigest()[:10]
    names={r:'aider-public-acceptance-'+r+'-'+suffix for r in endpoints}
    cids={r:run/'brokers'/f'{r}.cid' for r in names};attempted={r:False for r in names}
    controller=None
    try:
        for role,port,script,effort in [('lower',lower_port,ROOT/'evaluator/broker/lower_responses_broker.py',f.LOWER_EFFORT),('result_judge',judge_port,f.JUDGE_BROKER_SCRIPT,f.BUILDER_EFFORT)]:
            attempted[role]=True
            f.start_broker(name=names[role],script=script,credential=a.credential_file,value_port=port,effort=effort,cidfile=cids[role])
        if a.startup_only:
            stats={role:f.stats(endpoint) for role,endpoint in endpoints.items()}
            assert all(v.get('runtime',{}).get('calls',0)==0 for v in stats.values())
            f.write_json(run/'startup_verification.json',{'valid':True,'source':source,'external_model_calls':0,'stats':stats});return 0
        workspace=run/'builder_workspace/submission';workspace.mkdir(parents=True)
        controller=f.BuilderLifecycle(run_dir=run,workspace=workspace,lower_broker=endpoints['lower'],source=ROOT/'input/repository',image=a.lower_image,max_dev_rounds=a.max_dev_rounds)
        controller.controller.dependency_overlay=a.dependency_overlay
        controller.controller.judge_endpoint=endpoints['result_judge']
        f.start_builder_server(controller)
        provider=run/'builder_provider.toml';provider.write_text('model_provider="gateway_direct"\n')
        config=f.builder_config(run,public,workspace,controller,provider)
        instruction=run/'builder_task/instruction.md'
        instruction.write_text(instruction.read_text()+'''\nThis is non-formal public repair acceptance. Follow all four full public input documents and both dev descriptions. After the first authoritative feedback, make a substantive product revision and submit again with the exact preceding feedback digest. Report-only or formatting-only changes do not establish a product revision. This diagnostic does not change the formal one-submission minimum and never dispatches hidden or Code scoring.\n''')
        code=f.run_configured_builder(a.harbor,config,run,a.builder_timeout,credential=a.credential_file,transport='direct',base_url=a.builder_base_url,proxy=a.builder_proxy)
        if code==0 and controller.controller.records and not controller.controller.frozen:controller.controller.freeze_latest('builder_exit')
        native=controller.native_attestation()
        proof={'native_evidence':native,'builder_exit_code':code,'candidate_records':controller.controller.records,'events':controller.events,
            'accepted_rounds':len(controller.controller.records),'freeze':controller.controller.frozen,
            'source_unchanged':source==tree_digest(ROOT),'shared_sources_unchanged':all(hashlib.sha256(Path(path).read_bytes()).hexdigest()==h for path,h in bindings.items()),
            'formal_branch':False,'hidden_dispatched':False,'code_judge_dispatched':False}
        proof['acceptance_complete']=bool(code==0 and native['valid'] and native['revision_observed'] and proof['freeze'] and all(f.public_round_complete(r) for r in controller.controller.records) and proof['source_unchanged'] and proof['shared_sources_unchanged'])
        f.write_json(run/'builder_session_attestation.json',proof)
        f.write_json(run/'summary.json',{'status':'builder_acceptance_complete' if proof['acceptance_complete'] else 'builder_acceptance_incomplete',
            'formal_ready':False,'native_session':controller.session_id,'builder':native_stats(run),**{r:f.stats(e) for r,e in endpoints.items()}})
        return 0 if proof['acceptance_complete'] else 2
    finally:
        if controller:controller.close()
        for role,endpoint in endpoints.items():
            if attempted[role]:
                try:f.write_json(run/f'{role}_broker_stats.json',f.stats(endpoint))
                except Exception as exc:f.write_json(run/f'{role}_broker_stats_error.json',{'error_type':type(exc).__name__})
        clean=f.cleanup_owned_containers(names,attempted,cids)
        clean['removed_compose_projects']=f.cleanup_builder_containers(run)
        clean['completed']=clean['all_attempted_absent']
        clean['builder_broker_started']=False
        f.write_json(run/'builder_container_cleanup.json',clean)


if __name__=='__main__':raise SystemExit(main())
