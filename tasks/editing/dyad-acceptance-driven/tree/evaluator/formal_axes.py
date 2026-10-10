# This file is synchronized with the Group C formal scoring contract.
from pathlib import Path
import hashlib
import json
import runpy
import shutil
import sys

_root = Path(__file__).resolve().parents[1]
# Independent publication state names are intentionally retained in this
# wrapper's contract surface: result_reasons and code_errors remain separate.
_implementation = Path("@@AGENTSWE_EDITING_CONTROL@@/formal_axes_shared.py")
if not _implementation.is_file():
    raise RuntimeError(f"missing evaluator-owned shared formal axes implementation: {_implementation}")
_shared = runpy.run_path(
    str(_implementation),
    run_name="dyad_formal_axes",
    init_globals={"ROOT_OVERRIDE": _root},
)
# Functions returned by run_path retain a different globals namespace.
_shared = _shared["main"].__globals__
globals().update(_shared)
_shared_main = _shared["main"]
sys.path.insert(0, str(_root))
from evaluator.semantic_execution import case_files as _dyad_case_files, provenance_errors as _dyad_provenance_errors
from evaluator.semantic_execution import owned_file as _dyad_owned_file

# Same table semantic_execution.case_files builds, used only to answer WHICH
# evidence file is absent once the strict build has already refused.
_DYAD_EVIDENCE_KEYS = (
    ("case_input", "executed_task_path", "executed_task_sha256"),
    ("artifact", "artifact_path", "artifact_sha256"),
    ("trajectory", "trajectory_path", None),
    ("native", "native_evidence_path", "native_evidence_sha256"),
    ("oracle", "private_oracle_comparison_path", "private_oracle_comparison_sha256"),
)


def _exact_case_files(run_dir, case_id, record, hidden_path):
    if record.get("case_id") != case_id:
        raise ValueError("case record identity mismatch")
    try:
        return _dyad_case_files(record, run_dir)
    except ValueError as exc:
        # The shared publisher already refuses a single case whose lower-agent
        # evidence is absent ("missing lower-agent evidence: ..."), by reading
        # None entries out of this mapping.  Raising instead made one case's
        # absent evidence abort the entire Result axis.  A CHANGED (tampered)
        # file still raises: only absence degrades to a None entry.
        if not str(exc).startswith("missing evaluator evidence"):
            raise
        files = {}
        for name, key, hash_key in _DYAD_EVIDENCE_KEYS:
            try:
                files[name] = _dyad_owned_file(record, key, run_dir, hash_key)
            except (OSError, ValueError, TypeError, KeyError):
                files[name] = None
        return files


_shared["case_files"] = _exact_case_files
_shared["validate_model_artifact_provenance"] = _dyad_provenance_errors



from evaluator.frozen_evidence import validate_freeze as _validate_dyad_freeze, hidden_timing_errors as _dyad_timing_errors


def frozen_identity_errors(freeze, run_dir):
    try:
        _validate_dyad_freeze(freeze, run_dir)
    except (OSError, ValueError, KeyError, TypeError, IndexError, RuntimeError, AttributeError) as exc:
        return ["Dyad frozen identity invalid: " + str(exc)]
    return []


def code_frozen_identity(freeze, run_dir):
    document, candidate = _validate_dyad_freeze(freeze, run_dir)
    create = runpy.run_path('@@AGENTSWE_EDITING_CONTROL@@/code_eval.py',
                           run_name='dyad_create_digest_only')
    canonical_digest = create['tree_digest'](candidate)
    final_document, final_candidate = _validate_dyad_freeze(freeze, run_dir)
    if final_document != document or final_candidate != candidate:
        raise ValueError('frozen identity changed during canonical digest verification')
    return {'schema_version': 'agentswe-edit-dual-frozen-identity/v1', 'valid': True,
            'candidate_path': str(candidate), 'freeze_sha256': _shared['sha256_file'](document),
            'lifecycle_candidate_digest': freeze['candidate_digest'],
            'code_candidate_digest': canonical_digest,
            'algorithms': {'lifecycle': 'Dyad builder_protocol.tree_digest includes directories',
                           'code': 'Create code_eval.tree_digest files-and-symlinks'}}


_shared_hidden_lifecycle_errors = _shared['hidden_lifecycle_errors']


def hidden_lifecycle_errors(hidden, freeze, run_dir, selected=_shared['CASES']):
    return (_shared_hidden_lifecycle_errors(hidden, freeze, run_dir, selected)
            + _dyad_timing_errors(hidden, freeze, run_dir, selected))


_shared['frozen_identity_errors'] = frozen_identity_errors
_shared['code_frozen_identity'] = code_frozen_identity
_shared['hidden_lifecycle_errors'] = hidden_lifecycle_errors


def _run_dir_from_argv(argv: list[str] | None) -> Path:
    values = list(sys.argv[1:] if argv is None else argv)
    try:
        raw = values[values.index("--run-dir") + 1]
    except (ValueError, IndexError) as exc:
        raise ValueError("formal axes requires --run-dir") from exc
    return Path(raw).resolve()


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
    """Build a run-local shared-judge input root with exact executed tasks.

    Static sibling prompts are deliberately not a fallback: every hidden case
    must provide its executed task and private comparison from the lower run.
    """
    hidden_path, hidden = _shared["hidden_document"](run_dir)
    if hidden_path is None:
        raise ValueError("missing hidden-after-freeze evidence for exact-task routing")
    overlay = run_dir / "formal_scoring" / "run_local_input_root"
    overlay.mkdir(parents=True, exist_ok=True)
    input_link = overlay / "input"
    if not input_link.exists():
        input_link.symlink_to(_root / "input", target_is_directory=True)
    evaluator_dir = overlay / "evaluator"
    evaluator_dir.mkdir(exist_ok=True)
    # result_dimensions.json must travel with the rubric: the shared judge looks
    # for it next to the rubric it is given, and readiness resolves it from the
    # real task tree.  Without it the formal run would silently fall back to the
    # legacy 50/30/20 contract while readiness used the task-local dimensions.
    for name in ("code_rubric.md", "result_rubric.md", "result_dimensions.json"):
        source = _root / "evaluator" / name
        destination = evaluator_dir / name
        if not destination.exists() and source.exists():
            destination.symlink_to(source)
    routed: dict[str, Path] = {}
    for case_id in (_shared["CASES"] if case_ids is None else case_ids):
        record = _shared["find_case_record"](hidden, case_id)
        if not isinstance(record, dict):
            raise ValueError(f"missing hidden record for {case_id}")
        task_raw = record.get("executed_task_path")
        comparison_raw = record.get("private_oracle_comparison_path")
        if not isinstance(task_raw, str) or not isinstance(comparison_raw, str):
            raise ValueError(f"{case_id} lacks exact executed-task/oracle paths")
        task = Path(task_raw).resolve()
        comparison = Path(comparison_raw).resolve()
        if not task.is_file() or not comparison.is_file():
            raise ValueError(f"{case_id} exact task or private comparison is missing")
        if not _inside(task, run_dir) or not _inside(comparison, run_dir):
            raise ValueError(f"{case_id} exact task or private comparison escapes the run directory")
        comparison_value = _read_object(comparison)
        task_sha256 = hashlib.sha256(task.read_bytes()).hexdigest()
        comparison_sha256 = hashlib.sha256(comparison.read_bytes()).hexdigest()
        if task_sha256 != record.get("executed_task_sha256"):
            raise ValueError(f"{case_id} executed task bytes do not match its identity")
        if comparison_sha256 != record.get("private_oracle_comparison_sha256"):
            raise ValueError(f"{case_id} private comparison bytes do not match its identity")
        if comparison_value.get("case_id") != case_id or comparison_value.get("private_oracle_not_candidate_visible") is not True:
            raise ValueError(f"{case_id} private comparison case mismatch")
        if comparison_value.get("executed_task_sha256") != record.get("executed_task_sha256"):
            raise ValueError(f"{case_id} private comparison task identity mismatch")
        task_dir = overlay / "test_cases" / case_id
        task_dir.mkdir(parents=True, exist_ok=True)
        destination = task_dir / "input.md"
        shutil.copyfile(task, destination)
        routed[case_id] = comparison
    return overlay, routed


def main(argv: list[str] | None = None) -> int:
    run_dir = _run_dir_from_argv(argv)
    if _shared["hidden_document"](run_dir)[0] is None:
        return _shared_main(argv)
    overlay, comparisons = prepare_run_local_formal_root(run_dir, _shared["selected_case_ids"](argv))
    original_root = _shared["ROOT"]
    original_provenance = _shared["provenance_summary"]

    def run_local_provenance(case_id, record, artifact, destination):
        source = comparisons.get(case_id)
        if source is None:
            raise ValueError(f"missing run-local private comparison for {case_id}")
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(source, destination)
        return destination

    _shared["ROOT"] = overlay
    _shared["provenance_summary"] = run_local_provenance
    try:
        return _shared_main(argv)
    finally:
        _shared["ROOT"] = original_root
        _shared["provenance_summary"] = original_provenance
