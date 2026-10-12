#!/usr/bin/env python3
"""Generate case-local nonce/oracle outside the Candidate-visible payload."""
from __future__ import annotations

import argparse
import json
import secrets
from pathlib import Path
from typing import Any

try:
    from agentloop.protocol import write_json
    from agentloop.evaluator.case_contract import ORACLE_SCHEMA, VISIBLE_SCHEMA, contract_digest, digest_object, validate_private_oracle, validate_visible_case
except ModuleNotFoundError:  # direct script execution
    import sys
    sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
    from agentloop.protocol import write_json  # type: ignore
    from agentloop.evaluator.case_contract import ORACLE_SCHEMA, VISIBLE_SCHEMA, contract_digest, digest_object, validate_private_oracle, validate_visible_case  # type: ignore


def issue(case_id: str, template: dict[str, Any], evaluator_dir: Path, candidate_dir: Path, *, branch: int | None = None) -> dict[str, Any]:
    nonce = "case_" + secrets.token_hex(12)
    candidate_template = template.get("candidate") if isinstance(template.get("candidate"), dict) else {
        key: value for key, value in template.items()
        if key not in {"oracle_fields", "expected", "secret", "oracle", "private_oracle"}
    }
    oracle_template = template.get("private_oracle") if isinstance(template.get("private_oracle"), dict) else (
        template.get("oracle") if isinstance(template.get("oracle"), dict) else {}
    )
    from agentloop.evaluator.authority_world import specialize
    candidate_template, oracle_template = specialize(case_id, candidate_template, oracle_template, branch=branch)
    hidden = case_id.startswith("test_")
    digest = contract_digest(case_id) if hidden else digest_object({"case_id": case_id, "public_contract": "claude-public-policy/v1"})
    visible = {
        **candidate_template,
        "schema_version": VISIBLE_SCHEMA,
        "case_id": case_id,
        "runtime_nonce": nonce,
        "case_contract_digest": digest,
    }
    visible = validate_visible_case(visible, case_id=case_id, hidden=hidden)
    oracle = {
        **oracle_template,
        "schema_version": ORACLE_SCHEMA,
        "case_id": case_id,
        "runtime_nonce": nonce,
        "case_contract_digest": digest,
    }
    oracle.pop("oracle_digest", None)
    oracle["visible_case_digest"] = digest_object(visible)
    oracle["oracle_digest"] = digest_object(oracle)
    validate_private_oracle(oracle, visible, case_id=case_id)
    visible_path = candidate_dir / f"{case_id}.json"
    oracle_path = evaluator_dir / f"{case_id}.oracle.json"
    if visible_path.exists() or oracle_path.exists():
        raise FileExistsError("issued worlds are immutable; use a new experiment identity")
    write_json(oracle_path, oracle)
    write_json(visible_path, visible)
    return {"case_id": case_id, "candidate_payload": str(visible_path), "oracle_path": str(oracle_path), "oracle_mounted": False, "case_contract_digest": digest, "visible_case_digest": digest_object(visible)}


def main() -> int:
    parser = argparse.ArgumentParser(); parser.add_argument("--case-id", required=True); parser.add_argument("--template", type=Path, required=True); parser.add_argument("--evaluator-dir", type=Path, required=True); parser.add_argument("--candidate-dir", type=Path, required=True); args = parser.parse_args(); template = json.loads(args.template.read_text(encoding="utf-8")); print(json.dumps(issue(args.case_id, template, args.evaluator_dir.resolve(), args.candidate_dir.resolve()), indent=2)); return 0


if __name__ == "__main__": raise SystemExit(main())
