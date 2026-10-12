#!/usr/bin/env python3
"""Clean, reusable Search ReAct starter for BrowseComp optimization."""
from __future__ import annotations

import argparse
import json
import os
import re
import time
import urllib.request
from pathlib import Path

from agent.realtime_react import answer


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--input", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--run-dir", type=Path, required=True)
    args = parser.parse_args()
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.run_dir.mkdir(parents=True, exist_ok=True)
    rows = []
    errors = []
    usage = {"model_calls": 0, "search_calls": 0, "visit_calls": 0}
    for raw in args.input.read_text(encoding="utf-8").splitlines():
        if not raw.strip():
            continue
        item = json.loads(raw)
        try:
            response, row_usage = answer(item)
            for key in usage:
                usage[key] += int(row_usage.get(key, 0))
            rows.append({"id": item.get("id"), "response": response, "usage": row_usage})
        except Exception as exc:  # evaluator classifies missing/invalid row as agent failure
            errors.append(f"{item.get('id')}: {type(exc).__name__}")
    args.output.write_text(
        "".join(json.dumps(row, ensure_ascii=False) + "\n" for row in rows),
        encoding="utf-8",
    )
    (args.run_dir / "run_report.json").write_text(
        json.dumps({"status": "ok" if not errors else "partial", "artifact_paths": [str(args.output)], "errors": errors, "usage": usage}, indent=2) + "\n",
        encoding="utf-8",
    )
    return 0 if not errors else 75


if __name__ == "__main__":
    raise SystemExit(main())
