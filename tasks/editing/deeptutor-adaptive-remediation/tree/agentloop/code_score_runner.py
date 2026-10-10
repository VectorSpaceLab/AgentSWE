#!/usr/bin/env python3
"""Independent static Code-axis precheck; not a formal judge score."""

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


def precheck(repository: Path) -> dict:
    text = "\n".join(p.read_text(encoding="utf-8", errors="ignore") for p in repository.rglob("*.py"))
    checks = {
        "interface_lifecycle": "MasteryPathCapability" in text or "mastery" in text,
        "requirement_mechanism_coverage": "checkpoint" in text and "snapshot" in text,
        "analysis_evidence_integrity": "digest" in text or "provenance" in text,
        "safety_privacy_side_effects": "placeholder" in text and "isolat" in text,
        "recovery_honest_failure": any(word in text for word in ("stale", "retry", "tamper", "failure")),
        "testability_observability": "trajectory" in text or "self_test" in text,
        "maintainability_generalization": "argparse" in text and "Path" in text,
        "resource_discipline": "timeout" in text and "max" in text,
    }
    scores = {key: WEIGHTS[key] if checks[key] else 0 for key in WEIGHTS}
    return {"schema_version": "agentswe-deeptutor-code-precheck/v1", "formal_judge": False, "scores": scores, "total": sum(scores.values()), "checks": checks}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repository", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    result = precheck(args.repository.resolve())
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(result, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    print(json.dumps(result, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
