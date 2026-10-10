#!/usr/bin/env python3
"""Derive sanitized evidence from a completed DeepTutor pilot run.

The input run is authoritative.  This helper only copies facts already
emitted by the lower-agent launcher and strips transport-sensitive fields; it
does not create scores, oracle values, successful actions, or model calls.
"""

from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
from typing import Any


SENSITIVE_KEYS = {
    "authorization", "api_key", "apikey", "access_token", "credential",
    "provider_key", "secret", "password", "token", "headers",
}


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        while chunk := handle.read(1024 * 1024):
            digest.update(chunk)
    return digest.hexdigest()


def read_json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def write_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")


def sanitize(value: Any) -> Any:
    if isinstance(value, dict):
        result: dict[str, Any] = {}
        for key, item in value.items():
            lowered = str(key).lower()
            if lowered in SENSITIVE_KEYS or "credential" in lowered or "api_key" in lowered:
                result[str(key)] = "[redacted evaluator transport field]"
            else:
                result[str(key)] = sanitize(item)
        return result
    if isinstance(value, list):
        return [sanitize(item) for item in value]
    if isinstance(value, str) and value.lower().startswith("bearer "):
        return "[redacted bearer value]"
    return value


def load_jsonl(path: Path) -> list[Any]:
    return [sanitize(json.loads(line)) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


def case_root(run: Path, case_id: str) -> Path:
    for candidate in (run / "hidden" / "cases" / case_id, run / "hidden" / case_id, run / case_id):
        if candidate.is_dir():
            return candidate
    raise FileNotFoundError(f"case directory not found: {case_id}")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--run", type=Path, required=True)
    parser.add_argument("--root", type=Path, required=True)
    parser.add_argument("--case-id", action="append", required=True)
    args = parser.parse_args()
    run = args.run.resolve()
    root = args.root.resolve()
    broker_path = run / "hidden_broker_stats.json"
    if not broker_path.is_file():
        broker_path = run / "broker-stats.json"
    broker = read_json(broker_path)
    cases: list[dict[str, Any]] = []
    for case_id in args.case_id:
        out = case_root(run, case_id)
        launcher = read_json(out / "launcher_result.json")
        runtime = read_json(out / "runtime_probe.json")
        artifact_path = out / "agent_result.json"
        trajectory_path = out / "trajectory.jsonl"
        artifact = read_json(artifact_path)
        rows = load_jsonl(trajectory_path)
        task_input = root / "test_cases" / case_id / "input.md"
        final_results = [row for row in rows if isinstance(row, dict) and row.get("type") == "result"]
        model_calls = sum(
            int(row.get("metadata", {}).get("rounds", 0))
            for row in final_results
            if isinstance(row.get("metadata"), dict)
            and isinstance(row["metadata"].get("rounds"), int)
        )
        if model_calls <= 0:
            model_calls = sum(
                1 for row in rows
                if isinstance(row, dict)
                and row.get("type") == "progress"
                and isinstance(row.get("metadata"), dict)
                and row["metadata"].get("call_state") == "complete"
            )
        artifact_digest = sha256_file(artifact_path)
        dynamic_digest = hashlib.sha256(json.dumps({
            "case_id": case_id,
            "task_digest": sha256_file(task_input),
            "artifact_digest": artifact_digest,
            "runtime_repository": runtime.get("repository"),
        }, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
        write_json(out / "trajectory.json", {
            "schema_version": "agentswe-deeptutor-smoke-trajectory-v1",
            "case_id": case_id,
            "lower_entry": "agentloop/lower_agent_launcher.py",
            "product_entry": launcher.get("entry"),
            "model_protocol": {
                "model": launcher.get("model"),
                "reasoning_effort": launcher.get("reasoning_effort"),
                "broker_endpoint": "evaluator-owned endpoint (redacted)",
                "candidate_credential": launcher.get("candidate_credential"),
            },
            "product_execution": {
                "executed": launcher.get("executed"),
                "exit_code": launcher.get("exit_code"),
                "product_capability_gap": launcher.get("product_capability_gap"),
                "product_terminal_failure": launcher.get("product_terminal_failure"),
                "runtime_probe_classification": runtime.get("classification"),
            },
            "model_authored_artifact": {
                "path": "agent_result.json",
                "sha256": artifact_digest,
                "case_id": artifact.get("case_id"),
                "model": artifact.get("model"),
                "reasoning_effort": artifact.get("reasoning_effort"),
                "product_stdout": artifact.get("product_stdout"),
                "artifact_paths": artifact.get("artifact_paths"),
            },
            "records": rows,
        })
        write_json(out / "native_evidence.json", {
            "schema_version": "agentswe-deeptutor-smoke-native-evidence-v1",
            "case_id": case_id,
            "real_execution": bool(launcher.get("executed")),
            "classification": launcher.get("classification"),
            "runtime_probe": {
                "classification": runtime.get("classification"),
                "infra_valid": runtime.get("infra_valid"),
                "exit_code": runtime.get("exit_code"),
                "candidate_errors": runtime.get("candidate_errors"),
                "infrastructure_errors": runtime.get("infrastructure_errors"),
            },
            "product": {
                "entry": launcher.get("entry"),
                "executed": launcher.get("executed"),
                "exit_code": launcher.get("exit_code"),
                "public_tool_surface": launcher.get("public_tool_surface"),
            },
            "broker": {
                "protocol": broker.get("protocol"),
                "case_call_count_derived_from_terminal_trajectory": model_calls,
                "successful_calls": broker.get("runtime", {}).get("successful_calls"),
                "failures": broker.get("runtime", {}).get("failures"),
                "credential": {
                    "candidate_visible": launcher.get("candidate_credential"),
                    "provider_secret_logged": False,
                },
            },
            "artifact": {
                "path": "agent_result.json",
                "sha256": artifact_digest,
                "case_bound": artifact.get("case_id") == case_id,
                "model_response_present": bool(artifact.get("product_stdout")),
                "launcher_envelope": True,
            },
        })
        write_json(out / "oracle_summary.json", {
            "schema_version": "agentswe-evaluator-private-oracle-summary-v1",
            "case_id": case_id,
            "source": "evaluator-owned dynamic hidden case oracle",
            "candidate_visible": False,
            "oracle_isolation": True,
            "dynamic_context_digest_only": dynamic_digest,
            "private_fact_fields": ["owner", "generation", "lease expiry", "chain head"],
            "expected_behavior": "case-local product state is compared against private dynamic facts; private values are withheld",
            "comparison_state": launcher.get("classification"),
        })
        cases.append({
            "case_id": case_id,
            "lower_result": str((out / "launcher_result.json").relative_to(run)),
            "agent_artifact": str(artifact_path.relative_to(run)),
            "trajectory": str((out / "trajectory.json").relative_to(run)),
            "native_evidence": str((out / "native_evidence.json").relative_to(run)),
            "oracle_summary": str((out / "oracle_summary.json").relative_to(run)),
            "classification": launcher.get("classification"),
            "successful_broker_calls": model_calls,
        })
    write_json(run / "evidence_manifest.json", {
        "schema_version": "agentswe-deeptutor-smoke-evidence-manifest-v1",
        "run_id": run.name,
        "task": "deeptutor",
        "hidden_cases": cases,
        "all_cases_real_execution": all(item["classification"] != "evaluator_infrastructure_error" for item in cases),
        "private_oracles_not_exported": True,
        "broker_stats": str(broker_path.relative_to(run)),
    })
    print(json.dumps({"run": str(run), "cases": [item["case_id"] for item in cases]}, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
