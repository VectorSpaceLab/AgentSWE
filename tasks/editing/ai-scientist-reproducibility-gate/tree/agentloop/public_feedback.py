"""Builder-visible projections. Private execution ledgers stay evaluator-owned."""
import re


def clean(value):
    if isinstance(value, dict):
        return {str(key): clean(item) for key, item in value.items()}
    if isinstance(value, list):
        return [clean(item) for item in value]
    if isinstance(value, str):
        return re.sub(r'/(?:home|data)/[^\s\"\'<>]+|/run/secrets/[^\s\"\'<>]+', '[evaluator-private-path]', value)
    return value


def build_feedback(build):
    keys = ('valid','classification','failure','errors','changed_paths',
            'python_compileall','target_entry_candidates','patch_check','patch_apply',
            'compile_diagnostics')
    return clean({key: build[key] for key in keys if key in build})


def score_caps(semantic_feedback):
    """The evidence-bound ceiling contract the Result judge was handed.

    Cap id, the binding cap value and the evaluator's own reason text, exactly as
    ``evaluator/scientific_audit.case_world_score_caps`` published them for THIS
    public case.  The hidden path has always had this; the dev path computed it
    and then dropped it, so the Builder optimised against a channel that could not
    show it the ceiling that decides the hidden score.  Nothing here can carry
    hidden-case evidence: the caller iterates the public dev inventory only.
    """
    if not isinstance(semantic_feedback, dict):
        return None
    conditions = semantic_feedback.get('score_cap_conditions')
    conditions = conditions if isinstance(conditions, list) else []
    rows = [{'cap_id': item.get('cap_id'), 'status': item.get('status'),
             'maximum_score': item.get('maximum_score'),
             'requirement_ref': item.get('requirement_ref'),
             'reason': item.get('reason')}
            for item in conditions if isinstance(item, dict)]
    violated = [row for row in rows if row['status'] == 'violated']
    return clean({
        'binding_cap': semantic_feedback.get('score_cap'),
        'violated_cap_ids': [row['cap_id'] for row in violated],
        'violated_caps': violated,
        'cap_inventory': [{'cap_id': row['cap_id'], 'status': row['status'],
                           'maximum_score': row['maximum_score']} for row in rows],
    })


def harness_errors(result, semantic_feedback):
    """Verbatim evaluator harness / validator / judge errors for one public case.

    Every field is written by the evaluator's own launcher, artifact validator or
    Result judge about THIS public case.  Empty values are omitted so a clean case
    reports ``{}`` rather than a wall of nulls.
    """
    loop = result.get('action_loop') if isinstance(result.get('action_loop'), dict) else {}
    observation = result.get('product_observation') if isinstance(result.get('product_observation'), dict) else {}
    evaluation = result.get('result_evaluation') if isinstance(result.get('result_evaluation'), dict) else {}
    mismatches = result.get('author_claim_mismatches')
    rows = {
        'launcher_error': result.get('error'),
        'action_loop_error': loop.get('error'),
        'action_loop_failure_axis': loop.get('failure_axis'),
        'stopped_by_evaluator_budget_guard': loop.get('stopped_by_evaluator_budget_guard') or None,
        'authoring_error': result.get('authoring_error'),
        'authoring_failure_axis': result.get('authoring_failure_axis'),
        'missing_release_artifacts': observation.get('missing_release_artifacts'),
        'artifact_validator_claim_mismatches': mismatches[:20] if isinstance(mismatches, list) else None,
        'judge_errors': (semantic_feedback or {}).get('errors') if isinstance(semantic_feedback, dict) else None,
        'evaluation_unresolved_reason': (
            evaluation.get('reason') if evaluation.get('contract_valid') is not True else None),
    }
    return clean({key: value for key, value in rows.items()
                  if value not in (None, [], {}, '', False)})


def submission_payload(result, session_id):
    keys = ('accepted','submission_number','candidate_digest','patch_sha256',
            'feedback_digest','feedback_digest_ack','feedback','frozen',
            'duplicate_digest','round_consumed','state','error',
            'classification','classification_axis','expected_feedback_digest',
            'dev_scores','dev_mean','dev_passed','product_source_digest','retry_allowed')
    # Feedback is already the exact public projection whose file is hashed.
    payload = {key: (result[key] if key == 'feedback' else clean(result[key])) for key in keys if key in result}
    payload['builder_session_id'] = session_id
    if isinstance(result.get('freeze'),dict):
        payload['freeze'] = {'freeze_reason':result['freeze'].get('freeze_reason')}
    return payload
