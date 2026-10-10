#!/usr/bin/env python3
"""Evaluator-only dynamic case service; its oracle is never mounted in Candidate."""

from __future__ import annotations

import argparse
import secrets
from pathlib import Path

from protocol import sha256_bytes, write_json


def issue(case_id: str, output: Path) -> dict:
    nonce = secrets.token_hex(16)
    facts = {
        "case_id": case_id,
        "nonce": nonce,
        "release_decision": "release_with_revisions" if case_id == "dev_001" else "block",
        "expected_worker_policy": "partial_allowed" if case_id == "dev_002" else "complete_required",
    }
    oracle = {"schema_version": 1, "case_id": case_id, "facts": facts, "rollout_digest": sha256_bytes((case_id + nonce).encode())}
    write_json(output, oracle)
    return {"schema_version": 1, "case_id": case_id, "nonce": nonce, "candidate_payload": {"case_id": case_id, "nonce": nonce, "oracle": "private"}, "oracle_path": str(output)}


def main() -> int:
    parser = argparse.ArgumentParser(); parser.add_argument("--case-id", required=True); parser.add_argument("--oracle", type=Path, required=True); parser.add_argument("--payload", type=Path, required=True)
    args = parser.parse_args(); write_json(args.payload.resolve(), issue(args.case_id, args.oracle.resolve())["candidate_payload"]); print(args.oracle); return 0


if __name__ == "__main__": raise SystemExit(main())
