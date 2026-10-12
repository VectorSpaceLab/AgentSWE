"""One non-formal Result request and one independent Code request for Aider."""
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
    from harbor.formal_one_stop import stats, write_json
    from evaluator.code_inputs import prepare_code_inputs
    freeze_path = directory/'lifecycle/freeze_manifest.json'
    freeze = task.read_json(freeze_path)
    if freeze.get('readiness_profile') != 'single-dev-two-round-hidden-smoke-v1':
        raise ValueError('readiness judge requires its sealed profile')
    output = directory/'readiness_scoring'
    output.mkdir(exist_ok=False)
    case = directory/'lifecycle/evaluations/hidden/test_001'
    files = {'trajectory': case/'lower.stdout.log', 'native': case/'native_evidence.json',
             'oracle': case/'oracle_comparison.json', 'task': output/'task_input.md'}
    task_snapshot, task_binding = task.result_task_input(directory, case, 'test_001')
    files['task'].write_bytes(task_snapshot.read_bytes())
    artifact = case/'agent_artifact.json'
    if artifact.is_file():
        verdict, _ = task.execution_verdict(hidden, 'test_001', freeze['candidate_digest'])
        if verdict.get('classification') not in {'scoreable','candidate_zero'}:
            raise ValueError('hidden execution has no valid candidate attribution')
        if verdict.get('classification') == 'scoreable':
            contract=hidden.get('artifact_contract',{})
            if (contract.get('source')!='lower_product_workspace' or contract.get('evaluator_synthesized') is not False
                    or contract.get('copied_after_lower_exit') is not True or contract.get('preexisting_before_launch') is not False
                    or contract.get('write_evidence',{}).get('accepted') is not True or hidden.get('artifact_sha256')!=ref(artifact)['sha256']
                    or ref(case/'trajectory.log')['sha256']!=hidden.get('trajectory_digest')):
                raise ValueError('invalid Aider artifact provenance')
    else:
        artifact = output/'artifact_absence.json'
        write_json(artifact, {'candidate_authored': False, 'evidence_kind': 'evaluator_observed_absence',
            'artifact_present': False, 'execution_record': ref(case/'result.json')})
    files['artifact'] = artifact
    for path in files.values():
        if not path.is_file() or path.is_symlink() or not path.resolve().is_relative_to(directory):
            raise ValueError('missing/foreign actual judge input')
    result_command = [sys.executable, str(task.RESULT_JUDGE), '--case-id', 'test_001',
        '--task-input', str(files['task']), '--rubric', str(root/'evaluator/rubric.md'),
        '--agent-artifact', str(artifact), '--trajectory', str(files['trajectory']),
        '--native-evidence', str(files['native']), '--oracle-summary', str(files['oracle']),
        '--broker-endpoint', endpoint, '--max-transport-attempts', '1', '--output-dir', str(output/'result')]
    write_json(output/'input_binding.json', {'freeze': ref(freeze_path), 'result': {k: ref(p) for k,p in files.items()},
        'code_axis': 'skipped_by_policy'})
    before = stats(endpoint)
    invocations = {}
    for role, command in [('result', result_command)]:
        invocations[role] = invoke_once(command, output/role, list(files.values()) if role == 'result' else [root/'meta/code_rubric.md'])
    after = stats(endpoint)
    write_json(output/'result_broker_stats.json', after)
    result = checked_raw_contract(output/'result', 'result_score_contract.json', invocations['result'])
    errors = []
    if not task.valid_result_contract(result, 'test_001'):
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


from typing import Any
import subprocess
from evaluator.formal_finalize import read_json,write_json

def invoke_once(command: list[str], output: Path, input_paths: list[Path]) -> dict[str, Any]:
    """Claim a fresh task scoring phase, preserving the raw judge contract.

    The shared judge owns transport retries. A task adapter must not interpret
    a stale contract after a failed CLI invocation, nor silently invoke again.
    """
    inputs = [{'path': str(path), 'sha256': hashlib.sha256(path.read_bytes()).hexdigest()}
              for path in input_paths]
    output.parent.mkdir(parents=True, exist_ok=True)
    output.mkdir(exist_ok=False)
    write_json(output / 'task_judge_intent.json', {'schema_version': 'agentswe-task-judge-intent-v1',
        'command': command, 'inputs': inputs, 'logical_invocation_limit': 1})
    try:
        done = subprocess.run(command, text=True, capture_output=True, check=False)
        (output / 'task_cli_stdout.log').write_text(done.stdout, encoding='utf-8')
        (output / 'task_cli_stderr.log').write_text(done.stderr, encoding='utf-8')
        evidence = {'entry': command[1], 'exit_code': done.returncode,
            'stdout_tail': done.stdout[-1200:], 'stderr_tail': done.stderr[-1200:]}
    except Exception as error:
        evidence = {'entry': command[1], 'exit_code': None,
            'error_type': type(error).__name__, 'error': str(error)}
    write_json(output / 'task_judge_invocation.json', evidence)
    return evidence


def checked_raw_contract(output: Path, name: str, invocation: dict[str, Any], *,
                         expected_digest: str | None = None) -> dict[str, Any]:
    path = output / name
    reason = None
    raw_hash = hashlib.sha256(path.read_bytes()).hexdigest() if path.is_file() else None
    try:
        contract = read_json(path)
    except (OSError, ValueError):
        contract = {}
        reason = 'judge did not produce a readable contract'
    if invocation.get('exit_code') != 0:
        reason = 'judge CLI did not complete successfully'
    elif expected_digest is not None and contract.get('candidate_digest') != expected_digest:
        reason = 'Code contract materialized full-source identity mismatch'
    if reason:
        # This is a task-level invalidity wrapper, not a rewrite of the judge.
        return {'evaluation_state': 'infrastructure_error', 'contract_valid': False,
            'result_score_publishable': False, 'code_score_publishable': False,
            'reason': reason, 'raw_contract_path': str(path), 'raw_contract_sha256': raw_hash,
            'task_invocation_path': str(output / 'task_judge_invocation.json')}
    return contract
