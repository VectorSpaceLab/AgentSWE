#!/usr/bin/env python3
"""Issue case-local runtime inputs while keeping the oracle evaluator-owned."""
from __future__ import annotations
import argparse, hashlib, json, secrets
from pathlib import Path
try:
    from ..protocol import read_json, write_json
except ImportError:
    from agentloop.protocol import read_json, write_json

PRIVATE_KEYS = {
    "expected_pages", "direct_pages", "unaffected_pages",
    "required_text", "forbidden_text", "expected_examples",
    "expected_stale_kinds", "preserved_tokens", "preserved_anchors",
    "oracle", "secret", "required_actions", "public_fixture",
    "request", "scenario", "change_kind",
}

def issue(case_id: str, template: dict, evaluator_dir: Path, candidate_dir: Path, task_text: str | None = None) -> dict:
    nonce = secrets.token_hex(16)
    oracle = {
        "schema_version": 1,
        "case_id": case_id,
        "nonce": nonce,
        "expected": {k: v for k, v in template.items() if k in PRIVATE_KEYS},
        "oracle_digest": hashlib.sha256((case_id + nonce).encode()).hexdigest(),
    }
    write_json(evaluator_dir / f"{case_id}.oracle.json", oracle)
    visible = {k: v for k, v in template.items() if k not in PRIVATE_KEYS}
    visible.update({"case_id": case_id, "runtime_nonce": nonce, "oracle": "evaluator-owned"})
    if task_text is not None:
        visible["task"] = task_text
    request = candidate_dir / case_id / "request.json"
    write_json(request, visible)
    return {"case_id": case_id, "candidate_request": str(request), "oracle_path": str(evaluator_dir / f"{case_id}.oracle.json"), "oracle_mounted": False, "dynamic": True}

def main() -> int:
    parser = argparse.ArgumentParser(); parser.add_argument("--case-id", required=True); parser.add_argument("--template", type=Path, required=True); parser.add_argument("--evaluator-dir", type=Path, required=True); parser.add_argument("--candidate-dir", type=Path, required=True); parser.add_argument("--task-file", type=Path); args = parser.parse_args(); task = args.task_file.read_text(encoding="utf-8") if args.task_file else None; result = issue(args.case_id, read_json(args.template), args.evaluator_dir.resolve(), args.candidate_dir.resolve(), task); print(json.dumps(result)); return 0

if __name__ == "__main__": raise SystemExit(main())
