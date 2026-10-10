#!/usr/bin/env python3
"""Evaluator-owned Responses relay that preserves streaming upstream bodies."""
from __future__ import annotations

import argparse
import json
import threading
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

MODEL = "deepseek-flash"
EFFORT = "high"
SCHEMA = "agentswe-broker-stats/v1"


def load_key(path: Path) -> str:
    values: dict[str, str] = {}
    for raw in path.read_text(encoding="utf-8").splitlines():
        key, sep, value = raw.partition("=")
        if sep and key.strip() in {"DEEPSEEK_API_KEY", "OPENAI_API_KEY"}:
            values[key.strip()] = value.strip().strip("'\"")
    for key in ("DEEPSEEK_API_KEY", "OPENAI_API_KEY"):
        if values.get(key):
            return values[key]
    raise RuntimeError("credential file has no supported provider key")


class Stats:
    def __init__(self, path: Path) -> None:
        self.lock = threading.Lock()
        self.path = path
        self.value: dict[str, Any] = {
            "schema_version": SCHEMA,
            "role": "lower",
            "protocol": {"model": MODEL, "reasoning_effort": EFFORT, "transport": "responses-relay"},
            "runtime": {"calls": 0, "failures": 0, "successful_calls": 0, "input_tokens": 0, "output_tokens": 0, "total_tokens": 0},
            "requests": [],
        }
        self._write()

    def _write(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(json.dumps(self.value, indent=2) + "\n", encoding="utf-8")

    def record(self, ok: bool, usage: dict[str, Any] | None = None, error: str | None = None) -> None:
        with self.lock:
            runtime = self.value["runtime"]
            runtime["calls"] += 1
            runtime["successful_calls"] += int(ok)
            runtime["failures"] += int(not ok)
            usage = usage or {}
            inp = int(usage.get("input_tokens", usage.get("prompt_tokens", 0)) or 0)
            out = int(usage.get("output_tokens", usage.get("completion_tokens", 0)) or 0)
            total = int(usage.get("total_tokens", inp + out) or 0)
            runtime["input_tokens"] += max(0, inp)
            runtime["output_tokens"] += max(0, out)
            runtime["total_tokens"] += max(0, total)
            self.value["requests"].append({"ok": ok, "model": MODEL, "reasoning_effort": EFFORT, "input_tokens": inp, "output_tokens": out, "total_tokens": total, "error": error})
            self._write()

    def snapshot(self) -> dict[str, Any]:
        with self.lock:
            return json.loads(json.dumps(self.value))


def serve(bind: str, port: int, upstream: str, credential_file: Path, stats: Stats) -> None:
    key = load_key(credential_file)
    upstream_url = upstream.rstrip("/") + "/v1/responses"

    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def log_message(self, _format: str, *_args: object) -> None:
            return

        def send_json(self, status: int, value: object) -> None:
            body = json.dumps(value, ensure_ascii=False).encode()
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Connection", "close")
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self) -> None:  # noqa: N802
            if self.path == "/healthz":
                self.send_json(200, {"ok": True, "schema_version": SCHEMA})
            elif self.path == "/stats":
                self.send_json(200, stats.snapshot())
            else:
                self.send_json(404, {"error": "not_found"})

        def do_POST(self) -> None:  # noqa: N802
            if self.path != "/v1/responses":
                self.send_json(404, {"error": "not_found"})
                return
            try:
                size = int(self.headers.get("Content-Length", "0"))
                payload = json.loads(self.rfile.read(size))
                if not isinstance(payload, dict):
                    raise ValueError("request must be an object")
                payload["model"] = MODEL
                payload["reasoning"] = {"effort": EFFORT}
                body = json.dumps(payload).encode()
                request = urllib.request.Request(
                    upstream_url,
                    data=body,
                    method="POST",
                    headers={
                        "Authorization": f"Bearer {key}",
                        "Content-Type": "application/json",
                        "Accept": "text/event-stream, application/json",
                        "User-Agent": "AgentSWE-OpenClaw-Smoke-Streaming-Relay/1.0",
                    },
                )
                with urllib.request.urlopen(request, timeout=240) as response:
                    content_type = response.headers.get("Content-Type", "application/json")
                    response_body = response.read()
                    usage: dict[str, Any] = {}
                    if "json" in content_type.lower():
                        try:
                            parsed = json.loads(response_body)
                            if isinstance(parsed, dict) and isinstance(parsed.get("usage"), dict):
                                usage = parsed["usage"]
                        except (UnicodeDecodeError, json.JSONDecodeError):
                            pass
                    stats.record(True, usage)
                    self.send_response(response.status)
                    self.send_header("Content-Type", content_type)
                    self.send_header("Content-Length", str(len(response_body)))
                    self.send_header("Connection", "close")
                    self.end_headers()
                    self.wfile.write(response_body)
            except urllib.error.HTTPError as exc:
                stats.record(False, error=f"HTTPError:{exc.code}")
                self.send_json(502, {"error": "broker_upstream_failure", "type": "HTTPError", "status": exc.code})
            except Exception as exc:
                stats.record(False, error=type(exc).__name__)
                self.send_json(502, {"error": "broker_upstream_failure", "type": type(exc).__name__})

    ThreadingHTTPServer((bind, port), Handler).serve_forever()


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--bind", default="127.0.0.1")
    parser.add_argument("--port", type=int, required=True)
    parser.add_argument("--upstream", default="https://api.deepseek.com")
    parser.add_argument("--credential-file", type=Path, required=True)
    parser.add_argument("--stats-file", type=Path, required=True)
    args = parser.parse_args()
    serve(args.bind, args.port, args.upstream, args.credential_file, Stats(args.stats_file))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
