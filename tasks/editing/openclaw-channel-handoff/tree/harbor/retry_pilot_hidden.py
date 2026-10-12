#!/usr/bin/env python3
"""Retry only a zero-call pilot hidden infrastructure failure.

This recovery entry never starts or resumes a Builder and never creates a new
Candidate.  It is deliberately narrower than the one-stop lifecycle: the
existing run must already contain an eligible pilot freeze, and the
latest hidden attempt must have made zero broker calls.  The retry receives a
fresh evaluator-owned broker and writes to a new evidence directory.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from controller.two_round_controller import make_tree_read_only, tree_digest  # noqa: E402
from evaluator.formal_finalize import score_case  # noqa: E402
from evaluator.hidden_executor import run_suite  # noqa: E402
from harbor.formal_one_stop import (  # noqa: E402
    BUILDER_IMAGE,
    free_port,
    now,
    read_json,
    start_broker,
    write_json,
)
from lower_agent.launcher import read_broker_stats, runtime_stats  # noqa: E402


def next_retry(run_dir: Path) -> tuple[int, Path]:
    index = 1
    while (run_dir / f"pilot_hidden_retry_{index:03d}").exists():
        index += 1
    return index, run_dir / f"pilot_hidden_retry_{index:03d}"


def latest_hidden_attempt(run_dir: Path) -> tuple[Path, dict[str, Any]]:
    attempts = [run_dir / "pilot_hidden"]
    attempts.extend(sorted(run_dir.glob("pilot_hidden_retry_[0-9][0-9][0-9]")))
    for output in reversed(attempts):
        summary = output / "summary.json"
        if summary.is_file():
            return output, read_json(summary)
    raise RuntimeError("no prior pilot hidden attempt exists")


def zero_call_failure(attempt: dict[str, Any]) -> tuple[dict[str, int], dict[str, Any]]:
    attestation_path = Path(str(attempt.get("suite_attestation", "")))
    if not attestation_path.is_file():
        raise RuntimeError("prior hidden suite attestation is missing")
    attestation = read_json(attestation_path)
    broker = attestation.get("broker") if isinstance(attestation.get("broker"), dict) else {}
    delta = broker.get("delta") if isinstance(broker.get("delta"), dict) else {}
    normalized = {
        "calls": int(delta.get("calls", 0) or 0),
        "failures": int(delta.get("failures", 0) or 0),
        "successful_calls": int(delta.get("successful_calls", 0) or 0),
        "client_failures": int(delta.get("client_failures", 0) or 0),
        "provider_failures": int(delta.get("provider_failures", 0) or 0),
        # A deadline-aborted attempt is a Candidate outcome, never a provider outage, so a
        # truncated case must not look retryable. Pre-split deltas fall back to the raw
        # counter and stay retryable exactly as before.
        "unattributed_provider_failures": int(
            delta.get("unattributed_provider_failures", delta.get("provider_failures", 0)) or 0),
    }
    # A recovery attempt may follow either an untouched lower-runtime failure
    # or a broker-only provider outage.  It may never replay after a
    # successful model call or a client/protocol failure: those are already
    # candidate-observable executions and must remain final evidence.
    if normalized["successful_calls"] or normalized["client_failures"]:
        raise RuntimeError(
            "pilot hidden retry is forbidden after successful/client broker activity: "
            + json.dumps(normalized, sort_keys=True)
        )
    if normalized["calls"] and normalized["unattributed_provider_failures"] != normalized["calls"]:
        raise RuntimeError(
            "pilot hidden retry requires every prior broker call to be a provider failure: "
            + json.dumps(normalized, sort_keys=True)
        )
    cases = attempt.get("cases") if isinstance(attempt.get("cases"), dict) else {}
    record = cases.get("test_001") if isinstance(cases.get("test_001"), dict) else {}
    if not record:
        raise RuntimeError("prior hidden attempt has no test_001 record")
    return normalized, record


def recognized_infrastructure_failure(previous_output: Path, record: dict[str, Any]) -> tuple[str, str]:
    gateway_log = Path(str(record.get("gateway_log", previous_output / "test_001" / "gateway.log")))
    gateway_text = gateway_log.read_text(encoding="utf-8", errors="ignore") if gateway_log.is_file() else ""
    evaluator_report_path = Path(str(record.get("evaluator_run_report", "")))
    evaluator_report = read_json(evaluator_report_path) if evaluator_report_path.is_file() else {}
    serialized_report = json.dumps(evaluator_report, ensure_ascii=False)
    delta = record.get("broker_stats_delta") if isinstance(record.get("broker_stats_delta"), dict) else {}
    if (
        record.get("classification") == "broker_infrastructure_error"
        and int(delta.get("calls", 0) or 0) > 0
        and int(delta.get("successful_calls", 0) or 0) == 0
        and int(delta.get("unattributed_provider_failures", delta.get("provider_failures", 0)) or 0) == int(delta.get("calls", 0) or 0)
    ):
        return (
            "provider_upstream_failure",
            "fresh evaluator broker retry after an all-provider-failure hidden attempt",
        )
    if any(
        marker in gateway_text
        for marker in (
            "Control UI assets missing; building them now",
            "Control UI build failed",
        )
    ):
        return (
            "control_ui_autobuild_runtime_failure",
            "gateway.controlUi.enabled=false in evaluator-owned lower runtime config",
        )
    if (
        "embedded run timeout" in gateway_text
        and "attempt aborted before prompt submission" in serialized_report
        and int(delta.get("calls", 0) or 0) == 0
    ):
        return (
            "embedded_agent_timeout_before_broker_completion",
            "plugins.enabled=false and 600-second agent/provider/wait budgets in evaluator-owned lower runtime",
        )
    if (
        "starting channels and sidecars" in gateway_text
        and "gateway ready" not in gateway_text
        and "1006 abnormal closure" in serialized_report
    ):
        return (
            "gateway_sidecar_startup_not_ready",
            "OPENCLAW_SKIP_CHANNELS=1 plus successful-health readiness gate in evaluator-owned lower runtime",
        )
    raise RuntimeError("prior zero-call attempt is not a recognized lower-runtime infrastructure failure")


def validate_recovery(run_dir: Path) -> tuple[dict[str, Any], dict[str, Any], Path, Path, dict[str, Any]]:
    summary = read_json(run_dir / "summary.json")
    if summary.get("pilot") is not True or summary.get("evidence_kind") != "pilot":
        raise RuntimeError("recovery requires an existing pilot lifecycle")
    if summary.get("pilot_evidence_complete") is True:
        raise RuntimeError("pilot hidden evidence is already complete")
    builder = read_json(run_dir / "builder_session_attestation.json")
    if builder.get("pilot_lifecycle_eligible") is not True:
        raise RuntimeError("Builder/freeze pilot lifecycle is not eligible")
    if builder.get("public_case_inventory") != ["dev_001"] or builder.get("hidden_case_inventory") != ["test_001"]:
        raise RuntimeError("pilot case inventories are not dev_001/test_001 only")
    freeze_path = run_dir / "lifecycle" / "freeze_manifest.json"
    freeze = read_json(freeze_path)
    frozen = run_dir / "lifecycle" / "frozen_candidate"
    if freeze.get("candidate_digest") != freeze.get("candidate_2_digest"):
        raise RuntimeError("freeze is not bound to the latest accepted Candidate")
    if tree_digest(frozen) != freeze.get("candidate_digest"):
        raise RuntimeError("frozen Candidate digest changed before recovery")
    runtime_manifest_path = run_dir / "candidate_runtimes" / "candidate_002.json"
    runtime_manifest = read_json(runtime_manifest_path)
    if runtime_manifest.get("candidate_source_digest") != freeze.get("candidate_digest"):
        raise RuntimeError("latest accepted Candidate runtime is not bound to the frozen digest")
    if runtime_manifest.get("source_digest_stable") is not True or runtime_manifest.get("candidate_runtime_ready") is not True:
        raise RuntimeError("latest accepted Candidate runtime is not ready/stable")
    return summary, freeze, freeze_path, runtime_manifest_path, builder


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Retry a zero-call OpenClaw pilot hidden infrastructure failure")
    parser.add_argument("--run-dir", type=Path, required=True)
    parser.add_argument(
        "--credential-file",
        type=Path,
        default=Path("@@AGENTSWE_CREDENTIAL_FILE@@"),
    )
    parser.add_argument(
        "--runtime",
        type=Path,
        default=Path("@@AGENTSWE_ENVS@@/openclaw-channel-handoff-ledger-edit-v1"),
    )
    parser.add_argument("--broker-script", type=Path, default=ROOT / "broker" / "responses_broker.py")
    parser.add_argument("--broker-image", default=BUILDER_IMAGE)
    parser.add_argument("--timeout-seconds", type=int, default=900)
    args = parser.parse_args(argv)

    run_dir = args.run_dir.resolve()
    summary, original_freeze, freeze_path, runtime_manifest_path, _builder = validate_recovery(run_dir)
    previous_output, previous_attempt = latest_hidden_attempt(run_dir)
    previous_delta, previous_record = zero_call_failure(previous_attempt)
    infrastructure_classification, runtime_fix = recognized_infrastructure_failure(previous_output, previous_record)

    retry_index, retry_output = next_retry(run_dir)
    broker_port = free_port()
    endpoint = f"http://127.0.0.1:{broker_port}/v1/responses"
    suffix = hashlib.sha256(f"{run_dir}:{retry_index}".encode()).hexdigest()[:12]
    broker_name = f"openclaw-hidden-retry-{suffix}"
    cidfile = retry_output / "broker.cid"
    retry_started = now()
    recovery_record: dict[str, Any] = {
        "attempt": retry_index,
        "started_at": retry_started,
        "classification": infrastructure_classification,
        "prior_output": str(previous_output),
        "prior_suite_attestation": previous_attempt.get("suite_attestation"),
        "prior_broker_delta": previous_delta,
        "candidate_behavior_evaluated": previous_delta["successful_calls"] > 0,
        "runtime_fix": runtime_fix,
    }

    broker_started = False
    # Recovery is a new evidence run.  Copy the frozen source and manifest
    # into that run so the historical lifecycle remains read-only.
    retry_lifecycle = run_dir / f"pilot_hidden_retry_{retry_index:03d}_lifecycle"
    retry_freeze_path = retry_lifecycle / "freeze_manifest.json"
    retry_frozen_candidate = retry_lifecycle / "frozen_candidate"
    retry_lifecycle.mkdir(parents=True, exist_ok=True)
    shutil.copytree(run_dir / "lifecycle" / "frozen_candidate", retry_frozen_candidate, symlinks=True)
    make_tree_read_only(retry_frozen_candidate)
    retry_freeze = dict(original_freeze)
    retry_freeze["source_freeze_manifest"] = str(freeze_path)
    retry_freeze["source_freeze_manifest_sha256"] = hashlib.sha256(freeze_path.read_bytes()).hexdigest()
    retry_freeze["candidate_digest"] = tree_digest(retry_frozen_candidate)
    retry_freeze["hidden_started_at"] = None
    retry_freeze["hidden_completed_at"] = None
    retry_freeze["hidden_attestation"] = None
    retry_freeze["hidden_infrastructure_retries"] = [recovery_record]
    write_json(retry_freeze_path, retry_freeze)
    try:
        start_broker(
            name=broker_name,
            script=args.broker_script.resolve(),
            credential=args.credential_file.resolve(),
            port=broker_port,
            builder=False,
            image=args.broker_image,
            cidfile=cidfile,
        )
        broker_started = True
        before = read_broker_stats(endpoint)
        initial = runtime_stats(before)
        initial_provider_failures = int((before.get("runtime") or {}).get("provider_failures", 0))
        if initial["calls"] or initial["failures"] or initial["successful_calls"] or initial_provider_failures:
            raise RuntimeError("fresh hidden retry broker did not start with zero activity")
        write_json(run_dir / f"pilot_hidden_retry_{retry_index:03d}_broker_before.json", before)

        hidden = run_suite(
            freeze_manifest_path=retry_freeze_path,
            hidden_root=ROOT / "test_cases",
            output=retry_output,
            broker_endpoint=endpoint,
            runtime=args.runtime.resolve(),
            timeout_seconds=args.timeout_seconds,
            pilot=True,
            case_ids=("test_001",),
            builder_attestation_path=run_dir / "builder_session_attestation.json",
            runtime_product_manifest_path=runtime_manifest_path,
        )
        # run_suite has now committed a complete retry attempt (successful or
        # candidate-failing) to a distinct evidence directory and updated the
        # lifecycle timestamps.  Later summary/measurement errors must not
        # erase that auditable attempt from the canonical freeze manifest.
        after = read_broker_stats(endpoint)
        final_runtime = dict(after.get("runtime") or {})
        frozen_digest_after = tree_digest(retry_frozen_candidate)
        if frozen_digest_after != original_freeze.get("candidate_digest"):
            raise RuntimeError("frozen Candidate digest changed during hidden recovery")

        final_freeze = read_json(retry_freeze_path)
        final_retries = list(final_freeze.get("hidden_infrastructure_retries", []))
        if not final_retries:
            raise RuntimeError("hidden retry audit record was lost")
        final_retries[-1].update(
            {
                "completed_at": now(),
                "retry_output": str(retry_output),
                "retry_suite_attestation": hidden.get("suite_attestation"),
                "fresh_broker_initial_runtime": before.get("runtime", {}),
                "final_broker_runtime": final_runtime,
                "candidate_behavior_evaluated": int(final_runtime.get("calls", 0) or 0) > 0,
                "resolved": hidden.get("pilot_evidence_complete") is True,
            }
        )
        final_freeze["hidden_infrastructure_retries"] = final_retries
        write_json(retry_freeze_path, final_freeze)

        retry_case = (hidden.get("cases") or {}).get("test_001", {})
        retry_summary = {
            "schema_version": "openclaw-pilot-hidden-recovery-v1",
            "source_run": str(run_dir),
            "source_freeze_manifest": str(freeze_path),
            "source_freeze_manifest_sha256": retry_freeze["source_freeze_manifest_sha256"],
            "retry_freeze_manifest": str(retry_freeze_path),
            "retry_freeze_candidate_digest": frozen_digest_after,
            "prior_output": str(previous_output),
            "prior_attempt": previous_attempt,
            "hidden": hidden,
            "hidden_lower_broker": after,
            "fresh_hidden_broker_initial_calls": initial["calls"],
            "fresh_hidden_broker_initial_failures": initial["failures"],
            "fresh_hidden_broker_initial_provider_failures": initial_provider_failures,
            "pilot_evidence_complete": hidden.get("pilot_evidence_complete") is True,
            "pilot_not_formal": True,
            "formal_result_claimed": False,
            "formal_code_score_claimed": False,
            "code_score_claimed": False,
            "formal_result_valid": False,
            "result_axis": "N/A",
            "code_axis": "N/A",
            "hidden_infrastructure_retries": final_retries,
        }
        measurement: dict[str, Any] | str = "N/A"
        if retry_summary["pilot_evidence_complete"] and isinstance(retry_case, dict):
            measurement = score_case(retry_case, retry_output / "test_001")
            measurement.update(
                {
                    "schema_version": "openclaw-agentloop-pilot-measurement-v1",
                    "evidence_kind": "pilot",
                    "formal_result_publishable": False,
                    "code_score_publishable": False,
                }
            )
            write_json(retry_output / "pilot_scoring" / f"test_001_retry_{retry_index:03d}.json", measurement)
        retry_summary["pilot_measurement"] = measurement
        write_json(retry_output / "recovery_summary.json", retry_summary)
        print(json.dumps(retry_summary, indent=2, ensure_ascii=False))
        return 0 if retry_summary["pilot_evidence_complete"] else 2
    except Exception:
        raise
    finally:
        if broker_started:
            container_id = cidfile.read_text(encoding="ascii").strip() if cidfile.is_file() else ""
            if container_id:
                subprocess.run(["docker", "rm", "-f", container_id], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=False)
                inspected = subprocess.run(["docker", "inspect", container_id], text=True, capture_output=True, check=False)
                inspect_text = ((inspected.stdout or "") + (inspected.stderr or "")).strip()
                inspect_lower = inspect_text.lower()
                write_json(retry_output / "cleanup_attestation.json", {
                    "container_id": container_id,
                    "ownership_proven": True,
                    "absent_after_cleanup": inspected.returncode != 0 and ("no such object" in inspect_lower or "no such container" in inspect_lower),
                    "unrelated_containers_touched": False,
                })
            else:
                write_json(retry_output / "cleanup_attestation.json", {
                    "ownership_proven": False,
                    "absent_after_cleanup": False,
                    "cleanup_error": "broker startup attempted without current-run container ID",
                    "unrelated_containers_touched": False,
                })


if __name__ == "__main__":
    raise SystemExit(main())
