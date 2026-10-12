"""Bind captured Dyad execution to the same independent public/hidden judge."""
from __future__ import annotations

import hashlib
import json
from pathlib import Path
import sys

SHARED = Path('@@AGENTSWE_EDITING_CONTROL@@')
if str(SHARED) not in sys.path:
    sys.path.append(str(SHARED))
from execution_scoring import judge_execution_case


def digest(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def owned_file(record, key, root, hash_key=None):
    raw = record.get(key)
    if not isinstance(raw, str) or not raw:
        raise ValueError('missing evaluator evidence: ' + key)
    path = Path(raw).resolve()
    path.relative_to(Path(root).resolve())
    if not path.is_file():
        raise ValueError('missing evaluator evidence file: ' + key)
    if hash_key and digest(path) != record.get(hash_key):
        raise ValueError('changed evaluator evidence: ' + key)
    return path


ROOT = Path(__file__).resolve().parents[1]
RESULT_RUBRIC = ROOT / 'evaluator' / 'result_rubric.md'


def score_cap_contract(record, files):
    """Best-effort evaluator-issued ceiling bound to this case's judge inputs.

    The formal path hands the judge a byte-identical copy of the private
    comparison and the task rubric through the run-local overlay symlink, so
    digests taken here match what the judge reads.  Any failure returns None
    and the round proceeds uncapped rather than erroring.
    """
    try:
        from result_score_caps import build_cap_contract
    except ImportError:
        try:
            sys.path.insert(0, str(ROOT / 'evaluator'))
            from result_score_caps import build_cap_contract
        except ImportError:
            return None
    try:
        native = files['native']
        return build_cap_contract(
            case_id=record.get('case_id') or '', rubric=RESULT_RUBRIC,
            native_evidence=native, oracle_summary=files['oracle'],
            agent_artifact=files.get('artifact'),
            destination=native.with_name(native.stem + '.score-cap-contract.json'))
    except (OSError, ValueError, TypeError, KeyError):
        return None


# The shared Result judge refuses a native evidence input over 2,500,000 bytes
# and interpolates whatever it accepts straight into its prompt, so the usable
# band is the one cases have actually judged in (171-355 KB in 0919-fw-001).
# A projection is only built above this ceiling; below it the file is passed
# through untouched.
JUDGE_NATIVE_EVIDENCE_MAX_BYTES = 1_500_000
# Fields evaluator/result_score_caps.py::_assess reads out of the native
# evidence.  They are never projected, so evaluator-issued ceilings are decided
# on byte-identical values.
JUDGE_NATIVE_PRESERVED_FIELDS = ('workspace', 'product_action_trajectory',
                                 'scenario_checks', 'private_oracle_visible',
                                 'read_stability_probes', 'acceptance_gate_probes',
                                 'target_bytes_sha256_at_admission')
# Tried in order; the first projection under the ceiling wins.
JUDGE_NATIVE_PROJECTION_STEPS = ((1000, 20, 20), (600, 12, 12), (400, 8, 8), (200, 4, 4))


def _project_clip(value, cap, stats):
    if isinstance(value, str):
        raw = value.encode('utf-8')
        if len(raw) > cap:
            stats['clipped_strings'] += 1
            stats['clipped_bytes'] += len(raw) - cap
            return (raw[:cap].decode('utf-8', 'ignore')
                    + '\u2026[evaluator-truncated: %d of %d bytes omitted]'
                    % (len(raw) - cap, len(raw)))
        return value
    if isinstance(value, list):
        return [_project_clip(item, cap, stats) for item in value]
    if isinstance(value, dict):
        return {key: _project_clip(item, cap, stats) for key, item in value.items()}
    return value


def _project_elide(sequence, head, tail, key, stats):
    if len(sequence) <= head + tail:
        return sequence
    dropped = len(sequence) - head - tail
    stats['elided_entries'][key] = dropped
    marker = {'evaluator_elision': True, 'omitted_entries': dropped,
              'kept_head': head, 'kept_tail': tail,
              'note': 'evaluator omitted %d middle %s entries from this judge '
                      'projection; the complete record is the source native '
                      'evidence named in evaluator_judge_projection' % (dropped, key)}
    return list(sequence[:head]) + [marker] + list(sequence[-tail:])


def _project_once(document, per_value, head, tail):
    stats = {'clipped_strings': 0, 'clipped_bytes': 0, 'elided_entries': {}}
    projected = {}
    for key, value in document.items():
        if key in JUDGE_NATIVE_PRESERVED_FIELDS:
            projected[key] = value
            continue
        value = _project_clip(value, per_value, stats)
        if isinstance(value, list) and len(json.dumps(value, ensure_ascii=False)) > 50_000:
            value = _project_elide(value, head, tail, key, stats)
        projected[key] = value
    return projected, stats


def judge_native_projection(native, destination):
    """Return a judge-sized view of `native`, or `native` itself when small.

    The full file stays the source evidence and is named, sized and digested
    inside the projection, so the judge is told exactly what it is reading and
    the untruncated record remains on disk for any later audit.  Any failure
    returns the original path: a projection must never fail a valid round on
    its own -- the shared guard is still there to refuse a genuinely unusable
    input.
    """
    try:
        native = Path(native)
        source_bytes = native.stat().st_size
        if source_bytes <= JUDGE_NATIVE_EVIDENCE_MAX_BYTES:
            return native
        document = json.loads(native.read_text(encoding='utf-8'))
        if not isinstance(document, dict):
            return native
        source_digest = digest(native)
        for per_value, head, tail in JUDGE_NATIVE_PROJECTION_STEPS:
            projected, stats = _project_once(document, per_value, head, tail)
            projected['evaluator_judge_projection'] = {
                'evidence_kind': 'evaluator_bounded_projection',
                'candidate_authored': False,
                'reason': 'native evidence exceeds the shared Result judge input limit',
                'source_native_evidence_path': str(native),
                'source_native_evidence_sha256': source_digest,
                'source_bytes': source_bytes,
                'preserved_fields': list(JUDGE_NATIVE_PRESERVED_FIELDS),
                'per_value_byte_cap': per_value,
                'kept_head_entries': head, 'kept_tail_entries': tail,
                'clipped_strings': stats['clipped_strings'],
                'clipped_bytes': stats['clipped_bytes'],
                'elided_entries': stats['elided_entries'],
            }
            text = json.dumps(projected, indent=2, ensure_ascii=False) + '\n'
            data = text.encode('utf-8')
            if len(data) <= JUDGE_NATIVE_EVIDENCE_MAX_BYTES:
                break
        # No projected_bytes field: it would have to describe the document it is
        # written into.  The file size on disk is the authority.
        destination = Path(destination)
        if destination.is_file() and destination.read_text(encoding='utf-8') == text:
            return destination
        destination.parent.mkdir(parents=True, exist_ok=True)
        temporary = destination.with_suffix(destination.suffix + '.tmp')
        temporary.write_text(text, encoding='utf-8')
        temporary.replace(destination)
        return destination
    except (OSError, ValueError, TypeError, KeyError):
        return Path(native)


# The shared Result judge applies the same 2,500,000-byte refusal to the
# trajectory it applies to the native evidence (result_judge.py:889-890).  The
# trajectory has been 20-30 KB on every dyad case so far, but it is the one
# judge input that grows with what the lower agent SAYS rather than with what
# the evaluator records, so a verbose rollout can reach the limit and lose an
# otherwise valid round.  This gives it the same bounded view the native
# evidence gets: same ceiling, same per-value clipping, same head/tail elision,
# same self-describing provenance block, same never-fail-the-round contract.
JUDGE_TRAJECTORY_PRESERVED_FIELDS = ('schema_version', 'case_id',
                                     'model_selected_actions_only')


def judge_trajectory_projection(trajectory, destination):
    """Return a judge-sized view of the captured model trajectory.

    Identical in shape to `judge_native_projection`, including the guarantee
    that any failure returns the original path untouched.  The full trajectory
    stays on disk and is named and digested inside the projection.
    """
    try:
        trajectory = Path(trajectory)
        source_bytes = trajectory.stat().st_size
        if source_bytes <= JUDGE_NATIVE_EVIDENCE_MAX_BYTES:
            return trajectory
        document = json.loads(trajectory.read_text(encoding='utf-8'))
        if not isinstance(document, dict):
            return trajectory
        source_digest = digest(trajectory)
        for per_value, head, tail in JUDGE_NATIVE_PROJECTION_STEPS:
            stats = {'clipped_strings': 0, 'clipped_bytes': 0, 'elided_entries': {}}
            projected = {}
            for key, value in document.items():
                if key in JUDGE_TRAJECTORY_PRESERVED_FIELDS:
                    projected[key] = value
                    continue
                value = _project_clip(value, per_value, stats)
                if isinstance(value, list) and len(json.dumps(value, ensure_ascii=False)) > 50_000:
                    value = _project_elide(value, head, tail, key, stats)
                projected[key] = value
            projected['evaluator_judge_projection'] = {
                'evidence_kind': 'evaluator_bounded_projection',
                'candidate_authored': False,
                'reason': 'captured trajectory exceeds the shared Result judge input limit',
                'source_trajectory_path': str(trajectory),
                'source_trajectory_sha256': source_digest,
                'source_bytes': source_bytes,
                'preserved_fields': list(JUDGE_TRAJECTORY_PRESERVED_FIELDS),
                'per_value_byte_cap': per_value,
                'kept_head_entries': head, 'kept_tail_entries': tail,
                'clipped_strings': stats['clipped_strings'],
                'clipped_bytes': stats['clipped_bytes'],
                'elided_entries': stats['elided_entries'],
            }
            text = json.dumps(projected, indent=2, ensure_ascii=False) + '\n'
            data = text.encode('utf-8')
            if len(data) <= JUDGE_NATIVE_EVIDENCE_MAX_BYTES:
                break
        destination = Path(destination)
        if destination.is_file() and destination.read_text(encoding='utf-8') == text:
            return destination
        destination.parent.mkdir(parents=True, exist_ok=True)
        temporary = destination.with_suffix(destination.suffix + '.tmp')
        temporary.write_text(text, encoding='utf-8')
        temporary.replace(destination)
        return destination
    except (OSError, ValueError, TypeError, KeyError):
        return Path(trajectory)


def case_files(record, root):
    files = {
        'case_input': owned_file(record, 'executed_task_path', root, 'executed_task_sha256'),
        'artifact': owned_file(record, 'artifact_path', root, 'artifact_sha256'),
        'trajectory': owned_file(record, 'trajectory_path', root),
        'native': owned_file(record, 'native_evidence_path', root, 'native_evidence_sha256'),
        'oracle': owned_file(record, 'private_oracle_comparison_path', root,
                             'private_oracle_comparison_sha256'),
    }
    # owned_file above has already proved this is the evaluator-owned, unchanged
    # native evidence.  The judge is handed a bounded view of it; the ceiling
    # contract below is built from the same bytes, because the shared judge
    # cross-checks native_evidence_sha256 against its own command line.
    files['native'] = judge_native_projection(
        files['native'], files['native'].with_name(files['native'].stem + '.judge-projection.json'))
    # The shared publisher hands `files['trajectory']` straight to the judge as
    # `raw_trajectory` (formal_axes_shared.py:768), and nothing downstream
    # digests it, so the bounded view replaces the entry.  Below the ceiling
    # this returns the very same path.
    files['trajectory'] = judge_trajectory_projection(
        files['trajectory'],
        files['trajectory'].with_name(files['trajectory'].stem + '.judge-projection.json'))
    contract = score_cap_contract(record, files)
    if contract is not None:
        files['score_cap_contract'] = contract
    return files


def provenance_errors(record, artifact, trajectory, case_id):
    """Authenticate model capture without turning wrong claims into N/A."""
    errors = []
    provenance = record.get('artifact_provenance') or {}
    if not (record.get('product_entry_observed') is True
            and record.get('broker_calls_delta', 0) > 0
            and record.get('broker_successful_calls_delta', 0) > 0):
        errors.append('missing real product/model execution')
    if not (provenance.get('artifact_owner') == 'model_via_dyad_typed_chat'
            and provenance.get('producer_entry') == 'model finish action captured from Dyad persisted typed chat'
            and provenance.get('evaluator_synthesized') is False
            and provenance.get('exists') is True):
        errors.append('artifact lacks model capture provenance')
    try:
        if digest(artifact) != provenance.get('sha256') or digest(artifact) != record.get('artifact_sha256'):
            errors.append('artifact capture hash mismatch')
        # D23 malformed artifact: an artifact whose bytes do not parse is the Candidate's
        # own delivery defect and must be named as such.  Folding it into
        # 'unreadable captured artifact or trajectory' put it in the same bucket as an
        # OSError on evaluator-owned evidence, which is an evaluator-side fault.
        raw = Path(artifact).read_text()
        try:
            parsed = json.loads(raw)
        except ValueError:
            parsed = None
            errors.append('artifact is not parseable JSON')
        if parsed is not None and not isinstance(parsed, dict):
            errors.append('artifact is not a JSON object')
        if trajectory is None or not Path(trajectory).is_file():
            errors.append('missing captured model trajectory')
    except (OSError, ValueError, TypeError):
        errors.append('unreadable captured artifact or trajectory')
    return errors


# D23 malformed artifact: the findings above that are read out of the Candidate's own
# artifact bytes.  Everything else provenance_errors can return is an evaluator-recorded
# fact and keeps voiding the case the way it does today.
CANDIDATE_ARTIFACT_FINDINGS = frozenset({
    'artifact is not parseable JSON',
    'artifact is not a JSON object',
})
CANDIDATE_ARTIFACT_FATAL_CLASS = 'candidate_artifact_failure'


# D56 (2026-09-21): the fatal Candidate outcomes the evaluator can only observe through
# the ABSENCE of the evidence the product never got to write.  Both map to a class the
# shared contract already knows (execution_contract.py FATAL_CANDIDATE_CLASSES), so no
# control-plane file has to change.
NO_NATIVE_EVIDENCE_FATAL_CLASSES = {
    'case_budget_exhausted': (
        'candidate_timeout',
        'The Candidate exhausted the evaluator case budget without delivering a model-authored result.',
        'completed evaluator-owned model responses before the evaluator case deadline'),
    'product_exit_without_result': (
        'candidate_product_failure',
        'The Candidate product entry ended without delivering a model-authored result.',
        'completed evaluator-owned model responses before the product entry ended'),
}


def normalize_execution(record, *, candidate_digest, case_id, output, evidence_root):
    value = dict(record)
    value.update(case_id=case_id, candidate_digest=candidate_digest,
                 execution_attempted=True,
                 real_execution=record.get('product_entry_observed') is True,
                 broker_delta={'calls': record.get('broker_calls_delta', 0),
                               'successful_calls': record.get('broker_successful_calls_delta', 0)})
    if record.get('classification') not in ('valid', 'candidate_failure'):
        value.update(infra_valid=False, classification='infrastructure_invalid')
        return value
    if (record.get('failure_class') in NO_NATIVE_EVIDENCE_FATAL_CLASSES
            and not record.get('artifact_present')
            and record.get('broker_successful_calls_delta', 0) > 0):
        # The evaluator's own per-case deadline cut the Candidate off mid-flight,
        # before the evaluator's launcher could persist the native evidence, the
        # trajectory or the artifact.  The Candidate consumed a healthy
        # evaluator-owned model budget and delivered no result: a fatal Candidate
        # failure.  Attributing it must not require the very evidence the deadline
        # prevented from being written, so this runs before any owned_file call.
        evidence = [Path(output).resolve()]
        capture = (record.get('artifact_provenance') or {}).get('evaluator_capture')
        if isinstance(capture, str) and Path(capture).is_file():
            evidence.append(Path(capture).resolve())
        # Once the case-deadline reserve lets the launcher persist what it had,
        # a timed-out case also carries native evidence and a trajectory.  Bind
        # whatever actually reached disk; none of it is required.
        for key in ('native_evidence_path', 'trajectory_path'):
            extra = record.get(key)
            if isinstance(extra, str) and Path(extra).is_file():
                evidence.append(Path(extra).resolve())
        fatal_class, fatal_reason, preflight_evidence = NO_NATIVE_EVIDENCE_FATAL_CLASSES[
            record.get('failure_class')]
        value['environment_preflight'] = {'valid': True, 'evidence': preflight_evidence}
        value.update(classification=fatal_class, failure_attribution={
            'party': 'candidate', 'observed_by': 'evaluator', 'fatal': True,
            'reason': fatal_reason,
            'evidence_paths': [str(item) for item in evidence]})
        return value
    try:
        native = owned_file(record, 'native_evidence_path', evidence_root, 'native_evidence_sha256')
        captured = json.loads(native.read_text())
        if captured.get('real_product') is not True or captured.get('case_id') != case_id:
            raise ValueError('native product/case identity mismatch')
        task = owned_file(record, 'executed_task_path', evidence_root, 'executed_task_sha256')
        if (captured.get('executed_task_sha256') != digest(task)
                or captured.get('private_oracle_visible') is not False):
            raise ValueError('native capture task/privacy identity mismatch')
        oracle = owned_file(record, 'private_oracle_comparison_path', evidence_root,
                            'private_oracle_comparison_sha256')
        comparison = json.loads(oracle.read_text())
        if (comparison.get('case_id') != case_id or comparison.get('private_oracle_not_candidate_visible') is not True
                or comparison.get('executed_task_sha256') != digest(task)):
            raise ValueError('private comparison is not bound to the executed task')
        healthy = record.get('broker_successful_calls_delta', 0) > 0
        value['environment_preflight'] = {'valid': healthy,
            'evidence': 'actual product entry and completed evaluator-owned model response'}
        if (record.get('failure_class') == 'model_authored_artifact_missing'
                and not record.get('artifact_present') and healthy):
            value.update(classification='candidate_artifact_failure', failure_attribution={
                'party': 'candidate', 'observed_by': 'evaluator', 'fatal': True,
                'reason': 'The product completed model execution without delivering the required model-authored result.',
                'evidence_paths': [str(Path(output).resolve()), str(native)]})
            return value
        files = case_files(record, evidence_root)
        if captured.get('agent_artifact_origin') != 'model_finish_action':
            raise ValueError('native capture does not establish a model finish artifact')
        errors = provenance_errors(record, files['artifact'], files['trajectory'], case_id)
        if errors and healthy and all(error in CANDIDATE_ARTIFACT_FINDINGS for error in errors):
            # D23 malformed artifact: the product delivered a result file that violates the
            # disclosed contract after a healthy, evaluator-observed model execution.  That
            # is this case's fatal Candidate failure, not unresolved evidence, and it must
            # not void the Result axis for the other five cases.
            value.update(classification=CANDIDATE_ARTIFACT_FATAL_CLASS, failure_attribution={
                'party': 'candidate', 'observed_by': 'evaluator', 'fatal': True,
                'reason': 'The product-authored result violates the disclosed artifact contract: '
                          + '; '.join(errors) + '.',
                'evidence_paths': [str(Path(files['artifact']).resolve()), str(native)]})
            return value
        if errors:
            raise ValueError('; '.join(errors))
        value['artifact_validation'] = {'validated_by': 'evaluator', 'valid': True,
                                         'sha256': digest(files['artifact'])}
    except (OSError, ValueError, TypeError, KeyError) as exc:
        value.update(classification='unresolved', normalization_error=str(exc))
    return value


# --- judge resample (2026-09-20) ---------------------------------------------
# A shared Result judge that completed one paid-for response and then refused to
# publish it is an evaluator-side SAMPLING fault: the apparatus is healthy, the
# sample is not.  Booking it as infrastructure loses the Candidate's public
# round -- and on the ai-scientist tree it latched a terminal that ended the
# whole formal run (0905-edit-codex-xhigh-0919-fw-001-ai-scientist, 03:04 CST:
# deepseek-flash emitted a complete 100/100 verdict missing exactly one `}`,
# result_judge.close_unclosed_json appended it at the END of the text instead of
# at the container the model left open, and the misnested parse produced 31
# validation errors).  Across the 121 result contracts on this host 8 of the 65
# real judge calls are `model_output_invalid` -- about 12% -- so this is a
# routine event, not a freak one.
#
# The hidden axis already recovers by setting the refused attempt aside and
# judging again (see <run>/formal_scoring/result_axis/<case>.attempt-001-invalid
# on this host).  This gives the public round the same remedy, automatically and
# exactly once.  Nothing measured is repeated: the Candidate product is not
# re-executed, no lower-agent call is re-issued, the immutable evidence inputs
# are reused byte for byte, and the refused attempt is renamed rather than
# deleted so both verdicts stay auditable.  The control plane's
# one-response-per-immutable-directory contract (execution_scoring.py:96-116,
# result_judge.py:943-946) is preserved because the retry gets a fresh
# directory.
RESAMPLEABLE_JUDGE_STATES = {"model_output_invalid"}
JUDGE_RESAMPLE_ATTEMPTS = 1
# Everything the shared judge (result_judge.py:885-1016), the shared scorer
# (execution_scoring.py:96-119) and the task-local once-guards author inside the
# judge directory.  Any OTHER file there was written by this caller before the
# judge ran and is restored byte for byte into the fresh attempt.
JUDGE_AUTHORED_FILES = (
    "scoring_intent.json", "input_manifest.json", "judge_prompt.txt",
    "logical_request_started.json", "provider_response.json",
    "provider_response-attempts.json", "model_response.json",
    "result_eval_result.json", "result_score_contract.json",
    "task_judge_intent.json", "task_judge_invocation.json",
    "task_cli_stdout.log", "task_cli_stderr.log",
)


def refused_judge_state(judge_dir):
    """The judge's own evaluation_state when it answered and refused to publish.

    None keeps today's behaviour untouched.  A resample is offered only when the
    contract proves the apparatus worked: exactly one completed response with
    complete usage accounting, refused over the model's output rather than over
    the infrastructure.  A missing, unreadable, transport-failed or
    ``infrastructure_error`` contract returns None and stays terminal.
    """
    import json as _json
    from pathlib import Path as _Path
    try:
        contract = _json.loads(
            (_Path(judge_dir) / "result_score_contract.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if not isinstance(contract, dict) or contract.get("contract_valid") is True:
        return None
    usage = contract.get("provider_usage")
    if not isinstance(usage, dict):
        return None
    if usage.get("completed_responses") != 1 or usage.get("usage_complete") is not True:
        return None
    state = contract.get("evaluation_state")
    return state if state in RESAMPLEABLE_JUDGE_STATES else None


def resample_refused_verdict(judge_dir, rejudge, *, recreate_dir=True):
    """Ask the evaluator's own Result judge again, in a new immutable directory.

    Returns the new judge outcome, or None when nothing was resampled and the
    caller must keep the outcome it already holds.  ``recreate_dir`` is False
    for callers whose own once-guard creates the directory with
    ``mkdir(exist_ok=False)``.
    """
    import json as _json
    import shutil as _shutil
    from pathlib import Path as _Path
    judge_dir = _Path(judge_dir)
    outcome = None
    for attempt in range(1, JUDGE_RESAMPLE_ATTEMPTS + 1):
        state = refused_judge_state(judge_dir)
        if state is None:
            return outcome
        retired = judge_dir.parent / ("%s.attempt-%03d-%s" % (judge_dir.name, attempt, state))
        if retired.exists() or not judge_dir.is_dir():
            return outcome
        judge_dir.rename(retired)
        restored = []
        if recreate_dir:
            judge_dir.mkdir(parents=True)
            for item in sorted(retired.iterdir()):
                if item.name in JUDGE_AUTHORED_FILES or item.name.startswith("provider_response-attempt-"):
                    continue
                (_shutil.copytree if item.is_dir() else _shutil.copy2)(item, judge_dir / item.name)
                restored.append(item.name)
        (judge_dir.parent / ("judge_resample_%03d.json" % attempt)).write_text(
            _json.dumps({
                "schema_version": "agentswe-edit-judge-resample-v1",
                "attempt": attempt,
                "judge_dir": str(judge_dir),
                "retired_attempt": str(retired),
                "retired_evaluation_state": state,
                "restored_caller_inputs": restored,
                "candidate_product_re_executed": False,
                "lower_agent_calls_re_issued": 0,
                "reason": "the shared Result judge completed one response and refused to publish "
                          "it; the evaluator's own judge is resampled into a new immutable "
                          "directory and the refused attempt is retained",
            }, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
        outcome = rejudge()
    return outcome


def score_public(record, *, output, evidence_root, rubric, broker_endpoint):
    # The shared scorer owns immutable scoring intent and terminal validation.
    # Infrastructure and missing provenance never trigger a semantic request.
    paths = {key: record.get(field) for key, field in (
        ('case_input', 'executed_task_path'), ('artifact', 'artifact_path'),
        ('raw_trajectory', 'trajectory_path'), ('native_evidence', 'native_evidence_path'),
        ('private_oracle', 'private_oracle_comparison_path'))}
    # Same bounded view the formal path hands the judge; case_files below derives
    # the identical destination from the identical source, so both agree.
    if paths.get('native_evidence'):
        _native = Path(paths['native_evidence'])
        paths['native_evidence'] = str(judge_native_projection(
            _native, _native.with_name(_native.stem + '.judge-projection.json')))
    if paths.get('raw_trajectory'):
        _trajectory = Path(paths['raw_trajectory'])
        if _trajectory.is_file():
            paths['raw_trajectory'] = str(judge_trajectory_projection(
                _trajectory, _trajectory.with_name(_trajectory.stem + '.judge-projection.json')))
    caps = None
    try:
        caps = score_cap_contract(record, case_files(record, evidence_root))
    except (OSError, ValueError, TypeError, KeyError):
        caps = None
    def rejudge():
        return judge_execution_case(**paths, rubric=rubric, execution_record=record,
            candidate_digest=record['candidate_digest'], case_id=record['case_id'],
            output=output, broker_endpoint=broker_endpoint, score_cap_contract=caps)

    judgement = rejudge()
    again = resample_refused_verdict(output, rejudge)
    return judgement if again is None else again


def public_contract_checks(record):
    """The published cross-cutting contract verdicts for a public round.

    input/02 "independently verifiable product contract" states these seven obligations and promises
    the Builder gets them back case by case, so the public round is a real
    calibration channel instead of a guess.  Only the published check names and
    their booleans travel: no fixture bytes, no expected state, no oracle value.
    A missing or unreadable comparison yields no key at all.
    """
    raw = record.get('private_oracle_comparison_path')
    if not isinstance(raw, str) or not raw:
        return None
    try:
        comparison = json.loads(Path(raw).read_text(encoding='utf-8'))
    except (OSError, ValueError):
        return None
    checks = comparison.get('checks') if isinstance(comparison, dict) else None
    if not isinstance(checks, dict):
        return None
    published = {key[len('contract:'):]: value is True
                 for key, value in checks.items() if key.startswith('contract:')}
    return published or None


PUBLIC_DISPATCH_ERRORS_REPORTED = 8
PUBLIC_DISPATCH_ERROR_CHARS = 240


def public_execution_diagnostics(record):
    """Bounded, evaluator-owned facts about how the driven rollout actually ended.

    Without this block every zeroed public case reads as the same sentence --
    "The product completed model execution without delivering the required
    model-authored result" -- whether the product failed, the driving model
    emitted an unusable response, or the evaluator's own dispatcher could not
    deliver the action (0920-fh-002: dev_mean 0.0 in both rounds, and the
    Builder had nothing to iterate on).  Nothing here comes from the private
    oracle: it is the dispatch trajectory the Builder's own product answered,
    that product's own error strings, and the driver's own transport state.
    """
    raw = record.get('native_evidence_path')
    if not isinstance(raw, str) or not raw:
        return None
    try:
        native = json.loads(Path(raw).read_text(encoding='utf-8'))
    except (OSError, ValueError):
        return None
    if not isinstance(native, dict):
        return None
    trajectory = native.get('product_action_trajectory')
    trajectory = trajectory if isinstance(trajectory, list) else []
    rejected = []
    for item in trajectory:
        if not isinstance(item, dict):
            continue
        result = item.get('result')
        detail = None
        if item.get('dispatch_status') == 'error' and isinstance(result, dict):
            detail = result.get('action_error')
        elif isinstance(result, dict) and result.get('ok') is False:
            error = result.get('error')
            detail = error.get('message') if isinstance(error, dict) else error
        if not isinstance(detail, str) or not detail.strip():
            continue
        rejected.append({'sequence': item.get('sequence'), 'action': item.get('action'),
                         'rejected_with': detail[:PUBLIC_DISPATCH_ERROR_CHARS]})
    reprompts = native.get('model_driver_reprompts')
    reprompts = reprompts if isinstance(reprompts, list) else []
    return {
        'model_actions_dispatched': len(trajectory),
        'actions_rejected': len(rejected),
        'rejected_dispatches': rejected[:PUBLIC_DISPATCH_ERRORS_REPORTED],
        'action_protocol_complete': native.get('action_protocol_complete'),
        'malformed_or_missing_model_action': native.get('malformed_or_missing_model_action'),
        'model_response_transport_errors': native.get('model_response_transport_errors'),
        'evaluator_driver_reprompts': len(reprompts),
    }


def public_case_reason(record, diagnostics, voided):
    """One stated sentence per case: what decided this case's evaluation."""
    semantic = record.get('semantic_feedback') or {}
    if voided:
        return ('This case was not evaluated: the evaluator/provider side failed ('
                + str(record.get('failure_class') or record.get('classification'))
                + '). It carries no score, it is excluded from dev_mean, and your product '
                  'was not judged on it.')
    diagnostics = diagnostics or {}
    if diagnostics.get('malformed_or_missing_model_action') is True:
        if (diagnostics.get('model_response_transport_errors') or 0) > 0:
            return ('The evaluator driver lost the model response for this case (transport error '
                    'on the evaluator side) after '
                    + str(diagnostics.get('model_actions_dispatched', 0))
                    + ' dispatched action(s); no further product action could be issued.')
        return ('The driving model produced no dispatchable action marker after '
                + str(diagnostics.get('model_actions_dispatched', 0))
                + ' dispatched action(s), so the rollout ended before a result was delivered.')
    if record.get('artifact_present') is not True and diagnostics.get('actions_rejected'):
        return (str(diagnostics['actions_rejected']) + ' of '
                + str(diagnostics.get('model_actions_dispatched', 0))
                + ' dispatched actions were refused by your product\'s own handlers, and no '
                  'model-authored result was delivered; the refusals are listed above.')
    for key in ('reason', 'assessment'):
        text = semantic.get(key)
        if isinstance(text, str) and text.strip():
            return text
    return str(record.get('failure_class') or record.get('classification') or 'no evaluation recorded')


PUBLIC_CAP_FIELDS = ('cap_id', 'maximum_score', 'status', 'requirement_ref', 'reason')


def public_score_caps(semantic):
    """The evaluator-issued ceilings this public case was actually judged under.

    score_public() already hands the public judge the SAME cap contract the hidden
    axis issues (score_cap_contract() above -> evaluator/result_score_caps.py), but
    none of it came back: the Builder saw a capped score with no statement of which
    ceiling bound it or why.  The shared judge records its decision in the Result
    contract it writes (result_judge.py:1042-1043 `score_cap` / `score_cap_conditions`,
    plus its own `ceiling_assessments` and `errors`), so the contract is the source
    rather than a second, possibly divergent, computation here.

    Only the ceiling id, its value, its published requirement reference and the stated
    reason travel, with the judge's own harness error strings verbatim.  No hidden
    case, no private oracle expectation, no evaluator path -- `evidence_refs` are
    dropped on purpose and the caller's clean() still scrubs anything path-shaped.
    """
    raw = semantic.get('contract_path')
    if not isinstance(raw, str) or not raw:
        return None
    try:
        contract = json.loads(Path(raw).read_text(encoding='utf-8'))
    except (OSError, ValueError):
        return None
    if not isinstance(contract, dict) or 'score_cap_conditions' not in contract:
        return None
    conditions = contract.get('score_cap_conditions')
    assessments = contract.get('ceiling_assessments')
    errors = contract.get('errors')
    return {
        'score_cap': contract.get('score_cap'),
        'score_cap_conditions': [{key: entry.get(key) for key in PUBLIC_CAP_FIELDS}
                                 for entry in (conditions if isinstance(conditions, list) else [])
                                 if isinstance(entry, dict)],
        'ceiling_assessments': assessments if isinstance(assessments, list) else [],
        'harness_errors': [str(item) for item in errors] if isinstance(errors, list) else [],
    }


def public_case_feedback(record):
    semantic = record.get('semantic_feedback') or {}
    value = {key: record.get(key) for key in ('case_id', 'classification', 'failure_class',
        'artifact_present', 'product_entry_observed', 'acceptance_surface_observed',
        'acceptance_chain_complete', 'broker_calls_delta')}
    contract = public_contract_checks(record)
    if contract is not None:
        value['product_contract_checks'] = contract
    diagnostics = public_execution_diagnostics(record)
    if diagnostics is not None:
        value['execution_diagnostics'] = diagnostics
    # Every case states a score and a reason, and says whether the score is the
    # product's or absent because the evaluator voided the case.
    scored = (semantic.get('round_consumed') is True and type(semantic.get('score')) is int)
    voided = (record.get('infra_valid') is False
              and (record.get('attribution') or {}).get('owner') == 'evaluator/provider')
    value['evaluation'] = {
        'scored': scored,
        'score': semantic.get('score') if scored else None,
        'counts_toward_dev_mean': scored,
        'evaluation_voided_by_evaluator': bool(voided and not scored),
        'reason': public_case_reason(record, diagnostics, voided and not scored),
    }
    value['result'] = {key: semantic.get(key) for key in ('classification', 'score',
        'contract_valid', 'round_consumed', 'reason', 'assessment', 'major_errors', 'dimensions')}
    caps = public_score_caps(semantic)
    if caps is not None:
        value['score_caps'] = caps
    # Assessment is public-dev feedback; evaluator file paths remain private.
    import re
    def clean(item):
        if isinstance(item, dict): return {k: clean(v) for k, v in item.items()}
        if isinstance(item, list): return [clean(v) for v in item]
        if isinstance(item, str):
            return re.sub(r"/(?:home|data|run/secrets)/[^\s\"'<>]+", '[evaluator-private-path]', item)
        return item
    return clean(value)
