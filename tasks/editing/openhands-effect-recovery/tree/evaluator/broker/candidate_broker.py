#!/usr/bin/env python3
"""Evaluator-owned Responses broker for OpenHands lifecycle roles.

The candidate receives only a placeholder token.  This process owns the real
credential, overwrites model/reasoning parameters, and writes a small stats
record suitable for audit.  It deliberately does not implement an agent or
case oracle.
"""
from __future__ import annotations

import argparse
import hashlib
import re
import sys
import json
import time
import math
import os
import threading
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

sys.path.append("@@AGENTSWE_EDITING_CONTROL@@")
from responses_stream import direct_opener, read_response, strict_json, RedactedCapture


class CaseDeadlineExceeded(TimeoutError):
    """The evaluator's own case deadline aborted this request; not a provider fault."""

MODEL = "deepseek-flash"
LOWER_EFFORT = "high"
BUILDER_EFFORT = "max"
SCHEMA = "agentswe-broker-stats/v1"



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

def load_key(path: Path) -> str:
    values: dict[str, str] = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        key, sep, value = line.partition("=")
        if sep and key.strip() in {"OPENAI_API_KEY", "DEEPSEEK_API_KEY"}:
            values[key.strip()] = value.strip().strip('"').strip("'")
    for key in ("DEEPSEEK_API_KEY", "OPENAI_API_KEY"):
        if values.get(key):
            return values[key]
    raise RuntimeError("credential file has no supported provider key")


class Stats:
    def __init__(self, path: Path | None = None, *, effort: str = LOWER_EFFORT, role: str = "lower") -> None:
        self.lock = threading.Lock()
        self.path = path
        self.effort = effort
        self.role = role
        self.value: dict[str, Any] = {
            "schema_version": SCHEMA,
            "role": role,
            "protocol": {"model": MODEL, "reasoning_effort": effort, "transport": "responses"},
            "runtime": {"calls": 0, "failures": 0, "successful_calls": 0,
                         "input_tokens": 0, "output_tokens": 0, "total_tokens": 0},
            "requests": [],
        }
        if self.path:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            if self.path.exists():
                previous = json.loads(self.path.read_text())
                if previous.get("protocol") != self.value["protocol"] or previous.get("role") != role:
                    raise RuntimeError("existing broker ledger protocol mismatch")
                self.value = previous
            else:
                self._save()

    def _refresh(self):
        rows=self.value.get('requests',[])
        pending=sum(row.get('state')=='submitted_or_unknown' for row in self.value.get('logical_requests',{}).values())
        unknown=pending+sum(row.get('usage_state')!='known' for row in rows)
        usage={key:sum(row.get(key) or 0 for row in rows) for key in ('input_tokens','output_tokens','total_tokens')}
        self.value['known_usage_subtotal']=usage
        self.value['runtime']={'calls':len(rows)+pending,'successful_calls':sum(bool(row.get('ok')) for row in rows),
            'failures':sum(not row.get('ok') for row in rows),'in_flight_calls':pending,'unknown_usage_calls':unknown,
            'case_deadline_calls':sum(row.get('error')=='CaseDeadlineExceeded' for row in rows),
            **{key:None if unknown else value for key,value in usage.items()}}

    def _save(self):
        self._refresh()
        if self.path:
            temporary=self.path.with_suffix(self.path.suffix+'.tmp')
            with temporary.open('w') as handle:
                json.dump(self.value,handle,indent=2);handle.write('\n');handle.flush();os.fsync(handle.fileno())
            os.replace(temporary,self.path)
            descriptor=os.open(str(self.path.parent),os.O_DIRECTORY)
            try:os.fsync(descriptor)
            finally:os.close(descriptor)

    def reserve(self, request_id):
        with self.lock:
            intents = self.value.setdefault("logical_requests", {})
            existing = intents.get(request_id)
            if existing is not None: return json.loads(json.dumps(existing))
            intents[request_id] = {"state": "submitted_or_unknown", "request_sha256": request_id}
            self._save()
            return None

    def record(self, *, ok: bool, usage: dict[str, Any] | None, error: str | None,
               request_id: str | None = None, response: dict | None = None) -> None:
        with self.lock:
            if request_id and self.value.get('logical_requests',{}).get(request_id,{}).get('result') is not None:
                return  # downstream delivery failure cannot replace a completed provider result
            known = isinstance(usage,dict) and all(type(usage.get(key)) is int and usage[key]>=0 for key in ('input_tokens','output_tokens','total_tokens'))
            tokens={key:usage[key] if known else None for key in ('input_tokens','output_tokens','total_tokens')}
            row = {"ok": ok, "model": MODEL, "reasoning_effort": self.effort, **tokens,
                   "usage_state": "known" if known else "unknown", "error": error, "request_sha256": request_id,
                   "transport_attempts": 1, "upstream_completion": "completed" if ok else "unknown_or_failed"}
            self.value["requests"].append(row)
            if request_id:
                intent = self.value.setdefault("logical_requests", {}).setdefault(request_id, {})
                intent.update(state="completed" if ok else "unknown_or_failed", result=row)
                if response is not None:
                    # Response bodies contain no request credential and stay evaluator-private.
                    if self.path:
                        raw_dir = self.path.parent / (self.path.stem + "-responses")
                        raw_dir.mkdir(exist_ok=True)
                        raw_path = raw_dir / (request_id + ".json")
                        with raw_path.open("x") as handle: json.dump(response, handle);handle.flush();os.fsync(handle.fileno())
                        intent.update(response_path=str(raw_path), response_sha256=hashlib.sha256(raw_path.read_bytes()).hexdigest())
                    else: intent["response"] = response
            self._save()

    def completed_response(self, intent):
        if intent.get("state") != "completed": return None
        if "response" in intent: return intent["response"]
        path = Path(intent["response_path"])
        if hashlib.sha256(path.read_bytes()).hexdigest() != intent["response_sha256"]:
            raise RuntimeError("completed broker response hash mismatch")
        return json.loads(path.read_text())

    def snapshot(self) -> dict[str, Any]:
        with self.lock:
            self._refresh()
            result = json.loads(json.dumps(self.value))
        result["requests"] = result["requests"][-100:]
        return result


def usage_from_response(value: Any) -> dict[str, Any]:
    return value.get("usage", {}) if isinstance(value, dict) else {}


def normalize_responses_input(value: Any) -> list[dict[str, Any]]:
    """Return the provider-compatible structured Responses input form.

    The local OpenHands Agent Server boundary naturally emits one text task.
    The evaluator-owned provider accepts the canonical message/input_text
    representation used by the other working lower-agent brokers.  Keep an
    already structured non-empty input unchanged so this broker remains a
    transport lock rather than a prompt or action selector.
    """
    if isinstance(value, str) and value.strip():
        return [{
            "type": "message",
            "role": "user",
            "content": [{"type": "input_text", "text": value}],
        }]
    if isinstance(value, list) and value:
        return value
    raise ValueError("Responses input must be non-empty text or a structured list")


def serve(*, bind: str, port: int, upstream: str, credential_file: Path, stats: Stats) -> None:
    key = load_key(credential_file)
    upstream = upstream.rstrip("/")

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, _format: str, *_args: object) -> None:
            return

        def send_json(self, code: int, value: object) -> None:
            body = json.dumps(value, ensure_ascii=False).encode()
            self.send_response(code); self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body))); self.end_headers(); self.wfile.write(body)

        def do_GET(self) -> None:  # noqa: N802
            if self.path == "/healthz":
                self.send_json(200, {"ok": True, "schema_version": SCHEMA})
            elif self.path == "/stats":
                self.send_json(200, stats.snapshot())
            else:
                self.send_json(404, {"error": "not_found"})

        def do_POST(self) -> None:  # noqa: N802
            if self.path != "/v1/responses":
                self.send_json(404, {"error": "not_found"}); return
            request_id = None
            submitted = False
            deadline = None
            try:
                size = int(self.headers.get("Content-Length", "0"))
                deadline_raw=self.headers.get('X-AgentSWE-Case-Deadline')
                deadline=float(deadline_raw) if deadline_raw is not None else time.monotonic()+180
                if not math.isfinite(deadline) or deadline<=time.monotonic():raise ValueError('expired case deadline')
                deadline=min(deadline,time.monotonic()+600)
                payload = strict_json(self.rfile.read(size))
                if not isinstance(payload, dict): raise ValueError("request must be an object")
                payload["input"] = normalize_responses_input(payload.get("input"))
                payload["model"] = MODEL
                payload["reasoning"] = {"effort": stats.effort}
                payload["stream"] = True
                # The local Agent Server uses metadata only as an evaluator
                # envelope. Case/nonce/phase are already present in the exact
                # task text sent as input. The selected upstream rejects this
                # non-provider envelope with HTTP 400, so do not forward it.
                payload.pop("metadata", None)
                payload.pop("reasoning_effort", None)
                body = json.dumps(payload, sort_keys=True).encode()
                # Evaluator-owned per-evaluation scope. Two Candidates each run the
                # same case from the same pristine repository, so their first request
                # is byte-identical; without a scope they share one logical-request
                # identity and a single transient upstream failure leaves that identity
                # permanently unknown, so every later Candidate is refused for the rest
                # of the run. The candidate product cannot set this header.
                scope = self.headers.get('X-AgentSWE-Evaluation')
                if scope is not None and not re.fullmatch(r'[0-9a-f]{8,64}', scope):
                    self.send_json(400, {"error": {"type": "invalid_evaluation_scope"}}); return
                request_id = hashlib.sha256(
                    body if scope is None else scope.encode() + b'\0' + body).hexdigest()
                prior = stats.reserve(request_id)
                if prior is not None:
                    completed = stats.completed_response(prior)
                    if completed is not None: self.send_json(200, completed)
                    else: self.send_json(409, {"error": "prior_request_result_unknown", "request_sha256": request_id, "retry_allowed": False})
                    return
                request = urllib.request.Request(
                    upstream + "/v1/responses", data=body, method="POST",
                    headers={
                        "Content-Type": "application/json", "Accept": "application/json",
                        "Authorization": f"Bearer {key}", "User-Agent": "agentswe-openhands-evaluator-broker/1",
                    },
                )
                submitted = True
                capture = None
                capture_handle = None
                if stats.path:
                    capture_dir = stats.path.parent / (stats.path.stem + "-raw")
                    capture_dir.mkdir(exist_ok=True)
                    capture_handle = (capture_dir / (request_id + ".bin")).open("xb")
                    capture = RedactedCapture(capture_handle, key)
                try:
                    with direct_opener().open(request, timeout=max(.001,deadline-time.monotonic())) as response:
                        def read_with_deadline(size):
                            remaining=deadline-time.monotonic()
                            if remaining<=0:raise CaseDeadlineExceeded('absolute case deadline')
                            response.fp.raw._sock.settimeout(remaining)
                            return response.read1(size)
                        raw, streamed = read_response(read_with_deadline, content_type=response.headers.get("Content-Type", ""), is_success=True, capture=capture.write if capture else None)
                        result = streamed if streamed is not None else strict_json(raw)
                finally:
                    if capture: capture.finish()
                    if capture_handle: capture_handle.close()
                if not isinstance(result, dict) or not _completed_or_budget_limited(result) or not isinstance(result.get("id"), str) or not result["id"]:
                    stats.record(ok=False, usage=usage_from_response(result), error="response_not_typed_completed", request_id=request_id, response=result if isinstance(result, dict) else None)
                    self.send_json(502, {"error": "response_not_typed_completed", "retry_allowed": False}); return
                stats.record(ok=True, usage=usage_from_response(result), error=None, request_id=request_id, response=result)
                self.send_json(200, result)
            except urllib.error.HTTPError as exc:  # provider failures are infra evidence, not candidate zero
                # Preserve only the status class, never the provider response body.
                stats.record(ok=False, usage=None, error=f"HTTPError:{exc.code}", request_id=request_id)
                self.send_json(502, {"error": "broker_upstream_failure", "type": "HTTPError", "status": exc.code})
            except Exception as exc:  # provider failures are infra evidence, not candidate zero
                name = type(exc).__name__
                if isinstance(exc, TimeoutError) and deadline is not None and time.monotonic() >= deadline - 1.0:
                    name = "CaseDeadlineExceeded"  # the case's own deadline, not an upstream stall
                if submitted: stats.record(ok=False, usage=None, error=name, request_id=request_id)
                self.send_json(502 if submitted else 400, {"error": "broker_upstream_failure", "type": name})

    ThreadingHTTPServer((bind, port), Handler).serve_forever()


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--bind", default="127.0.0.1")
    parser.add_argument("--port", type=int, required=True)
    parser.add_argument("--upstream", default="https://api.deepseek.com")
    parser.add_argument("--credential-file", type=Path, required=True)
    parser.add_argument("--stats-file", type=Path)
    parser.add_argument("--role", choices=("lower", "builder"), default="lower")
    parser.add_argument("--reasoning-effort", choices=(LOWER_EFFORT, BUILDER_EFFORT), default=LOWER_EFFORT)
    args = parser.parse_args()
    expected = LOWER_EFFORT if args.role == "lower" else BUILDER_EFFORT
    if args.reasoning_effort != expected:
        parser.error(f"{args.role} broker requires reasoning effort {expected}")
    serve(
        bind=args.bind, port=args.port, upstream=args.upstream,
        credential_file=args.credential_file,
        stats=Stats(args.stats_file, effort=args.reasoning_effort, role=args.role),
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
