#!/usr/bin/env python3
"""Static independent Code-axis runner; never reads Agent-loop Result."""
from __future__ import annotations
import argparse, json
from pathlib import Path
try:
    from ..protocol import changed_paths, validate_delivery, write_json
    from .code_rubric import WEIGHTS
except ImportError:
    from agentloop.protocol import changed_paths, validate_delivery, write_json
    from agentloop.evaluator.code_rubric import WEIGHTS

def score(candidate: Path) -> dict:
    errors = validate_delivery(candidate); patch = candidate / "solution.patch"; text = patch.read_text(encoding="utf-8", errors="replace").lower() if patch.is_file() else ""; paths = changed_paths(patch) if patch.is_file() else []
    terms = {"impact-manifest", "receipt", "publication", "search", "generation", "tenant", "idempotent", "stale", "atomic", "hash", "offline", "docs-only"}; hits = sum(term in text for term in terms)
    dimensions = {
        "interface_lifecycle": WEIGHTS["interface_lifecycle"] if any(x in text for x in ("impact-manifest", "generation", "commit", "recovery")) else 0,
        "requirement_mechanism_coverage": min(WEIGHTS["requirement_mechanism_coverage"], hits * 2),
        "analysis_evidence_integrity": WEIGHTS["analysis_evidence_integrity"] if all(x in text for x in ("sha256", "manifest", "receipt")) else 0,
        "safety_privacy_side_effects": WEIGHTS["safety_privacy_side_effects"] if any(x in text for x in ("allowlist", "docs-only", "path", "tenant")) else 0,
        "recovery_honest_failure": WEIGHTS["recovery_honest_failure"] if any(x in text for x in ("retry", "stale", "conflict", "partial", "recover")) else 0,
        "testability_observability": WEIGHTS["testability_observability"] if any(x in text for x in ("test", "report", "example", "assert")) else 0,
        "maintainability_generalization": WEIGHTS["maintainability_generalization"] if paths and len(paths) <= 24 else 0,
        "resource_discipline": WEIGHTS["resource_discipline"] if "api_key=" not in text and "curl | sh" not in text else 0,
    }
    return {"schema_version": "agentswe-code-score/v1", "axis": "Code", "weights": WEIGHTS, "dimensions": dimensions, "score": sum(dimensions.values()), "changed_paths": paths, "delivery_errors": errors, "result_axis_read": False, "publishable": not errors, "scoring_mode": "static_conservative_phase_A; formal code judge remains independent"}

def main() -> int:
    parser = argparse.ArgumentParser(); parser.add_argument("--candidate", type=Path, required=True); parser.add_argument("--result", type=Path, required=True); args = parser.parse_args(); value = score(args.candidate.resolve()); write_json(args.result.resolve(), value); print(json.dumps(value, indent=2, ensure_ascii=False)); return 0 if value["publishable"] else 1

if __name__ == "__main__": raise SystemExit(main())
