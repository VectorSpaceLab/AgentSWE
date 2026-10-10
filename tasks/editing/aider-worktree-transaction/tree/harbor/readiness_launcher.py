"""Aider's real one-turn/two-round readiness launcher; no formal publication."""
import hashlib
import json
from pathlib import Path
import shutil
import sys
import types
from harbor.readiness_contract import PROFILE

def run(args, directory):
    from harbor import formal_one_stop as task
    from harbor.readiness_resources import retain_container,retained_manifest,inspect_container
    from evaluator.broker.lower_broker_runtime import start_lower_broker
    from evaluator.readiness_smoke import run as smoke
    from evaluator.formal_finalize import execution_verdict
    sys.path.insert(0,'@@AGENTSWE_EDITING_CONTROL@@')
    from readiness_binding import verify_binding
    from judge_broker_runtime import start_judge_broker
    root,run=task.ROOT,Path(directory).resolve()
    path=args.readiness_binding_file
    if path is None or path.is_symlink() or hashlib.sha256(path.read_bytes()).hexdigest()!=args.readiness_binding_sha256:
        raise ValueError('current evaluator binding bytes required')
    binding=verify_binding(root,json.loads(path.read_bytes()))
    if args.readiness_profile!=PROFILE or args.builder_transport!='direct':
        raise ValueError('readiness requires explicit profile and direct native Builder')
    if run.exists():raise ValueError('fresh readiness run required')
    run.mkdir(parents=True)
    task.write_json(run/'readiness_current_binding.json',binding)
    public=task.public_package(run)
    for case_root in (public/'dev_cases',public/'cases'):
        if case_root.exists():
            for child in case_root.iterdir():
                if child.name not in {'dev_001','dev_001.md'}:
                    shutil.rmtree(child) if child.is_dir() else child.unlink()
    ports={}
    while len(set(ports.values()))!=3:ports={r:task.port() for r in ('public','hidden','judge')}
    endpoints={r:f'http://127.0.0.1:{p}/v1/responses' for r,p in ports.items()}
    names={r:'aider-readiness-'+r+'-'+hashlib.sha256(str(run).encode()).hexdigest()[:12] for r in ports}
    cids={r:run/'brokers'/(r+'.cid') for r in ports}
    started=[];controller=None;judge=None
    def retain(role):
        cid=task.read_container_id(cids[role])
        prior=run/'readiness_resource_retention'/(str(cid)+'.json')
        if prior.is_file() and task.read_json(prior).get('retained_terminal') is True:
            current=inspect_container(cid)
            if current['Id']!=cid or current.get('State',{}).get('Status') not in {'exited','dead'}:
                raise ValueError('retained lower broker changed terminal state')
            return task.read_json(prior)
        return retain_container(task.read_container_id(cids[role]),run,expected_name=names[role],
            expected_mount=(str(cids[role].parent/(role+'-lower-transport')),'/evidence'))
    try:
        started.append('public')
        start_lower_broker(name=names['public'],credential=args.credential_file,image=args.image,
            port=ports['public'],cidfile=cids['public'],defer_removal=True)
        workspace=run/'builder_workspace/submission';workspace.mkdir(parents=True)
        controller=task.BuilderLifecycle(run_dir=run,workspace=workspace,lower_broker=endpoints['public'],
            source=root/'input/repository',image=args.lower_image,pilot_not_formal=True,max_dev_rounds=2,
            readiness_profile=PROFILE,current_binding=binding)
        controller.controller.dependency_overlay=args.dependency_overlay.resolve() if args.dependency_overlay else None
        provider=run/'builder_broker_provider.toml';provider.write_text('')
        task.start_builder_server(controller)
        config=task.builder_config(run,public,workspace,controller,provider,pilot_not_formal=True)
        task.write_json(run/'protocol_lock.json',{'profile':PROFILE,'public_cases':['dev_001'],'hidden_cases':['test_001'],
            'required_valid_rounds':2,'score_threshold':None,'single_uninterrupted_turn':True})
        code=task.run_configured_builder(args.harbor.resolve(),config,run,args.builder_timeout,
            credential=args.credential_file.resolve(),transport='direct',base_url=args.builder_base_url,proxy=args.builder_proxy)
        # close waits for the current request handler through the same submission lock.
        with controller.lock:
            native=controller.native_attestation()
            task.write_json(run/'native_builder_attestation.json',native)
            task.write_json(run/'readiness_builder_exit.json',{'exit_code':code,'native_valid':native['valid']})
            frozen=controller.controller.freeze_latest('builder_exit',builder_exit_evidence={'exit_code':code,'native_valid':native['valid']})
        controller.close()
        retain('public')
        started.append('hidden')
        start_lower_broker(name=names['hidden'],credential=args.credential_file,image=args.image,
            port=ports['hidden'],cidfile=cids['hidden'],defer_removal=True)
        initial=task.stats(endpoints['hidden'])
        if any(initial.get('runtime',{}).get(k)!=0 for k in ('calls','failures')):raise ValueError('hidden broker is not fresh')
        task.write_json(run/'readiness_hidden_initial.json',initial)
        with (run/'readiness_hidden_intent.json').open('x') as f:
            json.dump({'case':'test_001','freeze_sha256':hashlib.sha256((run/'lifecycle/freeze_manifest.json').read_bytes()).hexdigest(),
                'started_at':task.now(),'candidate_digest':frozen['candidate_digest']},f)
        controller.controller.broker=endpoints['hidden']
        results=controller.controller.run_hidden()
        if len(results)!=1:raise ValueError('exactly test_001 required')
        hidden=results[0]
        verdict,_=execution_verdict(hidden,'test_001',frozen['candidate_digest'])
        if verdict.get('classification') not in {'scoreable','candidate_zero'}:raise ValueError('hidden infrastructure invalid')
        task.write_json(run/'readiness_hidden_attestation.json',{'case':'test_001','result':hidden,'freeze_digest_stable':True})
        started.append('judge')
        judge=start_judge_broker(name=names['judge'],credential=args.credential_file,image=args.image,
            port=ports['judge'],cidfile=cids['judge'],defer_removal=True)
        summary=smoke(root,run,hidden,args.credential_file.resolve(),endpoints['judge'])
        task.write_json(run/'readiness_summary.json',{**summary,'pipeline_ready':False,'admission_required':True})
        return 0 if summary['complete'] else 2
    finally:
        if controller:controller.close()
        errors=[]
        for role in started:
            try:
                if role=='judge' and judge is not None:judge.close()
                elif role!='judge':retain(role)
            except Exception as exc:errors.append(role+': '+str(exc))
        try:retained_manifest(run,[types.SimpleNamespace(container_id=task.read_container_id(cids[r])) for r in started])
        except Exception as exc:errors.append(str(exc))
        task.write_json(run/'cleanup_attestation.json',{'complete':False,'removal_deferred':True,'coordinator_cleanup_required':True,'errors':errors})
