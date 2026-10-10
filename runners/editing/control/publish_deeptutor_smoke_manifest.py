#!/usr/bin/env python3
"""Publish a validated, non-formal DeepTutor smoke manifest.

Every field is derived from a completed run and its independent Result judge.
The command fails closed if any required evidence, digest, usage, or cleanup
condition is absent; it never invents a score or upgrades a pilot to formal.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import subprocess
from pathlib import Path
from typing import Any


def read_json(path: Path) -> dict[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError(f"expected JSON object: {path}")
    return value


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        while chunk := handle.read(1024 * 1024):
            digest.update(chunk)
    return digest.hexdigest()


def require(condition: bool, message: str) -> None:
    if not condition:
        raise RuntimeError(message)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--run", type=Path, required=True)
    parser.add_argument("--sibling", type=Path, required=True)
    parser.add_argument("--snapshot", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    run = args.run.resolve()
    sibling = args.sibling.resolve()
    snapshot = read_json(args.snapshot.resolve())
    task_snapshot = snapshot.get("tasks", {}).get("deeptutor", {})
    expected_digest = task_snapshot.get("sibling", {}).get("digest")
    require(isinstance(expected_digest, str) and expected_digest, "snapshot has no DeepTutor sibling digest")

    summary = read_json(run / "one_stop_summary.json")
    hidden = read_json(run / "hidden" / "hidden-after-freeze-attestation.json")
    cleanup = read_json(run / "cleanup_attestation.json")
    freeze = read_json(run / "lifecycle" / "freeze_manifest.json")
    judge = read_json(run / "result_judge" / "test_001" / "result_score_contract.json")
    artifact = read_json(run / "hidden" / "cases" / "test_001" / "agent_result.json")
    launcher = read_json(run / "hidden" / "cases" / "test_001" / "launcher_result.json")
    oracle = read_json(run / "hidden" / "cases" / "test_001" / "oracle_summary.json")
    broker = read_json(run / "hidden_broker_stats.json")

    require(summary.get("status") == "pilot_pipeline_complete", "one-stop pilot did not complete")
    require(
        summary.get("execution_mode") == "pilot"
        and hidden.get("pilot_not_formal") is True,
        "run is not explicitly marked non-formal",
    )
    require(hidden.get("all_cases_real") is True, "hidden execution was not real for every smoked case")
    require(hidden.get("executed_cases") == ["test_001"], "unexpected hidden case inventory")
    require(hidden.get("frozen_candidate_stable") is True, "frozen Candidate was not stable")
    require(freeze.get("candidate_digest") == hidden.get("candidate_digest"), "freeze/hidden Candidate digest mismatch")
    require(broker.get("model") == "deepseek-flash", "hidden broker model mismatch")
    require(broker.get("reasoning_effort") == "high", "hidden broker effort mismatch")
    require(int(broker.get("successful_calls", 0)) > 0, "no successful lower broker calls")
    require(launcher.get("executed") is True, "lower launcher did not execute")
    require(launcher.get("classification") in {"candidate_capability_gap", "candidate_failure", "candidate_valid"}, "unexpected candidate classification")
    require(artifact.get("case_id") == "test_001", "artifact is not case-bound")
    require(bool(artifact.get("product_stdout")), "model-driven product artifact has no product output")
    require(oracle.get("oracle_isolation") is True and oracle.get("candidate_visible") is False, "oracle isolation is not attested")
    require(judge.get("result_score_publishable") is True, "independent Result judge is not publishable")
    require(judge.get("judge", {}).get("model") == "deepseek-flash", "Result judge model mismatch")
    require(judge.get("judge", {}).get("reasoning_effort") == "max", "Result judge effort mismatch")
    require(judge.get("provider_usage", {}).get("logical_requests") == 1, "Result judge logical request count mismatch")
    require(judge.get("provider_usage", {}).get("completed_responses") == 1, "Result judge did not complete one response")
    require(cleanup.get("completed") is True and cleanup.get("all_started_containers_absent") is True, "run cleanup is incomplete")
    require(cleanup.get("unrelated_containers_touched") is False, "cleanup touched unrelated containers")

    cid_path = run / "result_judge_broker.cid"
    if cid_path.is_file():
        cid = cid_path.read_text(encoding="utf-8").strip()
        if cid:
            probe = subprocess.run(["docker", "inspect", cid], capture_output=True, text=True, check=False)
            require(probe.returncode != 0, "independent Result judge broker is still present")

    manifest = {
        "schema_version": "agentswe-edit-smoke-manifest-v2",
        "task": "deeptutor",
        "run_dir": str(run),
        "sibling_digest": expected_digest,
        "real_lower_product_entry": True,
        "lower_product_entry": launcher.get("entry"),
        "lower_model": broker.get("model"),
        "lower_effort": broker.get("reasoning_effort"),
        "successful_broker_calls": int(broker.get("successful_calls", 0)),
        "lower_broker_stats": str((run / "hidden_broker_stats.json").relative_to(run)),
        "agent_authored_artifact": True,
        "agent_artifact": str((run / "hidden" / "cases" / "test_001" / "agent_result.json").relative_to(run)),
        "agent_artifact_sha256": sha256_file(run / "hidden" / "cases" / "test_001" / "agent_result.json"),
        "oracle_isolation": True,
        "oracle_summary": str((run / "hidden" / "cases" / "test_001" / "oracle_summary.json").relative_to(run)),
        "native_evidence": str((run / "hidden" / "cases" / "test_001" / "native_evidence.json").relative_to(run)),
        "result_judge_model": judge["judge"]["model"],
        "result_judge_effort": judge["judge"]["reasoning_effort"],
        "result_judge_logical_requests": judge["provider_usage"]["logical_requests"],
        "result_judge_completed_responses": judge["provider_usage"]["completed_responses"],
        "result_judge_contract": str((run / "result_judge" / "test_001" / "result_score_contract.json").relative_to(run)),
        "result_score": judge.get("result_score"),
        "classification": launcher.get("classification"),
        "formal_result_publishable": False,
        "code_score_publishable": False,
        "cleanup_complete": True,
        "cleanup_attestation": str((run / "cleanup_attestation.json").relative_to(run)),
        "single_case_not_formal": True,
        "hidden_cases_smoked": 1,
        "hidden_cases": ["test_001"],
        "frozen_candidate_digest": freeze.get("candidate_digest"),
        "credential_values_recorded": False,
    }
    args.output.resolve().parent.mkdir(parents=True, exist_ok=True)
    args.output.resolve().write_text(json.dumps(manifest, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    print(json.dumps(manifest, indent=2, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
