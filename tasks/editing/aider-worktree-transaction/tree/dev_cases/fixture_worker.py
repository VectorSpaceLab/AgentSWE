#!/usr/bin/env python3
"""Deterministic worker/test process used by the public development cases."""
from __future__ import annotations

import argparse
import base64
import fcntl
import hashlib
import json
import os
import stat
import sys
import time
from pathlib import Path
from typing import Any


def append_event(kind: str, detail: dict[str, Any]) -> None:
    log = Path(os.environ["AIDER_COMMAND_LOG"])
    log.parent.mkdir(parents=True, exist_ok=True)
    lock = log.with_suffix(".lock")
    with lock.open("a+") as guard:
        fcntl.flock(guard, fcntl.LOCK_EX)
        active_path = log.with_suffix(".active.json")
        try:
            active = json.loads(active_path.read_text()) if active_path.exists() else {"count": 0, "peak": 0}
        except (OSError, json.JSONDecodeError):
            active = {"count": 0, "peak": 0}
        if kind == "worker_start":
            active["count"] += 1
            active["peak"] = max(active["peak"], active["count"])
        elif kind == "worker_end":
            active["count"] = max(0, active["count"] - 1)
        active_path.write_text(json.dumps(active, sort_keys=True) + "\n")
        record = {
            "kind": kind,
            "plan_id": os.environ.get("AIDER_PLAN_ID"),
            "subtask_id": os.environ.get("AIDER_SUBTASK_ID"),
            "repository_id": os.environ.get("AIDER_REPOSITORY_ID"),
            "cwd": str(Path.cwd()),
            "detail": detail,
        }
        with log.open("a", encoding="utf-8") as stream:
            stream.write(json.dumps(record, sort_keys=True) + "\n")


def safe_path(relative: str) -> Path:
    path = Path(relative)
    if path.is_absolute() or ".." in path.parts:
        raise ValueError(f"fixture path is not repository relative: {relative!r}")
    return Path.cwd() / path


def run_actions(path: Path) -> int:
    spec = json.loads(path.read_text(encoding="utf-8"))
    append_event("worker_start", {"fixture_sha256": hashlib.sha256(path.read_bytes()).hexdigest()})
    try:
        delay = float(spec.get("delay_seconds", 0))
        if delay:
            time.sleep(delay)
        for action in spec.get("actions", []):
            kind = action["op"]
            target = safe_path(action["path"])
            if kind in {"write", "append"}:
                target.parent.mkdir(parents=True, exist_ok=True)
                mode = "a" if kind == "append" else "w"
                with target.open(mode, encoding="utf-8", newline="") as stream:
                    stream.write(action["content"])
            elif kind == "write_base64":
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_bytes(base64.b64decode(action["content"], validate=True))
            elif kind == "delete":
                target.unlink()
            elif kind == "rename":
                destination = safe_path(action["to"])
                destination.parent.mkdir(parents=True, exist_ok=True)
                target.rename(destination)
            elif kind == "chmod":
                bits = target.stat().st_mode
                if action["executable"]:
                    target.chmod(bits | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
                else:
                    target.chmod(bits & ~0o111)
            elif kind == "symlink":
                link_target = Path(action["target"])
                if link_target.is_absolute() or ".." in link_target.parts:
                    raise ValueError("fixture symlink target must remain relative")
                target.parent.mkdir(parents=True, exist_ok=True)
                target.symlink_to(action["target"])
            else:
                raise ValueError(f"unknown action {kind!r}")
        return int(spec.get("exit_code", 0))
    finally:
        append_event("worker_end", {})


def check_assertion(item: dict[str, Any]) -> str | None:
    path = safe_path(item["path"])
    kind = item["op"]
    if kind == "exists" and not path.exists():
        return f"missing {item['path']}"
    if kind == "absent" and path.exists():
        return f"unexpected {item['path']}"
    if kind == "contains" and (not path.is_file() or item["content"] not in path.read_text(encoding="utf-8")):
        return f"content mismatch {item['path']}"
    if kind == "executable" and (not path.exists() or not (path.stat().st_mode & 0o111)):
        return f"not executable {item['path']}"
    if kind == "symlink" and (not path.is_symlink() or os.readlink(path) != item["target"]):
        return f"symlink mismatch {item['path']}"
    if kind == "sha256" and (not path.is_file() or hashlib.sha256(path.read_bytes()).hexdigest() != item["digest"]):
        return f"digest mismatch {item['path']}"
    return None


def run_test(path: Path) -> int:
    spec = json.loads(path.read_text(encoding="utf-8"))
    failures = [failure for item in spec.get("assertions", []) if (failure := check_assertion(item))]
    configured = int(spec.get("exit_code", 0))
    code = configured or (1 if failures else 0)
    append_event("test", {"fixture": path.name, "exit_code": code, "failures": failures})
    if failures:
        print("; ".join(failures), file=sys.stderr)
    print(spec.get("stdout", "fixture test complete"))
    return code


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("mode", choices=("worker", "test"))
    parser.add_argument("spec", type=Path)
    args = parser.parse_args()
    return run_actions(args.spec) if args.mode == "worker" else run_test(args.spec)


if __name__ == "__main__":
    raise SystemExit(main())
