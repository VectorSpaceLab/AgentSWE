#!/usr/bin/env python3
"""Freeze/hidden-order gate for the six evaluator-owned hidden cases."""
from __future__ import annotations

import argparse
import json
import subprocess
import time
from datetime import datetime
from pathlib import Path
from typing import Any, Callable

HIDDEN = tuple(f"test_{index:03d}" for index in range(1, 7))


def validate_freeze(manifest: dict[str, Any]) -> None:
    if manifest.get("schema_version") == "dyad-agentloop-freeze-v2":
        required = {"schema_version", "candidate_digest", "source_submission", "accepted_submission_count", "max_dev_rounds", "accepted_candidate_digests", "source_submission_id", "builder_session_id", "feedback_digest", "feedback_consumed", "feedback_chain_complete", "frozen_at"}
        if set(manifest) - required - {"hidden_started_at", "readonly", "candidate_digest_after_readonly", "pilot_not_formal", "builder_connection_id"} or not required <= set(manifest):
            raise ValueError("freeze manifest is incomplete")
        if not 1 <= int(manifest["source_submission"]) <= 10 or manifest["source_submission"] != manifest["accepted_submission_count"]:
            raise ValueError("freeze source submission is not the latest accepted submission")
        digests = manifest["accepted_candidate_digests"]
        if len(digests) != manifest["accepted_submission_count"] or len(set(digests)) != len(digests):
            raise ValueError("accepted candidate digests are not distinct")
        if manifest["feedback_consumed"] is not True or manifest["feedback_chain_complete"] is not True:
            raise ValueError("accepted-submission feedback chain is incomplete")
        if not manifest["builder_session_id"]:
            raise ValueError("same-session feedback attestation is incomplete")
        return
    required = {"schema_version", "candidate_digest", "candidate_1_digest", "candidate_2_digest", "source_submission_id", "builder_session_id", "candidate_1_feedback_sha256", "frozen_at"}
    if set(manifest) - required - {"hidden_started_at"} or not required <= set(manifest):
        raise ValueError("freeze manifest is incomplete")
    if manifest["schema_version"] != "dyad-agentloop-freeze-v1":
        raise ValueError("wrong freeze schema")
    if manifest["candidate_1_digest"] == manifest["candidate_2_digest"]:
        raise ValueError("Candidate 1 and Candidate 2 digests collide")
    if not manifest["builder_session_id"] or len(manifest["candidate_1_feedback_sha256"]) != 64:
        raise ValueError("same-session feedback attestation is incomplete")


def run_hidden_after_freeze(run_dir: Path, runner: Callable[[str, Path], dict[str, Any]]) -> dict[str, Any]:
    manifest_path = run_dir / "freeze_manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    validate_freeze(manifest)
    frozen = run_dir / "frozen_candidate"
    if not frozen.is_dir():
        raise ValueError("frozen Candidate directory is missing")
    started = datetime.now().astimezone().isoformat()
    if manifest.get("hidden_started_at") and started <= str(manifest["hidden_started_at"]):
        raise ValueError("hidden start time is not monotonic")
    cases = {case_id: runner(case_id, frozen) for case_id in HIDDEN}
    return {"schema_version": "dyad-agentloop-hidden-suite-v1", "hidden_started_at": started, "cases": cases, "hidden_after_freeze": True}


def run_real_hidden(run_dir: Path, broker_endpoint: str) -> dict[str, Any]:
    """Dispatch all six frozen cases through the real lower runner.

    This function intentionally has no placeholder fallback. A missing broker,
    launcher failure, provider error, or missing artifact remains visible in the
    per-case result and the suite is never promoted to a formal score.
    """
    launcher = Path(__file__).resolve().parents[2] / "evaluator" / "harness" / "run_lower_agent_case.py"
    frozen = run_dir / "frozen_candidate"
    hidden_dir = run_dir / "hidden"
    hidden_dir.mkdir(parents=True, exist_ok=True)
    started = datetime.now().astimezone().isoformat()
    cases: dict[str, Any] = {}
    for case_id in HIDDEN:
        output = hidden_dir / f"{case_id}.json"
        command = [
            "python3", str(launcher),
            "--repository", str(frozen),
            "--case", str(Path(__file__).resolve().parents[2] / "test_cases" / case_id),
            "--broker-endpoint", broker_endpoint,
            "--output", str(output),
            "--mode", "headless",
        ]
        launched_at = time.monotonic()
        completed = subprocess.run(command, text=True, capture_output=True, check=False, timeout=630)
        elapsed = round(time.monotonic() - launched_at, 3)
        if output.is_file():
            try:
                value = json.loads(output.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError) as exc:
                value = {"classification": "infrastructure-invalid", "failure_class": "invalid_hidden_result", "error": str(exc)}
        else:
            value = {"classification": "infrastructure-invalid", "failure_class": "hidden_runner_no_result"}
        if isinstance(value, dict):
            value.setdefault("runner_exit_code", completed.returncode)
            value.setdefault("runner_duration_seconds", elapsed)
            value.setdefault("runner_stdout_tail", completed.stdout[-2000:])
            value.setdefault("runner_stderr_tail", completed.stderr[-2000:])
        cases[case_id] = value
    return {"schema_version": "dyad-agentloop-hidden-suite-v1", "hidden_started_at": started, "cases": cases, "hidden_after_freeze": True, "execution": "real-lower-runner"}


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--run-dir", type=Path, required=True)
    parser.add_argument("--broker-endpoint", required=True)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    result = run_real_hidden(args.run_dir.resolve(), args.broker_endpoint)
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(json.dumps(result, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    print(json.dumps(result, indent=2, ensure_ascii=False))
    return 0 if all(isinstance(item, dict) and item.get("classification") in {"valid", "candidate_failure"} for item in result["cases"].values()) else 2


if __name__ == "__main__":
    raise SystemExit(main())
