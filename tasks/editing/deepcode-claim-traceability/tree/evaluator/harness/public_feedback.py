"""Public score/feedback projection, excluding evaluator execution internals."""
import re

PRIVATE_PATH = re.compile(r'''/(?:home|data|run/secrets)/[^\s"'<>]+''')
# 2026-09-21 oracle-boundary guard.  The evaluator's reserved adversarial-probe
# identifiers and its hidden case inventory are published by NO dev task:
# dev_cases/dev_002/input.md names neither, while the dev_002 probe identifiers
# appear verbatim in test_cases/test_002/input.md and in
# evaluator/harness/semantic_oracle.py:250-265.  Since the dev judge is handed the
# same score-cap contract as hidden (evaluator/harness/controller.py::dev_judge_inputs)
# those identifiers reach this projection two ways: inside a cap reason /
# requirement_ref, and inside the judge's own prose, because the contract entries are
# pasted into its prompt (result_judge.py:1044-1057).  The cap id, the maximum, the
# counts and the requirement text all survive; only the identifiers are replaced.
RESERVED_PROBE_ID = re.compile(r'\badv-[a-z0-9][a-z0-9-]*')
HIDDEN_CASE_REF = re.compile(r'''test_cases/[^\s",;)]*|\btest_0[0-9]{2}\b''')


def clean(value):
    if isinstance(value, dict): return {key: clean(item) for key,item in value.items()}
    if isinstance(value, list): return [clean(item) for item in value]
    if isinstance(value, str):
        value = PRIVATE_PATH.sub('[evaluator-private-path]', value)
        value = RESERVED_PROBE_ID.sub('[reserved-probe-id]', value)
        return HIDDEN_CASE_REF.sub('[evaluator-case-inventory]', value)
    return value

# Bounds for the per-case diagnosis below. The Builder gets the evaluator's own
# words about its own public dev case, not a transcript.
ASSESSMENT_LIMIT = 4000
REASON_LIMIT = 600
MAJOR_ERROR_LIMIT = 600
MAJOR_ERRORS_MAX = 12
CAP_REASON_LIMIT = 900
CAP_ENTRIES_MAX = 16
HARNESS_ERROR_LIMIT = 400
HARNESS_ERRORS_MAX = 24


def _text(value, limit):
    if not isinstance(value, str): return None
    value = value.strip()
    if not value: return None
    return value if len(value) <= limit else value[:limit - 1] + '\u2026'


def case_diagnosis(value):
    """Why this dev case scored what it scored, in bounded evaluator-owned text.

    Before this, `builder-feedback-candidate_00N.json` carried per case exactly
    case_id / classification / infra_valid / score / semantic_score_contract_valid
    -- a bare `candidate_behavior_failure, infra_valid: true, score 0`, i.e.
    "your product misbehaved", with nothing to act on.  On 0920-fh-003 that zero
    was our own forced-effort output-budget truncation five times out of ten dev
    case runs, and the Builder, seeing only a stochastic zero that alternated
    between dev_001 and dev_002, spent rounds 2 and 3 hunting a shared-resource
    locking bug that did not exist (rollout 03:26:58 / 03:27:30 / 03:40:07).

    Everything here is already about the Candidate's OWN run of a PUBLIC dev
    case: the judge's summary of what it did, the errors that capped it, and
    whether the evaluator's transport cut the session short.  No hidden case,
    no oracle key, no rubric internals beyond what judging that public case
    already told the Candidate through its score.
    """
    diagnosis = {}
    judgement = value.get('semantic_judgement')
    judgement = judgement if isinstance(judgement, dict) else {}
    attribution = value.get('failure_attribution')
    attribution = attribution if isinstance(attribution, dict) else {}
    reason = _text(value.get('reason'), REASON_LIMIT) or _text(judgement.get('reason'), REASON_LIMIT) \
        or _text(attribution.get('reason'), REASON_LIMIT)
    if reason is not None: diagnosis['reason'] = reason
    assessment = _text(judgement.get('assessment'), ASSESSMENT_LIMIT)
    if assessment is not None: diagnosis['assessment'] = assessment
    errors = judgement.get('major_errors')
    if isinstance(errors, list):
        bounded = [_text(item, MAJOR_ERROR_LIMIT) for item in errors[:MAJOR_ERRORS_MAX]]
        bounded = [item for item in bounded if item is not None]
        if bounded: diagnosis['major_errors'] = bounded
    # The one fact the Builder could never see: a zero that our own transport
    # caused.  True only when the evaluator's own broker ledger says so.
    truncated = value.get('lower_agent_output_truncated')
    if truncated is None:
        delta = value.get('broker_delta')
        if isinstance(delta, dict) and 'output_budget_truncations' in delta:
            truncated = int(delta.get('output_budget_truncations') or 0) > 0
    if isinstance(truncated, bool):
        diagnosis['lower_agent_output_truncated'] = truncated
        if truncated:
            diagnosis['lower_agent_output_truncated_note'] = (
                'The evaluator-owned transport ended at least one model turn on its '
                'output-token ceiling, so this run was cut short by the harness, not '
                'by the product. Do not treat this score as evidence about your code.')
    return diagnosis


def score_cap_diagnosis(value):
    """The evaluator-issued ceiling this dev case actually hit, and why.

    The hidden axis has issued an `agentswe-result-score-caps/v1` contract to its
    Result judge since 0920 (evaluator/result_score_caps.py, wired in
    evaluator/formal_axes.py:214-246); the public dev axis issued none until
    evaluator/harness/controller.py::dev_judge_inputs.  On
    0905-edit-codex-xhigh-0921-v4-001-deepcode the Builder was told round 4 dev_002
    scored 96 while that round's own evidence caps the case at 20 -- it exited on a
    number the hidden axis contradicts.

    Published here: the cap ids that fired, the binding maximum, the evaluator's or
    the judge's own reason text, and the PUBLIC requirement each condition restates.
    Not published: conditions that did not fire, any hidden case, any private oracle
    expectation.  `clean()` then removes evaluator-private paths.
    """
    caps = value.get('result_score_caps')
    if not isinstance(caps, dict):
        return {}
    rows = []
    entries = caps.get('violated')
    for item in (entries if isinstance(entries, list) else [])[:CAP_ENTRIES_MAX]:
        if not isinstance(item, dict) or not isinstance(item.get('cap_id'), str):
            continue
        row = {'cap_id': item['cap_id']}
        if type(item.get('maximum_score')) is int:
            row['maximum_score'] = item['maximum_score']
        for key, limit in (('requirement_ref', REASON_LIMIT), ('reason', CAP_REASON_LIMIT)):
            text = _text(item.get(key), limit)
            if text is not None:
                row[key] = text
        if isinstance(item.get('decided_by'), str):
            row['decided_by'] = item['decided_by']
        rows.append(row)
    summary = {'contract_issued': caps.get('contract_issued') is True,
               'violated_cap_ids': [row['cap_id'] for row in rows],
               'violated_caps': rows}
    if type(caps.get('binding_cap_value')) is int:
        summary['binding_cap_value'] = caps['binding_cap_value']
        summary['binding_cap_ids'] = [x for x in (caps.get('binding_cap_ids') or []) if isinstance(x, str)]
        summary['note'] = ('The evaluator checked these published requirements against this dev '
                           'case\u2019s own evidence and handed the same contract to the Result '
                           'judge that the hidden cases get. A violated condition limits the sum '
                           'of the dimension scores to its maximum_score; lifting the cap needs '
                           'the requirement actually met, not a higher-scoring narrative.')
    elif summary['contract_issued']:
        summary['note'] = ('The evaluator-issued ceiling contract was handed to the Result judge '
                           'and no condition fired on this dev case.')
    issue_errors = caps.get('issue_errors')
    if isinstance(issue_errors, list) and issue_errors:
        bounded = [_text(item, REASON_LIMIT) for item in issue_errors[:CAP_ENTRIES_MAX]]
        bounded = [item for item in bounded if item is not None]
        if bounded:
            summary['contract_issue_errors'] = bounded
    return {'score_caps': summary}


def harness_error_diagnosis(value):
    """Verbatim evaluator harness/validator lines for this public dev case."""
    errors = value.get('harness_errors')
    if not isinstance(errors, list):
        return {}
    bounded = [_text(item, HARNESS_ERROR_LIMIT) for item in errors[:HARNESS_ERRORS_MAX]]
    bounded = [item for item in bounded if item is not None]
    return {'harness_errors': bounded} if bounded else {}


def public_record(record):
    result = {key: record[key] for key in ('round', 'source_submission', 'candidate_digest', 'accepted', 'round_consumed', 'evidence_kind') if key in record}
    build = record.get('build', {})
    result['build'] = {key: build[key] for key in ('exit_code','stdout','stderr') if key in build}
    result['dev'] = []
    for value in record.get('dev', []):
        case = {key: value[key] for key in ('case_id','classification','infra_valid','score','reason','semantic_score_contract_valid') if key in value}
        judgement = value.get('semantic_judgement')
        if isinstance(judgement, dict) and isinstance(judgement.get('feedback'), dict): case['feedback'] = judgement['feedback']
        case.update(case_diagnosis(value))
        case.update(score_cap_diagnosis(value))
        case.update(harness_error_diagnosis(value))
        result['dev'].append(case)
    return clean(result)
