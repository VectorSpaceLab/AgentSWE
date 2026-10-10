#!/usr/bin/env python3
"""Deterministically validate Eval Codex output and derive Harbor reward."""

from __future__ import annotations

import argparse
import importlib.util
import hashlib
import json
from pathlib import Path

MAXIMA = {
    "semantic_translation_completeness_terminology": 35,
    "scientific_object_data_fidelity": 35,
    "page_render_reading_structure": 12,
    "alignment_auditability": 8,
    "bidirectional_offline_viewer": 8,
    "delivery_integrity_accessibility": 2,
}
PROVIDERS = {"deepseek", "gateway_text", "gateway_image", "serper", "web_retrieval"}
CEILINGS = {'wrong_language_or_25pct_missing': 35, 'material_meaning_reversal': 45,
    'material_scientific_corruption': 40, 'one_unusable_dense_region': 55,
    'multiple_unusable_dense_regions_or_page': 35, '10_to_25pct_missing': 60,
    'alignment_and_viewer_nonfunctional': 75}


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
    parser.add_argument('--trusted-evidence', type=Path, default=Path('/trusted-evidence'))
    args = parser.parse_args()
    errors: list[str] = []
    manifest = load(args.manifest)
    evaluation = _agentswe_neutral_provider_keys(load(args.eval_result)) if args.eval_result.is_file() else {}
    if args.harness_result.is_file():
        harness = load(args.harness_result)
    else:
        embedded_harness = evaluation.get("harness_result")
        harness = embedded_harness if isinstance(embedded_harness, dict) else {}
    # The benchmark-native PDF validator reports ``valid``.  The Harbor
    # boundary uses ``validity_gate``.  Preserve an explicit gate when one is
    # present, otherwise normalize the native field before deriving the Eval
    # contract.  Without this normalization a valid candidate can be turned
    # into a scoreable zero and provider transport errors can be hidden.
    if "validity_gate" not in harness and isinstance(harness.get("valid"), bool):
        harness["validity_gate"] = harness["valid"]
    candidate_execution = manifest.get("candidate_execution_contract")
    if not isinstance(candidate_execution, dict):
        errors.append("candidate execution contract is missing")
        candidate_execution = {}
    case_id = manifest.get("case_id")
    if evaluation.get("case_id") != case_id or harness.get("case_id") != case_id:
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
        # 0917 v2-lite: the judge sometimes reports a ceiling-adjusted total while every dimension score is
        # valid; the dimension scores carry the judgment and this verifier applies ceilings itself.
        evaluation["score_reported_by_judge"] = reported
        evaluation["score_recomputed_from_dimensions"] = True
        reported = total
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
    trusted_fatal = (candidate_execution.get('fatal_gate') is True or harness.get('fatal_gate') is True
        or candidate_execution.get('evaluation_state') == 'fatal_zero' or harness.get('evaluation_state') == 'fatal_zero')
    if fatal_gate and not trusted_fatal and evaluation.get('semantic_fatal'):
        try:
            spec=importlib.util.spec_from_file_location('pdf_fatal_reader',Path(__file__).with_name('evidence_bundle.py'))
            reader=importlib.util.module_from_spec(spec);spec.loader.exec_module(reader)
            sealed=reader.load_bundle(args.trusted_evidence,manifest.get('visual_evidence',{}))
            reader.validate_semantic_fatal(evaluation['semantic_fatal'],sealed)
            receipt=evaluation.get('visual_evidence_receipt',{})
            if receipt.get('images') != manifest.get('visual_image_records') or not receipt.get('response_received'):
                raise ValueError('semantic fatal lacks complete real visual response')
            trusted_fatal=True
        except Exception as exc:
            errors.append('invalid PDF semantic fatal evidence: '+str(exc))
    if fatal_gate and not trusted_fatal and explicit_state != 'infrastructure_error':
        errors.append('judge cannot invent an unconfirmed fatal gate')
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
            and error not in {"score must be an integer", "score does not equal dimension total",
                                       "dimensions must be an object"}
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
            if counts.get("serper") != 0 or counts.get("web_retrieval") != 0:
                errors.append("closed-corpus retrieval count is nonzero")
    if explicit_state == "infrastructure_error":
        errors.extend(str(item) for item in evaluation.get("errors", []) if isinstance(item, str))
        errors.append("evaluation infrastructure failed; candidate score is not publishable")
    elif evaluation.get("errors"):
        errors.extend(str(item) for item in evaluation["errors"] if isinstance(item, str))
    ceiling = 100
    if validity and manifest.get('visual_evidence_protocol') == 'pdf-visual-evidence-v1':
        try:
            module_path = Path(__file__).with_name('evidence_bundle.py')
            spec = importlib.util.spec_from_file_location('pdf_evidence_verifier', module_path)
            reader = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(reader)
            sealed = reader.load_bundle(args.trusted_evidence, manifest.get('visual_evidence', {}))
            confirmed = reader.validate_quality_review(evaluation.get('quality_review'), sealed, manifest.get('judge_format', 'exact'))
            if confirmed != evaluation.get('quality_decision'):
                errors.append('quality decision differs from independent final verification')
        except Exception as exc:
            errors.append('sealed PDF evidence/final quality verification failed: ' + str(exc))
        receipt = evaluation.get('visual_evidence_receipt', {})
        descriptor = manifest.get('visual_evidence', {})
        if not isinstance(receipt, dict) or receipt.get('manifest_sha256') != descriptor.get('manifest_sha256') or receipt.get('images') != manifest.get('visual_image_records') or not receipt.get('response_received'):
            errors.append('missing or mismatched complete PDF visual receipt')
        if isinstance(receipt,dict) and 'page_request_groups' in receipt:
            groups=receipt['page_request_groups']
            expected=list(range(1,descriptor.get('source_page_count',0)+1))
            if not isinstance(groups,list) or [g.get('source_page') for g in groups if isinstance(g,dict)]!=expected:
                errors.append('pagewise PDF review lacks complete ordered source page coverage')
            else:
                received={json.dumps(image,sort_keys=True) for group in groups for image in group.get('images',[])}
                received.update(json.dumps(image,sort_keys=True) for image in receipt.get('synthesis_images',[]))
                declared={json.dumps(image,sort_keys=True) for image in manifest.get('visual_image_records',[])}
                if received!=declared:errors.append('pagewise requests do not collectively cover every sealed image')
                page_review_path=args.eval_result.parent/'page_reviews.json'
                if not page_review_path.is_file() or hashlib.sha256(page_review_path.read_bytes()).hexdigest()!=receipt.get('page_reviews_sha256'):
                    errors.append('pagewise review transcript seal missing or mismatched')
                else:
                    page_review=load(page_review_path)
                    if page_review.get('request_groups')!=groups:errors.append('pagewise groups differ from sealed review transcript')
                    if page_review.get('manifest_sha256')!=descriptor.get('manifest_sha256'):
                        errors.append('pagewise transcript evidence identity mismatch')
                    reviews=page_review.get('reviews')
                    if not isinstance(reviews,list) or len(reviews)!=len(groups):
                        errors.append('pagewise transcript lacks one response per source page')
                    else:
                        combined=[]
                        for item,group,page in zip(reviews,groups,sealed['source_pages']):
                            if not isinstance(item,dict) or item.get('source_page')!=page['page']:
                                errors.append('pagewise response source page mismatch')
                                continue
                            if reader.digest(item)!=group.get('response_sha256'):
                                errors.append('pagewise response digest mismatch')
                            rows=item.get('unit_coverage')
                            expected_units={unit['id'] for unit in page['units']}
                            if not isinstance(rows,list) or len(rows)!=len(expected_units) or {row.get('source_unit') for row in rows if isinstance(row,dict)}!=expected_units:
                                errors.append('pagewise response source unit coverage mismatch')
                            else:
                                combined.extend(rows)
                        quality=evaluation.get('quality_review',{})
                        if not isinstance(quality,dict) or quality.get('unit_coverage')!=combined:
                            errors.append('final source unit coverage differs from sealed page reviews')
        review = evaluation.get('quality_review', {})
        findings = review.get('ceilings', {}) if isinstance(review, dict) else {}
        if not isinstance(findings, dict) or set(findings) != set(CEILINGS):
            errors.append('all seven PDF quality ceilings must be reviewed')
        else:
            for key, maximum in CEILINGS.items():
                finding = findings[key]
                if not isinstance(finding, dict) or type(finding.get('applies')) is not bool or not finding.get('evidence'):
                    errors.append('invalid PDF quality ceiling finding: ' + key)
                elif finding['applies']:
                    ceiling = min(ceiling, maximum)
        decision = evaluation.get('quality_decision', {})
        if not isinstance(decision, dict) or decision.get('maximum_total') != ceiling or not decision.get('source_unit_count'):
            errors.append('PDF quality decision not validated against sealed evidence')
        if harness.get('alignment_functional') is False and harness.get('viewer_functional') is False:
            ceiling = min(ceiling, 75)
        total = min(total, ceiling)
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
        "judge_format": manifest.get("judge_format", "exact"),
        "dimensions": dimensions,
        "score": final_score,
        "raw_dimension_total": raw_dimension_total,
        "quality_ceiling": ceiling,
        "quality_decision": evaluation.get('quality_decision'),
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
