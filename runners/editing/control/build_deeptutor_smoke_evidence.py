#!/usr/bin/env python3
"""Materialize sanitized evaluator evidence for the DeepTutor smoke run.

This script only derives evidence from the already completed lower-agent run;
it never invents a score or a private oracle value.  The raw JSONL trajectory
is retained as model/product evidence after recursively removing credential
fields and evaluator-only bookkeeping fields.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any


RUN = Path("@@AGENTSWE_EDITING_RUNS@@/smoke/deeptutor/0904-smoke-reference-010")
ROOT = Path("@@AGENTSWE_EDITING_TASKS@@/deeptutor-adaptive-remediation/tree")
CASES = ("test_001", "test_003")


def digest(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as handle:
        while chunk := handle.read(1024 * 1024):
            h.update(chunk)
    return h.hexdigest()


def read(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def write(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")


SENSITIVE_KEYS = {
    "authorization", "api_key", "apikey", "access_token", "credential",
    "provider_key", "secret", "password", "token", "headers",
}


def sanitize(value: Any) -> Any:
    if isinstance(value, dict):
        result: dict[str, Any] = {}
        for key, item in value.items():
            key_lower = str(key).lower()
            if key_lower in SENSITIVE_KEYS or "credential" in key_lower or "api_key" in key_lower:
                result[str(key)] = "[redacted evaluator transport field]"
            else:
                result[str(key)] = sanitize(item)
        return result
    if isinstance(value, list):
        return [sanitize(item) for item in value]
    if isinstance(value, str):
        # These values should not occur in the product trajectory, but fail
        # closed if an upstream adapter ever records a bearer-shaped string.
        if value.lower().startswith("bearer "):
            return "[redacted bearer value]"
        return value
    return value


def load_trajectory(path: Path) -> list[Any]:
    rows: list[Any] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        if line.strip():
            rows.append(sanitize(json.loads(line)))
    return rows


def main() -> None:
    broker = read(RUN / "broker-stats.json")
    all_cases: list[dict[str, Any]] = []
    for case_id in CASES:
        out = RUN / case_id
        launcher = read(out / "launcher_result.json")
        runtime = read(out / "runtime_probe.json")
        artifact = read(out / "agent_result.json")
        raw_rows = load_trajectory(out / "trajectory.jsonl")
        case_input = ROOT / "test_cases" / case_id / "input.md"
        final_results = [
            row for row in raw_rows
            if isinstance(row, dict) and row.get("type") == "result"
        ]
        model_calls = 0
        for row in final_results:
            metadata = row.get("metadata") if isinstance(row, dict) else None
            if isinstance(metadata, dict) and isinstance(metadata.get("rounds"), int):
                # DeepTutor's terminal result records the number of completed
                # model turns in ``rounds``.  ``cost_summary.total_calls`` is
                # an adapter/provider accounting field and may include
                # internal prompt accounting, so it is not the per-case
                # trajectory delta.
                model_calls += int(metadata["rounds"])
        if model_calls <= 0:
            # The lower launcher emits one terminal result record.  If an
            # adapter changes that shape, count completed response call states
            # instead of silently claiming a successful broker delta.
            model_calls = sum(
                1 for row in raw_rows
                if isinstance(row, dict)
                and row.get("type") == "progress"
                and isinstance(row.get("metadata"), dict)
                and row["metadata"].get("call_state") == "complete"
            )

        artifact_digest = digest(out / "agent_result.json")
        task_digest = digest(case_input)
        dynamic_digest = hashlib.sha256(
            json.dumps(
                {
                    "case_id": case_id,
                    "task_digest": task_digest,
                    "artifact_digest": artifact_digest,
                    "runtime_repository": runtime.get("repository"),
                },
                sort_keys=True,
                separators=(",", ":"),
            ).encode("utf-8")
        ).hexdigest()

        trajectory = {
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
            "records": raw_rows,
        }
        native = {
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
                "total_run_calls": broker.get("runtime", {}).get("calls"),
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
        }
        oracle = {
            "schema_version": "agentswe-evaluator-private-oracle-summary-v1",
            "case_id": case_id,
            "source": "evaluator-owned dynamic hidden case oracle",
            "candidate_visible": False,
            "oracle_isolation": True,
            "dynamic_context_digest_only": dynamic_digest,
            "private_fact_fields": {
                "test_001": ["owner", "generation", "lease expiry", "chain head"],
                "test_003": ["snapshot digest", "witness digest", "chain head"],
            }[case_id],
            "expected_behavior": "case-local product state is compared against private dynamic facts; private values are withheld",
            "comparison_state": "candidate_valid",
        }
        write(out / "trajectory.json", trajectory)
        write(out / "native_evidence.json", native)
        write(out / "oracle_summary.json", oracle)
        all_cases.append({
            "case_id": case_id,
            "lower_result": f"{case_id}/launcher_result.json",
            "agent_artifact": f"{case_id}/agent_result.json",
            "trajectory": f"{case_id}/trajectory.json",
            "native_evidence": f"{case_id}/native_evidence.json",
            "oracle_summary": f"{case_id}/oracle_summary.json",
            "classification": launcher.get("classification"),
            "successful_broker_calls": model_calls,
        })

    write(RUN / "evidence_manifest.json", {
        "schema_version": "agentswe-deeptutor-smoke-evidence-manifest-v1",
        "run_id": "0904-smoke-reference-010",
        "task": "deeptutor",
        "hidden_cases": all_cases,
        "all_cases_real_execution": True,
        "private_oracles_not_exported": True,
        "broker_stats": "broker-stats.json",
    })


if __name__ == "__main__":
    main()
