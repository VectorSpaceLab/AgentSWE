"""Consume case/Candidate-bound Result evidence, never infer a score from status.

This boundary accepts evaluator-produced semantic_result receipts. The receipt
must identify immutable judge inputs and a completed contract; an absent or
unresolved judge is unavailable feedback, not a capability zero.
"""
from __future__ import annotations

import hashlib
import json
from pathlib import Path


# Must stay byte-equivalent to evaluator/result_dimensions.json: the Builder
# feedback boundary refuses a judge contract whose dimension set differs.
MAXIMA = {
    "handoff_outcome_completion": 30, "fail_closed_authority": 20,
    "exactly_once_and_ordering": 20, "durable_state_and_provenance": 15,
    "honest_reporting_and_contract": 10, "privacy_and_route_scope": 5,
}


def sha(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def semantic_feedback(record, case_id, candidate_digest):
    """Paths/raw evidence stay evaluator-only; expose only verified assessment."""
    missing = {"score": None, "valid": False, "classification": "infrastructure-invalid",
               "reason": "semantic Result evidence is missing, unresolved, or not bound to this Candidate"}
    try:
        receipt = record.get("semantic_result")
        if not isinstance(receipt, dict):
            return missing
        if receipt.get("case_id") != case_id or receipt.get("candidate_digest") != candidate_digest:
            return missing
        if receipt.get("classification") not in {"scoreable", "candidate_zero"}:
            return missing
        path = Path(receipt["contract_path"])
        if sha(path) != receipt["contract_sha256"]:
            return missing
        value = json.loads(path.read_text())
        if (value.get("case_id") != case_id or value.get("contract_valid") is not True
                or value.get("result_score_publishable") is not True):
            return missing
        score = value.get("result_score")
        if type(score) is not int or not 0 <= score <= 100:
            return missing
        binding = json.loads(Path(receipt["binding_path"]).read_text())
        if (sha(receipt["binding_path"]) != receipt["binding_sha256"]
                or binding.get("case_id") != case_id
                or binding.get("candidate_digest") != candidate_digest
                or binding.get("contract_sha256") != receipt["contract_sha256"]):
            return missing
        inputs = binding.get("inputs")
        if not isinstance(inputs, dict) or not inputs:
            return missing
        for item in inputs.values():
            if sha(item["path"]) != item["sha256"]:
                return missing
        if receipt["classification"] == "candidate_zero":
            # A generic failure label is insufficient: only an evidenced,
            # evaluator-attributed terminal zero may bypass the model judge.
            if (score != 0 or value.get("candidate_digest") != candidate_digest
                    or value.get("evaluation_state") != "candidate_zero"
                    or value.get("judge_invoked") is not False or not value.get("evidence")):
                return missing
            for item in value["evidence"]:
                if sha(item["path"]) != item["sha256"]:
                    return missing
            from evaluator.candidate_outcome import validate_zero_contract
            if not validate_zero_contract(value,inputs,record):
                return missing
        else:
            judge, usage = value.get("judge", {}), value.get("provider_usage", {})
            if (judge.get("model") != "deepseek-flash" or judge.get("reasoning_effort") != "max"
                    or usage.get("logical_requests") != (2 if value.get("early_stop_resample") else 1)
                    or usage.get("completed_responses") != (2 if value.get("early_stop_resample") else 1)
                    or type(usage.get("transport_attempts")) is not int or usage["transport_attempts"] < 1
                    or any(type(usage.get(k)) is not int or usage[k] <= 0
                           for k in ("input_tokens", "output_tokens", "total_tokens"))
                    or usage["total_tokens"] < usage["input_tokens"] + usage["output_tokens"]):
                return missing
            dimensions = value.get("dimensions")
            if not isinstance(dimensions, dict) or set(dimensions) != set(MAXIMA):
                return missing
            for key, maximum in MAXIMA.items():
                item = dimensions[key]
                if (item.get("max") != maximum or type(item.get("score")) is not int
                        or not 0 <= item["score"] <= maximum):
                    return missing
            if sum(v["score"] for v in dimensions.values()) != score:
                return missing
        feedback = {"score": score, "valid": True, "classification": receipt["classification"],
                    "assessment": value.get("assessment", value.get("reason", "")),
                    "major_errors": value.get("major_errors", []),
                    "dimensions": value.get("dimensions", {})}
        # The same evidence-bound ceilings the hidden judge is given (issued to the dev
        # judge by evaluator/dev_result.py).  Without this the Builder saw a capped score
        # and no statement of what capped it, which is the calibration gap this wave
        # closes.  Only the task owner's determination about THIS dev case travels: the
        # ceiling id, its value, its published requirement reference and the stated
        # reason, the judge's own ceiling findings, and its harness error strings
        # verbatim.  evidence_refs are deliberately dropped (evaluator file layout), and
        # no hidden case, private oracle expectation or judge-private material crosses.
        feedback["score_cap"] = value.get("score_cap")
        feedback["score_cap_conditions"] = [
            {key: entry.get(key) for key in
             ("cap_id", "maximum_score", "status", "requirement_ref", "reason")}
            for entry in (value.get("score_cap_conditions") or []) if isinstance(entry, dict)]
        feedback["ceiling_assessments"] = value.get("ceiling_assessments") or []
        feedback["harness_errors"] = [str(item) for item in (value.get("errors") or [])]
        if receipt["classification"] == "candidate_zero":
            # A bare 'missing_core' is not a lever the Builder can pull.  Name which
            # of the three evidenced causes it was and expose the counters behind it.
            # These are the Candidate's own run facts; no hidden case, oracle
            # observation or judge-private material crosses this boundary.
            proof = value.get("causal_proof", {})
            feedback["fatal_output_reason"] = proof.get("fatal_output_reason")
            feedback["terminal_cause"] = proof.get("terminal_cause")
            feedback["terminal_cause_explained"] = proof.get("terminal_cause_explained")
            feedback["observed"] = proof.get("terminal_cause_detail", {})
        return feedback
    except (OSError, ValueError, KeyError, TypeError, AttributeError):
        return missing
