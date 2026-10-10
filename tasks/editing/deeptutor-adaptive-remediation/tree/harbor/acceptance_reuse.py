#!/usr/bin/env python3
"""Fresh lower/Result/Code diagnostic reusing verified historical frozen bytes.

This is NOT a new Builder lifecycle and cannot prove that a Builder consumed
the repaired semantic-dev feedback. No historical hidden score is reused.
Without --run-acceptance this entry performs only read-only validation.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys

from formal_one_stop import BrokerProcess, ROOT, DEFAULT_CREDENTIAL, DEFAULT_RUNTIME_PYTHON, read_json, write_json, stop_brokers
from agentloop.protocol import tree_digest, utc_now
from agentloop.run_hidden import run as run_hidden


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source-run", type=Path, required=True)
    parser.add_argument("--run-dir", type=Path, required=True)
    parser.add_argument("--expected-digest", required=True)
    parser.add_argument("--runtime-python", type=Path, default=DEFAULT_RUNTIME_PYTHON)
    parser.add_argument("--credential-file", type=Path, default=DEFAULT_CREDENTIAL)
    parser.add_argument("--run-acceptance", action="store_true")
    parser.add_argument("--stage-only", action="store_true", help="materialize a fresh frozen-source stage; no brokers/API")
    parser.add_argument("--resume-staged", action="store_true", help="run only a hash-verified stage created with --stage-only")
    args = parser.parse_args()
    source = args.source_run.resolve()
    origin_manifest = source / "lifecycle/freeze_manifest.json"
    origin = read_json(origin_manifest)
    frozen = Path(origin["candidate_path"])
    if origin.get("candidate_digest") != args.expected_digest or tree_digest(frozen) != args.expected_digest:
        raise ValueError("historical frozen Candidate identity mismatch")
    if not frozen.resolve().is_relative_to(source):
        raise ValueError("historical frozen Candidate escapes its declared run")
    if any(p.is_symlink() for p in frozen.rglob("*")):
        raise ValueError("historical frozen Candidate has symlinks")
    if args.stage_only and args.run_acceptance or args.resume_staged and not args.run_acceptance:
        parser.error("stage-only cannot run APIs; resume-staged requires run-acceptance")
    if args.run_dir.exists() and not args.resume_staged:
        raise ValueError("acceptance destination must not already exist")
    plan = {"kind": "acceptance_reuse", "candidate_digest": args.expected_digest,
        "source_run": str(source), "new_run": str(args.run_dir.resolve()),
        "hidden_cases": ["test_001"], "lower_model": "deepseek-flash", "lower_effort": "high",
        "result_judge": "deepseek-flash/xhigh", "code_judge": "deepseek-flash/xhigh",
        "historical_hidden_scores_reused": False, "builder_rerun": False,
        "builder_consumed_repaired_semantic_feedback": False,
        "complete_new_builder_lifecycle_gate_satisfied": False}
    if not args.run_acceptance and not args.stage_only:
        print(json.dumps({**plan, "read_only_validation_passed": True, "provider_calls": 0}, indent=2))
        return 0
    run = args.run_dir.resolve()
    if args.resume_staged:
        stage = read_json(run / "stage_input_hashes.json")
        if stage.get("candidate_digest") != args.expected_digest or stage.get("source_run") != str(source):
            raise ValueError("staged source identity differs")
        for path, expected in stage["sha256"].items():
            if not Path(path).is_file() or hashlib.sha256(Path(path).read_bytes()).hexdigest() != expected:
                raise ValueError("staged evaluator/input hash changed: " + path)
        if tree_digest(run / "lifecycle/frozen_candidate") != args.expected_digest:
            raise ValueError("staged frozen Candidate changed")
        if (run / "hidden").exists() or (run / "summary.json").exists():
            raise ValueError("staged acceptance already attempted; use a fresh run")
        prepared = read_json(run / "stage_code_preflight.json")
        if prepared.get("valid") is not True:
            raise ValueError("actual staged Code preflight not attested")
        for path, expected in prepared["input_sha256"].items():
            if not Path(path).is_file() or hashlib.sha256(Path(path).read_bytes()).hexdigest() != expected:
                raise ValueError("staged Code input changed: " + path)
    else:
        run.mkdir(parents=True, exist_ok=False)
    lifecycle = run / "lifecycle"
    if not args.resume_staged:
        lifecycle.mkdir()
    copied = lifecycle / "frozen_candidate"
    if not args.resume_staged:
        shutil.copytree(frozen, copied, symlinks=False)
    if tree_digest(copied) != args.expected_digest or tree_digest(frozen) != args.expected_digest:
        raise ValueError("frozen bytes changed during isolated acceptance copy")
    if not args.resume_staged:
        for path in (copied, *copied.rglob("*")):
            path.chmod(path.stat().st_mode & ~0o222)
    historical = lifecycle / "historical_source_freeze_manifest.json"
    if not args.resume_staged:
        shutil.copyfile(origin_manifest, historical)
    origin_attestation = Path(origin["builder_session_attestation"])
    if not args.resume_staged:
        shutil.copyfile(origin_attestation, lifecycle / "historical_builder_session_attestation.json")
    manifest = {**origin, "candidate_path": str(copied),
        "builder_session_attestation": str(lifecycle / "historical_builder_session_attestation.json"),
        "acceptance_reuse": True, "historical_freeze_manifest": str(historical),
        "historical_freeze_manifest_sha256": hashlib.sha256(historical.read_bytes()).hexdigest(),
        "repaired_dev_feedback_consumed": False}
    if not args.resume_staged:
        write_json(lifecycle / "freeze_manifest.json", manifest)
        write_json(run / "acceptance_reuse_provenance.json", {**plan, "started_at": utc_now()})
    if args.stage_only:
        sources = []
        for name in ("agentloop", "evaluator", "harbor", "test_cases", "dev_cases"):
            sources.extend(p for p in (ROOT / name).rglob("*") if p.is_file() and not p.is_symlink()
                and "__pycache__" not in p.parts)
        sources.extend(p for p in (ROOT / "input").glob("*.md") if p.is_file())
        shared = Path("@@AGENTSWE_EDITING_CONTROL@@")
        sources.extend(shared / name for name in ("formal_axes_shared.py", "execution_contract.py", "execution_scoring.py",
            "result_judge.py", "judge_broker_xhigh.py", "judge_broker_runtime.py", "judge_transport_configuration_delta.json",
            "code_judge_runner.py", "code_judge_entry.py", "code_judge_sitecustomize.py"))
        sources.extend((Path("@@AGENTSWE_EDITING_CONTROL@@/code_eval.py"), lifecycle / "freeze_manifest.json",
            lifecycle / "historical_source_freeze_manifest.json", lifecycle / "historical_builder_session_attestation.json"))
        write_json(run / "stage_input_hashes.json", {"candidate_digest": args.expected_digest, "source_run": str(source),
            "provider_calls": 0, "sha256": {str(path): hashlib.sha256(path.read_bytes()).hexdigest() for path in sorted(set(sources))}})
        print(json.dumps({**plan, "staged": True, "provider_calls": 0, "ready_for_code_preflight_not_paid": True}, indent=2))
        return 0
    lower = judge = None
    result = {**plan, "acceptance_complete": False, "formal_result_claimed": False}
    try:
        lower = BrokerProcess(run_dir=run, role="hidden", credential=args.credential_file,
            python=sys.executable, max_calls=100, max_tokens=1_000_000)
        judge = BrokerProcess(run_dir=run, role="result_judge", credential=args.credential_file,
            python=sys.executable, max_calls=10, max_tokens=1_000_000, broker_kind="responses_xhigh")
        hidden = run_hidden(lifecycle / "freeze_manifest.json", run / "hidden", broker_endpoint=lower.endpoint_local,
            python_executable=str(args.runtime_python.absolute()), case_ids=("test_001",), pilot_not_formal=True)
        command = [sys.executable, str(ROOT / "evaluator/formal_finalize.py"), "--run-dir", str(run),
            "--credential-file", str(args.credential_file), "--result-judge-broker-endpoint", judge.endpoint_local,
            "--acceptance-cases", "test_001"]
        finalized = subprocess.run(command, text=True, capture_output=True, check=False)
        (run / "finalizer.stdout.log").write_text(finalized.stdout)
        (run / "finalizer.stderr.log").write_text(finalized.stderr)
        aggregation = read_json(run / "acceptance_aggregation.json") if (run / "acceptance_aggregation.json").is_file() else {}
        result.update({"acceptance_complete": aggregation.get("acceptance_complete") is True,
            "finalizer_exit_code": finalized.returncode, "result_axis": aggregation.get("result_axis", "N/A"),
            "code_axis": aggregation.get("code_axis", "N/A"), "hidden_all_cases_real": hidden.get("all_cases_real"),
            "new_builder_lifecycle_gate_satisfied": False, "finished_at": utc_now()})
        return 0 if result["acceptance_complete"] else 2
    except Exception as exc:
        result.update({"error": f"{type(exc).__name__}: {exc}", "finished_at": utc_now()})
        return 2
    finally:
        cleanup = stop_brokers((judge, lower))
        write_json(run / "cleanup_attestation.json", {"containers": cleanup,
            "all_started_containers_absent": all(item.get("absent_after_cleanup") is True for item in cleanup),
            "builder_or_harbor_started": False})
        write_json(run / "summary.json", result)
        print(json.dumps(result, indent=2))

if __name__ == "__main__":
    raise SystemExit(main())
