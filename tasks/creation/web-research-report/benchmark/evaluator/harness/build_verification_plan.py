#!/usr/bin/env python3
"""Create the independent body/PDF checklist after the artifact gate passes."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any

from common import PROTOCOL_VERSION, case_id_from_input, load_oracle, read_json, write_json


def build(case_input: Path, output: Path, artifact_report: dict[str, Any], evaluator_dir: Path) -> dict[str, Any]:
    if not artifact_report.get("valid") or artifact_report.get("gate") != "artifact_validation_passed":
        return {"protocol_version": PROTOCOL_VERSION, "ready": False, "gate": "artifact_validation_required", "checks": []}
    case_id = case_id_from_input(case_input)
    sources_doc = read_json(output / "sources.json")
    graph = read_json(output / "evidence_graph.json")
    candidate_sources = sources_doc.get("sources", [])
    candidate_claims = graph.get("claims", [])
    central_claims = [claim for claim in candidate_claims if claim.get("materiality") == "material"][:12]
    checks: list[dict[str, Any]] = []
    oracle_version: str | None = None
    verification_topics: list[str] = ["active public development request"]
    frozen_sources: list[dict[str, Any]] = []
    policy = "Verify candidate-specific claims, sources, conflicts, and calculations independently against the active public request."
    if case_id.startswith("test_"):
        oracle = load_oracle(evaluator_dir)
        anchor = next(case for case in oracle["cases"] if case["case_id"] == case_id)
        oracle_version = oracle["oracle_version"]
        verification_topics = anchor["verification_topics"]
        frozen_sources = anchor["sources"]
        policy = "Frozen anchors stabilize central facts but are not a reference conclusion. Verify candidate-specific claims and calculations independently."
        for fact in anchor["facts"]:
            checks.append({"kind": "frozen_fact_anchor", "id": fact["id"], "centrality": fact["centrality"], "statement": fact["statement"], "source_ids": fact["source_ids"], "instruction": "Compare the report's relevant proposition with this frozen anchor, then prefer a fresh authoritative body check when accessible."})
    else:
        checks.append({"kind": "development_request", "id": case_id, "instruction": "Use only the active public request and its referenced assets as the task-specific authority; do not load hidden frozen anchors."})
    for claim in central_claims:
        checks.append({"kind": "candidate_material_claim", "id": claim.get("id"), "statement": claim.get("claim"), "instruction": "Retrieve every linked supporting or contradicting body, verify exact quote and locator, and search for a plausible controlling contradiction."})
    for source in candidate_sources:
        if source.get("access_depth") in {"pdf_full", "pdf_partial"}:
            checks.append({"kind": "candidate_pdf", "id": source.get("id"), "locator": source.get("locator"), "instruction": "Open the PDF independently; check title, version/date, page boundaries, cited pages, and whether pdf_full/pdf_partial is truthful."})
    return {
        "protocol_version": PROTOCOL_VERSION,
        "oracle_version": oracle_version,
        "case_id": case_id,
        "ready": True,
        "gate": "ready_for_independent_body_pdf_verification",
        "verification_topics": verification_topics,
        "frozen_sources": frozen_sources,
        "checks": checks,
        "policy": policy,
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--case-input", type=Path, required=True)
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--artifact-report", type=Path, required=True)
    parser.add_argument("--evaluator-dir", type=Path, default=Path(__file__).resolve().parents[1])
    parser.add_argument("--json-out", type=Path, required=True)
    args = parser.parse_args()
    result = build(args.case_input.resolve(), args.output_dir.resolve(), read_json(args.artifact_report), args.evaluator_dir.resolve())
    write_json(args.json_out.resolve(), result)
    print(json.dumps(result, indent=2))
    return 0 if result["ready"] else 2


if __name__ == "__main__":
    raise SystemExit(main())
