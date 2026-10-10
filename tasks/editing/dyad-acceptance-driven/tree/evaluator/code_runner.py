#!/usr/bin/env python3
"""Independent eight-axis Code rubric runner; no Agent-loop Result inputs."""
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


def score(values: dict[str, int]) -> dict[str, object]:
    errors = [key for key in WEIGHTS if not isinstance(values.get(key), int) or not 0 <= values[key] <= WEIGHTS[key]]
    if errors:
        raise ValueError("invalid or missing Code dimensions: " + ", ".join(errors))
    return {"schema_version": "agentswe-edit-code-score-v1", "dimensions": values, "total": sum(values.values()), "result_score_used": False}


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--scores", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    values = json.loads(args.scores.read_text(encoding="utf-8"))
    result = score(values)
    args.output.write_text(json.dumps(result, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(result, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
