#!/usr/bin/env python3
"""Fresh hidden acceptance of an immutable existing Aider product, no Builder replay."""
import argparse
import hashlib
import shutil
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from harbor.agentloop_controller import Controller, digest, _make_tree_read_only, now
from harbor.formal_one_stop import port, start_broker, stats, write_json, cleanup_owned_containers, JUDGE_BROKER_SCRIPT
from evaluator.formal_finalize import finalize, run_code_judge


def run(args):
    run_dir, source = args.run_dir.resolve(), args.candidate_source.resolve()
    if run_dir.exists() and any(run_dir.iterdir()):
        raise ValueError("acceptance requires a new output directory")
    if (not source.is_dir() or any(p.is_symlink() and source not in p.resolve().parents for p in source.rglob("*"))
            or not (source / "aider").is_dir()):
        raise ValueError("a materialized Aider product with no escaping symlinks is required")
    cases = tuple(args.cases)
    if not cases or len(set(cases)) != len(cases) or any(c not in {f"test_{i:03d}" for i in range(1, 7)} for c in cases):
        raise ValueError("acceptance cases must be a unique nonempty hidden subset")
    run_dir.mkdir(parents=True, exist_ok=True)
    lifecycle = run_dir / "lifecycle"
    frozen = lifecycle / "frozen_candidate"
    before = digest(source)
    shutil.copytree(source, frozen, symlinks=True)
    if digest(source) != before or digest(frozen) != before:
        raise RuntimeError("source changed during acceptance copy")
    _make_tree_read_only(frozen)
    write_json(run_dir / "acceptance_source.json", {"mode": "acceptance_reuse", "source": str(source),
        "candidate_digest": before, "source_modified": False, "builder_lifecycle_replayed": False,
        "formal_result_publishable": False})
    freeze = {"schema_version": "agentswe-aider-freeze-manifest-v2", "candidate_digest": before,
        "build_digest": before, "frozen_candidate_path": str(frozen), "freeze_reason": "acceptance_reuse",
        "frozen_at": now(), "frozen_tree_read_only": True, "frozen_tree_bounded_symlinks": True,
        "hidden_case_inventory": list(cases)}
    write_json(lifecycle / "freeze_manifest.json", freeze)
    if args.stage_only:
        write_json(run_dir / "stage_result.json", {"mode": "provider_free_stage", "provider_calls": 0,
            "docker_started": False, "formal_result_publishable": False, "candidate_digest": before})
        return 0
    run_code_judge(run_dir, freeze, args.credential_file.resolve(), preflight_only=True)
    ports = {"hidden": port(), "judge": port()}
    while ports["hidden"] == ports["judge"]:
        ports["judge"] = port()
    suffix = hashlib.sha256(str(run_dir).encode()).hexdigest()[:10]
    names = {role: f"agentswe-aider-accept-{role}-{suffix}" for role in ports}
    cids = {role: run_dir / f"{role}.cid" for role in ports}
    attempted = {role: False for role in ports}
    try:
        attempted["hidden"] = True
        start_broker(name=names["hidden"], script=ROOT / "evaluator/broker/lower_responses_broker.py",
            credential=args.credential_file.resolve(), value_port=ports["hidden"], effort="high", cidfile=cids["hidden"])
        endpoint = f"http://127.0.0.1:{ports['hidden']}/v1/responses"
        initial = stats(endpoint)
        if initial.get("runtime", {}).get("calls") != 0 or initial.get("runtime", {}).get("failures") != 0:
            raise RuntimeError("acceptance hidden broker must be fresh with zero calls")
        write_json(run_dir / "hidden_broker_initial.json", {"started_after_freeze": True, "stats": initial})
        controller = Controller(lifecycle, source, endpoint, True, dependency_overlay=args.dependency_overlay,
            image=args.lower_image, hidden_cases=cases, pilot_not_formal=True)
        controller.frozen = freeze
        controller.run_hidden()
        write_json(run_dir / "hidden_broker_stats.json", stats(endpoint))
        attempted["judge"] = True
        start_broker(name=names["judge"], script=JUDGE_BROKER_SCRIPT,
            credential=args.credential_file.resolve(), value_port=ports["judge"], effort="max", cidfile=cids["judge"])
        code, value = finalize(run_dir, args.credential_file.resolve(), f"http://127.0.0.1:{ports['judge']}/v1/responses", acceptance_cases=list(cases))
        write_json(run_dir / "summary.json", {"mode": "acceptance_reuse", "formal_result_publishable": False,
            "status": "acceptance_complete" if code == 0 else "acceptance_incomplete", "scoring": value})
        return code
    finally:
        write_json(run_dir / "cleanup_attestation.json", cleanup_owned_containers(names, attempted, cids))


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run-dir", type=Path, required=True)
    parser.add_argument("--candidate-source", type=Path, required=True)
    parser.add_argument("--cases", nargs="+", default=["test_001"])
    parser.add_argument("--stage-only", action="store_true")
    parser.add_argument("--credential-file", type=Path, default=Path("@@AGENTSWE_CREDENTIAL_FILE@@"))
    parser.add_argument("--lower-image", default="agentswe/edit-candidate-python311:0826")
    parser.add_argument("--dependency-overlay", type=Path, default=Path("@@AGENTSWE_ENVS@@/aider-worktree-transaction-ledger-edit-v1/lib/python3.11/site-packages"))
    raise SystemExit(run(parser.parse_args()))
