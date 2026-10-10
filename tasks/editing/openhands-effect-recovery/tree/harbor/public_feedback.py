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
classification_reason build_valid finished_at submitted_at role'''.split())

PUBLIC_FIELDS = PUBLIC_FIELDS | frozenset('candidate authoritative infrastructure_blocked dev_mean failure_attribution party infrastructure_invalid candidate_result_valid broker_delta calls failures successful_calls unknown_usage_calls in_flight_calls input_tokens output_tokens total_tokens semantic_feedback accepted_candidates resampling_forbidden product_source_identity delivery_candidate_digest'.split())

# 0921: the per-case reason a round ended as it did.  These are evaluator statements
# about the evaluator's own guards and about the shape of the Candidate's own final
# reply; they disclose no hidden case content, no oracle state and no host path.
PUBLIC_FIELDS = PUBLIC_FIELDS | frozenset("""budget_truncation truncated reasons turns_completed
artifact_defect artifact_reask defect attempted repaired second_defect remaining_seconds minimum_seconds
action_loop_stop dispatch_guard step reserve_seconds artifact_reserve_seconds turn_allowance_seconds
fixed_reserve_floor_seconds observed_turn_seconds case_deadline_calls recovered_transport_calls refused
below_reserve stopped_by request infrastructure_failures infrastructure_case_reasons fatal observed_by
product_party""".split())

# 0921: the product's own rejected-action text and the harness/validator errors
# the evaluator observed, as pre-rendered strings (Create's harness_error lines).
# Distinct key names on purpose: they widen no existing payload shape.
PUBLIC_FIELDS = PUBLIC_FIELDS | frozenset(
    "action_rejections action_rejection_count harness_errors".split())

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
