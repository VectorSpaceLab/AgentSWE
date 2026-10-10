from __future__ import annotations

import argparse
import json
import shutil
import sys
from pathlib import Path

if __package__ in {None, ""}:
    sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
    from evaluator.harness.case_specs import PUBLIC_CASES
    from evaluator.harness.common import HarnessError, prepare_candidate, run_build_gates
    from evaluator.harness.evaluate import evaluate_case
else:
    from .case_specs import PUBLIC_CASES
    from .common import HarnessError, prepare_candidate, run_build_gates
    from .evaluate import evaluate_case


def main() -> int:
    parser = argparse.ArgumentParser(description="Run one public DeepCode traceability case")
    parser.add_argument("--submission", type=Path, required=True)
    parser.add_argument("--case", choices=PUBLIC_CASES, required=True)
    parser.add_argument("--json-output", type=Path)
    parser.add_argument("--keep-worktree", action="store_true")
    args = parser.parse_args()
    prepared = None
    try:
        prepared = prepare_candidate(args.submission)
        gates = run_build_gates(prepared.repository)
        result = evaluate_case(prepared.repository, args.case, build_gates_passed=True)
        result["build_gates"] = [gate.to_json() for gate in gates]
        result["changed_paths"] = prepared.changed_paths
    except Exception as exc:
        result = {
            "case_id": args.case,
            "score": 0,
            "valid_artifact": False,
            "validity_error": f"{type(exc).__name__}: {exc}",
            "assertions": [],
            "dimensions": {
                "preserved_behavior": 0,
                "revision_review": 0,
                "resumable_execution": 0,
                "cross_surface": 0,
                "isolation_compatibility": 0,
                "operational_surfaces": 0,
            },
            "major_errors": [str(exc)],
        }
    payload = json.dumps(result, indent=2, sort_keys=True) + "\n"
    if args.json_output:
        args.json_output.parent.mkdir(parents=True, exist_ok=True)
        args.json_output.write_text(payload, encoding="utf-8")
    print(payload, end="")
    if prepared is not None and not args.keep_worktree:
        shutil.rmtree(prepared.workspace, ignore_errors=True)
    return 0 if result.get("score") == 100 else 1


if __name__ == "__main__":
    raise SystemExit(main())
