#!/usr/bin/env python3
"""Validate the shape of a Builder/Repair artifact before dev evaluation."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path


TRANSIENT_PARTS = {"__pycache__", ".pytest_cache", ".mypy_cache"}
TRANSIENT_SUFFIXES = {".pyc", ".pyo"}


def tree_digest(root: Path) -> str:
    digest = hashlib.sha256()
    for path in sorted(root.rglob("*"), key=lambda p: p.relative_to(root).as_posix()):
        relative_path = path.relative_to(root)
        if any(part in TRANSIENT_PARTS for part in relative_path.parts):
            continue
        if relative_path.suffix in TRANSIENT_SUFFIXES:
            continue
        relative = relative_path.as_posix().encode()
        if path.is_symlink():
            kind, payload = b"L", os.readlink(path).encode()
        elif path.is_file():
            digest.update(b"F" + len(relative).to_bytes(8, "big") + relative)
            digest.update(path.stat().st_size.to_bytes(8, "big"))
            with path.open("rb") as handle:
                while chunk := handle.read(1024 * 1024):
                    digest.update(chunk)
            continue
        elif path.is_dir():
            continue
        else:
            kind, payload = b"O", b""
        digest.update(kind + len(relative).to_bytes(8, "big") + relative)
        digest.update(len(payload).to_bytes(8, "big") + payload)
    return digest.hexdigest()


def write(path: Path, value: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--submission", type=Path, required=True)
    parser.add_argument("--verifier-dir", type=Path, required=True)
    args = parser.parse_args()
    errors: list[str] = []
    entry = args.submission / "run_agent.py"
    if not args.submission.is_dir():
        errors.append("submission directory missing")
    if not entry.is_file() or entry.is_symlink():
        errors.append("run_agent.py missing or not a regular file")
    elif entry.stat().st_size == 0:
        errors.append("run_agent.py is empty")
    forbidden_names = {
        "test_cases",
        "evaluator",
        "rubric",
        "rubric.md",
        "meta",
        "submissions",
        ".env",
    }
    if args.submission.is_dir():
        for path in args.submission.rglob("*"):
            relative = path.relative_to(args.submission)
            forbidden = next(
                (part for part in relative.parts if part in forbidden_names), None
            )
            if forbidden is not None:
                errors.append(
                    f"forbidden path component copied into submission: {relative}"
                )
            if path.is_symlink():
                errors.append(f"symbolic links are forbidden: {relative}")
            elif not path.is_file() and not path.is_dir():
                errors.append(f"special filesystem object is forbidden: {relative}")
    contract = {
        "schema_version": "1.0",
        "valid": not errors,
        "errors": errors,
        "submission_digest": tree_digest(args.submission) if args.submission.is_dir() else None,
        "entrypoint": "run_agent.py",
    }
    write(args.verifier_dir / "builder_contract.json", contract)
    write(args.verifier_dir / "reward.json", {"reward": 1.0 if not errors else 0.0})
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
