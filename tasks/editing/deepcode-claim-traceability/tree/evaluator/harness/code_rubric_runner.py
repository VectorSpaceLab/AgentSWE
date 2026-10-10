#!/usr/bin/env python3
"""Deterministic independent Code-axis contract runner."""
from __future__ import annotations

import argparse
import json
from pathlib import Path

WEIGHTS = {
    "interface_lifecycle": 15,
    "requirement_mechanism_coverage": 20,
    "analysis_evidence_integrity": 15,
    "safety_privacy_side_effects": 15,
    "recovery_honest_failure": 10,
    "testability_observability": 10,
    "maintainability_generalization": 10,
    "resource_discipline": 5,
}


def score(evidence: dict[str, object]) -> dict[str, object]:
    dimensions = evidence.get("dimensions", {})
    if not isinstance(dimensions, dict):
        raise ValueError("dimensions must be an object")
    raw: dict[str, int] = {}
    for name, maximum in WEIGHTS.items():
        value = dimensions.get(name, 0)
        if isinstance(value, bool) or not isinstance(value, int) or not 0 <= value <= maximum:
            raise ValueError(f"{name} must be an integer in [0,{maximum}]")
        raw[name] = value
    return {"schema_version": "deepcode-code-score-v1", "axis": "code", "dimensions": raw,
            "weights": WEIGHTS, "score": sum(raw.values()), "maximum": 100,
            "publishable": bool(evidence.get("source_evidence"))}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--evidence", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args(argv)
    result = score(json.loads(args.evidence.read_text(encoding="utf-8")))
    args.output.write_text(json.dumps(result, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(result, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
