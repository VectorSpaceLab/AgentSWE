#!/usr/bin/env python3
"""Canonical hidden Agent-loop runner; hidden is impossible before freeze."""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
from datetime import datetime, timezone

if __package__ in {None, ""}:
    from case_specs import HIDDEN_CASES
    from hidden_runner import load_freeze, run
else:
    from .case_specs import HIDDEN_CASES
    from .hidden_runner import load_freeze, run


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--freeze-manifest", type=Path, required=True)
    parser.add_argument("--hidden-root", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--broker-endpoint", required=True)
    parser.add_argument("--python", dest="python_executable", type=Path)
    parser.add_argument("--case", action="append", dest="cases")
    args = parser.parse_args(argv)
    freeze, _candidate, _digest, _frozen_at = load_freeze(
        args.freeze_manifest.resolve()
    )
    selected = args.cases or list(HIDDEN_CASES)
    args.output.mkdir(parents=True, exist_ok=True)
    results = []
    errors = []
    started_at = datetime.now(timezone.utc).isoformat()
    for case_id in selected:
        if case_id not in HIDDEN_CASES:
            raise SystemExit(f"unknown hidden case: {case_id}")
        try:
            results.append(run(
                frozen_manifest=args.freeze_manifest,
                hidden_root=args.hidden_root,
                case_id=case_id,
                output=args.output / case_id,
                broker_endpoint=args.broker_endpoint,
                python_executable=args.python_executable,
            ))
        except Exception as exc:
            error = {"case_id": case_id, "classification": "evaluator_infrastructure_error",
                     "error_type": type(exc).__name__, "error": str(exc)}
            errors.append(error)
            results.append(error)
    case_results = [item for item in results if item.get("schema_version") == "deepcode-agentloop-hidden-run-v2"]
    summary = {"schema_version": "deepcode-agentloop-hidden-suite-v2", "hidden_started_after_freeze": len(case_results) == 6 and all(item.get("hidden_started_after_freeze") is True for item in case_results),
               "cases": results, "count": len(results), "case_inventory": selected,
               "formal_result_claimed": False, "scheduled_records_used_as_results": False,
               "all_six_dispatched": selected == list(HIDDEN_CASES) and len(results) == 6,
               "all_six_have_result_json": len(case_results) == 6 and not errors and all(Path(str(item["evidence"]["result"])).is_file() for item in case_results),
               "frozen_digest_stable": len(case_results) == 6 and not errors and all(item.get("frozen_digest_stable") is True for item in case_results),
               "infrastructure_errors": errors,
               "finished_at": datetime.now(timezone.utc).isoformat()}
    summary["complete"] = bool(summary["all_six_dispatched"] and summary["hidden_started_after_freeze"] and summary["all_six_have_result_json"] and summary["frozen_digest_stable"])
    (args.output / "summary.json").write_text(json.dumps(summary, indent=2) + "\n", encoding="utf-8")
    (args.output / "hidden-after-freeze-attestation.json").write_text(json.dumps({
        "schema_version": "deepcode-agentloop-hidden-after-freeze-attestation/v2",
        "formal_result_claimed": False,
        "result_or_score_claimed": False,
        "freeze_manifest": str(args.freeze_manifest.resolve()),
        "source_submission": freeze["source_submission"],
        "case_inventory": selected,
        "executed_cases": [item.get("case_id") for item in results],
        "started_at": started_at,
        "finished_at": summary["finished_at"],
        "hidden_started_after_freeze": summary["hidden_started_after_freeze"],
        "scheduled_records_used_as_results": False,
        "all_six_dispatched": summary["all_six_dispatched"],
        "all_six_have_result_json": summary["all_six_have_result_json"],
        "frozen_digest_stable": summary["frozen_digest_stable"],
        "cases": results,
        "infrastructure_errors": errors,
        "complete": summary["complete"],
    }, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(summary, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
