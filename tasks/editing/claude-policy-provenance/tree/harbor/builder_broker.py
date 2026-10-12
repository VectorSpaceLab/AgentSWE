#!/usr/bin/env python3
"""Evaluator-owned Responses broker locked to deepseek-flash/xhigh.

This process is used only by the upper Builder.  The provider credential is
read by the broker and is never returned through health, stats, logs, or the
Builder environment.  Runtime clients authenticate with the fixed
``broker-only-placeholder`` token.
"""
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
from typing import Any


MODEL = "deepseek-flash"
EFFORT = "max"
PLACEHOLDER = "broker-only-placeholder"
STATS_TOKEN = "stats-only-placeholder"
MAX_BODY = 8 * 1024 * 1024
MAX_ATTEMPTS = 8
KEEPALIVE_SECONDS = 5.0
MAX_STREAM_SECONDS = 600.0
MAX_CALL_SECONDS = 1200.0


def read_sse_with_deadline(response: Any, *, deadline: float) -> bytes:
    """Read an SSE response with an interruptible absolute deadline."""

    chunks: list[bytes] = []
    errors: list[BaseException] = []
    done = threading.Event()

    def reader() -> None:
        try:
            while True:
                line = response.readline()
                if not line:
                    break
                chunks.append(line)
        except BaseException as exc:
            errors.append(exc)
        finally:
            done.set()

    threading.Thread(target=reader, daemon=True).start()
    remaining = max(0.0, deadline - time.monotonic())
    if not done.wait(remaining):
        # Some urllib/http.client response implementations take the same
        # internal read lock in ``close`` that a blocked ``readline`` owns.
        # Closing inline would therefore defeat the deadline and wedge this
        # request handler.  Close from a disposable daemon thread and return
        # control to the retry loop immediately.
        threading.Thread(target=response.close, daemon=True).start()
        done.wait(1.0)
        raise TimeoutError("responses_sse_deadline")
    if errors:
        raise errors[0]
    return b"".join(chunks)


def provider_key(path: Path) -> str:
    values: dict[str, str] = {}
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        if key.strip() in {"OPENAI_API_KEY", "DEEPSEEK_API_KEY", "AGENTSWE_PROVIDER_KEY"}:
            values[key.strip()] = value.strip().strip("'\"")
    # This evaluator deployment's proven Responses credential is DEEPSEEK_API_KEY.
    # Other legacy keys can coexist in the shared env file, so never select
    # them ahead of the canonical GATEWAY credential.
    for key in ("DEEPSEEK_API_KEY", "AGENTSWE_PROVIDER_KEY", "OPENAI_API_KEY"):
        if values.get(key):
            return values[key]
    raise RuntimeError("credential file has no supported provider key")


def response_text(value: dict[str, Any]) -> str:
    direct = value.get("output_text")
    if isinstance(direct, str):
        return direct
    chunks: list[str] = []
    for item in value.get("output", []) if isinstance(value.get("output"), list) else []:
        # Only assistant message items are the authored reply; reasoning items
        # (type "reasoning", parts "reasoning_text") precede them and are not text.
        if not isinstance(item, dict) or item.get("type") not in (None, "message"):
            continue
        for part in item.get("content", []) if isinstance(item.get("content"), list) else []:
            if (isinstance(part, dict) and isinstance(part.get("text"), str)
                    and part.get("type") in (None, "output_text", "text")):
                chunks.append(part["text"])
    return "".join(chunks)


def chat_to_responses(messages: object) -> list[dict[str, object]]:
    if not isinstance(messages, list) or not messages:
        raise ValueError("messages must be a non-empty list")
    result: list[dict[str, object]] = []
    for message in messages:
        if not isinstance(message, dict) or message.get("role") not in {"system", "developer", "user", "assistant"}:
            raise ValueError("invalid chat message")
        content = message.get("content")
        if not isinstance(content, str) or not content:
            raise ValueError("chat message content must be non-empty text")
        role = str(message["role"])
        result.append({"type": "message", "role": role, "content": [{
            "type": "output_text" if role == "assistant" else "input_text", "text": content,
        }]})
    return result


class State:
    def __init__(self) -> None:
        self.lock = threading.Lock()
        self.calls = 0
        self.completed_calls = 0
        self.successful_calls = 0
        self.failures = 0
        self.provider_failures = 0
        self.protocol_failures = 0
        self.delivery_failures = 0
        self.input_tokens = 0
        self.output_tokens = 0
        self.total_tokens = 0
        self.upstream_status_counts: dict[str, int] = {}

    def reserve(self) -> None:
        with self.lock:
            self.calls += 1

    def finish(self, *, failure: str | None, status: int | None = None,
               usage: dict[str, Any] | None = None) -> None:
        with self.lock:
            self.completed_calls += 1
            if failure:
                self.failures += 1
                if failure == "provider":
                    self.provider_failures += 1
                elif failure == "protocol":
                    self.protocol_failures += 1
                else:
                    self.delivery_failures += 1
            else:
                self.successful_calls += 1
            if status is not None:
                key = str(status)
                self.upstream_status_counts[key] = self.upstream_status_counts.get(key, 0) + 1
            usage = usage or {}
            self.input_tokens += int(usage.get("input_tokens", 0) or 0)
            self.output_tokens += int(usage.get("output_tokens", 0) or 0)
            self.total_tokens += int(usage.get("total_tokens", 0) or 0)

    def stats(self) -> dict[str, Any]:
        with self.lock:
            return {
                "schema_version": "agentswe-builder-broker-stats/v1",
                "protocol": {"model": MODEL, "reasoning_effort": EFFORT},
                "runtime": {
                    "calls": self.calls, "failures": self.failures,
                    "completed_calls": self.completed_calls,
                    "successful_calls": self.successful_calls,
                    "in_flight_calls": max(0, self.calls - self.completed_calls),
                    # Compatibility alias used by lower-level lifecycle
                    # adapters for their zero-call gate.
                    "tokens": self.total_tokens,
                    "provider_failures": self.provider_failures,
                    "protocol_failures": self.protocol_failures,
                    "delivery_failures": self.delivery_failures,
                    "input_tokens": self.input_tokens, "output_tokens": self.output_tokens,
                    "total_tokens": self.total_tokens,
                },
                "upstream": {"status_counts": dict(sorted(self.upstream_status_counts.items()))},
                "credential": {"builder_visible": PLACEHOLDER, "provider_secret_logged": False,
                               "credential_value_recorded": False},
            }

    def record_delivery_failure(self) -> None:
        """Record a broken client connection without double-counting a call."""
        with self.lock:
            self.delivery_failures += 1
            self.failures += 1
            if self.successful_calls > 0:
                self.successful_calls -= 1


class Handler(http.server.BaseHTTPRequestHandler):
    server_version = "agentswe-claude-builder-broker/1"
    protocol_version = "HTTP/1.1"

    @property
    def state(self) -> State:
        return self.server.state  # type: ignore[attr-defined]

    def log_message(self, _format: str, *_args: object) -> None:
        return

    def send_json(self, status: int, value: object) -> None:
        payload = json.dumps(value, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def do_GET(self) -> None:  # noqa: N802
        if self.path == "/healthz":
            self.send_json(200, {"ok": True, "status": "ok", "model": MODEL, "reasoning_effort": EFFORT})
        elif self.path == "/stats":
            if self.headers.get("Authorization") != f"Bearer {STATS_TOKEN}":
                self.send_json(401, {"error": "stats auth"})
            else:
                self.send_json(200, self.state.stats())
        else:
            self.send_json(404, {"error": "not found"})

    def do_POST(self) -> None:  # noqa: N802
        path = self.path.rstrip("/")
        if path not in {"/v1/responses", "/v1/chat/completions"}:
            self.send_json(404, {"error": "not found"})
            return
        if self.headers.get("Authorization") != f"Bearer {PLACEHOLDER}":
            self.send_json(401, {"error": "Builder must use placeholder credential"})
            return
        self.state.reserve()
        try:
            length = int(self.headers.get("Content-Length", "0"))
            if length <= 0 or length > MAX_BODY:
                raise ValueError("invalid request length")
            body = json.loads(self.rfile.read(length))
            if not isinstance(body, dict):
                raise ValueError("request is not an object")
            chat_mode = path == "/v1/chat/completions"
            if chat_mode:
                body["input"] = chat_to_responses(body.pop("messages"))
                body.pop("stream", None)
            require_responses_terminal = not chat_mode and body.get("stream") is True
            body["model"] = MODEL
            body["reasoning"] = {"effort": EFFORT}
            body.pop("max_tokens", None)
        except (ValueError, KeyError, json.JSONDecodeError) as exc:
            self.state.finish(failure="protocol")
            self.send_json(400, {"error": "broker_protocol_failure", "type": type(exc).__name__})
            return

        request = urllib.request.Request(
            self.server.provider_url,  # type: ignore[attr-defined]
            data=json.dumps(body, ensure_ascii=False).encode("utf-8"),
            headers={"Authorization": f"Bearer {self.server.provider_key}",  # type: ignore[attr-defined]
                     "Content-Type": "application/json", "Accept": "application/json",
                     "User-Agent": "agentswe-claude-builder-broker/1"},
            method="POST",
        )
        result: dict[str, Any] = {}
        complete = threading.Event()

        def fetch() -> None:
            last_error = "unknown"
            call_deadline = time.monotonic() + MAX_CALL_SECONDS
            try:
                for attempt in range(1, MAX_ATTEMPTS + 1):
                    if time.monotonic() >= call_deadline:
                        last_error = "transport_call_deadline"
                        break
                    try:
                        request_timeout = max(
                            1.0,
                            min(240.0, call_deadline - time.monotonic()),
                        )
                        with urllib.request.urlopen(
                            request, timeout=request_timeout
                        ) as response:
                            # A truncated chunked response raises
                            # http.client.IncompleteRead here.  Treat every
                            # response-read exception as a retryable transport
                            # failure; otherwise this worker can die before it
                            # signals ``complete`` and leave the Codex client
                            # waiting forever on the broker connection.
                            content_type = response.headers.get(
                                "Content-Type", ""
                            ).lower()
                            if "text/event-stream" in content_type:
                                # urllib's timeout is an inactivity timeout,
                                # not a total request deadline.  A malformed
                                # gateway stream can drip bytes forever and
                                # hold the one continuous Builder session
                                # indefinitely.  Read SSE by line and enforce
                                # an absolute per-attempt wall-clock bound.
                                stream_deadline = min(
                                    call_deadline,
                                    time.monotonic() + MAX_STREAM_SECONDS,
                                )
                                payload = read_sse_with_deadline(
                                    response, deadline=stream_deadline
                                )
                            else:
                                payload = response.read()
                            # A provider or gateway can close a streamed HTTP
                            # 200 cleanly at the transport layer while still
                            # truncating the Responses event stream.  Codex
                            # requires the response.completed event and will
                            # otherwise reconnect to a stream that this broker
                            # has already discarded.  Detect that semantic
                            # truncation here and retry the complete upstream
                            # request while the Builder call is still open.
                            if (
                                (require_responses_terminal or "text/event-stream" in content_type)
                                and b"response.completed" not in payload
                            ):
                                raise RuntimeError("incomplete_responses_sse")
                            result.update(
                                status=int(response.status),
                                payload=payload,
                                content_type=response.headers.get(
                                    "Content-Type", "application/json"
                                ),
                                attempt=attempt,
                            )
                        break
                    except urllib.error.HTTPError as exc:
                        last_error = f"http_{exc.code}"
                        result.update(status=exc.code)
                        # 522/524 are gateway timeout responses emitted by the
                        # evaluator's upstream edge.  They are transient for a
                        # long Responses turn and must be retried just like 502.
                        if exc.code not in {
                            429, 500, 502, 503, 504,
                            # Cloudflare/edge-origin transient failures.  A
                            # long xhigh Responses turn can surface any member
                            # of this family, not only 522/524.
                            520, 521, 522, 523, 524,
                        }:
                            break
                    except Exception as exc:
                        # Includes URLError, TimeoutError, SSL/OSError, and
                        # IncompleteRead/other malformed chunked transports.
                        # No such exception may escape this daemon thread.
                        last_error = f"transport_{type(exc).__name__}"
                        result.pop("payload", None)
                    if attempt < MAX_ATTEMPTS:
                        delay = min(8.0, float(2 ** (attempt - 1)))
                        if time.monotonic() + delay >= call_deadline:
                            last_error = "transport_call_deadline"
                            break
                        time.sleep(delay)
                result.setdefault("error", last_error)
            finally:
                # The request handler's keepalive loop must always terminate,
                # even if a previously unseen transport exception occurs.
                complete.set()

        threading.Thread(target=fetch, daemon=True).start()
        connected = True
        while not complete.wait(KEEPALIVE_SECONDS):
            if connected:
                try:
                    self.send_response_only(100)
                    self.end_headers()
                    self.wfile.flush()
                except (BrokenPipeError, ConnectionResetError, OSError):
                    connected = False
        payload = result.get("payload")
        if not isinstance(payload, bytes):
            status = int(result.get("status", 502))
            self.state.finish(failure="provider", status=status)
            payload = json.dumps({"error": {"type": "provider_failure", "message": result.get("error")}}).encode()
            response_status, content_type = 502, "application/json"
        else:
            response_status = int(result.get("status", 200))
            content_type = str(result.get("content_type", "application/json"))
            # Responses is allowed to return either one JSON object or a
            # standards-compliant SSE event stream.  The Codex Responses
            # client consumes both; parsing every Responses payload as JSON
            # incorrectly turns the SSE form into a provider failure.  Only
            # the legacy Chat Completions compatibility route needs a JSON
            # conversion here.
            if path == "/v1/chat/completions":
                try:
                    decoded = json.loads(payload)
                    usage = decoded.get("usage") if isinstance(decoded, dict) else None
                    text = response_text(decoded if isinstance(decoded, dict) else {})
                    payload = json.dumps({
                        "id": decoded.get("id", "response-proxy") if isinstance(decoded, dict) else "response-proxy",
                        "object": "chat.completion", "created": int(time.time()), "model": MODEL,
                        "choices": [{"index": 0, "message": {"role": "assistant", "content": text},
                                     "finish_reason": "stop"}], "usage": usage or {},
                    }).encode("utf-8")
                except (ValueError, json.JSONDecodeError):
                    self.state.finish(failure="provider", status=response_status)
                    payload = b'{"error":{"type":"provider_failure","message":"invalid upstream JSON"}}'
                    response_status, content_type = 502, "application/json"
                else:
                    self.state.finish(failure=None, status=response_status,
                                      usage=usage if isinstance(usage, dict) else None)
            else:
                # Do not inspect or rewrite Responses JSON/SSE.  It is a
                # successful provider delivery as long as the upstream gave
                # us a payload and HTTP status.
                self.state.finish(failure=None, status=response_status)
        if not connected:
            self.state.record_delivery_failure()
            return
        try:
            self.send_response(response_status)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)
        except (BrokenPipeError, ConnectionResetError, OSError):
            self.state.record_delivery_failure()


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--credential-file", type=Path, required=True)
    parser.add_argument("--provider-url", default=os.environ.get("AGENTSWE_PROVIDER_URL", "https://api.deepseek.com/v1/responses"))
    # Accepted for compatibility with the generic evaluator broker launcher.
    # The Builder broker remains hard-locked to xhigh and does not use runtime
    # budgets or caller-supplied role/effort values.
    parser.add_argument("--max-runtime-calls", type=int, default=0)
    parser.add_argument("--max-runtime-tokens", type=int, default=0)
    parser.add_argument("--stats-file", type=Path, default=None)
    parser.add_argument("--role", default="builder")
    parser.add_argument("--reasoning-effort", default=EFFORT)
    parser.add_argument("--bind", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=18081)
    args = parser.parse_args()
    server = http.server.ThreadingHTTPServer((args.bind, args.port), Handler)
    server.state = State()  # type: ignore[attr-defined]
    server.provider_url = args.provider_url  # type: ignore[attr-defined]
    server.provider_key = provider_key(args.credential_file.resolve())  # type: ignore[attr-defined]
    print(json.dumps({"ready": True, "model": MODEL, "reasoning_effort": EFFORT,
                      "bind": args.bind, "port": args.port}), flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        return 0
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
