"""DeepTutor formal scoring with exact run-local task/oracle routing."""
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
    run_name="deeptutor_formal_axes",
    init_globals={"ROOT_OVERRIDE": ROOT_LOCAL},
)
# run_path returns a copy; functions retain the execution namespace.  Inject
# routing into that namespace, not into the inert returned dictionary.
SHARED = SHARED["main"].__globals__
globals().update(SHARED)


def prepare_code_evidence_scope(candidate, digest, output_dir):
    planner = runpy.run_path(str(ROOT_LOCAL / 'evaluator/code_evidence_scope.py'),
        run_name='deeptutor_code_scope')
    return planner['prepare_code_evidence_scope'](candidate, digest, output_dir)


# Inject into the actual function namespace, including empty-hidden runs where
# Code remains independent of the Result axis.
SHARED['prepare_code_evidence_scope'] = prepare_code_evidence_scope


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

    def native_validation(record, artifact, trajectory, case_id):
        if str(ROOT_LOCAL) not in sys.path:
            sys.path.insert(0, str(ROOT_LOCAL))
        from agentloop.execution_evidence import artifact_authorship
        errors = original_validation(record, artifact, trajectory, case_id)
        actual = artifact_authorship(artifact.parent, case_id)
        if not actual["valid"]:
            errors.append("DeepTutor terminal response does not author the captured artifact")
        if record.get("artifact_validation", {}).get("sha256") != actual.get("sha256"):
            errors.append("DeepTutor task-native artifact attestation digest mismatch")
        return errors

    def run_local_provenance(case_id, record, artifact, destination):
        source = comparisons.get(case_id)
        if source is None:
            raise ValueError(f"missing run-local private comparison for {case_id}")
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(source, destination)
        return destination

    SHARED["ROOT"] = overlay
    SHARED["provenance_summary"] = run_local_provenance
    SHARED["validate_model_artifact_provenance"] = native_validation
    try:
        return SHARED["main"](argv)
    finally:
        SHARED["ROOT"] = original_root
        SHARED["provenance_summary"] = original_provenance
        SHARED["validate_model_artifact_provenance"] = original_validation


if __name__ == "__main__":
    raise SystemExit(main())
