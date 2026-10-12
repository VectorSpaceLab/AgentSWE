#!/usr/bin/env python3
"""Clean live Terminus-2 prediction wrapper."""
from __future__ import annotations

import argparse
import json
from pathlib import Path

from agent_policy import prediction_for


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--input", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--run-dir", type=Path, required=True)
    args = parser.parse_args()
    rows = []
    for line in args.input.read_text(encoding="utf-8").splitlines():
        if line.strip():
            row = json.loads(line)
            rows.append(prediction_for(row))
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.run_dir.mkdir(parents=True, exist_ok=True)
    args.output.write_text("".join(json.dumps(row) + "\n" for row in rows), encoding="utf-8")
    (args.run_dir / "run_report.json").write_text(json.dumps({"status": "ok", "artifact_paths": [str(args.output)], "errors": [], "usage": {}}, indent=2) + "\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
