"""Fresh full-public Codex native Builder acceptance; no hidden or Code phase."""
import argparse
import hashlib
import shutil
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from harbor import formal_one_stop as f
from harbor.direct_harbor_builder import native_stats
sys.path.insert(0, '@@AGENTSWE_EDITING_CONTROL@@')
from validate_formal_config import tree_digest as source_digest


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--run', type=Path, required=True)
    parser.add_argument('--credential-file', type=Path, default=Path('@@AGENTSWE_CREDENTIAL_FILE@@'))
    parser.add_argument('--harbor', type=Path, default=Path('@@AGENTSWE_HARBOR_BIN@@'))
    parser.add_argument('--runtime', type=Path, default=Path('@@AGENTSWE_ENVS@@/codex-project-memory-edit-v1'))
    parser.add_argument('--builder-base-url', default='https://api.deepseek.com/v1')
    parser.add_argument('--builder-proxy', default='http://127.0.0.1:7890')
    parser.add_argument('--builder-timeout', type=int, default=28800)
    parser.add_argument('--max-dev-rounds', type=int, default=10)
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument('--prepare-only', action='store_true')
    mode.add_argument('--startup-only', action='store_true')
    args = parser.parse_args(argv)
    if not 1 <= args.max_dev_rounds <= 10 or args.builder_timeout <= 0:
        parser.error('invalid Builder budget')
    run = args.run.resolve()
    run.mkdir(parents=True, exist_ok=False)
    source = source_digest(ROOT)
    shared = Path('@@AGENTSWE_EDITING_CONTROL@@')
    bindings = {str(shared / name): hashlib.sha256((shared / name).read_bytes()).hexdigest() for name in
                ['judge_broker_runtime.py', 'judge_broker_xhigh.py', 'responses_stream.py', 'execution_contract.py', 'execution_scoring.py', 'result_judge.py']}
    f.write_json(run / 'experiment_identity.json', {
        'schema_version': 'agentswe-codex-native-direct-public-acceptance/v1',
        'source': source, 'task_root': str(ROOT), 'shared_sources': bindings,
        'started_at': f.now(), 'kind': 'smoke', 'formal_branch': False,
        'public_cases': ['dev_001', 'dev_002'], 'hidden_dispatched': False,
        'code_judge_dispatched': False, 'historical_requests_replayed': False,
        'builder_transport': 'native_codex_direct', 'max_dev_rounds': args.max_dev_rounds,
        'necessity': 'Fresh native Builder follows all four full public input documents and both public dev cases; require a substantive same-session product revision based on the full authoritative feedback.'})
    public = run / 'builder_public_package'
    shutil.copytree(ROOT / 'input', public / 'input', symlinks=True)
    shutil.copytree(ROOT / 'dev_cases', public / 'dev_cases', symlinks=True)
    f.write_json(run / 'public_package_manifest.json', {'input_documents': sorted(p.name for p in (public / 'input').iterdir() if p.is_file()),
        'source_digest': source_digest(public), 'public_cases': ['dev_001', 'dev_002'], 'hidden_included': False})
    if args.prepare_only:
        return 0
    ports = {role: f.allocate_port() for role in ['lower', 'result_judge']}
    while ports['lower'] == ports['result_judge']:
        ports['result_judge'] = f.allocate_port()
    endpoints = {role: f'http://127.0.0.1:{port}/v1/responses' for role, port in ports.items()}
    suffix = hashlib.sha256(str(run).encode()).hexdigest()[:10]
    names = {role: 'codex-public-acceptance-' + role + '-' + suffix for role in ports}
    cids = {role: run / 'brokers' / (role + '.cid') for role in ports}
    attempted = {role: False for role in ports}
    controller = None
    try:
        for role, script in [('lower', ROOT / 'evaluator/broker/lower_responses_broker.py'), ('result_judge', f.JUDGE_BROKER_SCRIPT)]:
            attempted[role] = True
            f.start_broker(port=ports[role], script=script, image=f.BUILDER_IMAGE,
                credential=args.credential_file.resolve(), name=names[role], cidfile=cids[role])
        if args.startup_only:
            stats = {role: f.broker_stats(port) for role, port in ports.items()}
            assert all(value.get('runtime', {}).get('calls', 0) == 0 for value in stats.values())
            f.write_json(run / 'startup_verification.json', {'valid': True, 'source': source, 'external_model_calls': 0, 'stats': stats})
            return 0
        workspace = run / 'builder_workspace/submission'
        workspace.mkdir(parents=True)
        target = run / 'build_cache/target'
        f.write_json(run / 'build_preflight.json', f.baseline_build(ROOT, args.runtime.resolve(), target, run / 'build_preflight'))

        def evaluate(number, snapshot):
            return f.evaluate_public_candidate(benchmark=ROOT, run_dir=run, number=number,
                snapshot=snapshot, runtime=args.runtime.resolve(), shared_target=target,
                cases=('dev_001', 'dev_002'), lower_endpoint=endpoints['lower'],
                judge_endpoint=endpoints['result_judge'])

        controller = f.DevController(run_dir=run, workspace=workspace, evaluate=evaluate, max_dev_rounds=args.max_dev_rounds)
        controller.start()
        provider = run / 'builder_provider.toml'
        provider.write_text('model_provider="gateway_direct"\n')
        config = f.stage_builder(run_dir=run, public=public, workspace=workspace, controller=controller,
            provider_config=provider, public_cases=('dev_001', 'dev_002'))
        instruction = run / 'builder_task/instruction.md'
        instruction.write_text(instruction.read_text() + '\nThis is non-formal public repair acceptance. After the first authoritative feedback, make a substantive product revision and submit again with the exact preceding feedback digest. Report-only or formatting-only changes do not establish a product revision. This diagnostic leaves the formal one-submission minimum unchanged and never dispatches hidden or Code scoring.\n')
        builder = f.run_configured_builder(args.harbor, config, run, args.builder_timeout,
            credential=args.credential_file, transport='direct', base_url=args.builder_base_url, proxy=args.builder_proxy)
        controller.wait_idle()
        if builder['exit_code'] == 0 and controller.records and not controller.frozen:
            controller.freeze_latest('builder_exit')
        native = controller.native_attestation()
        proof = {'native_evidence': native, 'builder_exit_code': builder['exit_code'],
            'candidate_records': controller.records, 'feedback_deliveries': controller.feedback_deliveries,
            'accepted_rounds': len(controller.records), 'freeze': controller.frozen,
            'source_unchanged': source == source_digest(ROOT),
            'shared_sources_unchanged': all(hashlib.sha256(Path(path).read_bytes()).hexdigest() == digest for path, digest in bindings.items()),
            'formal_branch': False, 'hidden_dispatched': False, 'code_judge_dispatched': False}
        proof['acceptance_complete'] = bool(builder['exit_code'] == 0 and native['valid'] and native['revision_observed']
            and proof['freeze'] and proof['source_unchanged'] and proof['shared_sources_unchanged']
            and all(record['state'] == 'completed' and set(record['results']) == {'dev_001', 'dev_002'} for record in controller.records))
        f.write_json(run / 'builder_session_attestation.json', proof)
        f.write_json(run / 'summary.json', {'status': 'builder_acceptance_complete' if proof['acceptance_complete'] else 'builder_acceptance_incomplete',
            'formal_ready': False, 'native_session': controller.builder_session_id, 'builder': native_stats(run),
            **{role: f.broker_stats(port) for role, port in ports.items()}})
        return 0 if proof['acceptance_complete'] else 2
    finally:
        if controller:
            controller.stop()
        receipts = [f.cleanup_broker(role=role, name=names[role], port=ports[role], attempted=attempted[role], run_dir=run, cidfile=cids[role]) for role in ports]
        f.write_json(run / 'cleanup_attestation.json', {'complete': all(row['absent_after_cleanup'] for row in receipts),
            'brokers': receipts, 'builder_broker_started': False, 'unrelated_containers_touched': False})


if __name__ == '__main__':
    raise SystemExit(main())
