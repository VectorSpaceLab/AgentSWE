#!/usr/bin/env python3
"""Separate candidate failures from independently confirmed infrastructure outages."""

from __future__ import annotations

import argparse
import json
import re
from datetime import datetime
from pathlib import Path
from typing import Any

from common import PROTOCOL_VERSION, load_protocol, read_json, write_json


PROVIDER_ERROR = re.compile(r"\b(gateway|serper|search proxy)\b.*\b(408|409|425|429|500|502|503|504|520|522|524|timeout|connection)", re.I)
PROVIDER_FAILURE = re.compile(r"\b(gateway|serper|search proxy)\b.*\b(400|401|403|408|409|425|429|500|502|503|504|520|522|524|timeout|connection)", re.I)


def provider_map(health: dict[str, Any]) -> dict[str, dict[str, Any]]:
    return {item.get("provider"): item for item in health.get("providers", []) if isinstance(item, dict)}


def classify(artifact: dict[str, Any], process: dict[str, Any], health: dict[str, Any], output: Path, evaluator_dir: Path) -> dict[str, Any]:
    protocol = load_protocol(evaluator_dir)
    evidence: list[str] = []
    if not process.get("candidate_launched"):
        return {"protocol_version": PROTOCOL_VERSION, "classification": "harness_invalid", "ability_score": None, "evidence": ["candidate did not launch"]}
    if process.get("timed_out"):
        return {"protocol_version": PROTOCOL_VERSION, "classification": "candidate_failure", "ability_score": 0, "evidence": ["candidate exceeded the case wall limit"]}
    if artifact.get("valid"):
        return {"protocol_version": PROTOCOL_VERSION, "classification": "scoreable_success", "ability_score": "rubric", "evidence": ["complete artifacts passed deterministic validation"], "next_gate": "independent_body_pdf_verification"}
    run_report: dict[str, Any] = {}
    try:
        candidate_report = read_json(output / "run_report.json")
        if isinstance(candidate_report, dict):
            run_report = candidate_report
    except Exception:
        pass
    errors = " ".join(str(item) for item in run_report.get("errors", []))
    calls = (((run_report.get("usage") or {}).get("external_api_calls") or {}))
    providers = provider_map(health)
    confirmed: list[str] = []
    provider_claims: list[str] = []
    health_is_contemporaneous = False
    try:
        probe_time = datetime.fromisoformat(str(health["observed_at"]))
        run_time = datetime.fromisoformat(str(process["start_utc"]))
        health_is_contemporaneous = abs((run_time - probe_time).total_seconds()) <= 900
    except (KeyError, TypeError, ValueError):
        pass
    for provider in ("gateway", "serper"):
        item = providers.get(provider, {})
        attempts = item.get("attempts", [])
        provider_named = provider in errors.lower() or (provider == "serper" and "search proxy" in errors.lower())
        candidate_called = isinstance(calls.get(provider), int) and calls[provider] > 0
        if provider_named and candidate_called and PROVIDER_FAILURE.search(errors):
            provider_claims.append(provider)
        if health_is_contemporaneous and item.get("status") == "unavailable_retryable" and len(attempts) >= protocol["infrastructure_policy"]["minimum_independent_probe_attempts"]:
            if provider_named and candidate_called and PROVIDER_ERROR.search(errors):
                confirmed.append(provider)
    only_error_artifact = set(path.name for path in output.iterdir() if path.is_file()) <= {"run_report.json"}
    if confirmed and only_error_artifact and process.get("exit_code") not in (None, 0):
        evidence.append("candidate and independent health probe agree on a retryable outage: " + ", ".join(confirmed))
        evidence.append("candidate emitted only an honest error-form run report and no research artifacts")
        return {"protocol_version": PROTOCOL_VERSION, "classification": "infrastructure_invalid", "ability_score": None, "evidence": evidence, "required_action": "rerun_after_provider_health_recovers"}
    unobservable_claims: list[str] = []
    for provider in provider_claims:
        item = providers.get(provider, {})
        status = item.get("status")
        codes = [attempt.get("status_code") for attempt in item.get("attempts", []) if isinstance(attempt, dict)]
        if status in {"not_configured", "probe_failed"} or (status == "unavailable_nonretryable" and codes and all(code in {401, 403} for code in codes)):
            unobservable_claims.append(provider)
    if unobservable_claims:
        return {"protocol_version": PROTOCOL_VERSION, "classification": "harness_invalid", "ability_score": None, "evidence": ["provider health could not be independently observed for: " + ", ".join(unobservable_claims)], "required_action": "repair evaluator provider probe and rerun"}
    evidence.append("artifact validation failed without independently confirmed qualifying provider outage")
    return {"protocol_version": PROTOCOL_VERSION, "classification": "candidate_failure", "ability_score": 0, "evidence": evidence}


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--artifact-report", type=Path, required=True)
    parser.add_argument("--process-observation", type=Path, required=True)
    parser.add_argument("--provider-health", type=Path, required=True)
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--evaluator-dir", type=Path, default=Path(__file__).resolve().parents[1])
    parser.add_argument("--json-out", type=Path, required=True)
    args = parser.parse_args()
    result = classify(read_json(args.artifact_report), read_json(args.process_observation), read_json(args.provider_health), args.output_dir.resolve(), args.evaluator_dir.resolve())
    write_json(args.json_out.resolve(), result)
    print(json.dumps(result, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
