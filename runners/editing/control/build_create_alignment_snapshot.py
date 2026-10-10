#!/usr/bin/env python3
from __future__ import annotations

import hashlib
import importlib.util
import json
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


CREATE_ROOT = Path("@@AGENTSWE_LEGACY_HARBOR@@/0825-10create-v4")
SOURCES = (
    CREATE_ROOT / "suite_config.py",
    CREATE_ROOT / "launch_two_track.py",
    CREATE_ROOT / "run_branch.py",
    CREATE_ROOT / "code_eval.py",
)
OUTPUT = Path("@@AGENTSWE_EDITING_CONTROL@@/create_alignment_snapshot.json")


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        while chunk := handle.read(1024 * 1024):
            digest.update(chunk)
    return digest.hexdigest()


def load_suite_config() -> Any:
    spec = importlib.util.spec_from_file_location("create_suite_config", CREATE_ROOT / "suite_config.py")
    if spec is None or spec.loader is None:
        raise RuntimeError("cannot load Create suite_config.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def bundle_summary(path: Path) -> dict[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8"))
    lifecycle = value.get("dev_lifecycle", []) if isinstance(value, dict) else []
    freeze = value.get("freeze", {}) if isinstance(value, dict) else {}
    return {
        "path": str(path),
        "sha256": sha256(path),
        "task": value.get("task"),
        "profile": value.get("builder_profile"),
        "accepted_rounds": len(lifecycle) if isinstance(lifecycle, list) else None,
        "dev_passed_rounds": sum(
            1 for item in lifecycle
            if isinstance(item, dict) and item.get("dev_passed") is True
        ) if isinstance(lifecycle, list) else None,
        "freeze_reason": freeze.get("reason") if isinstance(freeze, dict) else None,
        "result_publishable": (value.get("result_axis") or {}).get("score_publishable")
        if isinstance(value.get("result_axis"), dict) else None,
        "code_publishable": (value.get("code_axis") or {}).get("code_score_publishable")
        if isinstance(value.get("code_axis"), dict) else None,
    }


def json_safe(value: Any) -> Any:
    if isinstance(value, Path):
        return str(value)
    if isinstance(value, dict):
        return {str(key): json_safe(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [json_safe(item) for item in value]
    return value


def main() -> int:
    module = load_suite_config()
    files: list[dict[str, Any]] = []
    for path in SOURCES:
        stat = path.stat()
        files.append({
            "path": str(path),
            "sha256": sha256(path),
            "size": stat.st_size,
            "mtime_ns": stat.st_mtime_ns,
        })
    bundles: list[dict[str, Any]] = []
    runs_root = CREATE_ROOT / "runs/two_dev"
    for path in sorted(runs_root.glob("*/*/dual_axis_score_bundle.json")):
        try:
            bundles.append(bundle_summary(path))
        except (OSError, json.JSONDecodeError, TypeError, ValueError):
            continue
    freeze_reasons: dict[str, int] = {}
    accepted_round_histogram: dict[str, int] = {}
    for item in bundles:
        reason = str(item.get("freeze_reason") or "missing")
        freeze_reasons[reason] = freeze_reasons.get(reason, 0) + 1
        rounds = str(item.get("accepted_rounds"))
        accepted_round_histogram[rounds] = accepted_round_histogram.get(rounds, 0) + 1
    snapshot = {
        "schema_version": "agentswe-edit-create-alignment-snapshot-v1",
        "created_at": datetime.now(timezone.utc).isoformat(),
        "source_root": str(CREATE_ROOT),
        "source_files": files,
        "profiles": json_safe(module.PROFILES),
        "task_count": len(module.TASKS),
        "task_names": list(module.TASKS),
        "effective_protocol": {
            "selected_profiles": ["codex_xhigh"],
            "profile_scope": "codex_xhigh_only",
            "builder_agent": "codex",
            "builder_model": "deepseek-flash",
            "builder_reasoning_effort": "max",
            "builder_harness_version": "0.144.1",
            "builder_provider": "gateway-responses",
            "max_dev_rounds": 10,
            "n_concurrent": 1,
            "dev_passed_threshold": "mean > 60",
            "dev_passed_is_automatic_freeze": False,
            "lower_model": "deepseek-flash",
            "lower_reasoning_effort": "high",
            "result_judge": "deepseek-flash/xhigh once per scoreable hidden",
            "code_judge": "deepseek-flash/xhigh once per frozen Candidate",
            "combined_score": None,
        },
        "observed_dual_axis_bundles": {
            "count": len(bundles),
            "freeze_reasons": freeze_reasons,
            "accepted_round_histogram": accepted_round_histogram,
            "bundles": bundles,
            "interpretation": (
                "Observed bundles are historical execution evidence, not an automatic publication selection. "
                "They demonstrate that Create permits varying accepted-round counts and both max_dev_rounds "
                "and Builder-driven exit; dev_passed is recorded per round and is not treated here as a new "
                "automatic Edit freeze condition."
            ),
        },
        "credential_values_recorded": False,
    }
    snapshot_bytes = json.dumps(snapshot, indent=2, ensure_ascii=False).encode("utf-8") + b"\n"
    OUTPUT.parent.mkdir(parents=True, exist_ok=True)
    temporary = OUTPUT.with_suffix(".json.tmp")
    temporary.write_bytes(snapshot_bytes)
    os.replace(temporary, OUTPUT)
    print(json.dumps({"output": str(OUTPUT), "sha256": sha256(OUTPUT), "bundles": len(bundles)}, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
