#!/usr/bin/env python3
"""Evaluator-owned OpenAI-compatible broker; Candidate never receives its key."""

from __future__ import annotations

import argparse
import json
import os
import threading
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

from protocol import LOWER_EFFORT, LOWER_MODEL, write_json


class BrokerState:
    def __init__(self, credential_file: Path, stats_file: Path) -> None:
        self.credential_file, self.stats_file = credential_file, stats_file
        self.lock = threading.Lock()
        self.stats: dict[str, Any] = {
            "schema_version": 1, "model": LOWER_MODEL, "reasoning_effort": LOWER_EFFORT,
            "calls": 0, "successful_calls": 0, "failures": 0, "prompt_tokens": 0,
            "completion_tokens": 0, "total_tokens": 0, "forced_overrides": 0,
            "last_error": None,
        }

    def record(self, **updates: Any) -> None:
        with self.lock:
            for key, value in updates.items():
                self.stats[key] = self.stats.get(key, 0) + value if isinstance(value, int) else value
            write_json(self.stats_file, self.stats)


def _credential(path: Path) -> str:
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        name, value = line.split("=", 1)
        if name.strip() in {"OPENAI_API_KEY", "DEEPSEEK_API_KEY", "AGENTSWE_PROVIDER_KEY"}:
            return value.strip().strip('"').strip("'")
    raise RuntimeError("evaluator credential unavailable")


def _usage(payload: dict[str, Any]) -> tuple[int, int]:
    usage = payload.get("usage") or {}
    return int(usage.get("prompt_tokens", usage.get("input_tokens", 0)) or 0), int(usage.get("completion_tokens", usage.get("output_tokens", 0)) or 0)


def make_handler(state: BrokerState, upstream: str):
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *_: Any) -> None:
            return

        def _send(self, code: int, payload: Any) -> None:
            data = json.dumps(payload, ensure_ascii=False).encode()
            self.send_response(code); self.send_header("Content-Type", "application/json"); self.send_header("Content-Length", str(len(data))); self.end_headers(); self.wfile.write(data)

        def do_GET(self) -> None:  # noqa: N802
            if self.path == "/healthz": self._send(200, {"ok": True, "model": LOWER_MODEL, "reasoning_effort": LOWER_EFFORT})
            elif self.path == "/stats": self._send(200, state.stats)
            else: self._send(404, {"error": "not_found"})

        def do_POST(self) -> None:  # noqa: N802
            length = int(self.headers.get("Content-Length", "0"))
            try: payload = json.loads(self.rfile.read(length))
            except Exception: self._send(400, {"error": "invalid_json"}); return
            if self.path not in {"/v1/chat/completions", "/v1/responses"}:
                self._send(404, {"error": "unsupported_endpoint"}); return
            if not isinstance(payload, dict): self._send(400, {"error": "request_object_required"}); return
            requested_model = payload.get("model")
            if requested_model != LOWER_MODEL: state.record(forced_overrides=1)
            payload["model"] = LOWER_MODEL
            requested_reasoning = payload.get("reasoning")
            requested_effort = requested_reasoning.get("effort") if isinstance(requested_reasoning, dict) else None
            if requested_effort != LOWER_EFFORT: state.record(forced_overrides=1)
            payload["reasoning"] = {"effort": LOWER_EFFORT}
            payload.pop("reasoning_effort", None)
            state.record(calls=1)
            try:
                key = _credential(state.credential_file)
                if not key or key == "broker-only-placeholder": raise RuntimeError("evaluator credential unavailable")
                request = urllib.request.Request(upstream.rstrip("/") + self.path, data=json.dumps(payload).encode(), method="POST", headers={"Content-Type": "application/json", "Authorization": "Bearer " + key})
                with urllib.request.urlopen(request, timeout=120) as response:
                    body = json.loads(response.read())
                prompt, completion = _usage(body if isinstance(body, dict) else {})
                state.record(successful_calls=1, prompt_tokens=prompt, completion_tokens=completion, total_tokens=prompt + completion)
                self._send(200, body)
            except (urllib.error.URLError, TimeoutError, RuntimeError, ValueError, json.JSONDecodeError) as exc:
                state.record(failures=1, last_error=f"{type(exc).__name__}: {exc}")
                self._send(502, {"error": {"type": "broker_failure", "message": "upstream unavailable"}})
    return Handler


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--credential-file", type=Path, required=True)
    parser.add_argument("--stats-file", type=Path, required=True)
    parser.add_argument("--bind", default="127.0.0.1")
    parser.add_argument("--port", type=int, required=True)
    parser.add_argument("--upstream", default=os.environ.get("AGENTSWE_UPSTREAM_BASE_URL", "https://api.openai.com"))
    args = parser.parse_args()
    state = BrokerState(args.credential_file.resolve(), args.stats_file.resolve())
    server = ThreadingHTTPServer((args.bind, args.port), make_handler(state, args.upstream))
    write_json(args.stats_file, state.stats)
    try: server.serve_forever()
    except KeyboardInterrupt: return 0
    finally: server.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
