#!/usr/bin/env python3
"""Emit the starter declarative OSWorld screenshot agent."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from desktop_policy import (
    INSTRUCTIONS,
    PLANNING_GUIDANCE,
    RECOVERY_GUIDANCE,
    VERIFICATION_GUIDANCE,
)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--input", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--run-dir", type=Path, required=True)
    args = parser.parse_args()
    args.run_dir.mkdir(parents=True, exist_ok=True)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    rows: list[dict] = []
    errors: list[str] = []
    try:
        for line_number, line in enumerate(args.input.read_text(encoding="utf-8").splitlines(), 1):
            if not line.strip():
                continue
            item = json.loads(line)
            case_id = item.get("id") if isinstance(item, dict) else None
            if not isinstance(case_id, str) or not case_id:
                raise ValueError(f"line {line_number}: missing id")
            rows.append({
                "id": case_id,
                "agent": {
                    "kind": "osworld_screenshot_react",
                    "instructions": INSTRUCTIONS,
                    "planning_guidance": PLANNING_GUIDANCE,
                    "recovery_guidance": RECOVERY_GUIDANCE,
                    "verification_guidance": VERIFICATION_GUIDANCE,
                },
            })
        args.output.write_text(
            "".join(json.dumps(row, ensure_ascii=False, sort_keys=True) + "\n" for row in rows),
            encoding="utf-8",
        )
        report = {
            "status": "success", "predictions": str(args.output),
            "rows_read": len(rows), "rows_written": len(rows), "errors": errors,
            "usage": {"model_calls": 0, "model_tokens": 0},
        }
        (args.run_dir / "run_report.json").write_text(
            json.dumps(report, indent=2, sort_keys=True) + "\n", encoding="utf-8"
        )
        return 0
    except Exception as exc:
        errors.append(f"{type(exc).__name__}: {exc}")
        report = {"status": "error", "rows_written": 0, "errors": errors}
        (args.run_dir / "run_report.json").write_text(
            json.dumps(report, indent=2, sort_keys=True) + "\n", encoding="utf-8"
        )
        return 1


if __name__ == "__main__":
    raise SystemExit(main())

