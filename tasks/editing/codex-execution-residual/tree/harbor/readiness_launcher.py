"""Fresh Codex readiness: one native turn, two dev rounds, one hidden, two judges."""
from pathlib import Path
import hashlib
import json
import shutil
import sys
import types


def run(args):
    from harbor import formal_one_stop as task
    from harbor.readiness_resources import retain_container, retained_manifest
    from evaluator.broker.lower_broker_runtime import start_lower_broker
    from evaluator.readiness_smoke import run as smoke
    sys.path.insert(0, '@@AGENTSWE_EDITING_CONTROL@@')
    from readiness_binding import verify_binding
    from judge_broker_runtime import start_judge_broker
    root, run_dir = args.benchmark.resolve(), args.run_dir.resolve()
    binding_file = args.readiness_binding_file
    if binding_file is None or binding_file.is_symlink() or hashlib.sha256(binding_file.read_bytes()).hexdigest() != args.readiness_binding_sha256:
        raise ValueError('readiness requires current evaluator binding bytes')
    binding = verify_binding(root, json.loads(binding_file.read_bytes()))
    if args.readiness_profile != task.READINESS_PROFILE or args.builder_transport != 'direct':
        raise ValueError('readiness requires its explicit profile and direct native Builder')
    if run_dir.exists():
        raise ValueError('fresh readiness run must not exist')
    run_dir.mkdir(parents=True)
    task.write_json(run_dir/'readiness_current_binding.json', binding)
    public = run_dir/'builder_public_package'
    shutil.copytree(root/'input', public/'input', symlinks=True)
    shutil.copytree(root/'dev_cases/dev_001', public/'dev_cases/dev_001', symlinks=True)
    workspace = run_dir/'builder_workspace/submission'
    workspace.mkdir(parents=True)
    provider = run_dir/'builder_broker_provider.toml'
    provider.write_text('')  # native execution writes the bound provider config before starting Harbor.
    target = run_dir/'build_cache/target'
    task.write_json(run_dir/'build_preflight.json', task.baseline_build(root, args.runtime.resolve(), target, run_dir/'build_preflight'))
    ports = {}
    while len(set(ports.values())) != 3:
        ports = {role: task.allocate_port() for role in ('public', 'hidden', 'judge')}
    endpoints = {role: f'http://127.0.0.1:{port}/v1/responses' for role, port in ports.items()}
    cids = {role: run_dir/'brokers'/(role+'.cid') for role in ports}
    names = {role: 'codex-readiness-'+role+'-'+hashlib.sha256(str(run_dir).encode()).hexdigest()[:12] for role in ports}
    started, judge, controller = [], None, None
    try:
        def evaluate(number, snapshot):
            request_root = cids['public'].parent/'public-lower-transport/lower_requests'
            before = {p.name for p in request_root.iterdir() if p.is_dir()}
            result = task.evaluate_public_candidate(benchmark=root, run_dir=run_dir, number=number,
                snapshot=snapshot, runtime=args.runtime.resolve(), shared_target=target,
                cases=('dev_001',), lower_endpoint=endpoints['public'], judge_endpoint=None,
                readiness_profile=task.READINESS_PROFILE)
            after = {p.name for p in request_root.iterdir() if p.is_dir()}
            task.write_json(run_dir/f'evaluations/submission_{number:03d}/readiness_request_binding.json',
                {'case': 'dev_001', 'before': sorted(before), 'after': sorted(after),
                 'request_ids': sorted(after-before), 'candidate_digest': task.tree_digest(snapshot)})
            return result
        controller = task.DevController(run_dir=run_dir, workspace=workspace, evaluate=evaluate,
            max_dev_rounds=2, public_cases=('dev_001',), readiness_profile=task.READINESS_PROFILE,
            current_binding=binding)
        controller.start()
        started.append('public')
        start_lower_broker(name=names['public'], credential=args.credential_file, image=args.broker_image,
            port=ports['public'], cidfile=cids['public'], defer_removal=True)
        config = task.stage_builder(run_dir=run_dir, public=public, workspace=workspace, controller=controller,
            provider_config=provider, public_cases=('dev_001',), pilot_not_formal=True)
        task.write_json(run_dir/'protocol_lock.json', {'profile': task.READINESS_PROFILE, 'public_cases': ['dev_001'],
            'hidden_cases': ['test_001'], 'required_valid_rounds': 2, 'score_threshold': None,
            'builder': {'model': 'deepseek-flash', 'effort': 'max', 'single_uninterrupted_turn': True}})
        builder = task.run_configured_builder(args.harbor.resolve(), config, run_dir, args.builder_timeout,
            credential=args.credential_file.resolve(), transport='direct', base_url=args.builder_base_url, proxy=args.builder_proxy)
        controller.wait_idle()
        native = controller.native_attestation()
        task.write_json(run_dir/'builder_session_attestation.json', {'builder': builder, 'native': native})
        frozen = controller.freeze_latest('builder_exit', builder_exit_evidence={
            'exit_code': builder['exit_code'], 'native_valid': native['valid'],
            'builder_session_id': controller.builder_session_id})
        controller.stop()
        # Public lower has no more requests; retain its stopped Docker identity for coordinator cleanup.
        retain_container(task.read_container_id(cids['public']), run_dir, expected_name=names['public'],
            expected_mount=(str(cids['public'].parent/'public-lower-transport'), '/evidence'))
        started.append('hidden')
        start_lower_broker(name=names['hidden'], credential=args.credential_file, image=args.broker_image,
            port=ports['hidden'], cidfile=cids['hidden'], defer_removal=True)
        initial = task.broker_stats(ports['hidden'])
        if any(initial.get('runtime', {}).get(key) != 0 for key in ('calls', 'failures')):
            raise ValueError('hidden broker is not fresh')
        task.write_json(run_dir/'readiness_hidden_initial.json', initial)
        intent = run_dir/'readiness_hidden_intent.json'
        with intent.open('x') as stream:
            json.dump({'case': 'test_001', 'freeze_sha256': hashlib.sha256((run_dir/'freeze_manifest.json').read_bytes()).hexdigest(),
                'started_at': task.now(), 'candidate_digest': frozen['candidate_digest']}, stream)
        before = task.tree_digest(Path(frozen['path']))
        binary = Path(frozen['binary'])
        if hashlib.sha256(binary.read_bytes()).hexdigest() != frozen['binary_sha256']:
            raise ValueError('frozen binary changed before hidden')
        hidden = task.run_agent_case(harness=root/'evaluator/harness/run_lower_agent_case.py',
            case=root/'test_cases/test_001', binary=binary, output=run_dir/'evaluations/hidden/test_001',
            endpoint=endpoints['hidden'])
        verdict, _ = task.load_formal_finalizer(root).execution_verdict(hidden, 'test_001', frozen['candidate_digest'])
        if verdict.get('classification') not in {'scoreable', 'candidate_zero'}:
            raise ValueError('hidden infrastructure-invalid: '+str(verdict.get('reason')))
        if before != task.tree_digest(Path(frozen['path'])) or hashlib.sha256(binary.read_bytes()).hexdigest() != frozen['binary_sha256']:
            raise ValueError('frozen delivery or binary changed during hidden')
        task.write_json(run_dir/'evaluations/hidden/test_001/result.json', hidden)
        task.write_json(run_dir/'readiness_hidden_attestation.json', {'case': 'test_001', 'result': hidden,
            'freeze_digest_stable': True, 'execution_valid': True, 'intent': str(intent)})
        started.append('judge')
        judge = start_judge_broker(name=names['judge'], credential=args.credential_file, image=args.broker_image,
            port=ports['judge'], cidfile=cids['judge'], defer_removal=True)
        summary = smoke(root, run_dir, hidden, args.credential_file.resolve(), endpoints['judge'])
        task.write_json(run_dir/'readiness_summary.json', {**summary, 'pipeline_ready': False, 'admission_required': True})
        return 0 if summary['complete'] else 2
    finally:
        errors = []
        if controller:
            controller.stop()
        for role in started:
            try:
                if role == 'judge' and judge is not None:
                    judge.close()
                elif role != 'public' or not (run_dir/'readiness_resource_retention'/(str(task.read_container_id(cids[role]))+'.json')).exists():
                    retain_container(task.read_container_id(cids[role]), run_dir, expected_name=names[role],
                        expected_mount=(str(cids[role].parent/(role+'-lower-transport')), '/evidence'))
            except Exception as exc:
                errors.append(role+': '+type(exc).__name__+': '+str(exc))
        try:
            retained_manifest(run_dir, [types.SimpleNamespace(container_id=task.read_container_id(cids[role])) for role in started])
        except Exception as exc:
            errors.append('manifest: '+str(exc))
        task.write_json(run_dir/'cleanup_attestation.json', {'complete': False, 'removal_deferred': True,
            'coordinator_cleanup_required': True, 'errors': errors})
