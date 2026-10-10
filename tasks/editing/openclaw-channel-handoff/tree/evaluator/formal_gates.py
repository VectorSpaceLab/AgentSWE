#!/usr/bin/env python3
"""Fail-closed provenance and evidence gates for formal OpenClaw runs."""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from controller.builder_session_controller import marker_paths
from controller.two_round_controller import DEV, HIDDEN, tree_digest, validate_freeze_manifest
from lower_agent.entry_contract import PRODUCTION_ENTRY

MODEL = "deepseek-flash"
BUILDER_MODEL = "deepseek-flash"  # upper Builder (Codex harness) only
LOWER_EFFORT = "high"
BUILDER_EFFORT = "max"
BROKER_KEYS = {"calls", "failures", "successful_calls", "input_tokens", "output_tokens", "total_tokens", "tokens"}


def read_object(path: Path) -> dict[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError(f"expected JSON object: {path}")
    return value


def validate_broker_protocol(value: dict[str, Any] | None, *, effort: str = LOWER_EFFORT) -> list[str]:
    if not isinstance(value, dict):
        return ["broker stats are unavailable"]
    protocol = value.get("protocol")
    errors: list[str] = []
    if not isinstance(protocol, dict):
        return ["broker stats protocol is missing"]
    if protocol.get("model") != MODEL:
        errors.append("broker model is not evaluator-locked to deepseek-flash")
    if protocol.get("reasoning_effort") != effort:
        errors.append(f"broker reasoning_effort is not evaluator-locked to {effort}")
    if not str(protocol.get("transport", "")).startswith("evaluator-owned"):
        errors.append("broker transport is not evaluator-owned")
    return errors


def validate_case_record(record: dict[str, Any], case_id: str) -> list[str]:
    errors: list[str] = []
    if record.get("case_id") != case_id:
        errors.append(f"{case_id}: case_id mismatch")
    if record.get("production_entry") != PRODUCTION_ENTRY:
        errors.append(f"{case_id}: real OpenClaw Gateway entry is not attested")
    if record.get("model") != MODEL or record.get("reasoning_effort") != LOWER_EFFORT:
        errors.append(f"{case_id}: lower model/effort lock mismatch")
    if record.get("credential_mode") != "placeholder-only":
        errors.append(f"{case_id}: Candidate credential mode is not placeholder-only")
    if record.get("candidate_sensitive_environment_keys") != []:
        errors.append(f"{case_id}: sensitive Candidate environment keys were not attested empty")
    if record.get("run_mode") != "formal":
        errors.append(f"{case_id}: record is not marked as formal")
    delta = record.get("broker_stats_delta")
    if not isinstance(delta, dict) or not BROKER_KEYS.issubset(delta):
        errors.append(f"{case_id}: complete broker delta is missing")
    for key in ("frozen_candidate_digest_before", "frozen_candidate_digest_after"):
        value = record.get(key)
        if not isinstance(value, str) or len(value) != 64:
            errors.append(f"{case_id}: {key} is missing")
    if record.get("frozen_candidate_digest_stable") is not True:
        errors.append(f"{case_id}: frozen Candidate digest was not stable")
    if not isinstance(record.get("classification"), str) or not record.get("classification"):
        errors.append(f"{case_id}: classification is missing")
    return errors


def validate_hidden_freeze_binding(run: Path) -> list[str]:
    """Bind hidden outcome and mutable timing state to the untouched freeze."""
    import hashlib
    try:
        freeze_path = run / 'lifecycle/freeze_manifest.json'
        digest = hashlib.sha256(freeze_path.read_bytes()).hexdigest()
        state = read_object(run / 'lifecycle/hidden_execution_state.json')
        suite = read_object(run / 'hidden/hidden-after-freeze-attestation.json')
        freeze = read_object(freeze_path)
        errors = []
        if state.get('freeze_manifest_sha256') != digest or suite.get('freeze_manifest_sha256') != digest:
            errors.append('hidden execution is not bound to immutable freeze bytes')
        if state.get('state') != 'completed':
            errors.append('hidden execution state is incomplete or unknown')
        if suite.get('evidence_kind') != 'formal' or suite.get('case_inventory') != list(HIDDEN):
            errors.append('formal Result requires the complete formal hidden inventory')
        if state.get('candidate_digest') != freeze.get('candidate_digest') or suite.get('candidate_digest') != freeze.get('candidate_digest'):
            errors.append('hidden execution Candidate digest differs from freeze')
        for name in ('hidden_started_at', 'hidden_completed_at'):
            if not state.get(name) or suite.get(name) != state.get(name):
                errors.append('hidden timing state does not bind suite: ' + name)
        if suite.get('freeze_before_hidden') is not True:
            errors.append('hidden did not start after freeze')
        return errors
    except (OSError, ValueError, KeyError, TypeError) as exc:
        return ['hidden freeze binding is unavailable: ' + type(exc).__name__]


def validate_builder_attestation(
    *,
    attestation_path: Path,
    freeze_manifest_path: Path,
    frozen_candidate: Path,
) -> tuple[dict[str, Any], list[str]]:
    errors: list[str] = []
    try:
        attestation = read_object(attestation_path)
    except (OSError, UnicodeDecodeError, json.JSONDecodeError, ValueError) as exc:
        return {}, [f"builder attestation is unreadable: {type(exc).__name__}: {exc}"]
    try:
        manifest = read_object(freeze_manifest_path)
        validate_freeze_manifest(manifest, frozen_candidate)
    except (OSError, UnicodeDecodeError, json.JSONDecodeError, ValueError) as exc:
        errors.append(f"freeze manifest is invalid: {type(exc).__name__}: {exc}")
        return attestation, errors
    expected_true = (
        "single_builder_process",
        "same_session",
        "distinct_candidate_digests",
        "feedback_chain_complete",
        "latest_candidate_frozen",
        "probe_marker_absent",
        "public_evidence_complete",
        "formal_lifecycle_eligible",
    )
    if attestation.get("schema_version") != "openclaw-builder-session-attestation-v2":
        errors.append("unsupported builder attestation schema")
    for key in expected_true:
        if attestation.get(key) is not True:
            errors.append(f"builder attestation gate is false: {key}")
    if attestation.get("builder_model") != BUILDER_MODEL or attestation.get("builder_reasoning_effort") != BUILDER_EFFORT:
        errors.append("Builder model/effort lock mismatch")
    if attestation.get("builder_exit_code") != 0:
        errors.append("Builder process did not exit successfully")
    if attestation.get("n_concurrent") != 1 or not 1 <= int(attestation.get("max_dev_rounds", 0)) <= 10:
        errors.append("Builder concurrency/round configuration is invalid")
    if attestation.get("dev_passed_is_automatic_freeze") is not False:
        errors.append("dev_passed must not automatically freeze")
    submissions = attestation.get("submissions")
    if not isinstance(submissions, list) or not 1 <= len(submissions) <= 10:
        errors.append("Builder attestation must contain 1..10 accepted submissions")
    else:
        if any(not isinstance(item, dict) for item in submissions):
            errors.append("Builder submission records are malformed")
        else:
            if submissions[-1].get("candidate_digest") != manifest.get("candidate_digest"):
                errors.append("latest accepted Candidate does not bind to the freeze manifest")
            if len(submissions) > 1 and attestation.get("feedback_chain_complete") is not True:
                errors.append("feedback chain is incomplete for one or more revisions")
            for number, submission in enumerate(submissions, start=1):
                dev = submission.get("dev_results")
                if not isinstance(dev, dict) or set(dev) != set(DEV):
                    errors.append(f"Candidate {number} public dev evidence is incomplete")
                    continue
                for case_id in DEV:
                    value = dev.get(case_id)
                    if not isinstance(value, dict):
                        errors.append(f"Candidate {number} {case_id} record is malformed")
                    else:
                        errors.extend(f"Candidate {number}: {item}" for item in validate_case_record(value, case_id))
                        if value.get("candidate_runtime_ready") is not True:
                            errors.append(f"Candidate {number} {case_id} runtime was not built")
    # Recompute from evaluator-private records and unmodified native bytes;
    # self-reported booleans in the attestation cannot establish a session.
    try:
        from harbor.native_builder_evidence import verify_native
        run = attestation_path.resolve().parent
        records = json.loads((run / "builder_submissions.json").read_text())
        deliveries = json.loads((run / "builder_feedback_deliveries.json").read_text())
        observations = json.loads((run / "builder_native_observations.json").read_text())
        if records != submissions:
            errors.append("attested submissions differ from evaluator-private records")
        mapped = [{**item, "feedback_path": item["feedback"],
            "build": {"candidate_repo_digest": item["candidate_digest"]}} for item in records]
        proof = verify_native(run, mapped, deliveries, observations)
        if not proof.get("valid"):
            errors.append("native Builder evidence is invalid: " + str(proof.get("errors")))
        if proof != attestation.get("native_evidence"):
            errors.append("native Builder evidence changed after attestation")
        if proof.get("native_thread_id") != attestation.get("builder_session_id"):
            errors.append("attested Builder session differs from actual native thread")
    except (OSError, ValueError, KeyError, TypeError) as exc:
        errors.append("native Builder evidence cannot be verified: " + type(exc).__name__)
    if marker_paths(frozen_candidate):
        errors.append("frozen Candidate contains PROBE_ONLY_CANDIDATE2_MARKER")
    if tree_digest(frozen_candidate) != manifest.get("candidate_digest"):
        errors.append("frozen Candidate digest changed after Builder attestation")
    if Path(str(attestation.get("freeze_manifest", ""))).resolve() != freeze_manifest_path.resolve():
        errors.append("Builder attestation points to a different freeze manifest")
    return attestation, errors


def validate_hidden_files(output: Path, records: dict[str, Any], *, selected_cases: tuple[str, ...] = HIDDEN) -> list[str]:
    errors: list[str] = []
    if not selected_cases or not set(selected_cases).issubset(HIDDEN):
        return ['selected hidden inventory is not canonical']
    if set(records) != set(selected_cases):
        errors.append("hidden result inventory differs from the requested cases")
        return errors
    for case_id in selected_cases:
        value = records.get(case_id)
        if not isinstance(value, dict):
            errors.append(f"{case_id}: hidden record is malformed")
            continue
        errors.extend(validate_case_record(value, case_id))
        if value.get("classification") in {
            "broker_infrastructure_error",
            "launcher_infrastructure_error",
            "infrastructure-invalid",
            "frozen_candidate_mutation",
        }:
            errors.append(f"{case_id}: formal suite contains infrastructure/integrity failure")
        for name in ("trajectory.json", "result.json", "hidden_case_attestation.json", "broker_before_suite_case.json", "broker_after_suite_case.json"):
            if not (output / case_id / name).is_file():
                errors.append(f"{case_id}: missing {name}")
    return errors
