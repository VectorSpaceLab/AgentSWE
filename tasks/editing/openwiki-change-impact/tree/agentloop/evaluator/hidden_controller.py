#!/usr/bin/env python3
"""Fail-closed, evaluator-owned, post-freeze executor for six hidden cases."""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
from pathlib import Path
from typing import Any

if __package__ in {None, ""}:
    sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

try:
    from ..protocol import (
        HIDDEN_CASES,
        file_sha256,
        parse_utc_timestamp,
        tree_digest,
        tree_is_read_only,
        utc_now,
        validate_internal_symlinks,
        write_json,
    )
    from .broker import CredentialError, EvaluatorBrokerLifecycle
    from .dynamic_case_service import issue
    from .fixture_service import runtime_case_paths
    from .lower_agent_launcher import read_broker_stats, run_case
except ImportError:
    from agentloop.protocol import (
        HIDDEN_CASES,
        file_sha256,
        parse_utc_timestamp,
        tree_digest,
        tree_is_read_only,
        utc_now,
        validate_internal_symlinks,
        write_json,
    )
    from agentloop.evaluator.broker import CredentialError, EvaluatorBrokerLifecycle
    from agentloop.evaluator.dynamic_case_service import issue
    from agentloop.evaluator.fixture_service import runtime_case_paths
    from agentloop.evaluator.lower_agent_launcher import read_broker_stats, run_case


def _case_root(cases_root: Path, case_id: str) -> Path:
    nested = cases_root / "test_cases"
    if (nested / case_id).is_dir():
        return nested
    if (cases_root / case_id).is_dir():
        return cases_root
    raise FileNotFoundError(f"case root missing for {case_id}: {cases_root}")


def _freeze_seal_path(freeze_manifest: Path) -> Path:
    return freeze_manifest.with_name("freeze_manifest.sha256")


def validate_freeze(manifest: dict[str, Any], run_dir: Path, freeze_manifest: Path | None = None,
                    *, allow_pilot_test_001: bool = False) -> Path:
    # Keep provider-free historical guard fixtures readable while all newly
    # emitted formal freezes use the generalized v2 ledger fields below.
    legacy_manifest = manifest.get("schema_version") == 1 and manifest.get("source_submission") == 2
    if legacy_manifest:
        manifest = dict(manifest)
        manifest.setdefault("accepted_submission_count", 2)
        manifest.setdefault("max_dev_rounds", 10)
        manifest.setdefault("accepted_candidate_digests", [
            manifest.get("candidate_1_digest"), manifest.get("candidate_2_digest")
        ])
        manifest.setdefault("feedback_chain_complete", True)
    required = {
        "schema_version", "source_submission", "source_submission_id",
        "accepted_submission_count", "max_dev_rounds", "accepted_candidate_digests",
        "candidate_digest",
        "repository_digest", "repository_digest_algorithm", "candidate_path",
        "builder_session_id", "feedback_digest", "feedback_consumed", "frozen_at",
        "feedback_chain_complete", "hidden_started_at", "hidden_allowed", "dev_evaluated",
        "immutable_repository", "credential_mounted_to_candidate",
        "hidden_case_inventory",
    }
    missing = sorted(required - set(manifest))
    if missing:
        raise ValueError(f"freeze manifest is incomplete: {', '.join(missing)}")
    if manifest["schema_version"] not in (1, 2):
        raise ValueError("unsupported freeze manifest schema")
    source_submission = manifest["source_submission"]
    accepted_count = manifest["accepted_submission_count"]
    max_dev_rounds = manifest["max_dev_rounds"]
    digests = manifest["accepted_candidate_digests"]
    if not isinstance(source_submission, int) or not 1 <= source_submission <= 10:
        raise ValueError("source submission must be in 1..10")
    if not isinstance(accepted_count, int) or not 1 <= accepted_count <= 10:
        raise ValueError("accepted submission count must be in 1..10")
    if not isinstance(max_dev_rounds, int) or not 1 <= max_dev_rounds <= 10:
        raise ValueError("max_dev_rounds must be in 1..10")
    if accepted_count != len(digests) or source_submission != accepted_count:
        raise ValueError("freeze source submission does not identify the latest accepted submission")
    if len(set(digests)) != len(digests):
        raise ValueError("accepted candidate digests are not distinct")
    if manifest["repository_digest_algorithm"] != "sha256-tree-v1":
        raise ValueError("unsupported repository digest algorithm")
    if manifest["hidden_allowed"] is not True or manifest["dev_evaluated"] is not True:
        raise ValueError("hidden is not enabled by a completed freeze")
    if manifest["feedback_consumed"] is not True or manifest["feedback_chain_complete"] is not True:
        raise ValueError("accepted-submission feedback chain is incomplete")
    if manifest["immutable_repository"] is not True:
        raise ValueError("frozen accepted repository was not sealed immutable")
    if manifest["credential_mounted_to_candidate"] is not False:
        raise ValueError("freeze manifest permits a Candidate credential mount")
    expected_inventory = ("test_001",) if allow_pilot_test_001 else HIDDEN_CASES
    if allow_pilot_test_001 and manifest.get("pilot_not_formal") is not True:
        raise ValueError("single-case acceptance requires an explicit pilot freeze")
    if not allow_pilot_test_001 and manifest.get("pilot_not_formal") is True:
        raise ValueError("pilot freeze cannot authorize the formal hidden suite")
    if manifest["hidden_case_inventory"] != list(expected_inventory):
        raise ValueError("hidden case inventory drift")
    parse_utc_timestamp(manifest["frozen_at"], "frozen_at")
    if manifest.get("hidden_started_at") is not None:
        raise ValueError("immutable freeze manifest must not be mutated with hidden state")
    repository = Path(str(manifest["candidate_path"])).resolve()
    run_root = run_dir.resolve()
    expected_repository = run_root / "frozen_candidate" / "repository"
    if repository != expected_repository:
        raise ValueError("frozen Candidate path is not the evaluator-owned run-local repository")
    if not repository.is_dir():
        raise ValueError(f"frozen Candidate directory is missing: {repository}")
    validate_internal_symlinks(repository)
    if not tree_is_read_only(repository):
        raise ValueError("frozen Candidate repository contains writable entries")
    actual = tree_digest(repository)
    if actual != manifest["candidate_digest"] or actual != manifest["repository_digest"]:
        raise ValueError("frozen Candidate repository digest changed")
    if freeze_manifest is not None:
        freeze_manifest = freeze_manifest.resolve()
        seal = _freeze_seal_path(freeze_manifest)
        try:
            expected_seal = seal.read_text(encoding="ascii").strip()
        except OSError as exc:
            raise ValueError("freeze manifest seal is missing") from exc
        if expected_seal != file_sha256(freeze_manifest):
            raise ValueError("freeze manifest seal mismatch")
        if freeze_manifest.stat().st_mode & 0o222 or seal.stat().st_mode & 0o222:
            raise ValueError("freeze manifest or seal is writable")
    return repository


def _template(case_id: str) -> dict[str, Any]:
    path = Path(__file__).resolve().parent / "case_templates.json"
    values = json.loads(path.read_text(encoding="utf-8"))
    value = values.get(case_id)
    if not isinstance(value, dict):
        raise KeyError(f"missing evaluator template: {case_id}")
    return value


def _claim_hidden_once(run_dir: Path, freeze_manifest: Path, freeze: dict[str, Any],
                       *, expected_cases: tuple[str, ...] = HIDDEN_CASES) -> dict[str, Any]:
    """Atomically consume the hidden gate; even a crashed run cannot replay."""
    if expected_cases != HIDDEN_CASES and not (
            expected_cases == ("test_001",) and freeze.get("pilot_not_formal") is True):
        raise ValueError("hidden gate only permits the formal suite or explicit test_001 pilot")
    if list(expected_cases) != freeze.get("hidden_case_inventory"):
        raise ValueError("hidden gate inventory differs from the sealed freeze")
    gate_path = run_dir / "hidden-once-gate.json"
    started_at = utc_now()
    if parse_utc_timestamp(started_at, "hidden_started_at") <= parse_utc_timestamp(freeze["frozen_at"], "frozen_at"):
        raise ValueError("hidden did not start after freeze")
    gate = {
        "schema_version": "openwiki-hidden-once-gate/v1",
        "state": "claimed",
        "hidden_started_at": started_at,
        "freeze_manifest": str(freeze_manifest),
        "freeze_manifest_sha256": file_sha256(freeze_manifest),
        "repository_digest": freeze["repository_digest"],
        "canonical_inventory": list(expected_cases),
    }
    data = (json.dumps(gate, sort_keys=True, separators=(",", ":"), ensure_ascii=False) + "\n").encode()
    try:
        descriptor = os.open(gate_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o444)
    except FileExistsError as exc:
        raise ValueError("hidden suite gate is already consumed; replay requires a new freeze") from exc
    try:
        remaining = memoryview(data)
        while remaining:
            written = os.write(descriptor, remaining)
            if written <= 0:
                raise OSError("failed to persist hidden gate")
            remaining = remaining[written:]
        os.fsync(descriptor)
    finally:
        os.close(descriptor)
    return {**gate, "path": str(gate_path)}


def hidden_lifecycle_fields(freeze: dict[str, Any], cases: dict[str, Any],
                            gate: dict[str, Any], expected_cases: tuple[str, ...]) -> dict[str, Any]:
    """Derive shared-finalizer claims from the actual per-case observations."""
    starts_valid = list(cases) == list(expected_cases)
    try:
        frozen_at = parse_utc_timestamp(freeze["frozen_at"], "frozen_at")
        hidden_at = parse_utc_timestamp(gate["hidden_started_at"], "hidden_started_at")
        starts_valid = starts_valid and hidden_at > frozen_at and all(
            parse_utc_timestamp(record.get("case_started_at"), "case_started_at") >= hidden_at
            for record in cases.values())
    except (KeyError, TypeError, ValueError):
        starts_valid = False
    digest = freeze["repository_digest"]
    stable = bool(cases) and all(
        record.get("frozen_digest_before") == digest == record.get("frozen_digest_after")
        for record in cases.values())
    return {
        "expected_cases": list(expected_cases), "executed_cases": list(cases),
        "all_cases_started_after_freeze": starts_valid, "frozen_digest_stable": stable,
        "frozen_at": freeze["frozen_at"],
    }


def _evidence_manifest(case_dir: Path, paths: dict[str, str | None]) -> Path:
    files: dict[str, dict[str, Any]] = {}
    for name, raw in paths.items():
        path = Path(raw) if raw else None
        exists = bool(path and path.is_file())
        files[name] = {
            "path": str(path) if path else None,
            "exists": exists,
            "sha256": file_sha256(path) if exists and path else None,
            "size_bytes": path.stat().st_size if exists and path else None,
        }
    destination = case_dir / "evidence-manifest.json"
    write_json(destination, {
        "schema_version": "openwiki-hidden-case-evidence/v1",
        "case_id": case_dir.name,
        "files": files,
    })
    return destination


def _infrastructure_invalid(record: dict[str, Any]) -> bool:
    run = record.get("run", {})
    return bool(record.get("infrastructure_invalid") or (isinstance(run, dict) and run.get("infrastructure_invalid")))


def _execute_cases(
    freeze_manifest: Path,
    output: Path,
    broker_endpoint: str,
    broker_instance_id: str,
    cases_root: Path,
    timeout: int,
    freeze: dict[str, Any],
    repository: Path,
    gate: dict[str, Any],
) -> tuple[dict[str, Any], dict[str, Any]]:
    from agentloop.evaluator.execution_evidence import prepare_evidence
    run_dir = freeze_manifest.parent
    cases: dict[str, Any] = {}
    suite_errors: list[str] = []
    stopped_on_tamper = False
    source_submission = freeze["source_submission"]
    repository_digest_before_suite = tree_digest(repository)

    for case_id in HIDDEN_CASES:
        before_digest = tree_digest(repository)
        record: dict[str, Any] = {
            "case_id": case_id,
            "phase": "hidden_after_freeze",
            "source_submission": source_submission,
            "broker_instance_id": broker_instance_id,
            "frozen_digest_before": before_digest,
            "frozen_digest_after": None,
            "repository_digest_before": before_digest,
            "repository_digest_after": None,
            "runtime_digest_before": None,
            "runtime_digest_after": None,
        }
        case_dir = run_dir / "hidden" / case_id
        if before_digest != freeze["repository_digest"]:
            record.update({
                "classification": "mount_isolation_failure",
                "infrastructure_invalid": True,
                "error": "frozen digest changed before case start",
            })
            suite_errors.append(f"{case_id}: frozen digest changed before start")
            stopped_on_tamper = True
        else:
            try:
                case_setup_started = time.monotonic()
                record["case_started_at"] = utc_now()
                request_root = run_dir / "hidden" / "requests"
                root = _case_root(cases_root, case_id)
                task_file = root / case_id / "input.md"
                task = task_file.read_text(encoding="utf-8") if task_file.is_file() else None
                issued = issue(case_id, _template(case_id), request_root / "evaluator", request_root / "candidate", task)
                runtime = runtime_case_paths(case_id, root, case_dir / "fixture")
                run = run_case(
                    repository,
                    Path(issued["candidate_request"]),
                    case_dir,
                    broker_endpoint,
                    timeout,
                    working_directory=Path(runtime["repository"]),
                    fixture_case_id=case_id,fixture_cases_root=root,fixture_output=case_dir/"fixture",
                    case_setup_started=case_setup_started,
                )
                runtime["change_source_kind"]=run.get("runtime_fixture",{}).get("change_source_kind")
                execution = prepare_evidence(run, case_id=case_id,
                    candidate_digest=freeze['candidate_digest'], repository=repository, output=case_dir,
                    request=issued['candidate_request'], cases_root=root, initial=runtime['repository'])
                record.update(execution)
                record.update({
                    "issued": issued,
                    "runtime": {
                        "repository": str(runtime["repository"]),
                        "manifest": str(runtime["manifest"]),
                        "change_source_kind": runtime["change_source_kind"],
                    },
                    "run": run,
                    "classification": execution.get("classification"),
                    "candidate_classification": run.get("candidate_classification"),
                    "infrastructure_invalid": bool(execution.get("infrastructure_invalid")),
                    "runtime_digest_before": run.get("workspace_digest_before"),
                    "runtime_digest_after": run.get("workspace_digest_after"),
                    "product_digest_before": run.get("product_digest_before"),
                    "product_digest_after": run.get("product_digest_after"),
                })
            except Exception as exc:
                record.update({
                    "classification": "evaluator_failure",
                    "candidate_classification": None,
                    "infrastructure_invalid": True,
                    "error": f"{type(exc).__name__}: {exc}",
                })

        after_digest = tree_digest(repository)
        record["frozen_digest_after"] = after_digest
        record["repository_digest_after"] = after_digest
        if after_digest != freeze["repository_digest"]:
            record.update({
                "classification": "mount_isolation_failure",
                "candidate_classification": record.get("candidate_classification"),
                "infrastructure_invalid": True,
                "error": "frozen digest changed after case execution",
            })
            stopped_on_tamper = True
        if _infrastructure_invalid(record):
            suite_errors.append(f"{case_id}: {record.get('classification', 'infrastructure_failure')}")

        evidence_paths: dict[str, str | None] = {
            "launcher_request": str(case_dir / "launcher_request.json"),
            "launcher_result": str(case_dir / "launcher_result.json"),
            "broker_before": str(case_dir / "broker_before.json"),
            "broker_after": str(case_dir / "broker_after.json"),
            "trajectory": str(case_dir / "trajectory.json"),
            "observed_trajectory": record.get("observed_trajectory_path"),
            "native_process_observation": str(case_dir / "native-process-observation.json"),
            "native_process_trace": str(case_dir / "native-process.trace"),
            "native_broker_observation": str(case_dir / "native-broker-observation.json"),
            "agent_result": record.get("artifact_validation",{}).get("artifact_path",str(case_dir / "workspace" / "agent_result.json")),
            "candidate_request": str(run_dir / "hidden" / "requests" / "candidate" / case_id / "request.json"),
            "executed_task": record.get("executed_task_path"),
            "private_oracle_comparison": record.get("private_oracle_comparison_path"),
        }
        case_attestation_path = case_dir / "hidden-case-attestation.json"
        write_json(case_attestation_path, {
            "schema_version": "openwiki-agentloop-hidden-case-attestation/v1",
            "case_id": case_id,
            "source_submission": source_submission,
            "broker_instance_id": broker_instance_id,
            "classification": record.get("classification"),
            "candidate_classification": record.get("candidate_classification"),
            "infrastructure_invalid": _infrastructure_invalid(record),
            "frozen_digest_before": record.get("frozen_digest_before"),
            "frozen_digest_after": after_digest,
            "repository_digest_before": record.get("repository_digest_before"),
            "repository_digest_after": after_digest,
            "runtime_digest_before": record.get("runtime_digest_before"),
            "runtime_digest_after": record.get("runtime_digest_after"),
            "product_digest_before": record.get("product_digest_before"),
            "product_digest_after": record.get("product_digest_after"),
            "broker_delta": record.get("run", {}).get("broker_delta", {}),
            "evidence": evidence_paths,
        })
        evidence_paths["case_attestation"] = str(case_attestation_path)
        evidence_manifest = _evidence_manifest(case_dir, evidence_paths)
        evidence_paths["evidence_manifest"] = str(evidence_manifest)
        record["evidence_paths"] = evidence_paths
        cases[case_id] = record
        if stopped_on_tamper:
            break

    complete_inventory = list(cases) == list(HIDDEN_CASES)
    repository_digest_after_suite = tree_digest(repository)
    repository_stable = (
        repository_digest_before_suite == freeze["repository_digest"] == repository_digest_after_suite
    )
    # Candidate behavior failures are valid measured outcomes.  Only missing
    # cases or infrastructure-invalid evidence makes the Result axis N/A.
    lifecycle_fields = hidden_lifecycle_fields(freeze, cases, gate, HIDDEN_CASES)
    eligible = (
        complete_inventory
        and repository_stable
        and lifecycle_fields["all_cases_started_after_freeze"]
        and not stopped_on_tamper
        and all(not _infrastructure_invalid(item) for item in cases.values())
    )
    attestation = {
        "schema_version": "openwiki-agentloop-hidden-attestation/v1",
        "hidden_after_freeze": True,
        "source_submission": source_submission,
        "freeze_manifest": str(freeze_manifest),
        "freeze_manifest_sha256": file_sha256(freeze_manifest),
        "hidden_once_gate": gate["path"],
        "hidden_started_at": gate["hidden_started_at"],
        "candidate_digest": freeze["candidate_digest"],
        "repository_digest_before_suite": repository_digest_before_suite,
        "repository_digest_after_suite": repository_digest_after_suite,
        "repository_digest_stable": repository_stable,
        "immutable_repository": tree_is_read_only(repository),
        "inventory": list(HIDDEN_CASES),
        "executed_case_ids": list(cases),
        "executed_case_count": len(cases),
        "complete_inventory": complete_inventory,
        "stopped_on_tamper": stopped_on_tamper,
        "scheduled_records_used": False,
        "broker_instance_id": broker_instance_id,
        "credential_mounted_to_candidate": False,
        "formal_result_eligible": eligible,
        "result_axis_status": "eligible" if eligible else "N/A",
        "suite_errors": suite_errors,
        "cases": cases,
    }
    attestation.update(lifecycle_fields)
    attestation_path = output.parent / "hidden-after-freeze-attestation.json"
    write_json(attestation_path, attestation)
    result = {
        "schema_version": "openwiki-agentloop-hidden-suite/v1",
        "phase": "B",
        "hidden_after_freeze": True,
        "source_submission": source_submission,
        "candidate_digest": freeze["candidate_digest"],
        "repository_digest": repository_digest_after_suite,
        "hidden_started_at": gate["hidden_started_at"],
        "complete_inventory": complete_inventory,
        "formal_result_eligible": eligible,
        "result_axis_status": "eligible" if eligible else "N/A",
        "attestation": str(attestation_path),
        "cases": cases,
        "suite_errors": suite_errors,
    }
    write_json(output, result)
    return result, attestation


def _write_lifecycle_failure(
    output: Path,
    freeze_manifest: Path,
    freeze: dict[str, Any],
    gate: dict[str, Any],
    classification: str,
    exc: Exception,
) -> dict[str, Any]:
    attestation_path = output.parent / "hidden-after-freeze-attestation.json"
    attestation = {
        "schema_version": "openwiki-agentloop-hidden-attestation/v1",
        "hidden_after_freeze": True,
        "source_submission": freeze["source_submission"],
        "freeze_manifest": str(freeze_manifest),
        "freeze_manifest_sha256": file_sha256(freeze_manifest),
        "hidden_once_gate": gate["path"],
        "hidden_started_at": gate["hidden_started_at"],
        "candidate_digest": freeze["candidate_digest"],
        "repository_digest_before_suite": freeze["repository_digest"],
        "repository_digest_after_suite": freeze["repository_digest"],
        "repository_digest_stable": True,
        "inventory": list(HIDDEN_CASES),
        "executed_case_ids": [],
        "executed_case_count": 0,
        "complete_inventory": False,
        "scheduled_records_used": False,
        "credential_mounted_to_candidate": False,
        "formal_result_eligible": False,
        "result_axis_status": "N/A",
        "suite_errors": [f"{classification}: {type(exc).__name__}"],
        "cases": {},
    }
    attestation.update(hidden_lifecycle_fields(freeze, {}, gate, HIDDEN_CASES))
    write_json(attestation_path, attestation)
    result = {
        "schema_version": "openwiki-agentloop-hidden-suite/v1",
        "phase": "B",
        "hidden_after_freeze": True,
        "source_submission": freeze["source_submission"],
        "candidate_digest": freeze["candidate_digest"],
        "repository_digest": freeze["repository_digest"],
        "hidden_started_at": gate["hidden_started_at"],
        "complete_inventory": False,
        "formal_result_eligible": False,
        "result_axis_status": "N/A",
        "classification": classification,
        "infrastructure_invalid": True,
        "attestation": str(attestation_path),
        "cases": {},
        "suite_errors": [f"{classification}: {type(exc).__name__}"],
    }
    write_json(output, result)
    return result


def run_hidden_suite(
    freeze_manifest: Path,
    output: Path,
    credential_file: Path,
    cases_root: Path,
    upstream: str = "https://api.deepseek.com",
    timeout: int = 600,
) -> dict[str, Any]:
    freeze_manifest = freeze_manifest.resolve()
    output = output.resolve()
    run_dir = freeze_manifest.parent
    if output.parent != run_dir or output.name != "hidden-result.json":
        raise ValueError("hidden output must be the evaluator-owned run-local hidden-result.json")
    if output.exists():
        raise ValueError("hidden result already exists; replay requires a new freeze")
    freeze = json.loads(freeze_manifest.read_text(encoding="utf-8"))
    if not isinstance(freeze, dict):
        raise ValueError("freeze manifest must be an object")
    repository = validate_freeze(freeze, run_dir, freeze_manifest)
    gate = _claim_hidden_once(run_dir, freeze_manifest, freeze)
    broker_dir = run_dir / "hidden" / "_broker"
    try:
        with EvaluatorBrokerLifecycle(broker_dir, credential_file, upstream) as broker:
            if not broker.endpoint:
                raise RuntimeError("evaluator broker endpoint unavailable")
            initial_stats = read_broker_stats(broker.endpoint)
            result, attestation = _execute_cases(
                freeze_manifest,
                output,
                broker.endpoint,
                broker.instance_id,
                cases_root.resolve(),
                timeout,
                freeze,
                repository,
                gate,
            )
            try:
                final_stats = read_broker_stats(broker.endpoint)
            except Exception as exc:
                # Preserve all per-case evidence already produced.  A broker
                # stats read failure invalidates the suite, but must not erase
                # the real trajectory and classifications.
                final_stats = None
                attestation["formal_result_eligible"] = False
                attestation["result_axis_status"] = "N/A"
                attestation["suite_errors"].append(
                    f"broker_stats_after_suite: {type(exc).__name__}"
                )
                result["formal_result_eligible"] = False
                result["result_axis_status"] = "N/A"
                result["suite_errors"].append(
                    f"broker_stats_after_suite: {type(exc).__name__}"
                )
            attestation["broker_lifecycle"] = str(broker.lifecycle_path)
            attestation["broker_stats"] = str(broker.stats_path)
            attestation["broker_stats_before_suite"] = initial_stats
            attestation["broker_stats_after_suite"] = final_stats
            attestation["independent_evaluator_owned_broker"] = (
                initial_stats.get("broker_instance_id") == broker.instance_id
                and isinstance(final_stats, dict)
                and final_stats.get("broker_instance_id") == broker.instance_id
                and int(initial_stats.get("calls", -1)) == 0
            )
            if not attestation["independent_evaluator_owned_broker"]:
                attestation["formal_result_eligible"] = False
                attestation["result_axis_status"] = "N/A"
                attestation["suite_errors"].append("broker lifecycle was not fresh and evaluator-owned")
                result["formal_result_eligible"] = False
                result["result_axis_status"] = "N/A"
                result["suite_errors"].append("broker lifecycle was not fresh and evaluator-owned")
            write_json(Path(result["attestation"]), attestation)
            write_json(output, result)
        # __exit__ writes the final stopped lifecycle record.  Do not rewrite
        # the suite evidence with a pre-stop process state.
        result["broker_lifecycle"] = str(broker.lifecycle_path)
        result["broker_stats"] = str(broker.stats_path)
        write_json(output, result)
        return result
    except CredentialError as exc:
        return _write_lifecycle_failure(output, freeze_manifest, freeze, gate, "credential_mount_failure", exc)
    except Exception as exc:
        return _write_lifecycle_failure(output, freeze_manifest, freeze, gate, "broker_failure", exc)


def run(
    freeze_manifest: Path,
    output: Path,
    credential_file: Path,
    cases_root: Path,
    upstream: str = "https://api.openai.com",
    timeout: int = 600,
) -> dict[str, Any]:
    return run_hidden_suite(freeze_manifest, output, credential_file, cases_root, upstream, timeout)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--freeze-manifest", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--credential-file", type=Path, required=True)
    parser.add_argument("--upstream", default=os.environ.get("AGENTSWE_UPSTREAM_BASE_URL", "https://api.deepseek.com"))
    parser.add_argument("--cases", type=Path, required=True)
    parser.add_argument("--timeout", type=int, default=600)
    args = parser.parse_args()
    result = run(
        args.freeze_manifest.resolve(),
        args.output.resolve(),
        args.credential_file.resolve(),
        args.cases.resolve(),
        args.upstream,
        args.timeout,
    )
    print(json.dumps(result, indent=2, ensure_ascii=False))
    return 0 if result.get("complete_inventory") and result.get("formal_result_eligible") is True else 2


if __name__ == "__main__":
    raise SystemExit(main())
