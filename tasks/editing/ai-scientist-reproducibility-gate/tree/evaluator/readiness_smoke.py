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


RUBRIC_RELATIVE = 'agentloop/result_rubric.md'


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
    dimensions = result_dimension_maxima(SHARED_ROOT / 'result_judge.py', RUBRIC_RELATIVE)
    observations = {}
    for role, identity, model_path, role_inputs in (
        ('result', result_id, output / 'result/model_response.json', {
            'dimension_maxima': dimensions, 'rubric_relative_path': RUBRIC_RELATIVE,
            'input_binding': ref(result_input_path),
            'bound_evidence': binding}),
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


# D23/D14 readiness absence (2026-09-21).  A hidden smoke case for which the Candidate
# authored no artifact is that case's Candidate zero -- the same outcome the FORMAL path
# books (evaluator/case_evidence.py:248-277 stamps the evaluator-observed fatal Candidate
# attribution; formal_axes_shared.py:725-727 turns that record into a `candidate_zero`
# Result contract) -- not an orchestration failure.
#
# On that branch `prepare_case_evidence` short-circuits and returns artifact,
# raw_trajectory, native_evidence and private_oracle ALL None, so readiness died on
# `Path(None)` at the paths dict below.  Readiness cannot simply skip the judge instead:
# the shared admission has no judge-less shape for the Result axis (v2_readiness.py:260,
# :262-263, :270-271, :327, :329-330, :386-389; its only skip, :252-257, is hard-scoped to
# role "code"), so a judge-less smoke can never be admitted.  The case is therefore judged
# on the normal path, exactly once, over an evaluator-completed evidence set.
CANDIDATE_ABSENCE_CLASSES = frozenset({
    'candidate_artifact_failure', 'candidate_behavior_failure',
    'candidate_product_failure', 'candidate_timeout'})
ABSENCE_SCHEMA = 'agentswe-ai-scientist-readiness-absence/v1'
ABSENCE_STANDIN_SCHEMA = 'agentswe-ai-scientist-readiness-absence-standin/v1'


def candidate_absence_admissible(record, attribution):
    """True only when the EVALUATOR's own record books a fatal Candidate outcome.

    Every field read here is written by the evaluator (case_evidence.py), never by the
    Candidate.  Anything infrastructure-shaped, unresolved or un-attributed keeps today's
    behaviour and still fails the readiness run closed.
    """
    return (isinstance(record, dict) and isinstance(attribution, dict)
            and record.get('classification') in CANDIDATE_ABSENCE_CLASSES
            and record.get('classification_axis') == 'candidate'
            and record.get('infrastructure_invalid') is not True
            and record.get('execution_attempted') is True
            and attribution.get('party') == 'candidate'
            and attribution.get('observed_by') == 'evaluator'
            and attribution.get('fatal') is True
            and attribution.get('infrastructure_invalid') is not True)


def absence_standin(path, *, case_id, record_path, reason, extra=None):
    """An evaluator-authored stand-in for a judge input the Candidate left unproducible.

    It states the absence and its cause and carries nothing the normal path would not
    already have handed the judge.  In particular the oracle stand-in discloses no
    expectation value: the whole point is that no comparison was possible.
    """
    document = {'schema_version': ABSENCE_STANDIN_SCHEMA, 'owner': 'evaluator',
                'authored_by': 'evaluator', 'candidate_authored': False,
                'case_id': case_id, 'present': False, 'reason': reason,
                'execution_record': ref(record_path)}
    if extra:
        document.update(extra)
    write(path, document)
    return path


def candidate_absence_evidence(*, case_id, record_path, candidate, candidate_digest,
                               preparation, evidence, record):
    """Complete the Result judge's evidence set for a case the Candidate left empty.

    Preferred source is `prepare_case_evidence` itself.  Its missing-artifact shortcut
    (case_evidence.py:248-277) is what withheld the trajectory, native evidence, private
    oracle and score-cap contract -- not any missing file: for such a case the launcher
    still wrote `raw_action_trajectory.json`, `case_world.json` and
    `scientific_snapshots/`.  `candidate_missing_artifact_finding` returning None is that
    module's own documented "keep today's behaviour verbatim" signal (case_evidence.py:96),
    i.e. do not take the shortcut, so it is suppressed for one re-derivation into a
    separate output directory.  The re-derived documents are byte-for-byte the ones the
    normal path would have produced, including `score_cap_contract.json`, so the judge is
    given no more and no less than it gets for a case that did deliver.

    The first call's fatal Candidate attribution is NOT discarded: it is what
    `candidate_absence_admissible` already proved and what the candidate-zero receipt
    records.  Whatever the re-derivation cannot produce falls back to an evaluator-authored
    stand-in, and the trajectory additionally falls back to the path the launcher record
    itself pins, verified against the record's own sha256.
    """
    import case_evidence as module
    guard = module.candidate_missing_artifact_finding
    derived, derivation = {}, {}
    try:
        module.candidate_missing_artifact_finding = lambda _record: None
        derived = module.prepare_case_evidence(
            case_id=case_id, record_path=record_path, candidate=candidate,
            candidate_digest=candidate_digest, output=preparation / 'scientific_absence')
    except Exception as exc:  # noqa: BLE001 - recorded, then replaced by stand-ins
        derivation = {'completed': False, 'error': '%s: %s' % (type(exc).__name__, exc)}
    else:
        derivation = {'completed': True, 'error': None,
                      'output': str(preparation / 'scientific_absence')}
    finally:
        module.candidate_missing_artifact_finding = guard

    loop = record.get('action_loop') if isinstance(record.get('action_loop'), dict) else {}
    integrity = record.get('integrity') if isinstance(record.get('integrity'), dict) else {}
    sources = {}

    trajectory = derived.get('raw_trajectory')
    if trajectory is not None:
        sources['trajectory'] = 'prepare_case_evidence_recomputed'
    else:
        recorded = loop.get('raw_trajectory_path')
        on_disk = Path(recorded) if recorded else None
        if (on_disk is not None and on_disk.is_file()
                and ref(on_disk)['sha256'] == loop.get('raw_trajectory_sha256')):
            trajectory, sources['trajectory'] = on_disk, 'launcher_recorded_raw_trajectory'
        else:
            sources['trajectory'] = 'evaluator_authored_absence_standin'
            trajectory = absence_standin(preparation / 'trajectory_absence_observation.json',
                case_id=case_id, record_path=record_path,
                reason='The launcher recorded no readable action trajectory for this case.',
                extra={'recorded_path': recorded,
                       'recorded_sha256': loop.get('raw_trajectory_sha256'),
                       'observed_operations': loop.get('operations') or []})

    native = derived.get('native_evidence')
    if native is not None:
        sources['native'] = 'prepare_case_evidence_recomputed'
    else:
        sources['native'] = 'evaluator_authored_absence_standin'
        native = absence_standin(preparation / 'native_evidence_absence_observation.json',
            case_id=case_id, record_path=record_path,
            reason='The Candidate produced no release artifact, so no scientific product'
                   ' files could be projected. The evaluator-observed facts of the run are'
                   ' carried here instead.',
            extra={'classification': record.get('classification'),
                   'classification_axis': record.get('classification_axis'),
                   'product_entry': record.get('entrypoint'),
                   'observed_operations': loop.get('observed_operations') or loop.get('operations') or [],
                   'artifact_hashes': integrity.get('artifact_hashes') or {},
                   'authoring_error': record.get('authoring_error'),
                   'model_author_claim_comparisons': [],
                   'author_claims_preserved_verbatim': True,
                   'scientific_product_files': {}})

    oracle = derived.get('private_oracle')
    if oracle is not None:
        sources['oracle'] = 'prepare_case_evidence_recomputed'
    else:
        sources['oracle'] = 'evaluator_authored_absence_standin'
        oracle = absence_standin(preparation / 'oracle_absence_observation.json',
            case_id=case_id, record_path=record_path,
            reason='No private oracle comparison was possible: the Candidate produced no'
                   ' release artifact to compare against the evaluator reference, and the'
                   ' evaluator could not recompute the reference for this case. No oracle'
                   ' expectation value is disclosed here.',
            extra={'reference_is_evaluator_only': True, 'oracle_comparison_performed': False,
                   'automatically_computed_score': None})

    completed = {**evidence, 'raw_trajectory': trajectory,
                 'native_evidence': native, 'private_oracle': oracle}
    cap = derived.get('score_cap_contract')
    if cap is not None:
        completed['score_cap_contract'] = cap
        cap_note = ('score cap contract recomputed on the normal path and passed to the'
                    ' judge as --score-cap-contract')
    else:
        completed.pop('score_cap_contract', None)
        cap_note = ('no score cap contract: the evaluator could not recompute the ceiling'
                    ' entries for a case with no release artifact, so --score-cap-contract'
                    ' is omitted exactly as it is for any case that carries no contract')
    return completed, sources, derivation, cap_note


def candidate_zero_receipt(*, path, run_dir, freeze_path, record_path, record, attribution,
                           evidence, sources, derivation, cap_note):
    """The evaluator's own Candidate-zero attribution, beside the judge's inputs.

    This is a receipt, not a score: the authoritative Result score for the bundle is the
    judge's contract.  It exists so the run states, in the evaluator's own words and
    anchored by digests, that this case's Result axis is a Candidate zero and why -- the
    same classification `formal_axes_shared.py:725-727` publishes for this record.
    """
    loop = record.get('action_loop') if isinstance(record.get('action_loop'), dict) else {}
    write(path, {
        'schema_version': ABSENCE_SCHEMA, 'owner': 'evaluator', 'run_id': run_dir.name,
        'case_id': evidence.get('case_id') or 'test_001',
        'evaluator_attribution': 'candidate_zero', 'result_state': 'candidate_zero',
        'evaluator_expected_result_score': 0,
        'authoritative_result_score': 'readiness_scoring/result/result_score_contract.json',
        'artifact_present': False, 'judge_dispatched': True,
        'classification': record.get('classification'),
        'classification_axis': record.get('classification_axis'),
        'failure_attribution': attribution,
        'authoring_error': record.get('authoring_error'),
        'rejected_agent_artifact_path': record.get('rejected_agent_artifact_path'),
        'candidate_digest': evidence.get('candidate_digest'),
        'freeze_sha256': ref(freeze_path)['sha256'],
        'raw_trajectory_path': loop.get('raw_trajectory_path'),
        'raw_trajectory_sha256': loop.get('raw_trajectory_sha256'),
        'attributed_evidence_paths': list(attribution.get('evidence_paths') or []),
        'launcher_record': ref(record_path),
        'judge_evidence_sources': sources, 'evidence_rederivation': derivation,
        'score_cap_contract': cap_note,
        'reason': 'The Candidate produced no model-authored agent_result.json for this'
                  ' hidden case. The evaluator owns the execution and observed the fatal'
                  ' Candidate attribution itself, so the absence is this case\'s Candidate'
                  ' zero rather than an orchestration failure. The Result judge is still'
                  ' dispatched exactly once, over evaluator-authored evidence, so the'
                  ' readiness bundle carries a real judge contract.'})
    return path


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
    """Use AI's real scientific evidence projection and the independent shared judges."""
    sys.path.insert(0,str(task_root/'evaluator'))
    from ai_shared_finalize import configure,load_shared
    from case_evidence import prepare_case_evidence
    from agentloop.readiness import execution_valid
    from agentloop.protocol import tree_digest
    shared=configure(load_shared(),run_dir)
    freeze_path,freeze=shared.freeze_document(run_dir)
    if freeze.get('readiness_profile')!=PROFILE: raise ValueError('readiness freeze required')
    errors=shared.hidden_lifecycle_errors(hidden,freeze,run_dir,('test_001',))
    if errors: raise ValueError('; '.join(errors))
    rows=hidden.get('cases',[])
    if len(rows)!=1 or rows[0].get('case_id')!='test_001': raise ValueError('one hidden case required')
    case=rows[0];record_path=Path(case['result_path'])
    if ref(record_path)['sha256']!=case['result_sha256']: raise ValueError('hidden raw record changed')
    raw=json.loads(record_path.read_text())
    if not execution_valid({**raw,'controller_finished_at':case['finished_at']}): raise ValueError('hidden transport invalid')
    repository=Path(freeze['candidate_path']);digest=freeze['candidate_digest']
    if tree_digest(repository)!=digest: raise ValueError('frozen source changed')
    output=run_dir/'readiness_scoring'
    if output.exists(): raise ValueError('judge smoke already attempted; no replay')
    preparation=run_dir/'readiness_judge_inputs';preparation.mkdir(exist_ok=False)
    evidence=prepare_case_evidence(case_id='test_001',record_path=record_path,candidate=repository,
        candidate_digest=digest,output=preparation/'scientific')
    artifact=evidence['artifact']
    # D23/D14 readiness absence: no Candidate artifact + the evaluator's own fatal
    # Candidate attribution == this case's Candidate zero.  Complete the judge's evidence
    # set (the short-circuit also withheld trajectory/native/oracle/score caps) and stay
    # on the normal path, so the bundle carries a real one-request judge contract.
    absence_receipt=None
    absence_record=evidence.get('execution_record') if isinstance(evidence.get('execution_record'),dict) else {}
    absence_attribution=absence_record.get('failure_attribution') or {}
    if artifact is None and candidate_absence_admissible(absence_record,absence_attribution):
        evidence,absence_sources,absence_derivation,absence_cap_note=candidate_absence_evidence(
            case_id='test_001',record_path=record_path,candidate=repository,
            candidate_digest=digest,preparation=preparation,evidence=evidence,record=absence_record)
        absence_receipt=candidate_zero_receipt(
            path=preparation/'candidate_zero_without_artifact.json',run_dir=run_dir,
            freeze_path=freeze_path,record_path=record_path,record=absence_record,
            attribution=absence_attribution,evidence=evidence,sources=absence_sources,
            derivation=absence_derivation,cap_note=absence_cap_note)
    if artifact is None:
        artifact=preparation/'artifact_absence_observation.json'
        write(artifact,{'candidate_authored':False,'artifact_present':False,'execution_record':ref(record_path)})
    # Copy task text into the sealed run so all validation references are run-owned.
    task=preparation/'task.md';task.write_bytes(Path(evidence['case_input']).read_bytes())
    paths={'task':task,'artifact':Path(artifact),'trajectory':Path(evidence['raw_trajectory']),
           'native':Path(evidence['native_evidence']),'oracle':Path(evidence['private_oracle'])}
    requirements=shared.public_requirements(preparation/'public_requirements')
    rubric=Path(evidence['rubric']);code_rubric=task_root/'evaluator/code_rubric.md'
    if not code_rubric.is_file(): code_rubric=task_root/'agentloop/code_rubric.md'
    if not code_rubric.is_file(): raise ValueError('AI Scientist Code rubric missing')
    result_command=[sys.executable,str(SHARED_ROOT/'result_judge.py'),'--case-id','test_001',
        '--task-input',str(task),'--rubric',str(rubric),'--agent-artifact',str(artifact),
        '--trajectory',str(paths['trajectory']),'--native-evidence',str(paths['native']),
        '--oracle-summary',str(paths['oracle']),'--broker-endpoint',result_endpoint,
        '--max-transport-attempts','1','--output-dir',str(output/'result')]
    if evidence.get('score_cap_contract'):
        result_command.extend(['--score-cap-contract',str(evidence['score_cap_contract'])])
    code_command=None  # Code axis retired 2026-09-19 (Result-only)
    write(preparation/'input_binding.json',{'freeze':ref(freeze_path),
        'code_identity':{'code_candidate_digest':digest,'source_algorithm':'authoritative-create-tree-v1'},
        'result':{name:ref(path) for name,path in paths.items()},
        # Present only on the absence path; an artifact-present run writes the same
        # document it wrote before, byte for byte.
        **({'candidate_zero_without_artifact':ref(absence_receipt)} if absence_receipt is not None else {})})
    before=shared.judge_broker_stats(result_endpoint)
    exits=dispatch_pair(result_command,code_command,output)
    after=shared.judge_broker_stats(result_endpoint)
    write(output/'result_broker_attestation.json',{'before':before,'after':after})
    result_path=output/'result/result_score_contract.json';code_path=output/'code/code_score_contract.json'
    result,result_errors=shared.valid_result_contract(result_path,'test_001')
    code,code_errors={},[]
    if shared.broker_runtime_counter(after,'calls')-shared.broker_runtime_counter(before,'calls')!=1:
        result_errors.append('Result smoke requires exactly one request')
    if shared.broker_runtime_counter(after,'failures')-shared.broker_runtime_counter(before,'failures')!=0:
        result_errors.append('Result smoke transport failed')
    summary={'profile':PROFILE,'evaluation_mode':'readiness_smoke','formal_result_publishable':False,
        'code_score_publishable':False,'score_threshold':None,'readiness_judges_complete':
        exits=={'result':0} and not result_errors,
        'exits':exits,'errors':{'result':result_errors,'code':code_errors}}
    if summary['readiness_judges_complete']:
        summary['observations']=export_judge_observations(run_dir,freeze_path,output,after,
            result_contract=result,code_contract=code)
    write(run_dir/'readiness_judge_smoke.json',summary)
    return summary

