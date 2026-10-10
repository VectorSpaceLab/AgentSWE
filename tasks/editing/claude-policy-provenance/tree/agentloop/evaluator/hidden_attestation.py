#!/usr/bin/env python3
"""Attest real hidden-after-freeze execution evidence without assigning score."""
from __future__ import annotations

import argparse
import json
import time
from pathlib import Path
from typing import Any

try:
    from ..protocol import MODEL, REASONING_EFFORT, read_json, sha256_file, tree_digest, write_json
    from .hidden_executor import CASE_IDS, FREEZE_SCHEMA, _runtime, candidate_digest, validate_freeze
    from .lower_agent_launcher import valid_agent_result
except ImportError:  # direct script execution
    import sys
    sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
    from agentloop.protocol import MODEL, REASONING_EFFORT, read_json, sha256_file, tree_digest, write_json  # type: ignore
    from agentloop.evaluator.hidden_executor import CASE_IDS, FREEZE_SCHEMA, _runtime, candidate_digest, validate_freeze  # type: ignore
    from agentloop.evaluator.lower_agent_launcher import valid_agent_result  # type: ignore


def _scheduled_records(run_dir: Path) -> list[dict[str, Any]]:
    path = run_dir / "controller.stdout"
    if not path.is_file():
        return []
    records: list[dict[str, Any]] = []
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError):
        return records
    for item in value.get("hidden", []) if isinstance(value, dict) and isinstance(value.get("hidden"), list) else []:
        if isinstance(item, dict) and item.get("status") == "scheduled_after_freeze":
            records.append(item)
    return records


def _case_attestation(record: dict[str, Any], output_root: Path) -> dict[str, Any]:
    case_id = record.get("case_id")
    result = dict(record)
    result["scheduled_record_excluded"] = True
    path_value = record.get("trajectory_path")
    trajectory_path = Path(path_value) if isinstance(path_value, str) else output_root / str(case_id) / "trajectory.json"
    if not trajectory_path.is_file():
        result["real_execution_evidence"] = False
        result["evidence_classification"] = record.get("classification", "no_trajectory")
        return result
    try:
        trajectory = read_json(trajectory_path)
    except Exception as exc:  # already classified by executor; retain safe type only
        result["real_execution_evidence"] = False
        result["evidence_classification"] = f"trajectory_unreadable_{type(exc).__name__}"
        return result
    events = trajectory.get("trajectory")
    broker = trajectory.get("broker")
    lower_failure = isinstance(events, list) and any(
        isinstance(item, dict) and item.get("kind") == "lower_agent_failure" for item in events
    )
    infrastructure_failure = next((
        str(item.get("failure_classification")) for item in events
        if isinstance(item, dict) and item.get("failure_classification") in {
            "provider_infrastructure_failure", "broker_infrastructure_failure", "evaluator_infrastructure_failure"
        }
    ), None) if isinstance(events, list) else None
    artifact_path = record.get("result_path")
    artifact = None
    if isinstance(artifact_path, str) and Path(artifact_path).is_file():
        try:
            artifact = read_json(Path(artifact_path))
        except Exception:
            artifact = None
    artifact_metadata = trajectory.get("artifact")
    expected_binding = trajectory.get("binding") if isinstance(trajectory.get("binding"), dict) else None
    artifact_authored = (
        isinstance(artifact, dict)
        and valid_agent_result(artifact, str(case_id), expected_binding=expected_binding)
        and isinstance(artifact_metadata, dict)
        and artifact_metadata.get("origin") == "lower_model_final_response"
        and artifact_metadata.get("evaluator_synthesized") is False
        and artifact_metadata.get("binding_verified") is True
    )
    result["artifact_present"] = artifact is not None
    result["artifact_contract_valid"] = isinstance(artifact, dict) and valid_agent_result(artifact, str(case_id), expected_binding=expected_binding)
    result["artifact_authored_by_lower_model"] = artifact_authored
    result["artifact_bound_to_product_trajectory"] = artifact_authored and artifact.get("binding") == expected_binding
    result["real_execution_evidence"] = (
        isinstance(events, list)
        and trajectory.get("schema_version") == "agentswe-claude-policy-agent-case/v1"
        and not lower_failure
        and artifact_authored
    )
    result["evidence_classification"] = infrastructure_failure or ("lower_agent_failure" if lower_failure else record.get("classification", "real_execution_evidence"))
    result["trajectory_sha256_rechecked"] = sha256_file(trajectory_path)
    if isinstance(broker, dict):
        before = broker.get("before")
        after = broker.get("after")
        b = _runtime(before if isinstance(before, dict) else None)
        a = _runtime(after if isinstance(after, dict) else None)
        result["broker_delta"] = {
            "calls": a["calls"] - b["calls"],
            "failures": a["failures"] - b["failures"],
            "successful_calls": a["successful_calls"] - b["successful_calls"],
            "tokens": a["tokens"] - b["tokens"],
        }
        result["broker_protocol"] = broker.get("required")
    return result


def attest(*, run_dir: Path, hidden_run: Path | None = None,
           output: Path | None = None, case_ids: list[str] | None = None,
           acceptance: bool = False) -> dict[str, Any]:
    selected = list(CASE_IDS) if case_ids is None else list(case_ids)
    if (not selected or len(selected) != len(set(selected))
            or any(item not in CASE_IDS for item in selected)
            or (not acceptance and selected != list(CASE_IDS))):
        raise ValueError("nonempty unique hidden subset requires explicit acceptance mode")
    run_dir = run_dir.resolve()
    hidden_run = (hidden_run or (run_dir / "hidden_after_freeze" / "hidden_run.json")).resolve()
    output = (output or (run_dir / "hidden_after_freeze_attestation.json")).resolve()
    run = read_json(hidden_run)
    freeze_path = Path(str(run["freeze_manifest"])).resolve()
    freeze, candidate, digest_before = validate_freeze(freeze_path)
    digest_after = candidate_digest(candidate)
    if digest_after != digest_before or run.get("candidate_digest_after") != digest_before:
        raise RuntimeError("Candidate digest is not stable across hidden execution")
    freeze_mtime = freeze_path.stat().st_mtime_ns
    run_mtime = hidden_run.stat().st_mtime_ns
    cases_by_id = {item.get("case_id"): item for item in run.get("cases", []) if isinstance(item, dict)}
    if list(cases_by_id) != selected:
        raise RuntimeError("hidden execution inventory does not match requested attestation")
    cases = [_case_attestation(cases_by_id.get(case_id, {
        "case_id": case_id, "status": "missing_from_hidden_run",
        "classification": "protocol_missing_case_record",
    }), hidden_run.parent) for case_id in selected]
    executed = [item for item in cases if item.get("real_execution_evidence")]
    failed = [item for item in cases if item.get("status") == "failed" or item.get("evidence_classification") in {
        "lower_agent_failure", "candidate_agent_failure", "launcher_or_trajectory_failure", "malformed_trajectory_schema",
    }]
    unavailable = [item for item in cases if item.get("classification") == "infra_case_spec_unavailable"]
    infrastructure_invalid = [item for item in cases if item.get("evidence_classification") in {
        "provider_infrastructure_failure", "broker_infrastructure_failure", "evaluator_infrastructure_failure",
        "launcher_or_trajectory_failure", "malformed_trajectory_schema",
    }]
    stats_before = run.get("broker", {}).get("before") if isinstance(run.get("broker"), dict) else None
    stats_after = run.get("broker", {}).get("after") if isinstance(run.get("broker"), dict) else None
    scheduled = _scheduled_records(run_dir)
    if unavailable or infrastructure_invalid:
        classification = "PARTIAL_REAL_EXECUTION_INFRA_GAP"
    elif failed:
        classification = "REAL_EXECUTION_WITH_CASE_FAILURES"
    elif len(executed) == len(selected):
        classification = "REAL_EXECUTION_EVIDENCE_UNSCORED"
    else:
        classification = "PARTIAL_REAL_EXECUTION"
    attestation = {
        "schema_version": "agentswe-claude-hidden-after-freeze-attestation/v1",
        "benchmark": "claude-policy-provenance-agentloop-v1",
        "formal_result_claimed": False,
        "acceptance_not_formal": acceptance,
        "case_inventory": selected,
        "classification": classification,
        "freeze": {
            "manifest_path": str(freeze_path),
            "manifest_sha256": sha256_file(freeze_path),
            "schema_version": freeze.get("schema_version"),
            "candidate_root": str(candidate),
            "candidate_digest": digest_before,
            "candidate_digest_matches_freeze": True,
        },
        "hidden_run": {
            "path": str(hidden_run),
            "sha256": sha256_file(hidden_run),
            "created_after_freeze": run_mtime > freeze_mtime,
            "candidate_digest_bound": run.get("candidate_digest") == digest_before,
        },
        "cases": cases,
        "summary": {
            "inventory_count": len(selected),
            "real_execution_evidence_count": len(executed),
            "failed_count": len(failed),
            "case_spec_unavailable_count": len(unavailable),
            "infrastructure_invalid_count": len(infrastructure_invalid),
            "scheduled_records_found": len(scheduled),
            "scheduled_records_used_as_results": False,
            "result_or_score_claimed": False,
        },
        "broker": {
            "required_model": MODEL,
            "required_reasoning_effort": REASONING_EFFORT,
            "before": stats_before,
            "after": stats_after,
            "before_runtime": _runtime(stats_before if isinstance(stats_before, dict) else None),
            "after_runtime": _runtime(stats_after if isinstance(stats_after, dict) else None),
        },
        "ordering": {
            "freeze_before_hidden": run_mtime > freeze_mtime,
            "candidate_digest_stable_after_hidden": digest_after == digest_before,
            "attested_at_unix": time.time(),
        },
        "isolation": {
            "hidden_case_specs_not_mounted_in_candidate": all(
                item.get("isolation", {}).get("hidden_cases_mounted") is False
                for item in cases
            ),
            "evaluator_source_not_mounted": all(
                item.get("isolation", {}).get("evaluator_source_mounted") is False
                for item in cases
            ),
            "provider_credential_not_mounted": all(
                item.get("isolation", {}).get("provider_credential_mounted") is False
                for item in cases
            ),
        },
        "failure_classifications": [
            {"case_id": item.get("case_id"), "classification": item.get("evidence_classification", item.get("classification"))}
            for item in cases if item.get("status") != "executed" or item.get("evidence_classification") == "lower_agent_failure"
        ],
        "note": "This artifact attests execution and isolation evidence only; it assigns no Result or Code points.",
    }
    write_json(output, attestation)
    return attestation


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run-dir", type=Path, required=True)
    parser.add_argument("--hidden-run", type=Path)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args(argv)
    value = attest(run_dir=args.run_dir, hidden_run=args.hidden_run, output=args.output)
    print(json.dumps(value, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
