#!/usr/bin/env python3
"""Evaluator-owned hidden-case executor for the real OpenClaw lower agent.

Hidden inputs and dynamic facts are created outside the Candidate.  Each case
is launched through the product's production Gateway CLI, with a fresh state,
workspace, and broker attribution.  This module never authors a Candidate
result artifact and never computes a formal score.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
import signal
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from controller.two_round_controller import (  # noqa: E402
    DEV,
    HIDDEN,
    TwoRoundController,
    parse_time,
    tree_digest,
    tree_is_read_only,
    validate_freeze_manifest,
)
from controller.builder_session_controller import marker_paths  # noqa: E402
from evaluator.case_service import write_private_case  # noqa: E402
from evaluator.candidate_runtime import validate_runtime_manifest, prepare_candidate_runtime, write_runtime_manifest  # noqa: E402
from evaluator.formal_gates import (  # noqa: E402
    PRODUCTION_ENTRY,
    validate_broker_protocol,
    validate_builder_attestation,
    validate_hidden_files,
)
from lower_agent.launcher import (  # noqa: E402
    AGENT_CLOCK_SECONDS,
    CASE_TIMEOUT_SECONDS,
    broker_stats_delta,
    read_broker_stats,
    runtime_stats,
)
from lower_agent.owned_resources import run_owned, MEMORY_BYTES, _ambient_aggregate, verify_aggregate  # noqa: E402
from evaluator.durable_state import capture_durable_state, expected_from_facts, canonical as durable_canonical  # noqa: E402
from evaluator.case_budget import CaseBudget  # noqa: E402
from evaluator.candidate_outcome import semantic_admitted  # noqa: E402


def sha256_file(path: Path, *, deadline: float | None = None) -> str:
    digest = hashlib.sha256()
    with path.open('rb') as stream:
        while True:
            if deadline is not None and time.monotonic() >= deadline:
                raise TimeoutError('artifact hashing exhausted case finalization budget')
            chunk = stream.read(1024 * 1024)
            if deadline is not None and time.monotonic() >= deadline:
                raise TimeoutError('artifact hashing exhausted case finalization budget')
            if not chunk:
                break
            digest.update(chunk)
    return digest.hexdigest()

MODEL = "deepseek-flash"
EFFORT = "high"
PLACEHOLDER = "broker-only-placeholder"
PROBE_MARKER = "PROBE_ONLY_CANDIDATE2_MARKER"


def now() -> str:
    return datetime.now(timezone.utc).isoformat()


def write_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(value, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    os.replace(temporary, path)


def freeze_run_mode(frozen_candidate: Path, *, formal: bool = False) -> tuple[str, str | None]:
    """Never infer formal provenance merely because a marker is absent."""
    hits = marker_paths(frozen_candidate)
    if formal and hits:
        raise RuntimeError(f"formal hidden run refuses {PROBE_MARKER}: {hits}")
    if formal:
        return "formal", None
    reason = f"explicit probe run; marker paths={hits}" if hits else "explicit probe run without Builder formal attestation"
    return "probe", reason


def safe_stats(endpoint: str, *, deadline: float | None = None) -> tuple[dict[str, object] | None, str | None]:
    try:
        if deadline is None:
            return read_broker_stats(endpoint), None
        return read_broker_stats(endpoint,timeout=min(5,deadline-time.monotonic())), None
    except Exception as exc:  # evaluator evidence records the exact failure class
        return None, f"{type(exc).__name__}: {exc}"


def private_values(path: Path) -> list[str]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
        return []
    return [str(item) for item in value.values() if isinstance(item, str)] if isinstance(value, dict) else []


def scrub_environment() -> dict[str, str]:
    env = dict(os.environ)
    sensitive_fragments = ("API_KEY", "ACCESS_KEY", "ACCESS_TOKEN", "AUTH_TOKEN", "CREDENTIAL", "CLIENT_SECRET", "PRIVATE_KEY")
    for key in list(env):
        upper = key.upper()
        if any(fragment in upper for fragment in sensitive_fragments):
            env.pop(key, None)
        elif "UPSTREAM" in upper and ("KEY" in upper or "TOKEN" in upper or "URL" in upper):
            env.pop(key, None)
    env["OPENAI_API_KEY"] = PLACEHOLDER
    env["AGENTSWE_REQUIRED_MODEL"] = MODEL
    env["AGENTSWE_REQUIRED_REASONING_EFFORT"] = EFFORT
    env["OPENCLAW_SKIP_CHANNELS"] = "1"
    return env


def route_params(case_id: str, view: dict[str, Any], task: str) -> dict[str, object]:
    channel = view.get("channel") if isinstance(view.get("channel"), dict) else {}
    params: dict[str, object] = {
        "message": task,
        "agentId": "main",
        "sessionKey": f"agent:main:{case_id}:loopback",
        # WebChat is OpenClaw's internal Gateway channel; synthetic provider
        # names are intentionally never put on the production RPC.
        "channel": "webchat",
        "accountId": str(channel.get("account_id", "default")),
        "to": str(channel.get("peer_id", "case-peer")),
        "deliver": False,
        "timeout": AGENT_CLOCK_SECONDS,
        "idempotencyKey": f"{case_id}-agent-loop",
    }
    if channel.get("thread_id"):
        params["threadId"] = str(channel["thread_id"])
    return params


def terminate_process_group(process: subprocess.Popen[str]) -> None:
    if process.poll() is not None:
        return
    try:
        os.killpg(process.pid, signal.SIGTERM)
        process.wait(timeout=10)
    except Exception:
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except Exception:
            pass


def launch_case(
    *,
    case_id: str,
    hidden_case: Path,
    frozen_candidate: Path,
    output: Path,
    broker_endpoint: str,
    runtime: Path | None,
    private_file: Path,
    view: dict[str, Any],
    timeout_seconds: int,
    run_mode: str = "probe",
    probe_reason: str | None = None,
    evaluation_started_monotonic: float | None = None,
    absolute_case_deadline: float | None = None,
    candidate_runtime_manifest_path: Path | None = None,
) -> dict[str, Any]:
    started = now()
    invocation_monotonic = time.monotonic()
    started_monotonic = invocation_monotonic if evaluation_started_monotonic is None else evaluation_started_monotonic
    if not math.isfinite(started_monotonic) or started_monotonic > invocation_monotonic:
        raise ValueError('case preparation start must be a finite past monotonic time')
    if not math.isfinite(timeout_seconds) or timeout_seconds <= 0:
        raise ValueError('case timeout must be finite and positive')
    effective_timeout_seconds = min(timeout_seconds, CASE_TIMEOUT_SECONDS)
    case_deadline = started_monotonic + effective_timeout_seconds
    if absolute_case_deadline is not None:
        if not math.isfinite(absolute_case_deadline):
            raise ValueError('case absolute deadline must be finite')
        case_deadline = min(case_deadline, absolute_case_deadline)
    if case_deadline <= invocation_monotonic:
        raise TimeoutError('case budget exhausted during preparation, before launcher dispatch')
    budget=CaseBudget(started_monotonic,case_deadline)
    evaluator_parent=_ambient_aggregate()
    evaluator_parent_observed=(verify_aggregate({'unit':evaluator_parent,'cgroup':'/'+evaluator_parent,
        'memory_bytes':MEMORY_BYTES}) if evaluator_parent else None)
    if run_mode == 'formal' and evaluator_parent_observed is None:
        raise RuntimeError('formal case preparation and observer require the verified aggregate suite/public worker')
    digest_before = tree_digest(frozen_candidate,deadline=budget.product_deadline)
    runtime_identity=None
    if candidate_runtime_manifest_path is not None:
        runtime_identity=sha256_file(candidate_runtime_manifest_path,deadline=budget.product_deadline)
        runtime_record=json.loads(candidate_runtime_manifest_path.read_text())
        if runtime_record.get('runtime_source_digest')!=digest_before:
            raise ValueError('runtime manifest does not identify the executed source')
    budget.remaining_for_launcher()
    case_output = output.resolve()
    if case_output.exists() and any(case_output.iterdir()):
        raise RuntimeError(f"hidden case output already exists: {case_output}")
    case_output.mkdir(parents=True, exist_ok=True)
    workspace = case_output / "workspace"
    workspace.mkdir(parents=True, exist_ok=True)
    # This is the only case-local projection made visible to the real agent.
    # The private facts remain in a separate evaluator-owned directory.
    write_json(workspace / "case_view.json", view)
    task_file = case_output / "evaluator_input.md"
    task_text = (hidden_case / "input.md").read_text(encoding="utf-8")
    task_text += (
        "\n\nThe evaluator has supplied a redacted runtime projection at "
        f"{workspace / 'case_view.json'}. Read that projection through the "
        "running OpenClaw environment and use only its visible fields. The "
        "projection is not an oracle and may omit information needed to prove "
        "a final state."
    )
    task_file.write_text(task_text + "\n", encoding="utf-8")
    params_file = case_output / "gateway_params.json"
    write_json(params_file, route_params(case_id, view, task_text))
    before, before_error = safe_stats(broker_endpoint,deadline=budget.product_deadline)
    write_json(case_output / "broker_before_suite_case.json", before if before is not None else {"status": "unavailable", "error": before_error})

    command = [
        sys.executable,
        str(ROOT / "lower_agent" / "launcher.py"),
        "--product",
        str(frozen_candidate),
        "--case",
        str(hidden_case),
        "--task-file",
        str(task_file),
        "--params",
        str(params_file),
        "--broker-endpoint",
        broker_endpoint,
        "--output",
        str(case_output),
        "--artifact-dir",
        str(workspace),
        "--private-values-file",
        str(private_file),
        "--case-deadline-monotonic",
        str(case_deadline),
        "--launcher-deadline-monotonic",
        str(budget.launcher_deadline),
    ]
    if runtime is not None:
        command.extend(["--runtime", str(runtime)])
    stdout_path, stderr_path = case_output / "launcher.stdout", case_output / "launcher.stderr"
    proc: subprocess.CompletedProcess[str] | None = None
    resource_attestation: dict[str, Any] | None = None
    resource_error: str | None = None
    timed_out = False
    stdout, stderr = "", ""
    try:
        remaining = budget.remaining_for_launcher()
        proc, resource_attestation = run_owned(command, cwd=ROOT, env=scrub_environment(),
            output=case_output / 'case_resources', timeout=remaining, memory_bytes=MEMORY_BYTES)
        stdout, stderr = proc.stdout or '', proc.stderr or ''
        timed_out = resource_attestation.get('timed_out') is True
    except (OSError, RuntimeError, ValueError, subprocess.SubprocessError) as exc:
        stderr = f"{type(exc).__name__}: {exc}"
        resource_error = stderr
        proc = None
    stdout_path.write_text(stdout[-12000:], encoding="utf-8")
    stderr_path.write_text(stderr[-12000:], encoding="utf-8")

    evaluator_report_path = case_output / "run_report.json"
    evaluator_report: dict[str, Any] = {}
    if evaluator_report_path.is_file():
        try:
            loaded = json.loads(evaluator_report_path.read_text(encoding="utf-8"))
            if isinstance(loaded, dict):
                evaluator_report = loaded
        except (OSError, UnicodeDecodeError, json.JSONDecodeError):
            evaluator_report = {}
    finalization_started=time.monotonic()
    after, after_error = safe_stats(broker_endpoint,deadline=budget.evidence_deadline)
    write_json(case_output / "broker_after_suite_case.json", after if after is not None else {"status": "unavailable", "error": after_error})
    delta = broker_stats_delta(before, after)
    digest_after=None;digest_error=None
    try:
        digest_after = tree_digest(frozen_candidate,deadline=budget.evidence_deadline)
    except (OSError,ValueError,TimeoutError) as exc:
        digest_error=type(exc).__name__
    digest_finished=time.monotonic()
    artifact_contract = evaluator_report.get("artifact_contract")
    if not isinstance(artifact_contract, dict):
        artifact_contract = {"valid": False, "error": "evaluator run report missing artifact_contract"}
    resource_valid = (isinstance(resource_attestation, dict) and resource_attestation.get('valid') is True
                      and (resource_attestation.get('cleanup') or {}).get('complete') is True)
    # Read only after the entire owned lower scope is verified empty, not
    # merely after a Gateway wrapper exits. State paths are evaluator-fixed.
    try:
        facts = json.loads(private_file.read_text())
        expected, hidden_values = expected_from_facts(facts,case_id)
        durable = capture_durable_state(state=case_output/'gateway_state',
            output=case_output/'durable-state',deadline=budget.evidence_deadline,expected=expected,
            private_values=hidden_values,writers_stopped=resource_valid)
        durable['case_binding'] = {'case_id':case_id,
            'case_bundle_sha256':hashlib.sha256(durable_canonical(facts)).hexdigest(),
            'owned_scope_unit':resource_attestation.get('unit') if isinstance(resource_attestation,dict) else None}
    except (OSError, ValueError, KeyError, TypeError) as exc:
        durable = {'schema_version':'openclaw-independent-durable-evidence/v1',
            'collection_valid':False,'error':'durable_context:'+type(exc).__name__}
    write_json(case_output/'durable_state.json',durable)
    if not resource_valid:
        classification, reason = 'launcher_infrastructure_error', resource_error or 'case resource scope or cleanup was not verified'
    elif timed_out:
        classification, reason = "launcher_infrastructure_error", "hidden lower-agent invocation timed out"
    elif proc is None:
        classification, reason = "launcher_infrastructure_error", stderr[-1000:] or "unable to launch lower agent"
    elif digest_error is not None:
        classification, reason = 'launcher_infrastructure_error', 'stopped-product digest could not be verified: '+digest_error
    elif digest_after != digest_before:
        classification, reason = "frozen_candidate_mutation", "frozen Candidate digest changed during hidden invocation"
    elif evaluator_report.get("classification"):
        classification, reason = str(evaluator_report["classification"]), str(evaluator_report.get("classification_reason", ""))
    elif delta.get('unattributed_failures', delta["failures"]) and not delta["successful_calls"]:
        classification, reason = "broker_infrastructure_error", "all broker calls failed"
    else:
        classification, reason = "launcher_infrastructure_error", "lower launcher produced no evaluator classification"
    if resource_valid and durable.get('collection_valid') is not True:
        classification, reason = 'launcher_infrastructure_error', 'independent durable-state evidence unavailable: '+str(durable.get('error'))
    if before is None or after is None:
        classification, reason = 'broker_infrastructure_error', 'case-bound before/after broker evidence is unavailable'
    elif delta.get('unattributed_usage_unknown_calls', delta.get('usage_unknown_calls', 0)) > 0:
        classification, reason = 'broker_infrastructure_error', 'case-local provider outcome or usage remains unresolved; no automatic replay'
    if time.monotonic() > case_deadline:
        classification, reason = 'launcher_infrastructure_error', 'case envelope exceeded during execution/cleanup/evidence capture; attribution incomplete'

    result = {
        "schema_version": "openclaw-agentloop-hidden-run-v1",
        "case_id": case_id,
        "started_at": started,
        "finished_at": now(),
        "hidden_case_input": str(hidden_case / "input.md"),
        "production_entry": evaluator_report.get('command_entry'),
        "planned_production_entry": PRODUCTION_ENTRY,
        "command": [item if item != broker_endpoint else "<evaluator-broker-endpoint>" for item in command],
        "lower_exit_code": proc.returncode if proc is not None else None,
        "lower_execution_attempted": proc is not None,
        "timed_out": timed_out,
        "timing_contract": {"requested_timeout_seconds": timeout_seconds,
                            "effective_timeout_seconds": effective_timeout_seconds,
                            "agent_clock_seconds": AGENT_CLOCK_SECONDS,
                            "lower_timing_contract": evaluator_report.get("timing_contract"),
                            "absolute_case_deadline_monotonic": case_deadline,
                            "elapsed_seconds": time.monotonic() - started_monotonic,
                            "preparation_before_launcher_seconds": invocation_monotonic-started_monotonic,
                            "resource_attestation": str(case_output / 'case_resources/resource-attestation.json'),
                            "aggregate_scope_valid": resource_valid,
                            "finalization_budget": budget.snapshot(),
                            "finalization_started_monotonic": finalization_started,
                            "digest_finished_monotonic": digest_finished,
                            "finalization_elapsed_before_record_seconds": time.monotonic()-finalization_started,
                            "evaluator_parent_cgroup": Path("/proc/self/cgroup").read_text(),
                            "evaluator_aggregate_parent": evaluator_parent,
                            "evaluator_parent_observed": evaluator_parent_observed,
                            "parent_memory_charged": evaluator_parent_observed is not None,
                            "outside_scope": "external scheduler only; fixture/hash/observer share verified aggregate parent" if evaluator_parent_observed else "diagnostic direct caller outside aggregate parent; not formal eligible"},
        "case_resources": resource_attestation,
        "durable_state": durable,
        "classification": classification,
        "classification_reason": reason,
        "frozen_candidate_digest_before": digest_before,
        "frozen_candidate_digest_after": digest_after,
        "frozen_candidate_digest_stable": digest_before == digest_after,
        "broker_stats_before": str(case_output / "broker_before_suite_case.json") if before is not None else {"error": before_error},
        "broker_stats_after": str(case_output / "broker_after_suite_case.json") if after is not None else {"error": after_error},
        "broker_stats_delta": delta,
        "model": MODEL,
        "reasoning_effort": EFFORT,
        "credential_mode": "placeholder-only",
        "candidate_sensitive_environment_keys": [],
        "candidate_visibility": {"hidden_input_mounted": False, "private_facts_mounted": False, "oracle_mounted": False, "case_view_only": True},
        "artifact_contract": artifact_contract,
        "agent_authored_artifact": bool(artifact_contract.get("valid")) or semantic_admitted({'case_id':case_id,'artifact_contract':artifact_contract}),
        "candidate_runtime_manifest": str(candidate_runtime_manifest_path.resolve()) if candidate_runtime_manifest_path is not None else None,
        "candidate_runtime_manifest_sha256": runtime_identity,
        "evaluator_run_report": str(evaluator_report_path),
        "gateway_log": evaluator_report.get('gateway_log',str(case_output / "gateway.log")),
        "native_case": evaluator_report.get('native_case'),
        "native_agent": evaluator_report.get('native_agent'),
        "launcher_stdout": str(stdout_path),
        "launcher_stderr": str(stderr_path),
        "authored_artifact": str(workspace / "agent_result.json") if (artifact_contract.get("valid") or semantic_admitted({'case_id':case_id,'artifact_contract':artifact_contract})) else None,
        "formal_result_claimed": False,
        "run_mode": run_mode,
        "probe_reason": probe_reason,
    }
    if result["agent_authored_artifact"] and result["authored_artifact"]:
        artifact_path = Path(str(result["authored_artifact"])).resolve()
        try:
            result["artifact_sha256"] = sha256_file(artifact_path,deadline=budget.evidence_deadline)
            admission_sha=artifact_contract.get('core_admission',{}).get('sha256')
            if admission_sha is not None and admission_sha!=result['artifact_sha256']:
                raise OSError('Agent artifact changed after lower capture')
            result["artifact_path"] = str(artifact_path)
            result["artifact_provenance"] = {
                "artifact_path": str(artifact_path),
                "artifact_owner": "lower_agent_product",
                "producer_entry": result['production_entry'],
                "evaluator_synthesized": False,
                "preexisting_before_launch": False,
                "trajectory_artifact_reference": True,
                "sha256": result["artifact_sha256"],
            }
        except (OSError, TimeoutError) as exc:
            classification, reason = 'launcher_infrastructure_error', 'final Agent artifact evidence unavailable: '+type(exc).__name__
            result.update(classification=classification,classification_reason=reason)
    trajectory = evaluator_report.get("trajectory")
    native_case=evaluator_report.get('native_case')
    client_events=native_case.get('agent_client_operations',[]) if isinstance(native_case,dict) else []
    write_json(case_output / "trajectory.json", (trajectory if isinstance(trajectory,list) else [])+client_events)
    write_json(
        case_output / "result.json",
        {
            "schema_version": "openclaw-agentloop-evaluator-result-v1",
            "case_id": case_id,
            "run_mode": run_mode,
            "formal_result_claimed": False,
            "classification": classification,
            "classification_reason": reason,
            "candidate_artifact": result["authored_artifact"],
            "artifact_contract": artifact_contract,
            "trajectory": str(case_output / "trajectory.json"),
            "attestation": str(case_output / "hidden_case_attestation.json"),
        },
    )
    write_json(case_output / "hidden_case_attestation.json", result)
    # Publishing the evidence is part of the original case envelope too.
    # Late writes may be retained as diagnostics, never as a scoreable run.
    if time.monotonic() > case_deadline:
        reason = 'case envelope exceeded while publishing final evidence; attribution incomplete'
        result.update(classification='launcher_infrastructure_error',classification_reason=reason)
        result['timing_contract']['elapsed_seconds'] = time.monotonic()-started_monotonic
        result['timing_contract']['record_publication_overrun'] = True
        write_json(case_output / 'result.json', {
            'schema_version':'openclaw-agentloop-evaluator-result-v1','case_id':case_id,
            'run_mode':run_mode,'formal_result_claimed':False,
            'classification':result['classification'],'classification_reason':reason,
            'candidate_artifact':result['authored_artifact'],'artifact_contract':artifact_contract,
            'trajectory':str(case_output/'trajectory.json'),
            'attestation':str(case_output/'hidden_case_attestation.json')})
        write_json(case_output / 'hidden_case_attestation.json', result)
    return result


def run_suite(
    *,
    freeze_manifest_path: Path,
    hidden_root: Path,
    output: Path,
    broker_endpoint: str,
    runtime: Path | None,
    timeout_seconds: int,
    formal: bool = False,
    pilot: bool = False,
    smoke: bool = False,
    case_ids: tuple[str, ...] = HIDDEN,
    builder_attestation_path: Path | None = None,
    runtime_product_manifest_path: Path | None = None,
    _suite_context: dict[str, Any] | None = None,
) -> dict[str, Any]:
    selected_cases = tuple(case_ids)
    if sum(bool(value) for value in (formal, pilot, smoke)) > 1:
        raise RuntimeError("formal, pilot and smoke hidden modes are mutually exclusive")
    if not selected_cases or not set(selected_cases).issubset(HIDDEN):
        raise RuntimeError("case_ids must be a non-empty subset of the canonical hidden inventory")
    if formal and selected_cases != HIDDEN:
        raise RuntimeError("formal hidden execution requires all six canonical cases")
    if pilot and selected_cases != ("test_001",):
        raise RuntimeError("pilot hidden execution requires exactly test_001")
    if _suite_context is None and (formal or pilot or smoke):
        from evaluator.suite_runtime import run_hidden_owned
        return run_hidden_owned(freeze_manifest_path=freeze_manifest_path, hidden_root=hidden_root,
            output=output, broker_endpoint=broker_endpoint, runtime=runtime, timeout_seconds=timeout_seconds,
            formal=formal, pilot=pilot, smoke=smoke, case_ids=selected_cases, builder_attestation_path=builder_attestation_path,
            runtime_product_manifest_path=runtime_product_manifest_path)
    suite_clock = _suite_context['clock'] if _suite_context is not None else None
    if suite_clock is not None:
        suite_clock.checkpoint('freeze_and_runtime_preflight')
    manifest = json.loads(freeze_manifest_path.read_text(encoding="utf-8"))
    freeze_manifest_sha256 = sha256_file(freeze_manifest_path)
    if not isinstance(manifest, dict):
        raise RuntimeError("freeze manifest must be an object")
    frozen_candidate = freeze_manifest_path.parent / "frozen_candidate"
    validate_freeze_manifest(manifest, frozen_candidate)
    run_mode, probe_reason = (
        ("pilot", "one-case pipeline pilot; excluded from formal Result and Code")
        if pilot
        else freeze_run_mode(frozen_candidate, formal=formal or smoke)
    )
    formal_gate_errors: list[str] = []
    if formal or smoke:
        if builder_attestation_path is None:
            raise RuntimeError("formal hidden run requires --builder-attestation")
        _, formal_gate_errors = validate_builder_attestation(
            attestation_path=builder_attestation_path.resolve(),
            freeze_manifest_path=freeze_manifest_path,
            frozen_candidate=frozen_candidate,
        )
        if formal_gate_errors:
            raise RuntimeError("formal Builder/freeze gate failed: " + "; ".join(formal_gate_errors))
    elif pilot:
        if builder_attestation_path is None:
            raise RuntimeError("pilot hidden run requires --builder-attestation")
        builder_attestation = json.loads(builder_attestation_path.read_text(encoding="utf-8"))
        if not isinstance(builder_attestation, dict) or builder_attestation.get("pilot_lifecycle_eligible") is not True:
            raise RuntimeError("pilot Builder/freeze gate failed")
        if builder_attestation.get("public_case_inventory") != ["dev_001"]:
            raise RuntimeError("pilot Builder attestation has the wrong public inventory")
    execution_candidate = frozen_candidate
    runtime_product_manifest: dict[str, Any] | None = None
    if runtime_product_manifest_path is not None:
        loaded = json.loads(runtime_product_manifest_path.read_text(encoding="utf-8"))
        if not isinstance(loaded, dict):
            raise RuntimeError("runtime product manifest must be an object")
        runtime_product_manifest = loaded
        execution_candidate = Path(str(loaded.get("runtime_product", ""))).resolve()
        runtime_errors = validate_runtime_manifest(loaded, frozen_candidate)
        if execution_candidate.is_dir() and marker_paths(execution_candidate):
            runtime_errors.append("Candidate runtime contains prohibited probe marker")
        if runtime_errors:
            raise RuntimeError("runtime product gate failed: " + "; ".join(runtime_errors))
    elif formal or pilot or smoke:
        raise RuntimeError(f"{run_mode} hidden run requires --runtime-product-manifest")
    if suite_clock is not None:
        # A public-dev build completed before Builder exit cannot start a new
        #4800s wall clock for free. Build once from the frozen source *inside*
        #this hidden suite, then reuse that exact image across its cases.
        control = _suite_context['control']
        runtime_product_manifest = prepare_candidate_runtime(candidate=frozen_candidate,
            output=control/'runtime-product',runtime=runtime,
            deadline_monotonic=suite_clock.build_deadline())
        runtime_product_manifest_path = control/'runtime_manifest.json'
        write_runtime_manifest(runtime_product_manifest_path,runtime_product_manifest)
        suite_clock.checkpoint('cold_build_complete')
        errors=validate_runtime_manifest(runtime_product_manifest,frozen_candidate)
        if errors:
            raise RuntimeError('suite cold build unavailable: '+'; '.join(errors))
        execution_candidate=Path(runtime_product_manifest['runtime_product'])
    if output.exists() and any(output.iterdir()):
        raise RuntimeError(f"hidden suite output already exists: {output}")
    output.mkdir(parents=True, exist_ok=True)
    hidden_root = hidden_root.resolve()
    for case_id in selected_cases:
        case_dir = hidden_root / case_id
        if not (case_dir / "input.md").is_file():
            raise RuntimeError(f"hidden case input missing: {case_dir / 'input.md'}")

    private_root = output.parent / f".{output.name}-evaluator-private"
    private_root.mkdir(parents=True, exist_ok=True)
    records: list[dict[str, Any]] = []
    suite_before, suite_before_error = safe_stats(broker_endpoint)
    freeze_digest = str(manifest["candidate_digest"])
    integrity_abort = False
    budget_abort = False

    def evaluate_hidden(_: Path) -> dict[str, Any]:
        nonlocal integrity_abort, budget_abort
        for case_id in selected_cases:
            if integrity_abort or budget_abort:
                break
            case_dir = hidden_root / case_id
            private_case = private_root / case_id
            try:
                case_started,case_deadline=(suite_clock.begin_case(case_id) if suite_clock is not None else (None,None))
                _, view = write_private_case(private_case, case_id)
                if runtime_product_manifest is not None:
                    runtime_errors = validate_runtime_manifest(runtime_product_manifest, frozen_candidate)
                    if runtime_errors:
                        raise RuntimeError('runtime product gate failed before case: '+ '; '.join(runtime_errors))
                record = launch_case(
                    case_id=case_id,
                    hidden_case=case_dir,
                    frozen_candidate=execution_candidate,
                    output=output / case_id,
                    broker_endpoint=broker_endpoint,
                    runtime=runtime,
                    private_file=private_case / "private_facts.json",
                    view=view,
                    timeout_seconds=timeout_seconds,
                    run_mode=run_mode,
                    probe_reason=probe_reason,
                    evaluation_started_monotonic=case_started,
                    absolute_case_deadline=case_deadline,
                    candidate_runtime_manifest_path=runtime_product_manifest_path,
                )
            except Exception as exc:
                from evaluator.suite_runtime import SuiteBudgetExpired
                if isinstance(exc, SuiteBudgetExpired):
                    budget_abort = True
                case_output = output / case_id
                case_output.mkdir(parents=True, exist_ok=True)
                before, before_error = safe_stats(broker_endpoint)
                after, after_error = safe_stats(broker_endpoint)
                write_json(case_output / "broker_before_suite_case.json", before if before is not None else {"status": "unavailable", "error": before_error})
                write_json(case_output / "broker_after_suite_case.json", after if after is not None else {"status": "unavailable", "error": after_error})
                digest = tree_digest(frozen_candidate)
                record = {
                    "schema_version": "openclaw-agentloop-hidden-run-v1",
                    "case_id": case_id,
                    "started_at": now(),
                    "finished_at": now(),
                    "production_entry": None,
                    "planned_production_entry": PRODUCTION_ENTRY,
                    "lower_execution_attempted": False,
                    "classification": "launcher_infrastructure_error",
                    "classification_reason": f"{type(exc).__name__}: {exc}",
                    "frozen_candidate_digest_before": digest,
                    "frozen_candidate_digest_after": digest,
                    "frozen_candidate_digest_stable": True,
                    "broker_stats_before": str(case_output / "broker_before_suite_case.json"),
                    "broker_stats_after": str(case_output / "broker_after_suite_case.json"),
                    "broker_stats_delta": broker_stats_delta(before, after),
                    "model": MODEL,
                    "reasoning_effort": EFFORT,
                    "credential_mode": "placeholder-only",
                    "candidate_sensitive_environment_keys": [],
                    "artifact_contract": {"valid": False, "error": f"{type(exc).__name__}: {exc}"},
                    "formal_result_claimed": False,
                    "run_mode": run_mode,
                    "probe_reason": probe_reason,
                }
                write_json(case_output / "trajectory.json", [])
                write_json(
                    case_output / "result.json",
                    {
                        "schema_version": "openclaw-agentloop-evaluator-result-v1",
                        "case_id": case_id,
                        "run_mode": run_mode,
                        "formal_result_claimed": False,
                        "classification": record["classification"],
                        "classification_reason": record["classification_reason"],
                        "candidate_artifact": None,
                        "artifact_contract": {"valid": False, "error": record["classification_reason"]},
                        "trajectory": str(case_output / "trajectory.json"),
                        "attestation": str(case_output / "hidden_case_attestation.json"),
                    },
                )
                write_json(case_output / "hidden_case_attestation.json", record)
            records.append(record)
            if tree_digest(frozen_candidate) != freeze_digest:
                integrity_abort = True
        return {case_id: next((item for item in records if item.get("case_id") == case_id), {"classification": "not-run"}) for case_id in selected_cases}

    # Point the compatibility gate at the lifecycle run directory (the parent
    # of frozen_candidate), not at the hidden-output directory.  This keeps the
    # canonical frozen tree and its manifest in one immutable lifecycle scope.
    controller = TwoRoundController(
        freeze_manifest_path.parent,
        lambda _number, _candidate: {},
        dev_case_ids=("dev_001",) if pilot else DEV,
        hidden_case_ids=selected_cases,
        evidence_kind="pilot" if pilot else ("formal" if formal or smoke else "probe"),
    )
    controller.frozen = dict(manifest)
    hidden_result = controller.run_hidden(evaluate_hidden)
    if suite_clock is not None:
        suite_clock.checkpoint('cases_complete_and_final_assembly')
    write_json(output / "hidden_result.json", hidden_result)
    suite_after, suite_after_error = safe_stats(broker_endpoint)
    suite_delta = broker_stats_delta(suite_before, suite_after)
    executed = [item for item in records if item.get("case_id") in selected_cases and item.get('lower_execution_attempted') is True]
    if formal or smoke:
        formal_gate_errors.extend(validate_broker_protocol(suite_before))
        formal_gate_errors.extend(validate_broker_protocol(suite_after))
        formal_gate_errors.extend(validate_hidden_files(output, hidden_result, selected_cases=selected_cases))
        if len(executed) != len(selected_cases):
            formal_gate_errors.append("suite did not execute all requested hidden cases")
        if integrity_abort:
            formal_gate_errors.append("formal suite aborted after frozen Candidate integrity change")
        if budget_abort:
            formal_gate_errors.append("formal suite stopped before granting a shortened or unbudgeted case")
        # No successful model call is Candidate behavior, not by itself an
        # infrastructure fault. The semantic/fatal outcome path still requires
        # the real entry, locked broker, complete evidence and causal attribution.
    formal_evidence_valid = bool(formal and not formal_gate_errors)
    smoke_evidence_complete = bool(smoke and not formal_gate_errors)
    attestation = {
        "schema_version": "openclaw-hidden-suite-attestation-v2",
        "case_inventory": list(selected_cases),
        "executed_case_ids": [str(item["case_id"]) for item in executed],
        "executed_count": len(executed),
        "all_six_invoked": formal and len(executed) == len(HIDDEN),
        "pilot_case_invoked": pilot and [item.get("case_id") for item in executed] == ["test_001"],
        "freeze_manifest": str(freeze_manifest_path),
        "freeze_at": manifest["frozen_at"],
        "freeze_manifest_sha256": freeze_manifest_sha256,
        "hidden_started_at": controller.hidden_state["hidden_started_at"],
        "hidden_completed_at": controller.hidden_state["hidden_completed_at"],
        "freeze_before_hidden": parse_time(str(manifest["frozen_at"])) <= parse_time(str(controller.hidden_state["hidden_started_at"])),
        "candidate_digest": freeze_digest,
        "candidate_digest_stable": all(item.get("frozen_candidate_digest_stable") is True for item in executed) and not integrity_abort,
        "integrity_abort": integrity_abort,
        "real_openclaw_gateway_entry": all(item.get("production_entry") == PRODUCTION_ENTRY for item in executed),
        "successful_model_invocation_observed": suite_delta.get('successful_calls',0)>0,
        "broker": {"before": suite_before if suite_before is not None else {"error": suite_before_error}, "after": suite_after if suite_after is not None else {"error": suite_after_error}, "delta": suite_delta, "protocol": {"model": MODEL, "reasoning_effort": EFFORT}},
        "scheduled_records_used": False,
        "oracle_disclosed": False,
        "formal_result_claimed": False,
        "run_mode": run_mode,
        "probe_reason": probe_reason,
        "builder_session_attestation": str(builder_attestation_path.resolve()) if builder_attestation_path else None,
        "runtime_product_manifest": str(runtime_product_manifest_path.resolve()) if runtime_product_manifest_path else None,
        "runtime_product_compiled_digest": runtime_product_manifest.get("compiled_digest") if runtime_product_manifest else None,
        "formal_evidence_valid": formal_evidence_valid,
        "evidence_kind": "smoke" if smoke else ("formal" if formal else "pilot" if pilot else "probe"),
        "smoke_evidence_complete": smoke_evidence_complete,
        "pilot_evidence_complete": bool(
            pilot
            and len(executed) == 1
            and executed[0].get("case_id") == "test_001"
            and executed[0].get("frozen_candidate_digest_stable") is True
            and executed[0].get("production_entry") == PRODUCTION_ENTRY
            and suite_delta.get("successful_calls", 0) > 0
        ),
        "formal_gate_errors": formal_gate_errors,
        "case_attestations": [str(output / str(item["case_id"]) / "hidden_case_attestation.json") for item in executed],
        "hidden_result": str(output / "hidden_result.json"),
    }
    write_json(output / "hidden-after-freeze-attestation.json", attestation)
    # The controller's basic attestation is deliberately supplemented with the
    # suite-level record above; it remains a non-score evidence artifact.
    if sha256_file(freeze_manifest_path) != freeze_manifest_sha256:
        raise RuntimeError("immutable freeze manifest changed during hidden execution")
    summary = {
        "schema_version": "openclaw-agentloop-hidden-suite-v2",
        "case_inventory": list(selected_cases),
        "cases": hidden_result,
        "executed_count": len(executed),
        "formal_result_claimed": False,
        "run_mode": run_mode,
        "probe_reason": probe_reason,
        "scheduled_records_used": False,
        "formal_evidence_valid": formal_evidence_valid,
        "evidence_kind": "smoke" if smoke else ("formal" if formal else "pilot" if pilot else "probe"),
        "smoke_evidence_complete": smoke_evidence_complete,
        "pilot_evidence_complete": attestation["pilot_evidence_complete"],
        "formal_gate_errors": formal_gate_errors,
        "suite_attestation": str(output / "hidden-after-freeze-attestation.json"),
        "runtime_product_manifest": str(runtime_product_manifest_path) if runtime_product_manifest_path is not None else None,
        "suite_clock": suite_clock.snapshot() if suite_clock is not None else None,
    }
    write_json(output / "summary.json", summary)
    return summary


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Run OpenClaw hidden cases after latest accepted Candidate freeze")
    parser.add_argument("--freeze-manifest", type=Path, required=True)
    parser.add_argument("--hidden-root", type=Path, default=ROOT / "test_cases")
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--broker-endpoint", required=True)
    parser.add_argument("--runtime", type=Path)
    parser.add_argument("--timeout-seconds", type=int, default=CASE_TIMEOUT_SECONDS)
    parser.add_argument("--formal", action="store_true", help="require same-session Builder provenance and reject probe Candidates")
    parser.add_argument("--builder-attestation", type=Path)
    parser.add_argument("--runtime-product-manifest", type=Path)
    args = parser.parse_args(argv)
    summary = run_suite(
        freeze_manifest_path=args.freeze_manifest.resolve(),
        hidden_root=args.hidden_root.resolve(),
        output=args.output.resolve(),
        broker_endpoint=args.broker_endpoint,
        runtime=args.runtime.resolve() if args.runtime else None,
        timeout_seconds=args.timeout_seconds,
        formal=args.formal,
        builder_attestation_path=args.builder_attestation.resolve() if args.builder_attestation else None,
        runtime_product_manifest_path=args.runtime_product_manifest.resolve() if args.runtime_product_manifest else None,
    )
    print(json.dumps(summary, indent=2, ensure_ascii=False))
    return 0 if not args.formal or summary.get("formal_evidence_valid") is True else 2


if __name__ == "__main__":
    raise SystemExit(main())
