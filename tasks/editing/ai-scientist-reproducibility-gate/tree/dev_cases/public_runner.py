from __future__ import annotations

import argparse
import json
from pathlib import Path

from public_harness import BENCHMARK_ROOT, HarnessError, execute_case, prepare_submission
from public_specs import SPECS


def main() -> int:
    parser = argparse.ArgumentParser(description="Run one public release-gate scenario, including attestation and notification outbox checks")
    parser.add_argument("--submission", type=Path, required=True)
    parser.add_argument("--case", choices=tuple(SPECS), required=True)
    parser.add_argument("--work-dir", type=Path, required=True)
    args = parser.parse_args()
    work_dir = args.work_dir.resolve()
    work_dir.mkdir(parents=True, exist_ok=True)
    try:
        prepared = prepare_submission(args.submission.resolve(), work_dir)
        result = execute_case(
            prepared,
            SPECS[args.case],
            BENCHMARK_ROOT / "dev_cases" / args.case,
            work_dir,
            timeout=120,
        )
    except HarnessError as exc:
        result = {"case_id": args.case, "score": 0, "valid": False, "failure": str(exc)}
    print(json.dumps(result, indent=2, sort_keys=True))
    return 0 if result.get("valid") else 1
