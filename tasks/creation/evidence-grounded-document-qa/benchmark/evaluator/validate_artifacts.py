#!/usr/bin/env python3
"""Validate v4 final-artifact schemas and evidence locators against source content."""

from __future__ import annotations

import argparse
import json
import math
import re
from pathlib import Path
from typing import Any

from source_native import SourceInspector


REQUIRED_FILES = (
    "answer.md",
    "claims_and_citations.json",
    "review.html",
    "review_manifest.json",
    "source_bundle.json",
    "run_report.json",
)
STATUSES = {"supported", "partially_supported", "contradicted", "unanswerable"}
CONFIDENCE = {"high", "medium", "low"}


def load_json(path: Path, errors: list[str]) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        errors.append(f"invalid {path.name}: {exc}")
        return None


def validate_claims(value: Any, inspector: SourceInspector, errors: list[str]) -> tuple[dict[str, dict[str, Any]], dict[str, dict[str, Any]]]:
    claim_map: dict[str, dict[str, Any]] = {}
    evidence_map: dict[str, dict[str, Any]] = {}
    if not isinstance(value, dict) or value.get("schema_version") != "4.0" or not isinstance(value.get("claims"), list):
        errors.append('claims_and_citations.json must be schema_version "4.0" with a claims array')
        return claim_map, evidence_map
    for index, claim in enumerate(value["claims"]):
        label = f"claim[{index}]"
        if not isinstance(claim, dict):
            errors.append(f"{label} is not an object")
            continue
        required = {"claim_id", "claim", "status", "confidence", "supporting_evidence", "contradicting_evidence"}
        missing = sorted(required - claim.keys())
        if missing:
            errors.append(f"{label} missing {', '.join(missing)}")
            continue
        claim_id = claim["claim_id"]
        if not isinstance(claim_id, str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}", claim_id):
            errors.append(f"{label} has invalid claim_id")
            continue
        if claim_id in claim_map:
            errors.append(f"duplicate claim_id {claim_id!r}")
            continue
        claim_map[claim_id] = claim
        if not isinstance(claim["claim"], str) or not claim["claim"].strip():
            errors.append(f"claim {claim_id!r} has empty text")
        if claim["status"] not in STATUSES:
            errors.append(f"claim {claim_id!r} has invalid status")
        if claim["confidence"] not in CONFIDENCE:
            errors.append(f"claim {claim_id!r} has invalid confidence")
        bucket_counts = {"supporting_evidence": 0, "contradicting_evidence": 0}
        for bucket, relation in (("supporting_evidence", "supports"), ("contradicting_evidence", "contradicts")):
            items = claim[bucket]
            if not isinstance(items, list):
                errors.append(f"claim {claim_id!r} {bucket} is not an array")
                continue
            bucket_counts[bucket] = len(items)
            for item_index, evidence in enumerate(items):
                prefix = f"claim {claim_id!r} {bucket}[{item_index}]"
                if not isinstance(evidence, dict):
                    errors.append(f"{prefix} is not an object")
                    continue
                required_evidence = {"evidence_id", "source", "locator", "quote_or_observation", "relation"}
                missing_evidence = sorted(required_evidence - evidence.keys())
                if missing_evidence:
                    errors.append(f"{prefix} missing {', '.join(missing_evidence)}")
                    continue
                evidence_id = evidence["evidence_id"]
                if not isinstance(evidence_id, str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}", evidence_id):
                    errors.append(f"{prefix} has invalid evidence_id")
                    continue
                if evidence_id in evidence_map:
                    errors.append(f"duplicate evidence_id {evidence_id!r}")
                    continue
                evidence_map[evidence_id] = {**evidence, "claim_id": claim_id}
                if evidence["relation"] != relation:
                    errors.append(f"evidence {evidence_id!r} relation must be {relation!r}")
                if not isinstance(evidence["source"], str) or not evidence["source"]:
                    errors.append(f"evidence {evidence_id!r} has invalid source")
                    continue
                if not isinstance(evidence["quote_or_observation"], str) or not evidence["quote_or_observation"].strip():
                    errors.append(f"evidence {evidence_id!r} has empty quote_or_observation")
                    continue
                try:
                    inspector.validate(evidence["source"], evidence["locator"], evidence["quote_or_observation"])
                except (OSError, UnicodeError, ValueError, KeyError) as exc:
                    errors.append(f"evidence {evidence_id!r} locator/content failure: {exc}")
        evidence_count = sum(bucket_counts.values())
        if evidence_count == 0:
            errors.append(f"claim {claim_id!r} has no evidence; abstentions require explicit scope/absence evidence in these cases")
        if claim["status"] in {"supported", "partially_supported", "unanswerable"} and bucket_counts["supporting_evidence"] == 0:
            errors.append(f"claim {claim_id!r} status {claim['status']!r} requires supporting/scope evidence")
        if claim["status"] == "contradicted" and bucket_counts["contradicting_evidence"] == 0:
            errors.append(f"claim {claim_id!r} status 'contradicted' requires contradicting evidence")
    return claim_map, evidence_map


def validate_report(value: Any, output_dir: Path, errors: list[str]) -> None:
    required = {
        "status",
        "artifact_paths",
        "errors",
        "llm_requests",
        "gateway_image_requests",
        "serper_requests",
        "web_retrieval",
        "elapsed_seconds",
    }
    if not isinstance(value, dict):
        errors.append("run_report.json must be an object")
        return
    missing = sorted(required - value.keys())
    if missing:
        errors.append(f"run_report.json missing {', '.join(missing)}")
    allowed = required | {"peak_rss_mb", "cpu_seconds"}
    unexpected = sorted(value.keys() - allowed)
    if unexpected:
        errors.append(f"run_report.json has unsupported fields: {', '.join(unexpected)}")
    if value.get("status") not in {"ok", "success"}:
        errors.append("successful artifact validation requires run_report status 'success' or legacy 'ok'")
    report_errors = value.get("errors")
    if not isinstance(report_errors, list) or any(not isinstance(item, str) for item in report_errors):
        errors.append("run_report errors must be an array of strings")
    elif report_errors:
        errors.append("successful run_report errors must be empty")
    paths = value.get("artifact_paths")
    if not isinstance(paths, list):
        errors.append("run_report artifact_paths must be an array")
    else:
        resolved_output = output_dir.resolve()
        normalized_paths: list[str] = []
        for item in paths:
            if not isinstance(item, str):
                errors.append("run_report artifact path is not a string")
                continue
            candidate = (output_dir / item).resolve() if not Path(item).is_absolute() else Path(item).resolve()
            if candidate.parent != resolved_output or candidate.name not in REQUIRED_FILES:
                errors.append(f"run_report artifact path escapes or names an unowned file: {item!r}")
            else:
                normalized_paths.append(candidate.name)
        if len(normalized_paths) != len(set(normalized_paths)):
            errors.append("run_report artifact_paths contains duplicates")
        if set(normalized_paths) != set(REQUIRED_FILES):
            errors.append("successful run_report artifact_paths must list all six owned artifacts exactly once")
    for key in ("llm_requests", "gateway_image_requests", "serper_requests"):
        value_key = value.get(key)
        if not isinstance(value_key, int) or value_key < 0:
            errors.append(f"run_report {key} must be a non-negative integer")
    if isinstance(value.get("llm_requests"), int) and value["llm_requests"] > 300:
        errors.append("run_report exceeds 300 combined model requests")
    if isinstance(value.get("gateway_image_requests"), int) and value["gateway_image_requests"] > 100:
        errors.append("run_report exceeds 100 image requests")
    if (
        isinstance(value.get("llm_requests"), int)
        and isinstance(value.get("gateway_image_requests"), int)
        and value["gateway_image_requests"] > value["llm_requests"]
    ):
        errors.append("run_report gateway_image_requests cannot exceed total llm_requests")
    if value.get("serper_requests") != 0:
        errors.append("closed-corpus case has nonzero serper_requests")
    retrieval = value.get("web_retrieval")
    if isinstance(retrieval, dict):
        nonzero = [key for key, item in retrieval.items() if item not in (0, False, None, [], {})]
        if nonzero:
            errors.append(f"closed-corpus web_retrieval is nonzero: {', '.join(nonzero)}")
    else:
        errors.append("run_report web_retrieval must be an object")
    elapsed = value.get("elapsed_seconds")
    if not isinstance(elapsed, (int, float)) or not math.isfinite(float(elapsed)) or elapsed < 0 or elapsed > 600:
        errors.append("run_report elapsed_seconds is invalid or exceeds 600")
    for key in ("peak_rss_mb", "cpu_seconds"):
        if key in value and (
            not isinstance(value[key], (int, float))
            or not math.isfinite(float(value[key]))
            or value[key] < 0
        ):
            errors.append(f"run_report {key} must be a finite non-negative number")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("case_dir", type=Path)
    parser.add_argument("output_dir", type=Path)
    args = parser.parse_args()
    case_dir, output_dir = args.case_dir.resolve(), args.output_dir.resolve()
    errors: list[str] = []
    try:
        inspector = SourceInspector(case_dir)
    except (OSError, ValueError) as exc:
        print(json.dumps({"valid": False, "errors": [str(exc)]}, indent=2))
        return 1
    for name in REQUIRED_FILES:
        path = output_dir / name
        if not path.is_file() or path.is_symlink():
            errors.append(f"missing or unsafe required artifact {name}")
    answer_path = output_dir / "answer.md"
    if answer_path.is_file():
        try:
            answer = answer_path.read_text(encoding="utf-8")
            if not answer.strip():
                errors.append("answer.md is empty")
            if re.search(r"\b(?:nan|infinity|inf)\b", answer, re.I):
                errors.append("answer.md contains non-finite numeric output")
        except UnicodeError as exc:
            errors.append(f"answer.md is not valid UTF-8: {exc}")
    claims_value = load_json(output_dir / "claims_and_citations.json", errors) if (output_dir / "claims_and_citations.json").is_file() else None
    claim_map, evidence_map = validate_claims(claims_value, inspector, errors)
    report_value = load_json(output_dir / "run_report.json", errors) if (output_dir / "run_report.json").is_file() else None
    validate_report(report_value, output_dir, errors)
    result = {
        "valid": not errors,
        "errors": errors,
        "claim_count": len(claim_map),
        "evidence_count": len(evidence_map),
        "source_count": len(inspector.assets),
    }
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0 if not errors else 1


if __name__ == "__main__":
    raise SystemExit(main())
