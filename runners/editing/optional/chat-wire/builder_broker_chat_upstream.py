#!/usr/bin/env python3
"""Evaluator-owned loopback broker: codex Responses in, DeepInfra Chat out.

codex CLI 0.144.1 refuses ``wire_api="chat"`` in a provider config and only
speaks the OpenAI *Responses* API.  DeepInfra serves only *Chat Completions*
(``https://api.deepinfra.com/v1/openai/chat/completions``; its ``/responses``
route is 404).  This process sits on loopback, is configured into codex as an
ordinary Responses provider, and translates both directions live.

It keeps the ledger, statistics, redaction, health and model/effort lock of
``builder_broker_xhigh.py`` so the readiness/formal evidence code that reads a
Builder broker ledger keeps working unchanged.  Differences from that broker are
listed in README.md section "与 builder_broker_xhigh 的差异".

The provider credential is read here and never returned through health, stats,
logs, the ledger or the Builder environment.  Runtime clients authenticate with
the fixed ``broker-only-placeholder`` token.
"""
from __future__ import annotations

import argparse
import hashlib
import http.server
import json
import os
import queue
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

from builder_request_ledger import RequestLedger
from responses_stream import direct_opener, strict_json, RedactedCapture, StreamProtocolError
import chat_translate

PLACEHOLDER = "broker-only-placeholder"
STATS_TOKEN = "stats-only-placeholder"
MAX_BODY = 32 * 1024 * 1024
SCHEMA = "agentswe-builder-broker-stats/v2"
PROTOCOL = "agentswe-builder-single-upstream/v1"
ERROR_SAMPLE_BYTES = 32768


def provider_key(path: Path, variable: str) -> str:
    """Read exactly the named credential; never fall back to another key."""
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        if key.strip() == variable:
            value = value.strip().strip("'\"")
            if value:
                return value
    raise RuntimeError("credential file has no %s" % variable)


# ---------------------------------------------------------------------------
# statistics, shaped exactly like builder_broker_xhigh.State
# ---------------------------------------------------------------------------
class State:
    def __init__(self, stats_path: Path | None, *, model: str, effort: str | None,
                 upstream_wire: str = "chat") -> None:
        self.lock = threading.Lock()
        self.stats_path = stats_path
        self.model = model
        self.effort = effort
        self.upstream_wire = upstream_wire
        self.calls = 0
        self.completed_calls = 0
        self.successful_calls = 0
        self.failures = 0
        self.provider_failures = 0
        self.protocol_failures = 0
        self.delivery_failures = 0
        self.input_tokens = 0
        self.output_tokens = 0
        self.total_tokens = 0
        self.cached_input_tokens = 0
        self.reasoning_output_tokens = 0
        self.estimated_cost = 0.0
        self.unknown_cost_requests = 0
        self.degenerate_responses = 0
        self.upstream_status_counts: dict[str, int] = {}
        self.actual_upstream_requests = 0
        self.unknown_usage_requests = 0
        self.cache_queries = 0
        self.in_flight_upstream_requests = 0
        if stats_path and stats_path.exists():
            old = strict_json(stats_path.read_bytes())
            if old.get("schema_version") != SCHEMA:
                raise ValueError("legacy Builder state requires explicit audit; refusing to reset it")
            runtime = old["runtime"]
            for name in ("calls", "completed_calls", "successful_calls", "failures",
                         "provider_failures", "protocol_failures", "delivery_failures",
                         "input_tokens", "output_tokens", "total_tokens",
                         "actual_upstream_requests", "unknown_usage_requests", "cache_queries",
                         "in_flight_upstream_requests"):
                setattr(self, name, runtime.get("known_" + name, runtime.get(name, 0)) or 0)
            self.estimated_cost = float(runtime.get("estimated_cost_usd", 0.0) or 0.0)
            self.cached_input_tokens = runtime.get("known_cached_input_tokens", 0) or 0
            self.reasoning_output_tokens = runtime.get("known_reasoning_output_tokens", 0) or 0
            self.upstream_status_counts = old["upstream"]["status_counts"]
        with self.lock:
            self._persist_locked()

    def _value_locked(self) -> dict[str, Any]:
        usage_complete = self.unknown_usage_requests + self.in_flight_upstream_requests == 0
        return {
            "schema_version": SCHEMA,
            "protocol": {"model": self.model, "reasoning_effort": self.effort,
                         "max_upstream_attempts_per_identity": 1,
                         "unknown_requests_replayed": False,
                         # -- fields added by package 114 --------------------
                         "upstream_wire": self.upstream_wire,
                         "client_wire": "responses",
                         "adapter": "builder_broker_chat_upstream/v1"},
            "runtime": {
                "calls": self.calls, "failures": self.failures,
                "actual_upstream_requests": self.actual_upstream_requests,
                "unknown_usage_requests": self.unknown_usage_requests,
                "cache_queries": self.cache_queries,
                "usage_complete": usage_complete,
                "in_flight_upstream_requests": self.in_flight_upstream_requests,
                "known_total_tokens": self.total_tokens,
                "known_input_tokens": self.input_tokens,
                "known_output_tokens": self.output_tokens,
                "completed_calls": self.completed_calls,
                "successful_calls": self.successful_calls,
                "in_flight_calls": max(0, self.calls - self.completed_calls),
                "tokens": self.total_tokens if usage_complete else None,
                "provider_failures": self.provider_failures,
                "protocol_failures": self.protocol_failures,
                "delivery_failures": self.delivery_failures,
                "input_tokens": self.input_tokens if usage_complete else None,
                "output_tokens": self.output_tokens if usage_complete else None,
                "total_tokens": self.total_tokens if usage_complete else None,
                # -- fields added by package 114 ------------------------------
                "known_cached_input_tokens": self.cached_input_tokens,
                "known_reasoning_output_tokens": self.reasoning_output_tokens,
                "estimated_cost_usd": round(self.estimated_cost, 10),
                "estimated_cost_complete": self.unknown_cost_requests == 0 and usage_complete,
                "unknown_cost_requests": self.unknown_cost_requests,
                "degenerate_responses": self.degenerate_responses,
            },
            "upstream": {"status_counts": dict(sorted(self.upstream_status_counts.items()))},
            "credential": {"builder_visible": PLACEHOLDER, "provider_secret_logged": False,
                           "credential_value_recorded": False},
        }

    def _persist_locked(self) -> None:
        if self.stats_path is None:
            return
        self.stats_path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.stats_path.with_suffix(self.stats_path.suffix + ".tmp")
        temporary.write_text(json.dumps(self._value_locked(), indent=2, ensure_ascii=False) + "\n",
                             encoding="utf-8")
        os.replace(temporary, self.stats_path)

    def reserve(self) -> None:
        with self.lock:
            self.calls += 1
            self._persist_locked()

    def finish(self, *, failure: str | None, status: int | None = None,
               usage: dict[str, Any] | None = None, request_sent: bool = False) -> None:
        with self.lock:
            self.completed_calls += 1
            if failure:
                self.failures += 1
                if failure == "provider":
                    self.provider_failures += 1
                elif failure == "protocol":
                    self.protocol_failures += 1
                else:
                    self.delivery_failures += 1
            else:
                self.successful_calls += 1
            if status is not None:
                key = str(status)
                self.upstream_status_counts[key] = self.upstream_status_counts.get(key, 0) + 1
            if request_sent:
                self.in_flight_upstream_requests = max(0, self.in_flight_upstream_requests - 1)
            known = isinstance(usage, dict) and all(
                type(usage.get(k)) is int and usage[k] >= 0
                for k in ("input_tokens", "output_tokens", "total_tokens"))
            if request_sent and not known:
                self.unknown_usage_requests += 1
            usage = usage or {}
            self.input_tokens += int(usage.get("input_tokens", 0) or 0)
            self.output_tokens += int(usage.get("output_tokens", 0) or 0)
            self.total_tokens += int(usage.get("total_tokens", 0) or 0)
            self.cached_input_tokens += int((usage.get("input_tokens_details") or {}).get("cached_tokens", 0) or 0)
            self.reasoning_output_tokens += int((usage.get("output_tokens_details") or {}).get("reasoning_tokens", 0) or 0)
            if request_sent:
                cost = usage.get("estimated_cost")
                if isinstance(cost, (int, float)):
                    self.estimated_cost += float(cost)
                else:
                    self.unknown_cost_requests += 1
            self._persist_locked()

    def record_upstream_start(self) -> None:
        with self.lock:
            self.actual_upstream_requests += 1
            self.in_flight_upstream_requests += 1
            self._persist_locked()

    def record_degenerate(self) -> None:
        with self.lock:
            self.degenerate_responses += 1
            self._persist_locked()

    def record_cache_query(self) -> None:
        with self.lock:
            self.cache_queries += 1
            self._persist_locked()

    def record_delivery_failure(self) -> None:
        with self.lock:
            self.delivery_failures += 1
            self.failures += 1
            self._persist_locked()

    def reconcile(self, ledger) -> None:
        summary = ledger.recover()
        cost, unknown_cost = ledger_cost(Path(ledger.root))
        with self.lock:
            self.calls = summary["calls"] + self.protocol_failures
            self.completed_calls = self.calls
            self.successful_calls = summary["completed"]
            self.provider_failures = summary["failed"]
            self.failures = self.provider_failures + self.protocol_failures + self.delivery_failures
            self.actual_upstream_requests = summary["sent"]
            self.unknown_usage_requests = summary["unknown_usage"]
            self.in_flight_upstream_requests = 0
            for name in ("input_tokens", "output_tokens", "total_tokens"):
                setattr(self, name, summary[name])
            self.estimated_cost = cost
            self.unknown_cost_requests = unknown_cost
            self._persist_locked()

    def stats(self) -> dict[str, Any]:
        with self.lock:
            return self._value_locked()


def ledger_cost(root: Path) -> tuple[float, int]:
    """Rebuild estimated_cost from durable receipts (package 114 addition)."""
    total = 0.0
    unknown = 0
    if not root.is_dir():
        return 0.0, 0
    for path in sorted(root.iterdir()):
        receipt = path / "completed.json"
        if not receipt.is_file():
            continue
        usage = (strict_json(receipt.read_bytes()) or {}).get("usage") or {}
        cost = usage.get("estimated_cost")
        if isinstance(cost, (int, float)):
            total += float(cost)
        else:
            unknown += 1
    return total, unknown


# ---------------------------------------------------------------------------
# chat SSE decoding
# ---------------------------------------------------------------------------
class ChatChunks:
    """Minimal SSE decoder for OpenAI chat.completion.chunk streams."""

    def __init__(self) -> None:
        self.pending = bytearray()
        self.done = False

    def feed(self, chunk: bytes):
        self.pending.extend(chunk)
        while True:
            index = self.pending.find(b"\n")
            if index < 0:
                break
            line = bytes(self.pending[:index]).rstrip(b"\r")
            del self.pending[:index + 1]
            if not line or line.startswith(b":"):
                continue
            if not line.startswith(b"data:"):
                continue
            payload = line[5:].strip()
            if payload == b"[DONE]":
                self.done = True
                continue
            try:
                yield strict_json(payload)
            except (ValueError, StreamProtocolError) as exc:
                raise StreamProtocolError("invalid chat chunk") from exc


def apply_chunk(emitter: chat_translate.ResponsesEmitter, chunk: dict) -> tuple[bytes, dict | None, str | None]:
    """One chat chunk -> Responses SSE bytes, usage (if present), finish_reason."""
    out = b""
    usage = chat_translate.map_usage(chunk.get("usage"))
    finish = None
    for choice in chunk.get("choices") or []:
        if not isinstance(choice, dict):
            continue
        delta = choice.get("delta") or {}
        if isinstance(delta, dict):
            reasoning = delta.get("reasoning_content") or delta.get("reasoning")
            if isinstance(reasoning, str) and reasoning:
                out += emitter.reasoning_delta(reasoning)
            content = delta.get("content")
            if isinstance(content, str) and content:
                out += emitter.text_delta(content)
            for call in delta.get("tool_calls") or []:
                if not isinstance(call, dict):
                    continue
                function = call.get("function") or {}
                out += emitter.tool_call_delta(
                    int(call.get("index") or 0), call.get("id"),
                    function.get("name") if isinstance(function, dict) else None,
                    function.get("arguments") if isinstance(function, dict) else None)
        if choice.get("finish_reason"):
            finish = choice["finish_reason"]
    return out, usage, finish


# ---------------------------------------------------------------------------
# fault injection (verification only; never used on a scored run)
# ---------------------------------------------------------------------------
def take_fault(path: Path | None) -> dict | None:
    if path is None or not path.is_file():
        return None
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except ValueError:
        return None
    if not isinstance(value, dict) or int(value.get("remaining", 0)) <= 0:
        return None
    value["remaining"] = int(value["remaining"]) - 1
    path.write_text(json.dumps(value) + "\n", encoding="utf-8")
    return value


# ---------------------------------------------------------------------------
# HTTP handler
# ---------------------------------------------------------------------------
class Handler(http.server.BaseHTTPRequestHandler):
    server_version = "agentswe-chat-upstream-builder-broker/1"
    protocol_version = "HTTP/1.1"

    @property
    def state(self) -> State:
        return self.server.state  # type: ignore[attr-defined]

    def log_message(self, _format: str, *_args: object) -> None:
        return

    def send_json(self, status: int, value: object, *, retry_after: float | None = None) -> None:
        payload = json.dumps(value, ensure_ascii=False).encode("utf-8")
        try:
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            if retry_after is not None:
                # codex 0.144.1 retries 5xx on its own, but a 429 without a
                # usable Retry-After is fatal for it ("exceeded retry limit,
                # last status: 429 Too Many Requests", measured
                # with codex 0.144.1).  DeepInfra rate limits with a bare 429, so the
                # broker supplies the header the client needs.
                self.send_header("Retry-After", str(int(max(1, round(retry_after)))))
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)
        except OSError:
            pass

    def do_GET(self) -> None:  # noqa: N802
        options = self.server.options  # type: ignore[attr-defined]
        if self.path == "/healthz":
            self.send_json(200, {"ok": True, "status": "ok", "model": options.model,
                                 "reasoning_effort": options.effort,
                                 "upstream_wire": "chat", "client_wire": "responses",
                                 "protocol": PROTOCOL})
        elif self.path == "/stats":
            if self.headers.get("Authorization") != "Bearer %s" % STATS_TOKEN:
                self.send_json(401, {"error": "stats auth"})
            else:
                self.send_json(200, self.state.stats())
        else:
            self.send_json(404, {"error": "not found"})

    # -- streaming helpers -------------------------------------------------
    def _begin_stream(self) -> None:
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream; charset=utf-8")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Transfer-Encoding", "chunked")
        self.end_headers()

    def _write(self, data: bytes) -> None:
        if not data:
            return
        self.wfile.write(b"%x\r\n" % len(data) + data + b"\r\n")
        self.wfile.flush()

    def _end_stream(self) -> None:
        self.wfile.write(b"0\r\n\r\n")
        self.wfile.flush()

    def do_POST(self) -> None:  # noqa: N802
        options = self.server.options  # type: ignore[attr-defined]
        path = self.path.rstrip("/")
        if path not in ("/v1/responses", "/v1/chat/completions"):
            self.send_json(404, {"error": "not found"})
            return
        if self.headers.get("Authorization") != "Bearer %s" % PLACEHOLDER:
            self.send_json(401, {"error": "Builder must use placeholder credential"})
            return
        try:
            length = int(self.headers.get("Content-Length", "0"))
            if length <= 0 or length > MAX_BODY:
                raise ValueError("invalid request length")
            body = strict_json(self.rfile.read(length))
            if not isinstance(body, dict):
                raise ValueError("request is not an object")
            if path == "/v1/chat/completions":
                # Accepted for parity with builder_broker_xhigh; codex never
                # uses it (it refuses wire_api="chat"), and a chat request is
                # already in the upstream shape apart from model/effort.
                translated = {"body": dict(body, model=options.model, stream=True,
                                           stream_options={"include_usage": True}),
                              "receipt": {"passthrough_chat": True}, "custom_tool_names": []}
                if options.effort:
                    translated["body"]["reasoning_effort"] = options.effort
                translated["body"].pop("max_output_tokens", None)
            else:
                translated = chat_translate.responses_to_chat(
                    body, model=options.model, effort=options.effort,
                    effort_policy=options.effort_policy, reasoning_mode=options.reasoning_mode)
        except (ValueError, KeyError, chat_translate.TranslationError, json.JSONDecodeError) as exc:
            self.state.reserve()
            self.state.finish(failure="protocol")
            self.send_json(400, {"error": "broker_protocol_failure", "type": type(exc).__name__,
                                 "detail": str(exc)[:300]})
            return

        ledger = self.server.ledger  # type: ignore[attr-defined]
        route = path
        identity = cached = error = None
        for attempt in range(options.max_attempts):
            try:
                identity, cached, error = ledger.claim(
                    translated["body"], route if attempt == 0 else "%s#attempt=%d" % (route, attempt),
                    options.provider_url)
            except (ValueError, OSError) as exc:
                self.send_json(409, {"error": "builder_request_identity_failure",
                                     "type": type(exc).__name__})
                return
            if cached or not error:
                break
            if options.replay_policy != "resend":
                break
        if error or cached:
            self.state.record_cache_query()
            if error:
                self.send_json(409, {"error": error, "automatic_retry_allowed": False})
                return
            try:
                self.send_response(cached["status"])
                self.send_header("Content-Type", cached["content_type"])
                self.send_header("Content-Length", str(len(cached["payload"])))
                self.end_headers()
                self.wfile.write(cached["payload"])
            except OSError:
                self.state.record_delivery_failure()
            return

        self.state.reserve()
        (identity / "translation_receipt.json").write_text(
            json.dumps(translated["receipt"], indent=2, sort_keys=True) + "\n", encoding="utf-8")
        if options.dump_bodies:
            # Verification aid only (off by default): neither file can contain a
            # credential -- the Authorization header is never part of a body.
            (identity / "request_responses.json").write_text(
                json.dumps(body, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
            (identity / "request_chat.json").write_text(
                json.dumps(translated["body"], indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
        self._exchange(options, ledger, identity, translated, body)

    # -- the upstream exchange --------------------------------------------
    def _exchange(self, options, ledger, identity: Path, translated: dict, body: dict) -> None:
        fault = take_fault(options.fault_file)
        started = time.monotonic()
        request = urllib.request.Request(
            options.provider_url,
            data=json.dumps(translated["body"], ensure_ascii=False).encode("utf-8"),
            headers={"Authorization": "Bearer %s" % self.server.provider_key,  # type: ignore[attr-defined]
                     "Content-Type": "application/json",
                     "Accept": "text/event-stream"},
            method="POST")
        sent = {"value": False}

        def on_start():
            ledger.sent(identity)
            sent["value"] = True
            self.state.record_upstream_start()

        if fault and fault.get("mode") == "http_error":
            # Injected before any upstream socket: nothing was sent.
            status = int(fault.get("status", 503))
            self._fail_before_stream(ledger, identity, options, status,
                                     json.dumps({"error": {"message": fault.get(
                                         "body", "injected upstream fault"),
                                         "type": "injected_fault"}}).encode(), sent=False)
            return

        response = None
        try:
            response = direct_opener(on_request_start=on_start).open(
                request, timeout=options.max_call_seconds)
        except urllib.error.HTTPError as exc:
            sample = b""
            try:
                sample = exc.read(ERROR_SAMPLE_BYTES)
            except Exception:
                pass
            finally:
                try:
                    exc.close()
                except Exception:
                    pass
            self._fail_before_stream(ledger, identity, options, exc.code, sample,
                                     sent=sent["value"])
            return
        except Exception as exc:
            self._fail_before_stream(ledger, identity, options, None,
                                     ("%s: %s" % (type(exc).__name__, exc)).encode()[:2000],
                                     sent=sent["value"])
            return

        chunks: "queue.Queue[object]" = queue.Queue()
        capture_file = (identity / "upstream.raw").open("xb")
        capture = RedactedCapture(capture_file, self.server.provider_key)  # type: ignore[attr-defined]

        def reader():
            try:
                while True:
                    block = response.read1(65536)
                    if not block:
                        break
                    capture.write(block)
                    chunks.put(block)
            except Exception as exc:  # pragma: no cover - socket teardown
                chunks.put(exc)
            finally:
                try:
                    capture.finish()
                    capture_file.close()
                except Exception:
                    pass
                chunks.put(None)

        threading.Thread(target=reader, daemon=True).start()

        response_id = "resp_" + hashlib.sha256(identity.name.encode()).hexdigest()[:24]
        emitter = chat_translate.ResponsesEmitter(
            response_id, options.model, int(time.time()),
            custom_tool_names=translated["custom_tool_names"],
            emit_reasoning=options.reasoning_summary == "emit")
        decoder = ChatChunks()
        produced = bytearray()
        connected = True

        def emit(data: bytes) -> None:
            nonlocal connected
            if not data or not connected:
                return
            produced.extend(data)
            try:
                self._write(data)
            except OSError:
                connected = False

        self._begin_stream()
        emit(emitter.start())

        usage = None
        finish_reason = None
        failure: str | None = None
        last_byte = time.monotonic()
        events = 0
        try:
            while True:
                if time.monotonic() - started > options.max_stream_seconds:
                    failure = "builder_response_deadline"
                    break
                try:
                    block = chunks.get(timeout=options.keepalive_seconds)
                except queue.Empty:
                    if time.monotonic() - last_byte > options.idle_timeout_seconds:
                        failure = "upstream_idle_timeout"
                        break
                    emit(emitter.keepalive())
                    continue
                if block is None:
                    break
                if isinstance(block, Exception):
                    failure = "upstream_read_%s" % type(block).__name__
                    break
                last_byte = time.monotonic()
                for chunk in decoder.feed(block):
                    data, chunk_usage, chunk_finish = apply_chunk(emitter, chunk)
                    events += 1
                    if chunk_usage:
                        usage = chunk_usage
                    if chunk_finish:
                        finish_reason = chunk_finish
                    emit(data)
                    if fault and fault.get("mode") == "cut" and events >= int(fault.get("after_events", 3)):
                        failure = "injected_stream_cut"
                        break
                if failure:
                    break
        except StreamProtocolError as exc:
            failure = "upstream_protocol_%s" % type(exc).__name__
        finally:
            try:
                response.close()
            except Exception:
                pass

        if failure == "injected_stream_cut":
            # Half-written stream, no terminal event: exactly what codex's
            # reconnect ladder is built for.
            ledger.fail(identity, error=failure, status=200, sent=sent["value"])
            self.state.finish(failure="provider", status=200, request_sent=sent["value"])
            # No terminating chunk and no keep-alive: the client sees a chunked
            # body that ends mid-stream, which is what a dropped provider
            # stream looks like and what codex's reconnect ladder is for.
            self.close_connection = True
            try:
                self.wfile.flush()
            except OSError:
                pass
            return
        if failure:
            emit(emitter.failed("upstream_stream_failure", failure, status=200))
            self._finish_stream(connected)
            ledger.fail(identity, error=failure, status=200, sent=sent["value"])
            self.state.finish(failure="provider", status=200, request_sent=sent["value"])
            return

        degenerate = chat_translate.degenerate_reasoning_only(
            finish_reason=finish_reason, tool_calls=emitter.tool_calls_seen,
            content=emitter.message_text, reasoning=emitter.reasoning_text,
            policy=options.degenerate_response_policy)
        if degenerate:
            # The provider produced no assistant content and no tool call, only
            # a reasoning fragment terminated by a raw chat-template token.  The
            # turn cannot advance, so this is a failed response, not a finished
            # one: codex's reconnect ladder re-sends the identical request.  No
            # content is invented and the upstream usage is still recorded.
            self.state.record_degenerate()
            emit(emitter.failed("degenerate_reasoning_only_response", degenerate, status=200))
            self._finish_stream(connected)
            ledger.fail(identity, error="degenerate_reasoning_only_response:" + degenerate,
                        status=200, sent=sent["value"])
            self.state.finish(failure="provider", status=200, usage=usage,
                              request_sent=sent["value"])
            return
        incomplete = "max_output_tokens" if finish_reason == "length" else None
        emit(emitter.complete(usage, incomplete_reason=incomplete))
        self._finish_stream(connected)
        payload = bytes(produced)
        ledger.complete(identity, payload=payload, status=200,
                        content_type="text/event-stream", usage=usage)
        if connected:
            self.state.finish(failure=None, status=200, usage=usage, request_sent=sent["value"])
        else:
            self.state.finish(failure="delivery", status=200, usage=usage, request_sent=sent["value"])

    def _finish_stream(self, connected: bool) -> None:
        if not connected:
            return
        try:
            self._end_stream()
        except OSError:
            pass

    def _fail_before_stream(self, ledger, identity: Path, options, status: int | None,
                            sample: bytes, *, sent: bool) -> None:
        """Nothing was streamed yet: answer with a shape codex can retry."""
        redacted = sample.replace(self.server.provider_key.encode(), b"[REDACTED]")  # type: ignore[attr-defined]
        try:
            (identity / "upstream_error_sample.redacted").open("xb").write(redacted[:ERROR_SAMPLE_BYTES])
        except OSError:
            pass
        ledger.fail(identity, error="upstream_status_%s" % status, status=status, sent=sent)
        self.state.finish(failure="provider", status=status, request_sent=sent)
        message = redacted[:2000].decode("utf-8", "replace")
        if options.upstream_error_mode == "http" and status is not None:
            client_status = status
            retry_after = None
            if status == 429:
                if options.rate_limit_policy == "as_503":
                    client_status = 503
                elif options.rate_limit_policy == "retry_after":
                    retry_after = options.retry_after_seconds
            self.send_json(client_status, {"error": {
                "message": message, "type": "upstream_error", "upstream_status": status,
                "client_status": client_status}}, retry_after=retry_after)
            return
        emitter = chat_translate.ResponsesEmitter("resp_failed", options.model, int(time.time()))
        self._begin_stream()
        try:
            self._write(emitter.start())
            self._write(emitter.failed("upstream_error", message, status=status))
            self._end_stream()
        except OSError:
            self.state.record_delivery_failure()


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--credential-file", type=Path, required=True)
    parser.add_argument("--credential-var", default="DEEPINFRA_API_KEY")
    parser.add_argument("--provider-url",
                        default=os.environ.get("AGENTSWE_PROVIDER_URL",
                                               "https://api.deepinfra.com/v1/openai/chat/completions"))
    parser.add_argument("--model", required=True)
    parser.add_argument("--effort", default="",
                        help="reasoning_effort sent upstream; empty sends none")
    parser.add_argument("--effort-policy", choices=("lock", "passthrough"), default="lock")
    parser.add_argument("--reasoning-mode", choices=("drop", "carry"), default="drop",
                        help="what to do with incoming Responses reasoning items")
    parser.add_argument("--reasoning-summary", choices=("emit", "drop"), default="emit",
                        help="whether reasoning_content becomes a Responses reasoning item")
    parser.add_argument("--upstream-error-mode", choices=("http", "sse"), default="http")
    parser.add_argument("--degenerate-response-policy",
                        choices=("off", "end-token", "any"), default="end-token",
                        help="what to do with a reasoning-only stop that carries no "
                             "content and no tool call.  end-token (default) fails "
                             "only the measured DeepInfra/Qwen signature - a raw "
                             "chat-template end token in the reasoning tail - so the "
                             "client retries; off restores the pre-115b behaviour.")
    parser.add_argument("--rate-limit-policy",
                        choices=("passthrough", "retry_after", "as_503"), default="as_503",
                        help="how an upstream 429 is presented to codex.  Measured "
                             "with codex 0.144.1: it dies on a bare 429 and ignores "
                             "Retry-After, but retries a 503, so as_503 is the default; the "
                             "true 429 is still what the ledger and status_counts record.")
    parser.add_argument("--retry-after-seconds", type=float, default=5.0)
    parser.add_argument("--replay-policy", choices=("refuse", "resend"), default="refuse")
    parser.add_argument("--max-attempts", type=int, default=8)
    parser.add_argument("--stats-file", type=Path, required=True)
    parser.add_argument("--fault-file", type=Path)
    parser.add_argument("--dump-bodies", action="store_true",
                        help="persist each request's Responses and Chat bodies in the ledger")
    parser.add_argument("--idle-timeout-seconds", type=float, default=900.0)
    parser.add_argument("--max-stream-seconds", type=float, default=3600.0)
    parser.add_argument("--max-call-seconds", type=float, default=3600.0)
    parser.add_argument("--keepalive-seconds", type=float, default=5.0)
    # Accepted for launcher compatibility with builder_broker_xhigh.
    parser.add_argument("--max-runtime-calls", type=int, default=0)
    parser.add_argument("--max-runtime-tokens", type=int, default=0)
    parser.add_argument("--role", default="builder")
    parser.add_argument("--reasoning-effort", default=None,
                        help="alias of --effort for the generic broker launcher")
    parser.add_argument("--upstream-wire", choices=("chat",), default="chat")
    parser.add_argument("--bind", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=18114)
    args = parser.parse_args()
    if args.reasoning_effort is not None and not args.effort:
        args.effort = args.reasoning_effort
    args.effort = args.effort or None

    server = http.server.ThreadingHTTPServer((args.bind, args.port), Handler)
    server.options = args  # type: ignore[attr-defined]
    server.ledger = RequestLedger(args.stats_file.resolve().parent / "builder_requests")  # type: ignore[attr-defined]
    server.state = State(args.stats_file.resolve(), model=args.model, effort=args.effort)  # type: ignore[attr-defined]
    server.state.reconcile(server.ledger)  # type: ignore[attr-defined]
    server.provider_key = provider_key(args.credential_file.resolve(), args.credential_var)  # type: ignore[attr-defined]
    print(json.dumps({"ready": True, "model": args.model, "reasoning_effort": args.effort,
                      "upstream_wire": "chat", "bind": args.bind, "port": args.port}), flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        return 0
    finally:
        server.server_close()
        server.ledger.close()  # type: ignore[attr-defined]
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
