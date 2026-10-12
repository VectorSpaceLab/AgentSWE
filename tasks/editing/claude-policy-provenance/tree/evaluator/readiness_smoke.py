"""Independent Claude Result/Code readiness smoke.

The two judges are dispatched by `formal_finalize.finalize` in acceptance mode
over the single after-freeze hidden case, writing into readiness roots rather
than the formal ones. Reusing that function is deliberate: it owns the case
evidence resolution, the oracle/native binding checks and the one-logical-request
contract checks, and a second copy of those would drift from it.

This module adds only what readiness needs on top: the run may not replay, the
outputs are never publishable, and each judge's answer is wrapped with the
independent identity the coordinator's bundle validator requires.
"""
from __future__ import annotations

import hashlib
import json
from pathlib import Path
import sys
import urllib.request

PROFILE = 'single-dev-two-round-hidden-smoke-v1'
SHARED_ROOT = Path('@@AGENTSWE_EDITING_CONTROL@@')
CODE_VALIDATOR = SHARED_ROOT / 'code_eval.py'
CODE_AXIS_SKIP_POLICY = {'id': 'edit-code-axis-retired-2026-09-19', 'evaluation_state': 'skipped_by_policy', 'reason': 'Result-only evaluation; Code judge not dispatched'}
CASE = 'test_001'


def write(path: Path, value) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, sort_keys=True, indent=2) + '\n')


def ref(path: Path) -> dict:
    return {'path': str(path), 'sha256': hashlib.sha256(Path(path).read_bytes()).hexdigest()}


def judge_payload(model_path):
    """The Result judge's own parsed verdict, not a second parse of its raw answer.

    D18 (2026-09-21) lets the shared judge repair a malformed answer ENVELOPE --
    unclosed or surplus closing delimiters, a non-JSON trailer, unknown top-level
    keys -- publish a valid contract and record the repair as `envelope_repair`,
    while `model_response.json` keeps the VERBATIM bytes the model sent.  Parsing
    those bytes a second time here raised JSONDecodeError and killed a whole
    readiness run for a case the judge had already scored (aider 0921-v4-002,
    result_score 99, DSML tool-call markup after a complete object).

    No repair recorded -> nothing changes: the verbatim answer is parsed exactly
    as before.  A repair recorded -> the judge's own validated verdict
    (`result_eval_result.json`) is used, but only after the contract proves it is
    publishable and its `model_response_digest` still binds the verbatim bytes on
    disk, which is the same file and the same digest the judge itself bound.  An
    invalid contract stays invalid: the original decode error is re-raised and the
    caller fails exactly as it does today.  `model_response.json` is never
    rewritten and stays the raw evidence `raw_model_output` digests.
    """
    model_path = Path(model_path)
    raw = model_path.read_bytes()
    verdict, decode_error = None, None
    try:
        verdict = json.loads(raw.decode("utf-8"))
    except ValueError as exc:
        decode_error = exc

    def verbatim():
        if decode_error is not None:
            raise decode_error
        if not isinstance(verdict, dict):
            raise ValueError("judge answer must be one JSON object: %s" % model_path)
        return verdict

    try:
        contract = json.loads(
            (model_path.parent / "result_score_contract.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return verbatim()
    repair = contract.get("envelope_repair") if isinstance(contract, dict) else None
    if not isinstance(repair, dict) or repair.get("applied") is not True:
        return verbatim()
    # The judge writes the answer plus exactly one newline and digests the answer.
    digest = hashlib.sha256(raw[:-1]).hexdigest() if raw.endswith(b"\n") else None
    if (contract.get("contract_valid") is not True
            or contract.get("result_score_publishable") is not True
            or contract.get("evaluation_state") != "scoreable"
            or contract.get("errors") != []
            or digest is None
            or contract.get("model_response_digest") != digest
            or repair.get("original_sha256") != digest):
        return verbatim()
    repaired = json.loads(
        (model_path.parent / "result_eval_result.json").read_text(encoding="utf-8"))
    if (not isinstance(repaired, dict)
            or repaired.get("case_id") != contract.get("case_id")
            or repaired.get("result_state") != contract.get("result_state")
            or repaired.get("result_score") != contract.get("result_score")):
        raise ValueError("repaired judge verdict disagrees with its contract: %s" % model_path)
    # `arithmetic_repair` is the judge's own repair record, not part of the answer
    # envelope the shared bundle validator re-checks; the contract keeps it.
    return {key: value for key, value in repaired.items() if key != "arithmetic_repair"}


def read(path: Path) -> dict:
    value = json.loads(Path(path).read_text())
    if not isinstance(value, dict):
        raise ValueError('Claude readiness evidence must be an object')
    return value


def broker_stats(endpoint: str) -> dict:
    request = urllib.request.Request(
        endpoint.rsplit('/v1/responses', 1)[0] + '/stats',
        headers={'Authorization': 'Bearer stats-only-placeholder'})
    with urllib.request.urlopen(request, timeout=10) as response:
        return json.loads(response.read())


def _result_dimension_maxima(task_root, rubric_relative_path):
    """Exactly the maxima readiness_judge_validation recomputes from this task's rubric."""
    import importlib.util
    if str(SHARED_ROOT) not in sys.path:
        sys.path.insert(0, str(SHARED_ROOT))  # result_judge imports responses_stream from the control plane
    spec = importlib.util.spec_from_file_location('readiness_result_judge', str(SHARED_ROOT / 'result_judge.py'))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module.load_dimensions(module.rubric_dimensions_path(Path(task_root) / rubric_relative_path))


def export_judge_observations(run_dir: Path, freeze_path: Path, output: Path,
                              before: dict, after: dict) -> dict:
    before_ids = {row.get('request_id') for row in before.get('attempts', [])}
    fresh = [row for row in after.get('attempts', [])
             if row.get('request_id') not in before_ids]
    if len(fresh) != 1 or not fresh[0].get('request_id'):
        raise ValueError('readiness Result smoke lacks one fresh request identity')
    result_id = fresh[0]['request_id']
    code_id = None  # Code axis retired 2026-09-19 (Result-only)
    freeze_sha = ref(freeze_path)['sha256']
    models = {'result': output / 'result' / CASE / 'model_response.json',
              'code': output / 'code/code_model_response.json'}
    validators = {'result': SHARED_ROOT / 'result_judge.py', 'code': CODE_VALIDATOR}
    identities = {'result': 'result:' + result_id, 'code': None}
    requests = {'result': result_id, 'code': code_id}
    inputs = {'result': output / 'result_validation_input.json',
              'code': output / 'code_validation_input.json'}
    observations = {}
    for role in ('result',):
        model = models[role]
        if not model.is_file():
            raise ValueError('readiness judge answer bytes are missing: ' + role)
        output_path = output / (role + '_raw_output.json')
        write(output_path, {'role': role, 'judge_session_id': identities[role],
                            'raw_model_output': ref(model), 'payload': judge_payload(model)})
        observation = output / (role + '_observation.json')
        write(observation, {
            'owner': 'evaluator', 'run_id': run_dir.name, 'role': role,
            'judge_session_id': identities[role], 'request_id': requests[role],
            'request_identity_kind': 'broker_request_id' if role == 'result' else 'provider_response_id',
            'model': 'deepseek-flash', 'effort': 'max', 'state': 'terminal',
            'formal': False, 'freeze_sha256': freeze_sha,
            'input': ref(inputs[role]), 'output': ref(output_path),
            'validator_source_sha256': ref(validators[role])['sha256']})
        observations[role] = ref(observation)
    # Code axis retired 2026-09-19 (Result-only): explicit evaluator-written skip observation.
    write(output / 'code_observation.json', {'owner': 'evaluator', 'run_id': run_dir.name, 'role': 'code',
        'state': 'skipped_by_policy', 'formal': False, 'judge_session_id': None, 'request_id': None,
        'freeze_sha256': freeze_sha, 'policy': CODE_AXIS_SKIP_POLICY})
    write(output / 'judge_observations.json', observations)
    return observations


def run(*, task_root, run_dir, credential, result_endpoint):
    task_root, run_dir = Path(task_root), Path(run_dir)
    sys.path.insert(0, str(task_root / 'evaluator'))
    from formal_finalize import finalize

    freeze_path = run_dir / 'lifecycle/freeze_manifest.json'
    freeze = read(freeze_path)
    if freeze.get('readiness_profile') != PROFILE:
        raise ValueError('readiness smoke requires an explicitly bound readiness freeze')
    output = run_dir / 'readiness_scoring'
    if output.exists():
        raise RuntimeError('readiness judge output already exists; no replay')
    output.mkdir(parents=True, exist_ok=False)

    freeze_sha = ref(freeze_path)['sha256']
    write(output / 'result_validation_input.json',
          {'role': 'result', 'freeze_sha256': freeze_sha, 'case': CASE,
           'rubric_relative_path': 'evaluator/rubric.md',
           # The shared validator binds the judge source and recomputes the rubric
           # dimensions from this task's own rubric; both must be in the input artifact.
           'validator_source_sha256': ref(SHARED_ROOT / 'result_judge.py')['sha256'],
           'dimension_maxima': _result_dimension_maxima(task_root, 'evaluator/rubric.md')})
    write(output / 'code_validation_input.json',
          {'role': 'code', 'freeze_sha256': freeze_sha,
           'candidate_digest': freeze.get('candidate_digest')})

    before = broker_stats(result_endpoint)
    code, aggregation = finalize(
        run_dir, output=run_dir / 'readiness_aggregation.json',
        result_judge_broker_endpoint=result_endpoint,
        credential_file=Path(credential).resolve(),
        acceptance_cases=[CASE],
        scoring_dirs={'result': output / 'result', 'code': output / 'code'},
        # Readiness evidence is that the judging pipeline works, so a case the
        # formal path would short-circuit to zero is still judged here.
        dispatch_zero_cases=True)
    after = broker_stats(result_endpoint)
    write(output / 'result_broker_attestation.json', {'before': before, 'after': after})

    complete = code == 0 and aggregation.get('acceptance_complete') is True
    summary = {
        'profile': PROFILE, 'evaluation_mode': 'readiness_smoke', 'score_threshold': None,
        'formal_result_publishable': False, 'formal_complete': False,
        'code_score_publishable': False,
        'readiness_judges_complete': complete,
        'finalizer_exit': code,
        'errors': {'result': [] if complete else aggregation.get('reasons', []),
                   'code': []},
    }
    if complete:
        summary['observations'] = export_judge_observations(
            run_dir, freeze_path, output, before, after)
    write(run_dir / 'readiness_judge_smoke.json', summary)
    return summary
