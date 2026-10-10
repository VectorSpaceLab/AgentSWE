#!/usr/bin/env python3
"""Run protocol-v1 gates after one candidate invocation."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from build_verification_plan import build
from classify_run import classify
from common import read_json, write_json
from validate_artifacts import validate


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--case-input", type=Path, required=True)
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--evidence-dir", type=Path, required=True)
    parser.add_argument("--evaluator-dir", type=Path, default=Path(__file__).resolve().parents[1])
    args = parser.parse_args()
    evidence = args.evidence_dir.resolve()
    artifact = validate(args.case_input.resolve(), args.output_dir.resolve(), args.evaluator_dir.resolve())
    write_json(evidence / "artifact_validation.json", artifact)
    classification = classify(
        artifact,
        read_json(evidence / "process_observation.json"),
        read_json(evidence / "provider_health.json"),
        args.output_dir.resolve(),
        args.evaluator_dir.resolve(),
    )
    write_json(evidence / "run_classification.json", classification)
    if classification["classification"] == "scoreable_success":
        plan = build(args.case_input.resolve(), args.output_dir.resolve(), artifact, args.evaluator_dir.resolve())
        write_json(evidence / "verification_plan.json", plan)
    else:
        plan = {"ready": False, "gate": "not_scoreable_success", "checks": []}
    result = {"artifact_validation": artifact, "run_classification": classification, "verification_plan": plan}
    print(json.dumps(result, indent=2))
    if classification["classification"] in {"infrastructure_invalid", "harness_invalid"}:
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
