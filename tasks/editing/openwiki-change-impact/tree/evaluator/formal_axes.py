"""OpenWiki formal scoring with exact run-local task/oracle routing."""
from __future__ import annotations

import hashlib
import json
import runpy
import shutil
import sys
from pathlib import Path

ROOT_LOCAL = Path(__file__).resolve().parents[1]
IMPLEMENTATION = Path("@@AGENTSWE_EDITING_CONTROL@@/formal_axes_shared.py")
if not IMPLEMENTATION.is_file():
    raise RuntimeError(f"missing evaluator-owned shared formal axes implementation: {IMPLEMENTATION}")
SHARED = runpy.run_path(
    str(IMPLEMENTATION),
    run_name="openwiki_formal_axes",
    init_globals={"ROOT_OVERRIDE": ROOT_LOCAL},
)
SHARED = SHARED["main"].__globals__
globals().update(SHARED)


SCORE_CAPS_IMPL = ROOT_LOCAL / "evaluator" / "result_score_caps.py"


def _score_caps_module():
    """Task-owned ceiling issuer; loaded per call so a stale import cannot bind it."""
    return runpy.run_path(str(SCORE_CAPS_IMPL))


def _run_dir_from_argv(argv: list[str] | None) -> Path:
    values = list(sys.argv[1:] if argv is None else argv)
    try:
        return Path(values[values.index("--run-dir") + 1]).resolve()
    except (ValueError, IndexError) as exc:
        raise ValueError("formal axes requires --run-dir") from exc


def _read_object(path: Path) -> dict:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError(f"JSON object required: {path}")
    return value


def _inside(path: Path, root: Path) -> bool:
    try:
        path.resolve().relative_to(root.resolve())
        return True
    except ValueError:
        return False


def _validated_openwiki_freeze(freeze: dict, run_dir: Path, *,
                               allow_pilot_test_001: bool = False) -> tuple[Path, Path]:
    """Validate original sealed lifecycle bytes before translating its digest."""
    sys.path.insert(0, str(ROOT_LOCAL))
    from agentloop.evaluator.hidden_controller import validate_freeze
    from agentloop.protocol import tree_digest

    run_dir = run_dir.resolve()
    freeze_path, original = SHARED['freeze_document'](run_dir)
    if freeze_path is None or freeze_path.is_symlink() or not _inside(freeze_path, run_dir):
        raise ValueError('missing evaluator-owned sealed freeze manifest')
    if original != freeze:
        raise ValueError('in-memory freeze differs from sealed manifest')
    owner = freeze_path.parent.resolve()
    repository = validate_freeze(original, owner, freeze_path,
        allow_pilot_test_001=allow_pilot_test_001)
    state_path = owner / 'controller_state.json'
    if state_path.is_symlink():
        raise ValueError('controller state must not be a symlink')
    state = _read_object(state_path)
    records = state.get('records')
    if (state.get('product_execution_guard') != 'openwiki-product-no-replay/v1'
            or state.get('frozen') != original
            or state.get('builder_session_id') != original['builder_session_id']
            or not isinstance(records, list) or not records
            or any(not isinstance(record, dict) for record in records)
            or len(records) != original['accepted_submission_count']
            or [record.get('round') for record in records] != list(range(1, len(records) + 1))
            or [record.get('candidate_digest') for record in records] != original['accepted_candidate_digests']):
        raise ValueError('sealed freeze does not match accepted controller history')
    latest = records[-1]
    build = latest.get('build') or {}
    if not isinstance(build, dict):
        raise ValueError('latest accepted build evidence is missing')
    entry = Path(str(build.get('product_entry', '')))
    accepted = entry.resolve().parent.parent
    if (build.get('valid') is not True or not entry.is_file()
            or latest.get('builder_session_id') != original['builder_session_id']
            or original['source_submission'] != latest['round']
            or original['source_submission_id'] != f"candidate-{latest['round']:03d}-{latest['candidate_digest'][:12]}"
            or not _inside(accepted, owner) or accepted == repository
            or tree_digest(accepted) != original['repository_digest']):
        raise ValueError('frozen source does not match the latest accepted product')
    return freeze_path, repository


def frozen_identity_errors(freeze: dict, run_dir: Path, *,
                            allow_pilot_test_001: bool = False) -> list[str]:
    """Missing/tampered lifecycle evidence is N/A, never a Candidate zero."""
    try:
        _validated_openwiki_freeze(freeze, run_dir, allow_pilot_test_001=allow_pilot_test_001)
    except (OSError, ValueError, KeyError, TypeError, IndexError) as exc:
        return ['OpenWiki frozen identity invalid: ' + str(exc)]
    return []


def code_frozen_identity(freeze: dict, run_dir: Path, *,
                         allow_pilot_test_001: bool = False) -> dict:
    """Bind task and unchanged Create algorithms to the same sealed directory."""
    freeze_path, repository = _validated_openwiki_freeze(freeze, run_dir,
        allow_pilot_test_001=allow_pilot_test_001)
    create = runpy.run_path('@@AGENTSWE_EDITING_CONTROL@@/code_eval.py',
                           run_name='openwiki_create_digest_only')
    canonical_digest = create['tree_digest'](repository)
    # Detect a source/manifest change across the independent canonical read.
    final_path, final_repository = _validated_openwiki_freeze(freeze, run_dir,
        allow_pilot_test_001=allow_pilot_test_001)
    if final_path != freeze_path or final_repository != repository:
        raise ValueError('frozen source identity changed while computing Code digest')
    return {
        'schema_version': 'agentswe-edit-dual-frozen-identity/v1', 'valid': True,
        'candidate_path': str(repository), 'freeze_sha256': SHARED['sha256_file'](freeze_path),
        'lifecycle_candidate_digest': freeze['candidate_digest'],
        'code_candidate_digest': canonical_digest,
        'algorithms': {'lifecycle': 'openwiki-sha256-tree-v1',
                       'code': 'Create code_eval.tree_digest files-and-symlinks'},
    }


SHARED['frozen_identity_errors'] = frozen_identity_errors
SHARED['code_frozen_identity'] = code_frozen_identity

_SHARED_HIDDEN_LIFECYCLE_ERRORS = SHARED['hidden_lifecycle_errors']


def hidden_lifecycle_errors(hidden: dict, freeze: dict, run_dir: Path,
                            selected: tuple[str, ...] = SHARED['CASES']) -> list[str]:
    errors = _SHARED_HIDDEN_LIFECYCLE_ERRORS(hidden, freeze, run_dir, selected)
    sys.path.insert(0, str(ROOT_LOCAL))
    from agentloop.evaluator.hidden_controller import hidden_lifecycle_fields
    try:
        freeze_path, sealed_freeze = SHARED['freeze_document'](run_dir)
        if freeze_path is None or sealed_freeze != freeze:
            raise ValueError('hidden lifecycle lacks its original sealed freeze')
        gate_path = freeze_path.parent / 'hidden-once-gate.json'
        if (gate_path.is_symlink() or not gate_path.is_file()
                or gate_path.stat().st_mode & 0o222):
            raise ValueError('hidden once gate is missing, writable, or a symlink')
        gate = _read_object(gate_path)
        freeze_sha = SHARED['sha256_file'](freeze_path)
        if (gate.get('schema_version') != 'openwiki-hidden-once-gate/v1'
                or gate.get('state') != 'claimed'
                or gate.get('freeze_manifest') != str(freeze_path)
                or gate.get('freeze_manifest_sha256') != freeze_sha
                or gate.get('repository_digest') != freeze.get('repository_digest')
                or gate.get('canonical_inventory') != list(selected)
                or hidden.get('freeze_manifest') != str(freeze_path)
                or hidden.get('freeze_manifest_sha256') != freeze_sha
                or hidden.get('hidden_once_gate') != str(gate_path)
                or hidden.get('hidden_started_at') != gate.get('hidden_started_at')):
            raise ValueError('hidden lifecycle differs from the immutable once gate')
        cases = hidden.get('cases')
        if not isinstance(cases, dict) or any(not isinstance(record, dict) for record in cases.values()):
            raise ValueError('hidden lifecycle lacks full per-case records')
        fields = hidden_lifecycle_fields(freeze, cases, gate, selected)
        if any(hidden.get(key) != value for key, value in fields.items()):
            raise ValueError('hidden lifecycle claims differ from recorded case timestamps or digests')
        if not fields['all_cases_started_after_freeze'] or not fields['frozen_digest_stable']:
            raise ValueError('hidden case timestamps or frozen digests are invalid')
    except (OSError, ValueError, KeyError, TypeError) as exc:
        errors.append('OpenWiki hidden lifecycle invalid: ' + str(exc))
    return errors


SHARED['hidden_lifecycle_errors'] = hidden_lifecycle_errors


def prepare_run_local_formal_root(run_dir: Path, case_ids=None, *,
                                  routing_errors: dict[str, str] | None = None) -> tuple[Path, dict[str, Path]]:
    hidden_path, hidden = SHARED["hidden_document"](run_dir)
    if hidden_path is None:
        raise ValueError("missing hidden-after-freeze evidence for exact-task routing")
    overlay = run_dir / "formal_scoring" / "run_local_input_root"
    overlay.mkdir(parents=True, exist_ok=True)
    input_link = overlay / "input"
    if not input_link.exists():
        input_link.symlink_to(ROOT_LOCAL / "input", target_is_directory=True)
    evaluator_dir = overlay / "evaluator"
    evaluator_dir.mkdir(exist_ok=True)
    for name in ("code_rubric.md", "result_rubric.md", "agentloop_result_rubric.md", "result_dimensions.json"):
        source = ROOT_LOCAL / "evaluator" / name
        if not source.is_file():
            source = ROOT_LOCAL / 'agentloop/evaluator' / name
        destination = evaluator_dir / name
        if source.is_file() and not destination.exists():
            destination.symlink_to(source)
    routed: dict[str, Path] = {}
    def route_case(case_id):
        record = SHARED["find_case_record"](hidden, case_id)
        if not isinstance(record, dict):
            raise ValueError(f"missing hidden record for {case_id}")
        task_raw = record.get("executed_task_path")
        comparison_raw = record.get("private_oracle_comparison_path")
        if not isinstance(task_raw, str) or not isinstance(comparison_raw, str):
            raise ValueError(f"{case_id} lacks exact executed-task/oracle paths")
        task = Path(task_raw).resolve()
        comparison = Path(comparison_raw).resolve()
        if not task.is_file() or not comparison.is_file() or not _inside(task, run_dir) or not _inside(comparison, run_dir):
            raise ValueError(f"{case_id} exact task or private comparison is missing/outside run directory")
        if record.get("executed_task_sha256") != hashlib.sha256(task.read_bytes()).hexdigest():
            raise ValueError(f"{case_id} executed task digest mismatch")
        if record.get("private_oracle_comparison_sha256") != hashlib.sha256(comparison.read_bytes()).hexdigest():
            raise ValueError(f"{case_id} private comparison digest mismatch")
        comparison_value = _read_object(comparison)
        if comparison_value.get("case_id") != case_id or comparison_value.get("candidate_visible") is not False:
            raise ValueError(f"{case_id} private comparison is not case-bound/private")
        destination = overlay / "test_cases" / case_id / "input.md"
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(task, destination)
        routed[case_id] = comparison
    for case_id in (SHARED["CASES"] if case_ids is None else case_ids):
        try:
            route_case(case_id)
        except (OSError, ValueError, TypeError, KeyError) as exc:
            if routing_errors is None:
                raise
            record = SHARED['find_case_record'](hidden, case_id)
            if not isinstance(record, dict) or not SHARED['infrastructure_reason'](record):
                routing_errors[case_id] = 'OpenWiki exact-task/oracle routing failed: ' + str(exc)
    return overlay, routed


def main(argv: list[str] | None = None) -> int:
    run_dir = _run_dir_from_argv(argv)
    if SHARED["hidden_document"](run_dir)[0] is None:
        return SHARED["main"](argv)
    selected = SHARED["selected_case_ids"](argv)
    values = list(sys.argv[1:] if argv is None else argv)
    allow_pilot = '--acceptance-cases' in values and selected == ('test_001',)
    routing_errors: dict[str, str] = {}
    overlay, comparisons = prepare_run_local_formal_root(run_dir, selected, routing_errors=routing_errors)
    original_root = SHARED["ROOT"]
    original_provenance = SHARED["provenance_summary"]
    original_validation = SHARED['validate_model_artifact_provenance']
    original_case_files = SHARED['case_files']
    original_identity_errors = SHARED['frozen_identity_errors']
    original_code_identity = SHARED['code_frozen_identity']
    original_classify = SHARED['classify_candidate_execution']
    original_infrastructure_reason = SHARED['infrastructure_reason']

    def routed_classification(record, *, case_id, candidate_digest):
        if case_id in routing_errors:
            record = dict(record, infrastructure_invalid=True, failure_attribution={
                'party': 'evaluator', 'observed_by': 'evaluator', 'reason': routing_errors[case_id]})
        return original_classify(record, case_id=case_id, candidate_digest=candidate_digest)

    def routed_infrastructure_reason(record):
        return routing_errors.get(record.get('case_id')) or original_infrastructure_reason(record)

    def native_validation(record, artifact, trajectory, case_id):
        sys.path.insert(0, str(ROOT_LOCAL))
        from agentloop.evaluator.execution_evidence import artifact_authorship, disconnect_only_incomplete
        try:
            output = Path(record['execution_record_path']).parent
        except (KeyError, TypeError):
            return ['OpenWiki execution record path is missing']
        actual = artifact_authorship(output, case_id,
            preexisting=record.get('artifact_preexisting', False))
        errors = []
        if actual['artifact_path'] != str(artifact):
            errors.append('OpenWiki Result artifact differs from the observed native artifact')
        if not actual['valid']:
            errors.append('OpenWiki final CLI model response does not author the captured native artifact')
        if record.get('artifact_validation', {}).get('sha256') != actual.get('sha256'):
            errors.append('OpenWiki task-native artifact attestation digest mismatch')
        from agentloop.evaluator.process_observation import load_observation, file_ref
        from agentloop.evaluator.broker_observation import load_observations
        try:
            context = json.loads((output / 'logical-context.json').read_text())
            context_fields = {k:v for k,v in context.items() if k != 'context_id'}
            expected_id = hashlib.sha256(json.dumps(context_fields, sort_keys=True).encode()).hexdigest()
            if (context.get('context_id') != expected_id
                    or context.get('case_id') != case_id
                    or context.get('candidate_digest') != record.get('executed_repository_digest')
                    or context.get('request_sha256') != record.get('executed_task_sha256')
                    or context.get('output_path') != str(output.resolve())):
                raise ValueError('process context differs from the judged execution')
            observation = load_observation(output, context)
            if observation.get('complete') is not True:
                raise ValueError('native OS evidence is incomplete')
            expected_broker_ref = record.get('native_broker_observation', {})
            if file_ref(output / 'native-broker-observation.json') != {
                    key: expected_broker_ref.get(key) for key in ('path', 'sha256')}:
                raise ValueError('broker summary changed after execution')
            broker_observation = load_observations(output, context['context_id'])
            if ((broker_observation.get('complete') is not True
                    and not disconnect_only_incomplete(broker_observation, record))
                    or not broker_observation.get('records')):
                raise ValueError('native broker evidence is incomplete')
            if file_ref(trajectory)['sha256'] != record.get('observed_trajectory_sha256'):
                raise ValueError('judge trajectory changed after execution')
        except (KeyError, OSError, TypeError, ValueError) as exc:
            errors.append('OpenWiki process evidence invalid: ' + str(exc))
        return errors

    # Evaluator-issued Result ceilings (0920 hardening).  The shared scorer reads
    # the contract only after ``provenance_summary`` has written the private
    # oracle, so the path is registered here and the bytes are written in that
    # hook below.  A missing module or unreadable evidence degrades to a contract
    # with no determinate ceiling; it never fails the scoring run.
    caps_inputs: dict[str, dict] = {}

    def native_case_files(run_dir, case_id, record, hidden_path):
        raw = record.get('execution_record_path')
        if not isinstance(raw, str):
            return original_case_files(run_dir, case_id, record, hidden_path)
        output = Path(raw).resolve().parent
        if not _inside(output, run_dir):
            raise ValueError('OpenWiki execution evidence escapes run directory')
        from agentloop.evaluator.execution_evidence import native_artifact_path
        files = {key: path if path.is_file() else None for key, path in {
            'artifact': native_artifact_path(output / 'workspace'),
            'trajectory': output / 'observed_trajectory.json', 'native': output / 'launcher_result.json',
            'rubric': ROOT_LOCAL / 'agentloop/evaluator/result_rubric.md'}.items()}
        contract = run_dir / 'formal_scoring' / 'result_axis' / case_id / 'result_score_caps.json'
        caps_inputs[case_id] = {'contract': contract, 'rubric': files.get('rubric'),
                                'native': output / 'launcher_result.json',
                                'record': Path(raw).resolve()}
        files['score_cap_contract'] = contract
        return files

    def run_local_provenance(case_id, record, artifact, destination):
        source = comparisons.get(case_id)
        if source is None:
            raise ValueError(f"missing run-local private comparison for {case_id}")
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(source, destination)
        inputs = caps_inputs.get(case_id)
        if inputs is not None:
            contract = Path(inputs['contract'])
            rubric = inputs.get('rubric') or (ROOT_LOCAL / 'agentloop/evaluator/result_rubric.md')
            native = Path(inputs['native'])
            try:
                caps = _score_caps_module()
                caps['write_contract'](contract, case_id=case_id, rubric=rubric,
                                       native_evidence=native, oracle_summary=destination,
                                       execution_record=inputs.get('record'))
            except Exception as exc:  # noqa: BLE001 - a ceiling never fails a run
                try:
                    contract.parent.mkdir(parents=True, exist_ok=True)
                    contract.write_text(json.dumps({
                        'schema_version': 'agentswe-result-score-caps/v1', 'case_id': case_id,
                        'issued_by': 'openwiki task owner, 0920 hardening',
                        'rubric_sha256': hashlib.sha256(Path(rubric).read_bytes()).hexdigest(),
                        'native_evidence_sha256': hashlib.sha256(native.read_bytes()).hexdigest(),
                        'oracle_summary_sha256': hashlib.sha256(destination.read_bytes()).hexdigest(),
                        'entries': [{'cap_id': 'c0_ceilings_unavailable', 'maximum_score': 100,
                                     'status': 'unavailable',
                                     'requirement_ref': 'agentloop/evaluator/result_rubric.md#evidence-bound-ceilings',
                                     'reason': 'Ceiling issuer unavailable: %s: %s' % (type(exc).__name__, exc),
                                     'evidence_refs': []}],
                    }, sort_keys=True, indent=2) + '\n', encoding='utf-8')
                except OSError:
                    pass
        return destination

    SHARED["ROOT"] = overlay
    SHARED["provenance_summary"] = run_local_provenance
    SHARED['validate_model_artifact_provenance'] = native_validation
    SHARED['case_files'] = native_case_files
    SHARED['frozen_identity_errors'] = lambda freeze, directory: frozen_identity_errors(
        freeze, directory, allow_pilot_test_001=allow_pilot and freeze.get('pilot_not_formal') is True)
    SHARED['code_frozen_identity'] = lambda freeze, directory: code_frozen_identity(
        freeze, directory, allow_pilot_test_001=allow_pilot and freeze.get('pilot_not_formal') is True)
    SHARED['classify_candidate_execution'] = routed_classification
    SHARED['infrastructure_reason'] = routed_infrastructure_reason
    try:
        return SHARED["main"](argv)
    finally:
        SHARED["ROOT"] = original_root
        SHARED["provenance_summary"] = original_provenance
        SHARED['validate_model_artifact_provenance'] = original_validation
        SHARED['case_files'] = original_case_files
        SHARED['frozen_identity_errors'] = original_identity_errors
        SHARED['code_frozen_identity'] = original_code_identity
        SHARED['classify_candidate_execution'] = original_classify
        SHARED['infrastructure_reason'] = original_infrastructure_reason


if __name__ == "__main__":
    raise SystemExit(main())
