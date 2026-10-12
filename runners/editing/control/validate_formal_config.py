#!/usr/bin/env python3
from __future__ import annotations

import argparse
import hashlib
import importlib.util
import json
import os
from pathlib import Path
from typing import Any

from formal_provenance import validate_alignment, validate_configuration_registry
from control_runtime import control_command, probe_control_runtime
from formal_commands import build_formal_command


ROOT = Path(__file__).resolve().parent
DIGEST_EXCLUSIONS = {
    ".git", "__pycache__", ".pytest_cache", ".mypy_cache", ".ruff_cache",
    "node_modules", ".runtime", ".formal_runs", "artifacts", "outputs",
}


def load_module(path: Path) -> Any:
    spec = importlib.util.spec_from_file_location("formal_config", path)
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
    digest = hashlib.sha256()
    for directory, dirnames, filenames in os.walk(root, followlinks=False):
        dirnames[:] = sorted(name for name in dirnames if name not in DIGEST_EXCLUSIONS)
        base = Path(directory)
        for name in sorted(filenames):
            path = base / name
            relative = path.relative_to(root).as_posix()
            if any(part in DIGEST_EXCLUSIONS for part in Path(relative).parts) or path.suffix in {".pyc", ".pyo"}:
                continue
            rel = relative.encode("utf-8")
            if path.is_symlink():
                payload = os.readlink(path).encode("utf-8")
                digest.update(b"L" + len(rel).to_bytes(8, "big") + rel)
                digest.update(len(payload).to_bytes(8, "big") + payload)
            elif path.is_file():
                digest.update(b"F" + len(rel).to_bytes(8, "big") + rel)
                digest.update(path.stat().st_size.to_bytes(8, "big"))
                with path.open("rb") as handle:
                    while chunk := handle.read(1024 * 1024):
                        digest.update(chunk)
    return digest.hexdigest()


def load_readiness_gate() -> tuple[dict[str, Any], list[str]]:
    path = ROOT / "formal_readiness_gate.json"
    if not path.is_file():
        return {}, [f"missing formal readiness gate: {path}"]
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except Exception as exc:
        return {}, [f"invalid formal readiness gate: {exc}"]
    if not isinstance(value, dict):
        return {}, ["formal readiness gate must be a JSON object"]
    return value, []


def command_template(task: str, sibling: Path, credential_file: Path, python: Path) -> list[str]:
    return build_formal_command(task, sibling, Path('<fresh-run-dir>'), credential_file, python)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", type=Path, default=ROOT / "formal_launch_manifest.json")
    parser.add_argument("--require-ready", action="store_true")
    args = parser.parse_args()
    cfg = load_module(ROOT / "formal_config.py")
    errors: list[str] = []
    gate, gate_errors = load_readiness_gate()
    errors.extend(gate_errors)
    if gate.get("profile") != "single-dev-two-round-hidden-smoke-v1":
        errors.append("formal admission requires the v2 readiness profile")
    snapshot_path = ROOT / "post_repair_tree_snapshot.json"
    try:
        snapshot = json.loads(snapshot_path.read_text(encoding="utf-8"))
        if not isinstance(snapshot, dict):
            raise ValueError("snapshot must be a JSON object")
    except Exception as exc:
        snapshot = {}
        errors.append(f"invalid post-repair tree snapshot: {exc}")
    gate_tasks = {
        item.get("task"): item
        for item in gate.get("tasks", [])
        if isinstance(item, dict) and isinstance(item.get("task"), str)
    }
    audited_ready_count = gate.get("ready_count")
    expected_count = gate.get("expected_count")
    formal_ready = gate.get("formal_ready") is True
    source_unchanged = gate.get("source_unchanged") is True
    formal_launch_authorized = (
        gate.get("profile") == "single-dev-two-round-hidden-smoke-v1"
        and formal_ready
        and source_unchanged
        and audited_ready_count == 10
        and expected_count == 10
        and gate.get("formal_launch_authorized_by_gate") is True
        and gate.get("formal_launch_authorized") is True
    )
    if gate.get("formal_launch_authorized") is not (formal_ready and source_unchanged and audited_ready_count == 10 and expected_count == 10):
        errors.append("readiness gate formal_launch_authorized is inconsistent with its explicit state")
    if expected_count != 10:
        errors.append(f"readiness gate expected_count must be 10, found {expected_count!r}")
    if tuple(cfg.SELECTED_PROFILES) != ("codex_xhigh",):
        errors.append("selected_profiles must equal ['codex_xhigh']")
    if len(cfg.TASKS) != 10:
        errors.append(f"expected exactly 10 tasks, found {len(cfg.TASKS)}")
    if cfg.MAX_DEV_ROUNDS != 5 or cfg.N_CONCURRENT != 1:
        errors.append("max_dev_rounds/n_concurrent must be 10/1")
    if cfg.DEV_PASSED_IS_AUTOMATIC_FREEZE is not False:
        errors.append("dev_passed must not automatically freeze")
    profile = cfg.PROFILES.get("codex_xhigh", {})
    expected_profile = {
        "builder_agent": "codex",
        "builder_model": "deepseek-flash",
        "builder_reasoning_effort": "max",
        "builder_harness_version": "0.144.1",
        "provider": "gateway-responses",
    }
    if profile != expected_profile:
        errors.append(f"codex_xhigh profile mismatch: {profile}")
    required_shared = (
        cfg.ALIGNMENT_SNAPSHOT, cfg.RESULT_JUDGE, cfg.FORMAL_AXES_SHARED,
        cfg.BUILDER_BROKER_XHIGH, cfg.BUILDER_BROKER_RUNTIME, cfg.BUILDER_REQUEST_LEDGER,
        cfg.RESPONSES_STREAM, cfg.RESPONSES_BROKER_XHIGH,
        cfg.ONE_STOP_CONTRACT_SHARED, cfg.DEV_LIFECYCLE_SHARED,
        cfg.CREATE_CODE_JUDGE,
        cfg.AUTHORITATIVE_CREATE_CODE_JUDGE,
        cfg.JUDGE_BROKER_XHIGH, cfg.JUDGE_BROKER_RUNTIME,
        ROOT / 'formal_commands.py', ROOT / 'readiness_admission.py',
    )
    for path in required_shared:
        if not path.is_file():
            errors.append(f"missing shared file: {path}")
    alignment_evidence: dict[str, Any] = {}
    configuration_evidence: dict[str, Any] = {}
    control_evidence: dict[str, Any] = {}
    try:
        control_evidence = probe_control_runtime(cfg.CONTROL_PYTHON, cfg.TASKS)
    except Exception as exc:
        errors.append(f"Host control Python preflight failed: {exc}")
    try:
        if getattr(cfg, "PROVENANCE_MODE", "paper") == "release":
            alignment_evidence = {
                "status": "paper-provenance: not available in release",
                "paper_record": {"path": str(cfg.ALIGNMENT_SNAPSHOT), "sha256": sha256(cfg.ALIGNMENT_SNAPSHOT)},
            }
        else:
            alignment_evidence = validate_alignment(
                cfg.ALIGNMENT_SNAPSHOT, cfg.SELECTED_CREATE_PROVENANCE, cfg.PROFILES,
                cfg.AUTHORITATIVE_CREATE_CODE_JUDGE.parent)
    except (OSError, ValueError, KeyError, TypeError, AttributeError) as exc:
        errors.append(f"Create alignment provenance is not verified: {exc}")
    try:
        configuration_evidence, configuration_errors = validate_configuration_registry(
            cfg.CONFIGURATION_DELTA_REGISTRY, cfg.TASKS)
        errors.extend(configuration_errors)
    except (OSError, ValueError, KeyError, TypeError, AttributeError) as exc:
        errors.append(f"Configuration difference evidence is not verified: {exc}")
    task_entries: list[dict[str, Any]] = []
    declared_ready_count = 0
    for task, sibling in cfg.TASKS.items():
        item_errors: list[str] = []
        current_sibling_digest = tree_digest(sibling) if sibling.is_dir() else None
        snapshot_sibling_digest = (
            (((snapshot.get("tasks") or {}).get(task) or {}).get("sibling") or {}).get("digest")
        )
        if not sibling.is_dir():
            item_errors.append("sibling missing")
        elif current_sibling_digest != snapshot_sibling_digest:
            item_errors.append(
                "current sibling digest does not match post-repair snapshot: "
                f"current={current_sibling_digest}, snapshot={snapshot_sibling_digest}"
            )
        one_stop = sibling / "harbor/formal_one_stop.py"
        contract = sibling / "meta/0905_case_contract.json"
        if not one_stop.is_file():
            item_errors.append("formal one-stop missing")
        if not contract.is_file():
            item_errors.append("0905 case contract missing")
            contract_value: dict[str, Any] = {}
        else:
            try:
                raw = json.loads(contract.read_text(encoding="utf-8"))
                contract_value = raw if isinstance(raw, dict) else {}
            except Exception as exc:
                contract_value = {}
                item_errors.append(f"invalid case contract: {exc}")
        if contract_value.get("task") != task:
            item_errors.append(f"case contract task mismatch: {contract_value.get('task')!r}")
        if contract_value.get("sibling") != str(sibling):
            item_errors.append("case contract sibling mismatch")
        declared_readiness = contract_value.get("readiness")
        if declared_readiness == "READY":
            declared_ready_count += 1
        audited_task = gate_tasks.get(task, {})
        readiness = audited_task.get("readiness")
        if readiness not in {"READY", "REPAIR", "BLOCKED"}:
            item_errors.append(f"missing or invalid audited readiness: {readiness!r}")
        if args.require_ready and readiness != "READY":
            item_errors.append(f"audited readiness is {readiness!r}, not READY")
        task_entries.append({
            "task": task,
            "sibling": str(sibling),
            "sibling_digest": current_sibling_digest,
            "one_stop": str(one_stop),
            "case_contract": str(contract),
            "readiness": readiness,
            "declared_readiness": declared_readiness,
            "readiness_errors": audited_task.get("errors", []),
            "errors": item_errors,
        })
        errors.extend(f"{task}: {message}" for message in item_errors)
    if args.require_ready and not formal_launch_authorized:
        errors.append(
            "formal readiness gate does not authorize launch: "
            f"ready_count={audited_ready_count!r}, expected_count={expected_count!r}, "
            f"formal_ready={formal_ready}, source_unchanged={source_unchanged}"
        )
    branches = [
        {
            "task": task,
            "profile": "codex_xhigh",
            "sibling": str(path),
            "command_template": command_template(task, path, cfg.CREDENTIAL_FILE, cfg.CONTROL_PYTHON),
        }
        for task, path in cfg.TASKS.items()
    ]
    if len(branches) != 10 or any(item["profile"] != "codex_xhigh" for item in branches):
        errors.append("branch inventory must contain exactly ten codex_xhigh branches")
    # A stale/true readiness flag cannot override a failed configuration check.
    formal_launch_authorized = formal_launch_authorized and not errors
    manifest = {
        "schema_version": "agentswe-0905-edit-formal-launch-v1",
        "ready_count": audited_ready_count,
        "expected_count": expected_count,
        "declared_ready_count": declared_ready_count,
        "formal_ready": formal_ready,
        "source_unchanged": source_unchanged,
        "formal_launch_authorized": formal_launch_authorized,
        "formal_evaluation_started": False,
        "profile_matrix": cfg.PROFILES,
        "selected_profiles": list(cfg.SELECTED_PROFILES),
        "profile_scope": "codex_xhigh_only",
        "expected_branch_count": 10,
        "branches": branches,
        "tasks": task_entries,
        "max_dev_rounds": cfg.MAX_DEV_ROUNDS,
        "n_concurrent": cfg.N_CONCURRENT,
        "dev_passed_is_automatic_freeze": cfg.DEV_PASSED_IS_AUTOMATIC_FREEZE,
        "builder": expected_profile,
        "lower": {"model": "deepseek-flash", "reasoning_effort": "high", "transport": "evaluator-owned Responses broker"},
        "result_judge": {"model": "deepseek-flash", "reasoning_effort": "max", "logical_requests_per_scoreable_hidden": 1},
        "formal_axes_shared": {"path": str(cfg.FORMAL_AXES_SHARED), "sha256": sha256(cfg.FORMAL_AXES_SHARED) if cfg.FORMAL_AXES_SHARED.is_file() else None},
        "shared_runtime": {
            "formal_commands": {"path": str(ROOT / 'formal_commands.py'), "sha256": sha256(ROOT / 'formal_commands.py') if (ROOT / 'formal_commands.py').is_file() else None},
            "readiness_admission": {"path": str(ROOT / 'readiness_admission.py'), "sha256": sha256(ROOT / 'readiness_admission.py') if (ROOT / 'readiness_admission.py').is_file() else None},
            "judge_broker_xhigh": {"path": str(cfg.JUDGE_BROKER_XHIGH), "sha256": sha256(cfg.JUDGE_BROKER_XHIGH) if cfg.JUDGE_BROKER_XHIGH.is_file() else None},
            "judge_broker_runtime": {"path": str(cfg.JUDGE_BROKER_RUNTIME), "sha256": sha256(cfg.JUDGE_BROKER_RUNTIME) if cfg.JUDGE_BROKER_RUNTIME.is_file() else None},
            "builder_broker_xhigh": {"path": str(cfg.BUILDER_BROKER_XHIGH), "sha256": sha256(cfg.BUILDER_BROKER_XHIGH) if cfg.BUILDER_BROKER_XHIGH.is_file() else None},
            "builder_broker_runtime": {"path": str(cfg.BUILDER_BROKER_RUNTIME), "sha256": sha256(cfg.BUILDER_BROKER_RUNTIME) if cfg.BUILDER_BROKER_RUNTIME.is_file() else None},
            "builder_request_ledger": {"path": str(cfg.BUILDER_REQUEST_LEDGER), "sha256": sha256(cfg.BUILDER_REQUEST_LEDGER) if cfg.BUILDER_REQUEST_LEDGER.is_file() else None},
            "responses_stream": {"path": str(cfg.RESPONSES_STREAM), "sha256": sha256(cfg.RESPONSES_STREAM) if cfg.RESPONSES_STREAM.is_file() else None},
            "responses_broker_xhigh": {"path": str(cfg.RESPONSES_BROKER_XHIGH), "sha256": sha256(cfg.RESPONSES_BROKER_XHIGH) if cfg.RESPONSES_BROKER_XHIGH.is_file() else None},
            "one_stop_contract": {"path": str(cfg.ONE_STOP_CONTRACT_SHARED), "sha256": sha256(cfg.ONE_STOP_CONTRACT_SHARED) if cfg.ONE_STOP_CONTRACT_SHARED.is_file() else None},
            "dev_lifecycle": {"path": str(cfg.DEV_LIFECYCLE_SHARED), "sha256": sha256(cfg.DEV_LIFECYCLE_SHARED) if cfg.DEV_LIFECYCLE_SHARED.is_file() else None},
        },
        "code_judge": {"model": "deepseek-flash", "reasoning_effort": "max", "candidate_level_evaluations": 1, "timeout_seconds": 900, "max_output_tokens": 64000, "runner": str(cfg.CREATE_CODE_JUDGE), "authoritative_implementation": str(cfg.AUTHORITATIVE_CREATE_CODE_JUDGE)},
        "axes": {"result": "six-hidden arithmetic mean", "code": "independent eight-dimension score", "combined_score": None},
        "create_alignment_snapshot": str(cfg.ALIGNMENT_SNAPSHOT),
        "create_alignment_snapshot_sha256": sha256(cfg.ALIGNMENT_SNAPSHOT) if cfg.ALIGNMENT_SNAPSHOT.is_file() else None,
        "create_alignment_verification": alignment_evidence,
        "post_repair_tree_snapshot": str(snapshot_path),
        "post_repair_tree_snapshot_sha256": sha256(snapshot_path) if snapshot_path.is_file() else None,
        "formal_readiness_gate": str(ROOT / "formal_readiness_gate.json"),
        "formal_readiness_gate_sha256": sha256(ROOT / "formal_readiness_gate.json") if (ROOT / "formal_readiness_gate.json").is_file() else None,
        "case_coverage_matrix": str(ROOT / 'case_coverage_matrix.json'),
        "case_coverage_matrix_sha256": sha256(ROOT / 'case_coverage_matrix.json') if (ROOT / 'case_coverage_matrix.json').is_file() else None,
        "configuration_deltas": [dict(task=task, **entry) for task, entry in
                                 configuration_evidence.get("tasks", {}).items()],
        "configuration_delta_registry": {key: value for key, value in
                                          configuration_evidence.items() if key != "tasks"},
        "control_runtime": control_evidence,
        "credential_file": str(cfg.CREDENTIAL_FILE),
        "credential_values_recorded": False,
        "formal_root": str(cfg.FORMAL_ROOT / "codex_xhigh"),
        "validation": {
            "valid": not errors,
            "ready_count": audited_ready_count,
            "declared_ready_count": declared_ready_count,
            "formal_launch_authorized": formal_launch_authorized,
            "errors": errors,
        },
    }
    args.output.parent.mkdir(parents=True, exist_ok=True)
    temporary = args.output.with_suffix(args.output.suffix + ".tmp")
    temporary.write_text(json.dumps(manifest, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    os.replace(temporary, args.output)
    print(json.dumps(manifest["validation"], indent=2, ensure_ascii=False))
    return 0 if not errors else 2


if __name__ == "__main__":
    raise SystemExit(main())
