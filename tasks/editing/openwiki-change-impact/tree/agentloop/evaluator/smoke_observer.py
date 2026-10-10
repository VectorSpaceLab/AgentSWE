#!/usr/bin/env python3
"""Evaluator-owned, credential-free observer for non-formal product smokes.

The observer gives the sibling launcher an inspectable schema-v2 ``/stats``
surface while forwarding Candidate requests to an already-running evaluator
broker.  It never reads, receives, or stores a provider credential: both hops
use the public ``broker-only-placeholder`` capability token.

This is intentionally a smoke-only utility.  Formal hidden execution must use
``EvaluatorBrokerLifecycle`` and its independent evaluator-owned credential.
"""
from __future__ import annotations

import argparse
import json
import os
import threading
import urllib.error
import urllib.parse
import urllib.request
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

if __package__ in {None, ""}:
    import sys

    sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
try:
    from ..protocol import LOWER_EFFORT, LOWER_MODEL, PLACEHOLDER_KEY, write_json
except ImportError:
    from agentloop.protocol import LOWER_EFFORT, LOWER_MODEL, PLACEHOLDER_KEY, write_json


def _join_endpoint(upstream: str, request_path: str) -> str:
    parsed = urllib.parse.urlsplit(upstream)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        raise ValueError("upstream endpoint must be HTTP(S)")
    if parsed.path == request_path:
        path = parsed.path
    elif parsed.path.endswith("/responses"):
        path = parsed.path.removesuffix("/responses") + request_path.removeprefix("/v1")
    elif parsed.path.endswith("/chat/completions"):
        path = parsed.path.removesuffix("/chat/completions") + request_path.removeprefix("/v1")
    elif parsed.path.endswith("/v1") and request_path.startswith("/v1/"):
        path = parsed.path + request_path[3:]
    else:
        path = parsed.path.rstrip("/") + request_path
    return urllib.parse.urlunsplit((parsed.scheme, parsed.netloc, path, "", ""))


class State:
    COUNTERS = frozenset({
        "calls",
        "successful_calls",
        "failures",
        "broker_failures",
        "provider_failures",
        "credential_failures",
        "prompt_tokens",
        "completion_tokens",
        "total_tokens",
        "forced_overrides",
    })

    def __init__(self, stats_path: Path, upstream: str) -> None:
        self.stats_path = stats_path
        self.upstream = upstream
        self.lock = threading.Lock()
        self.value: dict[str, Any] = {
            "schema_version": 2,
            "broker_instance_id": f"openwiki-smoke-observer-{uuid.uuid4().hex}",
            "model": LOWER_MODEL,
            "reasoning_effort": LOWER_EFFORT,
            "calls": 0,
            "successful_calls": 0,
            "failures": 0,
            "broker_failures": 0,
            "provider_failures": 0,
            "credential_failures": 0,
            "prompt_tokens": 0,
            "completion_tokens": 0,
            "total_tokens": 0,
            "forced_overrides": 0,
            "credential_source_key": "evaluator_upstream_broker",
            "credential_value_recorded": False,
            "credential_mounted_to_candidate": False,
            "provider_credential_held": False,
            "upstream_kind": "evaluator_owned_broker",
            "last_upstream_status": None,
            "last_failure_classification": None,
        }
        write_json(self.stats_path, self.value)

    def add(self, **updates: object) -> None:
        with self.lock:
            for key, value in updates.items():
                if key in self.COUNTERS and isinstance(value, int):
                    self.value[key] = int(self.value.get(key, 0) or 0) + value
                else:
                    self.value[key] = value
            write_json(self.stats_path, self.value)


def handler(state: State):
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *_args: object) -> None:
            return

        def _send(self, status: int, body: bytes, content_type: str) -> None:
            try:
                self.send_response(status)
                self.send_header("Content-Type", content_type)
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
            except (BrokenPipeError, ConnectionResetError):
                # A bounded smoke may terminate the product while an upstream
                # request is still resolving.  Preserve counters without
                # turning evaluator shutdown into a second failure class.
                return

        def _json(self, status: int, value: object) -> None:
            self._send(status, json.dumps(value, ensure_ascii=False).encode(), "application/json")

        def do_GET(self) -> None:  # noqa: N802
            if self.path == "/healthz":
                self._json(200, {
                    "ok": True,
                    "broker_instance_id": state.value["broker_instance_id"],
                    "model": LOWER_MODEL,
                    "reasoning_effort": LOWER_EFFORT,
                })
            elif self.path == "/stats":
                self._json(200, state.value)
            else:
                self._json(404, {"error": "not_found"})

        def do_POST(self) -> None:  # noqa: N802
            if self.path not in {"/v1/responses", "/v1/chat/completions"}:
                self._json(404, {"error": "unsupported_endpoint"})
                return
            if self.headers.get("Authorization") != f"Bearer {PLACEHOLDER_KEY}":
                state.add(
                    failures=1,
                    broker_failures=1,
                    last_failure_classification="broker_failure",
                )
                self._json(401, {"error": {"type": "broker_failure", "message": "placeholder required"}})
                return
            try:
                payload = json.loads(
                    self.rfile.read(int(self.headers.get("Content-Length", "0")))
                )
                if not isinstance(payload, dict):
                    raise ValueError("request body must be an object")
            except (ValueError, json.JSONDecodeError):
                state.add(
                    failures=1,
                    broker_failures=1,
                    last_failure_classification="broker_failure",
                )
                self._json(400, {"error": {"type": "broker_failure", "message": "invalid JSON"}})
                return
            if payload.get("model") != LOWER_MODEL or payload.get("reasoning_effort") != LOWER_EFFORT:
                state.add(forced_overrides=1)
            payload["model"] = LOWER_MODEL
            payload["reasoning_effort"] = LOWER_EFFORT
            state.add(calls=1)
            request = urllib.request.Request(
                _join_endpoint(state.upstream, self.path),
                data=json.dumps(payload).encode(),
                method="POST",
                headers={
                    "Accept": self.headers.get("Accept", "application/json"),
                    "Content-Type": "application/json",
                    "Authorization": f"Bearer {PLACEHOLDER_KEY}",
                    "User-Agent": "agentswe-openwiki-smoke-observer/1",
                },
            )
            try:
                with urllib.request.urlopen(request, timeout=180) as response:
                    body = response.read()
                    status = response.status
                    content_type = response.headers.get("Content-Type", "application/json")
                prompt = completion = 0
                try:
                    decoded = json.loads(body)
                    usage = decoded.get("usage", {}) if isinstance(decoded, dict) else {}
                    if isinstance(usage, dict):
                        prompt = int(usage.get("input_tokens", usage.get("prompt_tokens", 0)) or 0)
                        completion = int(usage.get("output_tokens", usage.get("completion_tokens", 0)) or 0)
                except (UnicodeError, json.JSONDecodeError, ValueError, TypeError):
                    pass
                state.add(
                    successful_calls=1,
                    prompt_tokens=prompt,
                    completion_tokens=completion,
                    total_tokens=prompt + completion,
                    last_upstream_status=status,
                    last_failure_classification=None,
                )
                self._send(status, body, content_type)
            except urllib.error.HTTPError as exc:
                body = exc.read()
                classification = "provider_failure"
                try:
                    decoded = json.loads(body)
                    error = decoded.get("error", {}) if isinstance(decoded, dict) else {}
                    if isinstance(error, dict) and error.get("type") == "broker_failure":
                        classification = "broker_failure"
                except (UnicodeError, json.JSONDecodeError):
                    pass
                state.add(
                    failures=1,
                    broker_failures=1 if classification == "broker_failure" else 0,
                    provider_failures=1 if classification == "provider_failure" else 0,
                    last_upstream_status=exc.code,
                    last_failure_classification=classification,
                )
                self._send(exc.code, body, exc.headers.get("Content-Type", "application/json"))
            except (urllib.error.URLError, TimeoutError, OSError):
                state.add(
                    failures=1,
                    provider_failures=1,
                    last_failure_classification="provider_failure",
                )
                self._json(502, {"error": {"type": "provider_failure", "message": "upstream unavailable"}})

    return Handler


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--upstream", required=True)
    parser.add_argument("--stats-file", type=Path, required=True)
    parser.add_argument("--bind", default="127.0.0.1")
    parser.add_argument("--port", type=int, required=True)
    args = parser.parse_args()
    state = State(args.stats_file.resolve(), args.upstream)
    server = ThreadingHTTPServer((args.bind, args.port), handler(state))
    server.serve_forever()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
