#!/usr/bin/env python3
"""Run the independent Eval Codex call and persist only non-secret evidence."""

from __future__ import annotations

import argparse
import base64
import hashlib
import http.client
import json
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
PROMPT_TEXT_SUFFIXES = {
    ".css", ".csv", ".htm", ".html", ".js", ".json", ".jsonl",
    ".log", ".md", ".svg", ".toml", ".tsv", ".txt", ".xml", ".yaml", ".yml",
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
        elif (
            path.is_file()
            and path.suffix.lower() in PROMPT_TEXT_SUFFIXES
            and path.stat().st_size <= 2_000_000
        ):
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
            digest = hashlib.sha256(path.read_bytes()).hexdigest()
            piece = (
                f"\n--- {path.relative_to(root)} ---\n"
                f"[BINARY OR LARGE FILE OMITTED: {path.stat().st_size} bytes; "
                f"sha256={digest}]\n"
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


def run_harness(args: argparse.Namespace, case_dir: Path, output_dir: Path) -> dict[str, object]:
    manifest = json.loads(args.manifest.read_text())
    execution = manifest.get('candidate_execution_contract', {})
    for key in ('candidate_digest', 'candidate_output_digest', 'case_digest'):
        if not isinstance(manifest.get(key), str) or not re.fullmatch('[0-9a-f]{64}', manifest[key]):
            raise RuntimeError('missing trusted identity: ' + key)
    if execution.get('case_digest') != manifest['case_digest']:
        raise RuntimeError('GUI active-case identity mismatch')
    if execution.get('candidate_digest') != manifest['candidate_digest'] or execution.get('output_digest') != manifest['candidate_output_digest']:
        raise RuntimeError('GUI Candidate/output identity mismatch')
    value = execution.get('trusted_harness_result')
    if not isinstance(value, dict) or value.get('case') != case_dir.name:
        raise RuntimeError('missing trusted GUI evidence')
    digest = hashlib.sha256(json.dumps(value, sort_keys=True, separators=(',', ':')).encode()).hexdigest()
    if digest != execution.get('trusted_harness_result_sha256'):
        raise RuntimeError('GUI evidence hash mismatch')
    if value.get('execution', {}).get('state_provenance') != 'evaluator-controlled-browser-v1':
        raise RuntimeError('GUI state lacks authoritative provenance')
    if value.get('browser_receipts', {}).get('protocol') != 'evaluator-controlled-browser-v1':
        raise RuntimeError('GUI evidence lacks evaluator-owned browser capture receipts')
    if value.get('fatal_gate') is not True and value.get('screenshot_bindings', {}).get('final_state.png', {}).get('capture_matched') is not True:
        raise RuntimeError('GUI final screenshot is not bound to evaluator browser capture')
    if value.get('evaluation_state') == 'infrastructure_error':
        raise RuntimeError('GUI evidence infrastructure failed')
    write_json(output_dir / 'harness_result.json', value)
    return value


def visual_payload(harness, candidate_output):
    content, receipt = [], []
    for row in harness.get('visual_images', []):
        name = row['file']
        if name not in ('initial_state.png', 'decisive_step.png', 'final_state.png'):
            raise RuntimeError('invalid GUI evidence image name')
        data = base64.b64decode(row['base64'], validate=True)
        if hashlib.sha256(data).hexdigest() != row['sha256']:
            raise RuntimeError('GUI screenshot evidence hash mismatch')
        path = candidate_output / name
        if path.is_symlink() or path.read_bytes() != data:
            raise RuntimeError('GUI screenshot output changed')
        content.extend([{'type': 'input_text', 'text': 'GUI evidence: ' + name},
                        {'type': 'input_image', 'image_url': 'data:image/png;base64,' + row['base64']}])
        receipt.append({key: row[key] for key in ('file', 'sha256', 'width', 'height')})
    if harness.get('fatal_gate') is not True and 'final_state.png' not in [row['file'] for row in receipt]:
        raise RuntimeError('final screenshot is missing from judge visual evidence')
    return content, receipt


def call_gateway(prompt: str, api_key: str, timeout: int, images=None) -> tuple[dict[str, object], int]:
    payload = {
        "model": MODEL,
        "reasoning": {"effort": REASONING_EFFORT},
        "max_output_tokens": 100000,
        "stream": True,
        "input": [{"type": "message", "role": "user", "content": [{"type": "input_text", "text": prompt}] + (images or [])}],
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
                    completed = True
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
            return strict_json("".join(chunks)), attempt + 1
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
    parser.add_argument("--timeout", type=int, default=600)
    args = parser.parse_args()
    args.output_dir.mkdir(parents=True, exist_ok=True)
    manifest = json.loads(args.manifest.read_text(encoding="utf-8"))
    case_id = str(manifest["case_id"])
    case_dir = (args.case_root / case_id).resolve()
    # Eval receives immutable artifacts and the candidate execution contract;
    # it must not rerun the fixture or receive candidate source.
    env_prefix = str(manifest["container_env_prefix"])
    os.environ["PATH"] = env_prefix + "/bin:" + os.environ.get("PATH", "")
    os.environ["SSL_CERT_FILE"] = str(eval_ca_bundle(Path(env_prefix)))
    os.environ["HOME"] = "/tmp/harbor-desktop-gui-eval-home"
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
    candidate_execution_contract = manifest.get("candidate_execution_contract")
    if not isinstance(candidate_execution_contract, dict):
        candidate_execution_contract = {}
    evidence_errors = []
    image_content, image_receipt = [], []
    try:
        harness = run_harness(args, case_dir, args.output_dir)
        image_content, image_receipt = visual_payload(harness, args.candidate_output)
    except (OSError, ValueError, RuntimeError, KeyError, TypeError) as exc:
        evidence_errors.append('trusted GUI evidence unavailable: ' + str(exc))
        harness = {'case': case_id, 'validity_gate': False, 'fatal_gate': False,
                   'evaluation_state': 'infrastructure_error', 'fatal_reasons': [], 'errors': []}
    harness.setdefault('advisory_findings', [])
    if candidate_execution_contract.get("credential_leak_detected") is True:
        credential_leak_detected = True
    contract_reasons = candidate_execution_contract.get("fatal_reasons")
    if isinstance(contract_reasons, list):
        harness["fatal_reasons"].extend(str(item) for item in contract_reasons)
    if candidate_execution_contract.get("timed_out") is True:
        harness["fatal_gate"] = True
        harness["fatal_reasons"].append("task_registered_timeout")
    if candidate_execution_contract.get("memory_violation") is True:
        harness["fatal_gate"] = True
        harness["fatal_reasons"].append("registered_memory_violation")
    if candidate_execution_contract.get("validity_gate") is not True:
        harness["advisory_findings"].append("candidate execution contract is invalid")
    if credential_leak_detected:
        harness["validity_gate"] = False
        harness["fatal_gate"] = True
        harness["fatal_reasons"].append("credential_leak")
        harness["credential_leak_detected"] = True
        harness_errors = harness.get("errors")
        if not isinstance(harness_errors, list):
            harness_errors = []
        harness_errors.append("candidate leaked a provisioned resource credential")
        harness["errors"] = harness_errors
    if unsafe_entries:
        harness["validity_gate"] = False
        harness["fatal_gate"] = True
        harness["fatal_reasons"].append("unsafe_output_entry")
        harness["output_structure_valid"] = False
        harness["unsafe_output_entries"] = unsafe_entries
        harness_errors = harness.get("errors")
        if not isinstance(harness_errors, list):
            harness_errors = []
        harness_errors.append("candidate output contains a symlink or special object")
        harness["errors"] = harness_errors
    else:
        harness["output_structure_valid"] = True
    if not evidence_errors:
        harness["evaluation_state"] = "fatal_zero" if harness["fatal_gate"] else "scoreable"
    harness = redact_object(harness, secret_strings)
    if not isinstance(harness, dict):
        raise RuntimeError("redacted harness result is not an object")
    prompt = (
        args.eval_prompt.read_text(encoding="utf-8")
        + "\n\n# Rubric\n"
        + args.rubric.read_text(encoding="utf-8")
        + f"\n\n# Active case: {case_id}\n"
        + (case_dir / "input.md").read_text(encoding="utf-8")
        + "\n\n# Harness result (validity and scoreability evidence)\n"
        + json.dumps({key: value for key, value in harness.items() if key != 'visual_images'}, indent=2, sort_keys=True)
        + "\n\n# Candidate final artifacts (source is intentionally unavailable)\n"
        + tree_text(args.candidate_output, secret_strings)
        + "\n\nReturn one JSON object only. Required shape: "
        + json.dumps({
            "case_id": case_id,
            "evaluation_state": "scoreable",
            "validity_gate": True,
            "dimensions": {name: {"score": 0, "max": maximum, "evidence": ""} for name, maximum in {
                "requested_application_outcome": 35,
                "scope_preservation_side_effect_boundary": 20,
                "workflow_recovery_verification": 15,
                "visual_evidence_quality_decisive_step": 15,
                "trace_fidelity_auditability": 10,
                "artifact_report_validity": 5,
            }.items()},
            "score": 0,
            "major_errors": [],
            "assessment": "",
        }, sort_keys=True)
    )
    api_key = env_values.get("AGENTSWE_JUDGE_API_KEY") or os.environ.get("DEEPSEEK_API_KEY") or env_values.get("DEEPSEEK_API_KEY") or os.environ.get("GATEWAY_API_KEY") or env_values.get("GATEWAY_API_KEY")
    prompt = redact_text(prompt, secret_strings)
    started = time.monotonic()
    errors: list[str] = list(evidence_errors)
    model_result: dict[str, object] = {}
    count = 0
    if evidence_errors:
        pass
    elif harness.get("fatal_gate") is True:
        # Security/identity fatal gates are deterministic zeros. Ordinary
        # validator failures remain scoreable and are sent to Eval Agent.
        model_result = {"dimensions": {}, "score": 0}
    elif not api_key:
        errors.append("DEEPSEEK_API_KEY is not available")
    else:
        try:
            model_result, count = call_gateway(prompt, api_key, args.timeout, image_content)
        except EvalRequestError as exc:
            count = exc.attempts
            errors.append(str(exc))
        except Exception as exc:
            errors.append(str(exc))
    if not isinstance(model_result, dict):
        model_result = {}
    model_result = redact_object(model_result, secret_strings)
    if not isinstance(model_result, dict):
        model_result = {}
    evaluation_state = normalize_evaluation_state(model_result, harness, errors)
    result_errors = list(errors)
    if evaluation_state == "infrastructure_error" and not result_errors:
        result_errors.append("Eval classified an infrastructure failure; candidate score is not publishable")
    # Keep evaluator output separate from provider transport details.
    eval_result = {
        "schema_version": "1.0",
        "case_id": case_id,
        "evaluation_mode": manifest["evaluation_mode"],
        "validity_gate": evaluation_state == "scoreable",
        "fatal_gate": bool(harness.get("fatal_gate")),
        "fatal_reasons": harness.get("fatal_reasons", []),
        "evaluation_state": evaluation_state,
        "dimensions": model_result.get("dimensions", {}),
        "score": model_result.get("score", 0),
        "major_errors": model_result.get("major_errors", []),
        "assessment": model_result.get("assessment", ""),
        "provider_counts": {"gateway_text": 0 if image_content else count, "gateway_image": count if image_content else 0, "serper": 0, "web_retrieval": 0, "deepseek": 0},
        "visual_evidence_receipt": image_receipt,
        "trusted_harness_result_sha256": candidate_execution_contract.get('trusted_harness_result_sha256'),
        "harness_result": harness,
        "credential_leak_detected": credential_leak_detected,
        "resource_secrets_redacted": True,
        "errors": result_errors,
        "runtime_seconds": round(time.monotonic() - started, 3),
    }
    write_json(args.output_dir / "eval_result.json", eval_result)
    # Never print the response or transport headers; Harbor captures only this marker.
    print(json.dumps({"case_id": case_id, "gateway_text": count, "errors": len(errors)}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
