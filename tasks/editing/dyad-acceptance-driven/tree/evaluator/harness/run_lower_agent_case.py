#!/usr/bin/env python3
"""Run one evaluator-owned Dyad lower case and classify its evidence.

This wrapper deliberately invokes the product launcher, not a replacement
coding agent. Headless mode executes Dyad's production IPC/services under the
repository harness; native mode is a separately labelled Electron smoke and
is never promoted to behavioral success from an exit code alone.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import subprocess
import sys
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
from evaluator.scenario_contract import (  # noqa: E402
    compare_scenario,
    persist_private_oracle,
    prepare_scenario,
)

MODEL = "deepseek-flash"
EFFORT = "high"
RESULT_SCHEMA_VERSION = "dyad-agentloop-case-result-v2"


def file_sha256(path: Path) -> str | None:
    if not path.is_file():
        return None
    return hashlib.sha256(path.read_bytes()).hexdigest()


def write_evidence_manifest(case_output: Path, case_name: str, paths: dict[str, str | None]) -> Path:
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
    destination = case_output.with_name(case_output.stem + ".evidence-manifest.json")
    destination.write_text(
        json.dumps({
            "schema_version": "dyad-lower-case-evidence/v1",
            "case_id": case_name,
            "files": files,
        }, indent=2, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )
    return destination


def broker_stats(endpoint: str) -> dict[str, Any]:
    import urllib.request
    base = endpoint.split("/v1/", 1)[0].rstrip("/")
    with urllib.request.urlopen(base + "/stats", timeout=5) as response:
        value = json.loads(response.read())
    if not isinstance(value, dict):
        raise ValueError("broker stats must be an object")
    return value


def _d56_read_json(path: Path) -> dict[str, Any] | None:
    """Read one evaluator-written evidence file; never fail the case over it."""
    try:
        loaded = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError, TypeError):
        return None
    return loaded if isinstance(loaded, dict) else None


def case_id(path: Path) -> str:
    return path.parent.name if path.name == "input.md" else path.stem


def delta(before: dict[str, Any], after: dict[str, Any], key: str) -> int:
    before_runtime = before.get("runtime", {}) if isinstance(before.get("runtime"), dict) else {}
    after_runtime = after.get("runtime", {}) if isinstance(after.get("runtime"), dict) else {}
    return int(after_runtime.get(key, 0) or 0) - int(before_runtime.get(key, 0) or 0)


def result_envelope(*, case_name: str, classification: str, failure_class: str | None,
                    broker: dict[str, Any], artifact_present: bool) -> dict[str, Any]:
    return {
        "schema_version": RESULT_SCHEMA_VERSION,
        "case_id": case_name,
        "classification": classification,
        "infra_valid": classification in {"valid", "candidate_failure"},
        "failure_class": failure_class,
        "attribution": {
            "owner": "candidate" if classification == "candidate_failure" else "evaluator/provider",
            "candidate_behavior_evaluable": classification in {"valid", "candidate_failure"},
        },
        "score": None,
        "broker": broker,
        "model_protocol": {
            "model": MODEL,
            "reasoning_effort": EFFORT,
            "transport": "evaluator-owned-responses-broker",
        },
        "artifact_present": artifact_present,
        "static_stage_a": False,
    }


def _valid_app_or_chat_id(value: object) -> bool:
    if isinstance(value, bool):
        return False
    if isinstance(value, int):
        return value > 0
    return isinstance(value, str) and bool(value.strip())


def case_binding_complete(workspace: dict[str, Any]) -> bool:
    if not _valid_app_or_chat_id(workspace.get("app_id")):
        return False
    if not _valid_app_or_chat_id(workspace.get("chat_id")):
        return False
    return all(
        isinstance(workspace.get(key), str) and bool(workspace[key].strip())
        for key in ("run_id", "session_id", "revision", "target_fingerprint")
    )


# --- D13 (2026-09-19) recovered upstream transport failures ---------------------------
# An upstream transport failure the lower agent recovered from -- it issued a new logical
# request and a later one in the same ledger succeeded -- is infrastructure noise, not a
# provider failure for this case. Same allowlist and exclusions as the shared admission
# normalizers (harbor/0905-edit-case-repair/v2_usage_normalizers.py, transport_error);
# duplicated here because the control plane is not importable at run time.
_D13_TRANSPORT_TOKENS = ("brokenpipe", "connectionreset", "connectionaborted", "connectionclosed",
                         "remotedisconnected", "serverdisconnected", "incompleteread",
                         "chunkedencoding", "ssleof", "prematureclose")
_D13_PROVIDER_SIDE_TOKENS = ("readtimeout", "readtimedout", "sockettimeout", "timeouterror",
                             "timedout", "connecttimeout", "connectionerror")
_D13_NON_TRANSPORT_TOKENS = ("credential", "apikey", "unauthor", "forbidden", "invalidrequest",
                             "protocolfailure", "schema", "casedeadline", "deadlineexceeded",
                             "maxoutputtokens", "brokerrestart", "notdispatched", "notsent", "cancel")
_D13_NON_TRANSPORT_TEXT = ("client:", "client_", "client failure", "client error", "clientfailure")


def _d13_has_5xx(text):
    groups = "".join(character if character.isdigit() else " " for character in text).split()
    return any(len(group) == 3 and group[0] == "5" for group in groups)


def _d13_transport_error(error):
    if not isinstance(error, str) or not error.strip():
        return False
    text = error.lower()
    squeezed = "".join(character for character in text if character.isalnum())
    if any(token in squeezed for token in _D13_NON_TRANSPORT_TOKENS):
        return False
    if any(token in text for token in _D13_NON_TRANSPORT_TEXT):
        return False
    if any(token in squeezed for token in _D13_TRANSPORT_TOKENS):
        return True
    provider_side = any(marker in squeezed for marker in ("provider", "upstream", "http"))
    if provider_side and any(token in squeezed for token in _D13_PROVIDER_SIDE_TOKENS):
        return True
    return bool(provider_side and _d13_has_5xx(text))


def _d13_rows(stats):
    """Ledger rows of either family: intent rows (`requests`) or attempts (`attempts`)."""
    if not isinstance(stats, dict):
        return []
    for key in ("requests", "attempts"):
        rows = stats.get(key)
        if isinstance(rows, list):
            return [row for row in rows if isinstance(row, dict)]
    return []


def _d13_row_ok(row):
    if "model_response_available" in row or "usage_unknown" in row:
        return (row.get("state") == "terminal" and row.get("model_response_available") is True
                and row.get("usage_unknown") is False)
    return (row.get("ok") is True and row.get("usage_state") == "known"
            and row.get("upstream_completion") == "completed")


def _d13_row_error(row):
    if row.get("failure_kind") == "client" or row.get("provider_outcome") == "not_dispatched":
        return None
    for key in ("error", "transport_abort_reason", "error_type", "failure_reason"):
        value = row.get(key)
        if isinstance(value, str) and value:
            return value
    return None


def _d13_row_attempts(row):
    for key in ("transport_attempts", "upstream_attempts"):
        if key in row:
            return row.get(key)
    return None


def _d13_identity(row):
    return row.get("request_sha256") or row.get("request_id")


def _d13_recovered_transport_calls(before, after):
    """Tolerated rows added between the snapshots; before=None counts the whole ledger."""
    rows = _d13_rows(after)
    seen = {_d13_identity(row) for row in _d13_rows(before)} if before is not None else set()
    count = 0
    for index, row in enumerate(rows):
        if _d13_identity(row) in seen or _d13_row_ok(row):
            continue
        if not _d13_transport_error(_d13_row_error(row)) or _d13_row_attempts(row) != 1:
            continue
        if any(_d13_row_ok(later) for later in rows[index + 1:]):
            count += 1
    return count
# --- end D13 --------------------------------------------------------------------------


# --- D56 (2026-09-21) the product entered and then wrote no native evidence -----------
# Every mount/launch/preflight blocker in this tree writes native evidence of its own
# (environment/headless_chat_flow.py:582-609 write_failure -> real_product False plus a
# setup_error; the git-runtime blocker also writes a launch record with exit_code 125),
# and classify() already attributes those at `real_product is not True`.  So "no native
# evidence file at all" cannot mean the product never entered: it means the evaluator's
# own case deadline, or a product exit, ended the scenario driver before its afterAll
# (environment/scenario_chat_flow.test.ts:869 -> :1107 writeEvidence) ran.
# D14/D23: a product that ran and produced no result is a Candidate outcome.
CASE_DEADLINE_ELAPSED_TOLERANCE_SECONDS = 30.0
# Covers headless_chat_flow.py:29 EVIDENCE_PERSIST_RESERVE_SECONDS (15 s) plus
# adapters/lower_agent_launcher.py:41 LAUNCHER_TAIL_RESERVE_SECONDS (5 s), the two
# reserves that make an exhausted case stop short of its own recorded budget.


def _d56_inner_record(launch_record):
    if not isinstance(launch_record, dict):
        return None
    inner = launch_record.get("headless_runtime_record")
    return inner if isinstance(inner, dict) else None


def _d56_product_entered(launch_record):
    """Evaluator-owned proof that the sandbox was built and the product entry ran.

    Read only from the launcher's own record, never from Candidate bytes.
    """
    if not isinstance(launch_record, dict) or launch_record.get("executed") is not True:
        return False
    inner = _d56_inner_record(launch_record)
    if not inner or not inner.get("command"):
        return False
    # 125 is this tree's own pre-launch blocker exit (headless_chat_flow.py:695).
    if inner.get("exit_code") == 125:
        return False
    return inner.get("network_namespace") == "isolated"


def _d56_deadline_exit(launch_record, case_resources):
    """Did an evaluator-owned deadline end this case?"""
    inner = _d56_inner_record(launch_record)
    if inner and (inner.get("timed_out") is True or inner.get("exit_code") == 124):
        return True
    if isinstance(launch_record, dict) and (launch_record.get("timed_out") is True
                                            or launch_record.get("exit_code") == 124):
        return True
    if isinstance(case_resources, dict):
        if case_resources.get("timed_out") is True:
            return True
        elapsed, budget = case_resources.get("elapsed_seconds"), case_resources.get("timeout_seconds")
        if (isinstance(elapsed, (int, float)) and not isinstance(elapsed, bool)
                and isinstance(budget, (int, float)) and not isinstance(budget, bool)
                and budget > 0 and elapsed >= budget - CASE_DEADLINE_ELAPSED_TOLERANCE_SECONDS):
            return True
    return False
# --- end D56 --------------------------------------------------------------------------


def classify(*, mode: str, launcher_exit: int, artifact: dict[str, Any] | None,
             native_evidence: dict[str, Any] | None = None,
             oracle_comparison: dict[str, Any] | None = None,
             launch_record: dict[str, Any] | None = None,
             case_resources: dict[str, Any] | None = None,
             before: dict[str, Any], after: dict[str, Any]) -> tuple[str, str | None]:
    if after.get("protocol", {}).get("model") != MODEL or after.get("protocol", {}).get("reasoning_effort") != EFFORT:
        return "infrastructure-invalid", "broker_protocol_mismatch"
    calls = delta(before, after, "calls")
    successful = delta(before, after, "successful_calls")
    provider_failures = delta(before, after, "provider_failures")
    broker_failures = delta(before, after, "broker_failures")
    failures = delta(before, after, "failures")
    client_failures = delta(before, after, "client_failures")
    if mode == "native":
        return "infrastructure-invalid", "native_smoke_has_no_driven_chat_evidence"
    unknown = delta(before, after, "unknown_usage_calls")
    if launcher_exit == 124 and calls > 0 and successful == calls - failures and failures <= 1 and unknown == failures:
        # The evaluator's own case deadline cut the Candidate's last in-flight request after it had
        # spent its budget on successful calls: the Candidate exhausted the case budget. That is
        # Candidate behavior, not provider or evaluator infrastructure.
        return "candidate_failure", "case_budget_exhausted"
    # Same verdict when the broker's own case-deadline timer wins the race against the
    # launcher's 124 branch: the lower process dies on the 502 and exits 1 a moment before
    # subprocess.run would have killed it, so the exit code alone cannot see the exhausted
    # budget.  0921-fx-001 dyad test_004/005/006 each spent 484.8-485.2 s of a 494 s budget
    # and ended on one deadline-killed in-flight request; they were voided as provider_error.
    deadline_failures = delta(before, after, "deadline_failures")
    if (deadline_failures > 0 and calls > 0 and successful == calls - failures
            and failures == deadline_failures and unknown == failures):
        return "candidate_failure", "case_budget_exhausted"
    # D13: an upstream transport failure this case recovered from -- a later request in the
    # same ledger succeeded -- is infrastructure noise, not this case's provider failure.
    recovered = _d13_recovered_transport_calls(before, after)
    if provider_failures > recovered:
        return "infrastructure-invalid", "provider_error"
    if broker_failures > 0 or failures > recovered or client_failures > 0:
        return "infrastructure-invalid", "broker_error"
    if not native_evidence:
        if launcher_exit == 124 and calls == 0:
            return "infrastructure-invalid", "timeout_before_product_entry"
        # D56: mount/launch/preflight blockers write their own native evidence, so a
        # launch record that shows the product entry executed, plus successful lower
        # calls this case made itself, is proof the Candidate ran.  It then delivered
        # no result, which is the Candidate's outcome and not infrastructure.
        if _d56_product_entered(launch_record) and calls > 0 and successful > 0:
            if launcher_exit == 124 or _d56_deadline_exit(launch_record, case_resources):
                return "candidate_failure", "case_budget_exhausted"
            return "candidate_failure", "product_exit_without_result"
        return "infrastructure-invalid", "candidate_mount_or_launcher_failure"
    if native_evidence.get("real_product") is not True:
        return "infrastructure-invalid", "headless_product_entry_failed"
    if calls == 0:
        # The scenario catches everything into setup_error and the surviving
        # `it` asserts nothing on the ordinary path, so a scenario that died
        # during the evaluator's own preparation still arrives here as exit 0
        # with no requests sent. Blaming the product for that is wrong, and it
        # is what four of four hidden runs reported. Re-attributed only while
        # the phase says the product had not been driven yet; evidence written
        # before setup_phase existed keeps its previous classification.
        setup_error = native_evidence.get("setup_error")
        if (isinstance(setup_error, str) and setup_error.strip()
                and native_evidence.get("setup_phase") == "evaluator-preparation"):
            return "infrastructure-invalid", "scenario_setup_failed"
        return "candidate_failure", "agent_no_model_call"
    if successful == 0:
        return "candidate_failure", "model_response_not_successful"
    if native_evidence.get("acceptance_surface_observed") is not True:
        return "candidate_failure", "acceptance_surface_missing"
    if not artifact:
        return "candidate_failure", "model_authored_artifact_missing"
    if artifact.get("schema_version") != "dyad-lower-agent-artifact-v3":
        return "candidate_failure", "model_authored_artifact_invalid"
    if native_evidence.get("action_protocol_complete") is not True:
        return "candidate_failure", "model_action_trajectory_incomplete"
    if native_evidence.get("generic_fallback_used") is not False:
        return "candidate_failure", "generic_single_flow_fallback_detected"
    if native_evidence.get("agent_artifact_origin") != "model_finish_action":
        return "candidate_failure", "model_authored_artifact_invalid"
    workspace = native_evidence.get("workspace") if isinstance(native_evidence.get("workspace"), dict) else {}
    artifact_workspace = artifact.get("workspace") if isinstance(artifact.get("workspace"), dict) else {}
    if not case_binding_complete(workspace) or artifact_workspace != workspace:
        return "candidate_failure", "acceptance_case_binding_incomplete"
    if artifact.get("task_sha256") != native_evidence.get("executed_task_sha256"):
        return "candidate_failure", "executed_task_identity_mismatch"
    if artifact.get("case_id") != native_evidence.get("case_id") or artifact.get("scenario_id") != native_evidence.get("scenario_id"):
        return "candidate_failure", "scenario_identity_mismatch"
    if not oracle_comparison:
        return "infrastructure-invalid", "independent_oracle_comparison_missing"
    recheck = native_evidence.get("independent_behavior_recheck")
    if isinstance(recheck, dict):
        native = recheck.get("native_behavior", {})
        if recheck.get("infrastructure_invalid") or native.get("infrastructure_invalid"):
            return "infrastructure-invalid", "independent_browser_runtime_failure"
    # Correctly attributed incomplete or failed task behavior is quality
    # evidence for the independent rubric, never a Python-assigned hard zero.
    return "valid", None


def _main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--repository", type=Path, required=True)
    parser.add_argument("--case", type=Path, required=True)
    parser.add_argument("--case-id")
    parser.add_argument("--broker-endpoint", required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--mode", choices=("headless", "native"), default="headless")
    parser.add_argument("--execute", action="store_true", help="legacy alias for --mode native")
    parser.add_argument("--case-deadline-monotonic", type=float, required=True)
    args = parser.parse_args()
    output = args.output.resolve(); output.parent.mkdir(parents=True, exist_ok=True)
    case_name = args.case_id or case_id(args.case)
    try:
        before = broker_stats(args.broker_endpoint)
        before_path = output.with_name(output.stem + ".broker_before.json")
        before_path.write_text(json.dumps(before, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    except Exception as exc:
        result = result_envelope(
            case_name=case_name,
            classification="infrastructure-invalid",
            failure_class="broker_unavailable",
            broker={"before": {}, "after": {}},
            artifact_present=False,
        )
        result.update({
            "product_entry": "not entered: evaluator-owned broker unavailable",
            "broker_error": f"{type(exc).__name__}: {exc}",
        })
        output.write_text(json.dumps(result, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
        print(json.dumps(result, sort_keys=True, ensure_ascii=False))
        return 0
    scenario_root = output.with_name(output.stem + ".scenario")
    try:
        prepared = prepare_scenario(case_name, args.case.resolve(), scenario_root)
    except Exception as exc:
        result = result_envelope(
            case_name=case_name,
            classification="infrastructure-invalid",
            failure_class="scenario_dispatch_invalid",
            broker={"before": before, "after": before},
            artifact_present=False,
        )
        result["scenario_error"] = f"{type(exc).__name__}: {exc}"
        output.write_text(json.dumps(result, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
        print(json.dumps(result, sort_keys=True, ensure_ascii=False))
        return 0
    launcher = ROOT / "adapters" / "lower_agent_launcher.py"
    mode = "native" if args.execute else args.mode
    launch_path = output.with_suffix(".launch.json")
    artifact_path = output.with_suffix(".launch.artifact.json")
    native_evidence_path = output.with_suffix(".launch.native-evidence.json")
    command = [
        "python3", str(launcher), "--repository", str(args.repository.resolve()),
        "--case", prepared["executed_task_path"], "--case-id", case_name,
        "--broker-endpoint", args.broker_endpoint, "--mode", mode,
        "--output", str(launch_path), "--artifact", str(artifact_path),
        "--native-evidence", str(native_evidence_path),
        "--scenario-public", prepared["public_fixture_path"],
        "--case-deadline-monotonic", str(args.case_deadline_monotonic),
    ]
    private_oracle_path = Path(prepared["private_oracle_path"])
    if private_oracle_path.exists() or private_oracle_path.is_symlink():
        raise RuntimeError("private oracle exists before lower subprocess launch")
    try:
        completed = subprocess.run(command, text=True, capture_output=True, check=False, timeout=max(.001, args.case_deadline_monotonic - __import__("time").monotonic()))
    except subprocess.TimeoutExpired as exc:
        completed = subprocess.CompletedProcess(command, 124, exc.stdout or "", exc.stderr or "timeout")
    finally:
        # Persist only after the lower process has returned or timed out.  The
        # Candidate process never shares a filesystem namespace containing the
        # current run's private oracle file.
        private_oracle_path = persist_private_oracle(prepared)
    try:
        after = broker_stats(args.broker_endpoint)
        after_path = output.with_name(output.stem + ".broker_after.json")
        after_path.write_text(json.dumps(after, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
        broker_error = None
    except Exception as exc:
        after = before
        broker_error = f"{type(exc).__name__}: {exc}"
    artifact = None
    if artifact_path.is_file():
        try:
            loaded = json.loads(artifact_path.read_text(encoding="utf-8"))
            artifact = loaded if isinstance(loaded, dict) else None
        except json.JSONDecodeError:
            artifact = None
    native_evidence = None
    if native_evidence_path.is_file():
        try:
            loaded = json.loads(native_evidence_path.read_text(encoding="utf-8"))
            native_evidence = loaded if isinstance(loaded, dict) else None
        except json.JSONDecodeError:
            native_evidence = None
    oracle_comparison_path = output.with_name(output.stem + ".private-oracle-comparison.json")
    oracle_comparison = compare_scenario(
        private_oracle_path,
        native_evidence_path,
        artifact_path if artifact_path.is_file() else None,
        oracle_comparison_path,
    )
    trajectory_path = artifact_path.with_suffix(".trajectory.json")
    artifact_provenance = {
        "artifact_path": str(artifact_path),
        "artifact_owner": "model_via_dyad_typed_chat",
        "producer_entry": "model finish action captured from Dyad persisted typed chat",
        "evaluator_capture": str(launch_path),
        "evaluator_synthesized": False,
        "exists": artifact_path.is_file(),
        "size_bytes": artifact_path.stat().st_size if artifact_path.is_file() else None,
        "sha256": file_sha256(artifact_path),
        "executed_task_sha256": prepared["executed_task_sha256"],
        "native_evidence_sha256": file_sha256(native_evidence_path),
    }
    call_delta = delta(before, after, "calls")
    successful_delta = delta(before, after, "successful_calls")
    provider_failure_delta = delta(before, after, "provider_failures")
    classification, failure_class = ("infrastructure-invalid", "broker_unavailable") if broker_error else classify(
        mode=mode, launcher_exit=completed.returncode, artifact=artifact,
        native_evidence=native_evidence, oracle_comparison=oracle_comparison,
        launch_record=_d56_read_json(launch_path),
        case_resources=_d56_read_json(
            launch_path.with_name(launch_path.stem + "-resources") / "resource-attestation.json"),
        before=before, after=after,
    )
    if broker_error:
        failure_class = "broker_unavailable"
    result = result_envelope(
        case_name=case_name,
        classification=classification,
        failure_class=failure_class,
        broker={"before": before, "after": after},
        artifact_present=artifact is not None,
    )
    result.update({
        "execution_mode": mode,
        "product_entry": "real typed chat:stream -> SQLite -> Git -> Acceptance IPC" if mode == "headless" else "npm start -> scripts/start-supervisor.mjs -> Electron Forge",
        "launcher_exit_code": completed.returncode,
        "artifact_sha256": file_sha256(artifact_path),
        "artifact_path": str(artifact_path),
        "artifact_provenance": artifact_provenance,
        "native_evidence_path": str(native_evidence_path),
        "native_evidence_present": native_evidence is not None,
        "native_evidence_sha256": file_sha256(native_evidence_path),
        "trajectory_path": str(trajectory_path),
        "trajectory_present": trajectory_path.is_file(),
        "broker_calls_delta": call_delta,
        "broker_successful_calls_delta": successful_delta,
        "broker_provider_failures_delta": provider_failure_delta,
        "product_entry_observed": bool(native_evidence and native_evidence.get("real_product") is True),
        "acceptance_surface_observed": bool(native_evidence and native_evidence.get("acceptance_surface_observed") is True),
        "acceptance_chain_complete": bool(oracle_comparison.get("passed") is True),
        "acceptance_case_binding_complete": bool(native_evidence and case_binding_complete(native_evidence.get("workspace", {}))),
        "original_task_path": prepared["original_task_path"],
        "original_task_sha256": prepared["original_task_sha256"],
        "executed_task_path": prepared["executed_task_path"],
        "executed_task_sha256": prepared["executed_task_sha256"],
        "scenario_id": prepared["scenario_id"],
        "scenario_axis": prepared["axis"],
        "scenario_public_fixture_path": prepared["public_fixture_path"],
        "scenario_public_fixture_sha256": prepared["public_fixture_sha256"],
        "private_oracle_comparison_path": str(oracle_comparison_path),
        "private_oracle_comparison_sha256": file_sha256(oracle_comparison_path),
        "private_oracle_comparison": oracle_comparison,
        "artifact": artifact,
        "broker_error": broker_error,
        "stdout_tail": completed.stdout[-2000:],
        "stderr_tail": completed.stderr[-2000:],
    })
    result["evidence_manifest"] = str(output.with_name(output.stem + ".evidence-manifest.json"))
    output.write_text(json.dumps(result, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    write_evidence_manifest(output, case_name, {
        "launcher_result": str(launch_path),
        "artifact": str(artifact_path),
        "trajectory": str(trajectory_path),
        "native_evidence": str(native_evidence_path),
        "executed_task": prepared["executed_task_path"],
        "scenario_public_fixture": prepared["public_fixture_path"],
        "private_oracle": prepared["private_oracle_path"],
        "private_oracle_comparison": str(oracle_comparison_path),
        "broker_before": str(output.with_name(output.stem + ".broker_before.json")),
        "broker_after": str(output.with_name(output.stem + ".broker_after.json")),
        "case_result": str(output),
    })
    print(json.dumps(result, sort_keys=True))
    return 0


def main() -> int:
    if "--owned-case" in sys.argv:
        sys.argv.remove("--owned-case")
        return _main()
    parser = argparse.ArgumentParser(add_help=False)
    parser.add_argument("--output", type=Path, required=True)
    args, _ = parser.parse_known_args()
    sys.path.insert(0, str(ROOT / "environment"))
    from owned_resources import run_owned
    # Case deadline leaves 100 s inside the 600 s owned scope for the lower process to
    # be reaped and the result/oracle/broker evidence to be persisted; 590 s left ~0 s and
    # the scope killed the runner before result.json existed (0919-ds-001: hidden_result_unreadable).
    deadline = __import__("time").monotonic() + 500
    proc, resources = run_owned([sys.executable, "-I", str(Path(__file__).resolve()), "--owned-case",
        *sys.argv[1:], "--case-deadline-monotonic", str(deadline)], cwd=Path("/"),
        env={"PATH": "/usr/bin:/bin", "LANG": "C.UTF-8"},
        output=args.output.resolve().with_name(args.output.stem + ".case-resources"), timeout=600)
    print(proc.stdout or "", end="")
    if proc.stderr: print(proc.stderr, file=sys.stderr, end="")
    return proc.returncode


if __name__ == "__main__":
    raise SystemExit(main())
