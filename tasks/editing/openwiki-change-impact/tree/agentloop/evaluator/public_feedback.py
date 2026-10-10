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

PUBLIC_FIELDS = PUBLIC_FIELDS | frozenset('candidate authoritative infrastructure_blocked dev_mean failure_attribution party infrastructure_invalid candidate_result_valid broker_delta calls failures successful_calls unknown_usage_calls in_flight_calls input_tokens output_tokens total_tokens semantic_feedback accepted_candidates'.split())
PUBLIC_FIELDS = PUBLIC_FIELDS | frozenset('cases valid result_score semantic_contract_valid documentation_correctness durable_recovery_correctness publication_search_convergence evidence_faithfulness_and_safety change_impact_evidence static_publication_correctness search_index_correctness cross_surface_convergence quality_findings terminal_response_matches format_valid ceiling_assessments violated consumes_capability_round idempotent retry_same_round build failure changed_paths stdout stderr stdout_tail stderr_tail builder_metadata required patch_check patch_apply'.split())
PUBLIC_FIELDS = PUBLIC_FIELDS | frozenset('retry_allowed replay_blocked product_source_digest'.split())
# 0921: the evaluator-issued Result ceilings the dev judge is bound to, and the verbatim
# harness/validator errors, both of which the hidden path already acted on silently.
PUBLIC_FIELDS = PUBLIC_FIELDS | frozenset('''score_caps contract_issued effective_ceiling
judge_score_cap violated_caps semantic_review_violated cap_id maximum_score status
harness_errors validator_errors'''.split())

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
