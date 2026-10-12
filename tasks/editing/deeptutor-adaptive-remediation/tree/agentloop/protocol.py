#!/usr/bin/env python3
"""Shared locked protocol helpers for the DeepTutor Agent-loop sibling."""

from __future__ import annotations

from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
from typing import Any


BENCHMARK_ROOT = Path(__file__).resolve().parents[1]
AUTHORITATIVE_BENCHMARK_ROOT = Path(
    "@@AGENTSWE_EDITING_TASKS@@/deeptutor-adaptive-remediation/a0src"
)
# The benchmark root contains evaluator material and is not the repository
# that receives a Candidate patch.  The product snapshot is the staged
# input/repository tree.
AUTHORITATIVE_SOURCE = AUTHORITATIVE_BENCHMARK_ROOT / "input" / "repository"
AUTHORITATIVE_SOURCE_DIGEST = (
    "1dda8c9737d0997b5efc9fa7a85348f96b05b73e67d5f5b8ac7625cd4eae57bc"
    )
AUTHORITATIVE_BENCHMARK_DIGEST = (
    "b3b510684520e23a237035a60990c46843157e564edd360325009d54791ad078"
)
LOWER_MODEL = "deepseek-flash"
LOWER_EFFORT = "high"
BUILDER_MODEL = "deepseek-flash"
BUILDER_EFFORT = "max"
PLACEHOLDER_TOKEN = "broker-only-placeholder"
STATS_TOKEN = "stats-only-placeholder"
DEV_CASES = ("dev_001", "dev_002")
HIDDEN_CASES = tuple(f"test_{index:03d}" for index in range(1, 7))
DELIVERY_FILES = ("solution.patch", "edit_report.json", "run_report.json")

CANDIDATE_CLASSIFICATIONS = {
    "candidate_delivery_failure",
    "candidate_patch_failure",
    "candidate_build_failure",
    "candidate_contract_failure",
    "candidate_agent_failure",
    "candidate_policy_failure",
    "candidate_capability_gap",
    "candidate_valid",
}
INFRA_CLASSIFICATIONS = {
    "broker_infrastructure_error",
    "provider_infrastructure_error",
    "credential_infrastructure_error",
    "mount_infrastructure_error",
    "docker_infrastructure_error",
    "evaluator_infrastructure_error",
    "launcher_infrastructure_error",
    "runtime_dependency_infrastructure_error",
}


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def read_json(path: Path) -> dict[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError(f"expected JSON object: {path}")
    return value


def write_json(path: Path, value: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(
        json.dumps(value, indent=2, ensure_ascii=False, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    os.replace(temporary, path)


def file_digest(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        while chunk := handle.read(1024 * 1024):
            digest.update(chunk)
    return digest.hexdigest()


def object_digest(value: object) -> str:
    """Digest one evaluator-owned JSON value using a canonical encoding."""
    payload = json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def tree_digest(root: Path) -> str:
    """Match the authoritative recursive digest, excluding interpreter caches."""
    digest = hashlib.sha256()
    for item in sorted(root.rglob("*"), key=lambda path: path.relative_to(root).as_posix()):
        if "__pycache__" in item.relative_to(root).parts or ".pytest_cache" in item.relative_to(root).parts:
            continue
        relative = item.relative_to(root).as_posix().encode("utf-8")
        if item.is_symlink():
            payload = os.readlink(item).encode("utf-8")
            digest.update(
                b"L"
                + len(relative).to_bytes(8, "big")
                + relative
                + len(payload).to_bytes(8, "big")
                + payload
            )
        elif item.is_file():
            digest.update(b"F" + len(relative).to_bytes(8, "big") + relative)
            digest.update(item.stat().st_size.to_bytes(8, "big"))
            with item.open("rb") as handle:
                while chunk := handle.read(1024 * 1024):
                    digest.update(chunk)
    return digest.hexdigest()


def changed_paths(patch: Path) -> list[str]:
    paths: set[str] = set()
    for line in patch.read_text(encoding="utf-8", errors="strict").splitlines():
        if line.startswith("diff --git a/") and " b/" in line:
            paths.add(line.split(" b/", 1)[1])
        elif line.startswith("+++ b/"):
            paths.add(line[6:].split("\t", 1)[0])
    return sorted(paths)


def safe_patch_paths(paths: list[str]) -> bool:
    return bool(paths) and all(
        not Path(path).is_absolute()
        and ".." not in Path(path).parts
        and path not in {"/dev/null", "dev/null"}
        for path in paths
    )


def _nonnegative_number(value: object) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool) and value >= 0


def _nonnegative_integer(value: object) -> bool:
    return isinstance(value, int) and not isinstance(value, bool) and value >= 0


def validate_delivery(root: Path, *, readiness: bool = False) -> list[str]:
    errors: list[str] = []
    if not root.is_dir():
        return ["delivery directory missing"]
    entries = {path.name for path in root.iterdir()}
    expected = set(DELIVERY_FILES)
    for name in sorted(expected - entries):
        errors.append(f"missing {name}")
    for name in sorted(entries - expected):
        errors.append(f"unexpected top-level artifact {name}")
    for path in root.rglob("*"):
        if path.is_symlink():
            errors.append(f"symlink forbidden: {path.relative_to(root)}")
        elif not path.is_file() and not path.is_dir():
            errors.append(f"special filesystem object forbidden: {path.relative_to(root)}")

    patch = root / "solution.patch"
    if patch.is_file():
        if patch.stat().st_size == 0:
            errors.append("solution.patch is empty")
        else:
            try:
                paths = changed_paths(patch)
            except UnicodeDecodeError:
                errors.append("solution.patch is not UTF-8")
            else:
                if not safe_patch_paths(paths):
                    errors.append("solution.patch has unsafe or unrecognized paths")

    edit_report_path = root / "edit_report.json"
    if edit_report_path.is_file():
        try:
            report = read_json(edit_report_path)
        except (ValueError, json.JSONDecodeError) as exc:
            errors.append(f"edit_report.json invalid: {type(exc).__name__}")
        else:
            for field in ("commands", "compatibility_notes", "limitations"):
                if not isinstance(report.get(field), list):
                    errors.append(f"edit_report.json {field} must be an array")
            changed = report.get("changed_paths")
            if changed is not None and not isinstance(changed, list):
                errors.append("edit_report.json changed_paths must be an array")

    run_report_path = root / "run_report.json"
    if run_report_path.is_file():
        try:
            report = read_json(run_report_path)
        except (ValueError, json.JSONDecodeError) as exc:
            errors.append(f"run_report.json invalid: {type(exc).__name__}")
        else:
            fields = {
                "schema_version",
                "status",
                "artifact_paths",
                "errors",
                "runtime_seconds",
                "peak_memory_bytes",
                "api_calls",
            }
            if readiness:
                fields |= {'builder_session_id','submission_number','revision_of_candidate_digest','feedback_digest'}
            if set(report) != fields:
                errors.append("run_report.json has incorrect top-level fields")
            if report.get("schema_version") != "1.0":
                errors.append("run_report.json schema_version must be 1.0")
            if not isinstance(report.get("status"), str) or not report.get("status"):
                errors.append("run_report.json status must be non-empty")
            if report.get("artifact_paths") != list(DELIVERY_FILES):
                errors.append("run_report.json artifact_paths must list the exact delivery")
            if not isinstance(report.get("errors"), list) or not all(
                isinstance(item, str) for item in report.get("errors", [])
            ):
                errors.append("run_report.json errors must be a string array")
            if not _nonnegative_number(report.get("runtime_seconds")):
                errors.append("run_report.json runtime_seconds must be non-negative")
            if not _nonnegative_integer(report.get("peak_memory_bytes")):
                errors.append("run_report.json peak_memory_bytes must be a non-negative integer")
            calls = report.get("api_calls")
            if not isinstance(calls, dict) or set(calls) != {"gateway", "serper", "web_retrieval"}:
                errors.append("run_report.json api_calls keys are incorrect")
            elif not all(_nonnegative_integer(value) for value in calls.values()):
                errors.append("run_report.json api_calls values must be non-negative integers")
    return errors


def load_case(case_id: str) -> dict[str, Any]:
    if case_id not in DEV_CASES + HIDDEN_CASES:
        raise ValueError(f"unknown case: {case_id}")
    return read_json(BENCHMARK_ROOT / "evaluator" / "cases" / f"{case_id}.json")


def is_infrastructure(value: dict[str, Any]) -> bool:
    return value.get("classification") in INFRA_CLASSIFICATIONS or value.get("infra_valid") is False


def candidate_zero(classification: str, reason: str, **extra: Any) -> dict[str, Any]:
    if classification not in CANDIDATE_CLASSIFICATIONS:
        raise ValueError(classification)
    return {
        "valid": True,
        "infra_valid": True,
        "classification": classification,
        "score": 0,
        "reason": reason,
        **extra,
    }


def infrastructure_na(classification: str, reason: str, **extra: Any) -> dict[str, Any]:
    if classification not in INFRA_CLASSIFICATIONS:
        raise ValueError(classification)
    return {
        "valid": False,
        "infra_valid": False,
        "classification": classification,
        "score": None,
        "reason": reason,
        **extra,
    }
