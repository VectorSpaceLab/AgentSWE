#!/usr/bin/env python3
"""Deterministic success-artifact gate; contains no task answer oracle."""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path
from typing import Any

from common import PROTOCOL_VERSION, case_id_from_input, load_protocol, read_json, write_json


CID = re.compile(r"C[1-9][0-9]*")
SID = re.compile(r"S[1-9][0-9]*")
PID = re.compile(r"P[1-9][0-9]*")
KID = re.compile(r"K[1-9][0-9]*")
CITATION = re.compile(r"\[(C[1-9][0-9]*(?:\s*,\s*C[1-9][0-9]*)*)\]")
ALLOWED_DEPTHS = {"full_page", "browser_rendered", "pdf_full", "pdf_partial", "partial_page", "search_snippet"}


def unique_ids(items: Any, pattern: re.Pattern[str], label: str, errors: list[str]) -> dict[str, dict[str, Any]]:
    result: dict[str, dict[str, Any]] = {}
    if not isinstance(items, list):
        errors.append(f"{label} must be an array")
        return result
    for index, item in enumerate(items):
        if not isinstance(item, dict) or not isinstance(item.get("id"), str) or not pattern.fullmatch(item["id"]):
            errors.append(f"{label}[{index}] has invalid id")
            continue
        if item["id"] in result:
            errors.append(f"{label} has duplicate id {item['id']}")
        result[item["id"]] = item
    return result


def validate(case_input: Path, output: Path, evaluator_dir: Path) -> dict[str, Any]:
    errors: list[str] = []
    warnings: list[str] = []
    protocol = load_protocol(evaluator_dir)
    required = protocol["required_success_artifacts"]
    for name in required:
        if not (output / name).is_file() or (output / name).is_symlink():
            errors.append(f"missing or unsafe required artifact: {name}")
    if errors:
        return {"valid": False, "gate": "artifact_validation_failed", "errors": errors, "warnings": warnings}
    try:
        report = (output / "report.md").read_text(encoding="utf-8")
        sources_doc = read_json(output / "sources.json")
        graph = read_json(output / "evidence_graph.json")
        run_report = read_json(output / "run_report.json")
    except Exception as exc:
        return {"valid": False, "gate": "artifact_validation_failed", "errors": [str(exc)], "warnings": warnings}
    if not report.strip() or "#" not in report:
        errors.append("report.md is empty or lacks Markdown structure")
    sources = unique_ids(sources_doc.get("sources") if isinstance(sources_doc, dict) else None, SID, "sources", errors)
    if not sources:
        errors.append("sources.json has no valid source")
    if not isinstance(sources_doc, dict) or sources_doc.get("schema_version") != "2.0":
        errors.append("sources.json schema_version must be 2.0")
    if not isinstance(graph, dict) or graph.get("schema_version") != "1.0":
        errors.append("evidence_graph.json schema_version must be 1.0")
        graph = {}
    claims = unique_ids(graph.get("claims"), CID, "claims", errors)
    passages = unique_ids(graph.get("passages"), PID, "passages", errors)
    calculations = unique_ids(graph.get("calculations"), KID, "calculations", errors)
    if not isinstance(graph.get("source_relations"), list):
        errors.append("source_relations must be an array")
    cited: set[str] = set()
    for match in CITATION.finditer(report):
        cited.update(part.strip() for part in match.group(1).split(","))
    missing_claims = sorted(cited - set(claims))
    if missing_claims:
        errors.append(f"report citations do not resolve: {missing_claims}")
    for cid, claim in claims.items():
        for edge_name in ("supports", "contradicts"):
            edges = claim.get(edge_name)
            if not isinstance(edges, list):
                errors.append(f"{cid}.{edge_name} must be an array")
                continue
            for edge in edges:
                if not isinstance(edge, dict):
                    errors.append(f"{cid}.{edge_name} contains a non-object edge")
                    continue
                sid, pid = edge.get("source_id"), edge.get("passage_id")
                if sid not in sources or pid not in passages:
                    errors.append(f"{cid}.{edge_name} has an unresolved source/passage edge")
                elif passages[pid].get("source_id") != sid:
                    errors.append(f"{cid}.{edge_name} passage ownership mismatch")
        calc_id = claim.get("calculation_id")
        if calc_id is not None and calc_id not in calculations:
            errors.append(f"{cid} references missing calculation {calc_id}")
    for pid, passage in passages.items():
        sid = passage.get("source_id")
        if sid not in sources:
            errors.append(f"{pid} references missing source")
        elif sources[sid].get("access_depth") == "search_snippet":
            errors.append(f"{pid} uses a search-snippet source as evidence")
        if not str(passage.get("quote", "")).strip():
            errors.append(f"{pid} has an empty quote")
    for sid, source in sources.items():
        if source.get("access_depth") not in ALLOWED_DEPTHS:
            errors.append(f"{sid} has invalid access_depth")
        locator = source.get("locator")
        if source.get("kind") == "web" and not isinstance(locator, str):
            errors.append(f"{sid} lacks a web locator")
    for kid, calculation in calculations.items():
        if not calculation.get("formula") or not isinstance(calculation.get("inputs"), list) or not calculation["inputs"]:
            errors.append(f"{kid} lacks formula or inputs")
        for value in calculation.get("inputs", []):
            if value.get("source_id") not in sources or value.get("passage_id") not in passages:
                errors.append(f"{kid} has unresolved input evidence")
    if not isinstance(run_report, dict) or run_report.get("status") != "success":
        errors.append("run_report.json must declare success")
    usage = run_report.get('usage',{}) if isinstance(run_report,dict) else {}
    calls = usage.get('external_api_calls',{}) if isinstance(usage,dict) else {}
    if not isinstance(calls,dict):calls={};errors.append('provider counts must be an object')
    for name in ("gateway", "serper", "web_retrieval"):
        if type(calls.get(name)) is not int or calls[name] < 0:
            errors.append(f"run_report provider count {name} is invalid")
    if calls.get("deepseek", 0) != 0:
        errors.append("DeepSeek is not an authorized runtime provider")
    if type(calls.get("gateway")) is int and calls['gateway'] > 300:
        errors.append("GATEWAY request budget exceeded")
    word_count = len(re.findall(r"\b[\w'-]+\b", re.sub(r"\[[^]]+\]\([^)]*\)", "", report)))
    if word_count < 900:
        warnings.append(f"report appears short: {word_count} words")
    return {
        "valid": not errors,
        "gate": "artifact_validation_passed" if not errors else "artifact_validation_failed",
        "protocol_version": PROTOCOL_VERSION,
        "case_id": case_id_from_input(case_input),
        "checks": {"word_count": word_count, "source_count": len(sources), "claim_count": len(claims), "passage_count": len(passages), "calculation_count": len(calculations)},
        "errors": errors,
        "warnings": warnings,
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--case-input", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--evaluator-dir", type=Path, default=Path(__file__).resolve().parents[1])
    parser.add_argument("--json-out", type=Path)
    args = parser.parse_args()
    result = validate(args.case_input.resolve(), args.output.resolve(), args.evaluator_dir.resolve())
    if args.json_out:
        write_json(args.json_out, result)
    print(json.dumps(result, indent=2))
    return 0 if result["valid"] else 1


if __name__ == "__main__":
    sys.exit(main())
