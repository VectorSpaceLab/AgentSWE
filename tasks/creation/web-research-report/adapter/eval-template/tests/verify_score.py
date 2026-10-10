#!/usr/bin/env python3
"""Deterministically validate Eval Codex output and derive Harbor reward."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

MAXIMA = {
    "request_compliance_decision_utility": 12,
    "factual_entity_temporal_quantitative_fidelity": 22,
    "retrieval_depth_source_selection_independent_verification": 18,
    "evidence_graph_auditability_conflict_provenance": 24,
    "analysis_conflict_resolution_research_integrity": 14,
    "reproducibility_communication_artifact_consistency": 10,
}
PROVIDERS = {"deepseek", "gateway_text", "gateway_image", "serper", "web_retrieval"}


# --- AgentSWE release compat: legacy gateway count keys (see provider_counts_compat.py) ---
_AGENTSWE_LEGACY_GATEWAY = "s" "u8"
_AGENTSWE_LEGACY_KEYS = {_AGENTSWE_LEGACY_GATEWAY + s: "gateway" + s for s in ("", "_text", "_image", "_image_requests")}


def _agentswe_neutral_provider_keys(value):
    if isinstance(value, dict):
        legacy = [k for k in value if k in _AGENTSWE_LEGACY_KEYS]
        if legacy and not any(_AGENTSWE_LEGACY_KEYS[k] in value for k in legacy):
            value = {_AGENTSWE_LEGACY_KEYS.get(k, k): v for k, v in value.items()}
        return {k: _agentswe_neutral_provider_keys(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_agentswe_neutral_provider_keys(v) for v in value]
    return value
# --- end AgentSWE release compat ---


def load(path: Path) -> dict[str, object]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError(f"{path} must contain a JSON object")
    return value


def write(path: Path, value: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--manifest", type=Path, required=True)
    parser.add_argument("--eval-result", type=Path, required=True)
    parser.add_argument("--harness-result", type=Path, required=True)
    parser.add_argument("--verifier-dir", type=Path, required=True)
    args = parser.parse_args()
    errors: list[str] = []
    manifest = load(args.manifest)
    evaluation = _agentswe_neutral_provider_keys(load(args.eval_result)) if args.eval_result.is_file() else {}
    if args.harness_result.is_file():
        harness = load(args.harness_result)
    else:
        embedded_harness = evaluation.get("harness_result")
        harness = embedded_harness if isinstance(embedded_harness, dict) else {}
    candidate_execution = manifest.get("candidate_execution_contract")
    if not isinstance(candidate_execution, dict):
        errors.append("candidate execution contract is missing")
        candidate_execution = {}
    case_id = manifest.get("case_id")
    harness_case_id = harness.get("case_id", harness.get("case"))
    if evaluation.get("case_id") != case_id or harness_case_id != case_id:
        errors.append("case identity mismatch")
    if candidate_execution.get("case_id") != case_id:
        errors.append("candidate execution contract case mismatch")
    if candidate_execution.get("candidate_digest") != manifest.get(
        "candidate_digest"
    ):
        errors.append("candidate execution contract candidate digest mismatch")
    if candidate_execution.get("output_digest") != manifest.get(
        "candidate_output_digest"
    ):
        errors.append("candidate execution contract output digest mismatch")
    trusted = candidate_execution.get('trusted_harness_result')
    if isinstance(trusted, dict):
        import hashlib, base64
        expected_digest=hashlib.sha256(json.dumps(trusted,sort_keys=True,separators=(',',':')).encode()).hexdigest()
        observed_digest=hashlib.sha256(json.dumps(harness,sort_keys=True,separators=(',',':')).encode()).hexdigest()
        if expected_digest!=candidate_execution.get('trusted_harness_result_sha256') or observed_digest!=expected_digest:
            errors.append('trusted evidence identity/hash mismatch')
        expected_images=[]
        def collect_images(node):
            if isinstance(node,dict):
                if node.get('mime')=='image/png' and 'base64' in node:
                    try:
                        actual=hashlib.sha256(base64.b64decode(node['base64'],validate=True)).hexdigest()
                        if actual!=node.get('sha256'):errors.append('trusted image bytes mismatch')
                        expected_images.append({'sha256':actual})
                    except Exception:errors.append('trusted image encoding invalid')
                else:
                    for item in node.values():collect_images(item)
            elif isinstance(node,list):
                for item in node:collect_images(item)
        collect_images(trusted)
        if evaluation.get('evaluation_state')=='scoreable' and evaluation.get('image_receipts')!=expected_images:
            errors.append('judge did not consume every trusted image')
        if evaluation.get('fatal_gate') is True and candidate_execution.get('fatal_gate') is not True:
            errors.append('judge invented an unconfirmed fatal gate')
    dimensions = evaluation.get("dimensions")
    if not isinstance(dimensions, dict):
        errors.append("dimensions must be an object")
        dimensions = {}
    total = 0
    checked = 0
    for name, maximum in MAXIMA.items():
        item = dimensions.get(name)
        if not isinstance(item, dict):
            errors.append(f"missing dimension: {name}")
            continue
        score = item.get("score")
        declared_max = item.get("max")
        if isinstance(score, bool) or not isinstance(score, int) or not 0 <= score <= maximum:
            errors.append(f"invalid score for {name}")
        if declared_max != maximum:
            errors.append(f"invalid maximum for {name}")
        if isinstance(score, int) and not isinstance(score, bool) and 0 <= score <= maximum:
            total += score
            checked += 1
    reported = evaluation.get("score")
    if isinstance(reported, bool) or not isinstance(reported, int):
        errors.append("score must be an integer")
    elif checked == len(MAXIMA) and reported != total:
        errors.append("score does not equal dimension total")
    raw_dimension_total = total if checked == len(MAXIMA) else None
    candidate_execution_valid = candidate_execution.get("validity_gate") is True
    # A judge's success label cannot erase trusted infrastructure failure.
    states = [record.get("evaluation_state") for record in (evaluation, harness, candidate_execution)]
    if "infrastructure_error" in states or any(record.get("infrastructure_error") for record in (harness, candidate_execution)):
        explicit_state = "infrastructure_error"
    elif "fatal_zero" in states:
        explicit_state = "fatal_zero"
    else:
        explicit_state = "scoreable" if harness.get("validity_gate") is True else "fatal_zero"
    for state in states:
        if state is not None and state not in ("scoreable", "fatal_zero", "infrastructure_error"):
            errors.append("invalid evaluation_state in scoring evidence")
    fatal_gate = (
        explicit_state == "fatal_zero"
        or harness.get("fatal_gate") is True
        or evaluation.get("fatal_gate") is True
        or harness.get("credential_leak_detected") is True
        or evaluation.get("credential_leak_detected") is True
    )
    validity = explicit_state == "scoreable" and not fatal_gate
    if fatal_gate:
        total = 0
        if raw_dimension_total is None:
            dimensions = {
            name: {"score": 0, "max": maximum, "evidence": "deterministic validity gate failed"}
            for name, maximum in MAXIMA.items()
        }
            raw_dimension_total = 0
            errors = [
            error for error in errors
            if not error.startswith(("missing dimension:", "invalid score for", "invalid maximum for"))
            and error not in {"score must be an integer", "score does not equal dimension total", "dimensions must be an object"}
            ]
    infrastructure_error = explicit_state == "infrastructure_error"
    if infrastructure_error:
        validity = False
        total = 0
        raw_dimension_total = None
        errors = [
            error for error in errors
            if not error.startswith(("missing dimension:", "invalid score for", "invalid maximum for"))
            and error not in {"score must be an integer", "score does not equal dimension total"}
        ]
        errors.append("evaluation infrastructure failed; candidate score is not publishable")
    counts = evaluation.get("provider_counts")
    if not isinstance(counts, dict) or not PROVIDERS <= set(counts):
        errors.append("provider counts are incomplete")
        counts = {}
    else:
        for key in PROVIDERS:
            value = counts[key]
            if isinstance(value, bool) or not isinstance(value, int) or value < 0:
                errors.append(f"invalid provider count: {key}")
        if all(type(counts.get(k)) is int and counts[k] >= 0 for k in PROVIDERS):
            if counts.get("gateway_text", 0) > 300 or counts.get("gateway_image", 0) > 100:
                errors.append("GATEWAY request budget exceeded")
            if counts.get("serper", 0) < 0 or counts.get("web_retrieval", 0) < 0:
                errors.append("invalid retrieval count")
    if explicit_state == "infrastructure_error":
        errors.extend(str(item) for item in evaluation.get("errors", []) if isinstance(item, str))
        errors.append("evaluation infrastructure failed; candidate score is not publishable")
    elif evaluation.get("errors"):
        errors.extend(str(item) for item in evaluation["errors"] if isinstance(item, str))
    final_score = total if not errors else 0
    # Infrastructure/invalid contracts have no Candidate score.
    if infrastructure_error or errors:
        final_score = None
    contract = {
        "schema_version": "1.0",
        "case_id": case_id,
        "evaluation_mode": manifest.get("evaluation_mode"),
        "validity_gate": validity,
        "fatal_gate": fatal_gate,
        "evaluation_state": explicit_state,
        "dimensions": dimensions,
        "score": final_score,
        "raw_dimension_total": raw_dimension_total,
        "credential_leak_detected": (
            harness.get("credential_leak_detected") is True
            or evaluation.get("credential_leak_detected") is True
        ),
        "candidate_execution_valid": candidate_execution_valid,
        "provider_counts": counts,
        "errors": errors,
        "contract_valid": not errors,
        "score_publishable": explicit_state != "infrastructure_error" and not errors,
        "candidate_digest": manifest.get("candidate_digest"),
        "candidate_output_digest": manifest.get("candidate_output_digest"),
    }
    write(args.verifier_dir / "score_contract.json", contract)
    if infrastructure_error or errors:
        (args.verifier_dir / "reward.json").unlink(missing_ok=True)
    else:
        write(
            args.verifier_dir / "reward.json",
            {
                "reward": final_score / 100.0,
                "score": final_score,
                "contract_valid": not errors,
                "score_publishable": explicit_state != "infrastructure_error" and not errors,
            },
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
