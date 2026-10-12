#!/usr/bin/env python3
"""Shared-judge formal publication for this Edit sibling.

Deterministic code in this module only validates provenance, classifies
infrastructure, and prepares bounded evidence.  It never computes a semantic
Result or Code score.  Formal scores come exclusively from the evaluator-owned
shared Result judge and the Create Code judge.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import subprocess
import sys
import urllib.request
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent))
from execution_contract import (
    classify_candidate_execution, candidate_zero_result_contract,
    infrastructure_reason as execution_infrastructure_reason,
)
from execution_scoring import judge_execution_case

ROOT = Path(globals().get("ROOT_OVERRIDE", Path(__file__).resolve().parents[1])).resolve()
CASES = tuple(f"test_{index:03d}" for index in range(1, 7))
RESULT_JUDGE = Path("@@AGENTSWE_EDITING_CONTROL@@/result_judge.py")
CODE_JUDGE = Path("@@AGENTSWE_EDITING_CONTROL@@/code_judge_runner.py")
# Code axis retired 2026-09-19 (Result-only). The Create code_eval.py judge is no
# longer dispatched; the aggregation records the retirement explicitly.
CODE_AXIS_POLICY = "skipped_by_policy"
CODE_AXIS_POLICY_RECORD = {"id": "edit-code-axis-retired-2026-09-19", "evaluation_state": "skipped_by_policy",
                           "reason": "Result-only evaluation; Code judge not dispatched", "code_score": None}
CODE_MAXIMA = {"interface_lifecycle": 15, "requirement_mechanism_coverage": 20,
               "analysis_evidence_integrity": 15, "safety_privacy_side_effects": 15,
               "recovery_honest_failure": 10, "testability_observability": 10,
               "maintainability_generalization": 10, "resource_discipline": 5}
INFRA_TOKENS = (
    "infrastructure", "provider_failure", "provider_error", "broker_failure",
    "broker_error", "credential", "mount_", "docker_", "evaluator_failure",
    "launcher_infrastructure", "runtime_dependency", "environment_failure",
)
PROVENANCE_KEYS = (
    "case_id", "tenant_id", "generation", "request_id", "operation_id",
    "receipt", "receipt_id", "rollout_id", "app_id", "chat_id", "revision",
    "target_fingerprint", "target_revision", "session_id",
)


def selected_case_ids(argv: list[str] | None = None) -> tuple[str, ...]:
    """An explicit subset is acceptance-only, even when it contains six cases."""
    parser = argparse.ArgumentParser(add_help=False)
    parser.add_argument("--acceptance-cases", nargs="+")
    args, _ = parser.parse_known_args(argv)
    selected = args.acceptance_cases
    if selected is None:
        return CASES
    if len(set(selected)) != len(selected) or any(case not in CASES for case in selected):
        raise ValueError("acceptance cases must be unique members of test_001..test_006")
    return tuple(case for case in CASES if case in selected)


def read_json(path: Path) -> dict[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError(f"JSON object required: {path}")
    return value


def write_json(path: Path, value: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(value, indent=2, ensure_ascii=False, sort_keys=True) + "\n", encoding="utf-8")
    os.replace(temporary, path)


def write_immutable_text(path: Path, text: str) -> None:
    """Prepare evidence once; an idempotent read must never replace old bytes."""
    if path.is_symlink():
        raise ValueError("immutable evaluator evidence must not be a symlink: " + str(path))
    payload = text.encode("utf-8")
    if path.exists():
        if not path.is_file() or path.read_bytes() != payload:
            raise ValueError("immutable evaluator evidence changed; use a new scoring directory: " + str(path))
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    # Exclusive creation also protects a concurrent evaluator's first write.
    with path.open("xb") as handle:
        handle.write(payload)


def write_immutable_json(path: Path, value: object) -> None:
    if path.is_symlink():
        raise ValueError("immutable evaluator evidence must not be a symlink: " + str(path))
    if path.is_file():
        if json.loads(path.read_text(encoding="utf-8")) != value:
            raise ValueError("immutable evaluator JSON changed; use a new scoring directory: " + str(path))
        return  # Preserve original formatting and its already recorded hash.
    write_immutable_text(path, json.dumps(value, indent=2, ensure_ascii=False, sort_keys=True) + "\n")


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        while chunk := handle.read(1024 * 1024):
            digest.update(chunk)
    return digest.hexdigest()


def judge_broker_stats(endpoint: str) -> dict[str, Any]:
    base = endpoint.split("/v1/", 1)[0].rstrip("/")
    request = urllib.request.Request(base + "/stats", headers={"Authorization": "Bearer stats-only-placeholder"})
    with urllib.request.urlopen(request, timeout=10) as response:
        value = json.loads(response.read())
    if not isinstance(value, dict):
        raise ValueError("Result judge broker stats must be an object")
    return value


def broker_runtime_counter(stats: dict[str, Any], field: str) -> int:
    runtime = stats.get("runtime") if isinstance(stats.get("runtime"), dict) else stats
    value = runtime.get(field, 0) if isinstance(runtime, dict) else 0
    return int(value) if isinstance(value, int) and not isinstance(value, bool) else 0


def local(path: Path, run_dir: Path) -> bool:
    try:
        path.resolve().relative_to(run_dir.resolve())
        return True
    except ValueError:
        return False


def nested_values(value: object, key: str) -> list[object]:
    found: list[object] = []
    if isinstance(value, dict):
        for name, item in value.items():
            if name == key:
                found.append(item)
            found.extend(nested_values(item, key))
    elif isinstance(value, list):
        for item in value:
            found.extend(nested_values(item, key))
    return found


def hidden_document(run_dir: Path) -> tuple[Path | None, dict[str, Any]]:
    preferred = (
        run_dir / "hidden-after-freeze-attestation.json",
        run_dir / "hidden" / "hidden-after-freeze-attestation.json",
        run_dir / "lifecycle" / "hidden-after-freeze-attestation.json",
        run_dir / "lifecycle" / "hidden-result.json",
        run_dir / "hidden_result.json",
    )
    for path in preferred:
        if path.is_file():
            try:
                return path, read_json(path)
            except (OSError, ValueError, json.JSONDecodeError):
                continue
    return None, {}


def find_case_record(value: object, case_id: str) -> dict[str, Any] | None:
    if isinstance(value, dict):
        direct = value.get(case_id)
        if isinstance(direct, dict):
            return direct
        if value.get("case_id") == case_id:
            return value
        for item in value.values():
            found = find_case_record(item, case_id)
            if found is not None:
                return found
    elif isinstance(value, list):
        for item in value:
            found = find_case_record(item, case_id)
            if found is not None:
                return found
    return None


def infrastructure_reason(record: dict[str, Any]) -> str | None:
    return execution_infrastructure_reason(record)


def candidate_paths(record: dict[str, Any]) -> list[Path]:
    paths: list[Path] = []
    # Only explicit model-artifact fields are eligible. Trajectories and
    # launcher/native results are evaluator evidence, never artifacts.
    for key in ("artifact_path", "agent_artifact_path", "agent_result_path", "authored_artifact", "agent_authored_artifact_path"):
        for raw in nested_values(record, key):
            if isinstance(raw, str) and raw:
                paths.append(Path(raw))
    return paths


def first_file(paths: list[Path], run_dir: Path) -> Path | None:
    for path in paths:
        resolved = path.resolve()
        if resolved.is_file() and local(resolved, run_dir):
            return resolved
    return None


def provider_usage_errors(usage: dict[str, Any], label: str) -> list[str]:
    """Require auditable positive token usage from one completed judge call."""
    errors: list[str] = []
    for field in ("input_tokens", "output_tokens", "total_tokens"):
        value = usage.get(field)
        if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
            errors.append(f"{label} {field} must be a positive integer")
    attempts = usage.get("transport_attempts")
    if isinstance(attempts, bool) or not isinstance(attempts, int) or attempts < 1:
        errors.append(f"{label} transport_attempts must be a positive integer")
    if all(isinstance(usage.get(field), int) and not isinstance(usage.get(field), bool) for field in ("input_tokens", "output_tokens", "total_tokens")):
        if usage["total_tokens"] < max(usage["input_tokens"], usage["output_tokens"]):
            errors.append(f"{label} total_tokens is inconsistent with input/output usage")
    return errors


def lower_execution_counts(record: dict[str, Any]) -> tuple[int, int]:
    """Extract lower calls from evaluator-owned broker evidence."""
    values: list[dict[str, Any]] = []
    for key in (
        "broker", "broker_delta", "broker_stats_delta", "lower_broker",
        "lower_broker_delta", "provider_usage",
    ):
        for value in nested_values(record, key):
            if isinstance(value, dict):
                values.append(value)
    calls = successful = 0
    for value in values:
        for key in ("calls_delta", "calls", "logical_requests"):
            raw = value.get(key)
            if isinstance(raw, int) and not isinstance(raw, bool):
                calls = max(calls, raw)
        for key in ("successful_calls", "completed_calls", "successful_logical_requests"):
            raw = value.get(key)
            if isinstance(raw, int) and not isinstance(raw, bool):
                successful = max(successful, raw)
    return calls, successful


def validate_model_artifact_provenance(
    record: dict[str, Any], artifact: Path, trajectory: Path | None, case_id: str,
) -> list[str]:
    """Reject a plausible filename that lacks lower-product provenance."""
    errors: list[str] = []
    if record.get("real_execution") is False:
        errors.append("lower execution is not real")
    calls, successful = lower_execution_counts(record)
    if calls <= 0 or successful <= 0:
        errors.append("lower broker has no successful model call")
    provenance = record.get("artifact_provenance") or record.get("artifact_contract")
    if record.get("agent_authored_artifact") is not True and not isinstance(provenance, dict):
        errors.append("model-authored artifact provenance is missing")
    if isinstance(provenance, dict):
        if provenance.get("evaluator_synthesized") is True:
            errors.append("artifact is evaluator-synthesized")
        if provenance.get("source") in {"evaluator", "native", "hidden"}:
            errors.append("artifact source is not lower-product workspace")
        if provenance.get("preexisting_before_launch") is True:
            errors.append("artifact preexisted lower launch")
        if provenance.get("trajectory_artifact_reference") is False:
            errors.append("trajectory does not reference artifact")
        expected = provenance.get("sha256") or provenance.get("artifact_sha256")
        if isinstance(expected, str) and expected and expected != sha256_file(artifact):
            errors.append("artifact provenance digest mismatch")
    try:
        value = read_json(artifact)
    except Exception:
        value = {}
        errors.append("artifact is not valid JSON")
    required_fields = ("schema_version", "case_id", "status", "summary", "artifacts")
    missing_fields = [field for field in required_fields if field not in value]
    if missing_fields:
        errors.append("artifact is missing required fields: " + ", ".join(missing_fields))
    if value.get("case_id") != case_id:
        errors.append("artifact case identity mismatch")
    for field in ("schema_version", "case_id", "status", "summary"):
        if field in value and (not isinstance(value[field], str) or not value[field].strip()):
            errors.append(f"artifact {field} must be a non-empty string")
    if "artifacts" in value and not isinstance(value["artifacts"], dict):
        errors.append("artifact artifacts must be a JSON object")
    if value.get("evaluator_synthesized") is True or value.get("artifact_owner") == "evaluator":
        errors.append("artifact claims evaluator ownership")
    expected_digest = record.get("artifact_sha256") or record.get("agent_artifact_sha256")
    if isinstance(expected_digest, str) and expected_digest and expected_digest != sha256_file(artifact):
        errors.append("recorded artifact digest mismatch")
    if trajectory is None or not trajectory.is_file():
        errors.append("lower trajectory is missing")
    return errors


def case_files(run_dir: Path, case_id: str, record: dict[str, Any], hidden_path: Path) -> dict[str, Path | None]:
    roots = [
        run_dir / "hidden" / case_id,
        run_dir / "hidden" / "cases" / case_id,
        run_dir / "lifecycle" / "hidden" / case_id,
    ]
    artifact_globs = ("workspace/agent_result.json", "agent_result.json")
    trajectory_globs = ("trajectory.json", "trajectory.jsonl", "lower-agent/trajectory.jsonl", "launcher_result.json")
    native_globs = ("result.json", "case_result.json", "hidden-case-attestation.json", "case-attestation.json")
    artifact = first_file(candidate_paths(record), run_dir)
    trajectory: Path | None = None
    native: Path | None = None
    for root in roots:
        if not root.is_dir():
            continue
        if artifact is None:
            artifact = first_file([path for pattern in artifact_globs for path in root.glob(pattern)], run_dir)
        if trajectory is None:
            trajectory = first_file([path for pattern in trajectory_globs for path in root.glob(pattern)], run_dir)
        if native is None:
            native = first_file([path for pattern in native_globs for path in root.glob(pattern)], run_dir)
    # Never substitute evaluator/native/hidden material for a missing
    # model-authored artifact or lower trajectory.  The caller must classify
    # that case as a candidate evidence failure and must not invoke the
    # semantic Result judge with an evaluator-generated stand-in.
    return {"artifact": artifact, "trajectory": trajectory, "native": native}


def oracle_path(case_id: str) -> Path | None:
    for path in (
        ROOT / "evaluator" / "manifests" / f"{case_id}.json",
        ROOT / "evaluator" / "cases" / f"{case_id}.json",
    ):
        if path.is_file():
            return path
    return None


def provenance_summary(case_id: str, record: dict[str, Any], artifact: Path, destination: Path) -> Path:
    oracle_file = oracle_path(case_id)
    if oracle_file is None:
        raise ValueError(f"evaluator-private oracle is missing for {case_id}")
    oracle = read_json(oracle_file)
    try:
        artifact_value: object = json.loads(artifact.read_text(encoding="utf-8"))
    except Exception as exc:
        artifact_value = {"artifact_parse_error": f"{type(exc).__name__}: {exc}"}
    comparisons: dict[str, Any] = {}
    for key in PROVENANCE_KEYS:
        expected = nested_values(oracle, key)
        observed = nested_values(artifact_value, key)
        if key == "case_id" and not expected:
            expected = [case_id]
        if expected or observed:
            comparisons[key] = {
                "expected": expected,
                "observed": observed,
                "match": bool(expected and observed and any(item in expected for item in observed)),
            }
    artifact_flags = {
        "present": artifact.is_file(),
        "sha256": sha256_file(artifact),
        "parseable_json_object": isinstance(artifact_value, dict) and "artifact_parse_error" not in artifact_value,
        "evaluator_synthesized_claimed": bool(
            isinstance(artifact_value, dict)
            and (artifact_value.get("evaluator_synthesized") is True or artifact_value.get("artifact_owner") == "evaluator")
        ),
        "raw_tool_event_only": bool(isinstance(artifact_value, dict) and artifact_value.get("type") in {"tool_event", "tool_result"}),
    }
    write_immutable_json(destination, {
        "schema_version": "agentswe-edit-oracle-comparison-v1",
        "case_id": case_id,
        "oracle_source": str(oracle_file) if oracle_file else None,
        "oracle_source_sha256": sha256_file(oracle_file) if oracle_file else None,
        "candidate_failure_remains_scoreable": True,
        "wrong_provenance_receives_no_credit": True,
        "artifact_integrity": artifact_flags,
        "provenance_comparisons": comparisons,
        "native_classification": record.get("classification"),
        "private_oracle_not_candidate_visible": True,
    })
    return destination


def valid_result_contract(path: Path, case_id: str, expected_digest: str = "") -> tuple[dict[str, Any], list[str]]:
    errors: list[str] = []
    try:
        value = read_json(path)
    except Exception as exc:
        return {}, [f"invalid Result judge contract: {exc}"]
    if value.get("schema_version") == "agentswe-execution-result/v1":
        checks = (
            value.get("case_id") == case_id,
            bool(expected_digest) and value.get("candidate_digest") == expected_digest,
            value.get("classification") == "candidate_zero",
            value.get("contract_valid") is True and value.get("result_score_publishable") is True,
            type(value.get("result_score")) is int and value["result_score"] == 0,
            value.get("judge_invoked") is False and value.get("provider_usage") is None,
            bool(value.get("evidence")),
        )
        if not all(checks):
            return value, ["invalid bound Candidate-zero execution contract"]
        try:
            if any(sha256_file(Path(item["path"])) != item["sha256"] for item in value["evidence"]):
                return value, ["Candidate-zero evidence digest mismatch"]
        except (KeyError, OSError, TypeError):
            return value, ["Candidate-zero evidence unavailable"]
        return value, []
    judge = value.get("judge") if isinstance(value.get("judge"), dict) else {}
    usage = value.get("provider_usage") if isinstance(value.get("provider_usage"), dict) else {}
    usage_errors = provider_usage_errors(usage, "Result judge")
    checks = (
        (value.get("case_id") == case_id, "case_id mismatch"),
        (value.get("contract_valid") is True, "contract_valid is not true"),
        (value.get("result_score_publishable") is True, "Result score is not publishable"),
        (judge.get("model") == "deepseek-flash", "wrong Result judge model"),
        (judge.get("reasoning_effort") == "max", "wrong Result judge effort"),
        (usage.get("logical_requests") == _expected_requests(value),
         "Result judge logical_requests must equal 1, or 2 with a recorded early_stop_resample"),
        (usage.get("completed_responses") == _expected_requests(value),
         "Result judge completed_responses must equal 1, or 2 with a recorded early_stop_resample"),
        (isinstance(value.get("result_score"), int) and not isinstance(value.get("result_score"), bool) and 0 <= value.get("result_score", -1) <= 100, "invalid Result score"),
    )
    errors.extend(message for ok, message in checks if not ok)
    errors.extend(usage_errors)
    return value, errors


def _expected_requests(contract):
    """1, or 2 when the contract records the single early-stop resample."""
    return 2 if isinstance(contract, dict) and contract.get("early_stop_resample") else 1


def broker_reconciliation_reasons(successful_delta, logical_delta, *, judged_cases, expected_successful_calls,
                                  attempted_transport_requests, earlier_reasons=False):
    """The Result judge broker's own counters against what the judged cases account for: successful calls against one
    per freshly judged case plus one per recorded early-stop resample; requests against the cases' transport attempts."""
    reasons = []
    if not earlier_reasons and successful_delta != expected_successful_calls:
        reasons.append(
            f"Result judge broker successful-call delta {successful_delta} does not match judged case count "
            f"{judged_cases} (expected {expected_successful_calls} successful calls with early-stop resamples)")
    if logical_delta != attempted_transport_requests:
        reasons.append(
            f"Result judge broker request delta {logical_delta} does not match transport request count {attempted_transport_requests}")
    return reasons


def judge_call_footprint(contract):
    """(transport requests, successful calls) one freshly judged case adds to the Result judge broker, or None when its
    logical_requests are not the 1, or 2 with a recorded early-stop resample, that valid_result_contract accepts.
    The broker reconciliation in main() sums these; counting only logical_requests == 1 there refused every formal
    run in which one judge answer was resampled."""
    usage = contract.get("provider_usage") if isinstance(contract, dict) else None
    if not isinstance(usage, dict) or usage.get("logical_requests") != _expected_requests(contract):
        return None
    return int(usage.get("transport_attempts") or 0), _expected_requests(contract)


def valid_code_contract(path: Path, expected_digest: str) -> tuple[dict[str, Any], list[str]]:
    errors: list[str] = []
    try:
        value = read_json(path)
    except Exception as exc:
        return {}, [f"invalid Code judge contract: {exc}"]
    if isinstance(value, dict) and value.get("evaluation_state") == CODE_AXIS_POLICY:
        return value, []
    judge = value.get("judge") if isinstance(value.get("judge"), dict) else {}
    usage = value.get("provider_usage") if isinstance(value.get("provider_usage"), dict) else {}
    manifest = value.get("source_manifest") if isinstance(value.get("source_manifest"), dict) else {}
    observed_digest = value.get("candidate_digest") or manifest.get("tree_digest")
    usage_errors = provider_usage_errors(usage, "Code judge")
    checks = (
        (value.get("contract_valid") is True, "Code contract_valid is not true"),
        (value.get("code_score_publishable") is True, "Code score is not publishable"),
        (judge.get("model") == "deepseek-flash", "wrong Code judge model"),
        (judge.get("reasoning_effort") == "max", "wrong Code judge effort"),
        (usage.get("logical_requests") == 1, "Code judge logical_requests must equal 1"),
        (usage.get("completed_responses") == 1, "Code judge completed_responses must equal 1"),
        (not expected_digest or observed_digest == expected_digest, "Code contract frozen digest mismatch"),
        (type(value.get("code_score")) is int and 0 <= value["code_score"] <= 100, "invalid Code score"),
        (value.get("code_score") == value.get("code_raw_score"), "Code final score differs from Create raw-score policy"),
    )
    errors.extend(message for ok, message in checks if not ok)
    errors.extend(usage_errors)
    dimensions = value.get("code_dimensions")
    if not isinstance(dimensions, dict) or set(dimensions) != set(CODE_MAXIMA):
        errors.append("Code contract lacks the eight required dimensions")
    else:
        total = 0
        for name, maximum in CODE_MAXIMA.items():
            row = dimensions[name]
            if (not isinstance(row, dict) or row.get("max") != maximum
                    or type(row.get("score")) is not int or not 0 <= row["score"] <= maximum):
                errors.append("Code dimension arithmetic is invalid: " + name)
            else:
                total += row["score"]
        if total != value.get("code_raw_score"):
            errors.append("Code dimensions do not sum to the raw score")
    return value, errors


def frozen_identity_errors(freeze: dict[str, Any], run_dir: Path) -> list[str]:
    """Recompute actual frozen bytes, including on cached score publication."""
    raw = freeze.get("candidate_path") or freeze.get("repository") or freeze.get("frozen_candidate") or freeze.get("frozen_candidate_path")
    expected = freeze.get("candidate_materialized_digest") or freeze.get("candidate_digest") or freeze.get("repository_digest")
    if not raw or not expected:
        return ["missing frozen source identity"]
    root = Path(raw).resolve()
    if not root.is_dir() or not local(root, run_dir):
        return ["frozen source is missing or outside this run"]
    digest = hashlib.sha256()
    try:
        for path in sorted(root.rglob("*"), key=lambda p: p.relative_to(root).as_posix()):
            rel = path.relative_to(root).as_posix().encode("utf-8")
            if path.is_symlink():
                if not local(path, root):
                    return ["frozen source symlink escapes its tree"]
                kind, payload = b"L", os.readlink(path).encode("utf-8")
            elif path.is_file():
                digest.update(b"F" + len(rel).to_bytes(8, "big") + rel)
                digest.update(path.stat().st_size.to_bytes(8, "big"))
                with path.open("rb") as handle:
                    while chunk := handle.read(1024 * 1024):
                        digest.update(chunk)
                continue
            elif path.is_dir():
                continue
            else:
                kind, payload = b"O", b""
            digest.update(kind + len(rel).to_bytes(8, "big") + rel)
            digest.update(len(payload).to_bytes(8, "big") + payload)
    except OSError as exc:
        return ["frozen source cannot be rehashed: " + type(exc).__name__]
    return [] if digest.hexdigest() == expected else ["actual frozen source digest mismatch"]


# Task adapters may validate a legacy lifecycle digest with their own original
# algorithm. Code still independently verifies the canonical Create byte digest.
_canonical_frozen_identity_errors = frozen_identity_errors


def code_frozen_identity(freeze: dict[str, Any], run_dir: Path) -> dict[str, Any] | None:
    """Task hook for two verified digest algorithms on the same frozen tree."""
    return None


def checked_code_frozen_identity(identity, freeze, run_dir, candidate):
    lifecycle_digest = str(freeze.get('candidate_materialized_digest')
        or freeze.get('candidate_digest') or freeze.get('repository_digest') or '')
    if identity is None:
        return lifecycle_digest
    freeze_path, _ = freeze_document(run_dir)
    if not (isinstance(identity, dict)
            and identity.get('schema_version') == 'agentswe-edit-dual-frozen-identity/v1'
            and identity.get('valid') is True
            and identity.get('lifecycle_candidate_digest') == lifecycle_digest
            and Path(str(identity.get('candidate_path', ''))).resolve() == candidate.resolve()
            and freeze_path is not None
            and identity.get('freeze_sha256') == sha256_file(freeze_path)
            and isinstance(identity.get('algorithms'), dict) and len(identity['algorithms']) >= 2):
        raise ValueError('Code/lifecycle digest bridge is not bound to this frozen tree')
    digest = identity.get('code_candidate_digest')
    if not isinstance(digest, str) or len(digest) != 64:
        raise ValueError('Code digest bridge lacks its canonical digest')
    canonical = {**freeze, 'candidate_path': str(candidate),
        'candidate_materialized_digest': digest, 'candidate_digest': digest,
        'repository_digest': digest}
    errors = _canonical_frozen_identity_errors(canonical, run_dir)
    if errors:
        raise ValueError('; '.join(errors))
    return digest


def freeze_document(run_dir: Path) -> tuple[Path | None, dict[str, Any]]:
    for path in (run_dir / "freeze_manifest.json", run_dir / "lifecycle" / "freeze_manifest.json", run_dir / "controller" / "freeze_manifest.json"):
        if path.is_file():
            return path, read_json(path)
    return None, {}


def hidden_lifecycle_errors(hidden: dict[str, Any], freeze: dict[str, Any], run_dir: Path,
                            selected: tuple[str, ...] = CASES) -> list[str]:
    """Validate the attestation boundary before any semantic judge call."""
    errors: list[str] = []
    expected = hidden.get("expected_cases")
    executed = hidden.get("executed_cases")
    inventory_label = "exactly test_001..test_006" if selected == CASES else "the selected acceptance cases"
    if expected != list(selected):
        errors.append("hidden expected_cases are not " + inventory_label)
    if executed != list(selected):
        errors.append("hidden executed_cases are not " + inventory_label)
    cases = hidden.get("cases")
    if isinstance(cases, list):
        ids = [item.get("case_id") for item in cases if isinstance(item, dict)]
        if ids != list(selected):
            errors.append("hidden case records are not canonical, ordered, and unique")
    # Inventory is proved by exact expected/executed IDs above. A one-case
    # acceptance must not pretend to have a six-case complete_inventory.
    # Real execution/artifact provenance or a fatal Candidate gate is proved
    # separately for each case below, not by a blanket all_cases_real marker.
    for key in ("all_cases_started_after_freeze", "frozen_digest_stable"):
        if hidden.get(key) is not True:
            errors.append(f"hidden attestation does not prove {key}")
    if freeze.get("hidden_allowed") is not True and freeze.get("hidden_only_after_freeze") is not True:
        errors.append("freeze manifest does not explicitly authorize post-freeze hidden execution")
    freeze_digest = freeze.get("candidate_materialized_digest") or freeze.get("candidate_digest")
    if not isinstance(freeze_digest, str) or not freeze_digest:
        errors.append("freeze manifest lacks a frozen Candidate digest")
    for raw in (hidden.get("freeze_manifest"), hidden.get("hidden_attestation")):
        if isinstance(raw, str) and raw:
            try:
                Path(raw).resolve().relative_to(run_dir.resolve())
            except ValueError:
                errors.append("hidden attestation references evidence outside run directory")
    return errors


def public_requirements(destination: Path) -> Path:
    chunks: list[str] = []
    for path in sorted((ROOT / "input").glob("*.md")):
        chunks.append(f"# {path.name}\n\n{path.read_text(encoding='utf-8').strip()}\n")
    if not chunks:
        raise ValueError("Code public requirements are unavailable")
    destination.mkdir(parents=True, exist_ok=True)
    write_immutable_text(destination / "requirements.md", "\n".join(chunks))
    return destination


def prepare_code_evidence_scope(candidate: Path, digest: str, output_dir: Path) -> dict[str, Any] | None:
    """Task-owned hook; default is the complete Create source pack."""
    return None


def code_scope_context_text(scope: dict[str, Any]) -> str:
    return (
        "# Evaluator source-change context (not new requirements)\n\n"
        "The evidence pack covers the Candidate-authored added/modified code listed below, "
        "plus task-relevant baseline context. Full frozen-tree identity is validated separately. "
        "Evaluator-generated metadata and unchanged dependency exclusions are explicitly listed "
        "with their reasons; not every excluded path is an unchanged baseline file. "
        "Generated result artifacts are not evidence of implementation quality. Deleted paths "
        "refer to baseline files absent from the frozen Candidate; do not invent source citations "
        "to those absent files. This context does not add requirements or change rubric weights.\n\n"
        + json.dumps({key: scope.get(key) for key in (
            "scope_basis", "selection_policy", "baseline_expected_digest", "changed_paths",
            "added_paths", "deleted_paths", "dependency_paths", "mandatory_included_paths",
            "mandatory_excluded_paths", "evaluator_generated_metadata_exclusions",
            "unchanged_dependency_pack_exclusions", "unchanged_optional_dependency_exclusions",
        )}, indent=2)
    )


def checked_code_scope(scope: dict[str, Any] | None, candidate: Path, digest: str) -> list[str]:
    if scope is None:
        return []
    if (scope.get("schema_version") != "agentswe-edit-code-scope/v1"
            or scope.get("valid") is not True
            or scope.get("errors", []) != []
            or scope.get("candidate_digest") != digest
            or scope.get("complete_change_coverage") is not True
            or not scope.get("baseline_expected_digest")
            or scope.get("baseline_expected_digest") != scope.get("baseline_observed_digest")):
        raise ValueError("invalid Code scope provenance/coverage contract")
    paths = scope.get("evidence_paths")
    if not isinstance(paths, list) or not paths or len(paths) != len(set(paths)):
        raise ValueError("Code evidence scope must have unique nonempty paths")
    for raw in paths:
        if (not isinstance(raw, str) or not raw or Path(raw).is_absolute()
                or ".." in Path(raw).parts or not (candidate / raw).exists()
                or not local(candidate / raw, candidate)):
            raise ValueError("Code evidence scope has an unavailable/escaping path")
    for field in ("changed_paths", "added_paths", "deleted_paths"):
        if not isinstance(scope.get(field), list):
            raise ValueError("Code scope lacks its explicit change inventory")
    for raw in set(scope["changed_paths"] + scope["added_paths"]) - set(scope["deleted_paths"]):
        if not any(raw == p or Path(p) in Path(raw).parents for p in paths):
            raise ValueError("Code scope omits changed Candidate source: " + raw)
    return paths


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run-dir", type=Path, required=True)
    parser.add_argument("--credential-file", type=Path)
    parser.add_argument("--result-judge-broker-endpoint")
    parser.add_argument("--output", type=Path)
    parser.add_argument("--result-judge", type=Path, default=RESULT_JUDGE)
    parser.add_argument("--code-judge", type=Path, default=CODE_JUDGE)
    parser.add_argument("--acceptance-cases", nargs="+")
    args = parser.parse_args(argv)
    selected = selected_case_ids(argv)
    acceptance = args.acceptance_cases is not None
    # Compatibility flags cannot redirect evaluator-owned judge entries.
    args.result_judge = RESULT_JUDGE
    args.code_judge = CODE_JUDGE
    run_dir = args.run_dir.resolve()
    output = (args.output or run_dir / ("acceptance_aggregation.json" if acceptance else "formal_aggregation.json")).resolve()
    result_reasons: list[str] = []
    code_errors: list[str] = []
    hidden_path, hidden = hidden_document(run_dir)
    freeze_path, freeze = freeze_document(run_dir)
    if hidden_path is None:
        result_reasons.append("missing complete hidden-after-freeze evidence")
    if freeze_path is None:
        result_reasons.append("missing frozen Candidate manifest")
        code_errors.append("missing frozen Candidate manifest")
    elif not local(freeze_path, run_dir):
        result_reasons.append("frozen Candidate manifest is outside the run directory")
        code_errors.append("frozen Candidate manifest is outside the run directory")
    if hidden_path is not None and freeze_path is not None:
        result_reasons.extend(hidden_lifecycle_errors(hidden, freeze, run_dir, selected))
    if freeze_path is not None:
        identity_errors = frozen_identity_errors(freeze, run_dir)
        result_reasons.extend(identity_errors)
        code_errors.extend(identity_errors)
    records = {case_id: find_case_record(hidden, case_id) for case_id in selected}
    if any(record is None for record in records.values()):
        result_reasons.append("hidden inventory does not cover the selected cases")
    if args.credential_file is None or not args.credential_file.is_file():
        code_errors.append("evaluator Code judge credential file is unavailable")
    result_contracts: dict[str, str] = {}
    result_scores: dict[str, int] = {}
    infra_cases: dict[str, str] = {}
    attempted_result_cases = 0
    attempted_transport_requests = 0
    expected_successful_calls = 0  # one per judged case, two for a case with the recorded early-stop resample
    zero_cases: list[str] = []
    judged_cases: list[str] = []
    scoring_root = run_dir / "formal_scoring" / "result_axis"
    if not result_reasons and hidden_path is not None:
        frozen_digest = str(freeze.get("candidate_materialized_digest") or freeze.get("candidate_digest") or "")
        verdicts = {case_id: classify_candidate_execution(records[case_id] or {}, case_id=case_id,
                    candidate_digest=frozen_digest) for case_id in selected}
        semantic_needed = any(verdict["classification"] not in {"candidate_zero", "infrastructure_invalid"}
                              for verdict in verdicts.values())
        result_broker_ready = True
        judge_broker_before = {}
        if semantic_needed:
            try:
                if not args.result_judge_broker_endpoint:
                    raise ValueError("separate evaluator-owned Result judge broker is unavailable")
                judge_broker_before = judge_broker_stats(args.result_judge_broker_endpoint)
            except Exception as exc:
                result_reasons.append(f"Result judge broker stats unavailable before scoring: {type(exc).__name__}: {exc}")
                result_broker_ready = False
        for case_id in selected:
            record = records[case_id] or {}
            infra = infrastructure_reason(record)
            if infra:
                infra_cases[case_id] = infra
                continue
            verdict = verdicts[case_id]
            if verdict["classification"] == "candidate_zero":
                contract_path = scoring_root / case_id / "result_score_contract.json"
                contract = candidate_zero_result_contract(verdict, case_id=case_id, candidate_digest=frozen_digest)
                if contract_path.exists() and read_json(contract_path) != contract:
                    result_reasons.append(f"{case_id}: existing contract differs from Candidate-zero evidence")
                    continue
                write_json(contract_path, contract)
                result_contracts[case_id] = str(contract_path)
                result_scores[case_id] = 0
                zero_cases.append(case_id)
                continue
            if not result_broker_ready:
                continue
            if record.get("case_id") != case_id or record.get("candidate_digest") != frozen_digest:
                result_reasons.append(f"{case_id}: execution is not bound to the frozen Candidate digest")
                continue
            lower_calls, lower_successful = lower_execution_counts(record)
            if lower_calls <= 0 or lower_successful <= 0:
                result_reasons.append(
                    f"{case_id}: candidate behavior failure with no successful lower-model call"
                )
                continue
            try:
                files = case_files(run_dir, case_id, record, hidden_path)
                missing_evidence = [name for name in ("artifact", "trajectory", "native") if files.get(name) is None]
                if missing_evidence:
                    result_reasons.append(
                        f"{case_id}: missing lower-agent evidence: {', '.join(sorted(missing_evidence))}"
                    )
                    continue
                provenance_errors = validate_model_artifact_provenance(
                    record, files["artifact"], files["trajectory"], case_id
                )
                if provenance_errors:
                    result_reasons.extend(
                        f"{case_id}: candidate evidence failure: {error}"
                        for error in provenance_errors
                    )
                    continue
                case_output = scoring_root / case_id
                oracle = provenance_summary(case_id, record, files["artifact"], case_output / "oracle_comparison.json")
                task_rubric = files.get("rubric") or ROOT / "evaluator" / (
                    "agentloop_result_rubric.md" if (ROOT / "evaluator/agentloop_result_rubric.md").is_file()
                    else "result_rubric.md" if (ROOT / "evaluator/result_rubric.md").is_file() else "rubric.md")
                scored_record = dict(record)
                scored_record["artifact_validation"] = {
                    "validated_by": "evaluator", "valid": True, "sha256": sha256_file(files["artifact"])}
                scored_record["broker_delta"] = {"calls": lower_calls, "successful_calls": lower_successful}
                feedback = judge_execution_case(
                    case_input=files.get("case_input") or ROOT / "test_cases" / case_id / "input.md",
                    rubric=task_rubric, artifact=files["artifact"], raw_trajectory=files["trajectory"],
                    native_evidence=files["native"], private_oracle=oracle, execution_record=scored_record,
                    candidate_digest=frozen_digest, case_id=case_id, output=case_output,
                    broker_endpoint=args.result_judge_broker_endpoint,
                    score_cap_contract=files.get("score_cap_contract"))
                contract_path = case_output / "result_score_contract.json"
                result_contracts[case_id] = str(contract_path)
                contract, errors = valid_result_contract(contract_path, case_id)
                if feedback.get("contract_valid") is not True:
                    errors.append(str(feedback.get("reason")))
                footprint = None if feedback.get("cached") else judge_call_footprint(contract)
                if footprint is not None:
                    attempted_result_cases += 1
                    attempted_transport_requests += footprint[0]
                if errors:
                    result_reasons.extend(f"{case_id}: {error}" for error in errors)
                else:
                    result_scores[case_id] = int(contract["result_score"])
                    if not feedback.get("cached"):
                        judged_cases.append(case_id)
                        expected_successful_calls += _expected_requests(contract)
            except Exception as exc:
                result_reasons.append(f"{case_id}: evaluator Result preparation failed: {type(exc).__name__}: {exc}")
        judge_broker_after = {}
        if semantic_needed:
            try:
                judge_broker_after = judge_broker_stats(args.result_judge_broker_endpoint)
            except Exception as exc:
                result_reasons.append(f"Result judge broker stats unavailable after scoring: {type(exc).__name__}: {exc}")
        if judge_broker_before and judge_broker_after:
            after_calls = broker_runtime_counter(judge_broker_after, "calls")
            before_calls = broker_runtime_counter(judge_broker_before, "calls")
            after_failures = broker_runtime_counter(judge_broker_after, "failures")
            before_failures = broker_runtime_counter(judge_broker_before, "failures")
            successful_delta = (
                broker_runtime_counter(judge_broker_after, "successful_calls")
                - broker_runtime_counter(judge_broker_before, "successful_calls")
                if ("successful_calls" in (judge_broker_after.get("runtime") or {})
                    or "successful_calls" in (judge_broker_before.get("runtime") or {}))
                else (after_calls - before_calls) - (after_failures - before_failures)
            )
            logical_delta = after_calls - before_calls
            result_reasons.extend(broker_reconciliation_reasons(
                successful_delta, logical_delta, judged_cases=len(judged_cases),
                expected_successful_calls=expected_successful_calls,
                attempted_transport_requests=attempted_transport_requests, earlier_reasons=bool(result_reasons)))
        write_json(run_dir / "formal_scoring" / "result_axis" / "judge_broker_attestation.json", {
            "schema_version": "agentswe-edit-result-judge-broker-attestation-v1",
            "endpoint": args.result_judge_broker_endpoint,
            "candidate_credential_exposed": False,
            "host_judge_real_credential_exposed": False,
            "placeholder": "broker-only-placeholder",
            "before": judge_broker_before,
            "after": judge_broker_after,
            "successful_call_delta": successful_delta if judge_broker_before and judge_broker_after else None,
            "logical_request_delta": logical_delta if judge_broker_before and judge_broker_after else None,
            "attempted_result_cases": attempted_result_cases,
            "attempted_transport_requests": attempted_transport_requests,
            "expected_successful_calls": expected_successful_calls,
            "cleanup_owner": "formal_one_stop",
        })
    if infra_cases:
        result_reasons.extend(f"{case_id}: infrastructure-invalid/N/A: {reason}" for case_id, reason in infra_cases.items())

    code_contract_path = run_dir / "formal_scoring" / "code_axis" / "code_score_contract.json"
    code_value: dict[str, Any] | str = "N/A"
    bridge = None
    code_skipped = CODE_AXIS_POLICY == "skipped_by_policy"
    if code_skipped:
        code_value = dict(CODE_AXIS_POLICY_RECORD)
    if not code_skipped and freeze_path is not None and not code_errors:
        candidate_raw = freeze.get("candidate_path") or freeze.get("repository") or freeze.get("frozen_candidate") or freeze.get("frozen_candidate_path")
        digest = str(freeze.get("candidate_materialized_digest") or freeze.get("candidate_digest") or freeze.get("repository_digest") or "")
        candidate = Path(str(candidate_raw)).resolve() if candidate_raw else Path()
        if not candidate.is_dir() or not local(candidate, run_dir):
            code_errors.append("frozen Candidate source is missing or outside the run directory")
        if not digest:
            code_errors.append("expected frozen Candidate digest is missing")
        bridge = None
        if not code_errors:
            try:
                bridge = code_frozen_identity(freeze, run_dir)
                digest = checked_code_frozen_identity(bridge, freeze, run_dir, candidate)
                if bridge is not None:
                    write_immutable_json(code_contract_path.parent / 'code_frozen_identity.json', bridge)
            except (OSError, ValueError, TypeError) as exc:
                code_errors.append('Code frozen identity verification failed: ' + str(exc))
        if not code_errors:
            requirements = run_dir / "formal_scoring" / "code_axis" / "public_requirements"
            try:
                requirements = public_requirements(requirements)
                scope = prepare_code_evidence_scope(candidate, digest, code_contract_path.parent)
                evidence_paths = checked_code_scope(scope, candidate, digest)
                if scope is not None:
                    write_immutable_json(code_contract_path.parent / "code_evidence_scope.json", scope)
                    # Public initial-artifact provenance and the exact Candidate
                    # change inventory are context, never new scoring criteria.
                    write_immutable_text(requirements / "evaluator_scope_context.md", code_scope_context_text(scope))
            except (OSError, ValueError, TypeError) as exc:
                code_errors.append("Code evidence scope preparation failed: " + str(exc))
                scope, evidence_paths = None, []
            rubric = ROOT / "evaluator" / "code_rubric.md"
            if not rubric.is_file():
                rubric = ROOT / "agentloop" / "evaluator" / "code_rubric.md"
            command = [
                sys.executable, str(args.code_judge.resolve()),
                "--candidate-source", str(candidate),
                "--public-requirements", str(requirements),
                "--code-rubric", str(rubric),
                "--credential-file", str(args.credential_file.resolve()),
                "--output-dir", str(code_contract_path.parent),
                "--expected-candidate-digest", digest,
                "--timeout", "900",
            ]
            for path in evidence_paths:
                command.extend(["--evidence-path", path])
            intent_path = code_contract_path.parent / "scoring_intent.json"
            identity = None
            if not code_errors:
                try:
                    identity = {"candidate_digest": digest, "freeze_sha256": sha256_file(freeze_path),
                                "requirements_sha256": sha256_file(requirements / "requirements.md"),
                                "scope_context_sha256": sha256_file(requirements / "evaluator_scope_context.md") if scope is not None else None,
                                "rubric_sha256": sha256_file(rubric), "judge_runner_sha256": sha256_file(args.code_judge),
                                "scope_sha256": sha256_file(code_contract_path.parent / "code_evidence_scope.json") if scope is not None else None}
                    if bridge is not None:
                        identity['frozen_identity_sha256'] = sha256_file(code_contract_path.parent / 'code_frozen_identity.json')
                except OSError as exc:
                    code_errors.append("Code input identity unavailable: " + type(exc).__name__)
            if code_errors:
                pass
            elif intent_path.exists():
                if read_json(intent_path) != identity:
                    code_errors.append("Code scoring inputs changed since prior logical request")
                else:
                    code_value, code_errors = valid_code_contract(code_contract_path, digest)
            elif code_contract_path.exists():
                code_errors.append("unbound preexisting Code contract cannot be adopted")
            else:
                intent_path.parent.mkdir(parents=True, exist_ok=True)
                with intent_path.open("x", encoding="utf-8") as handle:
                    json.dump(identity, handle, sort_keys=True)
                completed = subprocess.run(command, text=True, capture_output=True, check=False)
                (code_contract_path.parent / "runner.stdout.log").write_text(completed.stdout)
                (code_contract_path.parent / "runner.stderr.log").write_text(completed.stderr)
                code_value, code_errors = valid_code_contract(code_contract_path, digest)
                if completed.returncode != 0:
                    code_errors.append(f"Create Code judge exited {completed.returncode}")
    if freeze_path is not None:
        final_identity_errors = frozen_identity_errors(freeze, run_dir)
        if bridge is not None:
            try:
                current_bridge = code_frozen_identity(freeze, run_dir)
                checked_code_frozen_identity(current_bridge, freeze, run_dir, candidate)
                if current_bridge != bridge:
                    raise ValueError('Code/lifecycle digest bridge changed during scoring')
            except (OSError, ValueError, TypeError) as exc:
                final_identity_errors.append('final dual identity verification failed: ' + str(exc))
        result_reasons.extend(final_identity_errors)
        code_errors.extend(final_identity_errors)
    result_ok = not result_reasons and set(result_scores) == set(selected)
    code_ok = isinstance(code_value, dict) and not code_errors
    aggregation = {
        "schema_version": "agentswe-edit-formal-aggregation-v2",
        "formal_result_publishable": result_ok and not acceptance,
        "formal_complete": result_ok and code_ok and not acceptance,
        "acceptance_result_publishable": result_ok and acceptance,
        "acceptance_complete": result_ok and code_ok and acceptance,
        "evaluation_mode": "acceptance" if acceptance else "formal",
        "selected_cases": list(selected),
        "result_axis": ({
            "score": round(sum(result_scores.values()) / len(selected), 4),
            "maximum": 100,
            "aggregation": "arithmetic mean of selected Result contracts (semantic judge or evidenced fatal gate)",
            "case_scores": result_scores,
        } if result_ok else "N/A"),
        "result_judge_contracts": result_contracts,
        "code_score_publishable": code_ok and not code_skipped,
        "code_axis": code_value if code_ok else "N/A",
        "code_contract": str(code_contract_path) if code_contract_path.is_file() else None,
        "combined_score": None,
        "infrastructure_invalid_cases": infra_cases,
        "candidate_zero_cases": zero_cases,
        "semantically_judged_cases": [case for case in result_scores if case not in zero_cases],
        "fresh_semantic_judge_cases": judged_cases,
        "heuristic_fallback_used": False,
        "result_judge_broker_attestation": str(run_dir / "formal_scoring" / "result_axis" / "judge_broker_attestation.json") if (run_dir / "formal_scoring" / "result_axis" / "judge_broker_attestation.json").is_file() else None,
        "reasons": [
            *(f"Result: {reason}" for reason in result_reasons),
            *(f"Code: {reason}" for reason in code_errors),
        ],
        "result_reasons": result_reasons,
        "code_reasons": code_errors,
        "source_evidence": {"hidden": str(hidden_path) if hidden_path else None, "freeze": str(freeze_path) if freeze_path else None},
    }
    write_json(output, aggregation)
    print(json.dumps(aggregation, indent=2, ensure_ascii=False))
    return 0 if result_ok and code_ok else 2


if __name__ == "__main__":
    raise SystemExit(main())
