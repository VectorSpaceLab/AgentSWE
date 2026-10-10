"""AgentSWE unified provider broker: one evaluator-owned process per role.

Clients (Codex for BUILDER, the evaluator's own runtime/judge brokers for RUNTIME
and JUDGE, candidates' search calls for SEARCH) speak the OpenAI Responses wire
(or Chat, or the legacy search shape) to this loopback server with a placeholder
token.  This process alone holds the provider key and talks to the configured
provider over one of two upstream wires:

  responses  passthrough: the body is forwarded with only ``model`` and
             ``reasoning.effort`` pinned; stream bytes are relayed unchanged
             (SSE comment keep-alives are inserted only at event boundaries).
  chat       translation: Responses -> Chat Completions request, Chat stream ->
             Responses SSE (streaming clients) or one Responses JSON object
             (non-streaming clients); reasoning is carried back on every
             assistant turn; ``apply_patch`` round-trips as a custom tool.

Exactly one upstream submission is made per accepted client request.  Retry
policy stays where the protocol put it: Codex's own retry ladder for the
Builder, and each evaluator broker's retry loop for runtime/judge.  That keeps
every existing accounting gate valid:

  * BUILDER: ``agentswe-builder-broker-stats/v2`` + the immutable RequestLedger
    (byte-compatible with builder_broker_xhigh / the Lite chat broker).
  * RUNTIME/JUDGE/SEARCH: ``agentswe-broker-stats/v1`` whose ``runtime`` block
    carries ``calls`` / ``successful_calls`` / ``failures`` /
    ``upstream_attempts`` with the semantics of the Editing judge broker
    (``agentswe-judge-broker-stats/v1``): one call per accepted request; a call
    is successful iff the provider answered HTTP 200 with a terminal response
    whose status is ``completed``.  ``formal_axes_shared.py`` reads exactly these
    counters (successful-call delta == judged cases; call delta == transport
    attempts), so the gate holds unchanged whether the evaluator broker talks to
    the provider directly or through this process.

No request or response body, header or key value is written to logs, stats or
the event ledger.  Provider bytes that are persisted (builder ``upstream.raw``)
go through ``RedactedCapture``.
"""
from __future__ import annotations

import hashlib
import http.server
import json
import os
import queue
import threading
import time
import urllib.error
import urllib.request
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from . import chat_translate
from .request_ledger import RequestLedger
from .responses_stream import (RedactedCapture, ResponseEvents, StreamProtocolError,
                               direct_opener, strict_json)

VERSION = "agentswe-broker/1.0"
LEGACY_PLACEHOLDERS = ("broker-only-placeholder", "runtime-only-placeholder", "judge-only-placeholder")
STATS_TOKEN = "stats-only-placeholder"
MAX_BODY = 32 * 1024 * 1024
MAX_RESPONSE = 32 * 1024 * 1024
ERROR_SAMPLE_BYTES = 32768
BUILDER_SCHEMA = "agentswe-builder-broker-stats/v2"
BROKER_SCHEMA = "agentswe-broker-stats/v1"
BUILDER_PROTOCOL = "agentswe-builder-single-upstream/v1"
SEARCH_TYPES = ("search", "news", "scholar", "images")


# ---------------------------------------------------------------------------
# options
# ---------------------------------------------------------------------------
@dataclass
class Options:
    role: str
    upstream_wire: str                     # responses | chat | serper | legacy-proxy
    provider_url: str                      # full upstream URL (search: base URL)
    model: str | None = None               # pinned upstream model (None: client's)
    effort: str | None = None              # pinned effort (None: send none when locked)
    effort_policy: str = "lock"            # lock | passthrough
    model_policy: str = "lock"             # lock | passthrough
    reasoning_mode: str = "carry"          # chat wire: carry | drop
    reasoning_summary: str = "emit"        # chat wire: emit | drop
    default_max_tokens: int = chat_translate.DEFAULT_MAX_TOKENS
    degenerate_response_policy: str = "end-token"
    rate_limit_policy: str = "passthrough"  # passthrough | retry_after | as_503
    retry_after_seconds: float = 5.0
    upstream_error_mode: str = "http"      # http | sse (streaming clients only)
    replay_policy: str = "refuse"          # builder ledger: refuse | resend
    max_attempts: int = 8                  # builder ledger: identities per body under resend
    idle_timeout_seconds: float = 900.0
    max_stream_seconds: float = 3600.0
    max_call_seconds: float = 3600.0
    keepalive_seconds: float = 5.0
    nonstream_keepalive: bool = True       # HTTP 100 interim responses while waiting
    normalize_reported_model: bool = False
    client_tokens: tuple = LEGACY_PLACEHOLDERS
    stats_token: str = STATS_TOKEN
    stats_file: Path | None = None
    ledger_dir: Path | None = None
    dump_bodies: bool = False
    user_agent: str = VERSION
    extra: dict = field(default_factory=dict)


# ---------------------------------------------------------------------------
# accounting: BUILDER (builder-broker-stats/v2, immutable request ledger)
# ---------------------------------------------------------------------------
class BuilderState:
    """Statistics shaped exactly like builder_broker_xhigh.State (+ package 114 fields)."""

    def __init__(self, stats_path: Path | None, *, model: str | None, effort: str | None,
                 upstream_wire: str, default_max_tokens: int | None) -> None:
        self.lock = threading.Lock()
        self.stats_path = stats_path
        self.model, self.effort = model, effort
        self.upstream_wire = upstream_wire
        self.default_max_tokens = default_max_tokens
        for name in ("calls", "completed_calls", "successful_calls", "failures", "provider_failures",
                     "protocol_failures", "delivery_failures", "input_tokens", "output_tokens",
                     "total_tokens", "cached_input_tokens", "reasoning_output_tokens",
                     "unknown_cost_requests", "degenerate_responses", "actual_upstream_requests",
                     "unknown_usage_requests", "cache_queries", "in_flight_upstream_requests"):
            setattr(self, name, 0)
        self.estimated_cost = 0.0
        self.upstream_status_counts: dict[str, int] = {}
        if stats_path and stats_path.exists():
            old = strict_json(stats_path.read_bytes())
            if old.get("schema_version") != BUILDER_SCHEMA:
                raise ValueError("legacy Builder state requires explicit audit; refusing to reset it")
            runtime = old["runtime"]
            for name in ("calls", "completed_calls", "successful_calls", "failures", "provider_failures",
                         "protocol_failures", "delivery_failures", "input_tokens", "output_tokens",
                         "total_tokens", "actual_upstream_requests", "unknown_usage_requests",
                         "cache_queries", "in_flight_upstream_requests"):
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
            "schema_version": BUILDER_SCHEMA,
            "protocol": {"model": self.model, "reasoning_effort": self.effort,
                         "max_upstream_attempts_per_identity": 1,
                         "unknown_requests_replayed": False,
                         "upstream_wire": self.upstream_wire, "client_wire": "responses",
                         "adapter": VERSION, "default_max_tokens": self.default_max_tokens},
            "runtime": {
                "calls": self.calls, "failures": self.failures,
                "actual_upstream_requests": self.actual_upstream_requests,
                "unknown_usage_requests": self.unknown_usage_requests,
                "cache_queries": self.cache_queries, "usage_complete": usage_complete,
                "in_flight_upstream_requests": self.in_flight_upstream_requests,
                "known_total_tokens": self.total_tokens, "known_input_tokens": self.input_tokens,
                "known_output_tokens": self.output_tokens, "completed_calls": self.completed_calls,
                "successful_calls": self.successful_calls,
                "in_flight_calls": max(0, self.calls - self.completed_calls),
                "tokens": self.total_tokens if usage_complete else None,
                "provider_failures": self.provider_failures,
                "protocol_failures": self.protocol_failures,
                "delivery_failures": self.delivery_failures,
                "input_tokens": self.input_tokens if usage_complete else None,
                "output_tokens": self.output_tokens if usage_complete else None,
                "total_tokens": self.total_tokens if usage_complete else None,
                "known_cached_input_tokens": self.cached_input_tokens,
                "known_reasoning_output_tokens": self.reasoning_output_tokens,
                "estimated_cost_usd": round(self.estimated_cost, 10),
                "estimated_cost_complete": self.unknown_cost_requests == 0 and usage_complete,
                "unknown_cost_requests": self.unknown_cost_requests,
                "degenerate_responses": self.degenerate_responses,
            },
            "upstream": {"status_counts": dict(sorted(self.upstream_status_counts.items()))},
            "credential": {"builder_visible": "broker-only-placeholder", "provider_secret_logged": False,
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
               usage: dict | None = None, request_sent: bool = False) -> None:
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
            known = _usage_known(usage)
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
                if isinstance(cost, (int, float)) and not isinstance(cost, bool):
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

    def reconcile(self, ledger: RequestLedger) -> None:
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
    total, unknown = 0.0, 0
    if not root.is_dir():
        return 0.0, 0
    for path in sorted(root.iterdir()):
        receipt = path / "completed.json"
        if not receipt.is_file():
            continue
        usage = (strict_json(receipt.read_bytes()) or {}).get("usage") or {}
        cost = usage.get("estimated_cost")
        if isinstance(cost, (int, float)) and not isinstance(cost, bool):
            total += float(cost)
        else:
            unknown += 1
    return total, unknown


def _usage_known(usage: Any) -> bool:
    return isinstance(usage, dict) and all(
        type(usage.get(k)) is int and usage[k] >= 0 for k in ("input_tokens", "output_tokens", "total_tokens"))


# ---------------------------------------------------------------------------
# accounting: RUNTIME / JUDGE / SEARCH (broker-stats/v1 + JSONL event ledger)
# ---------------------------------------------------------------------------
class EventAccounting:
    """One event per accepted request; counters with the Editing judge-broker semantics."""

    def __init__(self, role: str, stats_path: Path | None, ledger_dir: Path | None, *,
                 model: str | None, effort: str | None, upstream_wire: str) -> None:
        self.role, self.model, self.effort, self.upstream_wire = role, model, effort, upstream_wire
        self.lock = threading.Lock()
        self.instance_id = uuid.uuid4().hex
        self.stats_path = stats_path
        self.events_path = None
        if ledger_dir is not None:
            ledger_dir.mkdir(parents=True, exist_ok=True)
            self.events_path = ledger_dir / ("%s-events.jsonl" % role)
            # A new broker instance must not append to an earlier run's evidence.
            with self.events_path.open("x"):
                pass
            os.chmod(self.events_path, 0o600)
        self.calls = self.completed = self.successful = self.failures = 0
        self.upstream_attempts = self.usage_unknown = self.degenerate = 0
        self.tokens = {"input_tokens": 0, "output_tokens": 0, "total_tokens": 0,
                       "cached_input_tokens": 0, "reasoning_output_tokens": 0}
        self.cost, self.unknown_cost = 0.0, 0
        self.status_counts: dict[str, int] = {}
        self.classifications: dict[str, int] = {}
        self._persist()

    def begin(self) -> dict:
        with self.lock:
            self.calls += 1
            event = {"schema_version": "1", "request_id": uuid.uuid4().hex, "role": self.role,
                     "started_unix": round(time.time(), 3), "upstream_attempts": 0}
            self._persist_locked()
            return event

    def upstream_started(self, event: dict) -> None:
        with self.lock:
            if not event["upstream_attempts"]:
                event["upstream_attempts"] = 1
                self.upstream_attempts += 1
                self._persist_locked()

    def finish(self, event: dict, *, status: int | None, classification: str,
               usage: dict | None = None, response_status: str | None = None,
               reported_model: str | None = None, extra: dict | None = None) -> None:
        with self.lock:
            self.completed += 1
            ok = classification == "success"
            if ok:
                self.successful += 1
            else:
                self.failures += 1
            if classification == "degenerate":
                self.degenerate += 1
            self.classifications[classification] = self.classifications.get(classification, 0) + 1
            if status is not None:
                self.status_counts[str(status)] = self.status_counts.get(str(status), 0) + 1
            known = _usage_known(usage)
            if event["upstream_attempts"] and not known:
                self.usage_unknown += 1
            if known:
                for key in ("input_tokens", "output_tokens", "total_tokens"):
                    self.tokens[key] += usage[key]
                self.tokens["cached_input_tokens"] += int((usage.get("input_tokens_details") or {}).get("cached_tokens", 0) or 0)
                self.tokens["reasoning_output_tokens"] += int((usage.get("output_tokens_details") or {}).get("reasoning_tokens", 0) or 0)
            cost = (usage or {}).get("estimated_cost")
            if isinstance(cost, (int, float)) and not isinstance(cost, bool):
                self.cost += float(cost)
            elif event["upstream_attempts"]:
                self.unknown_cost += 1
            event.update({"state": "terminal", "status": status, "classification": classification,
                          "response_status": response_status,
                          "reported_model": reported_model if isinstance(reported_model, str) and len(reported_model) < 200 else None,
                          "usage": {k: usage[k] for k in ("input_tokens", "output_tokens", "total_tokens")} if known else None,
                          "elapsed_seconds": round(time.time() - event["started_unix"], 3)})
            if extra:
                event.update(extra)
            if self.events_path is not None:
                with self.events_path.open("a", encoding="utf-8") as handle:
                    handle.write(json.dumps(event, sort_keys=True, ensure_ascii=False) + "\n")
            self._persist_locked()

    def stats(self) -> dict[str, Any]:
        with self.lock:
            return self._value_locked()

    def _value_locked(self) -> dict[str, Any]:
        in_flight = self.calls - self.completed
        return {
            "schema_version": BROKER_SCHEMA,
            "broker_instance_id": self.instance_id,
            "role": self.role,
            "protocol": {"model": self.model, "reasoning_effort": self.effort,
                         "upstream_wire": self.upstream_wire, "adapter": VERSION,
                         "max_upstream_attempts_per_request": 1, "inner_retries": 0},
            "runtime": {"calls": self.calls, "completed_calls": self.completed,
                        "successful_calls": self.successful, "failures": self.failures,
                        "in_flight_calls": in_flight, "upstream_attempts": self.upstream_attempts,
                        "usage_unknown_calls": self.usage_unknown,
                        "tokens": self.tokens["total_tokens"], **self.tokens,
                        "estimated_cost_usd": round(self.cost, 10),
                        "unknown_cost_requests": self.unknown_cost,
                        "degenerate_responses": self.degenerate,
                        "classifications": dict(sorted(self.classifications.items()))},
            "upstream": {"status_counts": dict(sorted(self.status_counts.items()))},
            "credential_values_recorded": False,
        }

    def _persist(self) -> None:
        with self.lock:
            self._persist_locked()

    def _persist_locked(self) -> None:
        if self.stats_path is None:
            return
        self.stats_path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.stats_path.with_suffix(self.stats_path.suffix + ".tmp")
        temporary.write_text(json.dumps(self._value_locked(), indent=2) + "\n", encoding="utf-8")
        os.replace(temporary, self.stats_path)


def classify_status(status: int | None) -> str:
    """Same split as the Creation broker: 4xx request errors belong to the client."""
    if status is None:
        return "transport_failure"
    if 200 <= status < 300:
        return "success"
    if status in (400, 405, 413, 415, 422):
        return "request_error"
    return "provider_failure"


# ---------------------------------------------------------------------------
# request shaping
# ---------------------------------------------------------------------------
def pin_responses_body(body: dict, options: Options) -> tuple[dict, dict]:
    """Pin ``model`` and ``reasoning.effort`` on a Responses body; return (body, receipt)."""
    out = dict(body)
    receipt: dict[str, Any] = {"model_requested": body.get("model")}
    if options.model and options.model_policy == "lock":
        out["model"] = options.model
    if options.effort_policy == "lock":
        reasoning = dict(out.get("reasoning") or {}) if isinstance(out.get("reasoning"), dict) else {}
        if options.effort:
            reasoning["effort"] = options.effort
        else:
            reasoning.pop("effort", None)
        if reasoning:
            out["reasoning"] = reasoning
        else:
            out.pop("reasoning", None)
    receipt["model_sent"] = out.get("model")
    receipt["reasoning_effort_sent"] = (out.get("reasoning") or {}).get("effort") if isinstance(out.get("reasoning"), dict) else None
    return out, receipt


def pin_chat_body(body: dict, options: Options) -> dict:
    out = dict(body)
    if options.model and options.model_policy == "lock":
        out["model"] = options.model
    if options.effort_policy == "lock":
        if options.effort:
            out["reasoning_effort"] = options.effort
        else:
            out.pop("reasoning_effort", None)
    if out.get("stream") is True:
        out["stream_options"] = {"include_usage": True}
    return out


def chat_messages_to_responses_input(messages: Any) -> list[dict]:
    """Chat messages -> Responses input (text only), as the evaluator brokers do."""
    if not isinstance(messages, list) or not messages:
        raise ValueError("messages must be a non-empty list")
    result = []
    for message in messages:
        if not isinstance(message, dict):
            raise ValueError("message must be an object")
        role = message.get("role")
        if role not in ("system", "developer", "user", "assistant"):
            raise ValueError("unsupported chat message role")
        content = message.get("content")
        if isinstance(content, list):
            content = "".join(p.get("text", "") for p in content if isinstance(p, dict) and isinstance(p.get("text"), str))
        if not isinstance(content, str) or not content:
            raise ValueError("chat message content must be non-empty text")
        result.append({"type": "message", "role": role, "content": [
            {"type": "output_text" if role == "assistant" else "input_text", "text": content}]})
    return result


def responses_message_text(response: dict) -> str:
    """Only ``message`` items' output text: never reasoning text (DeepSeek can return a reasoning item first)."""
    chunks = []
    for item in response.get("output") or []:
        if not isinstance(item, dict) or item.get("type") != "message":
            continue
        for part in item.get("content") or []:
            if isinstance(part, dict) and part.get("type") in ("output_text", "text") and isinstance(part.get("text"), str):
                chunks.append(part["text"])
    return "".join(chunks)


def responses_to_chat_completion(response: dict, model: str | None) -> dict:
    usage = response.get("usage") if isinstance(response.get("usage"), dict) else {}
    return {"id": response.get("id", "response-proxy"), "object": "chat.completion",
            "created": int(time.time()), "model": model or response.get("model"),
            "choices": [{"index": 0, "message": {"role": "assistant", "content": responses_message_text(response)},
                         "finish_reason": "length" if response.get("status") == "incomplete" else "stop"}],
            "usage": {"prompt_tokens": usage.get("input_tokens"), "completion_tokens": usage.get("output_tokens"),
                      "total_tokens": usage.get("total_tokens")}}


# ---------------------------------------------------------------------------
# chat SSE decoding (from the Lite broker)
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


def chat_usage_from_json(value: Any) -> dict | None:
    return chat_translate.map_usage(value.get("usage")) if isinstance(value, dict) else None


# ---------------------------------------------------------------------------
# HTTP handler
# ---------------------------------------------------------------------------
class _Upstream:
    """Result of opening one upstream request."""

    def __init__(self) -> None:
        self.response = None
        self.status: int | None = None
        self.content_type = ""
        self.error_sample = b""
        self.exception: str | None = None
        self.sent = False


class Handler(http.server.BaseHTTPRequestHandler):
    server_version = VERSION
    protocol_version = "HTTP/1.1"

    # -- plumbing ---------------------------------------------------------
    @property
    def options(self) -> Options:
        return self.server.options  # type: ignore[attr-defined]

    @property
    def accounting(self):
        return self.server.accounting  # type: ignore[attr-defined]

    def log_message(self, _format: str, *_args: object) -> None:
        return  # no request lines, bodies, headers or keys in logs

    def send_json(self, status: int, value: object, *, headers: dict | None = None) -> bool:
        payload = json.dumps(value, ensure_ascii=False).encode("utf-8")
        return self.send_bytes(status, payload, "application/json", headers=headers)

    def send_bytes(self, status: int, payload: bytes, content_type: str, *, headers: dict | None = None) -> bool:
        """Write one complete response; False when the client is gone."""
        try:
            self.send_response(status)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(payload)))
            for key, value in (headers or {}).items():
                self.send_header(key, value)
            self.end_headers()
            self.wfile.write(payload)
            self.wfile.flush()
            return True
        except OSError:
            return False

    def _redact(self, data: bytes) -> bytes:
        key = self.server.provider_key  # type: ignore[attr-defined]
        return data.replace(key.encode(), b"[REDACTED]") if key else data

    def _client_authorized(self, token: str | None) -> bool:
        return isinstance(token, str) and token in self.options.client_tokens

    def _bearer(self) -> str | None:
        value = self.headers.get("Authorization", "")
        return value[7:].strip() if value.startswith("Bearer ") else None

    def _read_body(self) -> dict:
        length = int(self.headers.get("Content-Length", "0"))
        if length <= 0 or length > MAX_BODY:
            raise ValueError("invalid request length")
        body = strict_json(self.rfile.read(length))
        if not isinstance(body, dict):
            raise ValueError("request is not an object")
        return body

    def _open_upstream(self, url: str, payload: bytes, *, accept: str, headers: dict | None = None,
                       on_start=None) -> _Upstream:
        """Exactly one submission; never a redirect, ambient proxy or automatic retry."""
        result = _Upstream()
        all_headers = {"Content-Type": "application/json", "Accept": accept,
                       "Accept-Encoding": "identity", "User-Agent": self.options.user_agent}
        key = self.server.provider_key  # type: ignore[attr-defined]
        if self.options.upstream_wire in ("responses", "chat") and key:
            all_headers["Authorization"] = "Bearer %s" % key
        all_headers.update(headers or {})
        request = urllib.request.Request(url, data=payload, headers=all_headers, method="POST")

        def started():
            result.sent = True
            if on_start is not None:
                on_start()

        try:
            result.response = direct_opener(on_request_start=started).open(
                request, timeout=self.options.max_call_seconds)
            result.status = result.response.status
            result.content_type = result.response.headers.get("Content-Type", "")
        except urllib.error.HTTPError as exc:
            result.status = exc.code
            try:
                result.error_sample = exc.read(ERROR_SAMPLE_BYTES)
            except Exception:
                pass
            try:
                exc.close()
            except Exception:
                pass
        except Exception as exc:  # DNS/TLS/socket: delivery unknown when sent
            result.exception = type(exc).__name__
        return result

    def _wait_with_interim(self, done: threading.Event) -> bool:
        """Block until ``done``; send HTTP 100 interim responses while waiting.

        Returns False when the client went away.  Interim 1xx responses are
        ignored by HTTP clients (urllib/http.client skip them) but keep idle
        timers and the Harbor egress sidecar from closing a quiet connection,
        exactly as the evaluator brokers already do towards their clients.
        """
        connected = True
        while not done.wait(self.options.keepalive_seconds):
            if not (connected and self.options.nonstream_keepalive):
                continue
            try:
                self.send_response_only(100)
                self.end_headers()
                self.wfile.flush()
            except OSError:
                connected = False
        return connected

    # -- GET ----------------------------------------------------------------
    def do_GET(self) -> None:  # noqa: N802
        if self.path in ("/healthz", "/health"):
            self.send_json(200, {"ok": True, "status": "ok", "version": VERSION, "role": self.options.role,
                                 "model": self.options.model, "reasoning_effort": self.options.effort,
                                 "upstream_wire": self.options.upstream_wire,
                                 "default_max_tokens": self.options.default_max_tokens
                                 if self.options.upstream_wire == "chat" else None})
        elif self.path in ("/stats", "/stats.json", "/v1/stats"):
            if self.headers.get("Authorization") != "Bearer %s" % self.options.stats_token:
                self.send_json(401, {"error": "stats auth"})
            else:
                self.send_json(200, self.accounting.stats())
        else:
            self.send_json(404, {"error": "not found"})

    # -- POST ---------------------------------------------------------------
    def do_POST(self) -> None:  # noqa: N802
        path = self.path.split("?", 1)[0].rstrip("/")
        role = self.options.role
        if role == "search":
            self._search(path)
            return
        if path not in ("/v1/responses", "/v1/chat/completions"):
            self.send_json(404, {"error": "not found"})
            return
        if not self._client_authorized(self._bearer()):
            self.send_json(401, {"error": "client must use the broker placeholder credential"})
            return
        try:
            body = self._read_body()
        except (ValueError, json.JSONDecodeError, StreamProtocolError) as exc:
            self._protocol_failure("request_invalid", exc)
            return
        if role == "builder":
            self._builder(path, body)
        else:
            self._evaluator(path, body)

    def _protocol_failure(self, kind: str, exc: Exception) -> None:
        if self.options.role == "builder":
            self.accounting.reserve()
            self.accounting.finish(failure="protocol")
        else:
            event = self.accounting.begin()
            self.accounting.finish(event, status=400, classification="request_error",
                                   extra={"error": kind})
        self.send_json(400, {"error": "broker_protocol_failure", "type": type(exc).__name__,
                             "detail": str(exc)[:300]})

    # ======================================================================
    # BUILDER role: Codex (streaming Responses) with the immutable ledger
    # ======================================================================
    def _builder(self, path: str, body: dict) -> None:
        options = self.options
        try:
            if path == "/v1/chat/completions":
                raise ValueError("the Builder role accepts the Responses wire only")
            if options.upstream_wire == "chat":
                translated = chat_translate.responses_to_chat(
                    body, model=options.model or str(body.get("model") or ""), effort=options.effort,
                    effort_policy=options.effort_policy, reasoning_mode=options.reasoning_mode,
                    default_max_tokens=options.default_max_tokens)
                upstream_body = translated["body"]
                receipt = translated["receipt"]
                custom_tools = translated["custom_tool_names"]
            else:
                upstream_body, receipt = pin_responses_body(body, options)
                custom_tools = []
        except (ValueError, KeyError, chat_translate.TranslationError) as exc:
            self._protocol_failure("translation", exc)
            return

        ledger: RequestLedger = self.server.ledger  # type: ignore[attr-defined]
        identity = cached = error = None
        for attempt in range(max(1, options.max_attempts)):
            try:
                identity, cached, error = ledger.claim(
                    upstream_body, path if attempt == 0 else "%s#attempt=%d" % (path, attempt),
                    options.provider_url)
            except (ValueError, OSError) as exc:
                self.send_json(409, {"error": "builder_request_identity_failure", "type": type(exc).__name__})
                return
            if cached or not error or options.replay_policy != "resend":
                break
        if error or cached:
            self.accounting.record_cache_query()
            if error:
                self.send_json(409, {"error": error, "automatic_retry_allowed": False})
                return
            if not self.send_bytes(cached["status"], cached["payload"], cached["content_type"]):
                self.accounting.record_delivery_failure()
            return

        self.accounting.reserve()
        receipt = dict(receipt, upstream_wire=options.upstream_wire, adapter=VERSION)
        (identity / "translation_receipt.json").write_text(
            json.dumps(receipt, indent=2, sort_keys=True) + "\n", encoding="utf-8")
        if options.dump_bodies:
            (identity / "request_client.json").write_text(json.dumps(body, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
            (identity / "request_upstream.json").write_text(json.dumps(upstream_body, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
        if options.upstream_wire == "chat":
            self._builder_chat(ledger, identity, upstream_body, custom_tools)
        else:
            self._builder_responses(ledger, identity, upstream_body)

    def _builder_fail_before_stream(self, ledger, identity: Path, status: int | None,
                                    sample: bytes, *, sent: bool) -> None:
        redacted = self._redact(sample)
        try:
            with (identity / "upstream_error_sample.redacted").open("xb") as handle:
                handle.write(redacted[:ERROR_SAMPLE_BYTES])
        except OSError:
            pass
        ledger.fail(identity, error="upstream_status_%s" % status, status=status, sent=sent)
        self.accounting.finish(failure="provider", status=status, request_sent=sent)
        message = redacted[:2000].decode("utf-8", "replace")
        if self.options.upstream_error_mode == "http" and status is not None:
            client_status, headers = status, {}
            if status == 429:
                if self.options.rate_limit_policy == "as_503":
                    client_status = 503
                elif self.options.rate_limit_policy == "retry_after":
                    headers["Retry-After"] = str(int(max(1, round(self.options.retry_after_seconds))))
            self.send_json(client_status, {"error": {"message": message, "type": "upstream_error",
                                                     "upstream_status": status, "client_status": client_status}},
                           headers=headers)
            return
        emitter = chat_translate.ResponsesEmitter("resp_failed", self.options.model or "", int(time.time()))
        self._begin_stream()
        try:
            self._write(emitter.start())
            self._write(emitter.failed("upstream_error", message, status=status))
            self._end_stream()
        except OSError:
            self.accounting.record_delivery_failure()

    def _begin_stream(self) -> None:
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream; charset=utf-8")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Transfer-Encoding", "chunked")
        self.end_headers()

    def _write(self, data: bytes) -> None:
        if data:
            self.wfile.write(b"%x\r\n" % len(data) + data + b"\r\n")
            self.wfile.flush()

    def _end_stream(self) -> None:
        self.wfile.write(b"0\r\n\r\n")
        self.wfile.flush()

    def _pump(self, response, capture_path: Path | None):
        """Reader thread: upstream bytes -> queue (and the redacted capture)."""
        chunks: "queue.Queue[object]" = queue.Queue()
        capture_file = capture_path.open("xb") if capture_path is not None else None
        key = self.server.provider_key  # type: ignore[attr-defined]
        capture = RedactedCapture(capture_file, key) if capture_file is not None and key else None

        def reader():
            try:
                while True:
                    block = response.read1(65536)
                    if not block:
                        break
                    if capture is not None:
                        capture.write(block)
                    chunks.put(block)
            except Exception as exc:  # pragma: no cover - socket teardown
                chunks.put(exc)
            finally:
                try:
                    if capture is not None:
                        capture.finish()
                    if capture_file is not None:
                        capture_file.close()
                except Exception:
                    pass
                chunks.put(None)

        threading.Thread(target=reader, daemon=True).start()
        return chunks

    def _builder_responses(self, ledger, identity: Path, upstream_body: dict) -> None:
        """Responses passthrough for the Builder: relay SSE bytes, account the terminal."""
        options = self.options
        stream = upstream_body.get("stream") is True
        sent = {"value": False}

        def on_start():
            ledger.sent(identity)
            sent["value"] = True
            self.accounting.record_upstream_start()

        upstream = self._open_upstream(options.provider_url, json.dumps(upstream_body, ensure_ascii=False).encode("utf-8"),
                                       accept="text/event-stream" if stream else "application/json", on_start=on_start)
        if upstream.response is None or upstream.status != 200:
            sample = upstream.error_sample or ("%s" % (upstream.exception or "")).encode()
            self._builder_fail_before_stream(ledger, identity, upstream.status, sample, sent=sent["value"])
            return
        response = upstream.response
        is_sse = upstream.content_type.split(";", 1)[0].strip().lower() == "text/event-stream"
        decoder = ResponseEvents(max_bytes=MAX_RESPONSE)
        decode_error: str | None = None
        produced = bytearray()
        connected = True
        started = time.monotonic()
        last_byte = started
        failure: str | None = None
        chunks = self._pump(response, identity / "upstream.raw")
        if is_sse:
            self._begin_stream()
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
                    if is_sse and connected and _at_event_boundary(produced):
                        try:
                            self._write(b": keepalive\n\n")
                        except OSError:
                            connected = False
                    continue
                if block is None:
                    break
                if isinstance(block, Exception):
                    failure = "upstream_read_%s" % type(block).__name__
                    break
                last_byte = time.monotonic()
                produced.extend(block)
                if len(produced) > MAX_RESPONSE:
                    failure = "response_byte_limit"
                    break
                if is_sse:
                    if decode_error is None:
                        try:
                            decoder.feed(block)
                        except StreamProtocolError as exc:
                            decode_error = str(exc)
                    if connected:
                        try:
                            self._write(block)
                        except OSError:
                            connected = False
        finally:
            try:
                response.close()
            except Exception:
                pass

        terminal = decoder.response if is_sse else None
        if not is_sse and failure is None:
            try:
                terminal = strict_json(bytes(produced))
            except (ValueError, StreamProtocolError):
                terminal = None
        usage = (terminal or {}).get("usage") if isinstance(terminal, dict) else None
        if failure is None and not isinstance(terminal, dict):
            failure = "no_terminal_response" + (":" + decode_error if decode_error else "")
        if failure is not None:
            if is_sse:
                self.close_connection = True
            else:
                self.send_json(502, {"error": {"type": "upstream_stream_failure", "message": failure}})
            ledger.fail(identity, error=failure, status=200, sent=sent["value"])
            self.accounting.finish(failure="provider", status=200, usage=usage, request_sent=sent["value"])
            return
        # Account before the stream terminator so a stats reader that has just seen
        # the terminal event never races the counters.
        ledger.complete(identity, payload=bytes(produced), status=200,
                        content_type="text/event-stream" if is_sse else "application/json",
                        usage=usage)
        if is_sse:
            self.accounting.finish(failure=None if connected else "delivery", status=200,
                                   usage=usage, request_sent=sent["value"])
            if connected:
                try:
                    self._end_stream()
                except OSError:
                    self.accounting.record_delivery_failure()
        else:
            self.accounting.finish(failure=None, status=200, usage=usage, request_sent=sent["value"])
            if not self.send_bytes(200, bytes(produced), upstream.content_type or "application/json"):
                self.accounting.record_delivery_failure()

    def _builder_chat(self, ledger, identity: Path, upstream_body: dict, custom_tools: list) -> None:
        """Chat translation for the Builder (the Lite broker's exchange, unchanged in behaviour)."""
        options = self.options
        sent = {"value": False}

        def on_start():
            ledger.sent(identity)
            sent["value"] = True
            self.accounting.record_upstream_start()

        started = time.monotonic()
        upstream = self._open_upstream(options.provider_url, json.dumps(upstream_body, ensure_ascii=False).encode("utf-8"),
                                       accept="text/event-stream", on_start=on_start)
        if upstream.response is None or upstream.status != 200:
            sample = upstream.error_sample or ("%s" % (upstream.exception or "")).encode()
            self._builder_fail_before_stream(ledger, identity, upstream.status, sample, sent=sent["value"])
            return
        response = upstream.response
        chunks = self._pump(response, identity / "upstream.raw")
        response_id = "resp_" + hashlib.sha256(identity.name.encode()).hexdigest()[:24]
        emitter = chat_translate.ResponsesEmitter(response_id, options.model or "", int(time.time()),
                                                  custom_tool_names=custom_tools,
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
                    if chunk_usage:
                        usage = chunk_usage
                    if chunk_finish:
                        finish_reason = chunk_finish
                    emit(data)
        except StreamProtocolError as exc:
            failure = "upstream_protocol_%s" % type(exc).__name__
        finally:
            try:
                response.close()
            except Exception:
                pass

        if failure:
            emit(emitter.failed("upstream_stream_failure", failure, status=200))
            self._finish_stream(connected)
            ledger.fail(identity, error=failure, status=200, sent=sent["value"])
            self.accounting.finish(failure="provider", status=200, request_sent=sent["value"])
            return
        degenerate = chat_translate.degenerate_reasoning_only(
            finish_reason=finish_reason, tool_calls=emitter.tool_calls_seen,
            content=emitter.message_text, reasoning=emitter.reasoning_text,
            policy=options.degenerate_response_policy)
        if degenerate:
            self.accounting.record_degenerate()
            emit(emitter.failed("degenerate_reasoning_only_response", degenerate, status=200))
            self._finish_stream(connected)
            ledger.fail(identity, error="degenerate_reasoning_only_response:" + degenerate,
                        status=200, sent=sent["value"])
            self.accounting.finish(failure="provider", status=200, usage=usage, request_sent=sent["value"])
            return
        incomplete = "max_output_tokens" if finish_reason == "length" else None
        emit(emitter.complete(usage, incomplete_reason=incomplete))
        ledger.complete(identity, payload=bytes(produced), status=200,
                        content_type="text/event-stream", usage=usage)
        self.accounting.finish(failure=None if connected else "delivery", status=200, usage=usage,
                               request_sent=sent["value"])
        self._finish_stream(connected)

    def _finish_stream(self, connected: bool) -> None:
        if not connected:
            return
        try:
            self._end_stream()
        except OSError:
            pass

    # ======================================================================
    # RUNTIME / JUDGE roles: evaluator brokers (mostly non-streaming JSON)
    # ======================================================================
    def _evaluator(self, path: str, body: dict) -> None:
        options = self.options
        event = self.accounting.begin()
        chat_client = path == "/v1/chat/completions"
        stream = body.get("stream") is True
        event.update({"client_wire": "chat" if chat_client else "responses",
                      "upstream_wire": options.upstream_wire, "stream": stream,
                      "body_sha256": hashlib.sha256(json.dumps(body, sort_keys=True, ensure_ascii=False).encode()).hexdigest()})
        try:
            if chat_client and options.upstream_wire == "chat":
                upstream_body = pin_chat_body(body, options)
                mode = "chat_passthrough"
            elif chat_client:
                responses_body = {k: v for k, v in body.items() if k not in ("messages", "stream", "stream_options", "max_tokens")}
                responses_body["input"] = chat_messages_to_responses_input(body.get("messages"))
                upstream_body, receipt = pin_responses_body(responses_body, options)
                upstream_body.pop("stream", None)
                stream = False
                mode = "chat_over_responses"
            elif options.upstream_wire == "chat":
                translated = chat_translate.responses_to_chat(
                    body, model=options.model or str(body.get("model") or ""), effort=options.effort,
                    effort_policy=options.effort_policy, reasoning_mode=options.reasoning_mode,
                    default_max_tokens=options.default_max_tokens)
                upstream_body = translated["body"]
                event["translation"] = {k: translated["receipt"].get(k) for k in (
                    "reasoning_effort_sent", "max_tokens_sent", "max_tokens_source", "image_parts",
                    "response_format_sent", "dropped_tools", "custom_tools")}
                mode = "responses_over_chat"
            else:
                upstream_body, receipt = pin_responses_body(body, options)
                event["model_sent"] = receipt.get("model_sent")
                event["reasoning_effort_sent"] = receipt.get("reasoning_effort_sent")
                mode = "responses_passthrough"
        except (ValueError, KeyError, chat_translate.TranslationError) as exc:
            self.accounting.finish(event, status=400, classification="request_error",
                                   extra={"error": "translation_%s" % type(exc).__name__})
            self.send_json(400, {"error": "broker_protocol_failure", "type": type(exc).__name__,
                                 "detail": str(exc)[:300]})
            return
        event["mode"] = mode
        if mode == "responses_over_chat":
            self._eval_responses_over_chat(event, body, upstream_body, translated["custom_tool_names"], stream)
        else:
            self._eval_relay(event, upstream_body, mode, stream)

    def _eval_relay(self, event: dict, upstream_body: dict, mode: str, stream: bool) -> None:
        """One upstream call on the client's own wire; relay status and bytes."""
        options = self.options
        url = options.provider_url
        payload = json.dumps(upstream_body, ensure_ascii=False).encode("utf-8")
        result: dict[str, Any] = {}
        done = threading.Event()

        def fetch():
            try:
                result["upstream"] = self._open_upstream(
                    url, payload, accept="text/event-stream" if stream else "application/json",
                    on_start=lambda: self.accounting.upstream_started(event))
                upstream = result["upstream"]
                if upstream.response is not None and not stream:
                    data = bytearray()
                    while True:
                        block = upstream.response.read1(65536)
                        if not block:
                            break
                        data.extend(block)
                        if len(data) > MAX_RESPONSE:
                            raise StreamProtocolError("response byte limit")
                    result["payload"] = bytes(data)
                    upstream.response.close()
            except Exception as exc:
                result["error"] = type(exc).__name__
            finally:
                done.set()

        threading.Thread(target=fetch, daemon=True).start()
        if not stream:
            connected = self._wait_with_interim(done)
        else:
            done.wait()
            connected = True
        upstream: _Upstream | None = result.get("upstream")
        if upstream is None or (upstream.response is None and upstream.status is None):
            reason = (upstream.exception if upstream else None) or result.get("error") or "unknown"
            self.accounting.finish(event, status=None, classification="transport_failure",
                                   extra={"error": "upstream_%s" % reason})
            if connected:
                self.send_json(502, {"error": {"type": "upstream_transport_failure", "message": reason}})
            return
        if upstream.status != 200:
            sample = self._redact(upstream.error_sample)
            self.accounting.finish(event, status=upstream.status, classification=classify_status(upstream.status))
            headers = {}
            if upstream.status == 429 and options.rate_limit_policy == "retry_after":
                headers["Retry-After"] = str(int(max(1, round(options.retry_after_seconds))))
            status = 503 if upstream.status == 429 and options.rate_limit_policy == "as_503" else upstream.status
            if connected:
                self.send_bytes(status, sample or json.dumps({"error": {"type": "upstream_error"}}).encode(),
                                upstream.content_type or "application/json", headers=headers)
            return

        if stream:
            self._eval_relay_stream(event, upstream, mode)
            return
        raw = result.get("payload")
        if raw is None:
            self.accounting.finish(event, status=200, classification="transport_failure",
                                   extra={"error": "upstream_read_%s" % result.get("error")})
            if connected:
                self.send_json(502, {"error": {"type": "upstream_read_failure"}})
            return
        usage, response_status, reported_model, out = None, None, None, raw
        content_type = upstream.content_type or "application/json"
        try:
            value = strict_json(raw)
        except (ValueError, StreamProtocolError):
            value = None
        if mode == "chat_passthrough" and isinstance(value, dict):
            usage = chat_usage_from_json(value)
            reported_model = value.get("model")
            response_status = "completed" if value.get("choices") else None
        elif isinstance(value, dict):
            usage = value.get("usage") if isinstance(value.get("usage"), dict) else None
            response_status = value.get("status")
            reported_model = value.get("model")
            if mode == "chat_over_responses":
                out = json.dumps(responses_to_chat_completion(value, options.model)).encode()
                content_type = "application/json"
            elif (options.normalize_reported_model and options.model and isinstance(reported_model, str)
                  and reported_model != options.model):
                value["model"] = options.model
                out = json.dumps(value, ensure_ascii=False).encode()
        classification = "success" if response_status == "completed" else (
            "incomplete" if response_status == "incomplete" else "invalid_response")
        extra = {"upstream_model": reported_model} if reported_model != options.model else None
        self.accounting.finish(event, status=200, classification=classification, usage=usage,
                               response_status=response_status, reported_model=reported_model, extra=extra)
        if connected:
            headers = {"X-AgentSWE-Upstream-Attempts": str(event["upstream_attempts"])}
            if isinstance(reported_model, str) and reported_model != options.model:
                headers["X-AgentSWE-Upstream-Model"] = reported_model[:120]
            self.send_bytes(200, out, content_type, headers=headers)

    def _eval_relay_stream(self, event: dict, upstream: _Upstream, mode: str) -> None:
        response = upstream.response
        is_sse = upstream.content_type.split(";", 1)[0].strip().lower() == "text/event-stream"
        decoder = ResponseEvents(max_bytes=MAX_RESPONSE) if mode != "chat_passthrough" else None
        chat_decoder = ChatChunks() if mode == "chat_passthrough" else None
        usage = None
        failure = None
        produced = bytearray()
        connected = True
        chunks = self._pump(response, None)
        if is_sse:
            self._begin_stream()
        last_byte = time.monotonic()
        while True:
            try:
                block = chunks.get(timeout=self.options.keepalive_seconds)
            except queue.Empty:
                if time.monotonic() - last_byte > self.options.idle_timeout_seconds:
                    failure = "upstream_idle_timeout"
                    break
                if is_sse and connected and _at_event_boundary(produced):
                    try:
                        self._write(b": keepalive\n\n")
                    except OSError:
                        connected = False
                continue
            if block is None:
                break
            if isinstance(block, Exception):
                failure = "upstream_read_%s" % type(block).__name__
                break
            last_byte = time.monotonic()
            produced.extend(block)
            try:
                if decoder is not None:
                    decoder.feed(block)
                elif chat_decoder is not None:
                    for chunk in chat_decoder.feed(block):
                        usage = chat_translate.map_usage(chunk.get("usage")) or usage
            except StreamProtocolError:
                decoder = chat_decoder = None
            if connected:
                try:
                    if is_sse:
                        self._write(block)
                except OSError:
                    connected = False
        try:
            response.close()
        except Exception:
            pass
        response_status = reported_model = None
        if decoder is not None and decoder.response is not None:
            usage = decoder.response.get("usage")
            response_status = decoder.response.get("status")
            reported_model = decoder.response.get("model")
        elif chat_decoder is not None and chat_decoder.done:
            response_status = "completed"
        classification = "success" if (failure is None and response_status == "completed") else (
            "transport_failure" if failure else "invalid_response")
        self.accounting.finish(event, status=200, classification=classification, usage=usage,
                               response_status=response_status, reported_model=reported_model,
                               extra={"error": failure} if failure else None)
        if is_sse:
            if failure is None and connected:
                try:
                    self._end_stream()
                except OSError:
                    pass
            else:
                self.close_connection = True
        elif connected:
            self.send_bytes(200, bytes(produced), upstream.content_type or "application/json")

    def _eval_responses_over_chat(self, event: dict, client_body: dict, upstream_body: dict,
                                  custom_tools: list, stream: bool) -> None:
        """Responses client, Chat provider: translate; stream SSE or return one JSON object."""
        options = self.options
        payload = json.dumps(upstream_body, ensure_ascii=False).encode("utf-8")
        opened: dict[str, Any] = {}
        ready = threading.Event()

        def open_it():
            try:
                opened["upstream"] = self._open_upstream(
                    options.provider_url, payload, accept="text/event-stream",
                    on_start=lambda: self.accounting.upstream_started(event))
            except Exception as exc:  # pragma: no cover
                opened["error"] = type(exc).__name__
            finally:
                ready.set()

        threading.Thread(target=open_it, daemon=True).start()
        connected = self._wait_with_interim(ready) if not stream else (ready.wait() or True)
        upstream: _Upstream | None = opened.get("upstream")
        if upstream is None or upstream.response is None or upstream.status != 200:
            status = upstream.status if upstream else None
            classification = classify_status(status) if status is not None else "transport_failure"
            self.accounting.finish(event, status=status, classification=classification,
                                   extra={"error": (upstream.exception if upstream else None) or opened.get("error")})
            if connected:
                body = self._redact(upstream.error_sample) if upstream and upstream.error_sample else json.dumps(
                    {"error": {"type": "upstream_error", "upstream_status": status}}).encode()
                client_status = status or 502
                if client_status == 429 and options.rate_limit_policy == "as_503":
                    client_status = 503
                self.send_bytes(client_status, body, "application/json")
            return
        response_id = "resp_" + event["request_id"][:24]
        emitter = chat_translate.ResponsesEmitter(response_id, options.model or str(client_body.get("model") or ""),
                                                  int(time.time()), custom_tool_names=custom_tools,
                                                  emit_reasoning=options.reasoning_summary == "emit")
        decoder = ChatChunks()
        chunks = self._pump(upstream.response, None)
        usage = None
        finish_reason = None
        failure = None
        if stream:
            self._begin_stream()
            try:
                self._write(emitter.start())
            except OSError:
                connected = False
        else:
            emitter.start()
        done = threading.Event()
        state: dict[str, Any] = {}

        def consume():
            nonlocal usage, finish_reason, failure, connected
            last_byte = time.monotonic()
            try:
                while True:
                    try:
                        block = chunks.get(timeout=options.keepalive_seconds)
                    except queue.Empty:
                        if time.monotonic() - last_byte > options.idle_timeout_seconds:
                            failure = "upstream_idle_timeout"
                            break
                        if stream and connected:
                            try:
                                self._write(emitter.keepalive())
                            except OSError:
                                connected = False
                        continue
                    if block is None:
                        break
                    if isinstance(block, Exception):
                        failure = "upstream_read_%s" % type(block).__name__
                        break
                    last_byte = time.monotonic()
                    for chunk in decoder.feed(block):
                        data, chunk_usage, chunk_finish = apply_chunk(emitter, chunk)
                        if chunk_usage:
                            usage = chunk_usage
                        if chunk_finish:
                            finish_reason = chunk_finish
                        if stream and connected and data:
                            try:
                                self._write(data)
                            except OSError:
                                connected = False
            except StreamProtocolError as exc:
                failure = "upstream_protocol_%s" % type(exc).__name__
            finally:
                try:
                    upstream.response.close()
                except Exception:
                    pass
                done.set()

        if stream:
            consume()
        else:
            threading.Thread(target=consume, daemon=True).start()
            connected = self._wait_with_interim(done) and connected
        degenerate = None if failure else chat_translate.degenerate_reasoning_only(
            finish_reason=finish_reason, tool_calls=emitter.tool_calls_seen,
            content=emitter.message_text, reasoning=emitter.reasoning_text,
            policy=options.degenerate_response_policy)
        if failure or degenerate:
            tail = emitter.failed("upstream_stream_failure" if failure else "degenerate_reasoning_only_response",
                                  failure or degenerate, status=200)
            classification = "transport_failure" if failure else "degenerate"
            self.accounting.finish(event, status=200, classification=classification, usage=usage,
                                   response_status="failed", reported_model=emitter.model,
                                   extra={"error": failure or degenerate})
            if stream:
                if connected:
                    try:
                        self._write(tail)
                        self._end_stream()
                    except OSError:
                        pass
            elif connected:
                self.send_json(502, {"error": {"type": "upstream_stream_failure" if failure else "degenerate_response",
                                               "message": failure or degenerate}})
            return
        incomplete = "max_output_tokens" if finish_reason == "length" else None
        tail = emitter.complete(usage, incomplete_reason=incomplete)
        response_status = emitter.final_response.get("status") if emitter.final_response else None
        self.accounting.finish(event, status=200,
                               classification="success" if response_status == "completed" else "incomplete",
                               usage=usage, response_status=response_status, reported_model=emitter.model)
        if stream:
            if connected:
                try:
                    self._write(tail)
                    self._end_stream()
                except OSError:
                    pass
        elif connected:
            self.send_json(200, emitter.final_response,
                           headers={"X-AgentSWE-Upstream-Attempts": str(event["upstream_attempts"])})

    # ======================================================================
    # SEARCH role: legacy search-proxy shape <-> Serper-compatible provider
    # ======================================================================
    def _search(self, path: str) -> None:
        options = self.options
        try:
            body = self._read_body()
        except (ValueError, json.JSONDecodeError, StreamProtocolError) as exc:
            self.send_json(400, {"error": "invalid_search_request", "type": type(exc).__name__})
            return
        if path in ("/serp_search_v1", "/v1/serp_search_v1"):
            # Legacy contract given to builders: POST {query, page, search_type, token}.
            token = body.get("token")
            query, page = body.get("query"), body.get("page", 1)
            search_type = body.get("search_type", "search")
        elif path.lstrip("/") in SEARCH_TYPES:
            # Serper-native clients: X-API-KEY header, body {q, page, ...}.
            token = self.headers.get("X-API-KEY")
            query, page = body.get("q"), body.get("page", 1)
            search_type = path.lstrip("/")
        else:
            self.send_json(404, {"error": "not found"})
            return
        if not self._client_authorized(token):
            self.send_json(401, {"error": "search client must use the placeholder token"})
            return
        if not isinstance(query, str) or not query.strip() or isinstance(page, bool) or not isinstance(page, int) \
                or not 1 <= page <= 100 or search_type not in SEARCH_TYPES:
            event = self.accounting.begin()
            self.accounting.finish(event, status=400, classification="request_error",
                                   extra={"kind": "search"})
            self.send_json(400, {"error": "invalid_search_request"})
            return
        event = self.accounting.begin()
        event.update({"kind": "search", "search_type": search_type})
        key = self.server.provider_key  # type: ignore[attr-defined]
        if options.upstream_wire == "serper":
            url = options.provider_url.rstrip("/") + "/" + search_type
            upstream_payload = dict({k: v for k, v in body.items() if k not in ("token", "query", "search_type")},
                                    q=query, page=page) if path.lstrip("/") not in SEARCH_TYPES else dict(body)
            headers = {"X-API-KEY": key}
        else:
            url = options.provider_url
            upstream_payload = {"query": query, "page": page, "search_type": search_type, "token": key}
            headers = {}
        result: dict[str, Any] = {}
        done = threading.Event()

        def fetch():
            try:
                upstream = self._open_upstream(url, json.dumps(upstream_payload, ensure_ascii=False).encode(),
                                               accept="application/json", headers=headers,
                                               on_start=lambda: self.accounting.upstream_started(event))
                result["upstream"] = upstream
                if upstream.response is not None:
                    result["payload"] = upstream.response.read(MAX_RESPONSE)
                    upstream.response.close()
            except Exception as exc:
                result["error"] = type(exc).__name__
            finally:
                done.set()

        threading.Thread(target=fetch, daemon=True).start()
        connected = self._wait_with_interim(done)
        upstream = result.get("upstream")
        if upstream is None or (upstream.response is None and upstream.status is None):
            self.accounting.finish(event, status=None, classification="transport_failure")
            if connected:
                self.send_json(502, {"error": {"type": "search_transport_failure"}})
            return
        if upstream.status != 200:
            self.accounting.finish(event, status=upstream.status, classification=classify_status(upstream.status))
            if connected:
                self.send_bytes(upstream.status, self._redact(upstream.error_sample) or b"{}",
                                upstream.content_type or "application/json")
            return
        self.accounting.finish(event, status=200, classification="success", response_status="completed")
        if connected:
            self.send_bytes(200, self._redact(result.get("payload") or b"{}"),
                            upstream.content_type or "application/json")


def _at_event_boundary(produced: bytearray) -> bool:
    return not produced or produced.endswith(b"\n\n") or produced.endswith(b"\r\n\r\n")


# ---------------------------------------------------------------------------
# server construction
# ---------------------------------------------------------------------------
class BrokerServer(http.server.ThreadingHTTPServer):
    daemon_threads = True

    def __init__(self, address: tuple[str, int], options: Options, provider_key: str) -> None:
        if not provider_key:
            raise ValueError("provider key is empty")
        self.options = options
        self.provider_key = provider_key
        self.ledger = None
        if options.role == "builder":
            if options.stats_file is None:
                raise ValueError("the Builder role requires --stats-file (ledger lives beside it)")
            self.ledger = RequestLedger(options.stats_file.resolve().parent / "builder_requests")
            self.accounting = BuilderState(options.stats_file.resolve(), model=options.model,
                                           effort=options.effort, upstream_wire=options.upstream_wire,
                                           default_max_tokens=options.default_max_tokens
                                           if options.upstream_wire == "chat" else None)
            self.accounting.reconcile(self.ledger)
        else:
            self.accounting = EventAccounting(options.role, options.stats_file, options.ledger_dir,
                                              model=options.model, effort=options.effort,
                                              upstream_wire=options.upstream_wire)
        super().__init__(address, Handler)

    def server_close(self) -> None:
        super().server_close()
        if self.ledger is not None:
            self.ledger.close()
