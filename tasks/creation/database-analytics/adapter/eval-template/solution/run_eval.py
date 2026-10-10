#!/usr/bin/env python3
"""Run the independent Eval Codex call and persist only non-secret evidence."""

from __future__ import annotations

import argparse
import base64
import hashlib
import sys
sys.path.insert(0, str(__import__("pathlib").Path(__file__).resolve().parent))
from trusted_evidence import load_contract, images, text_evidence
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
MAX_TRANSPORT_ATTEMPTS = int(os.environ.get("AGENTSWE_JUDGE_MAX_ATTEMPTS", "5"))
MAX_PROMPT_ARTIFACT_CHARS = 4_000_000


class EvalRequestError(RuntimeError):
    def __init__(self, message: str, attempts: int, status: int | None = None, body: bytes | None = None):
        super().__init__(message)
        self.attempts = attempts
        self.status = status  # HTTP status of a rejected request
        self.body = body  # its error body (at most ERROR_BODY_LIMIT bytes) for a 4xx


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


def tree_text(root: Path, secrets: tuple[str, ...], omit=None) -> str:
    """The candidate tree as judge text. omit (the overflow fallback only) maps a file to a marker that replaces its
    content, or None to keep it; without omit the text is the full-evidence prompt's."""
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
        elif path.is_file() and path.suffix.lower() in {".png", ".jpg", ".jpeg", ".pdf", ".sqlite", ".zip"}:
            continue
        elif path.is_file() and path.stat().st_size <= 2_000_000:
            marker = omit(path) if omit is not None else None
            if marker is None:
                content = path.read_text(encoding="utf-8", errors="replace")
                piece = (
                    f"\n--- {path.relative_to(root)} ---\n"
                    + redact_text(content, secrets)
                )
            else:
                piece = f"\n--- {path.relative_to(root)} ---\n" + marker
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


def run_harness(args: argparse.Namespace, case_dir: Path, output_dir: Path) -> dict[str, object]:
    value=load_contract(json.loads(args.manifest.read_text()),case_dir)
    write_json(output_dir / 'harness_result.json',value)
    return value


def call_gateway(prompt: str, api_key: str, timeout: int, image_parts=None, *, max_attempts: int | None = None,
                 http_errors: list | None = None) -> tuple[dict[str, object], int]:
    payload = {
        "model": MODEL,
        "reasoning": {"effort": REASONING_EFFORT},
        "max_output_tokens": 100000,
        "stream": True,
        "input": [{"type": "message", "role": "user", "content": [{"type": "input_text", "text": prompt}] + (image_parts or [])}],
    }
    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
        "Accept": "text/event-stream",
        "User-Agent": "AgentSWE-Harbor-Eval/1.0",
    }
    last_error: str | None = None
    attempts_allowed = MAX_TRANSPORT_ATTEMPTS if max_attempts is None else max_attempts
    for attempt in range(attempts_allowed):
        try:
            response = requests.post(
                ENDPOINT, headers=headers, json=payload,
                timeout=(30, timeout), stream=True,
            )
            if response.status_code >= 400:
                last_error = f"HTTPError:{response.status_code}"
                error_body = None
                if response.status_code < 500:
                    error_body = read_error_body(response)
                    if http_errors is not None:
                        http_errors.append({"attempt": attempt + 1, "status": response.status_code, "body": error_body})
                if response.status_code not in (429, 500, 502, 503, 504, 524):
                    raise EvalRequestError(f"Eval Codex request failed: {last_error}", attempt + 1,
                                           response.status_code, error_body)
                if attempt + 1 < attempts_allowed:
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
                    call_gateway.last_usage = event.get("response", {}).get("usage")
                    call_gateway.last_usage = event.get("response", {}).get("usage")
                    call_gateway.last_usage = event.get("response", {}).get("usage")
                    call_gateway.last_usage = event.get("response", {}).get("usage")
                    call_gateway.last_usage = event.get("response", {}).get("usage")
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
                if attempt + 1 < attempts_allowed:
                    time.sleep(min(8.0, 1.0 * (2**attempt)))
                continue
            return strict_json("".join(chunks)), attempt + 1
        except EvalRequestError:
            raise
        except (requests.RequestException, json.JSONDecodeError, ValueError, TimeoutError) as exc:
            last_error = type(exc).__name__
            if attempt + 1 < attempts_allowed:
                time.sleep(min(8.0, 1.0 * (2**attempt)))
    raise EvalRequestError(
        f"Eval Codex request failed after retries: {last_error}",
        attempts_allowed,
    )


# Final artifacts the delivery contract requires (run_report.json lists the rest it delivers).
FINAL_ARTIFACTS = ("answer.json", "queries.json", "result.csv", "chart.json", "dashboard.html", "decision.json",
                   "lineage.json", "run_report.json", "results")


# --- judge context overflow fallback (identical in every Creation eval that carries it) ---
# The first judge request carries the full prompt, unchanged. Only when the judge provider rejects that request
# because it does not fit the judge context is one reduced prompt built and sent, once:
#   trigger: HTTP 400 whose body says the context length was exceeded; or HTTP 400 with no provider message (an
#            empty body, or only the broker's placeholder) while estimate_tokens(prompt) > JUDGE_INPUT_TOKEN_BUDGET.
#   reduce, in order, re-estimating after each step and stopping once the estimate fits the budget:
#     (i)   repeated harness sub-values -> a reference to the copy kept (lossless);
#     (ii)  binary files (a NUL byte or invalid UTF-8) -> a path/size/sha256 marker;
#     (iii) files that are neither final artifacts nor listed in run_report.json -> the same marker.
#   still too large (or nothing to reduce): the case stays an infrastructure error, as without the fallback.
# eval_result.json records evidence_reduced, a non-scoring prompt_reduction receipt and a redacted sample of every
# judge 4xx body (judge_http_errors); prompt_reduction.json holds the same receipt.
JUDGE_CONTEXT_TOKENS = 1_048_576  # judge context window; the provider counts max_output_tokens against it
JUDGE_MAX_OUTPUT_TOKENS = 100000  # max_output_tokens of every judge request
JUDGE_INPUT_TOKEN_BUDGET = 900_000  # estimated input tokens; 94.9% of the 948,576 the completion leaves
ERROR_BODY_LIMIT = 32768  # bytes of a 4xx body read; the judge broker relays at most this much
ERROR_SAMPLE_CHARS = 2000  # characters of a redacted 4xx body kept in eval_result.json
DUPLICATE_MIN_CHARS = 1024  # smallest repeated harness value (canonical JSON characters) replaced in step (i)
IMAGE_TOKEN_FALLBACK = 16384  # estimate for an image whose PNG header cannot be read
CONTEXT_OVERFLOW = re.compile(
    r"maximum context length|context[ _-]?length[ _-]?exceeded|exceeds? (?:the )?(?:model'?s? )?(?:maximum )?"
    r"context (?:length|window|size)|context window (?:is )?exceeded|prompt is too long|input is too long"
    r"|too many (?:input )?tokens|reduce the length of the (?:messages|prompt|input)"
    r"|exceeds the maximum number of (?:input )?tokens|range of input length", re.IGNORECASE)
CONTEXT_LIMIT = re.compile(r"maximum context length is\s*([0-9][0-9,]*)\s*tokens", re.IGNORECASE)
REQUESTED_INPUT = re.compile(r"\(\s*([0-9][0-9,]*)\s*in the messages", re.IGNORECASE)
CREDENTIAL_TEXT = re.compile(r"(?i)\b(bearer\s+)[A-Za-z0-9._~+/=-]{8,}|\b(?:sk|pk|key|tok)-[A-Za-z0-9_-]{8,}")
_CONTROL_BYTES = bytes(b for b in range(32) if b not in (9, 10, 13)) + b"\x7f"
_NON_ASCII_BYTES = bytes(range(128, 256))


def estimate_tokens(text: str, extra: int = 0) -> int:
    """An upper estimate of the judge's input tokens for `text` (plus `extra`, e.g. images).

    Byte-level BPE tokenizers emit at most one token per UTF-8 byte, so a control character (NUL included) counts 1
    and a non-ASCII character counts its UTF-8 bytes: exact-or-over by construction. Other ASCII counts 0.8 per
    character, above every content class measured with the DeepSeek-V3 tokenizer (random printable ASCII 0.77,
    base64 0.70, hex 0.58, indented JSON 0.48, code and prose lower). 64 covers the chat template (30 measured)."""
    data = text.encode("utf-8", "surrogatepass")
    control = len(data) - len(data.translate(None, _CONTROL_BYTES))
    non_ascii = len(data) - len(data.translate(None, _NON_ASCII_BYTES))
    ascii_text = len(data) - control - non_ascii
    return control + non_ascii + (ascii_text * 4 + 4) // 5 + 64 + extra


def image_token_estimate(parts: list | None) -> int:
    """Upper estimate for input_image parts: one token per 750 pixels plus 85 per image (at or above the published
    per-image costs of the common vision judges); IMAGE_TOKEN_FALLBACK when the PNG header is unreadable."""
    total = 0
    for part in parts or []:
        url = part.get("image_url") if isinstance(part, dict) else None
        try:
            head = base64.b64decode(str(url).split(",", 1)[1][:64])
            width, height = int.from_bytes(head[16:20], "big"), int.from_bytes(head[20:24], "big")
            if not head.startswith(b"\x89PNG\r\n\x1a\n") or not 0 < width * height <= 1 << 31:
                raise ValueError("not a PNG header")
            total += -(-width * height // 750) + 85
        except (IndexError, ValueError, TypeError):
            total += IMAGE_TOKEN_FALLBACK
    return total


def read_error_body(response) -> bytes:
    """At most ERROR_BODY_LIMIT bytes of an HTTP error body; never raises."""
    data = b""
    try:
        for chunk in response.iter_content(chunk_size=8192):
            data += chunk or b""
            if len(data) >= ERROR_BODY_LIMIT:
                break
    except Exception:
        pass
    try:
        response.close()
    except Exception:
        pass
    return data[:ERROR_BODY_LIMIT]


def redacted_sample(body: bytes | None, secrets: tuple[str, ...]) -> str:
    """The first ERROR_SAMPLE_CHARS characters of an error body without credentials."""
    text = redact_text((body or b"").decode("utf-8", "replace"), tuple(value for value in secrets if value))
    text = CREDENTIAL_TEXT.sub(lambda match: (match.group(1) or "") + "[REDACTED]", text)
    return text[:ERROR_SAMPLE_CHARS]


def provider_message(text: str) -> bool:
    """The error body carries a message from the provider (not empty, not only the broker's placeholder)."""
    if not text.strip():
        return False
    try:
        value = json.loads(text)
    except ValueError:
        return True
    strings: list[str] = []

    def walk(node, key=None):
        if isinstance(node, dict):
            for child_key, child in node.items():
                walk(child, child_key)
        elif isinstance(node, list):
            for child in node:
                walk(child, key)
        elif isinstance(node, str) and key not in ("type", "upstream_status") and node.strip():
            strings.append(node)

    walk(value)
    return bool(strings)


def _token_count(match) -> int | None:
    return int(match.group(1).replace(",", "")) if match else None


def context_overflow(status: int | None, body: bytes | None, prompt_estimate: int) -> dict[str, object] | None:
    """Why a rejected judge request is a context overflow, or None when it is not one."""
    if status != 400:
        return None
    text = (body or b"").decode("utf-8", "replace")
    if CONTEXT_OVERFLOW.search(text):
        return {"reason": "provider_context_length",
                "provider_context_tokens": _token_count(CONTEXT_LIMIT.search(text)),
                "provider_input_tokens": _token_count(REQUESTED_INPUT.search(text))}
    if not provider_message(text) and prompt_estimate > JUDGE_INPUT_TOKEN_BUDGET:
        return {"reason": "no_error_body_estimate_over_budget",
                "provider_context_tokens": None, "provider_input_tokens": None}
    return None


def input_budget(trigger: dict[str, object]) -> int:
    """Estimated-token budget of the reduced prompt: JUDGE_INPUT_TOKEN_BUDGET, or 95% of the input room under a
    smaller context the provider stated."""
    limit = trigger.get("provider_context_tokens")
    if isinstance(limit, int) and limit > 0:
        return max(0, min(JUDGE_INPUT_TOKEN_BUDGET, (limit - JUDGE_MAX_OUTPUT_TOKENS) * 95 // 100))
    return JUDGE_INPUT_TOKEN_BUDGET


def _child_path(path: str, key) -> str:
    if isinstance(key, int):
        return f"{path}[{key}]"
    return f"{path}.{key}" if re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", key) else f"{path}[{json.dumps(key)}]"


def deduplicate(value: object) -> tuple[object, int]:
    """A copy of `value` in which every repeated object, array or string of at least DUPLICATE_MIN_CHARS canonical
    JSON characters is replaced by a reference to the copy kept (the shallowest, then the first in sorted-key
    order); larger values first. Returns (copy, number of values replaced)."""
    copy = json.loads(json.dumps(value))
    nodes: list[dict[str, object]] = []
    counter = [0]

    def walk(node, path, depth, parent, key, ancestors):
        order = counter[0]
        counter[0] += 1
        inner = ancestors + (order,)
        if isinstance(node, dict):
            text = "{" + ",".join(json.dumps(k, ensure_ascii=False) + ":" + walk(node[k], _child_path(path, k),
                                  depth + 1, node, k, inner) for k in sorted(node)) + "}"
        elif isinstance(node, list):
            text = "[" + ",".join(walk(item, _child_path(path, index), depth + 1, node, index, inner)
                                  for index, item in enumerate(node)) + "]"
        else:
            text = json.dumps(node, ensure_ascii=False)
        if parent is not None and isinstance(node, (dict, list, str)) and len(text) >= DUPLICATE_MIN_CHARS:
            nodes.append({"digest": hashlib.sha256(text.encode("utf-8", "surrogatepass")).hexdigest(),
                          "size": len(text), "depth": depth, "order": order, "path": path,
                          "parent": parent, "key": key, "ancestors": ancestors})
        return text

    walk(copy, "$", 0, None, None, ())
    groups: dict[str, list[dict[str, object]]] = {}
    for node in nodes:
        groups.setdefault(str(node["digest"]), []).append(node)
    replaced: set[int] = set()
    count = 0
    for members in sorted(groups.values(), key=lambda group: (-int(group[0]["size"]), int(group[0]["order"]))):
        alive = [m for m in members if m["order"] not in replaced and not replaced.intersection(m["ancestors"])]
        if len(alive) < 2:
            continue
        kept = min(alive, key=lambda m: (m["depth"], m["order"]))
        for member in alive:
            if member is kept:
                continue
            member["parent"][member["key"]] = (
                f"[DUPLICATE OMITTED: identical to {kept['path']} of this harness result, sha256 {member['digest']}]")
            replaced.add(int(member["order"]))
            count += 1
    return copy, count


def is_binary(data: bytes) -> bool:
    if b"\x00" in data:
        return True
    try:
        data.decode("utf-8")
    except UnicodeDecodeError:
        return True
    return False


def declared_artifacts(root: Path) -> set[str]:
    """FINAL_ARTIFACTS plus the paths run_report.json lists (artifact_paths or artifacts), as output-relative paths;
    an absolute entry also matches by each of its trailing paths."""
    entries = set(FINAL_ARTIFACTS)
    report = root / "run_report.json"
    try:
        if report.is_file() and not report.is_symlink() and report.stat().st_size <= 2_000_000:
            value = json.loads(report.read_text(encoding="utf-8"))
            for key in ("artifact_paths", "artifacts"):
                items = value.get(key) if isinstance(value, dict) else None
                if isinstance(items, list):
                    entries.update(item for item in items if isinstance(item, str))
    except (OSError, ValueError):
        pass
    declared: set[str] = set()
    for entry in entries:
        parts = [part for part in entry.replace("\\", "/").split("/") if part not in ("", ".")]
        if not parts or ".." in parts:
            continue
        if entry.startswith("/"):
            declared.update("/".join(parts[index:]) for index in range(len(parts)))
        else:
            declared.add("/".join(parts))
    return declared


def file_omitter(root: Path, *, binary: bool, non_artifact: bool, stats: dict[str, int]):
    """tree_text omit hook: the marker that replaces a file's content, or None to keep the content."""
    declared = declared_artifacts(root) if non_artifact else set()

    def omit(path: Path) -> str | None:
        rel = path.relative_to(root).as_posix()
        data = path.read_bytes()
        if binary and is_binary(data):
            kind = "binary"
        elif non_artifact and not (rel in declared or any(rel.startswith(entry + "/") for entry in declared)):
            kind = "non_artifact"
        else:
            return None
        stats[kind + "_files"] = stats.get(kind + "_files", 0) + 1
        stats[kind + "_bytes"] = stats.get(kind + "_bytes", 0) + len(data)
        label = "BINARY FILE" if kind == "binary" else "NON-ARTIFACT FILE"
        return f"[{label} OMITTED: {len(data)} bytes, sha256={hashlib.sha256(data).hexdigest()}]\n"

    return omit


def prompt_size(prompt: str, extra: int = 0) -> dict[str, object]:
    return {"chars": len(prompt), "utf8_bytes": len(prompt.encode("utf-8", "surrogatepass")),
            "estimated_tokens": estimate_tokens(prompt, extra),
            "sha256": hashlib.sha256(prompt.encode("utf-8", "surrogatepass")).hexdigest()}


def reduce_judge_prompt(render, harness_view: object, root: Path, budget: int, *, force_all: bool,
                        extra: int = 0) -> tuple[str | None, list[dict[str, object]], str]:
    """Apply steps (i)-(iii) in order; render(harness_view, omit) builds a prompt. Stops after the first applied step
    whose estimate fits `budget`, unless force_all. Returns (prompt or None, steps, outcome)."""
    steps: list[dict[str, object]] = []
    view, binary, non_artifact = harness_view, False, False
    prompt: str | None = None
    for name in ("deduplicate_harness", "omit_binary_files", "omit_non_artifact_files"):
        stats: dict[str, int] = {}
        if name == "deduplicate_harness":
            try:
                view, replaced = deduplicate(view)
            except RecursionError:
                replaced = 0
            stats["duplicates_replaced"] = replaced
            changed = replaced > 0
        else:
            binary = True
            non_artifact = name == "omit_non_artifact_files"
            changed = True
        if changed:
            omit = file_omitter(root, binary=binary, non_artifact=non_artifact, stats=stats) if binary else None
            candidate = render(view, omit)
            if binary:
                kind = "non_artifact" if non_artifact else "binary"
                stats = {key: value for key, value in stats.items() if key.startswith(kind)}
                stats.setdefault(kind + "_files", 0)
                changed = stats[kind + "_files"] > 0
        step: dict[str, object] = {"step": name, "applied": changed, **stats}
        if changed:
            prompt = candidate
            step.update(prompt_size(prompt, extra))
        steps.append(step)
        if changed and not force_all and int(step["estimated_tokens"]) <= budget:
            break
    if prompt is None:
        return None, steps, "nothing_to_reduce"
    if estimate_tokens(prompt, extra) > budget:
        return None, steps, "still_too_large"
    return prompt, steps, "fits"


def judge_with_fallback(send, prompt: str, rebuild, secrets: tuple[str, ...], extra: int = 0):
    """send(prompt, max_attempts, http_errors) -> (judge result, attempts); rebuild(budget, force_all) ->
    reduce_judge_prompt(...). Returns (judge result, attempts, errors, prompt_reduction, evidence_reduced,
    judge_http_errors). The first request is `prompt` as given; at most one reduced request follows it."""
    raw: list[dict[str, object]] = []

    def logged(kind: str, text: str, max_attempts: int, offset: int):
        log: list[dict[str, object]] = []
        try:
            return send(text, max_attempts, log)
        finally:
            raw.extend(dict(item, prompt=kind, attempt=offset + int(item["attempt"])) for item in log)

    def samples() -> list[dict[str, object]]:
        return [{"prompt": item["prompt"], "attempt": item["attempt"], "status": item["status"],
                 "body_bytes": len(item.get("body") or b""), "body_sample": redacted_sample(item.get("body"), secrets)}
                for item in raw]

    try:
        result, count = logged("original", prompt, MAX_TRANSPORT_ATTEMPTS, 0)
        return result, count, [], None, False, samples()
    except EvalRequestError as exc:
        count, first_error, status, body = exc.attempts, str(exc), exc.status, exc.body
    original = prompt_size(prompt, extra)
    trigger = context_overflow(status, body, int(original["estimated_tokens"]))
    if trigger is None:
        return {}, count, [first_error], None, False, samples()
    budget = input_budget(trigger)
    reported = trigger.get("provider_input_tokens")
    force_all = int(original["estimated_tokens"]) <= budget or (
        isinstance(reported, int) and reported > int(original["estimated_tokens"]))
    reduced, steps, outcome = rebuild(budget, force_all)
    receipt: dict[str, object] = {
        "schema_version": "1.0", "non_scoring": True, "reason": trigger["reason"],
        "trigger": {"status": status, "attempt": count, "body_bytes": len(body or b""),
                    "body_sample": redacted_sample(body, secrets)},
        "provider_context_tokens": trigger.get("provider_context_tokens"), "provider_input_tokens": reported,
        "judge_context_tokens": JUDGE_CONTEXT_TOKENS, "max_output_tokens": JUDGE_MAX_OUTPUT_TOKENS,
        "budget_estimated_tokens": budget, "all_steps_forced": force_all,
        "original": original, "steps": steps, "outcome": outcome,
        "reduced": prompt_size(reduced, extra) if reduced is not None else None, "retry": None}
    if reduced is None:
        return {}, count, [first_error, "judge prompt does not fit the judge context after evidence reduction: "
                           + outcome], receipt, False, samples()
    try:
        result, more = logged("reduced", reduced, max(1, MAX_TRANSPORT_ATTEMPTS - count), count)
    except EvalRequestError as retry:
        receipt["retry"] = {"attempts": retry.attempts, "outcome": "failed", "status": retry.status,
                            "error": str(retry)}
        return ({}, count + retry.attempts, [first_error, "reduced-evidence judge retry failed: " + str(retry)],
                receipt, False, samples())
    except Exception as retry:
        receipt["retry"] = {"attempts": None, "outcome": "failed", "status": None, "error": str(retry)}
        return {}, count, [first_error, "reduced-evidence judge retry failed: " + str(retry)], receipt, False, samples()
    receipt["retry"] = {"attempts": more, "outcome": "completed", "status": 200, "error": None}
    return result, count + more, [], receipt, True, samples()
# --- end judge context overflow fallback ---


def _apply_judge_config() -> None:
    """Judge endpoint, model and effort from the evaluator configuration (AGENTSWE_JUDGE_*)."""
    global ENDPOINT, MODEL, REASONING_EFFORT
    ENDPOINT = os.environ.get("AGENTSWE_JUDGE_RESPONSES_URL") or ENDPOINT
    MODEL = os.environ.get("AGENTSWE_JUDGE_MODEL") or MODEL
    REASONING_EFFORT = os.environ.get("AGENTSWE_JUDGE_EFFORT") or REASONING_EFFORT


def build_prompt(eval_prompt: str, rubric: str, case_id: str, case_input: str, harness_view: object,
                 candidate_output: Path, secrets: tuple[str, ...], omit=None) -> str:
    """The Result-judge prompt (before redaction). With text_evidence(harness) and no omit hook it is the prompt the
    paper's run_eval.py builds, byte for byte; the overflow fallback passes a reduced view and an omit hook."""
    return (
        eval_prompt
        + "\n\n# Rubric\n"
        + rubric
        + f"\n\n# Active case: {case_id}\n"
        + case_input
        + "\n\n# Harness result (validity and scoreability evidence)\n"
        + json.dumps(harness_view, indent=2, sort_keys=True)
        + "\n\n# Candidate final artifacts (source is intentionally unavailable)\n"
        + tree_text(candidate_output, secrets, omit)
        + "\n\nReturn one JSON object only. Required shape: "
        + json.dumps({
            "case_id": case_id,
            "evaluation_state": "scoreable",
            "validity_gate": True,
            "dimensions": {name: {"score": 0, "max": maximum, "evidence": ""} for name, maximum in {
                                                                                                       "request_artifact_compliance": 8,
                                                                                                       "numerical_sql_correctness": 36,
                                                                                                       "business_definition_coherence": 20,
                                                                                                       "privacy_insufficiency": 18,
                                                                                                       "anomaly_auditability": 12,
                                                                                                       "chart_communication_consistency": 6,
                                                                                                   }.items()},
            "score": 0,
            "major_errors": [],
            "assessment": "",
        }, sort_keys=True)
    )


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
    # Use the pinned task environment. Only this Eval Job gets the GATEWAY allowlist.
    env_prefix = str(manifest["container_env_prefix"])
    os.environ["PATH"] = env_prefix + "/bin:" + os.environ.get("PATH", "")
    os.environ["SSL_CERT_FILE"] = str(eval_ca_bundle(Path(env_prefix)))
    os.environ["HOME"] = "/tmp/harbor-database-analytics-eval-home"
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
    harness["fatal_gate"] = candidate_execution_contract.get("fatal_gate") is True
    harness["fatal_reasons"] = []
    harness["advisory_findings"] = []
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
        harness["validity_gate"] = False
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
    harness["evaluation_state"] = "fatal_zero" if harness["fatal_gate"] else "scoreable"
    harness = redact_object(harness, secret_strings)
    if not isinstance(harness, dict):
        raise RuntimeError("redacted harness result is not an object")
    prompt_inputs = (
        args.eval_prompt.read_text(encoding="utf-8"),
        args.rubric.read_text(encoding="utf-8"),
        case_id,
        (case_dir / "input.md").read_text(encoding="utf-8"),
    )
    prompt = build_prompt(*prompt_inputs, text_evidence(harness), args.candidate_output, secret_strings)
    api_key = env_values.get("AGENTSWE_JUDGE_API_KEY") or os.environ.get("DEEPSEEK_API_KEY") or env_values.get("DEEPSEEK_API_KEY") or os.environ.get("GATEWAY_API_KEY") or env_values.get("GATEWAY_API_KEY")
    prompt = redact_text(prompt, secret_strings)
    started = time.monotonic()
    errors: list[str] = []
    model_result: dict[str, object] = {}
    count = 0
    prompt_reduction: dict[str, object] | None = None
    evidence_reduced = False
    judge_http_errors: list[dict[str, object]] = []
    if harness.get("fatal_gate") is True:
        # Only registered fatal conditions bypass semantic scoring.
        model_result = {"dimensions": {}, "score": 0}
    elif not api_key:
        errors.append("DEEPSEEK_API_KEY is not available")
    else:
        redaction = secret_strings + (api_key,)
        image_parts = images(harness)
        image_tokens = image_token_estimate(image_parts)

        def send(text: str, max_attempts: int, log: list) -> tuple[dict[str, object], int]:
            return call_gateway(text, api_key, args.timeout, image_parts, max_attempts=max_attempts, http_errors=log)

        def rebuild(budget: int, force_all: bool):
            def render(view: object, omit) -> str:
                return redact_text(build_prompt(*prompt_inputs, view, args.candidate_output, secret_strings, omit),
                                   secret_strings)
            return reduce_judge_prompt(render, text_evidence(harness), args.candidate_output, budget,
                                       force_all=force_all, extra=image_tokens)

        try:
            (model_result, count, judge_errors, prompt_reduction, evidence_reduced,
             judge_http_errors) = judge_with_fallback(send, prompt, rebuild, redaction, image_tokens)
            errors.extend(judge_errors)
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
        "trusted_evidence_sha256": __import__("hashlib").sha256(json.dumps(harness,sort_keys=True,separators=(",",":")).encode()).hexdigest(),
        "major_errors": model_result.get("major_errors", []),
        "assessment": model_result.get("assessment", ""),
        "provider_counts": {"gateway_text": 0 if images(harness) else count, "gateway_image": count if images(harness) else 0, "serper": 0, "web_retrieval": 0, "deepseek": 0},
        "harness_result": harness,
        "credential_leak_detected": credential_leak_detected,
        "resource_secrets_redacted": True,
        "errors": result_errors,
        "api_usage": getattr(call_gateway, "last_usage", None),
        "runtime_seconds": round(time.monotonic() - started, 3),
        "image_receipts": [{"sha256":__import__("hashlib").sha256(__import__("base64").b64decode(item["image_url"].split(",",1)[1])).hexdigest()} for item in images(harness)],
        # Non-scoring: verify_score.py reads none of these three fields.
        "evidence_reduced": evidence_reduced,
        "prompt_reduction": redact_object(prompt_reduction, secret_strings),
        "judge_http_errors": judge_http_errors,
    }
    if prompt_reduction is not None:
        write_json(args.output_dir / "prompt_reduction.json", eval_result["prompt_reduction"])
    write_json(args.output_dir / "eval_result.json", eval_result)
    # Never print the response or transport headers; Harbor captures only this marker.
    print(json.dumps({"case_id": case_id, "gateway_text": count, "errors": len(errors)}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
