#!/usr/bin/env python3
"""Judge-only transport: one upstream submission per accepted HTTP request.

Retry policy belongs exclusively to result_judge.call_judge (also used by
Code). This broker NEVER retries or follows redirects. A disposable, exactly
owned subprocess bounds DNS/TLS/headers/body reads even for trickling servers.
Credentials and prompts travel through anonymous pipes, not files or argv.
"""
from __future__ import annotations

import argparse
import base64
import hashlib
import http.server
import io
import json
import math
import os
from pathlib import Path
import select
import signal
import socket
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
import uuid
from responses_stream import RedactedCapture, read_response, direct_opener, strict_json

MODEL = "deepseek-flash"
EFFORT = "max"
MAX_BODY = 8 * 1024 * 1024
MAX_RESPONSE = 32 * 1024 * 1024
PLACEHOLDERS = {"broker-only-placeholder", "judge-only-placeholder"}
STATS_TOKEN = "stats-only-placeholder"
UPSTREAM = "https://api.deepseek.com/v1/responses"


def provider_key(path):
    values = {}
    for raw in Path(path).read_text().splitlines():
        if "=" in raw and not raw.lstrip().startswith("#"):
            key, value = raw.split("=", 1)
            values[key.strip()] = value.strip().strip("'\"")
    for name in ("DEEPSEEK_API_KEY", "AGENTSWE_PROVIDER_KEY", "OPENAI_API_KEY"):
        if values.get(name):
            return values[name]
    raise RuntimeError("canonical credential has no supported provider key")


def worker():
    """Exactly one urlopen; no retry, even when server-side completion is unknown."""
    data = json.loads(sys.stdin.buffer.read())
    request = urllib.request.Request(data["endpoint"],
        data=data["body"].encode(), method="POST", headers={
            "Authorization": "Bearer " + data["key"], "Content-Type": "application/json",
            "Accept": "text/event-stream, application/json", "Accept-Encoding": "identity",
            "User-Agent": "AgentSWE-Edit-Judge-Broker/1.0"})
    # Disable environment-derived proxying and all implicit redirect requests.
    handle = capture = None
    status = 598
    result = {"transport_complete": False}
    request_started = False

    def mark_request_start():
        nonlocal request_started
        os.write(data["started_fd"], b"S")
        request_started = True
        os.close(data["started_fd"])

    try:
        opener = direct_opener(on_request_start=mark_request_start)
        # Persist partial bytes independently of worker stdout: the parent may
        # have to kill this exact process at its deadline or on client loss.
        handle = Path(data['capture_path']).open('xb')
        capture = RedactedCapture(handle, data['key'])
        try:
            response = opener.open(request, timeout=data["timeout"])
        except urllib.error.HTTPError as exc:
            response = exc  # Preserve the REAL status, including retryable ones.
        with response:
            status = response.code
            headers = {'upstream_http_status': status, 'content_type': response.headers.get('Content-Type', ''),
                       'retry_after': response.headers.get('Retry-After')}
            # Safe header fields only; no Authorization/cookies or provider body.
            with Path(data['headers_path']).open('x') as header_file:
                json.dump(headers, header_file); header_file.flush(); os.fsync(header_file.fileno())
            if response.headers.get('Content-Encoding', 'identity').lower() not in ('', 'identity'):
                raise ValueError('unexpected_compressed_response')
            read_response(response.read1, content_type=headers['content_type'],
                          is_success=status == 200, capture=capture.write, max_bytes=MAX_RESPONSE)
            result = {'transport_complete': True, 'status': status}
    except Exception as exc:
        # Never turn an ambiguous failure into retryable 502/504.
        result = {"transport_complete": False, "status": status,
                  "error": "uncertain_upstream_" + type(exc).__name__}
    finally:
        result['request_start_observed'] = request_started
        result['failure_phase'] = ('after_http_send_possible' if request_started
                                   else 'before_http_send')
        if not request_started:
            os.close(data["started_fd"])
        if capture is not None:
            capture.finish()
        if handle is not None:
            handle.close()
    print(json.dumps(result), flush=True)


# The field allowlist below exists so nothing can steer the judge. A closed
# output schema is different in kind from an instruction: it restricts the shape
# of the verdict and cannot tell the model what to conclude -- except through
# annotation fields some models read as guidance, and through any long string
# smuggled in as a key, name or enum value. Both are refused here, so admitting
# ``text`` widens the protocol by exactly one constrained value and no more.
SCHEMA_ANNOTATIONS = frozenset({"description", "title", "$comment", "examples", "default"})
MAX_SCHEMA_STRING = 64


def closed_output_schema(node) -> bool:
    if isinstance(node, dict):
        if SCHEMA_ANNOTATIONS & set(node):
            return False
        if node.get("type") == "object" and node.get("additionalProperties") is not False:
            return False
        return all(closed_output_schema(key) and closed_output_schema(value)
                   for key, value in node.items())
    if isinstance(node, list):
        return all(closed_output_schema(item) for item in node)
    if isinstance(node, str):
        return len(node) <= MAX_SCHEMA_STRING
    return True


def valid_response_format(value) -> bool:
    if not isinstance(value, dict) or set(value) != {"format"}:
        return False
    fmt = value["format"]
    if not isinstance(fmt, dict) or set(fmt) != {"type", "name", "strict", "schema"}:
        return False
    if (fmt["type"] != "json_schema" or fmt["name"] != "result_judge_verdict"
            or fmt["strict"] is not True or not isinstance(fmt["schema"], dict)):
        return False
    return closed_output_schema(fmt["schema"])


class State:
    def __init__(self, path, instance_id=None):
        self.path = Path(path)
        self.instance_id = instance_id or uuid.uuid4().hex
        self.path.parent.mkdir(parents=True, exist_ok=True)
        # A new broker must not overwrite an earlier run's evidence.
        with self.path.open("x") as handle:
            handle.write("{}\n")
        self.lock = threading.Lock()
        self.events = []
        self.persist()

    def snapshot(self):
        completed = [e for e in self.events if e["state"] != "in_flight"]
        successful = [e for e in completed if e.get("completed_response")]
        usage = {key: sum(e.get("usage", {}).get(key, 0) for e in completed)
                 for key in ("input_tokens", "output_tokens", "total_tokens")}
        return {"schema_version": "agentswe-judge-broker-stats/v1",
            "broker_instance_id": self.instance_id,
            "protocol": {"model": MODEL, "reasoning_effort": EFFORT,
                         "max_output_tokens": 64000, "inner_retries": 0,
                         "max_upstream_attempts_per_transport": 1,
                         "redirects_allowed": False, "absolute_deadline_seconds_max": 900,
                         "response_transport_modes": ["stream", "nonstream"],
                         "downstream_response_format": "terminal_json", "connect_timeout_seconds_max": 30,
                         "upstream_attempt_marker": "before_first_http_bytes_after_connection"},
            "runtime": {"calls": len(self.events), "completed_calls": len(completed),
                "successful_calls": len(successful), "failures": len(completed)-len(successful),
                "in_flight_calls": len(self.events)-len(completed), "tokens": usage["total_tokens"],
                "upstream_attempts": sum(e.get("upstream_attempts", 0) for e in self.events),
                "usage_unknown_calls": sum(e.get("usage_unknown", True) for e in self.events), **usage},
            "attempts": self.events, "credential_values_recorded": False}

    def persist(self):
        temporary = self.path.with_suffix(".tmp")
        with temporary.open("w") as handle:
            json.dump(self.snapshot(), handle, indent=2)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, self.path)

    def begin(self, body):
        with self.lock:
            event = {"request_id": uuid.uuid4().hex, "state": "in_flight",
                "payload_sha256": hashlib.sha256(body.encode()).hexdigest(),
                "upstream_attempts": 0, "usage_unknown": True}
            self.events.append(event)
            self.persist()  # Fail closed BEFORE issuing a request if evidence cannot persist.
            return event

    def update(self, event, **values):
        with self.lock:
            event.update(values)
            self.persist()


class Server(http.server.ThreadingHTTPServer):
    daemon_threads = True

    def __init__(self, bind, *, key, endpoint, stats_path, timeout=900, keepalive=5, instance_id=None):
        if not 0 < timeout <= 900 or not 0 < keepalive <= 5:
            raise ValueError("invalid fixed transport bounds")
        self.state = State(stats_path, instance_id)
        self.key, self.endpoint = key, endpoint
        self.timeout, self.keepalive = timeout, keepalive
        self.worker_lock = threading.Lock()
        self.workers = set()
        self.stopping = threading.Event()
        super().__init__(bind, Handler)

    def spawn_worker(self, write_fd):
        with self.worker_lock:
            if self.stopping.is_set():
                raise RuntimeError("broker_stopping")
            process = subprocess.Popen([sys.executable, str(Path(__file__).resolve()), "--worker"],
                stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                pass_fds=(write_fd,))
            self.workers.add(process)
            return process

    def stop_owned_workers(self):
        self.stopping.set()
        with self.worker_lock:
            workers = list(self.workers)
        for process in workers:
            try:
                process.kill()
            except ProcessLookupError:
                pass
        # A cleanup grace period, not extra time for upstream generation.
        until = time.monotonic() + 2
        while time.monotonic() < until:
            with self.state.lock:
                in_flight = self.state.snapshot()["runtime"]["in_flight_calls"]
            if not in_flight:
                break
            time.sleep(.02)
        for process in workers:
            process.wait(timeout=1)


class Handler(http.server.BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, *_args):
        pass  # No request payloads, credentials, or provider error bodies in logs.

    def send_body(self, status, body, headers=None):
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Connection", "close")
        for key, value in (headers or {}).items():
            self.send_header(key, value)
        self.end_headers()
        self.wfile.write(body)
        self.wfile.flush()
        self.close_connection = True

    def error(self, status, reason):
        self.send_body(status, json.dumps({"error": {"type": reason}}).encode())

    def do_GET(self):
        if self.path in ("/health", "/healthz"):
            self.send_body(200, b'{"status":"ok","model":"deepseek-flash","effort":"max"}')
        elif self.path in ("/stats", "/stats.json", "/v1/stats"):
            if self.headers.get("Authorization") != "Bearer " + STATS_TOKEN:
                self.error(401, "unauthorized")
                return
            with self.server.state.lock:
                raw = json.dumps(self.server.state.snapshot()).encode()
            self.send_body(200, raw)
        else:
            self.error(404, "not_found")

    def do_POST(self):
        if self.path.rstrip("/") != "/v1/responses":
            self.error(404, "responses_only")
            return
        if self.headers.get("Authorization") not in {"Bearer " + p for p in PLACEHOLDERS}:
            self.error(401, "unauthorized")
            return
        started = time.monotonic()
        try:
            budget = min(float(self.headers.get("X-AgentSWE-Judge-Deadline-Seconds", "900")),
                         self.server.timeout)
            length = int(self.headers.get("Content-Length", "0"))
            if not math.isfinite(budget) or budget <= 0 or not 0 < length <= MAX_BODY:
                raise ValueError()
            self.connection.settimeout(min(10, budget))
            body = json.loads(self.rfile.read(length))
            if (not isinstance(body, dict) or body.get("model") != MODEL
                    or body.get("reasoning") != {"effort": EFFORT}
                    or body.get("max_output_tokens") != 64000 or type(body.get("stream")) is not bool
                    or set(body) - {"model", "reasoning", "max_output_tokens", "stream", "input", "text"}
                    or not isinstance(body.get("input"), list) or not body["input"]
                    or ("text" in body and not valid_response_format(body["text"]))):
                raise ValueError()
            encoded = json.dumps(body, ensure_ascii=False)
        except (ValueError, TimeoutError, OSError):
            self.error(400, "invalid_judge_request")
            return
        deadline = started + budget
        event = self.server.state.begin(encoded)
        response_dir = self.server.state.path.parent / (self.server.state.path.stem + '-responses')
        response_dir.mkdir(exist_ok=True)
        response_path = response_dir / (event['request_id'] + '.raw')
        headers_path = response_dir / (event['request_id'] + '.http.json')
        self.server.state.update(event, transport_mode='stream' if body['stream'] else 'nonstream',
            provider_response_path=str(response_path), provider_headers_path=str(headers_path))
        read_fd, write_fd = os.pipe()
        process = None
        reader = None
        done = threading.Event()
        result = {}
        reason = None
        try:
            process = self.server.spawn_worker(write_fd)
            self.server.state.update(event, worker_pid=process.pid)
            message = json.dumps({"endpoint": self.server.endpoint, "key": self.server.key,
                "body": encoded, "timeout": max(.001, deadline-time.monotonic()),
                "started_fd": write_fd, 'capture_path': str(response_path), 'headers_path': str(headers_path)}).encode()
            os.close(write_fd)
            write_fd = -1

            def communicate():
                try:
                    output, _ = process.communicate(message)
                    result["output"] = output
                except Exception as exc:
                    result["worker_error"] = type(exc).__name__
                finally:
                    done.set()

            reader = threading.Thread(target=communicate, daemon=True)
            reader.start()
            next_keepalive = time.monotonic() + self.server.keepalive
            while not done.is_set():
                if select.select([read_fd], [], [], 0)[0]:
                    marker = os.read(read_fd, 1)
                    if marker:
                        self.server.state.update(event, upstream_attempts=1)
                if time.monotonic() >= deadline:
                    reason = "absolute_deadline_delivery_unknown"
                    break
                if select.select([self.connection], [], [], 0)[0]:
                    if not self.connection.recv(1, socket.MSG_PEEK):
                        reason = "client_disconnected_delivery_unknown"
                        break
                if time.monotonic() >= next_keepalive:
                    try:
                        self.send_response_only(100)
                        self.end_headers()
                        self.wfile.flush()
                    except OSError:
                        reason = "client_disconnected_delivery_unknown"
                        break
                    next_keepalive = time.monotonic() + self.server.keepalive
                done.wait(min(.05, max(0, deadline-time.monotonic())))
        except Exception as exc:
            reason = "broker_failure_" + type(exc).__name__
        finally:
            if process is not None and not done.is_set():
                # Only this Popen-owned network worker; never a broad PID/pattern kill.
                process.kill()
            if reader is not None:
                reader.join(5)
            if process is not None:
                process.wait(timeout=5)
                with self.server.worker_lock:
                    self.server.workers.discard(process)
            if select.select([read_fd], [], [], 0)[0] and os.read(read_fd, 1):
                self.server.state.update(event, upstream_attempts=1)
            os.close(read_fd)
            if write_fd >= 0:
                os.close(write_fd)
        headers = {"X-AgentSWE-Upstream-Attempts": str(event["upstream_attempts"])}
        metadata, response = {}, None
        try:
            upstream = json.loads(result.get('output', b''))
        except ValueError:
            upstream = {}
        status = 598
        raw = json.dumps({'error': {'type': reason or upstream.get('error', 'worker_protocol_failure')}}).encode()
        if headers_path.is_file():
            observed_headers = json.loads(headers_path.read_text())
            metadata.update(observed_headers)
            metadata['provider_headers_sha256'] = hashlib.sha256(headers_path.read_bytes()).hexdigest()
        if response_path.is_file():
            captured = response_path.read_bytes()
            metadata['provider_response_sha256'] = hashlib.sha256(captured).hexdigest()
            metadata['provider_response_bytes'] = len(captured)
            if upstream.get('transport_complete') is True:
                status = metadata.get('upstream_http_status', 598)
                raw = captured
            try:
                if metadata.get('upstream_http_status') == 200 and metadata.get('content_type', '').split(';', 1)[0].strip().lower() == 'text/event-stream':
                    _, response = read_response(io.BytesIO(captured).read1,
                        content_type=metadata['content_type'], is_success=True)
                    # The saved complete terminal can survive a race with worker
                    # termination. Preserve it, never submit a replacement.
                    status = 200
                    raw = json.dumps(response, ensure_ascii=False).encode()
                else:
                    response = strict_json(captured)
            except (ValueError, UnicodeError):
                response = None
        raw = raw.replace(self.server.key.encode(), b'[REDACTED]')
        usage = response.get('usage') if isinstance(response, dict) else None
        if (isinstance(usage, dict) and all(type(usage.get(k)) is int and usage[k] >= 0
                for k in ('input_tokens', 'output_tokens', 'total_tokens'))
                and usage['total_tokens'] >= usage['input_tokens'] + usage['output_tokens']):
            metadata.update(usage={k: usage[k] for k in ('input_tokens', 'output_tokens', 'total_tokens')}, usage_unknown=False)
        metadata['completed_response'] = bool(status == 200 and isinstance(response, dict)
            and response.get('status') == 'completed' and response.get('model') == MODEL and response.get('error') is None)
        metadata['response_status'] = response.get('status') if isinstance(response, dict) else None
        metadata['downstream_response_sha256'] = hashlib.sha256(raw).hexdigest()
        metadata['worker_transport_complete'] = upstream.get('transport_complete') is True
        metadata['request_start_observed'] = upstream.get('request_start_observed')
        metadata['failure_phase'] = upstream.get('failure_phase')
        metadata['worker_error_type'] = upstream.get('error')
        if (event['upstream_attempts'] == 0 and process is not None and process.returncode == 0
                and upstream.get('request_start_observed') is False
                and upstream.get('failure_phase') == 'before_http_send'):
            metadata.update(usage={'input_tokens': 0, 'output_tokens': 0, 'total_tokens': 0},
                            usage_unknown=False, upstream_submission_proven_absent=True)
        retry_after = metadata.pop('retry_after', None)
        if isinstance(retry_after, str) and '\r' not in retry_after and '\n' not in retry_after:
            headers['Retry-After'] = retry_after
        self.server.state.update(event, state="terminal", elapsed_seconds=time.monotonic()-started,
            worker_exit_code=process.returncode if process else None,
            worker_reaped=process is None or process.poll() is not None,
            downstream_http_status=status, transport_abort_reason=reason, **metadata)
        try:
            self.send_body(status, raw, headers)
        except OSError:
            self.server.state.update(event, client_delivery_failed=True)


def main():
    if sys.argv[1:] == ["--worker"]:
        worker()
        return
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--credential-file", type=Path, required=True)
    parser.add_argument("--bind", default="0.0.0.0")
    parser.add_argument("--port", type=int, required=True)
    parser.add_argument("--upstream", default=UPSTREAM)
    parser.add_argument("--stats-path", type=Path, required=True)
    parser.add_argument("--timeout", type=float, default=900)
    parser.add_argument("--instance-id", default=None)
    args = parser.parse_args()
    server = Server((args.bind, args.port), key=provider_key(args.credential_file),
        endpoint=args.upstream, stats_path=args.stats_path, timeout=args.timeout, instance_id=args.instance_id)

    def stop(_signum, _frame):
        raise SystemExit(128 + _signum)

    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)
    try:
        server.serve_forever()
    finally:
        server.stop_owned_workers()
        server.server_close()


if __name__ == "__main__":
    main()
