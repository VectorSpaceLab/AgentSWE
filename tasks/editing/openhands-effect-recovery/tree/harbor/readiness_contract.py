"""OpenHands task-local readiness contract.

This module is deliberately side-effect free.  It validates the reduced v2
profile before a runner can start a provider-backed process; it never writes
the shared registry or gate and never starts a broker.
"""
from __future__ import annotations

import hashlib
import json
from pathlib import Path

PROFILE = "single-dev-two-round-hidden-smoke-v1"
PUBLIC_CASES = ("dev_001",)
HIDDEN_CASES = ("test_001",)
REQUIRED_VALID_ROUNDS = 2
MODEL = "deepseek-flash"
EFFORT = "max"
FILES = ("solution.patch", "edit_report.json", "run_report.json")


def sha256(path: Path) -> str:
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def profile() -> dict:
    return {
        "profile": PROFILE,
        "public_cases": list(PUBLIC_CASES),
        "hidden_smoke_cases": list(HIDDEN_CASES),
        "required_valid_rounds": REQUIRED_VALID_ROUNDS,
        "require_same_builder_session": True,
        "require_exact_feedback_digest": True,
        "require_distinct_candidate_digest": True,
        "freeze_candidate": "latest_valid_revision",
        "require_result_judge_smoke": True,
        "require_code_judge_smoke": True,
        "require_complete_usage_ledger": True,
        "require_cleanup_attestation": True,
        "score_threshold": None,
    }


def validate_protocol_lock(lock: dict) -> list[str]:
    errors: list[str] = []
    if lock.get("profile") != PROFILE:
        errors.append("profile must be single-dev-two-round-hidden-smoke-v1")
    if lock.get("public_cases") != list(PUBLIC_CASES):
        errors.append("public inventory must contain only dev_001")
    if lock.get("hidden_cases") != list(HIDDEN_CASES):
        errors.append("hidden inventory must contain only test_001")
    if lock.get("required_valid_rounds") != REQUIRED_VALID_ROUNDS:
        errors.append("exactly two valid rounds are required")
    builder = lock.get("builder", {})
    if builder.get("model") != MODEL or builder.get("effort", builder.get("reasoning_effort")) != EFFORT:
        errors.append("Builder model/effort is not pinned")
    if builder.get("single_uninterrupted_turn", builder.get("single_continuous_session")) is not True:
        errors.append("Builder must use one uninterrupted native session")
    if lock.get("score_threshold") is not None:
        errors.append("readiness must not use a score threshold")
    if lock.get("formal_result_publishable") is True or lock.get("code_score_publishable") is True:
        errors.append("readiness smoke cannot publish formal scores")
    return errors


def validate_submission(submission: Path, *, number: int, previous_candidate_digest: str | None = None,
                        previous_feedback_digest: str | None = None) -> list[str]:
    """Validate the Builder's three-file delivery and revision metadata."""
    from adapters.materialize_candidate import validate as base_validate
    errors = list(base_validate(Path(submission)))
    path = Path(submission)
    if (not path.is_dir()) or set(p.name for p in path.iterdir()) != set(FILES):
        errors.append("submission must contain exactly solution.patch, edit_report.json, run_report.json")
    if not path.is_dir():
        return errors
    try:
        report = json.loads((path / "run_report.json").read_bytes())
    except Exception:
        return errors
    if not isinstance(report, dict):
        errors.append("run_report.json must be an object")
        return errors
    expected = {
        "submission_number": number,
        "revision_of_candidate_digest": previous_candidate_digest,
        "feedback_digest": previous_feedback_digest,
    }
    for key, value in expected.items():
        if report.get(key) != value:
            errors.append(f"run_report.json {key} does not match lifecycle")
    if number == 2:
        try:
            edit = json.loads((path / "edit_report.json").read_bytes())
            if not isinstance(edit, dict) or not isinstance(edit.get("feedback_response"), str) or not edit["feedback_response"].strip():
                errors.append("Candidate 2 edit_report.json must explain feedback consumption")
        except Exception:
            errors.append("edit_report.json is not valid JSON")
    return errors


def validate_round_chain(records: list[dict], *, session: str | None = None) -> list[str]:
    errors: list[str] = []
    if len(records) != REQUIRED_VALID_ROUNDS:
        return ["readiness requires exactly two accepted rounds"]
    prior_candidate = prior_feedback = None
    for number, record in enumerate(records, 1):
        if record.get("submission") != number:
            errors.append(f"round {number} has wrong submission number")
        if session is not None and record.get("builder_session_id") != session:
            errors.append("accepted rounds must use one Builder session")
        if not isinstance(record.get("candidate_digest"), str) or record["candidate_digest"] == prior_candidate:
            errors.append(f"round {number} Candidate digest is missing or not distinct")
        if record.get("feedback_digest_ack") != prior_feedback:
            errors.append(f"round {number} does not acknowledge the exact preceding feedback digest")
        if not isinstance(record.get("feedback_digest"), str) or len(record["feedback_digest"]) != 64:
            errors.append(f"round {number} feedback digest is missing")
        prior_candidate, prior_feedback = record.get("candidate_digest"), record.get("feedback_digest")
    return errors
