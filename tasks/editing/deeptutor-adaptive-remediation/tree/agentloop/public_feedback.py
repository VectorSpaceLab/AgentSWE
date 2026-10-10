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



MAX_ERROR_TEXT = 500
MAX_TRACE_CALLS = 20


def fixture_tool_diagnostic(output):
    """Summarize the evaluator fixture's own product tool calls for the Builder.

    Create repeats every ``harness_error`` the evaluator observed back to the
    Builder (create75 evaluator/adapters/web-research-report/one_stop.py,
    ``feedback_text``).  Edit's dev feedback had no channel for one at all: a
    product exception raised inside an evaluator fixture tool call reached the
    Builder only as a bare sentence, with neither the exception class nor the
    name of the tool call that raised it.

    Only the public tool name, the owner marker, the success flag and the
    product exception class/text are exported.  ``arguments``, ``raw_content``
    and the returned values stay evaluator-private, so no oracle expectation
    and no hidden-case content can travel this way.  Called on dev cases only
    (harbor/formal_one_stop.py, the public-case runner).
    """
    import json
    from pathlib import Path
    root = Path(output) / 'fixture'
    summary = {}
    try:
        failure = json.loads((root / 'fixture-failure.json').read_text(encoding='utf-8'))
    except (OSError, ValueError):
        failure = None
    if isinstance(failure, dict):
        for key in ('fixture_ready', 'classification', 'error_type', 'reason'):
            if failure.get(key) is not None:
                summary[key] = failure[key]
    try:
        trace = json.loads((root / 'fixture-preparation-trace.json').read_text(encoding='utf-8'))
    except (OSError, ValueError):
        trace = None
    calls = trace.get('calls') if isinstance(trace, dict) else None
    if isinstance(calls, list):
        rows = []
        for item in calls[-MAX_TRACE_CALLS:]:
            if not isinstance(item, dict):
                continue
            row = {'tool': item.get('tool'), 'owner': item.get('owner'), 'success': item.get('success')}
            if item.get('error_type') is not None:
                row['error_type'] = item['error_type']
            if item.get('error') is not None:
                row['error'] = str(item['error'])[:MAX_ERROR_TEXT]
            rows.append(row)
        summary['tool_calls'] = rows
        summary['tool_call_count'] = len(calls)
        failed = [row for row in rows if row.get('success') is not True]
        if failed:
            summary['failed_tool'] = failed[0].get('tool')
    return summary or None


def harness_error_lines(value):
    """Create-style ``harness_error`` lines: one per observed tool/fixture fault."""
    lines = []
    diagnostic = value.get('fixture_diagnostic')
    if isinstance(diagnostic, dict):
        for row in diagnostic.get('tool_calls') or []:
            if not isinstance(row, dict) or row.get('success') is True:
                continue
            kind = row.get('error_type')
            lines.append('evaluator fixture tool %s raised %s' % (
                row.get('tool'),
                ((str(kind) + ': ') if kind else '') + str(row.get('error') or 'no error text recorded')))
        if not lines and diagnostic.get('reason'):
            kind = diagnostic.get('error_type')
            lines.append('evaluator fixture preparation failed: '
                         + ((str(kind) + ': ') if kind else '') + str(diagnostic['reason']))
    text = value.get('public_diagnostic')
    if isinstance(text, str) and text.strip():
        lines.append('evaluator observed: ' + text.strip())
    return lines


JUDGE_FEEDBACK_KEYS = ('assessment', 'major_errors', 'dimensions')

# A dev Result judge is handed dev inputs only and cannot name a hidden case.
# This projection now carries free judge prose, so a hidden case id or a
# hidden-case path is masked rather than shipped if one ever appears.
HIDDEN_REFERENCE = re.compile(r'\btest_\d{3}\b|test_cases/|hidden[-_/]cases?/|/hidden/')


def mask_hidden_references(value):
    if isinstance(value, dict):
        return {str(key): mask_hidden_references(item) for key, item in value.items()}
    if isinstance(value, list):
        return [mask_hidden_references(item) for item in value]
    if isinstance(value, str):
        return HIDDEN_REFERENCE.sub('[hidden-case-reference-removed]', value)
    return value


def judge_feedback(judgement):
    """The dev Result judge's own words, as Create sends them.

    Create repeats the judge's assessment and every major_error, per dev case
    (create75 evaluator/adapters/web-research-report/one_stop.py, feedback_text;
    a rendered example is 0917-create-v2lite/runs/ds-flash-02/database-analytics/
    run/feedback/round_001.md).  Edit shipped none of it: this projection used to
    read judgement['feedback'], but execution_scoring.judge_execution_case
    (@@AGENTSWE_EDITING_CONTROL@@/execution_scoring.py:55-67) returns
    assessment / major_errors / dimensions at the TOP level and never a 'feedback'
    key, so the branch was dead and the Builder saw a bare score and
    "independent semantic Result judge completed".

    A dev case's own oracle values are public-case data and travel, exactly as in
    Create.  Hidden case ids and hidden-case paths are not, and are masked.
    """
    if not isinstance(judgement, dict):
        return None
    if isinstance(judgement.get('feedback'), dict):
        source = judgement['feedback']
    else:
        source = {key: judgement[key] for key in JUDGE_FEEDBACK_KEYS
                  if judgement.get(key) not in (None, '', [], {})}
    return mask_hidden_references(clean(source)) or None


def public_case_feedback(value):
    fields = ('case_id', 'terminal', 'valid', 'infra_valid', 'classification', 'score', 'reason')
    result = {key: clean(value[key]) for key in fields if key in value}
    diagnostic = value.get('public_diagnostic')
    if isinstance(diagnostic, dict):
        result['diagnostic'] = clean({k: diagnostic[k] for k in ('phase','error_type','message','module','symbol','product_paths') if k in diagnostic})
    elif isinstance(diagnostic, str) and diagnostic.strip():
        # execution_evidence.attest_execution:89 records a Candidate fixture
        # precondition failure as a plain string.  The dict-only branch above
        # dropped it silently, so the one sentence that named the product fault
        # never reached the Builder.
        result['diagnostic'] = clean({'message': diagnostic})
    fixture = value.get('fixture_diagnostic')
    if isinstance(fixture, dict) and fixture:
        result['fixture_diagnostic'] = clean(fixture)
    errors = harness_error_lines(value)
    if errors:
        result['harness_errors'] = clean(errors)
    feedback = judge_feedback(value.get('semantic_judgement'))
    if feedback:
        result['feedback'] = feedback
    return result
