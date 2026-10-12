"""Uniform run-local one-stop summary writer for Group C Edit siblings."""
from __future__ import annotations

import atexit
import json
from pathlib import Path
from typing import Any


def _read(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
        return value if isinstance(value, dict) else {}
    except (OSError, ValueError, json.JSONDecodeError):
        return {}


def _first(run_dir: Path, names: tuple[str, ...]) -> Path | None:
    for name in names:
        path = run_dir / name
        if path.is_file():
            return path
    return None


def write_summary(run_dir: Path, *, max_dev_rounds: int, n_concurrent: int, mode: str) -> None:
    if not run_dir.exists():
        return
    source_path = _first(run_dir, ("summary.json", "formal_summary.json", "formal_aggregation.json", "protocol_lock.json"))
    source = _read(source_path) if source_path else {}
    lifecycle_path = _first(run_dir, ("dev_lifecycle.json", "lifecycle/dev_lifecycle.json", "controller/dev_lifecycle.json", "lifecycle/controller_state.json", "controller_state.json"))
    freeze_path = _first(run_dir, ("freeze_manifest.json", "lifecycle/freeze_manifest.json", "controller/freeze_manifest.json"))
    hidden_path = _first(run_dir, ("hidden-after-freeze-attestation.json", "hidden/hidden-after-freeze-attestation.json", "lifecycle/hidden-after-freeze-attestation.json", "hidden_after_freeze_attestation.json", "lifecycle/hidden-result.json"))
    aggregation_path = run_dir / "formal_aggregation.json"
    aggregation = _read(aggregation_path) if aggregation_path.is_file() else {}
    freeze = _read(freeze_path) if freeze_path else {}
    hidden = _read(hidden_path) if hidden_path else {}
    cleanup_path = _first(run_dir, ("cleanup_attestation.json", "cleanup-attestation.json"))
    cleanup = _read(cleanup_path) if cleanup_path else {"status": "not-yet-recorded"}
    inventory = hidden.get("inventory") or hidden.get("expected_cases") or hidden.get("case_inventory") or hidden.get("executed_cases")
    if not isinstance(inventory, list):
        cases = hidden.get("cases")
        inventory = list(cases) if isinstance(cases, dict) else []
    result_contracts = aggregation.get("result_judge_contracts")
    if not isinstance(result_contracts, dict):
        result_contracts = {}
    value = {
        "schema_version": "agentswe-edit-one-stop-summary-v2",
        "status": source.get("status", aggregation.get("status", "completed" if source else "no-run-artifacts")),
        "execution_mode": mode,
        "max_dev_rounds": max_dev_rounds,
        "n_concurrent": n_concurrent,
        "dev_lifecycle": str(lifecycle_path) if lifecycle_path else None,
        "freeze": {
            "path": str(freeze_path) if freeze_path else None,
            "digest": freeze.get("candidate_digest") or freeze.get("repository_digest"),
            "reason": freeze.get("freeze_reason"),
            "source_submission": freeze.get("source_submission"),
        },
        "hidden": {"inventory": inventory, "summary": str(hidden_path) if hidden_path else None},
        "result_judge_contracts": result_contracts,
        "result_axis": aggregation.get("result_axis", source.get("result_axis", "N/A")),
        "code_contract": aggregation.get("code_contract"),
        "code_axis": aggregation.get("code_axis", source.get("code_axis", "N/A")),
        "combined_score": None,
        "cleanup_attestation": {"path": str(cleanup_path) if cleanup_path else None, "record": cleanup},
        "formal_result_claimed": aggregation.get("formal_result_publishable") is True,
        "code_score_claimed": aggregation.get("code_score_publishable") is True,
        "static_provider_calls": 0 if mode == "static" else None,
    }
    (run_dir / "one_stop_summary.json").write_text(json.dumps(value, indent=2, ensure_ascii=False, sort_keys=True) + "\n", encoding="utf-8")


def install_summary_writer(run_dir: Path | None, *, max_dev_rounds: int, n_concurrent: int, mode: str) -> None:
    if run_dir is not None:
        atexit.register(write_summary, run_dir.resolve(), max_dev_rounds=max_dev_rounds, n_concurrent=n_concurrent, mode=mode)
