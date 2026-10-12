#!/usr/bin/env python3
"""Conservative evaluator for the Agent-loop Result contract."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any

WEIGHTS = {
    "real_model_execution": 10,
    "product_trajectory": 15,
    "dynamic_remediation_accuracy": 20,
    "durable_state_and_provenance": 20,
    "final_artifact_contract": 10,
    "recovery_and_honest_failure": 15,
    "safety_and_isolation": 10,
}


def evaluate(artifact: Path, broker_stats: Path, trajectory: Path | None = None) -> dict[str, Any]:
    result: dict[str, Any] = {
        "schema_version": "agentswe-deeptutor-result-evaluation/v1",
        "status": "invalid",
        "classification": "candidate_contract_failure",
        "scores": {key: 0 for key in WEIGHTS},
        "total": 0,
        "evidence": [],
    }
    try:
        value = json.loads(artifact.read_text(encoding="utf-8"))
        stats = json.loads(broker_stats.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        result["reason"] = f"unable to parse result or broker stats: {exc}"
        return result
    if not isinstance(value, dict) or not isinstance(stats, dict):
        result["reason"] = "result and broker stats must be JSON objects"
        return result
    if int(stats.get("successful_calls", 0) or 0) <= 0:
        result.update({"status": "not_scored", "classification": "provider_infrastructure_error", "reason": "no successful broker call"})
        return result
    if trajectory is not None and not trajectory.is_file():
        result["reason"] = "trajectory artifact is missing"
        return result
    result.update({"status": "scored", "classification": "candidate_valid"})
    result["scores"]["real_model_execution"] = WEIGHTS["real_model_execution"]
    result["scores"]["final_artifact_contract"] = WEIGHTS["final_artifact_contract"]
    result["total"] = sum(result["scores"].values())
    result["evidence"] = ["successful evaluator-owned broker call", "parseable agent_result.json"]
    return result


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--artifact", type=Path, required=True)
    parser.add_argument("--broker-stats", type=Path, required=True)
    parser.add_argument("--trajectory", type=Path)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    value = evaluate(args.artifact, args.broker_stats, args.trajectory)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(value, indent=2, ensure_ascii=False, sort_keys=True) + "\n", encoding="utf-8")
    print(json.dumps(value, indent=2, ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
