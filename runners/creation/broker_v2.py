#!/usr/bin/env python3
"""Bounded Responses broker with content-free per-case failure attribution.

Uses the established Chat conversion/usage parser. Only this process reads the
real key. No request bodies, response bodies, auth headers or key values enter
the ledger. 400/422 are not rewritten to provider 502. Successful internal
retries produce ONE successful request record, not several failed cases.
"""
from __future__ import annotations

import argparse
import http.server
import json
import os
import re
import socket
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from pathlib import Path

from responses_broker import (
    UPSTREAM, MODEL, MAX_BODY, RUNTIME_TOKENS, JUDGE_TOKENS, STATS_TOKEN,
    dotenv, response_tokens, chat_messages_to_responses_input,
    responses_to_chat_completion,
)

VERSION = "AgentSWECreateBroker/2.0"
RUNTIME_EFFORT = os.environ.get("AGENTSWE_RUNTIME_EFFORT", "high")
JUDGE_EFFORT = os.environ.get("AGENTSWE_BROKER_JUDGE_EFFORT", "max")
# Providers may report a dated or aliased model id; any of these substrings is accepted.
MODEL_ALIASES = tuple(a for a in os.environ.get("AGENTSWE_RUNTIME_MODEL_ALIASES", "deepseek").split(",") if a)


def response_metadata(payload: bytes, content_type: str) -> dict:
    """Reject truncated/error envelopes even when HTTP status was 200."""
    if 'text/event-stream' in content_type or payload.lstrip().startswith((b'event:', b'data:')):
        terminal = None
        for line in payload.decode('utf-8').splitlines():
            if not line.startswith('data:'): continue
            data = line[5:].strip()
            if not data or data == '[DONE]': continue
            item = json.loads(data)
            if item.get('type') in {'error','response.failed'}:
                raise ValueError('upstream stream failed')
            if item.get('type') in {'response.completed','response.incomplete'}:
                terminal = item.get('response')
        if not isinstance(terminal,dict): raise ValueError('upstream stream is truncated')
        value = terminal
    else:
        value = json.loads(payload)
    if not isinstance(value,dict) or value.get('error') or value.get('status') == 'failed':
        raise ValueError('upstream response envelope failed')
    if not any(key in value for key in ('output','output_text')):
        raise ValueError('upstream response has no output envelope')
    model = value.get('model')
    if isinstance(model,str) and model != MODEL and not model.startswith(MODEL+'-') and not any(alias in model for alias in MODEL_ALIASES):
        raise ValueError('upstream reported a different model')
    return {'reported_model':model if isinstance(model,str) and len(model)<100 else None,
            'response_status':value.get('status')}


def failure_class(status: int, *, local: bool = False) -> str:
    if 200 <= status < 300:
        return "success"
    if local or status in {400, 405, 413, 415, 422}:
        return "candidate_request_error"
    # Auth, credit, endpoint and capacity failures belong to the service, not
    # to a Candidate carrying the broker's fixed placeholder credential.
    return "provider_failure"


class BrokerServer(http.server.ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True

    def __init__(self, address, *, api_key: str, upstream: str = UPSTREAM,
                 ledger_file: Path | None = None, max_attempts: int = 3,
                 request_timeout: float = 240, upstream_timeout: float = 90,
                 retry_delay: float = 1, max_runtime_calls: int = 0,
                 max_runtime_tokens: int = 0):
        super().__init__(address, Handler)
        self.api_key, self.upstream = api_key, upstream
        self.ledger_file = ledger_file
        self.max_attempts = max(1, max_attempts)
        self.request_timeout = max(0.1, request_timeout)
        self.upstream_timeout = max(0.1, upstream_timeout)
        self.retry_delay = max(0, retry_delay)
        self.max_runtime_calls = max_runtime_calls
        self.max_runtime_tokens = max_runtime_tokens
        self.lock = threading.Lock()
        self.events = []
        self.stats = {role: {"calls": 0, "tokens": 0, "failures": 0,
                            "upstream_failures": 0, "delivery_failures": 0,
                            "candidate_failures": 0, "transport_failures": 0,
                            "budget_exceeded": False} for role in ("runtime", "judge")}
        if ledger_file:
            ledger_file.parent.mkdir(parents=True, exist_ok=True)
            # An existing ledger indicates an accidental logical run reuse.
            with ledger_file.open("x"):
                pass
            ledger_file.chmod(0o600)

    def reserve(self, role):
        with self.lock:
            stats = self.stats[role]
            if role == "runtime" and (
                self.max_runtime_calls and stats["calls"] >= self.max_runtime_calls or
                self.max_runtime_tokens and stats["tokens"] >= self.max_runtime_tokens
            ):
                stats["budget_exceeded"] = True
                return False
            stats["calls"] += 1
            return True

    def record(self, event):
        with self.lock:
            stats = self.stats[event["role"]]
            stats["tokens"] += event.get("tokens", 0)
            classification = event["classification"]
            if classification != "success":
                stats["failures"] += 1
            if event.get("upstream_classification") == "provider_failure":
                stats["upstream_failures"] += 1
            if classification == "delivery_failure":
                stats["delivery_failures"] += 1
            if classification == "candidate_request_error":
                stats["candidate_failures"] += 1
            if classification == "transport_failure":
                stats["transport_failures"] += 1
            self.events.append(event)
            if self.ledger_file:
                with self.ledger_file.open("a") as handle:
                    handle.write(json.dumps(event, sort_keys=True) + "\n")
                    handle.flush()

    def public_stats(self):
        with self.lock:
            return {**{r: dict(s) for r, s in self.stats.items()},
                    "max_runtime_calls": self.max_runtime_calls,
                    "max_runtime_tokens": self.max_runtime_tokens,
                    "version": VERSION, "recorded_requests": len(self.events)}


class Handler(http.server.BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    server_version = VERSION

    def log_message(self, *args):
        pass

    def send_json(self, status, value):
        payload = json.dumps(value).encode()
        self.send_bytes(status, payload, "application/json")

    def send_bytes(self, status, payload, content_type):
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)
        self.wfile.flush()

    def do_GET(self):
        parsed = urllib.parse.urlsplit(self.path)
        if parsed.path == "/healthz":
            self.send_json(200, {"status": "ok", "version": VERSION})
            return
        if self.headers.get("Authorization") != f"Bearer {STATS_TOKEN}":
            self.send_json(401, {"error": "unauthorized"})
            return
        if parsed.path == "/stats":
            self.send_json(200, self.server.public_stats())
        elif parsed.path == "/events":
            query = urllib.parse.parse_qs(parsed.query)
            with self.server.lock:
                events = [e for e in self.server.events if all(
                    e.get(key) == values[0] for key, values in query.items()
                    if key in {"evaluation_id", "case_id", "role"}
                )]
            self.send_json(200, events)
        else:
            self.send_json(404, {"error": "not_found"})

    def do_POST(self):
        token = self.headers.get("Authorization", "").removeprefix("Bearer ").strip()
        role = "runtime" if token in RUNTIME_TOKENS else "judge" if token in JUDGE_TOKENS else None
        if role is None:
            self.send_json(401, {"error": "unauthorized"})
            return
        started = time.monotonic()
        request_id = uuid.uuid4().hex
        path = urllib.parse.urlsplit(self.path).path.rstrip("/")
        evaluation, case = None, None
        context = re.fullmatch(r"/context/([A-Za-z0-9_.%-]{1,240})/([A-Za-z0-9_.%-]{1,120})(/v1/(?:responses|chat/completions))", path)
        if context:
            evaluation, case = (urllib.parse.unquote(context.group(i)) for i in (1, 2))
            path = context.group(3)
        event = {"schema_version": "2", "request_id": request_id,
                 "role": role, "evaluation_id": evaluation, "case_id": case,
                 "requested_model": MODEL, "reasoning_effort": RUNTIME_EFFORT if role == "runtime" else JUDGE_EFFORT,
                 "attempt_statuses": [], "tokens": 0}
        if not self.server.reserve(role):
            self.send_json(429, {"error": {"type": "candidate_budget_exceeded"}})
            return

        def finish(status, payload, content_type="application/json", classification=None):
            event["outgoing_status"] = status
            event["classification"] = classification or failure_class(status)
            event["upstream_classification"] = event["classification"]
            event["runtime_seconds"] = round(time.monotonic() - started, 4)
            try:
                self.send_bytes(status, payload, content_type)
            except (BrokenPipeError, ConnectionResetError, OSError):
                event["classification"] = "delivery_failure"
            self.server.record(event)

        def fail(status, kind, classification):
            finish(status, json.dumps({"error": {"type": kind,
                   "upstream_http_status": event.get("upstream_status"),
                   "request_id": request_id, "attempts": len(event["attempt_statuses"])}}).encode(),
                   classification=classification)

        if path not in {"/v1/responses", "/v1/chat/completions"}:
            fail(404, "candidate_endpoint_invalid", "candidate_request_error")
            return
        try:
            length = int(self.headers.get("Content-Length", "0"))
            if not 0 < length <= MAX_BODY:
                raise ValueError("invalid_body_size")
            self.connection.settimeout(30)
            body = json.loads(self.rfile.read(length))
            if not isinstance(body, dict):
                raise ValueError("body_must_be_object")
            chat = path.endswith("chat/completions")
            if chat:
                body["input"] = chat_messages_to_responses_input(body.pop("messages"))
                body.pop("stream", None)
            if "input" not in body:
                raise ValueError("missing_input")
        except (ValueError, KeyError, TypeError, OSError):
            fail(400, "candidate_request_invalid", "candidate_request_error")
            self.close_connection = True
            return
        body["model"] = MODEL
        body["reasoning"] = {"effort": event["reasoning_effort"]}
        body.pop("max_tokens", None)
        request = urllib.request.Request(self.server.upstream,
            data=json.dumps(body).encode(), method="POST",
            headers={"Authorization": f"Bearer {self.server.api_key}",
                     "Content-Type": "application/json", "Accept": "application/json",
                     "User-Agent": VERSION})
        deadline = started + self.server.request_timeout
        done, cancelled = threading.Event(), threading.Event()
        result = {}

        def fetch():
            statuses = []
            try:
                for attempt in range(self.server.max_attempts):
                    remaining = deadline - time.monotonic()
                    if cancelled.is_set() or remaining <= 0:
                        break
                    try:
                        with urllib.request.urlopen(request, timeout=min(remaining, self.server.upstream_timeout)) as response:
                            chunks = []
                            while not cancelled.is_set() and time.monotonic() < deadline:
                                chunk = response.read1(65536)
                                if not chunk:
                                    break
                                chunks.append(chunk)
                            else:
                                raise TimeoutError()
                            statuses.append(response.status)
                            result.update(status=response.status, payload=b"".join(chunks),
                                          content_type=response.headers.get("Content-Type", "application/json"))
                            return
                    except urllib.error.HTTPError as exc:
                        statuses.append(exc.code)
                        result.update(status=exc.code, classification=failure_class(exc.code))
                        exc.close()
                        if exc.code not in {408, 425, 429} and exc.code < 500:
                            return
                    except (urllib.error.URLError, OSError, TimeoutError):
                        statuses.append("transport_error")
                        result.update(status=504, classification="transport_failure")
                    if attempt + 1 < self.server.max_attempts:
                        cancelled.wait(min(self.server.retry_delay * 2**attempt, max(0, deadline - time.monotonic())))
            except Exception:
                result.update(status=502, classification="broker_failure")
            finally:
                result["attempt_statuses"] = statuses
                done.set()

        threading.Thread(target=fetch, daemon=True).start()
        while not done.wait(min(5, max(0.01, deadline - time.monotonic()))):
            if time.monotonic() >= deadline:
                cancelled.set()
                event["attempt_statuses"] = list(result.get("attempt_statuses", []))
                fail(504, "broker_deadline_exceeded", "transport_failure")
                return
            try:
                self.send_response_only(100)
                self.end_headers()
                self.wfile.flush()
            except (BrokenPipeError, ConnectionResetError, OSError):
                cancelled.set()
                event.update(classification="delivery_failure", upstream_classification="unknown",
                             runtime_seconds=round(time.monotonic()-started, 4), outgoing_status=None)
                self.server.record(event)
                return
        event["attempt_statuses"] = result.get("attempt_statuses", [])
        event["upstream_status"] = result.get("status")
        if "payload" not in result:
            fail(result.get("status", 504), "upstream_error", result.get("classification", "transport_failure"))
            return
        payload = result["payload"]
        event["tokens"] = response_tokens(payload)
        try:
            event.update(response_metadata(payload, result["content_type"]))
        except (ValueError, UnicodeError, AttributeError):
            fail(502, "invalid_responses_payload", "provider_failure")
            return
        if chat:
            try:
                payload = responses_to_chat_completion(payload)
                result["content_type"] = "application/json"
            except (ValueError, UnicodeError):
                fail(502, "invalid_responses_payload", "provider_failure")
                return
        finish(result["status"], payload, result["content_type"])


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--credential-file", type=Path, required=True)
    p.add_argument("--bind", default="127.0.0.1")
    p.add_argument("--port", type=int, default=8080)
    p.add_argument("--upstream", default=UPSTREAM)
    p.add_argument("--ledger-file", type=Path, required=True)
    p.add_argument("--max-attempts", type=int, default=3)
    p.add_argument("--request-timeout", type=float, default=240)
    p.add_argument("--upstream-timeout", type=float, default=90)
    p.add_argument("--max-runtime-calls", type=int, default=0)
    p.add_argument("--max-runtime-tokens", type=int, default=0)
    args = vars(p.parse_args())
    creds = dotenv(args.pop("credential_file")); key = creds.get("AGENTSWE_RUNTIME_API_KEY")
    if not key:
        raise SystemExit("AGENTSWE_RUNTIME_API_KEY missing")
    address = (args.pop("bind"), args.pop("port"))
    with BrokerServer(address, api_key=key, **args) as server:
        server.serve_forever()


if __name__ == "__main__":
    main()
