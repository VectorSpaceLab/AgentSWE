#!/usr/bin/env python3
"""Apply, build, and run exactly one public development scenario."""

from __future__ import annotations

import json
import sys
from pathlib import Path

from harness_support import HarnessError, evaluate_case, prepare_candidate, public_parser


def main() -> int:
    args = public_parser().parse_args()
    benchmark_root = Path(__file__).resolve().parents[1]
    args.work_dir.mkdir(parents=True, exist_ok=True)
    try:
        prepared = prepare_candidate(
            benchmark_root,
            args.candidate.resolve(),
            args.work_dir.resolve(),
            install=not args.skip_install,
        )
        result = evaluate_case(
            prepared,
            benchmark_root / "dev_cases" / args.case,
            args.work_dir / args.case,
            keep_work=args.keep_work,
        )
    except HarnessError as exc:
        print(json.dumps({"valid": False, "score": 0, "error": str(exc)}, indent=2))
        return 2
    print(json.dumps(result.as_dict(), indent=2, sort_keys=True))
    for assertion in result.assertions:
        marker = "PASS" if assertion.passed else "FAIL"
        print(f"[{marker}] {assertion.assertion_id}: {assertion.detail}", file=sys.stderr)
    return 0 if result.valid else 2


if __name__ == "__main__":
    raise SystemExit(main())
