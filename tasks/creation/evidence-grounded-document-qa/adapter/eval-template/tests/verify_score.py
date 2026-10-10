#!/usr/bin/env python3
"""Deterministically validate Eval Codex output and derive Harbor reward."""

from __future__ import annotations

import argparse
import hashlib
import json
import math
from pathlib import Path

MAXIMA = {
    "factual_visual_computational_correctness": 28,
    "evidence_coverage_entailment": 22,
    "native_locator_bundle_integrity": 20,
    "revision_uncertainty_abstention": 12,
    "offline_review_interaction_audit_usability": 10,
    "artifact_validity_clarity_compliance": 8,
}
PROVIDERS = {"deepseek", "gateway_text", "gateway_image", "serper", "web_retrieval"}


def valid_score(value: object, maximum: int) -> bool:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return False
    try:
        numeric = float(value)
    except (OverflowError, TypeError, ValueError):
        return False
    return math.isfinite(numeric) and 0.0 <= numeric <= float(maximum)


def canonical_sha256(value: object) -> str:
    return hashlib.sha256(
        json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()


def valid_sha256(value: object) -> bool:
    return isinstance(value, str) and len(value) == 64 and all(
        character in "0123456789abcdef" for character in value
    )


def validate_quality_transcript(
    *, manifest: dict[str, object], evaluation: dict[str, object],
    harness: dict[str, object], eval_result_path: Path,
) -> list[str]:
    errors: list[str] = []
    config = manifest.get("quality_review_protocol")
    if not isinstance(config, dict):
        return errors
    protocol = config.get("name")
    transcript_name = config.get("transcript")
    if protocol != "qa-dimension-split-v1":
        return ["unsupported quality review protocol"]
    if config.get("dimensions") != MAXIMA:
        errors.append("quality review protocol dimensions do not match rubric")
    if transcript_name != "quality_review_transcript.json":
        errors.append("invalid quality review transcript path")
        return errors
    transcript_path = eval_result_path.parent / transcript_name
    if not transcript_path.is_file() or transcript_path.is_symlink():
        return errors + ["quality review transcript is missing"]
    try:
        transcript = load(transcript_path)
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        return errors + [f"quality review transcript is invalid: {type(exc).__name__}"]
    if evaluation.get("quality_review_protocol") != protocol:
        errors.append("evaluation quality review protocol mismatch")
    if hashlib.sha256(transcript_path.read_bytes()).hexdigest() != evaluation.get(
        "quality_review_transcript_sha256"
    ):
        errors.append("quality review transcript hash mismatch")
    identity = transcript.get("identity")
    if not isinstance(identity, dict) or identity != evaluation.get("quality_review_identity"):
        errors.append("quality review identity mismatch")
        identity = {}
    for key, expected in {
        "protocol": protocol,
        "case_id": manifest.get("case_id"),
        "case_digest": manifest.get("case_digest"),
        "candidate_digest": manifest.get("candidate_digest"),
        "candidate_output_digest": manifest.get("candidate_output_digest"),
        "harness_sha256": canonical_sha256(harness),
    }.items():
        if identity.get(key) != expected:
            errors.append(f"quality review identity has invalid {key}")
    inventory = transcript.get("artifact_inventory")
    if not isinstance(inventory, list) or canonical_sha256(inventory) != identity.get(
        "artifact_inventory_sha256"
    ):
        errors.append("quality review artifact inventory hash mismatch")
    base_identity = dict(identity)
    declared_bundle = base_identity.pop("evidence_bundle_sha256", None)
    if not valid_sha256(declared_bundle) or canonical_sha256(base_identity) != declared_bundle:
        errors.append("quality review evidence bundle hash mismatch")

    images = transcript.get("images")
    image_reviews = transcript.get("image_reviews")
    if not isinstance(images, list) or not isinstance(image_reviews, list):
        errors.append("quality review image transcript is malformed")
        images, image_reviews = [], []
    if evaluation.get("image_receipts") != images:
        errors.append("evaluation image receipts differ from transcript")
    image_by_id: dict[object, dict[str, object]] = {}
    for image in images:
        if not isinstance(image, dict):
            errors.append("quality review image record is malformed")
            continue
        image_id = image.get("id")
        if image_id in image_by_id or not isinstance(image_id, str) or not image_id:
            errors.append("quality review image id is missing or duplicated")
            continue
        if not valid_sha256(image.get("sha256")) or type(image.get("bytes")) is not int or image["bytes"] <= 0:
            errors.append(f"quality review image receipt is invalid: {image_id}")
        image_by_id[image_id] = image
    seen_images: set[object] = set()
    image_attempts = 0
    for record in image_reviews:
        if not isinstance(record, dict) or not isinstance(record.get("response"), dict):
            errors.append("quality review image response record is malformed")
            continue
        image = record.get("image")
        response = record["response"]
        image_id = image.get("id") if isinstance(image, dict) else None
        if image_by_id.get(image_id) != image or image_id in seen_images:
            errors.append("quality review image response identity mismatch")
            continue
        seen_images.add(image_id)
        review_id = canonical_sha256({
            "protocol": protocol,
            "evidence_bundle_sha256": declared_bundle,
            "image": image,
        })
        expected = {
            "protocol": protocol,
            "case_id": manifest.get("case_id"),
            "evidence_bundle_sha256": declared_bundle,
            "image_review_id": review_id,
            "image_id": image_id,
            "image_sha256": image.get("sha256"),
        }
        if record.get("image_review_id") != review_id or any(response.get(k) != v for k, v in expected.items()):
            errors.append(f"quality review image binding mismatch: {image_id}")
        if record.get("response_sha256") != canonical_sha256(response):
            errors.append(f"quality review image response hash mismatch: {image_id}")
        receipt = record.get("provider_receipt")
        if not isinstance(receipt, dict) or receipt.get("status") != "completed" or not valid_sha256(receipt.get("response_text_sha256")):
            errors.append(f"quality review image provider receipt is invalid: {image_id}")
        attempts = record.get("request_attempts")
        if type(attempts) is not int or attempts < 1:
            errors.append(f"quality review image attempt count is invalid: {image_id}")
        else:
            image_attempts += attempts
    if seen_images != set(image_by_id):
        errors.append("quality review image responses are incomplete")

    reviews = transcript.get("dimension_reviews")
    if not isinstance(reviews, list):
        errors.append("quality review dimension transcript is malformed")
        reviews = []
    dimensions = evaluation.get("dimensions")
    dimensions = dimensions if isinstance(dimensions, dict) else {}
    seen_dimensions: set[object] = set()
    text_attempts = 0
    for record in reviews:
        if not isinstance(record, dict) or not isinstance(record.get("response"), dict):
            errors.append("quality review dimension response record is malformed")
            continue
        dimension = record.get("dimension")
        response = record["response"]
        maximum = MAXIMA.get(dimension) if isinstance(dimension, str) else None
        if maximum is None or dimension in seen_dimensions:
            errors.append("quality review dimension is unknown or duplicated")
            continue
        seen_dimensions.add(dimension)
        input_images = record.get("input_images")
        if input_images != []:
            errors.append(f"quality review dimension unexpectedly retransmitted images: {dimension}")
            input_images = []
        review_id = canonical_sha256({
            "protocol": protocol,
            "evidence_bundle_sha256": declared_bundle,
            "dimension": dimension,
            "maximum": maximum,
            "input_images": input_images,
        })
        expected = {
            "protocol": protocol,
            "case_id": manifest.get("case_id"),
            "evidence_bundle_sha256": declared_bundle,
            "review_id": review_id,
            "dimension": dimension,
            "max": maximum,
        }
        if record.get("review_id") != review_id or record.get("maximum") != maximum or any(
            response.get(key) != value for key, value in expected.items()
        ):
            errors.append(f"quality review dimension binding mismatch: {dimension}")
        score = response.get("score")
        if not valid_score(score, maximum):
            errors.append(f"quality review dimension score is invalid: {dimension}")
        expected_dimension = {
            "score": score,
            "max": maximum,
            "evidence": "; ".join(response.get("evidence", [])) if isinstance(response.get("evidence"), list) else "",
            "deductions": response.get("deductions"),
            "review_id": review_id,
        }
        if dimensions.get(dimension) != expected_dimension:
            errors.append(f"evaluation dimension differs from transcript: {dimension}")
        if record.get("response_sha256") != canonical_sha256(response):
            errors.append(f"quality review dimension response hash mismatch: {dimension}")
        receipt = record.get("provider_receipt")
        if not isinstance(receipt, dict) or receipt.get("status") != "completed" or not valid_sha256(receipt.get("response_text_sha256")):
            errors.append(f"quality review dimension provider receipt is invalid: {dimension}")
        attempts = record.get("request_attempts")
        if type(attempts) is not int or attempts < 1:
            errors.append(f"quality review dimension attempt count is invalid: {dimension}")
        else:
            text_attempts += attempts
    if seen_dimensions != set(MAXIMA):
        errors.append("quality review dimension responses are incomplete")
    if evaluation.get("quality_review_complete") is not True:
        errors.append("quality review is not declared complete")
    counts = evaluation.get("provider_counts")
    if isinstance(counts, dict) and (
        counts.get("gateway_text") != text_attempts or counts.get("gateway_image") != image_attempts
    ):
        errors.append("quality review provider counts differ from transcript")
    return errors


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
    harness = load(args.harness_result) if args.harness_result.is_file() else {}
    candidate_execution = manifest.get("candidate_execution_contract")
    if not isinstance(candidate_execution, dict):
        errors.append("candidate execution contract is missing")
        candidate_execution = {}
    case_id = manifest.get("case_id")
    if evaluation.get("case_id") != case_id or harness.get("case") != case_id:
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
        if not valid_score(score, maximum):
            errors.append(f"invalid score for {name}")
        if declared_max != maximum:
            errors.append(f"invalid maximum for {name}")
        if valid_score(score, maximum):
            total += score
            checked += 1
    reported = evaluation.get("score")
    hard_feature_valid = harness.get("hard_feature_valid") is True
    if not valid_score(reported, 100):
        errors.append("score must be a finite number")
    elif checked == len(MAXIMA):
        # When the source-native hard feature is missing, the rubric score is
        # capped at 20, but it may legitimately be lower than that cap.  The
        # previous verifier compared the reported post-ceiling score against
        # the raw dimension total (for example 12 vs 97) and incorrectly
        # invalidated an otherwise well-formed Eval contract.
        if hard_feature_valid:
            if reported != total:
                errors.append("score does not equal dimension total")
        elif reported not in {total, min(total, 20)}:
            # Accept both producer conventions seen in the benchmark: some
            # Eval agents report the raw dimension total and let this verifier
            # apply the ceiling; others report their already-capped score.
            errors.append("score exceeds hard-feature ceiling")
    raw_dimension_total = total if checked == len(MAXIMA) else None
    candidate_execution_valid = candidate_execution.get("validity_gate") is True
    # A judge's success label cannot erase trusted infrastructure failure.
    states = [record.get("evaluation_state") for record in (evaluation, harness, candidate_execution)]
    if evaluation.get('evaluation_state') == 'fatal_zero' and harness.get('validity_gate') is True and candidate_execution.get('validity_gate') is True:
        errors.append('judge invented an unconfirmed fatal gate for deterministic-scoreable artifacts')
    if "infrastructure_error" in states or any(record.get("infrastructure_error") for record in (harness, candidate_execution)):
        explicit_state = "infrastructure_error"
    elif "fatal_zero" in states:
        explicit_state = "fatal_zero"
    else:
        explicit_state = "scoreable" if harness.get("validity_gate") is True else "fatal_zero"
    for state in states:
        if state is not None and state not in ("scoreable", "fatal_zero", "infrastructure_error"):
            errors.append("invalid evaluation_state in scoring evidence")
    credential_leak_detected = (
        harness.get("credential_leak_detected") is True
        or evaluation.get("credential_leak_detected") is True
        or candidate_execution.get("credential_leak_detected") is True
    )
    validity = (
        explicit_state == "scoreable"
        and harness.get("validity_gate") is True
        and candidate_execution_valid
        and not credential_leak_detected
    )
    if validity:
        errors.extend(validate_quality_transcript(
            manifest=manifest,
            evaluation=evaluation,
            harness=harness,
            eval_result_path=args.eval_result,
        ))
    if not validity:
        total = 0
        if raw_dimension_total is None:
            dimensions = {
                name: {
                    "score": 0,
                    "max": maximum,
                    "evidence": "deterministic validity gate failed",
                }
                for name, maximum in MAXIMA.items()
            }
            raw_dimension_total = 0
            errors = [
                error
                for error in errors
                if not error.startswith(
                    ("missing dimension:", "invalid score for", "invalid maximum for")
                )
                and error
                not in {"score must be a finite number", "score does not equal dimension total", "dimensions must be an object"}
            ]
    infrastructure_error = explicit_state == "infrastructure_error"
    if infrastructure_error:
        validity = False
        total = 0
        raw_dimension_total = None
        errors = [
            error for error in errors
            if not error.startswith(("missing dimension:", "invalid score for", "invalid maximum for"))
            and error not in {"score must be a finite number", "score does not equal dimension total"}
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
            combined_llm = (
                counts.get("deepseek", 0)
                + counts.get("gateway_text", 0)
                + counts.get("gateway_image", 0)
            )
            if combined_llm > 300:
                errors.append("combined DeepSeek/GATEWAY request budget exceeded")
            if counts.get("gateway_image", 0) > 100:
                errors.append("image-bearing GATEWAY request budget exceeded")
            if counts.get("serper") != 0 or counts.get("web_retrieval") != 0:
                errors.append("closed-corpus retrieval count is nonzero")
    if validity and evaluation.get("errors"):
        errors.extend(str(item) for item in evaluation["errors"] if isinstance(item, str))
    score_before_hard_feature_ceiling = total
    if validity and valid_score(reported, 100):
        # The Eval agent's reported score is authoritative within the rubric
        # bounds.  The hard-feature rule is a maximum, not an instruction to
        # round every valid result up to 20.
        total = reported
        if not hard_feature_valid:
            total = min(total, 20)
    final_score = total if not errors else 0
    # Infrastructure/invalid contracts have no Candidate score.
    if infrastructure_error or errors:
        final_score = None
    contract = {
        "schema_version": "1.0",
        "case_id": case_id,
        "evaluation_mode": manifest.get("evaluation_mode"),
        "validity_gate": validity,
        "evaluation_state": explicit_state,
        "judge_format": manifest.get("judge_format", "exact"),
        "judge_echo_repairs": evaluation.get("judge_echo_repairs", []),
        "dimensions": dimensions,
        "score": final_score,
        "raw_dimension_total": raw_dimension_total,
        "score_before_hard_feature_ceiling": score_before_hard_feature_ceiling,
        "hard_feature_valid": hard_feature_valid,
        "hard_feature_ceiling_applied": validity and not hard_feature_valid,
        "credential_leak_detected": credential_leak_detected,
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
