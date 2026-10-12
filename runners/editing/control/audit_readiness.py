#!/usr/bin/env python3
from __future__ import annotations

import argparse
import fcntl
import hashlib
import importlib.util
import json
import os
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from readiness_admission import check_admission, PROFILE


ROOT = Path(__file__).resolve().parent
REQUIRED_CASE_KEYS = {
    "task", "sibling", "dev_cases", "hidden_cases", "primary_user_goal_by_case",
    "primary_failure_axis_by_case", "secondary_failure_axes_by_case", "dynamic_facts",
    "candidate_visible_inputs", "evaluator_only_oracle", "real_lower_entry",
    "product_api_entry", "broker_model", "broker_effort", "agent_authored_artifact",
    "native_harness_role", "result_judge_entry", "code_judge_entry",
    "formal_one_stop_entry", "known_overlap_or_leakage", "readiness",
}
CANONICAL_DEV = ["dev_001", "dev_002"]
CANONICAL_HIDDEN = [f"test_{index:03d}" for index in range(1, 7)]
SMOKE_DIGEST_EXCLUSIONS = {
    ".git", "__pycache__", ".pytest_cache", ".mypy_cache", ".ruff_cache",
    "node_modules", ".runtime", ".formal_runs", "artifacts", "outputs",
}


def load_config() -> Any:
    path = ROOT / "formal_config.py"
    spec = importlib.util.spec_from_file_location("edit_formal_config", path)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"cannot load {path}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        while chunk := handle.read(1024 * 1024):
            digest.update(chunk)
    return digest.hexdigest()


def tree_digest(root: Path) -> str:
    """Digest the current sibling while excluding generated runtime state."""
    digest = hashlib.sha256()
    for directory, dirnames, filenames in os.walk(root, followlinks=False):
        dirnames[:] = sorted(name for name in dirnames if name not in SMOKE_DIGEST_EXCLUSIONS)
        base = Path(directory)
        for name in sorted(filenames):
            path = base / name
            relative = path.relative_to(root).as_posix()
            parts = Path(relative).parts
            if any(part in SMOKE_DIGEST_EXCLUSIONS for part in parts) or path.suffix in {".pyc", ".pyo"}:
                continue
            rel = relative.encode("utf-8")
            if path.is_symlink():
                payload = os.readlink(path).encode("utf-8")
                digest.update(b"L")
                digest.update(len(rel).to_bytes(8, "big"))
                digest.update(rel)
                digest.update(len(payload).to_bytes(8, "big"))
                digest.update(payload)
            elif path.is_file():
                digest.update(b"F")
                digest.update(len(rel).to_bytes(8, "big"))
                digest.update(rel)
                digest.update(path.stat().st_size.to_bytes(8, "big"))
                with path.open("rb") as handle:
                    while chunk := handle.read(1024 * 1024):
                        digest.update(chunk)
    return digest.hexdigest()


def write_json(path: Path, value: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(value, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    os.replace(temporary, path)


def path_from_contract(sibling: Path, value: object) -> Path | None:
    if not isinstance(value, str) or not value:
        return None
    path = Path(value)
    return path if path.is_absolute() else sibling / path


def path_under(path: Path, root: Path) -> bool:
    try:
        path.resolve().relative_to(root.resolve())
        return True
    except ValueError:
        return False


def prompt_scan(sibling: Path) -> list[str]:
    findings: list[str] = []
    patterns = {
        "score_weight": re.compile(r"\b(?:score weights?|rubric weights?|\d+\s*points?)\b", re.I),
        "oracle": re.compile(r"\b(?:private oracle|expected decision|correct terminal answer)\b", re.I),
        "credential": re.compile(r"\b(?:DEEPSEEK_API_KEY|OPENAI_API_KEY)\s*=", re.I),
    }
    for group in ("dev_cases", "test_cases"):
        root = sibling / group
        if not root.is_dir():
            continue
        task_files = sorted(root.glob("*/input.md")) + sorted(root.glob("*/task.md"))
        for path in task_files:
            text = path.read_text(encoding="utf-8", errors="replace")
            for label, pattern in patterns.items():
                if pattern.search(text):
                    findings.append(f"{path.relative_to(sibling)}:{label}")
    return findings


def _main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--matrix", type=Path, default=ROOT / "case_coverage_matrix.json")
    parser.add_argument("--gate", type=Path, default=ROOT / "formal_readiness_gate.json")
    args = parser.parse_args()
    cfg = load_config()
    snapshot_path = ROOT / "post_repair_tree_snapshot.json"
    snapshot: dict[str, Any] = {}
    if snapshot_path.is_file():
        try:
            raw_snapshot = json.loads(snapshot_path.read_text(encoding="utf-8"))
            snapshot = raw_snapshot if isinstance(raw_snapshot, dict) else {}
        except Exception:
            snapshot = {}
    rows: list[dict[str, Any]] = []
    gate_tasks: list[dict[str, Any]] = []
    for task, sibling in cfg.TASKS.items():
        contract_path = sibling / "meta/0905_case_contract.json"
        errors: list[str] = []
        current_sibling_digest = tree_digest(sibling) if sibling.is_dir() else None
        snapshot_sibling_digest = (((snapshot.get("tasks") or {}).get(task) or {}).get("sibling") or {}).get("digest")
        if current_sibling_digest != snapshot_sibling_digest:
            errors.append("current sibling digest does not match post-repair snapshot")
        if not contract_path.is_file():
            contract: dict[str, Any] = {"task": task, "sibling": str(sibling), "readiness": "REPAIR"}
            errors.append("missing meta/0905_case_contract.json")
        else:
            try:
                value = json.loads(contract_path.read_text(encoding="utf-8"))
                contract = value if isinstance(value, dict) else {}
            except Exception as exc:
                contract = {}
                errors.append(f"invalid case contract: {exc}")
        missing_keys = sorted(REQUIRED_CASE_KEYS - set(contract))
        if missing_keys:
            errors.append(f"missing contract keys: {missing_keys}")
        if contract.get("task") != task:
            errors.append(f"case contract task mismatch: {contract.get('task')!r} != {task!r}")
        if contract.get("sibling") != str(sibling):
            errors.append("case contract sibling mismatch")
        if contract.get("dev_cases") != CANONICAL_DEV:
            errors.append("dev inventory is not dev_001+dev_002")
        if contract.get("hidden_cases") != CANONICAL_HIDDEN:
            errors.append("hidden inventory is not test_001..test_006")
        if contract.get("broker_model") != "deepseek-flash" or contract.get("broker_effort") != "high":
            errors.append("lower broker model/effort mismatch")
        for field in ("real_lower_entry", "result_judge_entry", "code_judge_entry", "formal_one_stop_entry"):
            path = path_from_contract(sibling, contract.get(field))
            if path is None or not path.is_file():
                errors.append(f"{field} missing: {contract.get(field)!r}")
        if not isinstance(contract.get("product_api_entry"), str) or not contract.get("product_api_entry", "").strip():
            errors.append("product_api_entry is missing or empty")
        for directory in ("README.md", "input", "dev_cases", "test_cases", "evaluator", "meta"):
            if not (sibling / directory).exists():
                errors.append(f"required benchmark package path missing: {directory}")
        one_stop = sibling / "harbor/formal_one_stop.py"
        if one_stop.is_file():
            text = one_stop.read_text(encoding="utf-8", errors="replace")
            runtime_files = [one_stop]
            for relative in (
                "evaluator/formal_finalize.py", "evaluator/semantic_finalize.py",
                "evaluator/formal_axes.py", "evaluator/dev_lifecycle.py",
                "harbor/one_stop_contract.py",
            ):
                path = sibling / relative
                if path.is_file():
                    runtime_files.append(path)
            sibling_runtime_refs: list[str] = []
            for path in runtime_files:
                source = path.read_text(encoding="utf-8", errors="replace")
                if re.search(r"@@AGENTSWE_EDITING_SOURCES@@/\d{2}-edit-[^\"']+", source):
                    sibling_runtime_refs.append(str(path.relative_to(sibling)))
            if sibling_runtime_refs:
                errors.append(
                    "formal runtime imports mutable sibling paths: "
                    + ", ".join(sibling_runtime_refs)
                )
            for marker in ("max_dev_rounds", "n_concurrent"):
                if marker not in text:
                    errors.append(f"formal one-stop lacks {marker}")
            if task == "codex":
                if 'add_argument("--benchmark"' not in text:
                    errors.append("Codex formal one-stop lacks its required --benchmark input")
            elif re.search(r"add_argument\(\s*[\"']--run-formal[\"']", text) is None:
                errors.append("formal one-stop lacks explicit --run-formal authorization")
            if "if args.run_formal and not args.result_judge_broker_endpoint" in text:
                errors.append(
                    "formal one-stop requires an external Result-judge broker endpoint "
                    "without owning that broker lifecycle"
                )
            finalizer_paths = [
                sibling / "evaluator/formal_finalize.py",
                sibling / "evaluator/semantic_finalize.py",
                sibling / "evaluator/formal_axes.py",
            ]
            finalizer_text = "\n".join(
                path.read_text(encoding="utf-8", errors="replace")
                for path in finalizer_paths if path.is_file()
            )
            scoring_text = text + "\n" + finalizer_text
            if "result_judge.py" not in scoring_text and "AGENTSWE_RESULT_JUDGE" not in scoring_text:
                errors.append("formal one-stop/finalizer does not reference shared semantic Result judge")
            if "code_eval.py" not in scoring_text and "AGENTSWE_CODE_JUDGE" not in scoring_text:
                errors.append("formal one-stop/finalizer does not reference Create Code judge")
        else:
            errors.append("formal one-stop file missing")
        if task == "ai-scientist":
            for case_id in CANONICAL_HIDDEN:
                for name in ("task.md", "case_input.json"):
                    if not (sibling / "agentloop/cases" / case_id / name).is_file():
                        errors.append(f"AI Scientist lower task missing {case_id}/{name}")
            ai_controller = sibling / "agentloop/two_round_controller.py"
            ai_launcher = sibling / "agentloop/lower_agent_launcher.py"
            ai_finalizer = sibling / "evaluator/semantic_finalize.py"
            ai_controller_text = ai_controller.read_text(encoding="utf-8", errors="replace") if ai_controller.is_file() else ""
            ai_launcher_text = ai_launcher.read_text(encoding="utf-8", errors="replace") if ai_launcher.is_file() else ""
            ai_finalizer_text = ai_finalizer.read_text(encoding="utf-8", errors="replace") if ai_finalizer.is_file() else ""
            if not all(marker in ai_controller_text for marker in (
                "agentloop\" / \"lower_agent_launcher.py",
                "def run_hidden",
                "self.hidden_case_ids",
            )):
                errors.append("AI Scientist formal controller does not prove all configured hidden cases route through lower_agent_launcher.py")
            if not all(marker in ai_launcher_text for marker in (
                "launch_scientist_bfts.py",
                "LOWER_MODEL",
                "LOWER_EFFORT",
                "agent_result.json",
            )):
                errors.append("AI Scientist lower launcher does not prove product/model artifact routing")
            if not all(marker in ai_finalizer_text for marker in (
                "deepseek-flash/medium",
                "model-authored agent_result.json is missing",
                "SHARED_RESULT_JUDGE",
            )):
                errors.append("AI Scientist semantic finalizer lacks lower-model/artifact/shared-judge fail-closed markers")
        if task == "openhands" and one_stop.is_file():
            text = one_stop.read_text(encoding="utf-8", errors="replace")
            if "two_round_controller" not in text or "openhands_lower_agent.py" not in text:
                errors.append("OpenHands formal call chain does not prove controller -> lower launcher")
            oh_controller = sibling / "evaluator/controller/two_round_controller.py"
            oh_native = sibling / "evaluator/harness/evaluate_suite.py"
            oh_finalizer = sibling / "evaluator/semantic_finalize.py"
            oh_lower = sibling / "lower_agent/openhands_lower_agent.py"
            oh_controller_text = oh_controller.read_text(encoding="utf-8", errors="replace") if oh_controller.is_file() else ""
            oh_native_text = oh_native.read_text(encoding="utf-8", errors="replace") if oh_native.is_file() else ""
            oh_finalizer_text = oh_finalizer.read_text(encoding="utf-8", errors="replace") if oh_finalizer.is_file() else ""
            oh_lower_text = oh_lower.read_text(encoding="utf-8", errors="replace") if oh_lower.is_file() else ""
            if not all(marker in oh_controller_text for marker in (
                "def run_hidden",
                "self.hidden_cases",
                "hidden execution before accepted Candidate freeze",
            )):
                errors.append("OpenHands hidden controller lacks configured-inventory and post-freeze proof")
            if "native diagnostic evidence only and cannot publish formal Result" not in oh_native_text:
                errors.append("OpenHands native evaluate_suite.py is not explicitly barred from formal Result publication")
            if not all(marker in oh_finalizer_text for marker in (
                "model-authored agent_result.json is missing",
                "SHARED_RESULT_JUDGE",
            )):
                errors.append("OpenHands semantic finalizer lacks model-artifact/shared-judge fail-closed markers")
            if not all(marker in oh_lower_text for marker in (
                "ConversationService",
                "agent_result.json",
                "model-selected",
            )):
                errors.append("OpenHands lower launcher does not prove model-selected Candidate ConversationService execution and artifact output")
        prompt_findings = prompt_scan(sibling)
        if prompt_findings:
            errors.append(f"prompt leakage scan findings: {prompt_findings}")
        smoke_manifest = cfg.SMOKE_ROOT / task / "latest_smoke_manifest.json"
        self_readiness = contract.get("readiness")
        admission_ok, admission = check_admission(
            ROOT / "readiness_admissions", task=task,
            sibling_digest=current_sibling_digest,
            contract_path=contract_path, smoke_path=smoke_manifest,
        )
        smoke_ok = admission_ok
        if not smoke_ok:
            errors.append("current v2 evaluator-owned two-round readiness evidence missing or incomplete")
        if admission and admission.get("error"):
            errors.append(admission["error"])
        # Review metadata lives outside the executed tree: changing a status
        # must not invalidate or relabel the source of a completed smoke.
        # Every prior smoke/source/judge gate above remains mandatory.
        effective_readiness = "READY" if admission_ok and not errors else "REPAIR"
        row = {
            **contract,
            "task": task,
            "sibling": str(sibling),
            "sibling_digest": current_sibling_digest,
            "snapshot_sibling_digest": snapshot_sibling_digest,
            "contract_path": str(contract_path),
            "contract_digest": sha256(contract_path) if contract_path.is_file() else None,
            "prompt_scan_findings": prompt_findings,
            "smoke_manifest": str(smoke_manifest),
            "smoke_ok": smoke_ok,
            "audit_errors": errors,
            "declared_readiness": self_readiness,
            "evaluator_readiness_admission": admission,
            "readiness": effective_readiness,
            "readiness_blockers": [] if effective_readiness == "READY" else
                ([contract.get("remaining_blocker")] if contract.get("remaining_blocker") else []) + errors,
        }
        # Normalize judge metadata in the generated matrix so every task
        # exposes the same auditable protocol shape. Legacy declarations are
        # retained separately for provenance.
        raw_result_judge = row.get("result_judge")
        raw_code_judge = row.get("code_judge")
        if not isinstance(raw_result_judge, dict):
            if raw_result_judge is not None:
                row["result_judge_legacy"] = raw_result_judge
            raw_result_judge = {}
        if not isinstance(raw_code_judge, dict):
            if raw_code_judge is not None:
                row["code_judge_legacy"] = raw_code_judge
            raw_code_judge = {}
        row["result_judge"] = {
            **raw_result_judge,
            "entry": row.get("result_judge_entry"),
            "model": "deepseek-flash",
            "reasoning_effort": "max",
            "logical_requests_per_scoreable_hidden": 1,
            "completed_responses_required": 1,
            "task_local_rubric": True,
        }
        row["code_judge"] = {
            **raw_code_judge,
            "entry": row.get("code_judge_entry"),
            "model": "deepseek-flash",
            "reasoning_effort": "max",
            "candidate_level_evaluations": 1,
            "independent_from_result": True,
            "rubric": {
                "aider": "meta/code_rubric.md",
                "openhands": "evaluator/code_axis/code_rubric.json",
                "openwiki": "agentloop/evaluator/code_rubric.md",
            }.get(task, "evaluator/code_rubric.md"),
        }
        rows.append(row)
        gate_tasks.append({"task": task, "readiness": effective_readiness, "errors": errors})
    ready = sum(item["readiness"] == "READY" for item in gate_tasks)
    source_before = ROOT / "pre_repair_tree_snapshot.json"
    source_after = ROOT / "post_repair_tree_snapshot.json"
    source_unchanged: bool | None = None
    source_differences: list[str] = []
    if source_before.is_file() and source_after.is_file():
        before = json.loads(source_before.read_text(encoding="utf-8"))
        after = json.loads(source_after.read_text(encoding="utf-8"))
        for task in cfg.TASKS:
            left = before["tasks"][task]["source"]["digest"]
            source_path = Path(before["tasks"][task]["source"]["path"])
            right = tree_digest(source_path) if source_path.is_dir() else None
            if left != right:
                source_differences.append(task)
        source_unchanged = not source_differences
    matrix = {
        "schema_version": "agentswe-edit-case-coverage-matrix-v2",
        "profile": PROFILE,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "tasks": rows,
    }
    gate = {
        "schema_version": "agentswe-edit-formal-readiness-gate-v2",
        "profile": PROFILE,
        "score_threshold": None,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "ready_count": ready,
        "expected_count": 10,
        "formal_ready": ready == 10 and source_unchanged is True,
        "source_unchanged": source_unchanged,
        "source_differences": source_differences,
        "tasks": gate_tasks,
        "formal_launch_authorized_by_gate": ready == 10 and source_unchanged is True,
        "formal_launch_authorized": ready == 10 and source_unchanged is True,
        "formal_evaluation_started": False,
    }
    write_json(args.matrix, matrix)
    write_json(args.gate, gate)
    print(json.dumps({"ready_count": ready, "formal_ready": gate["formal_ready"], "source_unchanged": source_unchanged}, indent=2))
    return 0 if gate["formal_ready"] else 2


def main() -> int:
    # All audit invocations, including the formal launcher, share one writer lock.
    with (ROOT / "configuration_delta_registry.lock").open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        return _main()


if __name__ == "__main__":
    raise SystemExit(main())
