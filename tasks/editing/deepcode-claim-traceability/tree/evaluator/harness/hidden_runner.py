#!/usr/bin/env python3
"""Evaluator-owned post-freeze executor for one canonical hidden case."""
from __future__ import annotations

import argparse
import json
import os
import shutil
import stat
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent))

if __package__ in {None, ""}:
    from candidate_adapter import tree_digest
    from case_specs import HIDDEN_CASES, canonical_hidden_case
    from dynamic_case_runtime import assert_not_candidate_visible, create_case, prepare_project
    from semantic_oracle import observe as observe_semantics
else:
    from .candidate_adapter import tree_digest
    from .case_specs import HIDDEN_CASES, canonical_hidden_case
    from .dynamic_case_runtime import assert_not_candidate_visible, create_case, prepare_project
    from .semantic_oracle import observe as observe_semantics


FREEZE_SCHEMA = "deepcode-agentloop-freeze-v2"
CASE_SCHEMA = "deepcode-agentloop-hidden-run-v2"


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def parse_utc(value: object, *, field: str) -> datetime:
    if not isinstance(value, str):
        raise RuntimeError(f"{field} must be an ISO-8601 timestamp")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise RuntimeError(f"{field} must be an ISO-8601 timestamp") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise RuntimeError(f"{field} must include a timezone offset")
    return parsed.astimezone(timezone.utc)


def write_json(path: Path, value: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(value, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    os.replace(temporary, path)


def file_sha256(path: Path) -> str | None:
    if not path.is_file():
        return None
    import hashlib
    return hashlib.sha256(path.read_bytes()).hexdigest()


def write_evidence_manifest(case_dir: Path, paths: dict[str, str | None]) -> Path:
    """Persist machine-checkable provenance for every case evidence file."""
    files: dict[str, dict[str, Any]] = {}
    for name, raw in paths.items():
        path = Path(raw) if raw else None
        exists = bool(path and path.is_file())
        files[name] = {
            "path": str(path) if path else None,
            "exists": exists,
            "size_bytes": path.stat().st_size if exists and path else None,
            "sha256": file_sha256(path) if exists and path else None,
        }
    destination = case_dir / "evidence-manifest.json"
    write_json(destination, {
        "schema_version": "deepcode-hidden-case-evidence/v1",
        "case_id": case_dir.name,
        "files": files,
    })
    return destination


def read_object(path: Path) -> dict[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise RuntimeError(f"expected JSON object: {path}")
    return value


def is_tree_read_only(root: Path) -> tuple[bool, list[str]]:
    writable: list[str] = []
    for path in [root, *root.rglob("*")]:
        if path.is_symlink():
            continue
        try:
            mode = stat.S_IMODE(path.stat().st_mode)
        except OSError:
            writable.append(str(path))
            continue
        if mode & (stat.S_IWUSR | stat.S_IWGRP | stat.S_IWOTH):
            writable.append(str(path))
    return not writable, writable[:50]


def make_writable(root: Path) -> None:
    """Make only the disposable per-case Candidate copy writable."""
    for path in sorted(root.rglob("*"), key=lambda item: len(item.parts), reverse=True):
        if path.is_symlink():
            continue
        mode = stat.S_IMODE(path.stat().st_mode)
        path.chmod(mode | stat.S_IWUSR | (stat.S_IXUSR if path.is_dir() else 0))
    mode = stat.S_IMODE(root.stat().st_mode)
    root.chmod(mode | stat.S_IWUSR | stat.S_IXUSR)


def disjoint(child: Path, protected: Path) -> bool:
    try:
        child.resolve().relative_to(protected.resolve())
    except ValueError:
        return True
    return False


def load_freeze(path: Path) -> tuple[dict[str, Any], Path, str, datetime]:
    manifest = read_object(path)
    if manifest.get("schema_version") != FREEZE_SCHEMA:
        raise RuntimeError("hidden execution requires a valid frozen Candidate manifest")
    if manifest.get("hidden_allowed") is not True or not 1 <= int(manifest.get("source_submission", 0)) <= 10:
        raise RuntimeError("hidden execution requires a latest accepted Candidate freeze")
    if manifest.get("source_submission") != manifest.get("accepted_submission_count"):
        raise RuntimeError("hidden execution requires the latest accepted Candidate freeze")
    digests = manifest.get("accepted_candidate_digests")
    if not isinstance(digests, list) or len(digests) != manifest.get("accepted_submission_count") or len(set(digests)) != len(digests):
        raise RuntimeError("freeze manifest accepted-submission ledger is invalid")
    if (
        manifest.get("frozen_tree_read_only") is not True
        or manifest.get("frozen_digest_stable") is not True
        or manifest.get("frozen_digest_before") != manifest.get("candidate_digest")
        or manifest.get("frozen_digest_after") != manifest.get("candidate_digest")
    ):
        raise RuntimeError("freeze manifest does not attest an immutable stable accepted Candidate copy")
    frozen_at = parse_utc(manifest.get("frozen_at"), field="frozen_at")
    candidate = Path(str(manifest.get("candidate_path", ""))).resolve()
    if not candidate.is_dir() or candidate.is_symlink():
        raise RuntimeError("immutable frozen Candidate copy is unavailable")
    expected_digest = str(manifest.get("candidate_digest", ""))
    if len(expected_digest) != 64 or any(char not in "0123456789abcdef" for char in expected_digest):
        raise RuntimeError("freeze manifest has an invalid Candidate digest")
    if tree_digest(candidate) != expected_digest:
        raise RuntimeError("frozen Candidate digest changed before hidden execution")
    read_only, writable = is_tree_read_only(candidate)
    if not read_only:
        raise RuntimeError(f"frozen Candidate is not immutable: {writable[:3]}")
    return manifest, candidate, expected_digest, frozen_at


def classify_launcher(
    *,
    process: subprocess.CompletedProcess[str],
    lower_result: dict[str, Any] | None,
    result_error: str | None,
) -> tuple[str, str]:
    if result_error or lower_result is None:
        return "launcher_infrastructure_error", result_error or "lower-agent result missing"
    classification = str(lower_result.get("classification") or "")
    allowed = {
        "candidate_valid",
        # execution_evidence.py classifies a parseable but schema-incomplete artifact as
        # candidate_partial (contract_valid True); the public path accepts it, and the
        # judge grades completeness. Rejecting it here voided every such hidden case.
        "candidate_partial",
        "candidate_behavior_failure",
        "provider_failure",
        "credential_infrastructure_error",
        "protocol_infrastructure_error",
        "broker_infrastructure_error",
        "launcher_infrastructure_error",
        "evaluator_infrastructure_error",
    }
    if classification not in allowed:
        return "evaluator_infrastructure_error", f"unknown lower classification: {classification!r}"
    if process.returncode not in (0, 1) and classification not in {
        "provider_failure", "credential_infrastructure_error", "protocol_infrastructure_error",
        "broker_infrastructure_error", "launcher_infrastructure_error"
    }:
        return "launcher_infrastructure_error", f"lower launcher exited {process.returncode}"
    return classification, "lower-agent classification preserved"


def _run_in_scope(
    *,
    frozen_manifest: Path,
    hidden_root: Path,
    case_id: str,
    output: Path,
    broker_endpoint: str,
    python_executable: Path | None = None,
    launcher: Path | None = None,
    timeout: int = 600,
    case_deadline_monotonic: float | None = None,
) -> dict[str, object]:
    if case_id not in HIDDEN_CASES:
        raise RuntimeError(f"unknown hidden case: {case_id}")
    manifest, candidate, expected_digest, frozen_at = load_freeze(frozen_manifest.resolve())
    hidden_case = canonical_hidden_case(case_id, hidden_root)
    canonical_root = hidden_case.parent.resolve()

    output = output.resolve()
    if output.exists() and any(output.iterdir()):
        raise RuntimeError(f"hidden output already exists: {output}")
    output.mkdir(parents=True, exist_ok=True)
    for protected, label in ((candidate, "frozen Candidate"), (canonical_root, "canonical test_cases")):
        if not disjoint(output, protected):
            raise RuntimeError(f"hidden output may not be inside {label}")

    started_at = utc_now()
    started_dt = parse_utc(started_at, field="started_at")
    if started_dt < frozen_at:
        raise RuntimeError("hidden execution began before Candidate freeze")
    started_monotonic = time.monotonic()
    deadline = case_deadline_monotonic or started_monotonic + min(timeout, 590)
    digest_before = tree_digest(candidate)

    evaluator_state = output / "evaluator-private"
    state = create_case(case_id, evaluator_state)
    task_text = (hidden_case / "input.md").read_text(encoding="utf-8")
    prepared_project = output / 'prepared-project'
    world_facts=prepare_project(case_id, hidden_case / 'assets/project', prepared_project, state)

    runtime_projection = {
        "case_id": state["case_id"],
        "run_nonce": state["nonce"],
        "tenant": state["tenant"],
        "project": state["project"],
        "operation_id": state["operation_id"],
        "actual_project_seed": world_facts,
    }
    task_input = output / "task_input.md"
    task_input.write_text(
        task_text
        + "\n\n## Case-local runtime identity\n\n"
        + "The following identifiers are generated for this invocation and are "
        + "observable runtime facts, not expected outcomes or an oracle. Preserve "
        + "them in evidence only where the product exposes a corresponding field; "
        + "do not replace the task's declared scientific tenant/project with them.\n\n"
        + json.dumps(runtime_projection, indent=2, sort_keys=True)
        + "\n",
        encoding="utf-8",
    )
    candidate_runtime = output / "candidate-runtime"
    shutil.copytree(candidate, candidate_runtime, symlinks=True)
    make_writable(candidate_runtime)
    workspace = output / "workspace"
    deepcode_home = output / "deepcode-home"
    lower_output = output / "lower-agent"
    assert_not_candidate_visible(candidate_runtime, evaluator_state)
    assert_not_candidate_visible(candidate_runtime, hidden_case)
    assert_not_candidate_visible(candidate_runtime, frozen_manifest)

    launcher = (launcher or Path(__file__).with_name("deepcode_lower_agent.py")).resolve()
    runtime_python = Path(python_executable or sys.executable).absolute()
    command = [
        sys.executable,
        str(launcher),
        "--repository", str(candidate_runtime),
        "--task-file", str(task_input),
        "--workspace", str(workspace),
        "--project-source", str(prepared_project),
        "--deepcode-home", str(deepcode_home),
        "--broker-endpoint", broker_endpoint,
        "--output", str(lower_output),
        "--case-id", case_id,
        "--python", str(runtime_python),
        "--timeout", str(min(timeout, 600)),
        "--sandbox", "required",
        "--case-deadline-monotonic", str(deadline),
    ]
    launcher_started = utc_now()
    timed_out = False
    launch_error: str | None = None
    try:
        process = subprocess.run(
            command,
            text=True,
            capture_output=True,
            timeout=max(.001, deadline-time.monotonic()),
            check=False,
        )
    except subprocess.TimeoutExpired as exc:
        timed_out = True
        process = subprocess.CompletedProcess(command, 124, exc.stdout or "", exc.stderr or "timeout")
    except OSError as exc:
        launch_error = f"{type(exc).__name__}: {exc}"
        process = subprocess.CompletedProcess(command, 125, "", launch_error)
    (output / "launcher.stdout.log").write_text(process.stdout or "", encoding="utf-8")
    (output / "launcher.stderr.log").write_text(process.stderr or "", encoding="utf-8")
    launcher_finished = utc_now()

    lower_result_path = lower_output / "result.json"
    lower_result: dict[str, Any] | None = None
    result_error: str | None = launch_error
    if result_error is None:
        try:
            lower_result = read_object(lower_result_path)
        except (OSError, UnicodeError, json.JSONDecodeError, RuntimeError) as exc:
            result_error = f"{type(exc).__name__}: {exc}"
    classification, classification_reason = classify_launcher(
        process=process,
        lower_result=lower_result,
        result_error=result_error,
    )

    broker_before = lower_output / "broker_before.json"
    broker_after = lower_output / "broker_after.json"
    broker_evidence: dict[str, Any] = {
        "schema_version": "deepcode-agentloop-broker-evidence-v1",
        "case_id": case_id,
        "before_path": str(broker_before),
        "after_path": str(broker_after),
        "before_present": broker_before.is_file(),
        "after_present": broker_after.is_file(),
        "delta": lower_result.get("broker_delta") if lower_result else None,
        "model_protocol": lower_result.get("model_protocol") if lower_result else None,
    }
    try:
        before_value = read_object(broker_before)
        after_value = read_object(broker_after)
        broker_evidence["before"] = before_value
        broker_evidence["after"] = after_value
        broker_evidence["stats_valid"] = (
            before_value.get("schema_version") == "deepcode-agentloop-broker-stats-v1"
            and after_value.get("schema_version") == "deepcode-agentloop-broker-stats-v1"
        )
    except (OSError, UnicodeError, json.JSONDecodeError, RuntimeError) as exc:
        broker_evidence["stats_valid"] = False
        broker_evidence["error"] = f"{type(exc).__name__}: {exc}"
    write_json(output / "broker_evidence.json", broker_evidence)

    digest_after = tree_digest(candidate)
    read_only_after, writable_after = is_tree_read_only(candidate)
    candidate_runtime_digest = tree_digest(candidate_runtime)
    lower_sandbox = ((lower_result or {}).get("runtime") or {}).get("sandbox")
    credential = (lower_result or {}).get("credential_isolation")
    isolation = {
        "schema_version": "deepcode-agentloop-isolation-evidence-v1",
        "case_id": case_id,
        "canonical_hidden_root": str(canonical_root),
        "canonical_case": str(hidden_case),
        "canonical_case_digest": tree_digest(hidden_case),
        "candidate_runtime": str(candidate_runtime),
        "candidate_runtime_is_disposable_copy": candidate_runtime != candidate,
        "frozen_candidate": str(candidate),
        "frozen_tree_read_only_before": manifest.get("frozen_tree_read_only") is True,
        "frozen_tree_read_only_after": read_only_after,
        "writable_frozen_paths_after": writable_after,
        "frozen_digest_before": digest_before,
        "frozen_digest_after": digest_after,
        "frozen_digest_stable": digest_before == expected_digest == digest_after,
        "evaluator_private_state": str(evaluator_state),
        "evaluator_state_outside_candidate": disjoint(evaluator_state, candidate_runtime),
        "canonical_case_outside_candidate": disjoint(hidden_case, candidate_runtime),
        "canonical_test_cases_mounted_into_candidate": False,
        "evaluator_source_mounted_into_candidate": False,
        "lower_sandbox": lower_sandbox,
        "credential_isolation": credential,
        "candidate_credential": "broker-only-placeholder",
        "real_credential_exposed": False,
    }
    write_json(output / "isolation_evidence.json", isolation)
    write_json(output / "launcher.json", {
        "schema_version": "deepcode-agentloop-launcher-evidence-v1",
        "case_id": case_id,
        "argv": command,
        "started_at": launcher_started,
        "finished_at": launcher_finished,
        "exit_code": process.returncode,
        "timed_out": timed_out,
        "launch_error": launch_error,
        "stdout": str(output / "launcher.stdout.log"),
        "stderr": str(output / "launcher.stderr.log"),
        "lower_result": str(lower_result_path),
    })

    artifact = workspace / "agent_result.json"
    artifact_evidence: dict[str, Any] = {
        "schema_version": "deepcode-agentloop-terminal-artifact-evidence-v1",
        "case_id": case_id,
        "path": str(artifact),
        "present": artifact.is_file(),
        "contract_valid": bool((lower_result or {}).get("contract_valid")),
    }
    if artifact.is_file():
        import hashlib
        artifact_evidence["sha256"] = hashlib.sha256(artifact.read_bytes()).hexdigest()
        artifact_evidence["size_bytes"] = artifact.stat().st_size
    write_json(output / "terminal_artifact_evidence.json", artifact_evidence)

    evidence_paths = {
        "launcher": str(output / "launcher.json"),
        "launcher_stdout": str(output / "launcher.stdout.log"),
        "launcher_stderr": str(output / "launcher.stderr.log"),
        "lower_result": str(lower_result_path),
        "lower_stdout": str(lower_output / "stdout.jsonl"),
        "lower_stderr": str(lower_output / "stderr.log"),
        "broker": str(output / "broker_evidence.json"),
        "isolation": str(output / "isolation_evidence.json"),
        "terminal_artifact": str(output / "terminal_artifact_evidence.json"),
        "agent_result": str(artifact),
        "result": str(output / "result.json"),
        "case_attestation": str(output / "case-attestation.json"),
    }
    artifact_provenance = {
        "artifact_path": str(artifact),
        "artifact_owner": "lower_agent_product",
        "producer_entry": "DeepCode lower-agent product process",
        "evaluator_capture": "terminal_artifact_evidence.json",
        "evaluator_synthesized": False,
        "exists": artifact.is_file(),
        "size_bytes": artifact.stat().st_size if artifact.is_file() else None,
        "sha256": file_sha256(artifact),
    }

    finished_at = utc_now()
    result = {
        "schema_version": CASE_SCHEMA,
        "case_id": case_id,
        "classification": classification,
        "classification_reason": classification_reason,
        "started_at": started_at,
        "finished_at": finished_at,
        "duration_seconds": round(time.monotonic() - started_monotonic, 6),
        "freeze_manifest": str(frozen_manifest.resolve()),
        "source_submission": manifest.get("source_submission"),
        "frozen_digest": expected_digest,
        "frozen_digest_before": digest_before,
        "frozen_digest_after": digest_after,
        "frozen_digest_stable": digest_before == expected_digest == digest_after,
        "candidate_runtime_digest_after": candidate_runtime_digest,
        "canonical_case_digest": isolation["canonical_case_digest"],
        "hidden_started_after_freeze": started_dt >= frozen_at,
        "dynamic_state": "evaluator-private-not-candidate-visible",
        "runtime_projection": runtime_projection,
        "task_input": str(task_input),
        "state_id": state["operation_id"],
        "lower_agent_exit_code": process.returncode,
        "lower_result_valid": bool(lower_result and lower_result.get("contract_valid") is True),
        "broker_delta": lower_result.get("broker_delta") if lower_result else None,
        "terminal_artifact_present": artifact.is_file(),
        "artifact_provenance": artifact_provenance,
        "evidence_manifest": str(output / "evidence-manifest.json"),
        "evidence": {
            "launcher": str(output / "launcher.json"),
            "result": str(lower_result_path),
            "broker": str(output / "broker_evidence.json"),
            "isolation": str(output / "isolation_evidence.json"),
            "terminal_artifact": str(output / "terminal_artifact_evidence.json"),
            "evidence_manifest": str(output / "evidence-manifest.json"),
        },
        "candidate_visibility": {
            "canonical_test_case_tree": False,
            "evaluator_source": False,
            "oracle": False,
            "real_credential": False,
            "placeholder_credential": True,
            "sanitized_runtime_projection": True,
        },
    }
    # Persist the exact dynamic task and the evaluator-private comparison
    # produced for this invocation.  Formal scoring must consume these
    # run-local bytes rather than reconstructing a static sibling oracle.
    comparison_path = output / "private-oracle-comparison.json"
    artifact_value: object = {}
    if artifact.is_file():
        try:
            artifact_value = json.loads(artifact.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            artifact_value = {}
    observed_operation = artifact_value.get("operation_id") if isinstance(artifact_value, dict) else None
    semantic = observe_semantics(case_id, project=hidden_case / "assets/project", workspace=workspace, home=deepcode_home)
    from durable_observer import observe_durable
    durable = observe_durable(case_id, repository=candidate_runtime, workspace=workspace,
        python_executable=runtime_python, output=output / 'evaluator-private' / 'durable-observer', deadline=deadline)
    semantic['actual_world_seed_facts'] = world_facts
    semantic['unsafe_initial_stage_executed'] = (workspace / '.deepcode/unsafe-stage-executed').exists()
    semantic['independent_durable_mechanism_observation'] = durable
    semantic['independent_observer_is_lower_agent_evidence'] = False
    semantic['independent_observer_may_supply_agent_artifact'] = False

    comparison = {
        **semantic,
        "schema_version": "deepcode-run-local-oracle-comparison-v1",
        "case_id": case_id,
        "oracle_source": str(evaluator_state / "case_state.json"),
        "oracle_source_sha256": file_sha256(evaluator_state / "case_state.json"),
        "executed_task_path": str(task_input),
        "executed_task_sha256": file_sha256(task_input),
        "candidate_visible": False,
        "private_oracle_not_candidate_visible": True,
        "wrong_provenance_receives_no_credit": True,
        "provenance_comparisons": {
            "case_id": {
                "expected": [case_id],
                "observed": [artifact_value.get("case_id")] if isinstance(artifact_value, dict) else [],
                "match": isinstance(artifact_value, dict) and artifact_value.get("case_id") == case_id,
            },
            "operation_id": {
                "expected": [state["operation_id"]],
                "observed": [observed_operation] if observed_operation else [],
                "match": observed_operation == state["operation_id"],
            },
        },
    }
    write_json(comparison_path, comparison)
    result.update({
        "executed_task_path": str(task_input),
        "executed_task_sha256": file_sha256(task_input),
        "private_oracle_comparison_path": str(comparison_path),
        "private_oracle_comparison_sha256": file_sha256(comparison_path),
        "candidate_digest": expected_digest,
        "real_execution": bool(lower_result and lower_result.get("real_execution") is True),
        "execution_attempted": bool(lower_result and lower_result.get("execution_attempted") is True),
        "environment_preflight": (lower_result or {}).get("environment_preflight", {"valid": False}),
        "artifact_validation": (lower_result or {}).get("artifact_validation", {}),
        "infra_valid": (lower_result or {}).get("infra_valid", False),
    })
    if isinstance((lower_result or {}).get("failure_attribution"), dict):
        result["failure_attribution"] = lower_result["failure_attribution"]
    result["evidence"]["executed_task"] = str(task_input)
    result["evidence"]["private_oracle_comparison"] = str(comparison_path)
    if digest_after != expected_digest:
        result["classification"] = "evaluator_infrastructure_error"
        result["classification_reason"] = "immutable frozen digest changed during hidden execution"
    result['execution_record_path'] = str(output / 'result.json')
    result['observed_trajectory_path'] = str(lower_output / 'stdout.jsonl')
    write_json(output / "result.json", result)
    write_json(output / "case-attestation.json", {
        "schema_version": "deepcode-agentloop-hidden-case-attestation-v1",
        "case_id": case_id,
        "source_submission": manifest["source_submission"],
        "frozen_at": manifest["frozen_at"],
        "started_at": started_at,
        "finished_at": finished_at,
        "hidden_started_after_freeze": result["hidden_started_after_freeze"],
        "frozen_digest_stable": result["frozen_digest_stable"],
        "canonical_case_only": True,
        "evidence_files_present": all(
            Path(path).is_file()
            for name, path in result["evidence"].items()
            if name != "evidence_manifest"
        ),
        "artifact_provenance": artifact_provenance,
        "evidence_manifest": str(output / "evidence-manifest.json"),
        "classification": result["classification"],
    })
    evidence_paths["result"] = str(output / "result.json")
    evidence_paths["case_attestation"] = str(output / "case-attestation.json")
    evidence_paths["executed_task"] = str(task_input)
    evidence_paths["private_oracle_comparison"] = str(comparison_path)
    write_evidence_manifest(output, evidence_paths)
    return result


def run(**kwargs):
    """Entire preparation/lower/observer tree shares the one case cgroup."""
    sys.path.insert(0,str(Path(__file__).resolve().parent))
    from owned_resources import run_owned
    output=Path(kwargs['output']).resolve()
    controller=output.with_name(output.name+'-case-controller')
    controller.mkdir(parents=True,exist_ok=False)
    request={k:str(v) if isinstance(v,Path) else v for k,v in kwargs.items()}
    # run_owned arms the scope with RuntimeMaxSec=590 from a LATER instant than
    # this one, so a +590 inner deadline leaves the case driver no time to write
    # its timed-out record before SIGTERM.  Reserve 30 s inside the scope.
    request['case_deadline_monotonic']=time.monotonic()+590-30
    request_file=controller/'request.json';write_json(request_file,request)
    proc,att=run_owned([sys.executable,'-I',str(Path(__file__).resolve()),'--owned-request',str(request_file)],
        cwd=Path('/'),env={'PATH':'/usr/bin:/bin','LANG':'C.UTF-8'},output=controller/'resources',timeout=600)
    (controller/'stdout.log').write_text(proc.stdout or '');(controller/'stderr.log').write_text(proc.stderr or '')
    result_file=controller/'return.json'
    if proc.returncode or not result_file.is_file():
        raise RuntimeError('DeepCode owned hidden execution did not complete; inspect '+str(controller))
    return json.loads(result_file.read_text())


def main(argv: list[str] | None = None) -> int:
    argv=list(sys.argv[1:] if argv is None else argv)
    if argv[:1]==['--owned-request']:
        request_file=Path(argv[1]);kwargs=json.loads(request_file.read_text())
        for key in ['frozen_manifest','hidden_root','output','python_executable','launcher']:
            if kwargs.get(key) is not None:kwargs[key]=Path(kwargs[key])
        result=_run_in_scope(**kwargs)
        write_json(request_file.parent/'return.json',result)
        return 0
    parser = argparse.ArgumentParser()
    parser.add_argument("--freeze-manifest", type=Path, required=True)
    parser.add_argument("--hidden-root", type=Path, required=True)
    parser.add_argument("--case", choices=HIDDEN_CASES, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--broker-endpoint", required=True)
    parser.add_argument("--python", dest="python_executable", type=Path)
    parser.add_argument("--launcher", type=Path)
    args = parser.parse_args(argv)
    result = run(
        frozen_manifest=args.freeze_manifest,
        hidden_root=args.hidden_root,
        case_id=args.case,
        output=args.output,
        broker_endpoint=args.broker_endpoint,
        python_executable=args.python_executable,
        launcher=args.launcher,
    )
    print(json.dumps(result, indent=2, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
