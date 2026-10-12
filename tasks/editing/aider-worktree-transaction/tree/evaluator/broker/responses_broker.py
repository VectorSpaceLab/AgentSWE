#!/usr/bin/env python3
"""Evaluator-owned OpenAI-compatible broker for the real Aider lower agent."""
from __future__ import annotations

import argparse
import json
import os
import secrets
import threading
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

MODEL = "deepseek-flash"
EFFORT = "high"


class Ledger:
    def __init__(self, credential_file: Path | None, upstream: str) -> None:
        self.lock = threading.Lock()
        self.credential_file = credential_file
        self.upstream = upstream.rstrip("/")
        self.calls: list[dict[str, Any]] = []
        self.failures = 0
        self.input_tokens = 0
        self.output_tokens = 0

    def credential(self) -> str:
        if self.credential_file:
            values = {}
            for line in self.credential_file.read_text(encoding="utf-8").splitlines():
                if line.startswith(("OPENAI_API_KEY=", "DEEPSEEK_API_KEY=")):
                    name, value = line.split('=', 1)
                    values[name] = value.strip().strip("'\"")
            return values.get('DEEPSEEK_API_KEY') or values.get('OPENAI_API_KEY', '')
        return os.environ.get("AGENTSWE_UPSTREAM_API_KEY", "")

    def record(self, item: dict[str, Any]) -> None:
        with self.lock:
            self.calls.append(item)
            usage = item.get("usage") or {}
            self.input_tokens += int(usage.get("prompt_tokens", usage.get("input_tokens", 0)) or 0)
            self.output_tokens += int(usage.get("completion_tokens", usage.get("output_tokens", 0)) or 0)
            if item.get("failure"):
                self.failures += 1

    def stats(self) -> dict[str, Any]:
        with self.lock:
            return {
                "schema_version": "agentswe-broker-stats-v1",
                "protocol": {"model": MODEL, "reasoning_effort": EFFORT, "candidate_credential": "placeholder-only"},
                "runtime": {
                    "calls": len(self.calls), "failures": self.failures,
                    "successful_calls": len(self.calls) - self.failures,
                    "input_tokens": self.input_tokens, "output_tokens": self.output_tokens,
                    "total_tokens": self.input_tokens + self.output_tokens,
                },
                "calls": list(self.calls),
            }


def make_handler(ledger: Ledger):
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *_args: object) -> None:
            return

        def send_json(self, status: int, value: object) -> None:
            body = json.dumps(value, ensure_ascii=False).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self) -> None:  # noqa: N802
            if self.path == "/healthz":
                self.send_json(200, {"ok": True, "model": MODEL, "reasoning_effort": EFFORT})
            elif self.path == "/stats":
                self.send_json(200, ledger.stats())
            else:
                self.send_json(404, {"error": "not_found"})

        def do_POST(self) -> None:  # noqa: N802
            if self.path not in {"/v1/chat/completions", "/v1/responses"}:
                self.send_json(404, {"error": "unsupported_path"})
                return
            if self.headers.get('Authorization') != 'Bearer broker-only-placeholder':
                self.send_json(401, {'error': 'candidate_placeholder_required'})
                return
            try:
                raw = self.rfile.read(min(int(self.headers.get("Content-Length", "0")), 2_000_000))
                payload = json.loads(raw)
                if not isinstance(payload, dict):
                    raise ValueError("request is not an object")
                supplied_model = payload.get("model")
                payload["model"] = MODEL
                if self.path == '/v1/responses':
                    payload.pop('reasoning_effort', None)
                    payload['reasoning'] = {'effort': EFFORT}
                else:
                    payload.pop('reasoning', None)
                    payload['reasoning_effort'] = EFFORT
                payload["stream"] = False
                request = urllib.request.Request(
                    ledger.upstream + self.path.removeprefix("/v1"),
                    data=json.dumps(payload).encode("utf-8"), method="POST",
                    headers={
                        "Content-Type": "application/json",
                        "Accept": "application/json, text/event-stream",
                        "User-Agent": "AgentSWE-Tau3-Broker/1.0",
                    },
                )
                key = ledger.credential()
                if key:
                    request.add_header("Authorization", "Bearer " + key)
                with urllib.request.urlopen(request, timeout=180) as response:
                    result = json.loads(response.read(8_000_000))
                    usage = result.get("usage", {}) if isinstance(result, dict) else {}
                    ledger.record({
                        "request_id": secrets.token_hex(8), "path": self.path,
                        "supplied_model": supplied_model, "forced_model": MODEL,
                        "forced_reasoning_effort": EFFORT, "usage": usage, "failure": False,
                    })
                    self.send_json(response.status, result)
            except Exception as exc:
                ledger.record({
                    "request_id": secrets.token_hex(8), "path": self.path,
                    "forced_model": MODEL, "forced_reasoning_effort": EFFORT,
                    "failure": True, "error_type": type(exc).__name__,
                })
                self.send_json(502, {"error": {"type": "broker_failure", "message": f"{type(exc).__name__}: {str(exc)[-400:]}"}})

    return Handler


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--bind", default="127.0.0.1")
    parser.add_argument("--port", type=int, required=True)
    parser.add_argument("--upstream", default="https://api.deepseek.com/v1")
    parser.add_argument("--credential-file", type=Path)
    args = parser.parse_args()
    server = ThreadingHTTPServer((args.bind, args.port), make_handler(Ledger(args.credential_file, args.upstream)))
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        return 0
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
