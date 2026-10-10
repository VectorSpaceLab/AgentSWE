#!/usr/bin/env python3
"""Independent eight-axis Code rubric runner; it never reads Result scores."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from protocol import changed_paths, validate_delivery, write_json

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


def score(candidate: Path) -> dict:
    errors = validate_delivery(candidate)
    paths = changed_paths(candidate / "solution.patch") if not errors else []
    patch_text = (candidate / "solution.patch").read_text(encoding="utf-8", errors="replace").lower() if not errors else ""
    signals = set(paths)
    feature_terms = {"capsule", "budget", "attest", "notification", "provenance", "journal", "science_gate", "claim_contract"}
    feature_hits = sum(term in patch_text for term in feature_terms)
    dimensions = {
        "interface_lifecycle": WEIGHTS["interface_lifecycle"] if any(term in patch_text for term in ("claim_verification", "session", "prepare", "commit", "cancel")) else 0,
        "requirement_mechanism_coverage": WEIGHTS["requirement_mechanism_coverage"] if feature_hits >= 5 else round(WEIGHTS["requirement_mechanism_coverage"] * feature_hits / 5),
        "analysis_evidence_integrity": WEIGHTS["analysis_evidence_integrity"] if all(term in patch_text for term in ("evidence_manifest", "sha256", "replay")) else 0,
        "safety_privacy_side_effects": WEIGHTS["safety_privacy_side_effects"] if any(term in patch_text for term in ("authorized", "allowlist", "idempotent", "private")) and "credential" not in patch_text else 0,
        "recovery_honest_failure": WEIGHTS["recovery_honest_failure"] if any(term in patch_text for term in ("retry", "recover", "partial", "failure")) else 0,
        "testability_observability": WEIGHTS["testability_observability"] if any(term in patch_text for term in ("test", "assert", "receipt", "result")) else 0,
        "maintainability_generalization": WEIGHTS["maintainability_generalization"] if len(signals) <= 20 and signals and not any(path.startswith("/") for path in signals) else 0,
        "resource_discipline": WEIGHTS["resource_discipline"] if "docker run" not in patch_text and "api_key=" not in patch_text else 0,
    }
    return {"schema_version": 1, "axis": "Code", "weights": WEIGHTS, "dimensions": dimensions, "total": sum(dimensions.values()), "changed_paths": paths, "delivery_errors": errors, "result_score_read": False, "publishable": not errors, "scoring_mode": "static_conservative_heuristic; formal review remains independent"}


def main() -> int:
    parser = argparse.ArgumentParser(); parser.add_argument("--candidate", type=Path, required=True); parser.add_argument("--result", type=Path, required=True)
    args = parser.parse_args(); value = score(args.candidate.resolve()); write_json(args.result.resolve(), value); print(json.dumps(value, indent=2, sort_keys=True)); return 0 if value["publishable"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
