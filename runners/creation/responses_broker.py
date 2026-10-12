#!/usr/bin/env python3
"""Credential-holding OpenAI Responses reverse proxy with a locked upstream."""

from __future__ import annotations

import argparse
import http.server
import json
import os
import ssl
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path


UPSTREAM = os.environ.get("AGENTSWE_RUNTIME_RESPONSES_URL", "https://api.deepseek.com/v1/responses")
MODEL = os.environ.get("AGENTSWE_RUNTIME_MODEL", "deepseek-flash")
MAX_BODY = 8 * 1024 * 1024
MAX_ATTEMPTS = 5
INTERIM_KEEPALIVE_SECONDS = 5.0
RUNTIME_TOKENS = {"broker-only-placeholder", "runtime-only-placeholder"}
JUDGE_TOKENS = {"judge-only-placeholder"}
STATS_TOKEN = "stats-only-placeholder"


def chat_messages_to_responses_input(messages: object) -> list[dict[str, object]]:
    """Normalize OpenAI chat messages into evaluator-locked Responses input."""
    if not isinstance(messages, list) or not messages:
        raise ValueError("messages must be a non-empty list")
    result: list[dict[str, object]] = []
    for message in messages:
        if not isinstance(message, dict):
            raise ValueError("message must be an object")
        role = message.get("role")
        if role not in {"system", "developer", "user", "assistant"}:
            raise ValueError("unsupported chat message role")
        content = message.get("content")
        if not isinstance(content, str) or not content:
            raise ValueError("chat message content must be non-empty text")
        result.append({
            "type": "message",
            "role": role,
            "content": [{
                "type": "output_text" if role == "assistant" else "input_text",
                "text": content,
            }],
        })
    return result


def responses_to_chat_completion(payload: bytes) -> bytes:
    """Convert a non-streaming Responses result to a minimal chat result."""
    value = json.loads(payload)
    if not isinstance(value, dict):
        raise ValueError("Responses result must be an object")
    text = value.get("output_text")
    if not isinstance(text, str):
        chunks: list[str] = []
        output = value.get("output")
        if isinstance(output, list):
            for item in output:
                if not isinstance(item, dict) or item.get("type") != "message":
                    continue
                content = item.get("content")
                if isinstance(content, list):
                    for part in content:
                        if isinstance(part, dict) and part.get("type") in {"output_text", "text"} and isinstance(part.get("text"), str):
                            chunks.append(part["text"])
        text = "".join(chunks)
    usage = value.get("usage") if isinstance(value.get("usage"), dict) else {}
    return json.dumps({
        "id": value.get("id", "response-proxy"),
        "object": "chat.completion",
        "created": int(time.time()),
        "model": MODEL,
        "choices": [{
            "index": 0,
            "message": {"role": "assistant", "content": text},
            "finish_reason": "stop",
        }],
        "usage": usage,
    }).encode("utf-8")


def dotenv(path: Path) -> dict[str, str]:
    result: dict[str, str] = {}
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if line and not line.startswith("#") and "=" in line:
            key, value = line.split("=", 1)
            result[key.strip()] = value.strip().strip("'\"")
    return result


class Handler(http.server.BaseHTTPRequestHandler):
    server_version = "AgentSWEResponsesBroker/1.0"
    protocol_version = "HTTP/1.1"

    def do_POST(self) -> None:  # noqa: N802
        path = self.path.rstrip("/")
        if path not in {"/v1/responses", "/v1/chat/completions"}:
            self.send_error(404)
            return
        role = self._request_role()
        if role is None:
            self.send_error(401)
            return
        allowed, limit_error = self.server.reserve_call(role)  # type: ignore[attr-defined]
        if not allowed:
            payload = json.dumps(
                {"error": {"message": limit_error, "type": "budget_exceeded"}}
            ).encode("utf-8")
            self.send_response(429)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)
            return
        try:
            length = int(self.headers.get("Content-Length", "0"))
        except ValueError:
            self.send_error(400)
            return
        if length <= 0 or length > MAX_BODY:
            self.send_error(413)
            return
        try:
            body = json.loads(self.rfile.read(length))
            if not isinstance(body, dict):
                raise ValueError
        except (json.JSONDecodeError, ValueError):
            self.send_error(400)
            return
        chat_mode = path == "/v1/chat/completions"
        if chat_mode:
            try:
                body["input"] = chat_messages_to_responses_input(body.pop("messages"))
            except (KeyError, TypeError, ValueError) as exc:
                self.send_error(400, str(exc))
                return
            body.pop("stream", None)
        # Lock evaluator-owned provider configuration regardless of candidate input.
        body["model"] = MODEL
        body["reasoning"] = {"effort": "medium"}
        body.pop("max_tokens", None)
        request = urllib.request.Request(
            self.server.upstream,  # type: ignore[attr-defined]
            data=json.dumps(body, ensure_ascii=False).encode("utf-8"),
            headers={
                "Authorization": f"Bearer {self.server.api_key}",  # type: ignore[attr-defined]
                "Content-Type": "application/json",
                "Accept": "application/json",
                "User-Agent": "AgentSWE-Tau3-Broker/1.0",
            },
            method="POST",
        )
        result: dict[str, object] = {}
        completed = threading.Event()

        def fetch_upstream() -> None:
            last_error = "unknown"
            payload: bytes | None = None
            status = 502
            content_type = "application/json"
            attempt = MAX_ATTEMPTS
            try:
                for attempt in range(1, MAX_ATTEMPTS + 1):
                    try:
                        with urllib.request.urlopen(request, timeout=240) as response:
                            payload = response.read()
                            status = response.status
                            content_type = response.headers.get(
                                "Content-Type", "application/json"
                            )
                        break
                    except urllib.error.HTTPError as exc:
                        last_error = f"http_{exc.code}"
                        if exc.code not in {429, 500, 502, 503, 504}:
                            break
                    except urllib.error.URLError as exc:
                        last_error = (
                            "transport_URLError_"
                            f"{type(exc.reason).__name__}"
                        )
                    except (TimeoutError, OSError, ssl.SSLError) as exc:
                        last_error = f"transport_{type(exc).__name__}"
                    if attempt < MAX_ATTEMPTS:
                        time.sleep(min(8.0, float(2 ** (attempt - 1))))
            except Exception as exc:  # keep the broker failure contract JSON-only
                last_error = f"broker_{type(exc).__name__}"
                payload = None
                status = 502
                content_type = "application/json"
            finally:
                result.update({
                    "last_error": last_error,
                    "payload": payload,
                    "status": status,
                    "content_type": content_type,
                    "attempt": attempt,
                })
                completed.set()

        threading.Thread(target=fetch_upstream, daemon=True).start()

        # Harbor's transparent allowlist sidecar closes an HTTP connection when
        # it sees no response bytes for 15 seconds.  Responses model calls can
        # legitimately take longer before their first byte.  Repeated 100
        # Continue responses are protocol-valid, ignored by ordinary HTTP
        # clients, and preserve the eventual upstream status and JSON body.
        client_connected = True
        while not completed.wait(INTERIM_KEEPALIVE_SECONDS):
            if not client_connected:
                continue
            try:
                self.send_response_only(100)
                self.end_headers()
                self.wfile.flush()
            except (BrokenPipeError, ConnectionResetError, OSError):
                client_connected = False

        last_error = str(result.get("last_error", "unknown"))
        payload_value = result.get("payload")
        payload = payload_value if isinstance(payload_value, bytes) else None
        status = int(result.get("status", 502))
        content_type = str(result.get("content_type", "application/json"))
        attempt = int(result.get("attempt", MAX_ATTEMPTS))
        if payload is None:
            status = 502
            payload = json.dumps({"error": {"message": f"upstream_{last_error}", "type": "upstream_error", "attempts": attempt}}).encode()
            content_type = "application/json"
            self.server.finish_call(role, None, last_error)  # type: ignore[attr-defined]
        else:
            limit_error = self.server.finish_call(role, payload, None)  # type: ignore[attr-defined]
            if limit_error:
                status = 429
                payload = json.dumps(
                    {"error": {"message": limit_error, "type": "budget_exceeded"}}
                ).encode("utf-8")
                content_type = "application/json"
        if chat_mode and payload is not None and status == 200:
            try:
                payload = responses_to_chat_completion(payload)
                content_type = "application/json"
            except (ValueError, json.JSONDecodeError):
                status = 502
                payload = json.dumps({"error": {"message": "invalid_responses_payload", "type": "upstream_error"}}).encode()
                content_type = "application/json"
        if not client_connected:
            self.server.record_delivery_failure(role)  # type: ignore[attr-defined]
            return
        try:
            self.send_response(status)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)
            self.wfile.flush()
        except (BrokenPipeError, ConnectionResetError, OSError):
            self.server.record_delivery_failure(role)  # type: ignore[attr-defined]

    def do_GET(self) -> None:  # noqa: N802
        if self.path == "/healthz":
            payload = b'{"status":"ok"}\n'
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)
            return
        if self.path == "/stats":
            auth = self.headers.get("Authorization", "")
            if auth != f"Bearer {STATS_TOKEN}":
                self.send_error(401)
                return
            payload = json.dumps(self.server.public_stats(), sort_keys=True).encode("utf-8") + b"\n"  # type: ignore[attr-defined]
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)
            return
        self.send_error(404)

    def _request_role(self) -> str | None:
        auth = self.headers.get("Authorization", "")
        token = auth.removeprefix("Bearer ").strip()
        if token in RUNTIME_TOKENS:
            return "runtime"
        if token in JUDGE_TOKENS:
            return "judge"
        return None

    def log_message(self, format: str, *args: object) -> None:
        return


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--credential-file", type=Path, required=True)
    parser.add_argument("--bind", default="0.0.0.0")
    parser.add_argument("--port", type=int, default=8080)
    parser.add_argument("--upstream", default=UPSTREAM)
    parser.add_argument("--max-runtime-calls", type=int, default=0)
    parser.add_argument("--max-runtime-tokens", type=int, default=0)
    args = parser.parse_args()
    api_key = dotenv(args.credential_file).get("AGENTSWE_RUNTIME_API_KEY", "")
    if not api_key:
        raise SystemExit("AGENTSWE_RUNTIME_API_KEY missing")
    class BrokerServer(http.server.ThreadingHTTPServer):
        def __init__(self, address: tuple[str, int]) -> None:
            super().__init__(address, Handler)
            self.lock = threading.Lock()
            self.max_runtime_calls = max(0, args.max_runtime_calls)
            self.max_runtime_tokens = max(0, args.max_runtime_tokens)
            self.stats = {
                "runtime": {
                    "calls": 0,
                    "tokens": 0,
                    "failures": 0,
                    "upstream_failures": 0,
                    "delivery_failures": 0,
                    "budget_exceeded": False,
                },
                "judge": {
                    "calls": 0,
                    "tokens": 0,
                    "failures": 0,
                    "upstream_failures": 0,
                    "delivery_failures": 0,
                    "budget_exceeded": False,
                },
            }

        def reserve_call(self, role: str) -> tuple[bool, str]:
            with self.lock:
                current = self.stats[role]
                if role == "runtime" and self.max_runtime_calls and current["calls"] >= self.max_runtime_calls:
                    current["budget_exceeded"] = True
                    return False, "runtime_call_budget_exceeded"
                if role == "runtime" and self.max_runtime_tokens and current["tokens"] >= self.max_runtime_tokens:
                    current["budget_exceeded"] = True
                    return False, "runtime_token_budget_exceeded"
                current["calls"] += 1
                return True, ""

        def finish_call(self, role: str, payload: bytes | None, error: str | None) -> str:
            with self.lock:
                if error:
                    self.stats[role]["failures"] += 1
                    self.stats[role]["upstream_failures"] += 1
                    return ""
                self.stats[role]["tokens"] += response_tokens(payload or b"")
                if (
                    role == "runtime" and self.max_runtime_tokens
                    and self.stats[role]["tokens"] > self.max_runtime_tokens
                ):
                    self.stats[role]["budget_exceeded"] = True
                    return "runtime_token_budget_exceeded"
                return ""

        def record_delivery_failure(self, role: str) -> None:
            with self.lock:
                self.stats[role]["failures"] += 1
                self.stats[role]["delivery_failures"] += 1

        def public_stats(self) -> dict[str, object]:
            with self.lock:
                return {
                    "runtime": dict(self.stats["runtime"]),
                    "judge": dict(self.stats["judge"]),
                    "max_runtime_calls": self.max_runtime_calls,
                    "max_runtime_tokens": self.max_runtime_tokens,
                }

    server = BrokerServer((args.bind, args.port))
    server.api_key = api_key  # type: ignore[attr-defined]
    server.upstream = args.upstream  # type: ignore[attr-defined]
    server.serve_forever()
    return 0


def response_tokens(payload: bytes) -> int:
    """Extract aggregate usage from JSON or Responses SSE without retaining content."""
    candidates: list[dict[str, object]] = []
    try:
        value = json.loads(payload)
        if isinstance(value, dict):
            candidates.append(value)
    except (UnicodeDecodeError, json.JSONDecodeError):
        for raw in payload.splitlines():
            if not raw.startswith(b"data:"):
                continue
            data = raw[5:].strip()
            if not data or data == b"[DONE]":
                continue
            try:
                value = json.loads(data)
            except (UnicodeDecodeError, json.JSONDecodeError):
                continue
            if isinstance(value, dict):
                candidates.append(value)
    best = 0
    for value in candidates:
        response = value.get("response") if isinstance(value.get("response"), dict) else value
        usage = response.get("usage") if isinstance(response, dict) else None
        if not isinstance(usage, dict):
            continue
        total = usage.get("total_tokens") or usage.get("totalTokens")
        if isinstance(total, (int, float)):
            best = max(best, int(total))
        else:
            input_tokens = usage.get("input_tokens") or usage.get("inputTokens") or 0
            output_tokens = usage.get("output_tokens") or usage.get("outputTokens") or 0
            if isinstance(input_tokens, (int, float)) and isinstance(output_tokens, (int, float)):
                best = max(best, int(input_tokens + output_tokens))
    return best


if __name__ == "__main__":
    raise SystemExit(main())
