#!/usr/bin/env python3
"""Apply/build once and score all six isolated hidden cases."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path


BENCHMARK_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(BENCHMARK_ROOT / "dev_cases"))

from harness_support import HarnessError, evaluate_case, prepare_candidate  # noqa: E402


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Run the OpenWiki hidden suite")
    parser.add_argument("--candidate", required=True, type=Path)
    parser.add_argument("--work-dir", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--keep-work", action="store_true")
    return parser.parse_args()


def zero_results(error: str) -> list[dict[str, object]]:
    return [
        {
            "case_id": f"test_{index:03d}",
            "valid": False,
            "score": 0.0,
            "dimension_scores": {},
            "assertions": [{"id": "VALID-CANDIDATE", "passed": False, "detail": error}],
            "invocation": None,
            "replay_invocation": None,
            "scenario_invocations": [],
            "worktree": None,
            "safety_ceiling_applied": False,
            "availability_ceiling_applied": False,
        }
        for index in range(1, 7)
    ]


def main() -> int:
    args = parse_args()
    work_dir = args.work_dir.resolve()
    work_dir.mkdir(parents=True, exist_ok=True)
    try:
        prepared = prepare_candidate(
            BENCHMARK_ROOT,
            args.candidate.resolve(),
            work_dir,
            install=True,
        )
    except HarnessError as exc:
        error = str(exc)
        case_results = zero_results(error)
        payload = {
            "schema_version": "1.0",
            "valid_candidate": False,
            "candidate_error": error,
            "setup_results": [],
            "case_results": case_results,
            "mean_score": 0.0,
        }
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
        print(json.dumps(payload, indent=2, sort_keys=True))
        return 2

    results = []
    for index in range(1, 7):
        case_id = f"test_{index:03d}"
        case_dir = BENCHMARK_ROOT / "test_cases" / case_id
        manifest = json.loads(
            (BENCHMARK_ROOT / "evaluator" / "manifests" / f"{case_id}.json").read_text(encoding="utf-8")
        )
        try:
            result = evaluate_case(
                prepared,
                case_dir,
                work_dir / case_id,
                keep_work=args.keep_work,
                spec_override=manifest,
            )
        except Exception as exc:  # keep later isolated cases observable
            result_payload = {
                "case_id": case_id,
                "valid": False,
                "score": 0.0,
                "dimension_scores": {},
                "assertions": [
                    {
                        "id": "VALID-CASE-RUNNER",
                        "passed": False,
                        "detail": f"{type(exc).__name__}: {exc}",
                    }
                ],
                "invocation": None,
                "replay_invocation": None,
                "scenario_invocations": [],
                "worktree": None,
                "safety_ceiling_applied": False,
                "availability_ceiling_applied": False,
            }
        else:
            result_payload = result.as_dict()
        results.append(result_payload)

    mean = sum(float(item["score"]) for item in results) / 6.0
    payload = {
        "schema_version": "1.0",
        "valid_candidate": True,
        "candidate_error": None,
        "setup_results": [item.evidence() for item in prepared.setup_results],
        "case_results": results,
        "mean_score": round(mean, 2),
    }
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    print(json.dumps(payload, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
