#!/usr/bin/env python3
"""Shared, dependency-free helpers for evaluator protocol v1."""

from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path
from typing import Any


PROTOCOL_VERSION = "1.0.0"
CASE_RE = re.compile(r"(?:dev|test)_00[1-6]")


def read_json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def write_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def canonical_digest(value: Any) -> str:
    encoded = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def load_protocol(evaluator_dir: Path) -> dict[str, Any]:
    return read_json(evaluator_dir / "protocol" / "v1" / "protocol.json")


def load_oracle(evaluator_dir: Path) -> dict[str, Any]:
    return read_json(evaluator_dir / "frozen_evidence" / "v1" / "oracle.json")


def case_id_from_input(path: Path) -> str:
    for parent in (path.parent, *path.parents):
        if CASE_RE.fullmatch(parent.name):
            return parent.name
    raise ValueError(f"cannot derive test case id from {path}")


def compact_error(exc: BaseException) -> str:
    return re.sub(r"\s+", " ", f"{type(exc).__name__}: {exc}").strip()[:1000]
