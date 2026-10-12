#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
from pathlib import Path
import statistics
import sys

from benchmark_harness import HarnessError, execute_case, prepare_submission
from public_harness import MEMORY_LIMIT_BYTES, SOURCE_TREE_SHA256, snapshot_source_hash


def main() -> int:
    parser = argparse.ArgumentParser(description="Run all isolated hidden claim-gate scenarios")
    parser.add_argument("--submission", type=Path, required=True)
    parser.add_argument("--work-dir", type=Path, required=True)
    parser.add_argument("--result", type=Path)
    args = parser.parse_args()
    args.work_dir.mkdir(parents=True, exist_ok=True)
    try:
        prepared = prepare_submission(args.submission.resolve(), args.work_dir.resolve())
    except HarnessError as exc:
        payload = {"valid": False, "failure": str(exc), "scores": [0, 0, 0, 0, 0, 0], "mean": 0.0, "cases": []}
        print(json.dumps(payload, indent=2, sort_keys=True))
        if args.result:
            args.result.parent.mkdir(parents=True, exist_ok=True)
            args.result.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
        return 1

    results = []
    for index in range(1, 7):
        case_id = f"test_{index:03d}"
        try:
            results.append(execute_case(prepared, case_id, args.work_dir.resolve()))
        except Exception as exc:
            results.append({"case_id": case_id, "score": 0, "valid": False, "failure": f"isolated harness exception: {type(exc).__name__}: {exc}"})
    scores = [float(result.get("score", 0)) for result in results]
    suite_valid = all(result.get("valid") is True for result in results)
    runtimes = [float(result.get("runtime_seconds", 0)) for result in results if result.get("valid")]
    rss_values = [int(result.get("peak_memory_bytes", 0)) for result in results if result.get("valid")]
    payload = {
        "valid": suite_valid,
        "scores": scores,
        "mean": round(statistics.fmean(scores), 2),
        "cases": results,
        "preflight": {"delivery_schema": "1.0", "source_tree_hash": snapshot_source_hash(), "source_tree_hash_matches": snapshot_source_hash() == SOURCE_TREE_SHA256, "public_hidden_case_count": [2, 6]},
        "resource_headroom": {"max_runtime_seconds": max(runtimes, default=0), "case_timeout_seconds": 600, "runtime_headroom_seconds": round(600 - max(runtimes, default=0), 4), "max_peak_memory_bytes": max(rss_values, default=0), "rss_limit_bytes": MEMORY_LIMIT_BYTES, "rss_headroom_bytes": MEMORY_LIMIT_BYTES - max(rss_values, default=0)},
    }
    rendered = json.dumps(payload, indent=2, sort_keys=True)
    print(rendered)
    if args.result:
        args.result.parent.mkdir(parents=True, exist_ok=True)
        args.result.write_text(rendered + "\n", encoding="utf-8")
    return 0 if suite_valid else 1


if __name__ == "__main__":
    sys.exit(main())
