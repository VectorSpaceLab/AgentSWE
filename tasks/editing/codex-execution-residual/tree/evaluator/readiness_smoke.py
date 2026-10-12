"""One non-formal Result request and one independent Code request for Codex."""
from pathlib import Path
import hashlib
import json
import sys


RUBRIC_RELATIVE = 'evaluator/rubric.md'
CODE_AXIS_SKIP_POLICY = {'id': 'edit-code-axis-retired-2026-09-19', 'evaluation_state': 'skipped_by_policy', 'reason': 'Result-only evaluation; Code judge not dispatched'}


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


def run(root, directory, hidden, credential, endpoint):
    from evaluator import formal_finalize as task
    from harbor.formal_one_stop import broker_stats, write_json
    from evaluator.code_inputs import prepare_code_inputs
    freeze_path = directory/'freeze_manifest.json'
    freeze = task.read_json(freeze_path)
    if freeze.get('readiness_profile') != 'single-dev-two-round-hidden-smoke-v1':
        raise ValueError('readiness judge requires its sealed profile')
    output = directory/'readiness_scoring'
    output.mkdir(exist_ok=False)
    case = directory/'evaluations/hidden/test_001'
    # D6 (2026-09-19): the Result judge sees a bounded projection of the trajectory
    # (oversized strings truncated with an explicit marker); the original stays as evidence.
    from evaluator.judge_trajectory_projection import project as project_trajectory
    projected_trajectory, projection_manifest = project_trajectory(case/'candidate.stdout.jsonl')
    files = {'trajectory': projected_trajectory, 'native': case/'native_evidence.json',
             'oracle': case/'oracle_comparison.json', 'task': output/'task_input.md'}
    files['task'].write_bytes((root/'test_cases/test_001/input.md').read_bytes())
    artifact = case/'agent_artifact.json'
    if artifact.is_file():
        verdict, _ = task.execution_verdict(hidden, 'test_001', freeze['candidate_digest'])
        if verdict.get('classification') == 'scoreable':
            error = task.artifact_provenance_error(directory, 'test_001', hidden, case_dir=case)
            if error:
                raise ValueError(error)
        elif verdict.get('classification') != 'candidate_zero':
            raise ValueError('hidden execution has no valid candidate attribution')
    else:
        artifact = output/'artifact_absence.json'
        write_json(artifact, {'candidate_authored': False, 'evidence_kind': 'evaluator_observed_absence',
            'artifact_present': False, 'execution_record': ref(case/'result.json')})
    files['artifact'] = artifact
    for path in files.values():
        if not path.is_file() or path.is_symlink() or not path.resolve().is_relative_to(directory):
            raise ValueError('missing/foreign actual judge input')
    from evaluator.score_caps import build_contract
    caps = output/'result_score_caps.json'
    write_json(caps, build_contract('test_001', root/'evaluator/rubric.md', files['native'], files['oracle']))
    result_command = [sys.executable, str(task.RESULT_JUDGE), '--case-id', 'test_001',
        '--task-input', str(files['task']), '--rubric', str(root/'evaluator/rubric.md'),
        '--rubric-dimensions', str(root/'evaluator/result_dimensions.json'),
        '--agent-artifact', str(artifact), '--trajectory', str(files['trajectory']),
        '--native-evidence', str(files['native']), '--oracle-summary', str(files['oracle']),
        '--score-cap-contract', str(caps),
        '--broker-endpoint', endpoint, '--max-transport-attempts', '1', '--output-dir', str(output/'result')]
    write_json(output/'input_binding.json', {'freeze': ref(freeze_path), 'result': {k: ref(p) for k,p in files.items()},
        'score_caps': ref(caps),
        'trajectory_projection': ref(projection_manifest), 'trajectory_original': ref(case/'candidate.stdout.jsonl'),
        'code_axis': 'skipped_by_policy'})
    before = broker_stats(int(endpoint.split(':')[2].split('/')[0]))
    invocations = {}
    for role, command in [('result', result_command)]:
        invocations[role] = task.invoke_once(command, output/role, (list(files.values()) + [caps]) if role == 'result' else [root/'evaluator/code_rubric.md'])
    after = broker_stats(int(endpoint.split(':')[2].split('/')[0]))
    write_json(output/'result_broker_stats.json', after)
    result = task.checked_raw_contract(output/'result', 'result_score_contract.json', invocations['result'])
    errors = []
    if not task.valid(result, 'test_001'):
        errors.append('Result smoke contract invalid')
    if after.get('runtime', {}).get('calls', 0)-before.get('runtime', {}).get('calls', 0) != 1:
        errors.append('Result smoke did not make exactly one request')
    if not errors:
        result_request = after['attempts'][0]['request_id']
        for role, request, validator, raw_model, inputs in [
            ('result', result_request, task.RESULT_JUDGE, output/'result/model_response.json',
                {'rubric_relative_path': RUBRIC_RELATIVE,
                 'dimension_maxima': result_dimension_maxima(task.RESULT_JUDGE, RUBRIC_RELATIVE), 'actual_inputs': {k: ref(p) for k,p in files.items()}})]:
            identity = role+':'+request
            input_path, output_path = output/(role+'_validation_input.json'), output/(role+'_raw_output.json')
            write_json(input_path, {'role': role, 'freeze_sha256': ref(freeze_path)['sha256'],
                'validator_source_sha256': ref(validator)['sha256'], **inputs})
            write_json(output_path, {'role': role, 'judge_session_id': identity,
                'raw_model_output': ref(raw_model), 'payload': judge_payload(raw_model)})
            write_json(output/(role+'_observation.json'), {'owner': 'evaluator', 'run_id': directory.name,
                'role': role, 'judge_session_id': identity, 'request_id': request,
                'model': 'deepseek-flash', 'effort': 'max', 'state': 'terminal', 'formal': False,
                'freeze_sha256': ref(freeze_path)['sha256'], 'input': ref(input_path), 'output': ref(output_path)})
        # Code axis retired 2026-09-19 (Result-only): explicit evaluator-written skip observation.
        write_json(output / 'code_observation.json', {'owner': 'evaluator', 'run_id': directory.name, 'role': 'code',
            'state': 'skipped_by_policy', 'formal': False, 'judge_session_id': None, 'request_id': None,
            'freeze_sha256': ref(freeze_path)['sha256'], 'policy': CODE_AXIS_SKIP_POLICY})
    summary = {'complete': not errors, 'errors': errors, 'invocations': invocations,
        'readiness_only': True, 'formal_result_publishable': False, 'code_score_publishable': False}
    write_json(output/'summary.json', summary)
    return summary
