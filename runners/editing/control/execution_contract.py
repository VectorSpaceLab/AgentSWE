"""Evaluator-owned execution attribution; absence of evidence is never a zero.

Records must be assembled by the trusted launcher, not copied from a Candidate
artifact. This module does not grade task quality or infer fault from HTTP totals.
"""
from __future__ import annotations

import hashlib
from pathlib import Path
from typing import Any


FATAL_CANDIDATE_CLASSES = frozenset({
    "candidate_build_failure", "candidate_artifact_failure", "no_model_call",
    "candidate_behavior_failure", "candidate_product_failure",
    "candidate_timeout", "candidate_policy_violation",
})
INFRA_CLASSES = frozenset({
    "infrastructure_invalid", "infrastructure_error", "provider_failure",
    "broker_failure", "credential_failure", "evaluator_failure",
    "mount_failure", "docker_failure", "environment_failure",
})


def file_digest(path: str | Path) -> str:
    h = hashlib.sha256()
    with Path(path).open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            h.update(block)
    return h.hexdigest()


def lower_execution_counts(record: dict[str, Any]) -> tuple[int, int]:
    """Only explicitly designated evaluator broker counters are eligible."""
    rows = []
    for key in ("broker", "broker_delta", "broker_stats_delta", "lower_broker",
                "lower_broker_delta", "provider_usage"):
        row = record.get(key)
        if isinstance(row, dict):
            rows.append(row)
            if isinstance(row.get("runtime"), dict):
                rows.append(row["runtime"])
    def maximum(keys):
        return max([0] + [r[k] for r in rows for k in keys
                         if type(r.get(k)) is int and r[k] >= 0])
    return (maximum(("calls_delta", "calls", "logical_requests")),
            maximum(("successful_calls", "completed_calls", "successful_logical_requests")))


def infrastructure_reason(record: dict[str, Any]) -> str | None:
    attr = record.get("failure_attribution") or {}
    if (record.get("infrastructure_invalid") is True or record.get("infra_valid") is False
            or attr.get("party") in {"infrastructure", "provider", "evaluator"}):
        return "trusted execution record attributes failure to infrastructure"
    if record.get("classification") in INFRA_CLASSES:
        return "terminal infrastructure classification: " + record["classification"]
    preflight = record.get("environment_preflight") or {}
    if preflight.get("valid") is False:
        return "evaluator environment preflight failed"
    # Recovered retry events and old aggregate upstream_failures are not causal
    # evidence. Only an explicit terminal, unrecovered infrastructure event is.
    for event in record.get("infrastructure_events", []):
        if (isinstance(event, dict) and event.get("terminal") is True
                and event.get("recovered") is False):
            return "unrecovered terminal infrastructure event"
    return None


def classify_candidate_execution(record: dict[str, Any], *, case_id: str,
                                 candidate_digest: str) -> dict[str, Any]:
    def verdict(classification, reason, evidence=None):
        valid = classification in {"scoreable", "candidate_zero"}
        return {"case_id": case_id, "candidate_digest": candidate_digest,
                "classification": classification, "score": 0 if classification == "candidate_zero" else None,
                "contract_valid": valid, "round_consumed": valid,
                "reason": reason, "evidence": evidence or []}

    infra = infrastructure_reason(record)
    if infra:
        return verdict("infrastructure_invalid", infra)
    if (not case_id or not candidate_digest or record.get("case_id") != case_id
            or record.get("candidate_digest") != candidate_digest):
        return verdict("unresolved", "execution record is not bound to this case and frozen digest")
    attr = record.get("failure_attribution") or {}
    if (record.get("classification") in FATAL_CANDIDATE_CLASSES
            and attr.get("party") == "candidate" and attr.get("observed_by") == "evaluator"
            and attr.get("fatal") is True):
        if record.get("execution_attempted") is not True or (record.get("environment_preflight") or {}).get("valid") is not True:
            return verdict("unresolved", "Candidate fatal gate lacks attempted execution and healthy preflight")
        paths = attr.get("evidence_paths")
        if not isinstance(paths, list) or not paths or not attr.get("reason"):
            return verdict("unresolved", "Candidate fatal gate has no concrete attribution evidence")
        try:
            evidence = [{"path": str(Path(p).resolve()), "sha256": file_digest(p)} for p in paths]
        except (OSError, TypeError, ValueError):
            return verdict("unresolved", "Candidate attribution evidence is missing or unreadable")
        return verdict("candidate_zero", attr["reason"], evidence)
    calls, successful = lower_execution_counts(record)
    validation = record.get("artifact_validation") or {}
    if (record.get("real_execution") is True and calls > 0 and successful > 0
            and validation.get("validated_by") == "evaluator"
            and validation.get("valid") is True and validation.get("sha256")):
        return verdict("scoreable", "real lower execution and task-specific artifact provenance validated")
    return verdict("unresolved", "missing successful lower execution/provenance or explicit attributable fatal gate")


def candidate_zero_result_contract(verdict: dict[str, Any], *, case_id: str,
                                   candidate_digest: str, evidence_paths=None) -> dict[str, Any]:
    if not (verdict.get("classification") == "candidate_zero"
            and verdict.get("contract_valid") is True and verdict.get("evidence")
            and verdict.get("case_id") == case_id and verdict.get("candidate_digest") == candidate_digest):
        raise ValueError("a bound, evidenced evaluator Candidate-zero verdict is required")
    for item in verdict["evidence"]:
        if file_digest(item["path"]) != item["sha256"]:
            raise ValueError("Candidate-zero evidence changed after attribution")
    return {
        "schema_version": "agentswe-execution-result/v1", "case_id": case_id,
        "candidate_digest": candidate_digest, "classification": "candidate_zero",
        "evaluation_state": "candidate_zero", "result_state": "fatal_candidate_failure",
        "score": 0, "result_score": 0, "maximum": 100, "contract_valid": True,
        "score_publishable": True, "result_score_publishable": True,
        "judge_invoked": False, "judge_usage": None, "provider_usage": None,
        "rubric_dimensions": None, "reason": verdict["reason"],
        "evidence": verdict["evidence"], "source_refs": evidence_paths or [],
    }
