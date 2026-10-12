#!/usr/bin/env python3
"""Fixed lower-only transport, shared by the host relay and netns bridge.

No provider credentials, retry loop, redirect handling, task solver, or oracle
is present here. The existing evaluator broker still owns model/effort policy.
Responses are forwarded incrementally, including SSE; this does not claim that
the upstream broker itself is streaming. This helper never exposes /stats.
"""
from __future__ import annotations

import argparse
import hashlib
import http.client
import json
import math
import os
from pathlib import Path
import signal
import socket
import socketserver
import subprocess
import re
import tempfile
import threading
import time
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

PLACEHOLDER = "broker-only-placeholder"
HEALTH_PATH = "/agentswe/transport-health"
MODEL_PATH = "/v1/responses"

# Pre-dispatch deadline guard (2026-09-20, re-sized 2026-09-21).  The evaluator's
# broker reaps a worker that is still in flight at the absolute case deadline and
# books it usage_unknown with transport_abort_reason='absolute_case_deadline'; the
# shared readiness normalizers refuse any such ledger, because 'casedeadline' is
# excluded from the D13 recovered-transport class.  Do not start a request that
# cannot finish: below this much remaining budget the relay refuses locally, so the
# broker never sees the request and the ledger keeps only fully settled rows.
#
# The reserve must cover exactly one upstream request settling end to end, not a
# policy round number.  Evidence, 0919 formal run: the hidden lower ledger's 226
# settled attempts have elapsed_seconds p50 2.59, p95 8.71, p99 12.46, max 14.38;
# the public lower ledger's 47 attempts max 11.10; the widest figure this tree has
# recorded is 19.6.  25.0 covers that worst case with 28 % headroom and still fits
# a settle plus the relay's own teardown.  The former 60.0 was a policy floor, not
# a measurement: it silently withdrew the last 35 s of every case's model budget,
# and test_001/003/005 were refused with 54.7 s, 55.3 s and 44.8 s of case budget
# still on the clock after 50, 45 and 39 successful turns.
LOWER_DISPATCH_RESERVE_SECONDS = 25.0
# The guard belongs to the trusted host relay, whose snapshot the evaluator
# collects (embedded-agent/dispatch_guard.json).  The in-namespace bridge sits in
# front of it and, with an equal reserve, refused first -- so every refusal in the
# 0919 run was invisible to the evaluator and the Candidate's dev feedback could
# only say "missing_core".  The bridge now refuses nothing on budget grounds; the
# host relay's 503 is forwarded back through it byte for byte.
BRIDGE_DISPATCH_RESERVE_SECONDS = 0.0
DISPATCH_GUARD_SCHEMA = "openclaw-lower-dispatch-guard/v1"


class UnixConnection(http.client.HTTPConnection):
    def __init__(self, path: str, timeout: float):
        super().__init__("localhost", timeout=timeout)
        self.socket_path = path

    def connect(self):
        self.sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.sock.settimeout(self.timeout)
        self.sock.connect(self.socket_path)


class RelayHandler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, *_):
        pass

    def remaining(self) -> float:
        value = self.server.deadline - time.monotonic()
        if value <= 0:
            raise TimeoutError("case deadline exhausted")
        return value

    def dispatch_guard(self):
        """Refuse an upstream model request the case budget cannot settle.

        Returns None (dispatch unchanged) while remaining >= reserve.  Otherwise
        records the decision on the server and returns the refusal body.  Called
        before forward(), so no upstream connection is opened and the evaluator's
        broker never creates a ledger event for this request.
        """
        reserve = float(getattr(self.server, "dispatch_reserve", LOWER_DISPATCH_RESERVE_SECONDS))
        remaining = self.server.deadline - time.monotonic()
        if remaining >= reserve:
            return None
        with self.server.events_lock:
            completed = sum(1 for item in self.server.events if item.get("complete"))
            attempted = len(self.server.events)
        decision = {"schema_version": DISPATCH_GUARD_SCHEMA, "refused": True,
                    "reason": "case_budget_reserve_reached",
                    "remaining_seconds": round(remaining, 3),
                    "reserve_seconds": reserve,
                    "case_deadline_monotonic": self.server.deadline,
                    "turns_completed": completed,
                    "requests_attempted": attempted,
                    "host_relay": bool(self.server.is_host),
                    "stopped_by": "evaluator_budget_guard"}
        with self.server.guard_lock:
            self.server.guard_refusals.append(decision)
        return {"error": "lower_dispatch_refused_by_case_budget_guard",
                "upstream_attempts": 0, "retry_allowed": False,
                "remaining_seconds": decision["remaining_seconds"],
                "reserve_seconds": reserve}

    def small_response(self, status: int, value: dict):
        body = json.dumps(value).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Connection", "close")
        self.end_headers()
        self.wfile.write(body)
        self.close_connection = True

    def do_GET(self):
        # Local transport health is not model health and does not consume API.
        if self.path != HEALTH_PATH:
            return self.small_response(404, {"error": "route_not_exposed"})
        if self.server.is_host:
            return self.small_response(200, {"transport": "fixed-lower-relay", "model_request": False})
        self.forward(None)

    def do_POST(self):
        if self.path != MODEL_PATH:
            return self.small_response(404, {"error": "route_not_exposed"})
        if self.headers.get("Authorization") != "Bearer " + PLACEHOLDER:
            return self.small_response(401, {"error": "placeholder_required"})
        lengths = self.headers.get_all("Content-Length", [])
        if len(lengths) != 1 or self.headers.get("Transfer-Encoding"):
            return self.small_response(400, {"error": "one_content_length_required"})
        try:
            length = int(lengths[0])
            if length < 0:
                raise ValueError("negative content length")
            self.connection.settimeout(self.remaining())
            body = self.rfile.read(length)
            if len(body) != length:
                raise ValueError("incomplete request body")
        except (ValueError, OSError, TimeoutError):
            return self.small_response(400, {"error": "invalid_or_incomplete_request"})
        refusal = self.dispatch_guard()
        if refusal is not None:
            return self.small_response(503, refusal)
        self.forward(body)

    def forward(self, body: bytes | None):
        event = {"method": self.command, "path": self.path, "attempts": 0,
                 "complete": False, "response_bytes": 0, "status": None}
        if body is not None:
            event["request_sha256"] = hashlib.sha256(body).hexdigest()
        connection = None
        headers_sent = False
        try:
            connection = self.server.connect_upstream(self.remaining())
            with self.server.connections_lock:
                self.server.connections.add(connection)
            headers = {"Authorization": "Bearer " + PLACEHOLDER,
                       "Content-Type": "application/json",
                       "Accept": "application/json, text/event-stream"}
            if self.server.is_host and body is not None:
                # The trusted host relay stamps its own case deadline. Never
                # accept or forward a Candidate-supplied budget extension.
                headers["X-AgentSWE-Lower-Deadline-Monotonic"] = str(self.server.deadline)
                if getattr(self.server, "evaluation_scope", None):
                    headers["X-AgentSWE-Evaluation"] = self.server.evaluation_scope
            event["attempts"] = 1
            connection.request(self.command, self.path, body=body, headers=headers)
            response = connection.getresponse()
            event["status"] = response.status
            self.send_response(response.status)
            # Never forward provider cookies, authentication, or arbitrary
            # redirects. A 3xx status is preserved, but Location is not a new
            # network capability. No redirect is followed by either relay.
            for name in ("Content-Type", "Retry-After", "X-Request-Id"):
                value = response.getheader(name)
                if value is not None:
                    self.send_header(name, value)
            self.send_header("Connection", "close")
            self.end_headers()
            headers_sent = True
            while True:
                # read1 preserves first-byte/SSE delivery instead of waiting
                # for the entire upstream response. Recheck the case deadline.
                if connection.sock:
                    connection.sock.settimeout(self.remaining())
                self.connection.settimeout(self.remaining())
                chunk = response.read1(65536)
                if not chunk:
                    if response.length not in (None, 0):
                        raise http.client.IncompleteRead(b"", response.length)
                    break
                self.wfile.write(chunk)
                self.wfile.flush()
                event["response_bytes"] += len(chunk)
            event["complete"] = True
        except Exception as exc:
            # Do not invent a retryable 502 or replay an ambiguous submission.
            # Before headers, report a distinct local transport error. After
            # headers, end the incomplete stream; host evidence remains N/A.
            event["error_type"] = type(exc).__name__
            event["upstream_outcome"] = "unknown" if event["attempts"] else "not_dispatched"
            if not headers_sent:
                try:
                    self.small_response(598, {"error": "local_transport_incomplete", "outcome": event["upstream_outcome"]})
                except OSError:
                    pass
        finally:
            self.close_connection = True
            if connection:
                with self.server.connections_lock:
                    self.server.connections.discard(connection)
                connection.close()
            with self.server.events_lock:
                self.server.events.append(event)


class UnixServer(socketserver.ThreadingMixIn, socketserver.UnixStreamServer):
    daemon_threads = True
    block_on_close = False


_EVALUATION_SCOPE = None


def set_evaluation_scope(scope):
    """Bind this evaluator process to one evaluation identity scope.

    Process-local on purpose: the relay runs inside the evaluator launcher, never
    inside the candidate sandbox, so the scope cannot be read, set or forged by the
    product. Two Candidates run the same case from the same pristine repository and
    so issue byte-identical first requests; without a scope they share one logical
    request identity and a single transient upstream failure makes the broker refuse
    every later Candidate for the rest of the run.
    """
    global _EVALUATION_SCOPE
    if scope is not None and not re.fullmatch(r'[0-9a-f]{8,64}', scope):
        raise ValueError('evaluation scope must be an evaluator hex digest')
    _EVALUATION_SCOPE = scope


def configure_server(server, deadline: float, connect_upstream, *, is_host: bool):
    if not math.isfinite(deadline) or deadline <= time.monotonic():
        raise ValueError("transport deadline must be finite and in the future")
    server.deadline = deadline
    server.connect_upstream = connect_upstream
    server.is_host = is_host
    server.evaluation_scope = _EVALUATION_SCOPE
    server.events = []
    server.events_lock = threading.Lock()
    server.dispatch_reserve = (LOWER_DISPATCH_RESERVE_SECONDS if is_host
                               else BRIDGE_DISPATCH_RESERVE_SECONDS)
    server.guard_refusals = []
    server.guard_lock = threading.Lock()
    server.connections = set()
    server.connections_lock = threading.Lock()


class FixedLowerRelay:
    def __init__(self, endpoint: str, deadline: float):
        parsed = urllib.parse.urlsplit(endpoint)
        if (parsed.scheme != "http" or parsed.hostname != "127.0.0.1" or not parsed.port
                or parsed.path != MODEL_PATH or parsed.username or parsed.password
                or parsed.query or parsed.fragment):
            raise ValueError("relay requires the fixed loopback Responses broker")
        self.temporary = tempfile.TemporaryDirectory(prefix="agentswe-openclaw-relay-")
        self.socket_path = Path(self.temporary.name) / "lower.sock"
        self.server = UnixServer(str(self.socket_path), RelayHandler)
        configure_server(self.server, deadline,
                         lambda timeout: http.client.HTTPConnection("127.0.0.1", parsed.port, timeout=timeout),
                         is_host=True)
        self.thread = threading.Thread(target=self.server.serve_forever, kwargs={"poll_interval": 0.1}, daemon=True)
        self.thread.start()

    def dispatch_guard(self):
        """Evidence that the evaluator's budget guard, not the Candidate, ended the loop."""
        with self.server.guard_lock:
            refusals = [dict(item) for item in self.server.guard_refusals]
        with self.server.events_lock:
            completed = sum(1 for item in self.server.events if item.get("complete"))
        return {"schema_version": DISPATCH_GUARD_SCHEMA,
                "reserve_seconds": float(self.server.dispatch_reserve),
                "case_deadline_monotonic": self.server.deadline,
                "turns_completed": completed,
                "refusals": refusals, "refused_requests": len(refusals),
                "stopped_by_evaluator_budget_guard": bool(refusals)}

    def snapshot(self):
        with self.server.events_lock:
            events = [dict(item) for item in self.server.events]
        with self.server.connections_lock:
            active = len(self.server.connections)
        return {"schema_version": "openclaw-fixed-lower-transport-v1", "events": events,
                "dispatch_guard": self.dispatch_guard(),
                "active_connections": active, "relay_retries": 0, "redirects_followed": 0,
                "incomplete": any(not item["complete"] for item in events) or active > 0,
                "upstream_broker_streaming_or_retry_policy_verified": False}

    def close(self):
        with self.server.connections_lock:
            connections = list(self.server.connections)
        for connection in connections:
            if connection.sock:
                try:
                    connection.sock.shutdown(socket.SHUT_RDWR)
                except OSError:
                    pass
            connection.close()
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=1)
        self.temporary.cleanup()


def inside_main(argv=None):
    parser = argparse.ArgumentParser()
    parser.add_argument("--inside", action="store_true", required=True)
    parser.add_argument("--socket", required=True)
    parser.add_argument("--port", type=int, required=True)
    parser.add_argument("--deadline", type=float, required=True)
    parser.add_argument("command", nargs=argparse.REMAINDER)
    args = parser.parse_args(argv)
    command = args.command[1:] if args.command[:1] == ["--"] else args.command
    if not command:
        raise ValueError("actual product command is required")
    server = ThreadingHTTPServer(("127.0.0.1", args.port), RelayHandler)
    configure_server(server, args.deadline, lambda timeout: UnixConnection(args.socket, timeout), is_host=False)
    check = UnixConnection(args.socket, min(5, args.deadline - time.monotonic()))
    try:
        check.request("GET", HEALTH_PATH)
        response = check.getresponse()
        if response.status != 200 or json.loads(response.read()).get("model_request") is not False:
            raise RuntimeError("fixed lower relay preflight failed")
    finally:
        check.close()
    thread = threading.Thread(target=server.serve_forever, kwargs={"poll_interval": 0.1}, daemon=True)
    thread.start()
    process = subprocess.Popen(command, close_fds=True)
    def stop(_signum, _frame):
        if process.poll() is None:
            process.terminate()
    previous = signal.signal(signal.SIGTERM, stop)
    try:
        return process.wait(timeout=max(0.001, args.deadline - time.monotonic()))
    finally:
        if process.poll() is None:
            process.kill()
        process.wait(timeout=1)
        server.shutdown()
        server.server_close()
        signal.signal(signal.SIGTERM, previous)


if __name__ == "__main__":
    raise SystemExit(inside_main())
