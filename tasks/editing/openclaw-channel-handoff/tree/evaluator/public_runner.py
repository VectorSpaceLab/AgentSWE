#!/usr/bin/env python3
"""Legacy probe-only public runner.

The formal lifecycle is owned by ``harbor/formal_one_stop.py``.  This module is
retained only to reproduce old bounded probes and therefore requires an
explicit ``--legacy-probe`` flag.  It never attests a real Builder session and
its output is not eligible for formal hidden execution.
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from controller.two_round_controller import DEV, TwoRoundController, feedback_projection
from evaluator.case_service import write_private_case
from evaluator.hidden_executor import launch_case


def write_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")


def evaluate_round(
    *,
    round_number: int,
    candidate: Path,
    run_dir: Path,
    broker_endpoint: str,
    runtime: Path | None,
    timeout_seconds: int,
) -> dict[str, Any]:
    results: dict[str, Any] = {}
    for case_id in DEV:
        case_dir = ROOT / "dev_cases" / case_id
        private_dir = run_dir / ".public-evaluator-private" / f"candidate_{round_number:03d}" / case_id
        private_file, view = write_private_case(private_dir, case_id)
        try:
            results[case_id] = launch_case(
                case_id=case_id,
                hidden_case=case_dir,
                frozen_candidate=candidate,
                output=run_dir / "public" / f"candidate_{round_number:03d}" / case_id,
                broker_endpoint=broker_endpoint,
                runtime=runtime,
                private_file=private_file,
                view=view,
                timeout_seconds=timeout_seconds,
            )
        except Exception as exc:
            results[case_id] = {
                "case_id": case_id,
                "classification": "launcher_infrastructure_error",
                "classification_reason": f"{type(exc).__name__}: {exc}",
                "formal_result_claimed": False,
            }
    return results


def run_public_lifecycle(
    *,
    candidate_1: Path,
    candidate_2: Path,
    run_dir: Path,
    broker_endpoint: str,
    runtime: Path | None,
    builder_session_id: str,
    timeout_seconds: int,
    legacy_probe: bool = False,
) -> dict[str, Any]:
    if not legacy_probe:
        raise RuntimeError("formal public lifecycle requires harbor/formal_one_stop.py; use --legacy-probe only for historical probes")
    run_dir = run_dir.resolve()
    if run_dir.exists() and any(run_dir.iterdir()):
        raise RuntimeError(f"public lifecycle output already exists: {run_dir}")
    run_dir.mkdir(parents=True, exist_ok=True)
    session = builder_session_id.strip()
    if not session:
        raise ValueError("builder_session_id must be non-empty")

    holder: dict[str, Any] = {}

    def evaluate(round_number: int, snapshot: Path) -> dict[str, Any]:
        result = evaluate_round(
            round_number=round_number,
            candidate=snapshot,
            run_dir=run_dir,
            broker_endpoint=broker_endpoint,
            runtime=runtime,
            timeout_seconds=timeout_seconds,
        )
        holder[f"candidate_{round_number}"] = result
        return result

    controller = TwoRoundController(run_dir, evaluate)
    first = controller.submit(candidate_1.resolve())
    lifecycle: dict[str, Any] = {
        "schema_version": "openclaw-agentloop-public-lifecycle-v1",
        "builder_session_id": session,
        "run_mode": "probe",
        "formal_lifecycle_eligible": False,
        "feedback_delivered_before_candidate_2": False,
        "candidate_1": first.__dict__,
        "candidate_2": None,
        "freeze": None,
        "formal_result_claimed": False,
    }
    write_json(run_dir / "builder_feedback_for_candidate_2.json", {"builder_session_id": session, "candidate_1_digest": first.digest, "dev_feedback": feedback_projection(holder.get("candidate_1", {})), "feedback_source": str(run_dir / "feedback" / "candidate_001.json")})
    lifecycle["feedback_delivered_before_candidate_2"] = True
    if first.classification == "infrastructure-invalid":
        lifecycle["blocked_reason"] = "Candidate 1 public dev was infrastructure-invalid; no Candidate 2 or freeze was accepted"
        write_json(run_dir / "public_lifecycle.json", lifecycle)
        return lifecycle

    second = controller.submit(candidate_2.resolve())
    lifecycle["candidate_2"] = second.__dict__
    if second.classification == "infrastructure-invalid":
        lifecycle["blocked_reason"] = "Candidate 2 public dev was infrastructure-invalid; freeze was refused"
        write_json(run_dir / "public_lifecycle.json", lifecycle)
        return lifecycle
    lifecycle["freeze"] = controller.freeze_candidate_2()
    write_json(run_dir / "public_lifecycle.json", lifecycle)
    return lifecycle


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Run the legacy OpenClaw public probe; formal lifecycle uses the latest accepted Candidate")
    parser.add_argument("--candidate-1", type=Path, required=True)
    parser.add_argument("--candidate-2", type=Path, required=True)
    parser.add_argument("--run-dir", type=Path, required=True)
    parser.add_argument("--broker-endpoint", required=True)
    parser.add_argument("--runtime", type=Path)
    parser.add_argument("--builder-session-id", required=True)
    parser.add_argument("--timeout-seconds", type=int, default=900)
    parser.add_argument("--legacy-probe", action="store_true", help="allow historical probe execution; never formal")
    args = parser.parse_args(argv)
    result = run_public_lifecycle(
        candidate_1=args.candidate_1,
        candidate_2=args.candidate_2,
        run_dir=args.run_dir,
        broker_endpoint=args.broker_endpoint,
        runtime=args.runtime,
        builder_session_id=args.builder_session_id,
        timeout_seconds=args.timeout_seconds,
        legacy_probe=args.legacy_probe,
    )
    print(json.dumps(result, indent=2, ensure_ascii=False))
    return 0 if result.get("freeze") else 1


if __name__ == "__main__":
    raise SystemExit(main())
