#!/usr/bin/env python3
from __future__ import annotations
import argparse, os, shutil, time
from pathlib import Path
from common import BASELINE_INSTALL, ensure_npm, extract_pristine, npm_env, run, sha256, HarnessError

def main() -> int:
    parser = argparse.ArgumentParser(description="Prewarm the task-unique OpenHands npm tree")
    parser.add_argument("--repository", type=Path, required=True)
    args = parser.parse_args()
    log = []
    try:
        npm, log = ensure_npm()
        log += extract_pristine(args.repository.resolve(), BASELINE_INSTALL)
        marker = BASELINE_INSTALL / ".benchmark-lock-sha256"
        marker.unlink(missing_ok=True)
        for attempt in range(1, 4):
            cleanup_started = time.monotonic()
            shutil.rmtree(BASELINE_INSTALL / "node_modules", ignore_errors=True)
            log.append({"command": "remove partial node_modules", "cwd": str(BASELINE_INSTALL), "exit_code": 0,
                        "duration_seconds": round(time.monotonic() - cleanup_started, 3),
                        "stdout": "", "stderr": "", "attempt": attempt})
            result = run([*npm, "ci", "--ignore-scripts", "--no-audit", "--no-fund"], BASELINE_INSTALL, timeout=600, env=npm_env())
            log.append({**result.evidence(), "attempt": attempt})
            if result.exit_code == 0: break
        else: raise HarnessError("baseline npm ci failed after three attempts")
        marker_tmp = marker.with_suffix(".tmp")
        marker_tmp.write_text(sha256(BASELINE_INSTALL / "package-lock.json") + "\n")
        os.replace(marker_tmp, marker)
        print(__import__("json").dumps({"valid": True, "commands": log}, indent=2)); return 0
    except Exception as exc:
        print(__import__("json").dumps({"valid": False, "commands": log, "error": str(exc)}, indent=2)); return 1
if __name__ == "__main__": raise SystemExit(main())
