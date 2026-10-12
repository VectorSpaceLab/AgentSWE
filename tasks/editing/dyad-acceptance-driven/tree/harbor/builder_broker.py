#!/usr/bin/env python3
"""Evaluator-owned Builder broker locked to deepseek-flash/xhigh.

The only credential visible to the Builder is the placeholder token. The real
provider key is read by this process and never appears in responses, stats, or
logs. This file is intentionally standard-library-only so the broker image is
small and deterministic.
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


def provider_key(path: Path) -> str:
    values: dict[str, str] = {}
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        if key.strip() in {"AGENTSWE_PROVIDER_KEY", "DEEPSEEK_API_KEY", "OPENAI_API_KEY"}:
            values[key.strip()] = value.strip().strip("'\"")
    for key in ("AGENTSWE_PROVIDER_KEY", "DEEPSEEK_API_KEY", "OPENAI_API_KEY"):
        if values.get(key):
            return values[key]
    raise RuntimeError("credential file has no supported provider key")


class State:
    def __init__(self) -> None:
        self.lock = threading.Lock()
        self.calls = 0
        self.failures = 0
        self.provider_failures = 0
        self.protocol_failures = 0
        self.delivery_failures = 0
        self.input_tokens = 0
        self.output_tokens = 0
        self.total_tokens = 0
        self.status_counts: dict[str, int] = {}

    def record(self, *, failure: str | None, status: int | None, usage: dict[str, Any] | None = None) -> None:
        with self.lock:
            self.calls += 1
            if failure:
                self.failures += 1
                if failure == "provider":
                    self.provider_failures += 1
                elif failure == "protocol":
                    self.protocol_failures += 1
                else:
                    self.delivery_failures += 1
            if status is not None:
                key = str(int(status))
                self.status_counts[key] = self.status_counts.get(key, 0) + 1
            usage = usage or {}
            self.input_tokens += int(usage.get("input_tokens", 0) or 0)
            self.output_tokens += int(usage.get("output_tokens", 0) or 0)
            self.total_tokens += int(usage.get("total_tokens", 0) or 0)

    def snapshot(self) -> dict[str, Any]:
        with self.lock:
            return {
                "schema_version": "agentswe-builder-broker-stats-v1",
                "protocol": {"model": MODEL, "reasoning_effort": EFFORT, "transport": "evaluator-owned-responses-broker"},
                "runtime": {
                    "calls": self.calls, "failures": self.failures,
                    "successful_calls": self.calls - self.failures,
                    "provider_failures": self.provider_failures,
                    "protocol_failures": self.protocol_failures,
                    "delivery_failures": self.delivery_failures,
                    "input_tokens": self.input_tokens,
                    "output_tokens": self.output_tokens,
                    "total_tokens": self.total_tokens,
                },
                "upstream": {"status_counts": dict(sorted(self.status_counts.items()))},
                "credential": {"builder_visible": PLACEHOLDER, "provider_secret_logged": False, "credential_value_recorded": False},
            }


class Handler(http.server.BaseHTTPRequestHandler):
    server_version = "agentswe-dyad-builder-broker/1"
    protocol_version = "HTTP/1.1"

    def log_message(self, _format: str, *_args: object) -> None:
        return

    @property
    def state(self) -> State:
        return self.server.state  # type: ignore[attr-defined]

    def send_json(self, code: int, value: object) -> None:
        payload = json.dumps(value, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def do_GET(self) -> None:  # noqa: N802
        if self.path == "/healthz":
            self.send_json(200, {"ok": True, "model": MODEL, "reasoning_effort": EFFORT})
        elif self.path == "/stats":
            if self.headers.get("Authorization") != f"Bearer {STATS_TOKEN}":
                self.send_json(401, {"error": "stats auth"})
            else:
                self.send_json(200, self.state.snapshot())
        else:
            self.send_json(404, {"error": "not_found"})

    def do_POST(self) -> None:  # noqa: N802
        if self.path.rstrip("/") != "/v1/responses":
            self.send_json(404, {"error": "not_found"})
            return
        if self.headers.get("Authorization") != f"Bearer {PLACEHOLDER}":
            self.state.record(failure="protocol", status=401)
            self.send_json(401, {"error": "Builder must use broker-only-placeholder"})
            return
        try:
            length = int(self.headers.get("Content-Length", "0"))
            if length <= 0 or length > 16 * 1024 * 1024:
                raise ValueError("invalid request length")
            body = json.loads(self.rfile.read(length))
            if not isinstance(body, dict):
                raise ValueError("request must be a JSON object")
            body["model"] = MODEL
            reasoning = body.get("reasoning") if isinstance(body.get("reasoning"), dict) else {}
            reasoning["effort"] = EFFORT
            body["reasoning"] = reasoning
        except (ValueError, json.JSONDecodeError, TypeError) as exc:
            self.state.record(failure="protocol", status=400)
            self.send_json(400, {"error": {"type": "broker_protocol_failure", "message": type(exc).__name__}})
            return
        request = urllib.request.Request(
            self.server.provider_url,  # type: ignore[attr-defined]
            data=json.dumps(body, ensure_ascii=False).encode("utf-8"),
            headers={"Authorization": f"Bearer {self.server.provider_key}",  # type: ignore[attr-defined]
                     "Content-Type": "application/json", "Accept": "application/json, text/event-stream",
                     "User-Agent": "agentswe-dyad-builder-broker/1"},
            method="POST",
        )
        try:
            with urllib.request.urlopen(request, timeout=240) as response:
                payload = response.read()
                content_type = response.headers.get("Content-Type", "application/json")
                decoded = json.loads(payload)
                usage = decoded.get("usage") if isinstance(decoded, dict) else None
                self.state.record(failure=None, status=response.status, usage=usage if isinstance(usage, dict) else None)
                self.send_response(200)
                self.send_header("Content-Type", content_type)
                self.send_header("Content-Length", str(len(payload)))
                self.end_headers()
                self.wfile.write(payload)
        except urllib.error.HTTPError as exc:
            self.state.record(failure="provider", status=exc.code)
            self.send_json(502, {"error": {"type": "provider_failure", "status_code": exc.code}})
        except (urllib.error.URLError, TimeoutError, OSError, ssl.SSLError, ValueError, json.JSONDecodeError) as exc:
            self.state.record(failure="provider", status=None)
            self.send_json(502, {"error": {"type": "provider_failure", "message": type(exc).__name__}})


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--credential-file", type=Path, required=True)
    parser.add_argument("--provider-url", default=os.environ.get("AGENTSWE_PROVIDER_URL", "https://api.deepseek.com/v1/responses"))
    parser.add_argument("--bind", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=0)
    args = parser.parse_args()
    server = http.server.ThreadingHTTPServer((args.bind, args.port), Handler)
    server.state = State()  # type: ignore[attr-defined]
    server.provider_url = args.provider_url  # type: ignore[attr-defined]
    server.provider_key = provider_key(args.credential_file.resolve())  # type: ignore[attr-defined]
    print(json.dumps({"ready": True, "endpoint": f"http://{args.bind}:{server.server_port}/v1/responses", "model": MODEL, "reasoning_effort": EFFORT}), flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        return 0
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

