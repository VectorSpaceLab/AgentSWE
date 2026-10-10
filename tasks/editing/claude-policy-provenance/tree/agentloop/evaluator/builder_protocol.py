#!/usr/bin/env python3
"""Fail-closed verification of evaluator-attested Builder provenance.

The edit controller must never turn arbitrary patch files into a claimed
Agent-loop run. A real run supplies one evaluator-owned witness for the
continuous Builder connection and content-bound feedback for every revision.
This module only verifies non-secret, content-addressed fields; it does not
create a Builder witness and never treats a synthetic witness as formal evidence.
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from agentloop.protocol import sha256_file


BUILDER_MODEL = "deepseek-flash"
BUILDER_EFFORT = "max"
WITNESS_SCHEMA = "agentswe-builder-session-witness/v1"
FEEDBACK_SCHEMA = "agentswe-edit-feedback/v1"


def _string(value: Any, field: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise RuntimeError(f"Builder provenance field {field!r} is missing")
    return value.strip()


def _read_object(path: Path, label: str) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise RuntimeError(f"{label} is unreadable: {type(exc).__name__}") from exc
    if not isinstance(value, dict):
        raise RuntimeError(f"{label} must be a JSON object")
    return value


def feedback_digest(path: Path) -> str:
    return sha256_file(path.resolve())


def load_and_verify_witness(path: Path, *, expected_submissions: int = 1) -> dict[str, Any]:
    """Verify a Builder witness at a lifecycle boundary.

    One through ten ordered accepted submissions are supported. The first is a
    provisional no-feedback state. Every later submission must bind the latest
    evaluator feedback digest, while all accepted Candidate digests must be
    distinct and belong to one Builder session and connection.
    """
    if not 1 <= expected_submissions <= 10:
        raise ValueError("Builder witness supports between one and ten accepted submissions")
    witness = _read_object(path.resolve(), "Builder witness")
    if witness.get("schema_version") != WITNESS_SCHEMA:
        raise RuntimeError("Builder witness schema is not evaluator-attested")
    if witness.get("model") != BUILDER_MODEL or witness.get("reasoning_effort") != BUILDER_EFFORT:
        raise RuntimeError("Builder model/reasoning lock is invalid")
    if witness.get("single_connection") is not True:
        raise RuntimeError("Builder witness does not prove a single continuous connection")
    if witness.get("transport") != "evaluator-owned-single-session":
        raise RuntimeError("Builder witness transport is not evaluator-owned")
    _string(witness.get("session_id"), "session_id")
    _string(witness.get("connection_id"), "connection_id")
    if witness.get("builder_exit_code") not in (0, None):
        raise RuntimeError("Builder did not exit successfully")
    submissions = witness.get("submissions")
    if not isinstance(submissions, list) or len(submissions) != expected_submissions:
        raise RuntimeError(f"Builder witness must contain exactly {expected_submissions} accepted submissions")
    numbers = [item.get("submission_number") for item in submissions if isinstance(item, dict)]
    if numbers != list(range(1, expected_submissions + 1)):
        raise RuntimeError("Builder submissions must be consecutively ordered")
    for item in submissions:
        if not isinstance(item, dict) or item.get("session_id") != witness["session_id"]:
            raise RuntimeError("Candidate submissions are not bound to the same Builder session")
        if item.get("connection_id") != witness["connection_id"]:
            raise RuntimeError("Candidate submissions are not bound to the same Builder connection")
        _string(item.get("patch"), "submission.patch")
        _string(item.get("candidate_digest"), "submission.candidate_digest")
    digests = [str(item["candidate_digest"]) for item in submissions]
    if len(set(digests)) != len(digests):
        raise RuntimeError("accepted Candidate digests are not distinct")
    if expected_submissions == 1:
        if witness.get("feedback_consumed") not in (None, False):
            raise RuntimeError("provisional Builder witness cannot claim feedback consumption")
        if witness.get("feedback_digest") is not None or witness.get("latest_feedback_digest") is not None:
            raise RuntimeError("provisional Builder witness cannot contain feedback binding")
    else:
        if witness.get("feedback_consumed") is not True:
            raise RuntimeError("Builder witness does not prove feedback consumption")
        _string(witness.get("feedback_digest"), "feedback_digest")
        if witness.get("latest_feedback_digest") != witness.get("feedback_digest"):
            raise RuntimeError("latest revision is not bound to the consumed feedback digest")
        if submissions[-1].get("feedback_digest") != witness.get("feedback_digest"):
            raise RuntimeError("latest submission is not bound to the consumed feedback digest")
        for number, item in enumerate(submissions[1:], 2):
            _string(item.get("feedback_digest"), f"submission_{number}.feedback_digest")
    return witness


def load_and_verify_feedback(path: Path, *, session_id: str, candidate_1_digest: str,
                             connection_id: str | None = None) -> tuple[dict[str, Any], str]:
    resolved = path.resolve()
    feedback = _read_object(resolved, "evaluator feedback")
    if feedback.get("schema_version") != FEEDBACK_SCHEMA:
        raise RuntimeError("feedback record schema is invalid")
    if feedback.get("builder_session_id") != session_id:
        raise RuntimeError("feedback record belongs to a different Builder session")
    if feedback.get("connection_id") is not None and not isinstance(feedback.get("connection_id"), str):
        raise RuntimeError("feedback record connection_id is malformed")
    if connection_id is not None and feedback.get("connection_id") != connection_id:
        raise RuntimeError("feedback record belongs to a different Builder connection")
    bound_digest = feedback.get("candidate_digest", feedback.get("candidate_1_digest"))
    if bound_digest != candidate_1_digest:
        raise RuntimeError("feedback is not bound to the preceding materialized Candidate")
    digest = feedback_digest(resolved)
    declared = feedback.get("feedback_digest")
    if declared is not None and declared != digest:
        raise RuntimeError("feedback record digest does not match its bytes")
    feedback["feedback_digest"] = digest
    return feedback, digest


def verify_submission_binding(witness: dict[str, Any], *, number: int, patch: Path, candidate_digest: str) -> None:
    submissions = witness.get("submissions")
    if not 1 <= number <= 10 or not isinstance(submissions, list) or len(submissions) < number:
        raise RuntimeError(f"Builder witness has no Candidate {number} submission")
    item = submissions[number - 1]
    if not isinstance(item, dict):
        raise RuntimeError("Builder submission record is malformed")
    recorded_patch = Path(_string(item.get("patch"), "submission.patch")).resolve()
    if recorded_patch != patch.resolve():
        raise RuntimeError(f"Candidate {number} patch is not the patch attested by the Builder")
    if item.get("candidate_digest") != candidate_digest:
        raise RuntimeError(f"Candidate {number} digest does not match the Builder witness")
