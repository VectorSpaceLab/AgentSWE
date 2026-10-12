#!/usr/bin/env python3
"""Small, offline-verifiable protocol helpers for the Dyad Builder session."""
from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any

BUILDER_MODEL = "deepseek-flash"
BUILDER_EFFORT = "max"
LOWER_MODEL = "deepseek-flash"
LOWER_EFFORT = "high"
PLACEHOLDER = "broker-only-placeholder"
DEV_CASES = ("dev_001", "dev_002")
HIDDEN_CASES = tuple(f"test_{index:03d}" for index in range(1, 7))


def sha256_file(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def tree_digest(root: Path) -> str:
    digest = hashlib.sha256()
    for path in sorted(root.rglob("*"), key=lambda item: item.relative_to(root).as_posix()):
        relative = path.relative_to(root).as_posix().encode("utf-8")
        digest.update(len(relative).to_bytes(8, "big"))
        digest.update(relative)
        if path.is_symlink():
            kind, payload = b"L", path.readlink().as_posix().encode("utf-8")
        elif path.is_file():
            kind, payload = b"F", path.read_bytes()
        else:
            kind, payload = b"D", b""
        digest.update(kind)
        digest.update(len(payload).to_bytes(8, "big"))
        digest.update(payload)
    return digest.hexdigest()


def write_json(path: Path, value: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")


def feedback_digest(path: Path) -> str:
    return sha256_file(path)


def load_and_verify_witness(path: Path, *, expected_submissions: int) -> dict[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError("Builder witness must be an object")
    required = {
        "schema_version", "model", "reasoning_effort", "single_connection",
        "transport", "session_id", "connection_id", "feedback_consumed",
        "feedback_digest", "candidate_2_feedback_digest", "submissions",
    }
    if not required <= set(value):
        raise ValueError("Builder witness is incomplete")
    if value["schema_version"] != "agentswe-builder-session-witness/v1":
        raise ValueError("wrong Builder witness schema")
    if value["model"] != BUILDER_MODEL or value["reasoning_effort"] != BUILDER_EFFORT:
        raise ValueError("Builder model or reasoning effort is not locked")
    if value["single_connection"] is not True or not value["session_id"] or not value["connection_id"]:
        raise ValueError("Builder session/connection binding is incomplete")
    submissions = value["submissions"]
    if not isinstance(submissions, list) or len(submissions) != expected_submissions:
        raise ValueError("unexpected Builder submission count")
    for index, item in enumerate(submissions, start=1):
        if not isinstance(item, dict) or item.get("submission_number") != index:
            raise ValueError("Builder submissions are not ordered")
        if item.get("session_id") != value["session_id"] or item.get("connection_id") != value["connection_id"]:
            raise ValueError("submission escaped the Builder connection")
        if not isinstance(item.get("candidate_digest"), str) or len(item["candidate_digest"]) != 64:
            raise ValueError("submission candidate digest is missing")
    if expected_submissions == 2:
        if value["feedback_consumed"] is not True:
            raise ValueError("Candidate 2 feedback consumption is not attested")
        if not isinstance(value["feedback_digest"], str) or len(value["feedback_digest"]) != 64:
            raise ValueError("feedback digest is missing")
        if value["candidate_2_feedback_digest"] != value["feedback_digest"]:
            raise ValueError("Candidate 2 feedback binding is inconsistent")
        if submissions[0]["candidate_digest"] == submissions[1]["candidate_digest"]:
            raise ValueError("Candidate digests must differ")
        if submissions[1].get("feedback_digest") != value["feedback_digest"]:
            raise ValueError("Candidate 2 does not cite the consumed feedback")
    return value

