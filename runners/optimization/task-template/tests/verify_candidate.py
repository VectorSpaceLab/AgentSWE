#!/usr/bin/env python3
from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path


def digest(path: Path) -> str:
    h = hashlib.sha256()
    for item in sorted(path.rglob("*"), key=lambda p: p.relative_to(path).as_posix()):
        if item.is_file() and not item.is_symlink():
            h.update(b"F" + item.relative_to(path).as_posix().encode() + item.read_bytes())
    return h.hexdigest()


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--case-id", required=True)
    parser.add_argument("--candidate-evidence", type=Path, required=True)
    parser.add_argument("--resource-evidence", type=Path, required=True)
    parser.add_argument("--predictions", type=Path, required=True)
    parser.add_argument("--verifier-dir", type=Path, required=True)
    parser.add_argument("--candidate-digest", required=True)
    args = parser.parse_args()
    args.verifier_dir.mkdir(parents=True, exist_ok=True)
    infrastructure_errors: list[str] = []
    agent_errors: list[str] = []
    evidence = json.loads(args.candidate_evidence.read_text()) if args.candidate_evidence.is_file() else {}
    if evidence.get("case_id") != args.case_id:
        infrastructure_errors.append("trusted candidate evidence case id mismatch")
    if evidence.get("exit_code") != 0:
        agent_errors.append("candidate harness exited nonzero")
    if evidence.get("timed_out") is True:
        agent_errors.append("candidate timed out")
    if evidence.get("credential_leak_detected") is True:
        agent_errors.append("candidate leaked a resource credential")
    resource_mode = evidence.get("resource_mode")
    resource_evidence = {}
    if args.resource_evidence.is_file():
        try:
            resource_evidence = json.loads(args.resource_evidence.read_text())
        except json.JSONDecodeError:
            resource_evidence = {}
    broker_stats = resource_evidence.get("broker_stats") if isinstance(resource_evidence, dict) else None
    resource_budget_exceeded = False
    if resource_mode == "brokered-v1":
        if evidence.get("credentials_injected") is True:
            infrastructure_errors.append("real resource credentials were injected into candidate")
        if resource_evidence.get("case_id") != args.case_id or not isinstance(broker_stats, dict):
            infrastructure_errors.append("Harbor-collected broker evidence unavailable")
        else:
            limits = broker_stats.get("limits")
            resources = broker_stats.get("resources")
            if broker_stats.get("credential_brokered") is not True or broker_stats.get("candidate_secret_exposed") is not False:
                infrastructure_errors.append("resource broker isolation evidence invalid")
            if broker_stats.get("all_requests_settled") is not True:
                infrastructure_errors.append("resource broker requests were not fully settled")
            if broker_stats.get("finalized") is not True:
                infrastructure_errors.append("resource broker was not finalized")
            if limits != {"model": 20, "search": 20, "visit": 20} or not isinstance(resources, dict):
                infrastructure_errors.append("resource broker budget evidence invalid")
            else:
                for kind in ("model", "search", "visit"):
                    stats = resources.get(kind)
                    if not isinstance(stats, dict) or not isinstance(stats.get("calls"), int):
                        infrastructure_errors.append(f"{kind} resource stats invalid")
                    elif stats["calls"] > limits[kind]:
                        infrastructure_errors.append(f"{kind} resource call count exceeds broker limit")
                    elif stats.get("budget_exceeded") is True:
                        resource_budget_exceeded = True
    if evidence.get("submission_digest_before") != args.candidate_digest or evidence.get("submission_digest_after") != args.candidate_digest:
        infrastructure_errors.append("submission changed during execution")
    rows = []
    if not args.predictions.is_file():
        agent_errors.append("predictions.jsonl missing")
    else:
        for line in args.predictions.read_text(encoding="utf-8", errors="replace").splitlines():
            if line.strip():
                try:
                    rows.append(json.loads(line))
                except json.JSONDecodeError:
                    agent_errors.append("prediction line is not JSON")
    if len(rows) != 1 or rows[0].get("id") != args.case_id:
        agent_errors.append("prediction must contain exactly one row with the case id")
    contract_valid = not infrastructure_errors
    contract = {"schema_version": "1.2", "case_id": args.case_id, "validity_gate": contract_valid, "contract_valid": contract_valid, "score": 100 if contract_valid else 0, "candidate_digest": args.candidate_digest, "prediction_digest": digest(args.predictions.parent) if args.predictions.is_file() else None, "errors": infrastructure_errors + agent_errors, "infrastructure_errors": infrastructure_errors, "agent_errors": agent_errors, "agent_failure": bool(agent_errors), "resource_mode": resource_mode, "resource_budget_exceeded": resource_budget_exceeded, "broker_stats": broker_stats}
    (args.verifier_dir / "score_contract.json").write_text(json.dumps(contract, indent=2) + "\n", encoding="utf-8")
    (args.verifier_dir / "reward.json").write_text(json.dumps({"reward": 1.0 if contract_valid else 0.0}) + "\n", encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
