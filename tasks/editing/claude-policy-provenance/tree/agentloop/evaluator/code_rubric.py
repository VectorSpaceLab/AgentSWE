#!/usr/bin/env python3
"""Separate eight-axis Code rubric with fixed weights."""
DIMENSIONS = {
    "interface_lifecycle": 15,
    "requirement_mechanism_coverage": 20,
    "analysis_evidence_integrity": 15,
    "safety_privacy_side_effects": 15,
    "recovery_honest_failure": 10,
    "testability_observability": 10,
    "maintainability_generalization": 10,
    "resource_discipline": 5,
}

def rubric() -> dict[str, object]:
    return {"schema_version": "agentswe-code-rubric/v1", "independent_from_result": True, "weights": DIMENSIONS}
