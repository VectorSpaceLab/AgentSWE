#!/usr/bin/env python3
"""Validate benchmark layout, protocol, and hidden frozen-oracle structure."""

from __future__ import annotations

import argparse
import hashlib
import re
import sys
from pathlib import Path

from common import PROTOCOL_VERSION, load_oracle, load_protocol


def validate(root: Path) -> dict[str, object]:
    errors: list[str] = []
    protocol = load_protocol(root / "evaluator")
    oracle = load_oracle(root / "evaluator")
    if protocol.get("protocol_version") != PROTOCOL_VERSION:
        errors.append("protocol version mismatch")
    if protocol.get("frozen_evidence_version") != oracle.get("oracle_version"):
        errors.append("protocol and oracle versions differ")
    oracle_bytes = (root / "evaluator" / "frozen_evidence" / "v1" / "oracle.json").read_bytes()
    if protocol.get("frozen_evidence_sha256") != hashlib.sha256(oracle_bytes).hexdigest():
        errors.append("frozen oracle SHA-256 does not match protocol manifest")
    dev_ids = sorted(p.parent.name for p in (root / "dev_cases").glob("dev_*/input.md"))
    test_ids = sorted(p.parent.name for p in (root / "test_cases").glob("test_*/input.md"))
    if dev_ids != ["dev_001", "dev_002"]:
        errors.append(f"expected exactly dev_001/dev_002, found {dev_ids}")
    expected_tests = [f"test_{number:03d}" for number in range(1, 7)]
    if test_ids != expected_tests:
        errors.append(f"expected six ordered tests, found {test_ids}")
    cases = oracle.get("cases")
    if not isinstance(cases, list) or [case.get("case_id") for case in cases if isinstance(case, dict)] != expected_tests:
        errors.append("oracle must contain exactly test_001 through test_006 in order")
        cases = []
    for case in cases:
        source_ids = {source.get("id") for source in case.get("sources", []) if isinstance(source, dict)}
        if len(source_ids) < 2 or None in source_ids:
            errors.append(f"{case.get('case_id')}: invalid or duplicate oracle sources")
        for fact in case.get("facts", []):
            if not set(fact.get("source_ids", [])).issubset(source_ids):
                errors.append(f"{case.get('case_id')}/{fact.get('id')}: unresolved source id")
    hidden_tokens = {str(fact.get("statement", "")) for case in cases for fact in case.get("facts", [])}
    runtime_text = "\n".join(
        path.read_text(encoding="utf-8")
        for path in sorted([*(root / "dev_cases").glob("*/input.md"), *(root / "test_cases").glob("*/input.md")])
    )
    for statement in hidden_tokens:
        if len(statement) > 24 and statement in runtime_text:
            errors.append("a frozen fact statement is copied verbatim into a runtime case")
    for path in sorted((root / "test_cases").glob("*/input.md")):
        text = path.read_text(encoding="utf-8")
        if not re.search(r"202[5-6]-\d{2}-\d{2}", text):
            errors.append(f"{path}: missing explicit evidence cutoff")
    return {
        "valid": not errors,
        "protocol_version": protocol.get("protocol_version"),
        "oracle_version": oracle.get("oracle_version"),
        "dev_cases": dev_ids,
        "test_cases": test_ids,
        "errors": errors,
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--benchmark", type=Path, default=Path(__file__).resolve().parents[2])
    args = parser.parse_args()
    result = validate(args.benchmark.resolve())
    import json
    print(json.dumps(result, indent=2))
    return 0 if result["valid"] else 1


if __name__ == "__main__":
    sys.exit(main())
