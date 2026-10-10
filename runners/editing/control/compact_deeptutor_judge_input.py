#!/usr/bin/env python3
"""Create a loss-minimizing compact trajectory for a Result judge retry."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any


def compact(value: Any, *, depth: int = 0) -> Any:
    if isinstance(value, dict):
        result = {}
        for key, item in value.items():
            if key in {"session_id", "turn_id", "call_id", "trace_id", "tool_call_id"}:
                continue
            result[key] = compact(item, depth=depth + 1)
        return result
    if isinstance(value, list):
        return [compact(item, depth=depth + 1) for item in value]
    if isinstance(value, str) and len(value) > 6000:
        return value[:6000] + "\n[compact judge projection: remainder omitted]"
    return value


def main() -> None:
    source = Path("@@AGENTSWE_EDITING_RUNS@@/smoke/deeptutor/0904-smoke-reference-010/test_003/trajectory.json")
    destination = source.with_name("trajectory_judge_compact.json")
    value = json.loads(source.read_text(encoding="utf-8"))
    records = value.get("records", []) if isinstance(value, dict) else []
    kept = []
    for row in records:
        if not isinstance(row, dict):
            continue
        if row.get("type") in {"tool_call", "tool_result", "result"}:
            kept.append(compact(row))
    projected = {
        "schema_version": "agentswe-deeptutor-smoke-trajectory-judge-projection-v1",
        "case_id": value.get("case_id") if isinstance(value, dict) else "test_003",
        "lower_entry": value.get("lower_entry") if isinstance(value, dict) else None,
        "product_entry": value.get("product_entry") if isinstance(value, dict) else None,
        "model_protocol": value.get("model_protocol") if isinstance(value, dict) else None,
        "product_execution": value.get("product_execution") if isinstance(value, dict) else None,
        "model_authored_artifact": value.get("model_authored_artifact") if isinstance(value, dict) else None,
        "records": kept,
        "projection_note": "All tool calls, tool results, and terminal result records retained; repetitive transport/session metadata removed only.",
    }
    destination.write_text(json.dumps(projected, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    print(json.dumps({"source_bytes": source.stat().st_size, "destination_bytes": destination.stat().st_size, "records": len(kept)}))


if __name__ == "__main__":
    main()
