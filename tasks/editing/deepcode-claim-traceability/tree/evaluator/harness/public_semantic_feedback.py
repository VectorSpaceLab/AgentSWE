"""Typed, bounded public facts from completed evaluator-owned case evidence.

This does not parse or repair model score output, execute a case, or authorize
another attempt. Paths are supplied by the evaluator, never by the Candidate.
The authority boundary is the evaluator-owned run, not a signature against an
administrator capable of rewriting that run and this module.
"""
from __future__ import annotations
from dataclasses import dataclass
import hashlib
import json
import os
from pathlib import Path
import re
import stat

PUBLIC_CASES = ('dev_001', 'dev_002')
CLASSIFICATIONS = frozenset(('candidate_valid', 'candidate_partial', 'candidate_behavior_failure'))
# 2026-09-21 audit finding.  An invalid judge contract used to be projected only when
# its `errors` list was EXACTLY ONE string matching the JSONDecodeError shape; every
# other invalid contract -- a ceiling violation, a dimension-key mismatch, a `ran_out`
# answer, a missing top-level key, several errors at once -- fell through
# `return None` and the Builder got no feedback record at all for that case, silently.
# The rejection stays exactly as terminal as before; only the diagnosis is emitted.
JSON_DECODE_ERROR = re.compile(
    r'JSONDecodeError: [^\r\n]{1,240}: line [1-9][0-9]* column [1-9][0-9]* \(char [0-9]+\)')
PRIVATE_PATH = re.compile('/(?:home|data|run/secrets)/[^\\s"\'<>]+')
JUDGE_ERROR_LINE_CHARS = 400
JUDGE_ERROR_LINES_MAX = 16
RESULT_JUDGE_ERRORS = {
    'result_score_json_invalid':
        "The evaluator's Result judge answered but its JSON could not be parsed, so this "
        "case has no publishable score. This is an evaluator-side sampling fault, not "
        "evidence about your product.",
    'result_score_contract_invalid':
        "The evaluator's Result judge answered but the answer failed the shared validator, "
        "so this case has no publishable score. The verbatim validator lines are in "
        "result_judge_error_lines.",
    'result_judge_infrastructure_error':
        "The evaluator's Result judge did not complete. This is evaluator infrastructure, "
        "not evidence about your product.",
}


def _public_line(value):
    text = ' '.join(str(value).split())
    text = PRIVATE_PATH.sub('[evaluator-owned-path]', text)
    if len(text) > JUDGE_ERROR_LINE_CHARS:
        text = text[:JUDGE_ERROR_LINE_CHARS - 1] + '\u2026'
    return text


QUALITY = {
    'DeepCode artifact schema/case mismatch': 'artifact_schema_or_case_mismatch',
    'missing task-native list observations': 'missing_observations',
    'missing task-native list tool_trajectory_summary': 'missing_tool_trajectory_summary',
    'missing task-native list state_receipts': 'missing_state_receipts',
    'missing task-native list artifact_paths': 'missing_artifact_paths',
    'missing task-native decision/safety object': 'missing_decision_or_safety',
}


@dataclass(frozen=True)
class PublicCaseRejectionFeedback:
    case_id: str
    classification: str
    infra_valid: bool
    artifact_valid: bool
    semantic_score_contract_valid: bool
    score: int | None
    result_judge_error: str | None
    quality_findings: tuple[str, ...]
    result_judge_error_lines: tuple[str, ...] = ()

    def __post_init__(self):
        if (type(self.case_id) is not str or self.case_id not in PUBLIC_CASES
                or type(self.classification) is not str or self.classification not in CLASSIFICATIONS
                or self.infra_valid is not True or self.artifact_valid is not True
                or type(self.semantic_score_contract_valid) is not bool
                or type(self.quality_findings) is not tuple
                or len(set(self.quality_findings)) != len(self.quality_findings)
                or any(type(x) is not str or x not in QUALITY.values() for x in self.quality_findings)):
            raise ValueError('invalid typed public case feedback')
        if type(self.result_judge_error_lines) is not tuple or any(
                type(x) is not str or not x or len(x) > JUDGE_ERROR_LINE_CHARS
                or PRIVATE_PATH.search(x) for x in self.result_judge_error_lines):
            raise ValueError('invalid public Result judge error lines')
        if self.semantic_score_contract_valid:
            if (type(self.score) is not int or not 0 <= self.score <= 100
                    or self.result_judge_error is not None or self.result_judge_error_lines != ()):
                raise ValueError('invalid public score')
        elif (self.score is not None or self.result_judge_error not in RESULT_JUDGE_ERRORS
                or not self.result_judge_error_lines
                or len(self.result_judge_error_lines) > JUDGE_ERROR_LINES_MAX
                or len(set(self.result_judge_error_lines)) != len(self.result_judge_error_lines)):
            raise ValueError('invalid public Result error')

    def public_dict(self):
        # Validate again so a mutated/forged instance fails closed at serialization.
        self.__post_init__()
        return {key: getattr(self, key) for key in (
            'case_id', 'classification', 'infra_valid', 'artifact_valid',
            'semantic_score_contract_valid', 'score', 'result_judge_error')} | {
                'quality_findings': list(self.quality_findings),
                'result_judge_error_lines': list(self.result_judge_error_lines),
                'result_judge_reason': RESULT_JUDGE_ERRORS.get(self.result_judge_error)}


def _safe(path: Path, *, missing=False):
    if type(path) is not Path and not isinstance(path, Path):
        raise ValueError('path required')
    if not path.is_absolute() or '..' in path.parts:
        raise ValueError('absolute owned path required')
    for part in (path, *path.parents):
        try:
            if stat.S_ISLNK(part.lstat().st_mode):
                raise ValueError('symlink evidence rejected')
        except FileNotFoundError:
            if not missing:
                raise


def _bytes(path: Path, maximum=2 * 1024 * 1024):
    _safe(path)
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
    with os.fdopen(fd, 'rb') as handle:
        info = os.fstat(handle.fileno())
        if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1 or info.st_size > maximum:
            raise ValueError('invalid owned evidence file')
        return handle.read(maximum + 1)


def _pairs(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError('duplicate JSON key')
        result[key] = value
    return result


def _invalid_constant(value):
    raise ValueError('non-finite JSON constant')


def _read(path):
    value = json.loads(_bytes(path), object_pairs_hook=_pairs, parse_constant=_invalid_constant)
    if type(value) is not dict:
        raise ValueError('evidence object required')
    return value


def _sha(data):
    return hashlib.sha256(data).hexdigest()


def _provider_completed(contract):
    usage, judge = contract.get('provider_usage'), contract.get('judge')
    if type(usage) is not dict or type(judge) is not dict:
        return False
    return (judge.get('model') == 'deepseek-flash' and judge.get('reasoning_effort') == 'max'
        and type(usage.get('logical_requests')) is int
        and usage['logical_requests'] == (2 if contract.get('early_stop_resample') else 1)
        and type(usage.get('completed_responses')) is int
        and usage['completed_responses'] == (2 if contract.get('early_stop_resample') else 1)
        and type(usage.get('transport_attempts')) is int and 1 <= usage['transport_attempts'] <= 100
        and all(type(usage.get(k)) is int and 0 < usage[k] <= 10**12
                for k in ('input_tokens', 'output_tokens', 'total_tokens'))
        and usage['total_tokens'] >= usage['input_tokens'] + usage['output_tokens'])


def collect_public_case_feedback(*, run_dir: Path, evaluation_root: Path,
                                 repository: Path, case_root: Path, case_id: str,
                                 candidate_digest: str):
    """Return None when evidence is missing/invalid, without changing outcome."""
    try:
        if case_id not in PUBLIC_CASES or type(case_id) is not str:
            return None
        if type(candidate_digest) is not str or not re.fullmatch('[0-9a-f]{64}', candidate_digest):
            return None
        for path in (run_dir, evaluation_root, case_root):
            _safe(path)
        if (evaluation_root.parent != run_dir / 'evaluations'
                or not re.fullmatch(r'candidate_[0-9]{3}_attempt_[0-9]+', evaluation_root.name)
                or repository.parent != run_dir / 'candidates'
                or not re.fullmatch(r'candidate_[0-9]{3}', repository.name)
                or case_root.name != case_id or case_root.parent.name != 'dev_cases'):
            return None
        _safe(repository, missing=True)
        output = evaluation_root / case_id
        request = _read(evaluation_root / (case_id + '-case-request.json'))
        if any(request.get(k) != str(v) for k, v in (
                ('repository', repository), ('case', case_root), ('output', output))):
            return None
        if request.get('case_id') != case_id or ('candidate_digest' in request
                and request['candidate_digest'] != candidate_digest):
            return None
        context = _read(output / 'logical-context.json')
        task_sha = _sha(_bytes(case_root / 'input.md'))
        if (context.get('case_id') != case_id or context.get('candidate_digest_before') != candidate_digest
                or context.get('task_sha256') != task_sha):
            return None
        execution = _read(output / 'controller_execution_result.json')
        result = _read(output / 'controller_result.json')
        if (execution.get('case_id') != case_id or execution.get('candidate_digest_after') != candidate_digest
                or execution.get('candidate_repository') != str(repository)
                or result.get('case_id') != case_id or result.get('candidate_digest') != candidate_digest
                or result.get('candidate_digest_after') != candidate_digest
                or execution.get('infra_valid') is not True or result.get('infra_valid') is not True
                or result.get('classification') != execution.get('classification')):
            return None
        validation = execution.get('artifact_validation')
        if (type(validation) is not dict or validation.get('validated_by') != 'evaluator'
                or validation.get('valid') is not True or result.get('artifact_validation') != validation):
            return None
        artifact = output / 'workspace' / 'agent_result.json'
        artifact_sha = _sha(_bytes(artifact))
        if validation.get('sha256') != artifact_sha:
            return None
        findings = validation.get('quality_schema_findings')
        if (type(findings) is not list or len(findings) > len(QUALITY)
                or any(type(x) is not str or x not in QUALITY for x in findings)
                or len(set(findings)) != len(findings)):
            return None
        scoring = output / 'semantic_scoring'
        intent = _read(scoring / 'scoring_intent.json')
        identity = intent.get('identity')
        if type(identity) is not dict or intent.get('identity_sha256') != _sha(
                json.dumps(identity, sort_keys=True, separators=(',', ':')).encode()):
            return None
        normalized = dict(execution)
        normalized['candidate_digest'] = candidate_digest
        if (identity.get('case_id') != case_id or identity.get('candidate_digest') != candidate_digest
                or identity.get('execution_record') != normalized
                or identity.get('model') != 'deepseek-flash' or identity.get('effort') != 'max'):
            return None
        inputs = identity.get('inputs')
        if type(inputs) is not dict or any(inputs.get(k) != {'path': str(p), 'sha256': h}
                for k, p, h in (('task_input', case_root / 'input.md', task_sha),
                                ('agent_artifact', artifact, artifact_sha))):
            return None
        contract = _read(scoring / 'result_score_contract.json')
        if (contract.get('schema_version') != 'agentswe-edit-result-score-contract-v1'
                or contract.get('case_id') != case_id or not _provider_completed(contract)
                or contract.get('prompt_digest') != _sha(_bytes(scoring / 'judge_prompt.txt'))):
            return None
        valid = contract.get('contract_valid')
        if type(valid) is not bool or result.get('semantic_score_contract_valid') is not valid:
            return None
        if valid:
            score = contract.get('result_score')
            if (contract.get('result_score_publishable') is not True
                    or contract.get('evaluation_state') != 'scoreable'
                    or type(score) is not int or not 0 <= score <= 100
                    or result.get('score') != score or type(result.get('score')) is not int
                    or contract.get('errors') != []
                    or contract.get('input_manifest_digest') != _sha(_bytes(scoring / 'input_manifest.json'))):
                return None
            # The judge writes exactly one additional newline. Hash bytes only;
            # do not parse, repair, or rejudge the model response.
            raw = _bytes(scoring / 'model_response.json')
            if not raw.endswith(b'\n') or contract.get('model_response_digest') != _sha(raw[:-1]):
                return None
            error, judge_lines = None, ()
        else:
            # ANY invalid contract shape now yields a record with a reason; the old
            # exactly-one-JSONDecodeError rule silently dropped the rest (see the
            # JSON_DECODE_ERROR note above).  Nothing is parsed, repaired or rejudged:
            # the evaluator's own validator lines are copied out, bounded and scrubbed.
            state = contract.get('evaluation_state')
            errors = contract.get('errors')
            if (contract.get('result_score_publishable') is not False
                    or state not in ('model_output_invalid', 'infrastructure_error')
                    or contract.get('result_score') is not None or result.get('score') is not None
                    or type(errors) is not list or any(type(x) is not str for x in errors)):
                return None
            if state == 'infrastructure_error':
                error = 'result_judge_infrastructure_error'
            elif len(errors) == 1 and JSON_DECODE_ERROR.fullmatch(errors[0]):
                error = 'result_score_json_invalid'
            else:
                error = 'result_score_contract_invalid'
            lines = [line for line in dict.fromkeys(
                _public_line(x) for x in errors[:JUDGE_ERROR_LINES_MAX]) if line]
            if not lines:
                lines = ['the evaluator recorded no publishable Result contract and no '
                         'reportable validator line for this case']
            score, judge_lines = None, tuple(lines)
        return PublicCaseRejectionFeedback(case_id, result['classification'], True, True,
            valid, score, error, tuple(QUALITY[x] for x in findings), judge_lines)
    except (OSError, ValueError, TypeError, KeyError, OverflowError, RecursionError):
        return None


def rejection_feedback_payload(candidate_digest, cases):
    """Serialize only exact typed values; invalid diagnostics are omitted."""
    try:
        if type(candidate_digest) is not str or not re.fullmatch('[0-9a-f]{64}', candidate_digest):
            return None
        values = [x.public_dict() for x in cases if type(x) is PublicCaseRejectionFeedback]
        if not values or len({x['case_id'] for x in values}) != len(values):
            return None
        return {'candidate_digest': candidate_digest, 'evaluation_state': 'not_evaluable',
                'accepted': False, 'round_consumed': False, 'dev': values}
    except (ValueError, TypeError, KeyError, AttributeError):
        return None
