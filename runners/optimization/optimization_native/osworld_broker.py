#!/usr/bin/env python3
"""Evaluator-owned screenshot broker with a locked vision model and schema."""

from __future__ import annotations

import argparse
import base64
import hashlib
import http.server
import json
import os
import ssl
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

# The shared effort spellings (broker/agentswe_broker/config.py; the controller mounts the package read-only).
from agentswe_broker.config import normalize_effort
from osworld_agent import action_schema


# Release: the upstream Responses endpoint and model come from the evaluator credential file the
# runner writes (AGENTSWE_RUNTIME_RESPONSES_URL / _MODEL, see main()); the paper locked its
# provider gateway endpoint and gpt-5.6-sol. The OSWorld vision call keeps the paper's
# "high" effort unless AGENTSWE_OSWORLD_EFFORT says otherwise (it is not the runtime effort that the
# other Optimization brokers lock). The runner exports that setting (task.json config_env) and the
# controller passes it to this container; it is read with the role efforts' spellings
# (normalize_effort): none / off / unset leave the effort out of the request, explicit-none sends
# the literal "none" (DeepSeek's no-thinking mode), any other value is sent as given.
UPSTREAM = "https://api.deepseek.com/v1/responses"
MODEL = "deepseek-flash"
EFFORT: str | None = "high"
# Strict structured output, as in the paper. Some Responses providers (the release's default DeepSeek
# endpoint among them) reject the nullable type unions of this schema in strict mode; for those the
# runner sets AGENTSWE_OSWORLD_SCHEMA_STRICT=0, and the broker then requests JSON-object output and
# states the same action schema in an evaluator-owned instruction (JSON_OBJECT_INSTRUCTIONS). Every
# action is validated against the schema by osworld_agent either way, so a malformed action stays an
# ordinary agent failure.
SCHEMA_STRICT = True
JSON_OBJECT_INSTRUCTIONS = (
    "Respond with exactly one JSON object, no prose. It must validate against this JSON Schema "
    "(every listed property present; use null where it does not apply):\n"
)
MAX_BODY = 12 * 1024 * 1024
MAX_IMAGE_BYTES = 8 * 1024 * 1024
MAX_ATTEMPTS = 5


def dotenv(path: Path) -> dict[str, str]:
    values: dict[str, str] = {}
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if line and not line.startswith("#") and "=" in line:
            key, value = line.split("=", 1)
            values[key.strip()] = value.strip().strip("'\"")
    return values


def extract_png_data_url(value: Any) -> tuple[str, int]:
    if not isinstance(value, str) or not value.startswith("data:image/png;base64,"):
        raise ValueError("only inline PNG input_image is allowed")
    encoded = value.split(",", 1)[1]
    try:
        payload = base64.b64decode(encoded, validate=True)
    except ValueError as exc:
        raise ValueError("invalid image base64") from exc
    if not payload.startswith(b"\x89PNG\r\n\x1a\n"):
        raise ValueError("image is not PNG")
    if len(payload) > MAX_IMAGE_BYTES:
        raise ValueError("image exceeds byte limit")
    return hashlib.sha256(payload).hexdigest(), len(payload)


def locked_payload(body: Any) -> tuple[dict[str, Any], dict[str, Any]]:
    if not isinstance(body, dict) or set(body) != {"input", "screen"}:
        raise ValueError("request must contain exactly input and screen")
    screen = body["screen"]
    if not isinstance(screen, dict) or set(screen) != {"width", "height"}:
        raise ValueError("screen must contain width and height")
    width, height = screen["width"], screen["height"]
    if isinstance(width, bool) or not isinstance(width, int) or not 640 <= width <= 3840:
        raise ValueError("invalid screen width")
    if isinstance(height, bool) or not isinstance(height, int) or not 480 <= height <= 2160:
        raise ValueError("invalid screen height")
    messages = body["input"]
    if not isinstance(messages, list) or len(messages) != 1:
        raise ValueError("exactly one user message is required")
    message = messages[0]
    if not isinstance(message, dict) or set(message) != {"role", "content"} or message["role"] != "user":
        raise ValueError("invalid user message")
    content = message["content"]
    if not isinstance(content, list) or len(content) != 2:
        raise ValueError("message requires one text and one image")
    text_part, image_part = content
    if not isinstance(text_part, dict) or set(text_part) != {"type", "text"} or text_part["type"] != "input_text":
        raise ValueError("invalid text part")
    text = text_part["text"]
    if not isinstance(text, str) or not text or len(text) > 40_000:
        raise ValueError("invalid input text")
    if not isinstance(image_part, dict) or set(image_part) != {"type", "image_url"} or image_part["type"] != "input_image":
        raise ValueError("invalid image part")
    image_sha256, image_bytes = extract_png_data_url(image_part["image_url"])
    upstream: dict[str, Any] = {"model": MODEL}
    if EFFORT is not None:
        upstream["reasoning"] = {"effort": EFFORT}
    upstream.update({
        "input": messages,
        "text": {
            "format": {
                "type": "json_schema",
                "name": "osworld_single_action",
                "strict": True,
                "schema": action_schema(width, height),
            }
        },
        "store": False,
    })
    if not SCHEMA_STRICT:
        upstream["text"] = {"format": {"type": "json_object"}}
        upstream["instructions"] = JSON_OBJECT_INSTRUCTIONS + json.dumps(
            action_schema(width, height), separators=(",", ":"))
    return upstream, {"image_sha256": image_sha256, "image_bytes": image_bytes, "width": width, "height": height}


def usage_tokens(payload: bytes) -> int:
    try:
        usage = json.loads(payload).get("usage", {})
        return int(usage.get("total_tokens", 0) or 0)
    except (ValueError, TypeError, AttributeError):
        return 0


def upstream_error(exc: urllib.error.HTTPError) -> str:
    """Keep only bounded provider diagnostics; never log requests or credentials."""
    try:
        payload = json.loads(exc.read(64 * 1024))
        error = payload.get("error", {}) if isinstance(payload, dict) else {}
        if not isinstance(error, dict):
            error = {}
        parts = [f"http_{exc.code}"]
        for key in ("type", "code", "message"):
            value = error.get(key)
            if isinstance(value, str) and value:
                parts.append(value.replace("\n", " ")[:500])
        return ":".join(parts)[:1200]
    except Exception:
        return f"http_{exc.code}"


class Handler(http.server.BaseHTTPRequestHandler):
    server_version = "OSWorldVisionBroker/1.0"

    def do_GET(self) -> None:  # noqa: N802
        if self.path == "/healthz":
            self._reply(200, b'{"status":"ok"}\n')
            return
        if self.path == "/stats" and self.headers.get("Authorization") == f"Bearer {self.server.stats_token}":  # type: ignore[attr-defined]
            self._reply(200, json.dumps(self.server.public_stats(), sort_keys=True).encode() + b"\n")  # type: ignore[attr-defined]
            return
        self.send_error(404)

    def do_POST(self) -> None:  # noqa: N802
        if self.path.rstrip("/") != "/v1/osworld/action":
            self.send_error(404)
            return
        if self.headers.get("Authorization") != f"Bearer {self.server.runtime_token}":  # type: ignore[attr-defined]
            self.send_error(401)
            return
        try:
            length = int(self.headers.get("Content-Length", "0"))
            if length < 1 or length > MAX_BODY:
                raise ValueError("invalid body size")
            upstream, evidence = locked_payload(json.loads(self.rfile.read(length)))
        except (ValueError, TypeError, json.JSONDecodeError) as exc:
            self.send_error(400, str(exc))
            return
        allowed, reason = self.server.reserve_call()  # type: ignore[attr-defined]
        if not allowed:
            self._reply(429, json.dumps({"error": {"type": "budget_exceeded", "message": reason}}).encode())
            return
        request = urllib.request.Request(
            UPSTREAM,
            data=json.dumps(upstream, ensure_ascii=False).encode(),
            headers={
                "Authorization": f"Bearer {self.server.api_key}",  # type: ignore[attr-defined]
                "Content-Type": "application/json",
                "Accept": "application/json",
                "User-Agent": "AgentSWE-OSWorld-Broker/1.0",
            },
            method="POST",
        )
        payload: bytes | None = None
        last_error = "unknown"
        status = 502
        for attempt in range(1, MAX_ATTEMPTS + 1):
            try:
                with urllib.request.urlopen(request, timeout=240) as response:
                    payload, status = response.read(), response.status
                break
            except urllib.error.HTTPError as exc:
                last_error = upstream_error(exc)
                if exc.code not in {429, 500, 502, 503, 504}:
                    break
            except (urllib.error.URLError, TimeoutError, OSError, ssl.SSLError) as exc:
                last_error = f"transport_{type(exc).__name__}"
            if attempt < MAX_ATTEMPTS:
                time.sleep(min(8, 2 ** (attempt - 1)))
        if payload is None:
            self.server.finish_call(0, last_error, evidence)  # type: ignore[attr-defined]
            self._reply(502, json.dumps({"error": {"type": "upstream_error", "message": last_error}}).encode())
            return
        budget_error = self.server.finish_call(usage_tokens(payload), None, evidence)  # type: ignore[attr-defined]
        if budget_error:
            self._reply(429, json.dumps({"error": {"type": "budget_exceeded", "message": budget_error}}).encode())
            return
        self._reply(status, payload)

    def _reply(self, status: int, payload: bytes) -> None:
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def log_message(self, format: str, *args: object) -> None:
        return


def make_server(
    address: tuple[str, int], api_key: str, runtime_token: str, stats_token: str,
    max_calls: int, max_tokens: int,
) -> http.server.ThreadingHTTPServer:
    class Server(http.server.ThreadingHTTPServer):
        def __init__(self) -> None:
            super().__init__(address, Handler)
            self.api_key = api_key
            self.runtime_token = runtime_token
            self.stats_token = stats_token
            self.max_calls = max_calls
            self.max_tokens = max_tokens
            self.lock = threading.Lock()
            self.stats = {
                "model": MODEL, "reasoning_effort": EFFORT, "schema_strict": SCHEMA_STRICT, "output_mode": "json_schema_strict" if SCHEMA_STRICT else "json_object_schema_instructions", "calls": 0,
                "tokens": 0, "failures": 0, "budget_exceeded": False,
                "inflight": 0, "images": [],
            }

        def reserve_call(self) -> tuple[bool, str]:
            with self.lock:
                if self.stats["inflight"]:
                    return False, "concurrent_call_rejected"
                if self.stats["calls"] >= self.max_calls:
                    self.stats["budget_exceeded"] = True
                    return False, "model_call_budget_exceeded"
                if self.stats["tokens"] >= self.max_tokens:
                    self.stats["budget_exceeded"] = True
                    return False, "model_token_budget_exceeded"
                self.stats["calls"] += 1
                self.stats["inflight"] = 1
                return True, ""

        def finish_call(self, tokens: int, error: str | None, evidence: dict[str, Any]) -> str:
            with self.lock:
                self.stats["inflight"] = 0
                self.stats["tokens"] += max(0, tokens)
                self.stats["images"].append(evidence)
                if error:
                    self.stats["failures"] += 1
                if self.stats["tokens"] > self.max_tokens:
                    self.stats["budget_exceeded"] = True
                    return "model_token_budget_exceeded"
                return ""

        def public_stats(self) -> dict[str, Any]:
            with self.lock:
                return json.loads(json.dumps(self.stats))

    return Server()


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--credential-file", type=Path, required=True)
    parser.add_argument("--bind", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8090)
    parser.add_argument("--max-calls", type=int, default=30)
    parser.add_argument("--max-tokens", type=int, default=100_000)
    parser.add_argument("--runtime-token", required=True)
    parser.add_argument("--stats-token", required=True)
    args = parser.parse_args()
    global UPSTREAM, MODEL, EFFORT, SCHEMA_STRICT
    values = dotenv(args.credential_file)
    UPSTREAM = values.get("AGENTSWE_RUNTIME_RESPONSES_URL") or os.environ.get("AGENTSWE_RUNTIME_RESPONSES_URL") or UPSTREAM
    MODEL = values.get("AGENTSWE_RUNTIME_MODEL") or os.environ.get("AGENTSWE_RUNTIME_MODEL") or MODEL
    effort_setting = values.get("AGENTSWE_OSWORLD_EFFORT") or os.environ.get("AGENTSWE_OSWORLD_EFFORT")
    if effort_setting:  # unset keeps the paper's "high"
        EFFORT = normalize_effort(effort_setting)
    strict = values.get("AGENTSWE_OSWORLD_SCHEMA_STRICT") or os.environ.get("AGENTSWE_OSWORLD_SCHEMA_STRICT") or "1"
    SCHEMA_STRICT = strict.strip().lower() not in {"0", "false", "no"}
    api_key = values.get("AGENTSWE_RUNTIME_API_KEY") or values.get("GATEWAY_API_KEY", "")
    if not api_key:
        raise SystemExit("AGENTSWE_RUNTIME_API_KEY missing")
    if len(args.runtime_token) < 32 or len(args.stats_token) < 32:
        raise SystemExit("broker tokens must contain at least 32 characters")
    server = make_server(
        (args.bind, args.port), api_key, args.runtime_token, args.stats_token,
        args.max_calls, args.max_tokens,
    )
    try:
        server.serve_forever()
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
