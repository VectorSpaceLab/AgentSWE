"""Allow-listed Builder socket payloads, excluding evaluator input/provenance."""
from __future__ import annotations
import re

PUBLIC_FIELDS = frozenset('''schema_version generated_at source_submission candidate_digest
builder_session_id builder_connection_id connection_id candidate_number submission_number
submission_id state accepted duplicate accepted_submissions required_submissions max_dev_rounds
public_inventory public_cases dev dev_001 dev_002 case_id classification candidate_observation
decision completion_claim outcome reason summary trajectory_event_kinds artifact_present score maximum
score_kind result_judge_feedback assessment major_errors dimensions task_completion evidence_grounding
recovery_and_safety max weight evidence rationale eligible_for_revision eligibility_reasons
dev_score dev_scores dev_passed feedback feedback_digest expected_feedback_digest feedback_digest_ack
round records preflight frozen freeze_reason source_submission_id frozen_at reason error error_type
error_detail errors details missing_delivery_files ready_for_submission submission_consumed round_consumed
workspace_digest patch_sha256 trigger exit_code command_count lower_execution_required
oracle_included native_suite_used_as_result public_projection_version contract_errors
classification_reason build_valid finished_at submitted_at role
delivery_errors build_error preflight_message stderr_tail
score_caps issued applied_ceiling violated cap_id maximum_score requirement_ref harness_errors'''.split())


def public_text(value: str) -> str:
    # Host evidence paths are evaluator implementation detail, including paths
    # embedded in exception messages or compiler stderr. Candidate relative
    # paths and public /workspace paths stay useful in feedback.
    return re.sub(r'/(?:home|data|tmp|root)/[^\s\"\'<>]+', '<evaluator-path>', value)


def public_payload(value):
    if isinstance(value, dict):
        return {key: public_payload(child) for key, child in value.items() if key in PUBLIC_FIELDS}
    if isinstance(value, list):
        return [public_payload(child) for child in value]
    if isinstance(value, str):
        return public_text(value)
    if value is None or isinstance(value, (int, float, bool)):
        return value
    return None


def semantic_feedback(contract):
    if not isinstance(contract, dict):
        return None
    return public_payload({key: contract[key] for key in
        ('assessment', 'major_errors', 'dimensions', 'task_completion', 'evidence_grounding', 'recovery_and_safety')
        if key in contract})


# --- evidence-bound ceilings and harness faults in public feedback (2026-09-21)
# A dev round that reports a number the hidden round cannot reproduce is not
# feedback.  The dev judge is now handed the same score-cap contract as the
# hidden judge (evaluator/formal_finalize.py:run_result_judge), and these two
# projections say so out loud: which ceilings bound this case and why, and what
# the evaluator's own harness/validators recorded against it.

CAP_FEEDBACK_FIELDS = ("cap_id", "maximum_score", "requirement_ref", "reason")
HARNESS_ERROR_LIMIT = 24
HARNESS_ERROR_CHARS = 600


def cap_line(value: str) -> str:
    text = " ".join(str(value).split())
    return text if len(text) <= HARNESS_ERROR_CHARS else text[:HARNESS_ERROR_CHARS] + " ..."


def score_cap_feedback(contract):
    """The evidence-bound ceilings this case was actually judged under.

    Identical to what the hidden axis issues and to the sentences handed to the
    judge.  Only ``violated`` entries travel: each names a public requirement
    reference and an evaluator observation about this very submission.  No
    hidden case and no private oracle expectation is quoted -- undecided
    (``unavailable``) entries, whose text can name a case's expected terminal
    class, are dropped upstream in formal_finalize.score_cap_binding.
    """
    binding = contract.get("score_cap_binding") if isinstance(contract, dict) else None
    if not isinstance(binding, dict):
        return None
    violated = [{key: item.get(key) for key in CAP_FEEDBACK_FIELDS}
                for item in (binding.get("violated") or []) if isinstance(item, dict)]
    for item in violated:
        item["reason"] = cap_line(item.get("reason") or "")
        item["requirement_ref"] = cap_line(item.get("requirement_ref") or "")
    return public_payload({
        "issued": binding.get("issued") is True,
        "applied_ceiling": binding.get("applied_ceiling"),
        "violated": violated,
        "error": binding.get("error"),
    })


def harness_error_lines(result):
    """Evaluator harness / validator lines recorded against this case.

    Every line is evaluator-authored text about the Candidate's own rollout: the
    CaseRuntime state fault, the deterministic artifact-contract validator
    codes, the isolated transport relay errors, an infrastructure failure
    attribution (including the Aider file-mention reflection-ceiling harness
    fault), and the evaluator/judge result contract's own state and errors.
    Nothing here reads a hidden case or the private oracle, and public_text
    scrubs host paths out of every line.
    """
    if not isinstance(result, dict):
        return []
    lines: list[str] = []
    state_error = result.get("state_error")
    if isinstance(state_error, str) and state_error.strip():
        lines.append("state_error: " + state_error)
    validation = result.get("artifact_validation")
    if isinstance(validation, dict):
        for code in validation.get("errors") or []:
            lines.append("artifact_validation: %s" % code)
    preflight = result.get("environment_preflight")
    if isinstance(preflight, dict) and preflight.get("valid") is not True:
        lines.append("environment_preflight: evaluator preflight is not valid")
    transport = result.get("transport_evidence")
    if isinstance(transport, dict):
        for channel in sorted(transport):
            record = transport[channel]
            if not isinstance(record, dict):
                continue
            for item in record.get("errors") or []:
                lines.append("transport_evidence.%s: %s" % (channel, item))
    attribution = result.get("failure_attribution")
    if isinstance(attribution, dict):
        reason = attribution.get("reason")
        if attribution.get("party") != "candidate" and isinstance(reason, str) and reason.strip():
            lines.append("failure_attribution: " + reason)
        if attribution.get("harness_file_mention_ceiling") is True:
            lines.append("harness_file_mention_ceiling: exhausted; the evaluator harness parsed "
                         "restored product stdout as Aider file mentions and spent the reflection "
                         "ceiling, so model replies were discarded before apply_updates()")
    contract = result.get("result_judge_contract")
    if isinstance(contract, dict):
        state = contract.get("evaluation_state")
        if isinstance(state, str) and state and state != "scoreable":
            detail = contract.get("reason")
            lines.append("result_contract: evaluation_state=%s%s"
                         % (state, "; " + detail if isinstance(detail, str) and detail else ""))
        for item in contract.get("errors") or []:
            lines.append("result_contract: %s" % item)
    return public_payload([cap_line(line) for line in lines[:HARNESS_ERROR_LIMIT]])
