#!/usr/bin/env python3
"""Run the independent Eval Codex call and persist only non-secret evidence."""

from __future__ import annotations

import argparse
import base64
import hashlib
import http.client
import json
import math
import os
import re
try:
    import requests
except ImportError:
    from pip._vendor import requests
import ssl
import subprocess
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path


ENDPOINT = "https://api.deepseek.com/v1/responses"
MODEL = "deepseek-flash"
REASONING_EFFORT = "max"
MAX_TEXT_REQUESTS = 300
MAX_TRANSPORT_ATTEMPTS = 5
MAX_PROMPT_ARTIFACT_CHARS = 4_000_000
QUALITY_REVIEW_PROTOCOL = "qa-dimension-split-v1"


# --- AgentSWE release judge format (eval manifest "judge_format"; absent = exact, the paper's rule) ---
JUDGE_FORMAT = "exact"
JUDGE_ECHO_REPAIRS: list[dict[str, object]] = []


def bind_request_identity(response: dict[str, object], expected: dict[str, object], kind: str) -> None:
    """Identity fields of a review are the single-shot request's own values.

    exact: the judge must echo every field verbatim (paper protocol). release: a missing or wrong echo is
    recorded in judge_echo_repairs and the request's value is used; a judge-supplied identity never wins.
    """
    for key, value in expected.items():
        if response.get(key) == value:
            continue
        if JUDGE_FORMAT != "release":
            raise ValueError(f"{kind} response has invalid {key}")
        JUDGE_ECHO_REPAIRS.append({"review": kind, "field": key,
                                   "echo": "missing" if key not in response else "wrong",
                                   "echoed": None if key not in response else str(response.get(key))[:80]})
        response[key] = value
# --- end AgentSWE release judge format ---
DIMENSION_MAXIMA = {
    "factual_visual_computational_correctness": 28,
    "evidence_coverage_entailment": 22,
    "native_locator_bundle_integrity": 20,
    "revision_uncertainty_abstention": 12,
    "offline_review_interaction_audit_usability": 10,
    "artifact_validity_clarity_compliance": 8,
}


class EvalRequestError(RuntimeError):
    def __init__(self, message: str, attempts: int):
        super().__init__(message)
        self.attempts = attempts


# A judge reports an infrastructure failure in prose only with the standalone token
# INFRASTRUCTURE_ERROR or INFRASTRUCTURE_INVALID (any case, no letter, digit or underscore on
# either side). Prose such as "evaluator-side infrastructure errors" and longer identifiers such
# as INFRASTRUCTURE_ERRORS are not markers; the structured evaluation_state still is.
INFRASTRUCTURE_MARKER = re.compile(r"(?<!\w)INFRASTRUCTURE_(?:ERROR|INVALID)(?!\w)", re.IGNORECASE)


def normalize_evaluation_state(
    model_result: dict[str, object],
    harness: dict[str, object],
    transport_errors: list[str],
) -> str:
    """Make infrastructure classification deterministic and non-scoreable."""
    if transport_errors or harness.get("evaluation_state") == "infrastructure_error":
        return "infrastructure_error"
    assessment = str(model_result.get("assessment", "")).upper()
    major_errors = model_result.get("major_errors", [])
    marker_text = "\n".join(
        [assessment]
        + [str(item).upper() for item in major_errors if isinstance(item, str)]
    )
    if INFRASTRUCTURE_MARKER.search(marker_text):
        return "infrastructure_error"
    state = model_result.get("evaluation_state")
    if state in {"scoreable", "fatal_zero", "infrastructure_error"}:
        return str(state)
    fallback = harness.get("evaluation_state", "scoreable")
    return str(fallback) if fallback in {"scoreable", "fatal_zero"} else "scoreable"


def write_json(path: Path, value: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def redact_text(text: str, values: tuple[str, ...]) -> str:
    for value in values:
        text = text.replace(value, "[REDACTED_RESOURCE_SECRET]")
    return text


def redact_object(value: object, secrets: tuple[str, ...]) -> object:
    if isinstance(value, str):
        return redact_text(value, secrets)
    if isinstance(value, list):
        return [redact_object(item, secrets) for item in value]
    if isinstance(value, dict):
        return {
            redact_text(str(key), secrets): redact_object(item, secrets)
            for key, item in value.items()
        }
    return value


def redact_file(path: Path, secrets: tuple[str, ...]) -> None:
    if not path.is_file():
        return
    if path.stat().st_size > 16 * 1024 * 1024:
        path.write_text(
            "[EVALUATOR LOG OMITTED: exceeded 16 MiB safety limit]\n",
            encoding="utf-8",
        )
        return
    text = path.read_text(encoding="utf-8", errors="replace")
    path.write_text(redact_text(text, secrets), encoding="utf-8")


def tree_contains_secret(root: Path, secrets: tuple[str, ...]) -> bool:
    encoded = tuple(value.encode("utf-8") for value in secrets if value)
    if not encoded:
        return False
    for path in root.rglob("*"):
        if path.is_symlink() or not path.is_file():
            continue
        overlap = max(len(value) for value in encoded) - 1
        tail = b""
        with path.open("rb") as handle:
            while chunk := handle.read(1024 * 1024):
                data = tail + chunk
                if any(value in data for value in encoded):
                    return True
                tail = data[-overlap:] if overlap else b""
    return False


def tree_text(root: Path, secrets: tuple[str, ...]) -> str:
    pieces: list[str] = []
    used = 0
    for path in sorted(root.rglob("*"), key=lambda p: p.relative_to(root).as_posix()):
        if path.is_symlink():
            piece = (
                f"\n--- {path.relative_to(root)} ---\n"
                "[UNSAFE SYMLINK OMITTED]"
            )
            pieces.append(piece)
            used += len(piece)
        elif path.is_file() and path.stat().st_size <= 2_000_000:
            if path.name in {'review.html', 'source_bundle.json'}:
                pieces.append(f'\n--- {path.relative_to(root)} ---\n[Original bytes and offline review DOM were independently checked; see trusted source evidence and browser interaction observations.]\n')
                continue
            content = path.read_text(encoding="utf-8", errors="replace")
            piece = (
                f"\n--- {path.relative_to(root)} ---\n"
                + redact_text(content, secrets)
            )
            if used + len(piece) > MAX_PROMPT_ARTIFACT_CHARS:
                remaining = max(0, MAX_PROMPT_ARTIFACT_CHARS - used)
                pieces.append(piece[:remaining])
                pieces.append("\n[ARTIFACT PROMPT LIMIT REACHED]\n")
                break
            pieces.append(piece)
            used += len(piece)
        elif path.is_file():
            piece = (
                f"\n--- {path.relative_to(root)} ---\n"
                f"[FILE OMITTED: {path.stat().st_size} bytes]\n"
            )
            pieces.append(piece)
            used += len(piece)
        if used >= MAX_PROMPT_ARTIFACT_CHARS:
            pieces.append("\n[ARTIFACT PROMPT LIMIT REACHED]\n")
            break
    return "".join(pieces)


def unsafe_output_entries(root: Path) -> list[str]:
    return [
        path.relative_to(root).as_posix()
        for path in root.rglob("*")
        if path.is_symlink() or (not path.is_file() and not path.is_dir())
    ]


def parse_dotenv(path: Path) -> dict[str, str]:
    values: dict[str, str] = {}
    if path.is_file():
        for raw in path.read_text(encoding="utf-8", errors="replace").splitlines():
            line = raw.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, value = line.split("=", 1)
            values[key.strip()] = value.strip().strip("\"'")
    return values


def response_text(body: object) -> str:
    if isinstance(body, dict):
        direct = body.get("output_text")
        if isinstance(direct, str) and direct.strip():
            return direct
        output = body.get("output")
        if isinstance(output, list):
            chunks: list[str] = []
            stream_failed = None  # 0916 sse-retry
            for item in output:
                if not isinstance(item, dict):
                    continue
                content = item.get("content")
                if isinstance(content, list):
                    for part in content:
                        if isinstance(part, dict) and isinstance(part.get("text"), str):
                            chunks.append(part["text"])
            if chunks:
                return "".join(chunks)
    raise ValueError("GATEWAY response did not contain output text")


def strict_json(text: str) -> dict[str, object]:
    cleaned = text.strip()
    fenced = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", cleaned, flags=re.S)
    if fenced:
        cleaned = fenced.group(1)
    else:
        start, end = cleaned.find("{"), cleaned.rfind("}")
        if start >= 0 and end > start:
            cleaned = cleaned[start : end + 1]
    value = json.loads(cleaned)
    if not isinstance(value, dict):
        raise ValueError("Eval response JSON must be an object")
    return value


def eval_ca_bundle(prefix: Path) -> Path:
    candidates = [
        *sorted((prefix / "lib").glob("python*/site-packages/certifi/cacert.pem")),
        *sorted((prefix / "lib").glob("python*/site-packages/pip/_vendor/certifi/cacert.pem")),
    ]
    if not candidates:
        raise EvalRequestError("Eval CA bundle is unavailable", 0)
    return candidates[0]


def eval_provider_required(harness: dict[str, object]) -> bool:
    """Only rubric-score artifacts that passed the deterministic gate."""
    return harness.get("validity_gate") is True


def run_harness(args: argparse.Namespace, case_dir: Path, output_dir: Path) -> dict[str, object]:
    # Browser execution belongs only to the credential-free offline verifier.
    # The host supplies its identity-bound evidence, never a Candidate file.
    manifest = json.loads(args.manifest.read_text())
    execution = manifest.get('candidate_execution_contract', {})
    if not isinstance(execution, dict):
        raise RuntimeError('trusted QA verifier contract missing')
    for key in ('candidate_digest', 'candidate_output_digest', 'case_digest'):
        if not isinstance(manifest.get(key), str) or not re.fullmatch('[0-9a-f]{64}', manifest[key]):
            raise RuntimeError('missing or malformed trusted identity: ' + key)
    if execution.get('case_digest') != manifest['case_digest']:
        raise RuntimeError('trusted QA verifier active-case digest mismatch')
    value = execution.get('trusted_harness_result')
    if not isinstance(value, dict) or value.get('case') != case_dir.name:
        raise RuntimeError('trusted QA verifier result missing or case mismatch')
    digest = hashlib.sha256(json.dumps(value, sort_keys=True, separators=(',', ':')).encode()).hexdigest()
    if digest != execution.get('trusted_harness_result_sha256'):
        raise RuntimeError('trusted QA verifier evidence digest mismatch')
    if execution.get('candidate_digest') != manifest.get('candidate_digest') or execution.get('output_digest') != manifest.get('candidate_output_digest'):
        raise RuntimeError('trusted QA verifier delivery/output identity mismatch')
    if value.get('evaluation_state') == 'infrastructure_error':
        raise RuntimeError('trusted QA verifier infrastructure failed')
    write_json(output_dir / 'harness_result.json', value)
    return value


def canonical_sha256(value: object) -> str:
    encoded = json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def artifact_inventory(root: Path) -> list[dict[str, object]]:
    return [
        {
            "path": path.relative_to(root).as_posix(),
            "bytes": path.stat().st_size,
            "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
        }
        for path in sorted(root.rglob("*"), key=lambda item: item.relative_to(root).as_posix())
        if path.is_file() and not path.is_symlink()
    ]


def prompt_files(root: Path, names: tuple[str, ...], secrets: tuple[str, ...]) -> str:
    pieces: list[str] = []
    for name in names:
        path = root / name
        if not path.is_file() or path.is_symlink():
            pieces.append(f"\n--- {name} ---\n[MISSING OR UNSAFE FILE]\n")
            continue
        pieces.append(
            f"\n--- {name} ---\n"
            + redact_text(path.read_text(encoding="utf-8", errors="replace"), secrets)
        )
    return "".join(pieces)


def qa_image_catalog(harness: dict[str, object]) -> list[dict[str, object]]:
    """Return evaluator-owned image bytes plus public, hash-only receipts."""
    catalog: list[dict[str, object]] = []
    for index, source in enumerate(harness.get("trusted_source_evidence", [])):
        if not isinstance(source, dict):
            continue
        data_url = source.get("image_data_url")
        if not isinstance(data_url, str) or not data_url.startswith("data:"):
            continue
        match = re.fullmatch(r"data:([^;,]+);base64,(.*)", data_url, flags=re.S)
        if not match:
            raise ValueError("trusted source image is not a base64 data URL")
        raw = base64.b64decode(match.group(2), validate=True)
        catalog.append({
            "id": f"source:{source.get('source_id', index)}",
            "role": "trusted_source_image",
            "source_id": source.get("source_id"),
            "mime_type": match.group(1),
            "bytes": len(raw),
            "sha256": hashlib.sha256(raw).hexdigest(),
            "image_url": data_url,
        })
    browser = harness.get("checks", {}).get("browser", {})
    if isinstance(browser, dict):
        for index, screenshot in enumerate(browser.get("screenshots", [])):
            if not isinstance(screenshot, dict):
                continue
            encoded = screenshot.get("bytes_base64")
            mime = screenshot.get("mime_type")
            if not isinstance(encoded, str) or not isinstance(mime, str):
                raise ValueError("trusted browser screenshot is missing encoded bytes")
            raw = base64.b64decode(encoded, validate=True)
            catalog.append({
                "id": f"browser:{screenshot.get('evidence_id', index)}:{index}",
                "role": "offline_review_screenshot",
                "source_id": screenshot.get("source_id"),
                "evidence_id": screenshot.get("evidence_id"),
                "mime_type": mime,
                "bytes": len(raw),
                "sha256": hashlib.sha256(raw).hexdigest(),
                "image_url": f"data:{mime};base64,{encoded}",
            })
    return catalog


def public_image_record(item: dict[str, object]) -> dict[str, object]:
    return {key: value for key, value in item.items() if key != "image_url"}


def images_for_dimension(
    dimension: str, catalog: list[dict[str, object]]
) -> list[dict[str, object]]:
    # Each actual image is inspected once above. Those hash-bound observations
    # are then shared unchanged across all rubric dimensions, avoiding repeated
    # image charges and preventing image transport from changing point weights.
    return []


def prompt_harness_view(
    harness: dict[str, object], dimension: str, catalog: list[dict[str, object]]
) -> dict[str, object]:
    view = json.loads(json.dumps(harness))
    contract = view.get("candidate_execution_contract")
    if isinstance(contract, dict):
        contract.pop("trusted_harness_result", None)
    for source in view.get("trusted_source_evidence", []):
        if isinstance(source, dict) and source.get("image_data_url"):
            source["image_data_url"] = "[attached only to the relevant dimension request]"
    browser = view.get("checks", {}).get("browser", {})
    if isinstance(browser, dict):
        browser["screenshots"] = [
            public_image_record(item)
            for item in catalog
            if item.get("role") == "offline_review_screenshot"
        ]
        interactions = browser.get("interaction_checks", {})
        pixels = browser.get("pixel_checks", [])
        if dimension != "offline_review_interaction_audit_usability":
            browser["interaction_checks"] = {
                "claim_count": len(interactions.get("claimChecks", []))
                if isinstance(interactions, dict) else 0,
                "evidence_count": len(interactions.get("evidenceChecks", []))
                if isinstance(interactions, dict) else 0,
                "all_records_retained_in_hash_bound_offline_harness": True,
            }
            browser["pixel_checks"] = {
                "count": len(pixels) if isinstance(pixels, list) else 0,
                "failed": sum(
                    1 for item in pixels
                    if isinstance(item, dict) and item.get("visible") is not True
                ) if isinstance(pixels, list) else 0,
                "all_records_retained_in_hash_bound_offline_harness": True,
            }
    return view


def dimension_artifacts(
    root: Path, dimension: str, secrets: tuple[str, ...]
) -> str:
    names = {
        "factual_visual_computational_correctness": (
            "answer.md", "claims_and_citations.json", "run_report.json",
        ),
        "evidence_coverage_entailment": (
            "answer.md", "claims_and_citations.json",
        ),
        "native_locator_bundle_integrity": (
            "claims_and_citations.json", "source_bundle.json", "review_manifest.json",
        ),
        "revision_uncertainty_abstention": (
            "answer.md", "claims_and_citations.json",
        ),
        "offline_review_interaction_audit_usability": (
            "claims_and_citations.json", "review_manifest.json",
        ),
        "artifact_validity_clarity_compliance": (
            "answer.md", "claims_and_citations.json", "source_bundle.json",
            "review_manifest.json", "run_report.json",
        ),
    }[dimension]
    return prompt_files(root, names, secrets)


def evidence_identity(
    manifest: dict[str, object], harness: dict[str, object], inventory: list[dict[str, object]]
) -> dict[str, object]:
    identity = {
        "protocol": QUALITY_REVIEW_PROTOCOL,
        "case_id": manifest.get("case_id"),
        "case_digest": manifest.get("case_digest"),
        "candidate_digest": manifest.get("candidate_digest"),
        "candidate_output_digest": manifest.get("candidate_output_digest"),
        "harness_sha256": canonical_sha256(harness),
        "artifact_inventory_sha256": canonical_sha256(inventory),
    }
    identity["evidence_bundle_sha256"] = canonical_sha256(identity)
    return identity


def dimension_review_id(
    identity: dict[str, object], dimension: str, maximum: int,
    image_records: list[dict[str, object]],
) -> str:
    return canonical_sha256({
        "protocol": QUALITY_REVIEW_PROTOCOL,
        "evidence_bundle_sha256": identity["evidence_bundle_sha256"],
        "dimension": dimension,
        "maximum": maximum,
        "input_images": image_records,
    })


def image_review_id(identity: dict[str, object], image_record: dict[str, object]) -> str:
    return canonical_sha256({
        "protocol": QUALITY_REVIEW_PROTOCOL,
        "evidence_bundle_sha256": identity["evidence_bundle_sha256"],
        "image": image_record,
    })


def validate_image_response(
    response: dict[str, object], *, case_id: str, identity: dict[str, object],
    image_record: dict[str, object], review_id: str,
) -> dict[str, object]:
    expected = {
        "protocol": QUALITY_REVIEW_PROTOCOL,
        "case_id": case_id,
        "evidence_bundle_sha256": identity["evidence_bundle_sha256"],
        "image_review_id": review_id,
        "image_id": image_record["id"],
        "image_sha256": image_record["sha256"],
    }
    bind_request_identity(response, expected, "image")
    response["visible_observations"] = normalize_string_list(
        response.get("visible_observations"),
        field="image response observations", require_nonempty=True,
    )
    response["defects"] = normalize_string_list(
        response.get("defects"), field="image response defects",
        require_nonempty=False,
    )
    for key in ("readability", "locator_assessment"):
        if not isinstance(response.get(key), str) or not str(response[key]).strip():
            raise ValueError(f"image response {key} must be nonempty")
    return response


def validate_dimension_response(
    response: dict[str, object], *, case_id: str, dimension: str, maximum: int,
    identity: dict[str, object], review_id: str,
) -> dict[str, object]:
    expected = {
        "protocol": QUALITY_REVIEW_PROTOCOL,
        "case_id": case_id,
        "evidence_bundle_sha256": identity["evidence_bundle_sha256"],
        "review_id": review_id,
        "dimension": dimension,
        "max": maximum,
    }
    bind_request_identity(response, expected, "dimension")
    score = response.get("score")
    if not _valid_score(score, maximum):
        raise ValueError("dimension response score is outside rubric bounds")
    # The source rubric specifies bounds but does not require integer
    # granularity. Canonicalize integral floats while preserving justified
    # fractional scores in the hash-bound transcript.
    response["score"] = _canonical_score(score)
    evidence = normalize_string_list(
        response.get("evidence"), field="dimension response evidence",
        require_nonempty=True,
    )
    deductions = normalize_string_list(
        response.get("deductions"), field="dimension response deductions",
        require_nonempty=False,
    )
    major_errors = normalize_string_list(
        response.get("major_errors"), field="dimension response major_errors",
        require_nonempty=False,
    )
    response["evidence"] = evidence
    response["deductions"] = deductions
    response["major_errors"] = major_errors
    assessment = response.get("assessment")
    if not isinstance(assessment, str) or not assessment.strip():
        raise ValueError("dimension response assessment must be nonempty")
    return response


def _valid_score(value: object, maximum: int) -> bool:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return False
    try:
        numeric = float(value)
    except (OverflowError, TypeError, ValueError):
        return False
    return math.isfinite(numeric) and 0.0 <= numeric <= float(maximum)


def _canonical_score(value: int | float) -> int | float:
    numeric = float(value)
    return int(numeric) if numeric.is_integer() else numeric


def normalize_string_list(value: object, *, field: str, require_nonempty: bool) -> list[str]:
    if not isinstance(value, list) or (require_nonempty and not value):
        qualifier = "nonempty " if require_nonempty else ""
        raise ValueError(f"{field} must be a {qualifier}list")
    normalized: list[str] = []
    for item in value:
        if isinstance(item, str):
            text = item.strip()
        elif isinstance(item, (dict, list)):
            text = json.dumps(item, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        else:
            raise ValueError(f"{field} entries must be strings or structured objects")
        if not text:
            raise ValueError(f"{field} entries must be nonempty")
        normalized.append(text)
    return normalized


def valid_sha256(value: object) -> bool:
    return isinstance(value, str) and bool(re.fullmatch(r"[0-9a-f]{64}", value))


def resume_transcript(
    path: Path | None, *, identity: dict[str, object],
    inventory: list[dict[str, object]], images: list[dict[str, object]],
) -> tuple[dict[str, object], int, int]:
    empty: dict[str, object] = {
        "schema_version": "1.0", "protocol": QUALITY_REVIEW_PROTOCOL,
        "identity": identity, "artifact_inventory": inventory,
        "images": images, "image_reviews": [], "dimension_reviews": [],
    }
    if path is None:
        return empty, 0, 0
    if not path.is_file() or path.is_symlink():
        raise ValueError("resume quality transcript is missing or unsafe")
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError("resume quality transcript must be an object")
    for key, expected in {
        "schema_version": "1.0", "protocol": QUALITY_REVIEW_PROTOCOL,
        "identity": identity, "artifact_inventory": inventory, "images": images,
    }.items():
        if value.get(key) != expected:
            raise ValueError(f"resume quality transcript has invalid {key}")
    image_records = value.get("image_reviews")
    dimension_records = value.get("dimension_reviews")
    if not isinstance(image_records, list) or not isinstance(dimension_records, list):
        raise ValueError("resume quality transcript review lists are malformed")
    catalog = {str(item["id"]): item for item in images}
    seen_images: set[str] = set()
    image_attempts = 0
    for record in image_records:
        if not isinstance(record, dict) or not isinstance(record.get("response"), dict):
            raise ValueError("resume image review record is malformed")
        image = record.get("image")
        if not isinstance(image, dict) or image != catalog.get(str(image.get("id"))):
            raise ValueError("resume image review inventory binding mismatch")
        image_id = str(image["id"])
        if image_id in seen_images:
            raise ValueError("resume image review is duplicated")
        seen_images.add(image_id)
        expected_id = image_review_id(identity, image)
        response = validate_image_response(
            record["response"], case_id=str(identity["case_id"]),
            identity=identity, image_record=image, review_id=expected_id,
        )
        if record.get("image_review_id") != expected_id:
            raise ValueError("resume image review id mismatch")
        if record.get("response_sha256") != canonical_sha256(response):
            raise ValueError("resume image response hash mismatch")
        receipt = record.get("provider_receipt")
        if not isinstance(receipt, dict) or receipt.get("status") != "completed" or not valid_sha256(
            receipt.get("response_text_sha256")
        ):
            raise ValueError("resume image provider receipt is invalid")
        attempts = record.get("request_attempts")
        if type(attempts) is not int or attempts < 1:
            raise ValueError("resume image attempt count is invalid")
        image_attempts += attempts
    seen_dimensions: set[str] = set()
    text_attempts = 0
    for record in dimension_records:
        if not isinstance(record, dict) or not isinstance(record.get("response"), dict):
            raise ValueError("resume dimension review record is malformed")
        dimension = record.get("dimension")
        maximum = DIMENSION_MAXIMA.get(dimension) if isinstance(dimension, str) else None
        if maximum is None or dimension in seen_dimensions:
            raise ValueError("resume dimension review is unknown or duplicated")
        seen_dimensions.add(dimension)
        input_images = record.get("input_images")
        if input_images != []:
            raise ValueError("resume dimension unexpectedly contains image retransmission")
        expected_id = dimension_review_id(identity, dimension, maximum, [])
        response = validate_dimension_response(
            record["response"], case_id=str(identity["case_id"]),
            dimension=dimension, maximum=maximum, identity=identity,
            review_id=expected_id,
        )
        if record.get("review_id") != expected_id or record.get("maximum") != maximum:
            raise ValueError("resume dimension review id mismatch")
        if record.get("response_sha256") != canonical_sha256(response):
            raise ValueError("resume dimension response hash mismatch")
        receipt = record.get("provider_receipt")
        if not isinstance(receipt, dict) or receipt.get("status") != "completed" or not valid_sha256(
            receipt.get("response_text_sha256")
        ):
            raise ValueError("resume dimension provider receipt is invalid")
        attempts = record.get("request_attempts")
        if type(attempts) is not int or attempts < 1:
            raise ValueError("resume dimension attempt count is invalid")
        text_attempts += attempts
    return value, text_attempts, image_attempts


def call_gateway(
    prompt: str, api_key: str, timeout: int,
    images: list[dict[str, object]] | None = None,
) -> tuple[dict[str, object], int]:
    call_gateway.last_usage = None
    call_gateway.last_receipt = None
    payload = {
        "model": MODEL,
        "reasoning": {"effort": REASONING_EFFORT},
        "max_output_tokens": 100000,
        "stream": True,
        "input": [{"type": "message", "role": "user", "content": [
            {"type": "input_text", "text": prompt},
            *[
                {"type": "input_image", "image_url": str(item["image_url"])}
                for item in (images or [])
            ],
        ]}],
    }
    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
        "Accept": "text/event-stream",
        "User-Agent": "AgentSWE-Harbor-Eval/1.0",
    }
    last_error: str | None = None
    for attempt in range(MAX_TRANSPORT_ATTEMPTS):
        try:
            response = requests.post(
                ENDPOINT, headers=headers, json=payload,
                timeout=(30, timeout), stream=True,
            )
            if response.status_code >= 400:
                last_error = f"HTTPError:{response.status_code}"
                if response.status_code not in (429, 500, 502, 503, 504, 524):
                    raise EvalRequestError(f"Eval Codex request failed: {last_error}", attempt + 1)
                if attempt + 1 < MAX_TRANSPORT_ATTEMPTS:
                    time.sleep(min(8.0, 1.0 * (2**attempt)))
                continue
            response_headers = getattr(response, "headers", {}) or {}
            content_type = response_headers.get("Content-Type", "")
            if 'text/event-stream' not in content_type and hasattr(response, "json"):
                body = response.json()
                if body.get('status') != 'completed' or body.get('error') or body.get('incomplete_details'):
                    raise EvalRequestError('Eval Codex response did not complete', attempt + 1)
                output_text = response_text(body)
                call_gateway.last_usage = body.get('usage')
                call_gateway.last_receipt = {
                    'model': body.get('model'),
                    'status': body.get('status'),
                    'usage': body.get('usage'),
                    'response_text_sha256': hashlib.sha256(output_text.encode()).hexdigest(),
                    'content_type': content_type,
                }
                return strict_json(output_text), attempt + 1
            chunks: list[str] = []
            completed = False
            stream_failed = None  # 0916 sse-retry (init)
            for raw_line in response.iter_lines(decode_unicode=True):
                line = (raw_line or "").strip()
                if not line.startswith("data:"):
                    continue
                data = line[5:].strip()
                if not data or data == "[DONE]":
                    continue
                event = json.loads(data)
                if not isinstance(event, dict):
                    continue
                event_type = str(event.get("type", ""))
                if event_type == "response.output_text.delta":
                    delta = event.get("delta")
                    if isinstance(delta, str):
                        chunks.append(delta)
                elif event_type == "response.completed":
                    response_body = event.get("response", {})
                    if not isinstance(response_body, dict) or response_body.get("status") != "completed":
                        raise EvalRequestError("Eval completed event is invalid", attempt + 1)
                    completed = True
                    call_gateway.last_usage = response_body.get("usage")
                elif event_type in {"response.failed", "response.incomplete", "error"}:
                    # 0916 sse-retry: an SSE failure event is a provider/transport fault; retry like HTTP 5xx.
                    stream_failed = event_type
                    break
            if stream_failed or not completed:
                last_error = 'stream:' + str(stream_failed or 'incomplete')
                try:
                    response.close()
                except Exception:
                    pass
                if attempt + 1 < MAX_TRANSPORT_ATTEMPTS:
                    time.sleep(min(8.0, 1.0 * (2**attempt)))
                continue
            output_text = "".join(chunks)
            call_gateway.last_receipt = {
                'model': MODEL,
                'status': 'completed',
                'usage': call_gateway.last_usage,
                'response_text_sha256': hashlib.sha256(output_text.encode()).hexdigest(),
                'content_type': content_type,
            }
            return strict_json(output_text), attempt + 1
        except EvalRequestError:
            raise
        except (requests.RequestException, json.JSONDecodeError, ValueError, TimeoutError) as exc:
            last_error = type(exc).__name__
            if attempt + 1 < MAX_TRANSPORT_ATTEMPTS:
                time.sleep(min(8.0, 1.0 * (2**attempt)))
    raise EvalRequestError(
        f"Eval Codex request failed after retries: {last_error}",
        MAX_TRANSPORT_ATTEMPTS,
    )


def _apply_judge_config() -> None:
    """Judge endpoint, model and effort from the evaluator configuration (AGENTSWE_JUDGE_*)."""
    global ENDPOINT, MODEL, REASONING_EFFORT
    ENDPOINT = os.environ.get("AGENTSWE_JUDGE_RESPONSES_URL") or ENDPOINT
    MODEL = os.environ.get("AGENTSWE_JUDGE_MODEL") or MODEL
    REASONING_EFFORT = os.environ.get("AGENTSWE_JUDGE_EFFORT") or REASONING_EFFORT


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--manifest", type=Path, required=True)
    parser.add_argument("--case-root", type=Path, required=True)
    parser.add_argument("--candidate-output", type=Path, required=True)
    parser.add_argument("--eval-prompt", type=Path, required=True)
    parser.add_argument("--rubric", type=Path, required=True)
    parser.add_argument("--harness", type=Path, required=True)
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--credential-file", type=Path, required=True)
    parser.add_argument("--resume-transcript", type=Path)
    parser.add_argument("--timeout", type=int, default=600)
    args = parser.parse_args()
    args.output_dir.mkdir(parents=True, exist_ok=True)
    manifest = json.loads(args.manifest.read_text(encoding="utf-8"))
    global JUDGE_FORMAT
    JUDGE_FORMAT = manifest.get("judge_format", "exact")
    case_id = str(manifest["case_id"])
    case_dir = (args.case_root / case_id).resolve()
    # The evaluator harness runs deterministic artifact checks; only this Eval Job
    # may call the provisioned GATEWAY endpoint.
    env_prefix = str(manifest["container_env_prefix"])
    os.environ["PATH"] = env_prefix + "/bin:" + os.environ.get("PATH", "")
    os.environ["SSL_CERT_FILE"] = str(eval_ca_bundle(Path(env_prefix)))
    os.environ["HOME"] = "/tmp/harbor-evidence-grounded-document-qa-eval-home"
    Path(os.environ["HOME"]).mkdir(parents=True, exist_ok=True)
    env_values = parse_dotenv(args.credential_file)
    _apply_judge_config()
    secret_strings = tuple(value for value in env_values.values() if value)
    credential_leak_detected = tree_contains_secret(
        args.candidate_output, secret_strings
    )
    unsafe_entries = unsafe_output_entries(args.candidate_output)
    security_marker = args.candidate_output / ".agentswe_security_violation.json"
    if security_marker.is_file() and not security_marker.is_symlink():
        try:
            marker_value = json.loads(
                security_marker.read_text(encoding="utf-8", errors="replace")
            )
        except (json.JSONDecodeError, OSError):
            marker_value = {}
        if isinstance(marker_value, dict):
            credential_leak_detected = credential_leak_detected or (
                marker_value.get("credential_leak_detected") is True
            )
            marked_unsafe = marker_value.get("unsafe_output_entries")
            if isinstance(marked_unsafe, list):
                unsafe_entries.extend(
                    value for value in marked_unsafe if isinstance(value, str)
                )
    unsafe_entries = sorted(set(unsafe_entries))
    harness = run_harness(args, case_dir, args.output_dir)
    redact_file(args.output_dir / "harness.stdout.log", secret_strings)
    candidate_execution_contract = manifest.get("candidate_execution_contract")
    if not isinstance(candidate_execution_contract, dict):
        candidate_execution_contract = {}
    if candidate_execution_contract.get("validity_gate") is not True:
        harness["validity_gate"] = False
        harness_errors = harness.get("errors")
        if not isinstance(harness_errors, list):
            harness_errors = []
        harness_errors.append("candidate execution contract is invalid")
        harness["errors"] = harness_errors
    harness["candidate_execution_contract"] = candidate_execution_contract
    if credential_leak_detected:
        harness["validity_gate"] = False
        harness["credential_leak_detected"] = True
        harness_errors = harness.get("errors")
        if not isinstance(harness_errors, list):
            harness_errors = []
        harness_errors.append("candidate leaked a provisioned resource credential")
        harness["errors"] = harness_errors
    if unsafe_entries:
        harness["validity_gate"] = False
        harness["output_structure_valid"] = False
        harness["unsafe_output_entries"] = unsafe_entries
        harness_errors = harness.get("errors")
        if not isinstance(harness_errors, list):
            harness_errors = []
        harness_errors.append("candidate output contains a symlink or special object")
        harness["errors"] = harness_errors
    else:
        harness["output_structure_valid"] = True
    harness = redact_object(harness, secret_strings)
    if not isinstance(harness, dict):
        raise RuntimeError("redacted harness result is not an object")
    # Persist the exact enriched/redacted object bound into the quality-review
    # identity. run_harness() writes the raw trusted result before the staged
    # Candidate execution contract and output-safety checks are attached; the
    # final verifier must receive the same object the judge hashed.
    write_json(args.output_dir / "harness_result.json", harness)
    inventory = artifact_inventory(args.candidate_output)
    image_catalog = qa_image_catalog(harness)
    public_images = [public_image_record(item) for item in image_catalog]
    identity = evidence_identity(manifest, harness, inventory)
    rubric_text = args.rubric.read_text(encoding="utf-8")
    evaluator_text = args.eval_prompt.read_text(encoding="utf-8")
    case_text = (case_dir / "input.md").read_text(encoding="utf-8")
    api_key = env_values.get("AGENTSWE_JUDGE_API_KEY") or os.environ.get("DEEPSEEK_API_KEY") or env_values.get("DEEPSEEK_API_KEY") or os.environ.get("GATEWAY_API_KEY") or env_values.get("GATEWAY_API_KEY")
    started = time.monotonic()
    errors: list[str] = []
    model_result: dict[str, object] = {}
    text_count = 0
    image_count = 0
    review_phase = "image"
    transcript: dict[str, object] = {
        "schema_version": "1.0", "protocol": QUALITY_REVIEW_PROTOCOL,
        "identity": identity, "artifact_inventory": inventory,
        "images": public_images, "image_reviews": [], "dimension_reviews": [],
    }
    if not eval_provider_required(harness):
        # Deterministic invalidity is an authoritative, scoreable zero. A
        # provider call cannot change that result and must not gate validity.
        model_result = {"dimensions": {}, "score": 0}
    elif not api_key:
        errors.append("DEEPSEEK_API_KEY is not available")
    else:
        try:
            transcript, text_count, image_count = resume_transcript(
                args.resume_transcript, identity=identity, inventory=inventory,
                images=public_images,
            )
            prior_image_responses = {
                str(record["image"]["id"]): record["response"]
                for record in transcript["image_reviews"]
            }
            image_findings: list[dict[str, object]] = []
            for image_item in image_catalog:
                public_image = public_image_record(image_item)
                if str(public_image["id"]) in prior_image_responses:
                    image_findings.append(prior_image_responses[str(public_image["id"])])
                    continue
                review_id = image_review_id(identity, public_image)
                prompt = redact_text(
                    evaluator_text
                    + "\n\n# Full rubric (context; this request does not assign points)\n"
                    + rubric_text
                    + f"\n\n# Active case: {case_id}\n"
                    + case_text
                    + "\n\n# Frozen evidence identity\n"
                    + json.dumps(identity, sort_keys=True)
                    + "\n\n# One actual evaluator-owned image\n"
                    + json.dumps(public_image, sort_keys=True)
                    + "\n\nInspect only the attached image. Describe visible content, readability, "
                    + "whether the identified native target is visibly inspectable, and concrete visual defects. "
                    + "This is a non-scoring observation that will be supplied unchanged to all six rubric reviews. "
                    + "Do not infer hidden content or assign rubric points. Return one JSON object only:\n"
                    + json.dumps({
                        "protocol": QUALITY_REVIEW_PROTOCOL,
                        "case_id": case_id,
                        "evidence_bundle_sha256": identity["evidence_bundle_sha256"],
                        "image_review_id": review_id,
                        "image_id": public_image["id"],
                        "image_sha256": public_image["sha256"],
                        "visible_observations": ["concrete observation"],
                        "readability": "concrete assessment",
                        "locator_assessment": "concrete assessment",
                        "defects": [],
                    }, sort_keys=True),
                    secret_strings,
                )
                prompt_sha = hashlib.sha256(prompt.encode()).hexdigest()
                response, attempts = call_gateway(prompt, api_key, args.timeout, [image_item])
                image_count += attempts
                response = validate_image_response(
                    response, case_id=case_id, identity=identity,
                    image_record=public_image, review_id=review_id,
                )
                response = redact_object(response, secret_strings)
                record = {
                    "image_review_id": review_id,
                    "image": public_image,
                    "prompt_sha256": prompt_sha,
                    "request_attempts": attempts,
                    "provider_receipt": getattr(call_gateway, "last_receipt", None),
                    "response": response,
                    "response_sha256": canonical_sha256(response),
                }
                transcript["image_reviews"].append(record)
                image_findings.append(response)
                write_json(args.output_dir / f"image-review-{len(image_findings):03d}.json", record)

            dimensions: dict[str, object] = {}
            major_errors: list[str] = []
            assessments: list[str] = []
            for record in transcript["dimension_reviews"]:
                response = record["response"]
                dimension = str(record["dimension"])
                maximum = DIMENSION_MAXIMA[dimension]
                dimensions[dimension] = {
                    "score": response["score"], "max": maximum,
                    "evidence": "; ".join(response["evidence"]),
                    "deductions": response["deductions"],
                    "review_id": record["review_id"],
                }
                major_errors.extend(response["major_errors"])
                assessments.append(f"{dimension}: {response['assessment']}")
            review_phase = "dimension"
            for dimension, maximum in DIMENSION_MAXIMA.items():
                if dimension in dimensions:
                    continue
                images = images_for_dimension(dimension, image_catalog)
                image_records = [public_image_record(item) for item in images]
                review_id = dimension_review_id(identity, dimension, maximum, image_records)
                prompt = redact_text(
                    evaluator_text
                    + "\n\n# Full rubric (unchanged)\n"
                    + rubric_text
                    + f"\n\n# Active case: {case_id}\n"
                    + case_text
                    + "\n\n# Frozen evidence identity\n"
                    + json.dumps(identity, sort_keys=True)
                    + "\n\n# Trusted offline harness view\n"
                    + json.dumps(
                        prompt_harness_view(harness, dimension, image_catalog),
                        ensure_ascii=False, sort_keys=True, separators=(",", ":"),
                    )
                    + "\n\n# Candidate final artifacts relevant to this dimension\n"
                    + dimension_artifacts(args.candidate_output, dimension, secret_strings)
                    + "\n\n# Prior per-image observations (non-scoring, hash-bound)\n"
                    + json.dumps(image_findings, ensure_ascii=False, sort_keys=True)
                    + "\n\nScore exactly one named rubric dimension. Apply that dimension's full original "
                    + "criteria and maximum; do not move deductions to another dimension, change the maximum, "
                    + "invent a fatal gate, or rescore any other dimension. Cite concrete artifact, source, "
                    + "validator, browser, pixel, or image evidence. The rubric does not require integer "
                    + "granularity: use a finite fractional score when the evidence warrants it, and do not "
                    + "round a justified fractional score. Return one JSON object only:\n"
                    + json.dumps({
                        "protocol": QUALITY_REVIEW_PROTOCOL,
                        "case_id": case_id,
                        "evidence_bundle_sha256": identity["evidence_bundle_sha256"],
                        "review_id": review_id,
                        "dimension": dimension,
                        "score": 0,
                        "max": maximum,
                        "evidence": ["concrete evidence"],
                        "deductions": [],
                        "major_errors": [],
                        "assessment": "concise dimension assessment",
                    }, sort_keys=True),
                    secret_strings,
                )
                if len(prompt) > MAX_PROMPT_ARTIFACT_CHARS:
                    raise EvalRequestError("dimension prompt exceeds configured complete-evidence budget", 0)
                prompt_sha = hashlib.sha256(prompt.encode()).hexdigest()
                response, attempts = call_gateway(prompt, api_key, args.timeout)
                text_count += attempts
                response = validate_dimension_response(
                    response, case_id=case_id, dimension=dimension, maximum=maximum,
                    identity=identity, review_id=review_id,
                )
                response = redact_object(response, secret_strings)
                record = {
                    "review_id": review_id,
                    "dimension": dimension,
                    "maximum": maximum,
                    "input_images": image_records,
                    "prompt_sha256": prompt_sha,
                    "request_attempts": attempts,
                    "provider_receipt": getattr(call_gateway, "last_receipt", None),
                    "response": response,
                    "response_sha256": canonical_sha256(response),
                }
                transcript["dimension_reviews"].append(record)
                write_json(args.output_dir / f"dimension-review-{dimension}.json", record)
                dimensions[dimension] = {
                    "score": response["score"],
                    "max": maximum,
                    "evidence": "; ".join(response["evidence"]),
                    "deductions": response["deductions"],
                    "review_id": review_id,
                }
                major_errors.extend(response["major_errors"])
                assessments.append(f"{dimension}: {response['assessment']}")
            model_result = {
                "dimensions": dimensions,
                "score": sum(item["score"] for item in dimensions.values()),
                "major_errors": major_errors,
                "assessment": "\n".join(assessments),
                "evaluation_state": "scoreable",
            }
        except EvalRequestError as exc:
            # Count the failed request attempts in addition to already completed calls.
            if review_phase == "image":
                image_count += exc.attempts
            else:
                text_count += exc.attempts
            errors.append(str(exc))
        except Exception as exc:
            errors.append(str(exc))
    transcript_path = args.output_dir / "quality_review_transcript.json"
    write_json(transcript_path, transcript)
    transcript_sha256 = hashlib.sha256(transcript_path.read_bytes()).hexdigest()
    if not isinstance(model_result, dict):
        model_result = {}
    model_result = redact_object(model_result, secret_strings)
    if not isinstance(model_result, dict):
        model_result = {}
    provider_required = eval_provider_required(harness)
    evaluation_state = normalize_evaluation_state(model_result, harness, errors)
    result_errors = list(errors)
    if evaluation_state == "infrastructure_error" and not result_errors:
        result_errors.append("Eval classified an infrastructure failure; candidate score is not publishable")
    # Keep evaluator output separate from provider transport details.
    eval_result = {
        "schema_version": "1.0",
        "case_id": case_id,
        "evaluation_mode": manifest["evaluation_mode"],
        "validity_gate": evaluation_state == "scoreable" and provider_required,
        "evaluation_state": evaluation_state,
        "dimensions": model_result.get("dimensions", {}),
        "score": model_result.get("score", 0),
        "major_errors": model_result.get("major_errors", []),
        "assessment": model_result.get("assessment", ""),
        "quality_review_protocol": QUALITY_REVIEW_PROTOCOL,
        "judge_format": JUDGE_FORMAT,
        "judge_echo_repairs": JUDGE_ECHO_REPAIRS,
        "quality_review_identity": identity,
        "quality_review_transcript_sha256": transcript_sha256,
        "quality_review_complete": (
            len(transcript["dimension_reviews"]) == len(DIMENSION_MAXIMA)
            and len(transcript["image_reviews"]) == len(image_catalog)
            and not errors
        ),
        "image_receipts": public_images,
        "provider_counts": {"gateway_text": text_count, "gateway_image": image_count, "serper": 0, "web_retrieval": 0, "deepseek": 0},
        "harness_result": harness,
        "credential_leak_detected": credential_leak_detected,
        "resource_secrets_redacted": True,
        "errors": result_errors,
        "runtime_seconds": round(time.monotonic() - started, 3),
    }
    write_json(args.output_dir / "eval_result.json", eval_result)
    # Never print the response or transport headers; Harbor captures only this marker.
    print(json.dumps({"case_id": case_id, "gateway_text": text_count, "gateway_image": image_count, "errors": len(errors)}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
