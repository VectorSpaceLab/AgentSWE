#!/usr/bin/env python3
"""AI Scientist evaluator-only medium transport, reused from verified OpenClaw.

Only the single-upstream LowerServer is reachable from this CLI. Legacy
classes are retained as source provenance, never selected for Builder/judge.
"""
from __future__ import annotations
import argparse, json, os, signal, threading, time, urllib.error, urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
import ctypes, hashlib, http.client, math, select, socket, struct, subprocess, sys, uuid
sys.path.insert(0, str(Path(__file__).resolve().parent))
# Docker mounts the two identity modules beside /broker.py; direct task CLI
# workers have a sanitized environment and must resolve the trusted task root.
_task_root = Path(__file__).resolve().parent.parent
if (_task_root / 'agentloop/lower_request_identity.py').is_file():
    sys.path.insert(0, str(_task_root))
from responses_stream import direct_opener, evaluator_proxy_url, read_response, strict_json
from lower_request_reserve import RequestReserve, ReplayForbidden, load_history
try:
    from agentloop.lower_request_identity import CONTEXT_HEADER, private_file, normalized_payload
except ModuleNotFoundError:
    from lower_request_identity import CONTEXT_HEADER, private_file, normalized_payload

MODEL = "deepseek-flash"
EFFORT = "high"
PLACEHOLDER = "broker-only-placeholder"
STATS_TOKEN = "stats-only-placeholder"
MAX_ATTEMPTS = 6
MAX_CALL_SECONDS = 720.0
RETRYABLE_HTTP_STATUS = {429, 500, 502, 503, 504, 520, 521, 522, 523, 524}

class Stats:
    def __init__(self) -> None:
        self.lock = threading.Lock(); self.calls = 0; self.failures = 0; self.successful_calls = 0
        self.client_failures = 0; self.provider_failures = 0
        self.last_error = None; self.last_status = None
        self.input_tokens = 0; self.output_tokens = 0
    def record(self, payload: dict, ok: bool, usage: dict | None = None, failure_kind: str | None = None, error: str | None = None, status: int | None = None) -> None:
        with self.lock:
            self.calls += 1; self.failures += int(not ok); self.successful_calls += int(ok)
            self.client_failures += int(not ok and failure_kind == "client")
            self.provider_failures += int(not ok and failure_kind == "provider")
            if not ok:
                self.last_error = error
                self.last_status = status
            usage = usage or {}
            self.input_tokens += int(usage.get("input_tokens", 0) or usage.get("prompt_tokens", 0) or 0)
            self.output_tokens += int(usage.get("output_tokens", 0) or usage.get("completion_tokens", 0) or 0)
    def snapshot(self) -> dict:
        with self.lock:
            return {"schema_version":"agentswe-broker-stats-v1", "protocol":{"model":MODEL,"reasoning_effort":EFFORT,"transport":"evaluator-owned-responses-broker"}, "runtime":{"calls":self.calls,"failures":self.failures,"successful_calls":self.successful_calls,"client_failures":self.client_failures,"provider_failures":self.provider_failures,"last_error":self.last_error,"last_status":self.last_status,"input_tokens":self.input_tokens,"output_tokens":self.output_tokens,"total_tokens":self.input_tokens+self.output_tokens}}

class Handler(BaseHTTPRequestHandler):
    server_version = "AgentSWE-OpenClaw-Broker/1.0"
    def do_GET(self) -> None:
        if self.path.rstrip("/") == "/healthz": return self.send_json(200, {"ok": True, "protocol": {"model": MODEL, "reasoning_effort": EFFORT}})
        if self.path.rstrip("/") == "/stats":
            if self.headers.get("Authorization") != f"Bearer {STATS_TOKEN}":
                return self.send_json(401, {"error": {"type": "unauthorized", "message": "stats requires evaluator authorization"}})
            return self.send_json(200, self.server.stats.snapshot())
        self.send_error(404)
    def do_POST(self) -> None:
        if self.path.rstrip("/") != "/v1/responses": return self.send_error(404)
        if self.headers.get("Authorization") != f"Bearer {PLACEHOLDER}": return self.send_error(401)
        try:
            length = int(self.headers.get("Content-Length", "0")); body = json.loads(self.rfile.read(length))
            if not isinstance(body, dict): raise ValueError("object required")
        except Exception as exc:
            self.server.stats.record({}, False, failure_kind="client", error=type(exc).__name__); return self.send_json(400, {"error":{"type":"invalid_request","message":str(exc)}})
        body["model"] = MODEL; body["reasoning"] = {"effort": EFFORT}; body.pop("max_output_tokens", None)
        upstream = self.server.upstream
        if not upstream:
            self.server.stats.record(body, False, failure_kind="provider", error="provider_unconfigured", status=503); return self.send_json(503, {"error":{"type":"provider_unconfigured","message":"broker upstream is not configured"}})
        request = urllib.request.Request(upstream, data=json.dumps(body).encode(), headers={"Authorization":f"Bearer {self.server.upstream_key}","Content-Type":"application/json","Accept":"application/json, text/event-stream","User-Agent":"AgentSWE-OpenClaw-Broker/2.0"}, method="POST")
        last_error = "provider_request_failed"
        last_status = None
        deadline = time.monotonic() + MAX_CALL_SECONDS
        for attempt in range(1, MAX_ATTEMPTS + 1):
            if time.monotonic() >= deadline:
                last_error = "provider_call_deadline"
                break
            try:
                timeout = max(1.0, min(240.0, deadline - time.monotonic()))
                with urllib.request.urlopen(request, timeout=timeout) as response:
                    data = response.read()
                    content_type = response.headers.get("Content-Type", "application/json")
                    status = response.status
                # The Responses API may return a JSON object or a valid SSE
                # stream. OpenClaw's Responses client consumes both; parsing
                # every body as JSON incorrectly turns SSE into a provider
                # failure.
                usage = None
                if "text/event-stream" not in content_type.lower():
                    value = json.loads(data)
                    if not isinstance(value, dict):
                        raise ValueError("upstream response is not an object")
                    usage = value.get("usage")
                self.server.stats.record(body, True, usage)
                return self.send_bytes(status, data, content_type)
            except urllib.error.HTTPError as exc:
                last_status = exc.code
                last_error = f"http_{exc.code}"
                if exc.code not in RETRYABLE_HTTP_STATUS:
                    break
            except Exception as exc:
                last_error = f"transport_{type(exc).__name__}"
            if attempt < MAX_ATTEMPTS:
                delay = min(8.0, float(2 ** (attempt - 1)))
                if time.monotonic() + delay >= deadline:
                    last_error = "provider_call_deadline"
                    break
                time.sleep(delay)
        self.server.stats.record(body, False, failure_kind="provider", error=last_error, status=last_status)
        error = {"type": "provider_error", "message": last_error}
        if last_status is not None:
            error["status"] = last_status
        return self.send_json(502, {"error": error})
    def send_bytes(self, code: int, data: bytes, content_type: str = "application/json") -> None:
        self.send_response(code); self.send_header("Content-Type",content_type); self.send_header("Content-Length",str(len(data))); self.end_headers(); self.wfile.write(data)
    def send_json(self, code: int, value: dict) -> None: self.send_bytes(code, json.dumps(value).encode())
    def log_message(self, *_: object) -> None: return

def dotenv(path: Path | None) -> dict[str, str]:
    result: dict[str, str] = {}
    if path is None:
        return result
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if line and not line.startswith("#") and "=" in line:
            key, value = line.split("=", 1)
            result[key.strip()] = value.strip().strip("'\"")
    return result


# Lower-only transport. The legacy Handler/Stats above remain unchanged for
# explicit --builder callers. No lower request enters their retry loop.
LOWER_EFFORT = "high"
LOWER_MAX_SECONDS = 600.0
LOWER_DEADLINE_HEADER = "X-AgentSWE-Lower-Deadline-Monotonic"


class NoLowerRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


class StreamingRedactor:
    """Redact literal credential bytes even across chunks, without buffering SSE."""
    def __init__(self, secret: str):
        self.secret = secret.encode()
        self.pending = b""

    def feed(self, chunk: bytes, *, final=False) -> bytes:
        data = (self.pending + chunk).replace(self.secret, b"[REDACTED]") if self.secret else self.pending + chunk
        self.pending = b""
        if not final:
            for size in range(min(len(self.secret) - 1, len(data)), 0, -1):
                if data.endswith(self.secret[:size]):
                    self.pending = data[-size:]
                    return data[:-size]
        return data


class ResponseObservation:
    """Observe, never rewrite, a JSON response or typed Responses SSE events.

    A known terminal incomplete response is not a severed network stream.
    Missing usage stays unknown instead of being invented as zero usage.
    """
    def __init__(self, content_type: str):
        self.sse = "text/event-stream" in content_type.lower()
        self.pending = b""
        self.data_lines = []
        self.response = None
        self.invalid = False
        self.terminal_events = 0

    def accept(self, value):
        if not isinstance(value, dict):
            self.invalid = True
            return
        if self.sse:
            kind = value.get("type")
            if not isinstance(kind, str):
                self.invalid = True
            if kind not in {"response.completed", "response.incomplete", "response.failed", "response.done"}:
                return
            value = value.get("response")
            self.terminal_events += 1
        if not isinstance(value, dict) or value.get("status") not in {"completed", "incomplete", "failed", "cancelled"}:
            self.invalid = True
            return
        self.response = value

    def event(self):
        data = b"\n".join(self.data_lines)
        self.data_lines = []
        if data and data != b"[DONE]":
            try:
                self.accept(json.loads(data))
            except (ValueError, UnicodeError):
                self.invalid = True

    def feed(self, data: bytes):
        self.pending += data
        if self.sse:
            while b"\n" in self.pending:
                line, self.pending = self.pending.split(b"\n", 1)
                line = line.rstrip(b"\r")
                if not line:
                    self.event()
                elif line.startswith(b"data:"):
                    self.data_lines.append(line[5:].lstrip(b" "))

    def finish(self):
        if self.sse:
            # An unterminated partial event is not silently manufactured into
            # a valid terminal event. Whitespace after a complete event is OK.
            if self.pending.strip() or self.data_lines:
                self.invalid = True
        else:
            try:
                self.accept(json.loads(self.pending))
            except (ValueError, UnicodeError):
                self.invalid = True
        response = self.response or {}
        usage = response.get("usage")
        known = isinstance(usage, dict) and all(type(usage.get(k)) is int and usage[k] >= 0
            for k in ("input_tokens", "output_tokens", "total_tokens"))
        return {"response_status": response.get("status"), "returned_model": response.get("model"),
            "model_identity_valid": response.get("model") == MODEL,
            "response_id": response.get("id"), "incomplete_details": response.get("incomplete_details"),
            "response_error_present": response.get("error") is not None,
            "terminal_event_count": self.terminal_events, "protocol_invalid": self.invalid,
            "terminal_observed": self.response is not None and not self.invalid,
            "usage": {k: usage[k] for k in ("input_tokens", "output_tokens", "total_tokens")} if known else None,
            "usage_unknown": not known}


def lower_frame(kind: bytes, data: bytes):
    sys.stdout.buffer.write(kind + struct.pack("!I", len(data)) + data)
    sys.stdout.buffer.flush()


def lower_worker():
    """One owned network attempt; credentials arrive only through anonymous stdin."""
    message = json.loads(sys.stdin.buffer.read())
    # Linux broker workers must die with their exact parent even if it cannot
    # run finally (SIGKILL). This is not a broad process-pattern cleanup.
    if sys.platform != "linux" or ctypes.CDLL(None, use_errno=True).prctl(1, signal.SIGKILL, 0, 0, 0) != 0:
        raise RuntimeError("lower worker parent-death protection unavailable")
    if os.getppid() != message["parent_pid"]:
        raise RuntimeError("lower worker parent identity changed before request")
    try:
        remaining = message["deadline"] - time.monotonic()
        if remaining <= 0:
            raise TimeoutError("deadline before dispatch")
        request = urllib.request.Request(message["endpoint"], data=message["body"].encode(), method="POST",
            headers={"Authorization": "Bearer " + message["key"], "Content-Type": "application/json",
                     "Accept": "application/json, text/event-stream", "User-Agent": "AgentSWE-OpenClaw-Lower/3.0"})
        opener = direct_opener(on_request_start=lambda: lower_frame(b"A", b"1"))
        try:
            response = opener.open(request, timeout=remaining)
        except urllib.error.HTTPError as exc:
            response = exc
        with response:
            content_type = response.headers.get("Content-Type", "application/json")
            redactor = StreamingRedactor(message["key"])
            metadata = {"status": response.code, "content_type": content_type,
                        "retry_after": response.headers.get("Retry-After")}
            encoded = json.dumps(metadata).encode().replace(message["key"].encode(), b"[REDACTED]")
            lower_frame(b"H", encoded)
            def safe_read(size):
                while True:
                    if time.monotonic() >= message["deadline"]:
                        raise TimeoutError("absolute lower deadline")
                    chunk = response.read1(size)
                    if not chunk:
                        remaining_length = getattr(response, "length", None)
                        if remaining_length not in (None, 0):
                            raise http.client.IncompleteRead(b"", remaining_length)
                        return redactor.feed(b"", final=True)
                    safe = redactor.feed(chunk)
                    if safe:
                        return safe
            raw, value = read_response(safe_read, content_type=content_type,
                is_success=response.code == 200, capture=lambda chunk: lower_frame(b"D", chunk))
            # Only a typed terminal event (or an explicit final JSON fallback)
            # supplies the envelope. Text deltas never manufacture completion.
            if value is None:
                value = strict_json(raw)
            observer = ResponseObservation("application/json")
            observer.pending = json.dumps(value).encode()
            metadata = observer.finish()
            if content_type.split(";", 1)[0].strip().lower() == "text/event-stream":
                metadata["terminal_event_count"] = 1
            if response.code == 200 and not message["downstream_stream"]:
                lower_frame(b"J", json.dumps(value, ensure_ascii=False).encode())
            lower_frame(b"T", json.dumps({"transport_complete": True, **metadata}).encode())
    except Exception as exc:
        lower_frame(b"E", json.dumps({"error_type": type(exc).__name__, "provider_outcome": "unknown"}).encode())


class LowerState:
    def __init__(self, path: Path, history=None):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.lock = threading.RLock()
        self.history = history or load_history(None)
        self.events = json.loads(json.dumps(self.history['attempts']))
        self.historical_count = len(self.events)
        self.instance_id = uuid.uuid4().hex
        with self.path.open("x") as handle:
            handle.write("{}\n")
        self.persist()

    def snapshot(self):
        with self.lock:
            completed = [e for e in self.events if e["state"] == "terminal"]
            successes = [e for e in completed if e.get("model_response_available")]
            usage = {k: sum((e.get("usage") or {}).get(k, 0) for e in completed)
                     for k in ("input_tokens", "output_tokens", "total_tokens")}
            failed = [e for e in completed if not e.get("model_response_available")]
            return {"schema_version": "agentswe-broker-stats-v2", "broker_instance_id": self.instance_id,
                "protocol": {"model": MODEL, "reasoning_effort": LOWER_EFFORT,
                    "transport": "evaluator-owned-responses-broker", "streaming": True,
                    "upstream_stream": True, "nonstream_downstream": "typed_terminal_json",
                    "inner_retries": 0, "max_upstream_attempts_per_transport": 1, "redirects_allowed": False,
                    "absolute_case_deadline_required": True, "absolute_deadline_seconds_max": LOWER_MAX_SECONDS},
                "runtime": {"calls": len(self.events), "completed_calls": len(completed),
                    "in_flight_calls": len(self.events) - len(completed), "successful_calls": len(successes),
                    "failures": len(failed), "client_failures": sum(e.get("failure_kind") == "client" for e in failed),
                    "provider_failures": sum(e.get("failure_kind") != "client" for e in failed),
                    "upstream_attempts": sum(e.get("upstream_attempts", 0) for e in self.events),
                    "usage_unknown_calls": sum(e.get("usage_unknown", True) for e in self.events),
                    "tokens": usage["total_tokens"], **usage}, "attempts": [dict(e) for e in self.events],
                "credential_values_recorded": False,
                "historical_calls": self.historical_count,
                "current_invocation_calls": len(self.events) - self.historical_count,
                "usage_accounting": {"known_total_tokens": usage["total_tokens"],
                    "complete_total_tokens": None if any(e.get('usage_unknown', True) for e in self.events) else usage["total_tokens"]}}

    def persist(self):
        with self.lock:
            temporary = self.path.with_suffix(".tmp")
            with temporary.open("w") as handle:
                json.dump(self.snapshot(), handle, indent=2)
                handle.flush(); os.fsync(handle.fileno())
            os.replace(temporary, self.path)

    def begin(self, body: bytes, deadline: float, identity=None):
        with self.lock:
            event = {"request_id": identity['request_id'] if identity else uuid.uuid4().hex, "state": "in_flight",
                     "payload_sha256": hashlib.sha256(body).hexdigest(), "deadline_monotonic": deadline,
                     "upstream_attempts": 0, "usage_unknown": True, "response_bytes": 0}
            if identity is not None:
                event['logical_request'] = identity
            self.events.append(event); self.persist()
            return event

    def update(self, event, **values):
        with self.lock:
            event.update(values); self.persist()

    def rejected(self, raw: bytes, kind: str, reason: str):
        event = self.begin(raw, 0)
        self.update(event, state='terminal', model_response_available=False, failure_kind=kind,
            transport_abort_reason=reason, provider_outcome='not_dispatched', usage_unknown=False,
            usage={'input_tokens': 0, 'output_tokens': 0, 'total_tokens': 0}, worker_reaped=True)


class LowerServer(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(self, bind, *, endpoint: str, key: str, stats_path: Path, timeout=LOWER_MAX_SECONDS,
                 context_key: bytes, history_seed: Path | None = None, history_seed_sha256: str | None = None):
        if not math.isfinite(timeout) or not 0 < timeout <= LOWER_MAX_SECONDS:
            raise ValueError("invalid lower timeout")
        history = load_history(history_seed, history_seed_sha256)
        self.request_reserve = RequestReserve(Path(stats_path).parent / 'request_reserve', context_key, history)
        self.state = LowerState(stats_path, history)
        self.endpoint, self.key, self.timeout = endpoint, key, timeout
        self.workers = set(); self.worker_lock = threading.Lock(); self.stopping = threading.Event()
        super().__init__(bind, LowerHandler)

    def spawn_worker(self):
        with self.worker_lock:
            if self.stopping.is_set():
                raise RuntimeError("lower broker is stopping")
            process = subprocess.Popen([sys.executable, str(Path(__file__).resolve()), "--lower-worker"],
                stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                env={"PATH": "/usr/bin:/bin",
                     "AGENTSWE_EVALUATOR_PROXY_URL": evaluator_proxy_url()})
            self.workers.add(process)
            return process

    def stop_owned_workers(self):
        self.stopping.set()
        with self.worker_lock:
            processes = list(self.workers)
        for process in processes:
            if process.poll() is None:
                process.kill()
        for process in processes:
            process.wait(timeout=2)
        end = time.monotonic() + 2
        while time.monotonic() < end:
            if self.state.snapshot()["runtime"]["in_flight_calls"] == 0:
                break
            time.sleep(.02)


class LowerHandler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, *_):
        pass

    def small_response(self, status: int, value):
        data = json.dumps(value).encode()
        self.send_response(status); self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data))); self.send_header("Connection", "close")
        self.end_headers(); self.wfile.write(data); self.wfile.flush(); self.close_connection = True

    def do_GET(self):
        if self.path == "/healthz":
            return self.small_response(200, {"ok": True, "protocol": self.server.state.snapshot()["protocol"]})
        if self.path == "/stats":
            if self.headers.get("Authorization") != "Bearer " + STATS_TOKEN:
                return self.small_response(401, {"error": "stats_auth_required"})
            return self.small_response(200, self.server.state.snapshot())
        self.small_response(404, {"error": "route_not_exposed"})

    def do_POST(self):
        if self.path != "/v1/responses":
            return self.small_response(404, {"error": "responses_only"})
        if self.headers.get("Authorization") != "Bearer " + PLACEHOLDER:
            return self.small_response(401, {"error": "placeholder_required"})
        started = time.monotonic()
        try:
            supplied = float(self.headers.get(LOWER_DEADLINE_HEADER, "nan"))
            if not math.isfinite(supplied) or supplied <= started:
                raise ValueError("missing or expired absolute case deadline")
            deadline = min(supplied, started + self.server.timeout)
            lengths = self.headers.get_all("Content-Length", [])
            if len(lengths) != 1 or self.headers.get("Transfer-Encoding"):
                raise ValueError("one Content-Length required")
            length = int(lengths[0])
            if length <= 0:
                raise ValueError("empty request")
            self.connection.settimeout(max(.001, deadline - time.monotonic()))
            raw = self.rfile.read(length)
            if len(raw) != length:
                raise ValueError("truncated request")
        except (ValueError, OSError, TimeoutError):
            self.server.state.rejected(b'', 'lower_protocol_or_transport', 'invalid_request_framing_or_case_deadline')
            return self.small_response(400, {"error": "invalid_request_or_case_deadline", "upstream_attempts": 0})
        try:
            body = json.loads(raw)
            if not isinstance(body, dict):
                raise ValueError("object required")
            downstream_stream = body.get("stream", False)
            if type(downstream_stream) is not bool:
                raise ValueError("stream must be boolean")
            body["stream"] = True
            body["model"] = MODEL; body["reasoning"] = {"effort": LOWER_EFFORT}
            body.pop("max_output_tokens", None)  # retain the original lower payload policy
            encoded = json.dumps(body, ensure_ascii=False)
        except (ValueError, UnicodeError):
            # Complete malformed JSON/object bytes came from the caller and
            # were not changed by the fixed relay. This is not a provider outage.
            self.server.state.rejected(raw, 'client', 'malformed_candidate_request_object')
            return self.small_response(400, {"error": "malformed_candidate_request_object", "upstream_attempts": 0})
        try:
            identity = self.server.request_reserve.reserve(self.headers.get(CONTEXT_HEADER), encoded.encode(),
                legacy_bytes=normalized_payload(body, stream=False))
        except ReplayForbidden as exc:
            return self.small_response(409, {"error": str(exc), "replay_allowed": False, "upstream_attempts": 0})
        except (ValueError, TypeError, KeyError):
            return self.small_response(403, {"error": "trusted_lower_context_required", "replay_allowed": False, "upstream_attempts": 0})
        event = self.server.state.begin(encoded.encode(), deadline, identity)
        self.server.state.update(event, payload_bytes=len(encoded.encode()),
            upstream_stream=True, downstream_stream=downstream_stream,
            started_monotonic=started, budget_seconds=deadline-started)
        response_dir = self.server.state.path.parent / (self.server.state.path.stem + "-responses")
        response_dir.mkdir(exist_ok=True)
        response_path = response_dir / (event["request_id"] + ".body")
        process = None; writer = None; headers_sent = False; terminal = None; upstream = {}
        pending = b""; reason = None; response_sha = hashlib.sha256(); response_bytes = 0
        normalized_response = None; headers_after_seconds = None; first_body_after_seconds = None
        writer_done = threading.Event()
        def write_input():
            try:
                message = {"endpoint": self.server.endpoint, "key": self.server.key, "body": encoded,
                           "deadline": deadline, "parent_pid": os.getpid(),
                           "downstream_stream": downstream_stream}
                process.stdin.write(json.dumps(message).encode()); process.stdin.close()
            except (OSError, ValueError):
                pass
            finally:
                writer_done.set()
        def consume(data: bytes, *, deliver=True):
            nonlocal pending, headers_sent, terminal, upstream, reason, response_bytes
            nonlocal normalized_response, headers_after_seconds, first_body_after_seconds
            pending += data
            while len(pending) >= 5:
                size = struct.unpack("!I", pending[1:5])[0]
                if len(pending) < size + 5:
                    break
                kind, payload = pending[:1], pending[5:5+size]
                pending = pending[5+size:]
                if kind == b"A":
                    self.server.state.update(event, upstream_attempts=1)
                elif kind == b"H":
                    upstream = json.loads(payload)
                    headers_after_seconds = time.monotonic() - started
                    if deliver and (downstream_stream or upstream["status"] != 200):
                        self.send_response(int(upstream["status"]))
                        self.send_header("Content-Type", upstream["content_type"])
                        if upstream.get("retry_after"):
                            self.send_header("Retry-After", upstream["retry_after"])
                        self.send_header("Connection", "close"); self.end_headers(); self.wfile.flush()
                        headers_sent = True
                elif kind == b"D":
                    evidence_file.write(payload); response_sha.update(payload); response_bytes += len(payload)
                    if first_body_after_seconds is None:
                        first_body_after_seconds = time.monotonic() - started
                    if deliver and (downstream_stream or upstream.get("status") != 200):
                        self.wfile.write(payload); self.wfile.flush()
                elif kind == b"J":
                    normalized_response = json.loads(payload)
                elif kind == b"T":
                    terminal = json.loads(payload)
                elif kind == b"E":
                    reason = "worker_" + json.loads(payload)["error_type"]
                else:
                    raise ValueError("unknown worker frame")
        try:
            with response_path.open("xb") as evidence_file:
                try:
                    process = self.server.spawn_worker()
                    self.server.state.update(event, worker_pid=process.pid)
                    writer = threading.Thread(target=write_input, daemon=True); writer.start()
                    os.set_blocking(process.stdout.fileno(), False)
                    while True:
                        if time.monotonic() >= deadline:
                            reason = "absolute_case_deadline"; break
                        if self.server.stopping.is_set():
                            reason = "broker_stopping"; break
                        self.connection.settimeout(max(.001, deadline - time.monotonic()))
                        readable, _, _ = select.select([process.stdout, self.connection], [], [], max(0, min(.05, deadline-time.monotonic())))
                        if process.stdout in readable:
                            chunk = os.read(process.stdout.fileno(), 65536)
                            if not chunk:
                                break
                            consume(chunk)
                        if self.connection in readable and not self.connection.recv(1, socket.MSG_PEEK):
                            reason = "downstream_disconnected"; break
                except Exception as exc:
                    # D52: the connection timeout and the select() budget on the two
                    # lines above are armed from THIS deadline, so a read that fails
                    # once the deadline has passed is the evaluator ending the case,
                    # not an upstream fault.  Only this per-row reason changes:
                    # failure_kind stays "provider_or_transport" and every runtime
                    # counter keeps its value, so agentloop/lower_transport.py:26-32
                    # snapshot_valid() and the shared admission normalizers still see
                    # the ledger shape they already accept.
                    reason = ("absolute_case_deadline" if time.monotonic() >= deadline
                              else "local_transport_" + type(exc).__name__)
                finally:
                    if process is not None:
                        if process.poll() is None:
                            process.kill()
                        process.wait(timeout=2)
                        if writer:
                            writer.join(timeout=1)
                        # Preserve terminal evidence already written by the
                        # worker even when client disconnect raced delivery.
                        while True:
                            try:
                                rest = os.read(process.stdout.fileno(), 65536)
                            except BlockingIOError:
                                break
                            if not rest:
                                break
                            consume(rest, deliver=False)
                        process.stdout.close()
                        with self.server.worker_lock:
                            self.server.workers.discard(process)
        finally:
            metadata = terminal or {"transport_complete": False, "terminal_observed": False, "usage_unknown": True}
            available = bool(upstream.get("status") == 200 and metadata.get("transport_complete")
                and metadata.get("terminal_observed") and metadata.get("response_status") in {"completed", "incomplete"}
                and metadata.get("model_identity_valid")
                and not metadata.get("response_error_present"))
            self.server.state.update(event, state="terminal", elapsed_seconds=time.monotonic()-started,
                worker_reaped=process is None or process.poll() is not None,
                worker_exit_code=process.returncode if process else None, upstream_http_status=upstream.get("status"),
                transport_abort_reason=reason, model_response_available=available,
                provider_outcome="observed_terminal" if metadata.get("terminal_observed") else "unknown",
                failure_kind=None if available else "provider_or_transport",
                provider_response_path=str(response_path), provider_response_sha256=response_sha.hexdigest(),
                response_bytes=response_bytes, upstream_content_type=upstream.get("content_type"),
                response_headers_after_seconds=headers_after_seconds,
                first_response_body_after_seconds=first_body_after_seconds,
                response_phase=("terminal" if metadata.get("terminal_observed") else
                    "body" if response_bytes else "headers" if upstream else
                    "post_sent" if event["upstream_attempts"] else "connection_setup"), **metadata)
            self.close_connection = True
        if not headers_sent and available and normalized_response is not None:
            try:
                self.small_response(200, normalized_response)
                headers_sent = True
            except OSError:
                pass
        if not headers_sent:
            try:
                self.small_response(598, {"error": "upstream_delivery_unknown", "upstream_attempts": event["upstream_attempts"]})
            except OSError:
                pass


def run_lower_server(args):
    if args.stats_output is None:
        raise ValueError("lower broker requires --stats-output outside Candidate")
    server = LowerServer((args.bind, args.port), endpoint=args.upstream, key=args.upstream_key,
                         stats_path=args.stats_output, context_key=private_file(args.context_key_file),
                         history_seed=args.history_seed, history_seed_sha256=args.history_seed_sha256)
    def stop(_signum, _frame):
        raise SystemExit(128 + _signum)
    signal.signal(signal.SIGTERM, stop); signal.signal(signal.SIGINT, stop)
    try:
        server.serve_forever()
    finally:
        server.stop_owned_workers(); server.server_close(); server.state.persist()
    return 0


def main() -> int:
    p=argparse.ArgumentParser()
    p.add_argument("--bind", default="127.0.0.1")
    p.add_argument("--port", type=int, required=True)
    p.add_argument("--upstream", default="https://api.deepseek.com/v1/responses")
    p.add_argument("--credential-file", type=Path, required=True)
    p.add_argument("--stats-output", type=Path, required=True)
    p.add_argument("--context-key-file", type=Path, required=True)
    p.add_argument("--history-seed-sha256")
    p.add_argument("--history-seed", type=Path)
    args=p.parse_args()
    credentials = dotenv(args.credential_file)
    # Keep the evaluated endpoint fixed.  The shared credential file may
    # contain legacy endpoint variables for unrelated runs; allowing those to
    # override this broker made otherwise-valid GATEWAY credentials return 403.
    args.upstream = args.upstream or "https://api.deepseek.com/v1/responses"
    args.upstream_key = credentials.get("DEEPSEEK_API_KEY", "") or credentials.get("AGENTSWE_UPSTREAM_API_KEY", "")
    if not args.upstream_key:
        raise SystemExit("evaluator broker credential is missing")
    return run_lower_server(args)
if __name__ == "__main__":
    if sys.argv[1:] == ["--lower-worker"]:
        lower_worker()
    else:
        raise SystemExit(main())
