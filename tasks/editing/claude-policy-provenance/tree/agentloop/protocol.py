#!/usr/bin/env python3
"""Small shared helpers for the Claude policy-provenance Agent-loop adapter."""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
from typing import Any


MODEL = "deepseek-flash"
REASONING_EFFORT = "high"
RESULT_SCHEMA = "agentswe-claude-policy-agent-result/v1"
CANDIDATE_IGNORED_NAMES = {".git", "__pycache__", ".evaluator-pyc", ".agentloop_build.json"}


def write_json(path: Path, value: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(value, indent=2, ensure_ascii=False, sort_keys=True) + "\n", encoding="utf-8")
    os.replace(temporary, path)


def read_json(path: Path) -> dict[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError(f"expected JSON object: {path}")
    return value


def tree_digest(root: Path, *, ignore: set[str] | None = None) -> str:
    """Deterministic digest including relative names, type, and file bytes."""
    ignored = ignore or {".git", "__pycache__", ".evaluator-pyc"}
    digest = hashlib.sha256()
    for path in sorted(root.rglob("*"), key=lambda item: item.relative_to(root).as_posix()):
        relative = path.relative_to(root)
        if any(part in ignored for part in relative.parts):
            continue
        name = relative.as_posix().encode()
        digest.update(len(name).to_bytes(8, "big")); digest.update(name)
        if path.is_symlink():
            kind, payload = b"L", os.readlink(path).encode()
        elif path.is_file():
            kind, payload = b"F", path.read_bytes()
        else:
            kind, payload = b"D", b""
        digest.update(kind); digest.update(len(payload).to_bytes(8, "big")); digest.update(payload)
    return digest.hexdigest()


def candidate_tree_digest(root: Path) -> str:
    """Digest the candidate product, excluding evaluator-generated metadata."""
    return tree_digest(root, ignore=CANDIDATE_IGNORED_NAMES)


def assert_regular_tree(root: Path) -> None:
    """Reject symlinks and special files before a Candidate is frozen."""
    for path in root.rglob("*"):
        if path.is_symlink() or (not path.is_file() and not path.is_dir()):
            raise ValueError(f"candidate tree is not regular-file-only: {path}")


def sha256_file(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()
