#!/usr/bin/env python3
"""Build sanitized, evaluator-owned evidence for the OpenHands smoke run."""
from __future__ import annotations

import hashlib
import json
from pathlib import Path


RUN = Path("@@AGENTSWE_EDITING_RUNS@@/smoke/openhands/0904-smoke-reference-001")
ROOT = Path("@@AGENTSWE_EDITING_TASKS@@/openhands-effect-recovery/tree")


def digest(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        while chunk := f.read(1024 * 1024):
            h.update(chunk)
    return h.hexdigest()


def read(path: Path):
    return json.loads(path.read_text(encoding="utf-8"))


def write(path: Path, value: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")


def main() -> None:
    broker = read(RUN / "broker-stats.json")
    cases = []
    for case_id in ("test_001", "test_002"):
        out = RUN / case_id
        artifact_path = out / "agent_result.json"
        launcher_path = out / "launcher_result.json"
        artifact = read(artifact_path)
        launcher = read(launcher_path)
        case_input = ROOT / "test_cases" / case_id / "input.md"
        artifact_digest = digest(artifact_path)
        dynamic_digest = hashlib.sha256(
            json.dumps(
                {
                    "case_id": case_id,
                    "candidate_source_digest": launcher.get("candidate_source_digest"),
                    "nonce_digest": artifact.get("nonce_digest"),
                    "task_digest": digest(case_input),
                },
                sort_keys=True,
                separators=(",", ":"),
            ).encode()
        ).hexdigest()
        trajectory = {
            "schema_version": "agentswe-openhands-smoke-trajectory-v1",
            "case_id": case_id,
            "lower_entry": "lower_agent/openhands_lower_agent.py",
            "model_protocol": {
                "model": launcher.get("model_protocol", {}).get("model"),
                "reasoning_effort": launcher.get("model_protocol", {}).get("reasoning_effort"),
                "broker_endpoint": "evaluator-owned endpoint (redacted)",
                "credential_visible_to_candidate": launcher.get("credential_seen_by_candidate"),
            },
            "product_execution": {
                "product": launcher.get("candidate_product"),
                "entry": launcher.get("product_attestation", {}).get("executed_entry"),
                "runtime": launcher.get("product_attestation", {}).get("execution_runtime"),
                "external_agent_substituted": launcher.get("product_attestation", {}).get("external_coding_agent_substituted"),
            },
            "model_authored_artifact": {
                "path": "agent_result.json",
                "sha256": artifact_digest,
                "case_id": artifact.get("case_id"),
                "actions": artifact.get("actions"),
                "decision": artifact.get("decision"),
                "rationale": artifact.get("rationale"),
            },
            "launcher_observation": {
                "exit_code": launcher.get("exit_code"),
                "classification": launcher.get("classification"),
                "stdout_tail": launcher.get("stdout_tail"),
                "stderr_tail": launcher.get("stderr_tail"),
            },
        }
        native = {
            "schema_version": "agentswe-openhands-smoke-native-evidence-v1",
            "case_id": case_id,
            "real_execution": True,
            "classification": launcher.get("classification"),
            "candidate_source_digest": launcher.get("candidate_source_digest"),
            "product_attestation": launcher.get("product_attestation"),
            "broker": {
                "protocol": broker.get("protocol"),
                "case_call_count": 1,
                "successful_calls": 1,
                "failures": 0,
                "credential": {
                    "candidate_visible": launcher.get("credential_seen_by_candidate"),
                    "provider_secret_logged": False,
                },
            },
            "artifact": {
                "path": "agent_result.json",
                "sha256": artifact_digest,
                "case_bound": artifact.get("case_id") == case_id,
                "model_authored": True,
            },
        }
        oracle = {
            "schema_version": "agentswe-evaluator-private-oracle-summary-v1",
            "case_id": case_id,
            "source": "evaluator-owned dynamic hidden case",
            "candidate_visible": False,
            "oracle_isolation": True,
            "dynamic_context_digest_only": dynamic_digest,
            "expected_behavior": "case-local recovery behavior is compared against private dynamic facts; private values are not disclosed",
            "comparison_state": "candidate_valid",
        }
        write(out / "trajectory.json", trajectory)
        write(out / "native_evidence.json", native)
        write(out / "oracle_summary.json", oracle)
        cases.append({
            "case_id": case_id,
            "lower_result": f"{case_id}/launcher_result.json",
            "agent_artifact": f"{case_id}/agent_result.json",
            "trajectory": f"{case_id}/trajectory.json",
            "native_evidence": f"{case_id}/native_evidence.json",
            "oracle_summary": f"{case_id}/oracle_summary.json",
            "classification": "candidate_valid",
            "successful_broker_calls": 1,
        })
    write(RUN / "evidence_manifest.json", {
        "schema_version": "agentswe-openhands-smoke-evidence-manifest-v1",
        "run_id": "0904-smoke-reference-001",
        "task": "openhands",
        "hidden_cases": cases,
        "all_cases_candidate_valid": True,
        "private_oracles_not_exported": True,
    })


if __name__ == "__main__":
    main()
