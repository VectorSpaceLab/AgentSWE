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
revision_of_candidate_digest
round records preflight frozen freeze_reason source_submission_id frozen_at reason error error_type
error_detail errors details missing_delivery_files ready_for_submission submission_consumed round_consumed
workspace_digest patch_sha256 trigger exit_code command_count lower_execution_required
oracle_included native_suite_used_as_result public_projection_version contract_errors
classification_reason build_valid finished_at submitted_at role retry_allowed blocked_products'''.split())

# 0921: the evaluator-evidenced Result ceilings the dev judge was actually given.
# agentloop/evaluator/semantic_score.py:155-208 writes result_score_caps.json and
# passes it to the judge on BOTH paths, and the judge echoes score_cap /
# score_cap_conditions back in result_score_contract.json -- but semantic_feedback
# forwarded only assessment/major_errors/dimensions, so whether the Builder ever
# learned which ceiling bound its score depended on the judge mentioning it in
# prose.  Cap ids and cap reasons are explicitly Builder-visible; cap-specific key
# names are used so no other payload shape is widened.
PUBLIC_FIELDS = PUBLIC_FIELDS | frozenset(
    'score_cap score_cap_conditions cap_id cap_status cap_maximum_score requirement_ref'.split())

# 0921: this tree's Result dimensions are task-local
# (evaluator/result_dimensions.json, the same file semantic_score.py:202 hands
# the judge as --rubric-dimensions), and none of their names were in the generic
# allowlist above -- so every per-dimension score and its evidence was stripped
# and the Builder received "dimensions": {} while the judge had filled in all
# five.  Read from the tree's own single source rather than hardcoded here; if
# the file cannot be read the allowlist is left exactly as it was.
def _result_dimension_names():
    import json
    from pathlib import Path
    for parent in Path(__file__).resolve().parents:
        candidate = parent / 'evaluator' / 'result_dimensions.json'
        if candidate.is_file():
            try:
                value = json.loads(candidate.read_text(encoding='utf-8'))
            except (OSError, ValueError):
                return frozenset()
            if isinstance(value, dict):
                return frozenset(str(key) for key in value)
            return frozenset()
    return frozenset()


PUBLIC_FIELDS = PUBLIC_FIELDS | _result_dimension_names()


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


def score_cap_feedback(contract):
    """Name the ceilings this case was scored under, and why the bound ones bound.

    Every entry keeps its cap id and status.  A violated entry also keeps the
    evaluator's own reason and the public requirement it cites
    (input/*.md), which is what tells the Builder what to fix.  Entries that did
    not fire carry no reason: their text only restates that nothing disagreed.
    """
    entries = contract.get('score_cap_conditions')
    if not isinstance(entries, list):
        return None
    rows = []
    for item in entries:
        if not isinstance(item, dict):
            continue
        row = {'cap_id': item.get('cap_id'), 'cap_status': item.get('status'),
               'cap_maximum_score': item.get('maximum_score')}
        if item.get('status') == 'violated':
            if item.get('reason') is not None:
                row['reason'] = item['reason']
            if item.get('requirement_ref') is not None:
                row['requirement_ref'] = item['requirement_ref']
        rows.append(row)
    return rows or None


def semantic_feedback(contract):
    if not isinstance(contract, dict):
        return None
    value = {key: contract[key] for key in
        ('assessment', 'major_errors', 'dimensions', 'task_completion', 'evidence_grounding', 'recovery_and_safety')
        if key in contract}
    if contract.get('score_cap') is not None:
        value['score_cap'] = contract['score_cap']
    caps = score_cap_feedback(contract)
    if caps:
        value['score_cap_conditions'] = caps
    return public_payload(value)
