#!/usr/bin/env python3
"""Evaluator-owned Responses broker; Candidate receives only a placeholder token.

The broker is intentionally standalone and standard-library-only.  It forces the
lower-agent protocol on every request, keeps provider credentials outside the
Candidate, and records calls/failures/tokens without recording prompts or secrets.
"""
from __future__ import annotations

import argparse
import http.server
import json
import os
import time
import urllib.error
import urllib.request
from pathlib import Path
from threading import Lock
from typing import Any

try:
    from agentloop.evaluator.responses_stream import direct_opener
except ModuleNotFoundError:
    from responses_stream import direct_opener

MODEL = "deepseek-flash"
EFFORT = "high"
PLACEHOLDER = "broker-only-placeholder"



def _completed_or_budget_limited(value):
    """Accept a completed response, or one finished inside the Candidate's own
    output budget.

    The latter arrives as HTTP 200 with its own id, complete usage and no error
    -- exactly what a real API client receives when it sets max_output_tokens --
    so treating it as a provider fault invents a failure production cannot
    produce. Nothing else about a response is admitted here.
    """
    if not isinstance(value, dict):
        return False
    if value.get('status') == 'completed':
        return True
    details = value.get('incomplete_details')
    return (value.get('status') == 'incomplete' and isinstance(details, dict)
            and details.get('reason') == 'max_output_tokens'
            and value.get('error') is None)

class IncompleteProviderResponse(RuntimeError):
    """A parseable response is not proof that the provider completed it."""


class BrokerState:
    def __init__(self) -> None:
        self.lock = Lock()
        self.calls = 0
        self.failures = 0
        self.broker_failures = 0
        self.provider_failures = 0
        self.credential_failures = 0
        self.protocol_failures = 0
        self.input_tokens = 0
        self.output_tokens = 0
        self.total_tokens = 0
        self.upstream_transport_attempts = 0
        self.completed_responses = 0
        self.unknown_usage_requests = 0
        self.client_delivery_failures = 0
        self.request_ledger: list[dict[str, Any]] = []
        self.handlers_started = 0
        self.handlers_finished = 0
        self.upstream_status_counts: dict[str, int] = {}
        self.last_protocol = {"model": MODEL, "reasoning_effort": EFFORT}

    def record(
        self,
        *,
        failed: bool,
        failure_classification: str | None = None,
        status_code: int | None = None,
        usage: dict[str, Any] | None = None,
        upstream_attempted: bool = False,
        response_id: str | None = None,
    ) -> None:
        with self.lock:
            self.calls += 1
            self.failures += int(failed)
            if failed:
                if failure_classification == "provider_failure":
                    self.provider_failures += 1
                elif failure_classification == "credential_failure":
                    self.credential_failures += 1
                elif failure_classification == "protocol_failure":
                    self.protocol_failures += 1
                else:
                    self.broker_failures += 1
            if status_code is not None:
                key = str(int(status_code))
                self.upstream_status_counts[key] = self.upstream_status_counts.get(key, 0) + 1
            usage_known = isinstance(usage, dict) and all(type(usage.get(key)) is int
                and usage[key] >= 0 for key in ('input_tokens', 'output_tokens', 'total_tokens'))
            self.upstream_transport_attempts += int(upstream_attempted)
            self.completed_responses += int(upstream_attempted and not failed)
            self.unknown_usage_requests += int(upstream_attempted and not usage_known)
            self.request_ledger.append({'logical_request': self.calls,
                'transport_attempts': int(upstream_attempted), 'completed': upstream_attempted and not failed,
                'response_id': response_id, 'status_code': status_code,
                'submission_state': ('completed' if not failed else 'unknown') if upstream_attempted else 'not_sent',
                'usage_known': usage_known if upstream_attempted else True,
                'usage': usage if usage_known else None, 'automatic_retry': False})
            usage = usage if usage_known else {}
            self.input_tokens += int(usage.get("input_tokens", 0) or 0)
            self.output_tokens += int(usage.get("output_tokens", 0) or 0)
            self.total_tokens += int(usage.get("total_tokens", 0) or 0)

    def stats(self) -> dict[str, Any]:
        with self.lock:
            return {"schema_version": "agentswe-broker-stats/v1", "protocol": {"model": MODEL, "reasoning_effort": EFFORT},
                    "runtime": {"calls": self.calls, "failures": self.failures, "successful_calls": self.calls - self.failures,
                                 "broker_failures": self.broker_failures, "provider_failures": self.provider_failures,
                                 "credential_failures": self.credential_failures, "protocol_failures": self.protocol_failures,
                                 "input_tokens": self.input_tokens, "output_tokens": self.output_tokens,
                                 "total_tokens": self.total_tokens,
                                 "known_tokens": self.total_tokens,
                                 "unknown_usage_requests": self.unknown_usage_requests,
                                 "usage_complete": self.unknown_usage_requests == 0,
                                 "upstream_transport_attempts": self.upstream_transport_attempts,
                                 "completed_responses": self.completed_responses},
                    "delivery": {"client_delivery_failures": self.client_delivery_failures},
                    "lifecycle": {"schema_version": "agentswe-broker-lifecycle/v1", "state": "open",
                                  "server_close_completed": False, "handlers_started": self.handlers_started,
                                  "handlers_finished": self.handlers_finished,
                                  "in_flight": self.handlers_started - self.handlers_finished},
                    "request_ledger": list(self.request_ledger),
                    "upstream": {"status_counts": dict(sorted(self.upstream_status_counts.items()))},
                    "credential": {"candidate_visible": PLACEHOLDER, "provider_secret_logged": False,
                                   "credential_value_recorded": False}}


def provider_key(path: Path) -> str:
    values: dict[str, str] = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        key, separator, value = line.partition("=")
        if separator and key.strip() in {"OPENAI_API_KEY", "DEEPSEEK_API_KEY", "AGENTSWE_PROVIDER_KEY"}:
            values[key.strip()] = value.strip().strip('"\'')
    for key in ("AGENTSWE_PROVIDER_KEY", "DEEPSEEK_API_KEY", "OPENAI_API_KEY"):
        if values.get(key):
            return values[key]
    raise RuntimeError("credential file has no supported provider key")


class Handler(http.server.BaseHTTPRequestHandler):
    server_version = "agentswe-claude-broker/1"

    def log_message(self, _format: str, *_args: object) -> None:
        return

    @property
    def state(self) -> BrokerState:
        return self.server.state  # type: ignore[attr-defined]

    def send_json(self, code: int, value: object) -> None:
        body = json.dumps(value, ensure_ascii=False).encode()
        self.send_response(code); self.send_header("Content-Type", "application/json"); self.send_header("Content-Length", str(len(body))); self.end_headers(); self.wfile.write(body)

    def do_GET(self) -> None:  # noqa: N802
        if self.path == "/healthz":
            self.send_json(200, {"ok": True, "model": MODEL, "reasoning_effort": EFFORT})
        elif self.path == "/stats":
            if self.headers.get("Authorization") != "Bearer stats-only-placeholder": self.send_json(401, {"error": "stats auth"})
            else: self.send_json(200, self.state.stats())
        else: self.send_json(404, {"error": "not found"})

    def do_POST(self) -> None:  # noqa: N802
        if self.path != "/v1/responses": self.send_json(404, {"error": "not found"}); return
        with self.state.lock: self.state.handlers_started += 1
        try: self._handle_responses_post()
        finally:
            with self.state.lock: self.state.handlers_finished += 1

    def _handle_responses_post(self) -> None:
        if self.headers.get("Authorization") != f"Bearer {PLACEHOLDER}":
            self.state.record(failed=True, failure_classification="protocol_failure", status_code=401)
            self.send_json(401, {"error": "Candidate must use placeholder credential"}); return
        upstream_attempted = False
        recorded = False
        decoded = None
        try:
            length = int(self.headers.get("Content-Length", "0"))
            if length <= 0 or length > 2_000_000:
                raise ValueError("invalid request length")
            request = json.loads(self.rfile.read(length))
            if not isinstance(request, dict): raise ValueError("request is not an object")
            request["model"] = MODEL
            reasoning = request.get("reasoning") if isinstance(request.get("reasoning"), dict) else {}
            reasoning["effort"] = EFFORT; request["reasoning"] = reasoning
            api_key = self.server.provider_key  # type: ignore[attr-defined]
            headers = {"Authorization": f"Bearer {api_key}", "Content-Type": "application/json",
                       "Accept": "application/json", "User-Agent": "agentswe-claude-evaluator-broker/1"}
            upstream = urllib.request.Request(self.server.provider_url, data=json.dumps(request).encode(), headers=headers, method="POST")  # type: ignore[attr-defined]
            def mark_http_send():
                nonlocal upstream_attempted
                upstream_attempted = True
            with direct_opener(on_request_start=mark_http_send).open(upstream, timeout=120) as response:
                status_code = int(response.status)
                payload = response.read(); decoded = json.loads(payload)
            if not isinstance(decoded, dict) or decoded.get('object') != 'response' or not _completed_or_budget_limited(decoded):
                raise IncompleteProviderResponse('provider did not return a completed Responses object')
            usage = decoded.get("usage") if isinstance(decoded, dict) else None
            self.state.record(failed=False, status_code=status_code, usage=usage if isinstance(usage, dict) else None,
                              upstream_attempted=True, response_id=decoded.get('id'))
            recorded = True
            self.send_response(200); self.send_header("Content-Type", "application/json"); self.send_header("Content-Length", str(len(payload))); self.end_headers(); self.wfile.write(payload)
        except urllib.error.HTTPError as exc:
            self.state.record(failed=True, failure_classification="provider_failure", status_code=exc.code,
                              upstream_attempted=upstream_attempted)
            self.send_json(502, {"error": "provider_failure", "type": type(exc).__name__, "status_code": exc.code})
        except (urllib.error.URLError, TimeoutError, ConnectionError, OSError) as exc:
            if recorded:
                with self.state.lock:
                    self.state.client_delivery_failures += 1
                return
            self.state.record(failed=True, failure_classification="provider_failure", upstream_attempted=upstream_attempted)
            self.send_json(502, {"error": "provider_failure", "type": type(exc).__name__})
        except IncompleteProviderResponse as exc:
            self.state.record(failed=True, failure_classification='provider_failure', upstream_attempted=True,
                              response_id=decoded.get('id') if isinstance(decoded, dict) else None,
                              usage=decoded.get('usage') if isinstance(decoded, dict) else None)
            self.send_json(502, {'error': 'provider_incomplete_response', 'type': type(exc).__name__})
        except (ValueError, json.JSONDecodeError) as exc:
            self.state.record(failed=True, failure_classification="provider_failure" if upstream_attempted else "protocol_failure",
                              upstream_attempted=upstream_attempted)
            self.send_json(502 if upstream_attempted else 400, {"error": "provider_invalid_response" if upstream_attempted else "broker_protocol_failure", "type": type(exc).__name__})


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--credential-file", type=Path, required=True)
    parser.add_argument("--provider-url", default=os.environ.get("AGENTSWE_PROVIDER_URL", "https://api.openai.com/v1/responses"))
    parser.add_argument("--bind", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=18080)
    args = parser.parse_args()
    server = http.server.ThreadingHTTPServer((args.bind, args.port), Handler)
    server.state = BrokerState()  # type: ignore[attr-defined]
    server.provider_url = args.provider_url  # type: ignore[attr-defined]
    server.provider_key = provider_key(args.credential_file)  # type: ignore[attr-defined]
    print(json.dumps({"ready": True, "model": MODEL, "reasoning_effort": EFFORT, "bind": args.bind, "port": args.port}), flush=True)
    try: server.serve_forever()
    except KeyboardInterrupt: return 0
    finally: server.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
