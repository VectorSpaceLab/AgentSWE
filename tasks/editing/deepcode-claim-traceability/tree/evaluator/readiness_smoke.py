"""Independent judge smoke; functional zero never skips either judge invocation."""
from __future__ import annotations

import hashlib
import json
from pathlib import Path
import subprocess
import sys

PROFILE = 'single-dev-two-round-hidden-smoke-v1'
CODE_AXIS_SKIP_POLICY = {'id': 'edit-code-axis-retired-2026-09-19', 'evaluation_state': 'skipped_by_policy', 'reason': 'Result-only evaluation; Code judge not dispatched'}
SHARED_ROOT = Path('@@AGENTSWE_EDITING_CONTROL@@')


RUBRIC_RELATIVE = 'evaluator/agentloop_result_rubric.md'


def result_dimension_maxima(judge_path, rubric_relative_path):
    """Return exactly the maxima the shared judge validator will expect.

    ``readiness_judge_validation`` recomputes these from this task's own rubric
    and refuses the bundle unless the bound value matches exactly, so they are
    derived through that same judge module, from the same relative path written
    into the observation. A task with no ``result_dimensions.json`` keeps the
    legacy 50/30/20 contract, which the authority expresses as ``None`` -- not
    as a map read back off the score contract.
    """
    import importlib.util
    spec = importlib.util.spec_from_file_location('readiness_result_judge', str(judge_path))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    rubric = Path(__file__).resolve().parents[1] / rubric_relative_path
    return module.load_dimensions(module.rubric_dimensions_path(rubric))


def write(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, sort_keys=True, indent=2) + '\n')


def ref(path):
    return {'path': str(path), 'sha256': hashlib.sha256(path.read_bytes()).hexdigest()}


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


def export_judge_observations(run_dir, freeze_path, output, broker_after, *, result_contract, code_contract):
    """Wrap unchanged actual outputs and their validation inputs for the coordinator."""
    freeze_sha = ref(freeze_path)['sha256']
    run_id = run_dir.name
    result_attempts = broker_after.get('attempts', [])
    if len(result_attempts) != 1 or not result_attempts[0].get('request_id'):
        raise ValueError('independent Result smoke lacks its unique broker request identity')
    result_id = 'result:' + result_attempts[0]['request_id']
    code_request = None  # Code axis retired 2026-09-19 (Result-only)
    validators = {'result': SHARED_ROOT / 'result_judge.py',
        'code': Path('@@AGENTSWE_EDITING_CONTROL@@/code_eval.py')}
    result_input_path = run_dir / 'readiness_judge_inputs/input_binding.json'
    binding = json.loads(result_input_path.read_text())
    native_contract_inputs = result_contract.get('input_files') or result_contract.get('inputs') or {}
    dimensions = result_dimension_maxima(SHARED_ROOT / 'result_judge.py', RUBRIC_RELATIVE)
    observations = {}
    for role, identity, model_path, role_inputs in (
        ('result', result_id, output / 'result/model_response.json', {
            'dimension_maxima': dimensions, 'rubric_relative_path': RUBRIC_RELATIVE,
            'input_binding': ref(result_input_path),
            'bound_evidence': binding, 'contract_inputs': native_contract_inputs}),
        ):
        input_path = output / (role + '_validation_input.json')
        output_path = output / (role + '_raw_output.json')
        write(input_path, {'role': role, 'freeze_sha256': freeze_sha,
            'validator_source_sha256': ref(validators[role])['sha256'], **role_inputs})
        write(output_path, {'role': role, 'judge_session_id': identity,
            'raw_model_output': ref(model_path), 'payload': judge_payload(model_path)})
        observation_path = output / (role + '_observation.json')
        write(observation_path, {'owner': 'evaluator', 'run_id': run_id, 'role': role,
            'judge_session_id': identity, 'request_id': result_attempts[0]['request_id'] if role == 'result' else code_request,
            'request_identity_kind': 'broker_request_id' if role == 'result' else 'provider_response_id',
            'model': 'deepseek-flash', 'effort': 'max', 'state': 'terminal', 'formal': False,
            'freeze_sha256': freeze_sha, 'input': ref(input_path), 'output': ref(output_path)})
        observations[role] = ref(observation_path)
    # Code axis retired 2026-09-19 (Result-only): explicit evaluator-written skip observation.
    code_observation = output / 'code_observation.json'
    write(code_observation, {'owner': 'evaluator', 'run_id': run_id, 'role': 'code', 'state': 'skipped_by_policy',
        'formal': False, 'judge_session_id': None, 'request_id': None, 'freeze_sha256': freeze_sha,
        'policy': CODE_AXIS_SKIP_POLICY})
    observations['code'] = ref(code_observation)
    write(output / 'judge_observations.json', observations)
    return observations


def dispatch_pair(result_command, code_command, output, runner=None):
    """One dispatch per role, exclusive run namespace, no retry or score threshold."""
    output.mkdir(parents=True, exist_ok=False)
    runner = runner or subprocess.run
    write(output / 'dispatch_intent.json', {'profile': PROFILE,
        'evaluation_mode': 'readiness_smoke', 'formal_result_publishable': False,
        'commands': {'result': result_command, 'code': code_command}})
    exits = {}
    for role, command in [(r, c) for r, c in [('result', result_command), ('code', code_command)] if c is not None]:
        completed = runner(command, capture_output=True, text=True, check=False)
        (output / (role + '.stdout.log')).write_text(completed.stdout)
        (output / (role + '.stderr.log')).write_text(completed.stderr)
        exits[role] = completed.returncode
    write(output / 'dispatch_terminal.json', exits)
    return exits


def run(*, task_root, run_dir, hidden, credential, result_endpoint):
    sys.path.insert(0, str(task_root / 'evaluator'))
    import formal_axes
    shared = formal_axes.SHARED
    freeze_path, freeze = shared['freeze_document'](run_dir)
    if freeze.get('readiness_profile') != PROFILE:
        raise ValueError('readiness smoke requires an explicitly bound readiness freeze')
    from evaluator.harness.candidate_adapter import tree_digest
    repository=Path(freeze['candidate_path'])
    if not repository.is_dir() or not repository.resolve().is_relative_to(run_dir.resolve()) or tree_digest(repository)!=freeze['candidate_digest']:
        raise ValueError('DeepCode frozen repository identity invalid')
    errors = shared['hidden_lifecycle_errors'](hidden, freeze, run_dir, ('test_001',))
    if errors:
        raise ValueError('; '.join(errors))
    record = shared['find_case_record'](hidden, 'test_001')
    if shared['infrastructure_reason'](record):
        raise ValueError('infrastructure-invalid hidden execution cannot enter judge smoke')
    output = run_dir / 'readiness_scoring'
    if output.exists():
        raise RuntimeError('readiness judge output already exists; no replay')
    preparation = run_dir / 'readiness_judge_inputs'
    preparation.mkdir(exist_ok=False)
    paths = {name: Path(record[key]) for name, key in (
        ('task', 'executed_task_path'), ('trajectory', 'observed_trajectory_path'),
        ('oracle', 'private_oracle_comparison_path'), ('native', 'execution_record_path'))}
    for name, path in paths.items():
        if not path.is_file() or path.is_symlink() or not path.resolve().is_relative_to(run_dir.resolve()):
            raise ValueError('missing or foreign judge evidence: ' + name)
    artifact = Path(record.get('artifact_provenance', {}).get('artifact_path') or '')
    if not artifact.is_file():
        # Explicit evaluator observation, never presented as Candidate-authored output.
        artifact = preparation / 'artifact_absence_observation.json'
        write(artifact, {'evidence_kind': 'evaluator_observed_absence',
            'candidate_authored': False, 'artifact_present': False,
            'execution_record': ref(paths['native']), 'classification': record.get('classification')})
    elif artifact.is_symlink() or not artifact.resolve().is_relative_to(run_dir.resolve()):
        raise ValueError('artifact escaped evaluator-owned run')
    bridge = shared['code_frozen_identity'](freeze, run_dir)
    digest = bridge['code_candidate_digest']
    requirements = shared['public_requirements'](preparation / 'public_requirements')
    rubric = task_root / 'evaluator/agentloop_result_rubric.md'
    code_rubric = task_root / 'evaluator/code_rubric.md'
    if not code_rubric.is_file():
        code_rubric = task_root / 'agentloop/evaluator/code_rubric.md'
    # 2026-09-21: the Result judge sees a bounded projection of the trajectory and of the
    # private oracle comparison; the originals stay on disk as evidence and the projected
    # paths are what result_judge.py digests into input_manifest.json/prompt_digest.
    import importlib.util as _ilu
    _spec = _ilu.spec_from_file_location('deepcode_judge_input_projection',
        str(task_root / 'evaluator' / 'judge_input_projection.py'))
    _projection = _ilu.module_from_spec(_spec)
    _spec.loader.exec_module(_projection)
    judge_trajectory, judge_oracle, projection_manifests = _projection.project_case(
        trajectory=paths['trajectory'], oracle=paths['oracle'])
    result_command = [sys.executable, str(SHARED_ROOT / 'result_judge.py'),
        '--case-id', 'test_001', '--task-input', str(paths['task']), '--rubric', str(rubric),
        '--agent-artifact', str(artifact), '--trajectory', str(judge_trajectory),
        '--native-evidence', str(paths['native']), '--oracle-summary', str(judge_oracle),
        '--broker-endpoint', result_endpoint, '--max-transport-attempts', '1',
        '--output-dir', str(output / 'result')]
    code_command = None  # Code axis retired 2026-09-19 (Result-only)
    write(preparation / 'input_binding.json', {'freeze': ref(freeze_path), 'code_identity': bridge,
        'result': {name: ref(path) for name, path in {**paths, 'artifact': artifact}.items()},
        'judge_input_projection': {'manifests': [ref(path) for path in projection_manifests],
            'trajectory_shown_to_judge': ref(judge_trajectory),
            'oracle_shown_to_judge': ref(judge_oracle)}})
    broker_before = shared['judge_broker_stats'](result_endpoint)
    exits = dispatch_pair(result_command, code_command, output)
    broker_after = shared['judge_broker_stats'](result_endpoint)
    broker_receipt = {'before': broker_before, 'after': broker_after,
        'logical_request_delta': shared['broker_runtime_counter'](broker_after, 'calls') -
            shared['broker_runtime_counter'](broker_before, 'calls'),
        'failure_delta': shared['broker_runtime_counter'](broker_after, 'failures') -
            shared['broker_runtime_counter'](broker_before, 'failures')}
    write(output / 'result_broker_attestation.json', broker_receipt)
    result_path = output / 'result/result_score_contract.json'
    code_path = output / 'code/code_score_contract.json'
    result, result_errors = shared['valid_result_contract'](result_path, 'test_001')
    code, code_errors = {}, []
    usage = result.get('provider_usage', {})
    if usage.get('logical_requests') != 1 or usage.get('completed_responses') != 1:
        result_errors.append('Result smoke did not complete exactly one logical request')
    if broker_receipt['logical_request_delta'] != 1 or broker_receipt['failure_delta'] != 0:
        result_errors.append('Result smoke broker usage differs from one completed call')
    summary = {'profile': PROFILE, 'evaluation_mode': 'readiness_smoke',
        'score_threshold': None, 'formal_result_publishable': False, 'formal_complete': False,
        'code_score_publishable': False, 'readiness_judges_complete':
            exits == {'result': 0} and not result_errors,
        'result_contract': ref(result_path) if result_path.exists() else None,
        'code_contract': ref(code_path) if code_path.exists() else None,
        'errors': {'result': result_errors, 'code': code_errors}, 'exits': exits}
    if summary['readiness_judges_complete']:
        summary['observations'] = export_judge_observations(run_dir, freeze_path, output, broker_after,
            result_contract=result, code_contract=code)
    write(run_dir / 'readiness_judge_smoke.json', summary)
    return summary
