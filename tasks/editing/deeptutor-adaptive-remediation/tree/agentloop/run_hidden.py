#!/usr/bin/env python3
"""Execute evaluator-owned hidden cases after an immutable accepted freeze.

This runner deliberately does not manufacture ``not_run`` records.  Each case
gets a fresh writable copy of the frozen Candidate tree, a fresh DeepTutor
runtime/output directory, and an evaluator-owned broker-stats delta.  A
candidate capability failure is retained as a candidate outcome; launcher,
dependency, broker, and evaluator failures remain infrastructure failures.
"""

from __future__ import annotations

import argparse
from datetime import datetime, timezone
import hashlib
import json
import os
import secrets
from pathlib import Path
import shutil
import subprocess
import sys
import time
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit, urlunsplit
from urllib.request import Request, urlopen

try:
    from .execution_evidence import attest_execution
    from .protocol import (
        HIDDEN_CASES,
        LOWER_EFFORT,
        LOWER_MODEL,
        STATS_TOKEN,
        infrastructure_na,
        load_case,
        read_json,
        tree_digest,
        utc_now,
        write_json,
    )
except ImportError:  # pragma: no cover
    from execution_evidence import attest_execution
    from protocol import (  # type: ignore
        HIDDEN_CASES,
        LOWER_EFFORT,
        LOWER_MODEL,
        STATS_TOKEN,
        infrastructure_na,
        load_case,
        read_json,
        tree_digest,
        utc_now,
        write_json,
    )


ROOT = Path(__file__).resolve().parents[1]
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


def _broker_failure(delta: dict[str, int] | None) -> tuple[str, str] | None:
    if not isinstance(delta, dict):
        return None
    if int(delta.get("provider_failures", 0) or 0) > 0:
        return "provider_infrastructure_error", "evaluator broker recorded provider failure"
    # delivery failures are not this case's calls: broker.delivery_failure() fires when the
    # upstream request already succeeded and only the write back to the client socket
    # failed, so it increments neither `calls` nor `failures`.  The broker is a run-level
    # singleton sliced per case by stats_delta(), and the dominant producer of a delivery
    # failure is the PREVIOUS case being killed at its timeout with one request in flight:
    # that orphan resolves after that case's broker_after snapshot and so lands in this
    # case's window.  The raw count stays in broker_delta.json as evidence.
    for counter in ("failures", "protocol_failures", "upstream_failures"):
        value = int(delta.get(counter, 0) or 0)
        if value > 0:
            return "broker_infrastructure_error", f"evaluator broker recorded {counter}={value}"
    return None


def _stats_url(endpoint: str) -> str:
    parsed = urlsplit(endpoint)
    path = parsed.path.rstrip("/")
    if path.endswith("/v1/responses"):
        path = path[: -len("/v1/responses")]
    return urlunsplit((parsed.scheme, parsed.netloc, f"{path}/stats", "", ""))


def read_broker_stats(endpoint: str, *, timeout: float = 5.0) -> dict[str, Any]:
    request = Request(
        _stats_url(endpoint),
        headers={"Authorization": f"Bearer {STATS_TOKEN}", "Accept": "application/json"},
    )
    with urlopen(request, timeout=timeout) as response:
        value = json.loads(response.read())
    if not isinstance(value, dict):
        raise ValueError("broker stats response must be an object")
    return value


def _number(value: Any) -> int:
    return int(value) if isinstance(value, (int, float)) and not isinstance(value, bool) else 0



def _candidate_fatal_gate(item: dict) -> bool:
    attribution = item.get("failure_attribution") or {}
    return (item.get("classification") == "candidate_product_failure"
            and attribution.get("party") == "candidate" and attribution.get("observed_by") == "evaluator"
            and attribution.get("fatal") is True and bool(attribution.get("evidence_paths"))
            and item.get("execution_attempted") is True
            and (item.get("environment_preflight") or {}).get("valid") is True)

def broker_stats_view(stats: dict[str, Any] | None) -> dict[str, Any]:
    """Project either supported broker stats schema onto the lower-run view.

    Product lower brokers expose the flat ``agentswe-broker-stats/v1`` shape;
    the validated shared Builder broker exposes nested ``runtime``/``judge``
    counters.  Keep raw responses in evidence and normalize only for checks
    and deltas.
    """
    if not isinstance(stats, dict):
        return {}
    runtime = stats.get("runtime")
    if not isinstance(runtime, dict):
        return dict(stats)
    failures = _number(runtime.get("failures"))
    calls = _number(runtime.get("calls"))
    return {
        "model": stats.get("model"),
        "reasoning_effort": stats.get("reasoning_effort"),
        "calls": calls,
        "successful_calls": max(0, calls - failures),
        "failures": failures,
        "provider_failures": _number(runtime.get("upstream_failures")),
        "delivery_failures": _number(runtime.get("delivery_failures")),
        "total_tokens": _number(runtime.get("tokens")),
        "budget_exceeded": runtime.get("budget_exceeded") is True,
    }


def stats_delta(before: dict[str, Any], after: dict[str, Any]) -> dict[str, Any]:
    """Keep unknown usage unknown; measure known usage as a separate subtotal."""
    counters=("calls","successful_calls","failures","provider_failures","delivery_failures")
    tokens=("input_tokens","output_tokens","total_tokens")
    before_view=broker_stats_view(before);after_view=broker_stats_view(after)
    result={key:_number(after_view.get(key))-_number(before_view.get(key)) for key in counters}
    for key in tokens:
        previous,current=before_view.get(key),after_view.get(key)
        result[key]=current-previous if type(previous) is int and type(current) is int else None
    if isinstance(before_view.get('known_usage_subtotal'),dict) and isinstance(after_view.get('known_usage_subtotal'),dict):
        previous=before_view['known_usage_subtotal'];current=after_view['known_usage_subtotal']
        result['known_usage_subtotal']={key:current[key]-previous[key] if type(previous.get(key)) is int and type(current.get(key)) is int and current[key]>=previous[key] else None for key in tokens}
    return result


def _budget_exceeded(stats: dict[str, Any] | None, context_id: str | None = None) -> bool:
    view = broker_stats_view(stats)
    if view.get("budget_scope") == "case-context/v1" and context_id:
        return view.get("context_budgets", {}).get(context_id, {}).get("budget_exceeded") is True
    return view.get("budget_exceeded") is True


def _parse_timestamp(value: Any, field: str) -> datetime:
    if not isinstance(value, str) or not value.strip():
        raise RuntimeError(f"freeze manifest is missing {field}")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise RuntimeError(f"freeze manifest has invalid {field}") from exc
    if parsed.tzinfo is None:
        raise RuntimeError(f"freeze manifest {field} must include a timezone")
    return parsed.astimezone(timezone.utc)


def _make_tree_writable(root: Path) -> None:
    for item in sorted(root.rglob("*"), key=lambda path: len(path.parts)):
        mode = item.stat().st_mode
        if item.is_dir():
            os.chmod(item, mode | 0o700)
        else:
            os.chmod(item, mode | 0o600)
    os.chmod(root, root.stat().st_mode | 0o700)


def _copy_fresh_runtime(frozen: Path, destination: Path) -> None:
    if destination.exists():
        raise RuntimeError(f"fresh runtime destination already exists: {destination}")
    shutil.copytree(
        frozen,
        destination,
        symlinks=False,
        ignore=shutil.ignore_patterns("__pycache__", ".pytest_cache"),
    )
    _make_tree_writable(destination)


def _parse_launcher_result(output: Path) -> dict[str, Any] | None:
    path = output / "launcher_result.json"
    if not path.is_file():
        return None
    try:
        value = read_json(path)
    except (OSError, ValueError, json.JSONDecodeError):
        return None
    return value


def _artifact_valid(output: Path, expected_case_id: str | None = None) -> bool:
    artifact = output / "agent_result.json"
    if not artifact.is_file():
        return False
    try:
        value = json.loads(artifact.read_text(encoding="utf-8"))
    except (OSError, ValueError, json.JSONDecodeError):
        return False
    if not isinstance(value, dict):
        return False
    required = ("schema_version", "case_id", "status", "summary", "artifacts")
    if any(key not in value for key in required):
        return False
    if not all(isinstance(value.get(key), str) and str(value.get(key)).strip() for key in ("schema_version", "case_id", "status", "summary")):
        return False
    if not isinstance(value.get("artifacts"), dict):
        return False
    return expected_case_id is None or value.get("case_id") == expected_case_id


def _file_sha256(path: Path) -> str | None:
    if not path.is_file():
        return None
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _write_evidence_manifest(case_dir: Path, paths: dict[str, str | None]) -> Path:
    """Record existence, size, and digest for the complete case evidence set."""
    files: dict[str, dict[str, Any]] = {}
    for name, raw in paths.items():
        path = Path(raw) if raw else None
        exists = bool(path and path.is_file())
        files[name] = {
            "path": str(path) if path else None,
            "exists": exists,
            "size_bytes": path.stat().st_size if exists and path else None,
            "sha256": _file_sha256(path) if exists and path else None,
        }
    destination = case_dir / "evidence-manifest.json"
    write_json(destination, {
        "schema_version": "deeptutor-hidden-case-evidence/v1",
        "case_id": case_dir.name,
        "files": files,
    })
    return destination


def _classify_case(
    launcher: dict[str, Any] | None,
    *,
    case_id: str,
    process_exit: int,
    output: Path,
    broker_delta: dict[str, int] | None,
    stats_error: str | None,
) -> tuple[str, str]:
    if stats_error:
        return "evaluator_infrastructure_error", f"broker stats unavailable: {stats_error}"
    if launcher is None:
        return "launcher_infrastructure_error", "launcher_result.json was not produced"
    classification = str(launcher.get("classification") or "")
    if classification in INFRA_CLASSIFICATIONS:
        return classification, str(launcher.get("error") or launcher.get("stderr_tail") or classification)
    broker_failure = _broker_failure(broker_delta)
    if broker_failure is not None:
        return broker_failure
    if classification == "candidate_fixture_precondition_failure" and launcher.get("runtime_probe", {}).get("infra_valid") is True:
        return "candidate_capability_gap", "public product interface could not establish case preconditions: " + str(launcher.get("error", ""))
    if process_exit != 0 and not launcher.get("executed"):
        return "launcher_infrastructure_error", "launcher did not execute the lower agent"
    if not launcher.get("executed"):
        return "launcher_infrastructure_error", "lower agent execution was not requested"
    if _budget_exceeded(launcher.get("broker_after"), launcher.get("logical_context_id")):
        if (launcher.get("broker_after") or {}).get("budget_scope") == "case-context/v1":
            return "candidate_policy_violation", "actual case exhausted its disclosed model-call/token budget"
        return "broker_infrastructure_error", "legacy evaluator broker budget was exceeded during the case"
    if broker_delta is not None and broker_delta.get("successful_calls", 0) == 0:
        return "candidate_agent_failure", "executed lower agent made no successful call while broker statistics show no infrastructure failure"
    if classification.startswith("candidate_"):
        return classification, str(launcher.get("classification_reason") or classification)
    if not _artifact_valid(output, case_id):
        return "candidate_contract_failure", "lower agent did not produce a parseable terminal artifact"
    return "candidate_valid", "real lower-agent execution produced a parseable terminal artifact"


def _case(
    *,
    case_id: str,
    frozen: Path,
    endpoint: str,
    output_root: Path,
    python_executable: str,
    launcher_path: Path,
) -> dict[str, Any]:
    started_at = utc_now()
    started_monotonic = time.monotonic()
    frozen_digest_before = tree_digest(frozen)
    case_output = output_root / case_id
    case_output.mkdir(parents=True, exist_ok=False)
    runtime_repository = case_output / "candidate_runtime"
    prompt_source = ROOT / str(load_case(case_id).get("prompt_file", ""))
    if not prompt_source.is_file():
        result = infrastructure_na("evaluator_infrastructure_error", "hidden prompt is missing")
        result.update({"case_id": case_id, "started_at": started_at, "duration_seconds": 0})
        write_json(case_output / "case_result.json", result)
        return result
    runtime_nonce = secrets.token_hex(16)
    runtime_projection = {"case_id": case_id}
    prompt_file = case_output / "executed_task.md"
    prompt_file.write_text(
        prompt_source.read_text(encoding="utf-8"),
        encoding="utf-8",
    )

    # The owned case driver copies frozen source inside the same 4GiB/600s scope.

    before: dict[str, Any] | None = None
    after: dict[str, Any] | None = None
    stats_error: str | None = None
    try:
        before = read_broker_stats(endpoint)
        write_json(case_output / "broker_before.json", before)
    except (OSError, HTTPError, URLError, TimeoutError, ValueError, json.JSONDecodeError) as exc:
        stats_error = f"{type(exc).__name__}: {exc}"

    command = [
        python_executable,
        str(launcher_path),
        "--repository",
        str(runtime_repository),
        "--source-repository", str(frozen),
        "--prompt",
        str(prompt_file),
        "--output",
        str(case_output),
        "--broker-endpoint",
        endpoint,
        "--execute",
        "--python",
        python_executable,
    ]
    env = dict(os.environ)
    env["PYTHONDONTWRITEBYTECODE"] = "1"
    env["DEEPTUTOR_CASE_ID"] = case_id
    env["DEEPTUTOR_RUNTIME_NONCE"] = runtime_nonce
    process_exit = -1
    stdout = ""
    stderr = ""
    try:
        completed = subprocess.run(
            command,
            cwd=ROOT,
            env=env,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=900,
            check=False,
        )
        process_exit = completed.returncode
        stdout = completed.stdout[-8000:]
        stderr = completed.stderr[-8000:]
    except subprocess.TimeoutExpired as exc:
        stderr = f"hidden lower-agent timeout: {exc}"
    except OSError as exc:
        stderr = f"hidden launcher OS error: {exc}"

    try:
        after = read_broker_stats(endpoint)
        write_json(case_output / "broker_after.json", after)
    except (OSError, HTTPError, URLError, TimeoutError, ValueError, json.JSONDecodeError) as exc:
        if stats_error is None:
            stats_error = f"{type(exc).__name__}: {exc}"

    launcher = _parse_launcher_result(case_output)
    context_path = case_output / "fixture" / "fixture-context.json"
    if context_path.is_file():
        context = read_json(context_path)
        runtime_projection = {key: context[key] for key in ("case_id", "runtime_nonce", "path_id", "session_id") if key in context}
    delta = stats_delta(before, after) if before is not None and after is not None else None
    if delta is not None:
        write_json(case_output / "broker_delta.json", delta)
    classification, reason = _classify_case(
        {**(launcher or {}), "broker_after": after},
        case_id=case_id,
        process_exit=process_exit,
        output=case_output,
        broker_delta=delta,
        stats_error=stats_error,
    )
    frozen_digest_after = tree_digest(frozen)
    result: dict[str, Any] = {
        "schema_version": "agentswe-deeptutor-hidden-case/v1",
        "case_id": case_id,
        "started_at": started_at,
        "finished_at": utc_now(),
        "duration_seconds": time.monotonic() - started_monotonic,
        "command": command,
        "process_exit_code": process_exit,
        "launcher_executed": bool(launcher and launcher.get("executed")),
        "terminal_product_artifact": _artifact_valid(case_output, case_id),
        "classification": classification,
        "infra_valid": classification not in INFRA_CLASSIFICATIONS,
        "reason": reason,
        "broker": {
            "before": before,
            "after": after,
            "delta": delta,
            "model": broker_stats_view(after or before).get("model"),
            "reasoning_effort": broker_stats_view(after or before).get("reasoning_effort"),
        },
        "frozen_candidate_digest_before": frozen_digest_before,
        "frozen_candidate_digest_after": frozen_digest_after,
        "frozen_candidate_stable": frozen_digest_after == frozen_digest_before,
        "stdout_tail": stdout,
        "stderr_tail": stderr,
        "executed_task_path": str(prompt_file),
        "executed_task_sha256": _file_sha256(prompt_file),
        "runtime_projection": runtime_projection,
    }
    # The digest is sampled again after serialising evidence.  The frozen tree
    # itself must not be touched by the lower runtime.
    result["frozen_candidate_digest_after"] = tree_digest(frozen)
    result["frozen_candidate_stable"] = result["frozen_candidate_stable"] and (
        result["frozen_candidate_digest_before"] == result["frozen_candidate_digest_after"]
    )
    artifact_path = case_output / "agent_result.json"
    trajectory_path = case_output / "trajectory.jsonl"
    artifact_provenance = {
        "artifact_path": str(artifact_path),
        "artifact_owner": "lower_agent_product",
        "producer_entry": "DeepTutor mastery_path product process",
        "evaluator_capture": "agentloop/lower_agent_entry.py",
        "evaluator_synthesized": False,
        "exists": artifact_path.is_file(),
        "size_bytes": artifact_path.stat().st_size if artifact_path.is_file() else None,
        "sha256": _file_sha256(artifact_path),
    }
    comparison_path = case_output / "private-oracle-comparison.json"
    native_oracle_path = case_output / "fixture" / "fixture-observation.json"
    native_oracle = read_json(native_oracle_path) if native_oracle_path.is_file() else {}
    write_json(comparison_path, {
        **native_oracle,
        "schema_version": "deeptutor-run-local-oracle-comparison-v2",
        "case_id": case_id,
        "executed_task_path": str(prompt_file),
        "executed_task_sha256": _file_sha256(prompt_file),
        "runtime_nonce_digest": hashlib.sha256(str(runtime_projection.get("runtime_nonce", runtime_nonce)).encode()).hexdigest(),
        "candidate_visible": False,
        "expected_values_disclosed": False,
        "private_oracle_not_candidate_visible": True,
        "classification": result["classification"],
        "semantic_oracle_complete": bool(native_oracle.get("expected_invariants") and native_oracle.get("post_agent_product_observations")),
        "native_oracle_path": str(native_oracle_path),
        "native_oracle_sha256": _file_sha256(native_oracle_path),
    })
    result.update({
        "private_oracle_comparison_path": str(comparison_path),
        "private_oracle_comparison_sha256": _file_sha256(comparison_path),
    })
    evidence_paths = {
        "runtime_probe": str(case_output / "runtime_probe.json"),
        "public_tool_surface": str(case_output / "public_tool_surface.json"),
        "responses_adapter": str(case_output / "responses_adapter_installed.json"),
        "launcher_result": str(case_output / "launcher_result.json"),
        "trajectory": str(trajectory_path),
        "agent_result": str(artifact_path),
        "broker_before": str(case_output / "broker_before.json"),
        "broker_after": str(case_output / "broker_after.json"),
        "broker_delta": str(case_output / "broker_delta.json"),
        "case_result": str(case_output / "case_result.json"),
        "executed_task": str(prompt_file),
        "private_oracle_comparison": str(comparison_path),
    }
    result["artifact_provenance"] = artifact_provenance
    result["evidence_manifest"] = str(case_output / "evidence-manifest.json")
    result = attest_execution(result, launcher or {}, output=case_output, case_id=case_id,
        candidate_digest=frozen_digest_before)
    result['execution_record_path'] = str(case_output / 'case_result.json')
    result['observed_trajectory_path'] = str(trajectory_path)
    write_json(case_output / "case_result.json", result)
    _write_evidence_manifest(case_output, evidence_paths)
    return result


def run(
    freeze_manifest: Path,
    output: Path,
    *,
    broker_endpoint: str,
    python_executable: str | None = None,
    launcher: Path | None = None,
    case_ids: tuple[str, ...] = HIDDEN_CASES,
    pilot_not_formal: bool = False,
) -> dict[str, Any]:
    freeze = read_json(freeze_manifest.resolve())
    source_submission = freeze.get("source_submission")
    accepted_count = freeze.get("accepted_submission_count", source_submission)
    if freeze.get("hidden_allowed") is not True:
        raise RuntimeError("a verified latest-accepted Candidate freeze (formerly Candidate 2 freeze) is required before hidden execution")
    if not isinstance(source_submission, int) or not 1 <= source_submission <= 10:
        raise RuntimeError("freeze manifest has an invalid latest accepted submission")
    if not isinstance(accepted_count, int) or accepted_count != source_submission:
        raise RuntimeError("freeze manifest accepted-submission ledger is inconsistent")
    if freeze.get("feedback_received") is not True:
        raise RuntimeError("hidden execution requires an evaluator feedback record")
    if freeze.get("same_session_verified") is not True:
        raise RuntimeError("hidden execution requires a verified same-session Builder lifecycle")
    if freeze.get("frozen_tree_read_only") is not True or freeze.get("frozen_tree_regular") is not True:
        raise RuntimeError("hidden execution requires an immutable regular-file freeze")
    frozen_at = _parse_timestamp(freeze.get("frozen_at"), "frozen_at")
    frozen = Path(str(freeze.get("candidate_path", ""))).resolve()
    if not frozen.is_dir():
        raise RuntimeError("frozen Candidate snapshot is missing")
    expected_digest = str(freeze.get("candidate_digest") or "")
    if not expected_digest or tree_digest(frozen) != expected_digest:
        raise RuntimeError("frozen Candidate digest changed before hidden execution")
    if any(item.is_symlink() for item in frozen.rglob("*")):
        raise RuntimeError("frozen Candidate contains a symlink")
    if any((item.stat().st_mode & 0o222) != 0 for item in (frozen, *frozen.rglob("*"))):
        raise RuntimeError("frozen Candidate is not read-only")
    if tuple(case_ids) != HIDDEN_CASES and not (
        pilot_not_formal and tuple(case_ids) == ("test_001",)
    ):
        raise RuntimeError("formal hidden execution must contain exactly test_001 through test_006")

    output = output.resolve()
    if output.exists() and any(output.iterdir()):
        raise RuntimeError("hidden output directory must be new and empty")
    output.mkdir(parents=True, exist_ok=True)
    python_value = python_executable or os.environ.get("DEEPTUTOR_PYTHON") or sys.executable
    launcher_path = (launcher or Path(__file__).with_name("lower_agent_launcher.py")).resolve()
    try:
        suite_before = read_broker_stats(broker_endpoint)
    except (OSError, HTTPError, URLError, TimeoutError, ValueError, json.JSONDecodeError) as exc:
        raise RuntimeError(f"clean hidden broker stats are unavailable: {type(exc).__name__}: {exc}") from exc
    suite_before_view = broker_stats_view(suite_before)
    if suite_before_view.get("model") != LOWER_MODEL or suite_before_view.get("reasoning_effort") != LOWER_EFFORT:
        raise RuntimeError("hidden broker model/reasoning lock mismatch")
    if any(_number(suite_before_view.get(key)) != 0 for key in ("calls", "successful_calls", "failures", "provider_failures")):
        raise RuntimeError("hidden execution requires a fresh zero-call evaluator broker")
    if _budget_exceeded(suite_before):
        raise RuntimeError("hidden execution broker budget is already exhausted")
    write_json(output / "broker_suite_before.json", suite_before)
    hidden_started_at = utc_now()
    hidden_started = _parse_timestamp(hidden_started_at, "hidden_started_at")
    if hidden_started <= frozen_at:
        raise RuntimeError("hidden execution must start strictly after accepted Candidate freeze")
    inventory = {
        "schema_version": "agentswe-deeptutor-hidden-inventory/v1",
        "cases": list(case_ids),
        "pilot_not_formal": pilot_not_formal,
        "freeze_manifest": str(freeze_manifest.resolve()),
        "freeze_digest": expected_digest,
        "frozen_at": freeze.get("frozen_at"),
        "hidden_started_at": hidden_started_at,
        "hidden_strictly_after_freeze": True,
    }
    write_json(output / "hidden_inventory.json", inventory)
    records: list[dict[str, Any]] = []
    for case_id in case_ids:
        # Re-check before each case, not only once at suite start.
        if tree_digest(frozen) != expected_digest:
            raise RuntimeError(f"frozen Candidate digest changed before {case_id}")
        records.append(
            _case(
                case_id=case_id,
                frozen=frozen,
                endpoint=broker_endpoint,
                output_root=output / "cases",
                python_executable=python_value,
                launcher_path=launcher_path,
            )
        )
    suite_after: dict[str, Any] | None = None
    suite_stats_error: str | None = None
    try:
        suite_after = read_broker_stats(broker_endpoint)
        write_json(output / "broker_suite_after.json", suite_after)
        write_json(output / "broker_suite_delta.json", stats_delta(suite_before, suite_after))
    except (OSError, HTTPError, URLError, TimeoutError, ValueError, json.JSONDecodeError) as exc:
        suite_stats_error = f"{type(exc).__name__}: {exc}"
    final_digest = tree_digest(frozen)
    infrastructure_cases = [
        item for item in records if item.get("classification") in INFRA_CLASSIFICATIONS
    ]
    attestation: dict[str, Any] = {
        "schema_version": "agentswe-deeptutor-hidden-attestation/v1",
        "pilot_not_formal": pilot_not_formal,
        "candidate_digest": expected_digest,
        "source_submission": source_submission,
        "frozen_at": freeze.get("frozen_at"),
        "hidden_started_at": hidden_started_at,
        "hidden_after_freeze": hidden_started > frozen_at,
        "expected_cases": list(case_ids),
        "executed_cases": [item.get("case_id") for item in records],
        "all_cases_invoked": len(records) == len(case_ids)
        and all(item.get("launcher_executed") is True or _candidate_fatal_gate(item) for item in records),
        "all_cases_real": len(records) == len(case_ids)
        and all(
            (
                item.get("launcher_executed") is True
                and item.get("environment_preflight", {}).get("valid") is True
                and _number(item.get("broker", {}).get("delta", {}).get("successful_calls")) > 0
            )
            # A Candidate-attributed fatal case (evaluator fatal gate, healthy preflight) is
            # a real case consumed as candidate_zero by the shared contract, not missing evidence.
            or _candidate_fatal_gate(item)
            for item in records
        ),
        "candidate_fatal_cases": [item.get("case_id") for item in records if _candidate_fatal_gate(item)],
        "frozen_candidate_stable": final_digest == expected_digest
        and all(item.get("frozen_candidate_stable") is True for item in records),
        "all_cases_materialized": len(records) == len(case_ids) and all(item.get("executed_task_sha256") for item in records),
        "all_cases_started_after_freeze": all(_parse_timestamp(item["started_at"], "started_at") > frozen_at for item in records),
        "complete_inventory": tuple(case_ids) == HIDDEN_CASES,
        "frozen_digest_stable": final_digest == expected_digest and all(item.get("frozen_candidate_stable") is True for item in records),
        "broker_stats_present": all(
            isinstance(item.get("broker", {}).get("before"), dict)
            and isinstance(item.get("broker", {}).get("after"), dict)
            for item in records
        ),
        "clean_broker_at_start": True,
        "broker_suite_before": suite_before,
        "broker_suite_after": suite_after,
        "broker_suite_delta": stats_delta(suite_before, suite_after)
        if suite_after is not None
        else None,
        "broker_suite_stats_error": suite_stats_error,
        "infrastructure_case_count": len(infrastructure_cases),
        "formal_result_eligible": not pilot_not_formal
        and len(records) == len(case_ids)
        and all(
            (
                item.get("launcher_executed") is True
                and item.get("environment_preflight", {}).get("valid") is True
                and _number(item.get("broker", {}).get("delta", {}).get("successful_calls")) > 0
            )
            or _candidate_fatal_gate(item)
            for item in records
        )
        and not infrastructure_cases
        and final_digest == expected_digest
        and hidden_started > frozen_at
        and suite_stats_error is None
        and suite_after is not None
        and broker_stats_view(suite_after).get("model") == LOWER_MODEL
        and broker_stats_view(suite_after).get("reasoning_effort") == LOWER_EFFORT
        and _number(broker_stats_view(suite_after).get("provider_failures")) == 0
        and (broker_stats_view(suite_after).get("budget_scope") == "case-context/v1" or not _budget_exceeded(suite_after)),
        "formal_result_published": False,
        "result_axis": "N/A" if pilot_not_formal else None,
        "code_axis": "N/A" if pilot_not_formal else None,
        "results": records,
        "attested_at": utc_now(),
    }
    write_json(output / "hidden-after-freeze-attestation.json", attestation)
    return attestation


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--freeze-manifest", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--broker-endpoint", required=True)
    parser.add_argument("--python", dest="python_executable")
    parser.add_argument("--launcher", type=Path)
    args = parser.parse_args()
    result = run(**vars(args))
    print(json.dumps(result, indent=2, ensure_ascii=False, sort_keys=True))
    return 0 if result.get("all_cases_invoked") and result.get("frozen_candidate_stable") else 1


if __name__ == "__main__":
    raise SystemExit(main())
