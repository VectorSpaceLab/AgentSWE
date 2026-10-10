#!/usr/bin/env python3
"""Measure an immutable historical Claude delivery without replaying a Builder."""
import argparse
import hashlib
import json
import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from harbor.formal_one_stop import (validate_delivery, delivery_digest, free_port, start_broker,
    cleanup_owned_container, broker_stats, LOWER_IMAGE, JUDGE_BROKER_SCRIPT, now)
from agentloop.evaluator.materialize import materialize
from agentloop.evaluator.controller import _read_only_tree
from agentloop.evaluator.hidden_executor import run_hidden, candidate_digest, CASE_IDS, FREEZE_SCHEMA
from agentloop.evaluator.hidden_attestation import attest
from agentloop.evaluator.case_contract import load_case_bundle
from agentloop.protocol import write_json
from evaluator.formal_finalize import finalize, prepare_code_inputs, CODE_JUDGE

LOWER_PROVIDER_URL = 'https://api.deepseek.com/v1/responses'


def start_acceptance_lower(*, name, credential, port, cidfile):
    """Do not inherit the legacy standalone broker's OpenAI endpoint default."""
    return start_broker(name=name, script=ROOT / 'agentloop/evaluator/broker.py',
        credential=credential, port=port, image=LOWER_IMAGE, cidfile=cidfile,
        provider_url=LOWER_PROVIDER_URL)


def preflight_code(run_dir, frozen, frozen_digest, credential):
    requirements, code_digest = prepare_code_inputs(run_dir, frozen, frozen_digest)
    output = run_dir / 'formal_scoring/code_preflight'
    completed = subprocess.run([sys.executable, str(CODE_JUDGE),
        '--candidate-source', str(frozen), '--public-requirements', str(requirements),
        '--code-rubric', str(ROOT / 'evaluator/code_rubric.md'),
        '--credential-file', str(credential), '--output-dir', str(output),
        '--expected-candidate-digest', code_digest, '--preflight-only'],
        text=True, capture_output=True, check=False, timeout=240)
    write_json(run_dir / 'code_preflight.json', {'provider_calls': 0,
        'preflight_valid': completed.returncode == 0, 'output': str(output),
        'exit_code': completed.returncode, 'candidate_digest': frozen_digest,
        'code_full_tree_digest': code_digest, 'lower_started': False})
    if completed.returncode:
        raise RuntimeError('independent Code runtime/source preflight failed before lower execution')


def staged_identity(args):
    """Bind a provider-free stage to exactly the later acceptance inputs."""
    run_dir = args.run_dir.resolve()
    frozen = run_dir / 'lifecycle/frozen_candidate'
    source = json.loads((run_dir / 'acceptance_source.json').read_text())
    freeze = json.loads((run_dir / 'lifecycle/freeze_manifest.json').read_text())
    digest = candidate_digest(frozen)
    if digest != source['candidate_digest'] or digest != freeze['candidate_digest']:
        raise ValueError('staged Candidate identity changed')
    if freeze['hidden_case_inventory'] != list(args.cases):
        raise ValueError('staged hidden subset changed')
    if (delivery_digest(args.delivery.resolve()) != source['delivery_digest']
            or delivery_digest(run_dir / 'delivery') != source['delivery_digest']):
        raise ValueError('staged delivery identity changed')
    preflight = json.loads((run_dir / 'code_preflight.json').read_text())
    if preflight.get('preflight_valid') is not True or preflight.get('lower_started') is not False:
        raise ValueError('stage lacks successful provider-free Code preflight')
    requirements, code_digest = prepare_code_inputs(run_dir, frozen, digest)
    if preflight.get('code_full_tree_digest') != code_digest:
        raise ValueError('staged Code full-tree identity changed')
    hashes = {}
    for name in ("judge_broker_xhigh.py", "judge_broker_runtime.py", "result_judge.py"):
        path = JUDGE_BROKER_SCRIPT.parent / name
        hashes[str(path)] = hashlib.sha256(path.read_bytes()).hexdigest()
    for case_id in args.cases:
        *_, visible, oracle = load_case_bundle(args.hidden_cases_dir.resolve(), case_id)
        for path in (visible, oracle):
            hashes[str(path)] = hashlib.sha256(path.read_bytes()).hexdigest()
    for relative in ('acceptance_source.json', 'lifecycle/freeze_manifest.json',
                     'code_preflight.json', 'formal_scoring/code_source_binding.json'):
        hashes[relative] = hashlib.sha256((run_dir / relative).read_bytes()).hexdigest()
    for path in sorted(requirements.iterdir()):
        hashes[str(path.resolve().relative_to(run_dir))] = hashlib.sha256(path.read_bytes()).hexdigest()
    return {'schema_version': 'agentswe-claude-staged-acceptance/v1',
            'candidate_digest': digest, 'code_full_tree_digest': code_digest,
            'cases': list(args.cases), 'hashes': hashes, 'provider_calls': 0}


def resume_preflight(args):
    run_dir = args.run_dir.resolve()
    for forbidden in ('hidden.cid', 'judge.cid', 'hidden_after_freeze', 'summary.json', 'cleanup_attestation.json'):
        if (run_dir / forbidden).exists():
            raise ValueError('staged acceptance has already attempted execution')
    expected = json.loads((run_dir / 'staged_acceptance_identity.json').read_text())
    if staged_identity(args) != expected:
        raise ValueError('provider-free stage inputs changed before execution')
    return run_dir / 'lifecycle/frozen_candidate', expected['candidate_digest']


def run(args):
    run_dir, source = args.run_dir.resolve(), args.delivery.resolve()
    resume = getattr(args, 'resume_preflight', False)
    if run_dir.exists() and any(run_dir.iterdir()) and not resume:
        raise ValueError("acceptance requires a new output directory")
    if validate_delivery(source):
        raise ValueError("existing delivery violates the task's unchanged delivery contract")
    cases = list(args.cases)
    if not cases or len(set(cases)) != len(cases) or any(c not in CASE_IDS for c in cases):
        raise ValueError("acceptance cases must be a unique hidden subset")
    for case_id in cases:
        load_case_bundle(args.hidden_cases_dir.resolve(), case_id)
    lifecycle = run_dir / "lifecycle"
    frozen = lifecycle / "frozen_candidate"
    freeze_path = lifecycle / "freeze_manifest.json"
    if resume:
        frozen, current_digest = resume_preflight(args)
    else:
        run_dir.mkdir(parents=True, exist_ok=True)
        delivery = run_dir / "delivery"
        before = delivery_digest(source)
        shutil.copytree(source, delivery, symlinks=False)
        if delivery_digest(source) != before or delivery_digest(delivery) != before:
            raise RuntimeError("historical delivery changed during copying")
        build = materialize(ROOT / "input/repository", delivery / "solution.patch", frozen)
        current_digest = candidate_digest(frozen)
        _read_only_tree(frozen)
        write_json(run_dir / "acceptance_source.json", {"mode": "acceptance_reuse", "source": str(source),
            "delivery_digest": before, "candidate_digest": current_digest, "source_modified": False,
            "builder_lifecycle_replayed": False, "formal_result_publishable": False})
        write_json(freeze_path, {"schema_version": FREEZE_SCHEMA, "candidate_root": str(frozen),
            "candidate_digest": current_digest, "hidden_allowed": True, "frozen_at": now(),
            "frozen_tree_read_only": True, "frozen_tree_regular": True,
            "hidden_case_inventory": cases, "freeze_reason": "acceptance_reuse"})
        if args.stage_only:
            write_json(run_dir / "stage_result.json", {"mode": "provider_free_stage", "provider_calls": 0,
                "docker_started": False, "formal_result_publishable": False, "build": build})
            return 0 if build.get("build_valid") else 2
        preflight_code(run_dir, frozen, current_digest, args.credential_file.resolve())
        if getattr(args, 'preflight_only', False):
            write_json(run_dir / 'staged_acceptance_identity.json', staged_identity(args))
            return 0
    ports = {"hidden": free_port(), "judge": free_port()}
    while ports["hidden"] == ports["judge"]:
        ports["judge"] = free_port()
    suffix = hashlib.sha256(str(run_dir).encode()).hexdigest()[:10]
    names = {role: f"agentswe-claude-accept-{role}-{suffix}" for role in ports}
    cids = {role: run_dir / f"{role}.cid" for role in ports}
    attempted = {role: False for role in ports}
    phase = 'lower_startup'
    try:
        attempted["hidden"] = True
        write_json(run_dir / 'lower_provider_configuration.json', {'provider_url': LOWER_PROVIDER_URL,
            'model': 'deepseek-flash', 'reasoning_effort': 'high', 'credential_values_recorded': False})
        start_acceptance_lower(name=names["hidden"], credential=args.credential_file.resolve(),
            port=ports["hidden"], cidfile=cids["hidden"])
        endpoint = f"http://127.0.0.1:{ports['hidden']}/v1/responses"
        hidden_run = run_dir / "hidden_after_freeze"
        phase = 'hidden_execution'
        run_hidden(freeze_manifest=freeze_path, cases_dir=args.hidden_cases_dir.resolve(), output=hidden_run,
            broker_endpoint=endpoint, case_ids=cases, acceptance_not_formal=True, timeout=600)
        attest(run_dir=lifecycle, hidden_run=hidden_run / "hidden_run.json",
            output=lifecycle / "hidden_after_freeze_attestation.json", case_ids=cases, acceptance=True)
        write_json(run_dir / "hidden_broker_stats.json", broker_stats(endpoint))
        attempted["judge"] = True
        phase = 'judge_startup'
        start_broker(name=names["judge"], script=JUDGE_BROKER_SCRIPT, credential=args.credential_file.resolve(),
            port=ports["judge"], image=LOWER_IMAGE, cidfile=cids["judge"])
        phase = 'independent_scoring'
        code, value = finalize(run_dir, credential_file=args.credential_file.resolve(),
            result_judge_broker_endpoint=f"http://127.0.0.1:{ports['judge']}/v1/responses", acceptance_cases=cases)
        write_json(run_dir / "summary.json", {"mode": "acceptance_reuse", "formal_result_publishable": False,
            "status": "acceptance_complete" if code == 0 else "acceptance_incomplete", "scoring": value})
        return code
    except Exception as exc:
        # Never let an evaluator/transport exception erase the run's terminal
        # status. Do not infer a Candidate zero or resample from this handler.
        write_json(run_dir / 'summary.json', {'mode': 'acceptance_reuse',
            'formal_result_publishable': False, 'acceptance_complete': False,
            'status': 'acceptance_incomplete', 'result_axis': 'N/A', 'code_axis': 'N/A',
            'evaluation_state': 'evaluator_exception', 'failed_phase': phase,
            'error_type': type(exc).__name__, 'automatic_retry': False,
            'raw_scoring_contracts': [str(path.relative_to(run_dir)) for path in
                sorted((run_dir / 'formal_scoring').glob('**/*score_contract.json'))]})
        return 2
    finally:
        # Capture counters while brokers still exist, including judge failure.
        for role in ports:
            if attempted[role]:
                try:
                    value = broker_stats(f'http://127.0.0.1:{ports[role]}/v1/responses')
                    write_json(run_dir / f'{role}_broker_final_stats.json', value)
                except Exception as exc:
                    write_json(run_dir / f'{role}_broker_final_stats.json', {'available': False, 'error_type': type(exc).__name__})
        records = [cleanup_owned_container(role, cids[role], attempted=attempted[role]) for role in ports]
        write_json(run_dir / "cleanup_attestation.json", {"records": records,
            "cleanup_complete": all(r.get("absent_after_cleanup") is True for r in records)})


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run-dir", type=Path, required=True)
    parser.add_argument("--delivery", type=Path, required=True)
    parser.add_argument("--hidden-cases-dir", type=Path, required=True)
    parser.add_argument("--cases", nargs="+", default=["test_001"])
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--stage-only", action="store_true")
    mode.add_argument("--preflight-only", action="store_true")
    mode.add_argument("--resume-preflight", action="store_true")
    parser.add_argument("--credential-file", type=Path, default=Path("@@AGENTSWE_CREDENTIAL_FILE@@"))
    raise SystemExit(run(parser.parse_args()))
