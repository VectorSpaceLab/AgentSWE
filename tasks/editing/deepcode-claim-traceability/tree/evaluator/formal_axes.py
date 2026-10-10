"""DeepCode formal scoring with exact run-local task/oracle routing."""
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
    run_name="deepcode_formal_axes",
    init_globals={"ROOT_OVERRIDE": ROOT_LOCAL},
)
# run_path returns a copy; functions retain the execution namespace.  Inject
# routing into that namespace, not into the inert returned dictionary.
SHARED = SHARED["main"].__globals__
globals().update(SHARED)

IDENTITY_HOOKS = runpy.run_path(str(ROOT_LOCAL / "evaluator" / "code_identity_scope.py"))
for _name in ("frozen_identity_errors", "code_frozen_identity", "prepare_code_evidence_scope"):
    SHARED[_name] = IDENTITY_HOOKS[_name]
    globals()[_name] = IDENTITY_HOOKS[_name]


SCORE_CAPS_IMPL = ROOT_LOCAL / "evaluator" / "result_score_caps.py"
PROJECTION_IMPL = ROOT_LOCAL / "evaluator" / "judge_input_projection.py"


def _score_caps_module():
    """Task-owned ceiling issuer; loaded per call so a stale import cannot bind it."""
    return runpy.run_path(str(SCORE_CAPS_IMPL))


def _projection_module():
    """Task-owned bounded judge projection; loaded per call, same rule as the caps issuer."""
    return runpy.run_path(str(PROJECTION_IMPL))


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


ROUND_TWO_REFRESH = "agentswe-deepcode-round-two-refresh/v1"
ROUND_TWO_KEYS = ("product_store_count", "agent_captured_file_count", "product_store_paths",
                  "persisted_operation_receipts", "persisted_refusal_receipts",
                  "persisted_refusal_error_codes", "reserved_probe_receipts_observed",
                  "accepted_mutation_estimate", "ledger_event_count",
                  "round_two_assertion_comparisons", "round_two_checks_unobserved",
                  "round_two_checks_unavailable")


def _refresh_round_two(destination: Path) -> None:
    """Re-derive the store-derived round-two block on the scoring-time oracle copy.

    Input: ``actual_persisted_product_state``, the verbatim persisted product state the
    hidden run recorded, copied here unchanged.  Nothing is re-executed and no evidence is
    invented -- only the booleans/counters derived from that state are recomputed with the
    evaluator's current store recognition.  The frozen private comparison under
    ``lifecycle/`` keeps its attested bytes and digest.
    """
    value = _read_object(destination)
    state = value.get("actual_persisted_product_state")
    case_id = value.get("case_id")
    if not isinstance(state, list) or not isinstance(case_id, str):
        return
    harness = str(ROOT_LOCAL / "evaluator" / "harness")
    if harness not in sys.path:
        sys.path.insert(0, harness)
    from semantic_oracle import round_two_observations  # noqa: PLC0415 - task-local, loaded late
    before = {key: value.get(key) for key in ROUND_TWO_KEYS}
    value.update(round_two_observations(case_id, state))
    after = {key: value.get(key) for key in ROUND_TWO_KEYS}
    source = ROOT_LOCAL / "evaluator" / "harness" / "semantic_oracle.py"
    value["evaluator_round_two_recomputed"] = {
        "schema_version": ROUND_TWO_REFRESH,
        "why": "store recognition changed after this hidden run; the round-two block is a "
               "function of actual_persisted_product_state, which is unchanged here",
        "source": "evaluator/harness/semantic_oracle.py::round_two_observations",
        "source_sha256": hashlib.sha256(source.read_bytes()).hexdigest(),
        "changed": before != after, "before": before}
    destination.write_text(json.dumps(value, ensure_ascii=False, indent=1, sort_keys=True) + "\n",
                           encoding="utf-8")


def _inside(path: Path, root: Path) -> bool:
    try:
        path.resolve().relative_to(root.resolve())
        return True
    except ValueError:
        return False


def prepare_run_local_formal_root(run_dir: Path, case_ids=None) -> tuple[Path, dict[str, Path]]:
    """Build a strict shared-judge input root from hidden-run evidence."""
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
        destination = evaluator_dir / name
        if source.is_file() and not destination.exists():
            destination.symlink_to(source)
    routed: dict[str, Path] = {}
    for case_id in (SHARED["CASES"] if case_ids is None else case_ids):
        record = SHARED["find_case_record"](hidden, case_id)
        if not isinstance(record, dict):
            raise ValueError(f"missing hidden record for {case_id}")
        task_raw = record.get("executed_task_path")
        comparison_raw = record.get("private_oracle_comparison_path")
        if not isinstance(task_raw, str) or not isinstance(comparison_raw, str):
            raise ValueError(f"{case_id} lacks exact executed-task/oracle paths")
        task = Path(task_raw).resolve()
        comparison = Path(comparison_raw).resolve()
        if (
            not task.is_file()
            or not comparison.is_file()
            or not _inside(task, run_dir)
            or not _inside(comparison, run_dir)
        ):
            raise ValueError(f"{case_id} exact task or private comparison is missing/outside run directory")
        task_digest = hashlib.sha256(task.read_bytes()).hexdigest()
        comparison_digest = hashlib.sha256(comparison.read_bytes()).hexdigest()
        if task_digest != record.get("executed_task_sha256"):
            raise ValueError(f"{case_id} executed task digest mismatch")
        if comparison_digest != record.get("private_oracle_comparison_sha256"):
            raise ValueError(f"{case_id} private comparison digest mismatch")
        comparison_value = _read_object(comparison)
        if comparison_value.get("case_id") != case_id:
            raise ValueError(f"{case_id} private comparison case mismatch")
        if comparison_value.get("candidate_visible") is not False:
            raise ValueError(f"{case_id} private comparison is candidate-visible")
        if comparison_value.get("executed_task_sha256") != record.get("executed_task_sha256"):
            raise ValueError(f"{case_id} private comparison task identity mismatch")
        destination = overlay / "test_cases" / case_id / "input.md"
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(task, destination)
        routed[case_id] = comparison
    return overlay, routed


def main(argv: list[str] | None = None) -> int:
    run_dir = _run_dir_from_argv(argv)
    # Let the shared finalizer materialize its ordinary N/A aggregation for
    # genuinely empty/partial runs.  Once hidden evidence exists, exact
    # run-local task/oracle routing is mandatory and any routing defect must
    # fail closed rather than falling back to static prompts or oracles.
    hidden_path, _hidden = SHARED["hidden_document"](run_dir)
    if hidden_path is None:
        return SHARED["main"](argv)
    case_ids = SHARED["selected_case_ids"](argv)
    overlay, comparisons = prepare_run_local_formal_root(run_dir, case_ids)
    original_root = SHARED["ROOT"]
    original_provenance = SHARED["provenance_summary"]
    original_validation = SHARED["validate_model_artifact_provenance"]
    original_files = SHARED["case_files"]

    # 2026-09-21 bounded judge projection: the judge is handed a projected trajectory, but
    # task-native provenance validation must keep reading the ORIGINAL bytes, so the
    # projection is recorded here and swapped back before validate_artifact sees it.
    original_trajectories: dict[str, Path] = {}

    def native_validation(record, artifact, trajectory, case_id):
        trajectory = original_trajectories.get(case_id, trajectory)
        if str(ROOT_LOCAL) not in sys.path: sys.path.insert(0, str(ROOT_LOCAL))
        from evaluator.harness.execution_evidence import validate_artifact
        errors = validate_artifact(artifact, trajectory, case_id,
            preexisting=record.get("artifact_provenance", {}).get("preexisting_before_launch") is True)
        if record.get("artifact_validation", {}).get("valid") is not True:
            errors.append("DeepCode task-native artifact validation was not attested")
        if record.get("artifact_validation", {}).get("sha256") != hashlib.sha256(artifact.read_bytes()).hexdigest():
            errors.append("DeepCode artifact attestation hash mismatch")
        calls, successful = SHARED["lower_execution_counts"](record)
        if calls <= 0 or successful <= 0: errors.append("no successful real lower-model call")
        return errors

    # Evaluator-issued Result ceilings.  The shared scorer reads the contract
    # only after ``provenance_summary`` has written the private oracle, so the
    # path is registered here and the bytes are written in that hook below.
    caps_contracts: dict[str, Path] = {}
    caps_native: dict[str, Path] = {}

    def native_files(run_dir, case_id, record, hidden_path):
        files = original_files(run_dir, case_id, record, hidden_path)
        raw = record.get("evidence", {}).get("result")
        if isinstance(raw, str):
            trajectory = Path(raw).parent / "stdout.jsonl"
            if trajectory.is_file() and _inside(trajectory, run_dir):
                original_trajectories[case_id] = trajectory
                projected, _projection_manifest = _projection_module()["project_trajectory"](trajectory)
                files["trajectory"] = projected
        if SCORE_CAPS_IMPL.is_file() and files.get("native") is not None:
            caps = run_dir / "formal_scoring" / "result_axis" / case_id / "result_score_caps.json"
            caps_contracts[case_id] = caps
            caps_native[case_id] = files["native"]
            files["score_cap_contract"] = caps
        return files

    def run_local_provenance(case_id, record, artifact, destination):
        source = comparisons.get(case_id)
        if source is None:
            raise ValueError(f"missing run-local private comparison for {case_id}")
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(source, destination)
        # The frozen comparison stays byte-identical; this scoring-time copy carries the
        # round-two block re-derived from its own recorded persisted product state.
        _refresh_round_two(destination)
        # 2026-09-21 bounded judge projection of the private oracle comparison.  The caps
        # contract binds to the sha256 of the file the judge is actually handed
        # (result_judge.py:752-755), so the ceilings below are issued from these same bytes;
        # evaluator/result_score_caps.py::build_entries reads none of the bounded keys, so
        # the issued conditions are unchanged.  The unprojected copy stays as evidence.
        _projection = _projection_module()
        if _projection["PROJECT_ORACLE"]:
            destination, _oracle_manifest = _projection["project_oracle"](destination)
        caps = caps_contracts.get(case_id)
        native = caps_native.get(case_id)
        if caps is not None and native is not None:
            # Determinate conditions are decided from evaluator evidence only; a
            # failure here degrades to "no ceiling", it never fails the run.
            _score_caps_module()["write_contract"](
                caps, case_id=case_id,
                rubric=ROOT_LOCAL / "evaluator" / "agentloop_result_rubric.md",
                native_evidence=native, oracle_summary=destination)
        return destination

    SHARED["ROOT"] = overlay
    SHARED["provenance_summary"] = run_local_provenance
    SHARED["validate_model_artifact_provenance"] = native_validation
    SHARED["case_files"] = native_files
    try:
        return SHARED["main"](argv)
    finally:
        SHARED["ROOT"] = original_root
        SHARED["provenance_summary"] = original_provenance
        SHARED["validate_model_artifact_provenance"] = original_validation
        SHARED["case_files"] = original_files


if __name__ == "__main__":
    raise SystemExit(main())
