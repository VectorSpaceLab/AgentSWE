#!/usr/bin/env python3
import argparse
import json
from pathlib import Path

parser = argparse.ArgumentParser()
parser.add_argument("--eval-result", type=Path, required=True)
parser.add_argument("--verifier-dir", type=Path, required=True)
args = parser.parse_args()
args.verifier_dir.mkdir(parents=True, exist_ok=True)
value = json.loads(args.eval_result.read_text()) if args.eval_result.is_file() else {"score": 0, "errors": ["eval result missing"]}
score = int(value.get("score", 0))
infrastructure_failure = bool(value.get("infrastructure_failure"))
official = bool(value.get("official_evaluation"))
validity_gate = bool(value.get("validity_gate"))
valid = 0 <= score <= 100 and official and validity_gate and not infrastructure_failure
contract = {
    "schema_version": "2.0",
    "case_id": value.get("case_id"),
    "validity_gate": validity_gate and official and not infrastructure_failure,
    "contract_valid": valid,
    "score": score if valid else 0,
    "reward": score / 100 if valid else 0,
    "errors": value.get("errors", []),
    "official_evaluation": official,
    "infrastructure_failure": infrastructure_failure,
    "deterministic_infrastructure_failure": bool(value.get("deterministic_infrastructure_failure")),
    "native_task_available": bool(value.get("native_task_available")),
    "native_metrics": value.get("native_metrics", {}),
}
(args.verifier_dir / "score_contract.json").write_text(json.dumps(contract, indent=2) + "\n")
(args.verifier_dir / "reward.json").write_text(json.dumps({"reward": contract["reward"]}) + "\n")
