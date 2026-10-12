"""Bounded observations of bytes crossing the evaluator-owned lower relay.

Observation errors never authorize changing a broker response.  Candidate tool
outputs remain untrusted; model text and tool calls do not prove tool execution.
"""
from __future__ import annotations

import hashlib
import json
import os
import threading
import time
from pathlib import Path
from typing import Any

MAX_EVENTS = 512
MAX_BYTES = 4 * 1024 * 1024
MAX_EXCHANGES = 512
MAX_CASE_BYTES = 16 * 1024 * 1024
MAX_ERRORS = 32


def _sha256(raw: bytes) -> str:
    return hashlib.sha256(raw).hexdigest()


def _encode(value: Any) -> bytes:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"), sort_keys=True).encode()


def _load_json(raw):
    def unique_pairs(pairs):
        value = {}
        for key, item in pairs:
            if key in value:
                raise ValueError("duplicate_json_key:" + key)
            value[key] = item
        return value
    return json.loads(raw, object_pairs_hook=unique_pairs)


def _source_ref() -> dict[str, str]:
    path = Path(__file__).resolve()
    return {"path": str(path), "sha256": _sha256(path.read_bytes())}


class ExchangeObservation:
    """One bounded exchange; feed_response observes chunks without transforming them."""

    def __init__(self, *, context_id: str, request: bytes, content_type: str = "",
                 limit: int = MAX_BYTES) -> None:
        self.context_id, self.content_type = context_id, content_type
        self.limit = max(0, limit)
        self.started_at = time.time()
        self.request_sha256, self.request_bytes = _sha256(request), len(request)
        self.request = request if len(request) <= self.limit else b""
        self.response_buffer = bytearray()
        self.response_hasher = hashlib.sha256()
        self.response_bytes = 0
        self.events: list[dict[str, Any]] = []
        self.tool_calls: dict[str, dict[str, Any]] = {}
        self.tool_outputs: list[dict[str, Any]] = []
        self.assistant_messages: dict[str, dict[str, Any]] = {}
        self.response_item_keys: dict[str, str] = {}
        self.chat_indices: dict[tuple[int, int], str] = {}
        self.choice_finish_reasons: dict[int, str | None] = {}
        self.errors: list[str] = []
        self.done_marker = False
        self.terminal_seen = False
        self.model_status: str | None = None
        self.protocol: str | None = None
        self.completed_event = False
        self._event_bytes = 0
        self._finished: dict[str, Any] | None = None
        self.sequence: int | None = None
        self._store_registered = False
        self._submitted = False
        self._reservation = 0
        self._store_owner = None
        if len(request) > self.limit:
            self._error("request_bytes_limit_exceeded")

    def _error(self, message: str) -> None:
        if message not in self.errors and len(self.errors) < MAX_ERRORS:
            self.errors.append(message)

    def feed_response(self, chunk: bytes) -> None:
        if self._finished is not None:
            self._error("response_chunk_after_finish")
            return
        self.response_hasher.update(chunk)
        self.response_bytes += len(chunk)
        room = max(0, self.limit - len(self.response_buffer))
        self.response_buffer.extend(chunk[:room])
        if self.response_bytes > self.limit:
            self._error("response_bytes_limit_exceeded")

    def parse_request(self) -> None:
        if not self.request and self.request_bytes:
            return
        try:
            value = _load_json(self.request)
            if not isinstance(value, dict):
                raise ValueError("request_not_object")
            messages = value.get("messages", [])
            inputs = value.get("input", [])
            for item in (messages if isinstance(messages, list) else []) + (inputs if isinstance(inputs, list) else []):
                if not isinstance(item, dict):
                    continue
                if item.get("role") == "tool" or item.get("type") in {"function_call_output", "tool_result"}:
                    if len(self.tool_outputs) >= MAX_EVENTS:
                        self._error("request_tool_output_limit_exceeded")
                        break
                    self.tool_outputs.append({"source": "candidate_request", "untrusted": True,
                        **{key: item[key] for key in ("tool_call_id", "call_id", "output", "content", "type") if key in item}})
        except Exception as exc:
            self._error("malformed_request:" + type(exc).__name__)

    def _event(self, event: dict[str, Any]) -> bool:
        encoded_bytes = len(_encode(event))
        if len(self.events) >= MAX_EVENTS or self._event_bytes + encoded_bytes > self.limit:
            self._error("event_capture_limit_exceeded")
            return False
        self._event_bytes += encoded_bytes
        self.events.append(event)
        return True

    def _call(self, key: str, *, call_id=None, name=None, arguments=None,
              fragment=False, complete=False) -> dict[str, Any] | None:
        if key not in self.tool_calls:
            if len(self.tool_calls) >= MAX_EVENTS:
                self._error("tool_call_limit_exceeded")
                return None
            self.tool_calls[key] = {"key": key, "call_id": None, "name": None,
                "arguments": "", "argument_complete": False, "source": "model_response"}
        call = self.tool_calls[key]
        for field, value in (("call_id", call_id), ("name", name)):
            if value is not None:
                if not isinstance(value, str):
                    self._error("malformed_tool_call_" + field)
                else:
                    call[field] = value
        if arguments is not None:
            if not isinstance(arguments, str):
                self._error("malformed_tool_call_arguments")
            elif fragment:
                call["arguments"] += arguments
            else:
                call["arguments"] = arguments
        if complete:
            call["argument_complete"] = True
        return call

    def _text(self, key: str, text: Any, *, fragment=False, complete=False) -> None:
        if not isinstance(text, str):
            self._error("malformed_assistant_text")
            return
        if key not in self.assistant_messages:
            if len(self.assistant_messages) >= MAX_EVENTS:
                self._error("assistant_message_limit_exceeded")
                return
            self.assistant_messages[key] = {"key": key, "text": "", "complete": False,
                "source": "model_response", "proof_of_tool_execution": False}
        message = self.assistant_messages[key]
        message["text"] = message["text"] + text if fragment else text
        message["complete"] = message["complete"] or complete

    def _response_item(self, item: dict[str, Any], *, complete: bool, position=0) -> None:
        item_type = item.get("type")
        item_id, call_id = item.get("id"), item.get("call_id")
        if item_type in {"function_call", "tool_call"}:
            if not isinstance(item_id, str) or not isinstance(call_id, str):
                self._error("malformed_responses_call_identity")
                return
            if complete and (not isinstance(item.get("name"), str) or not isinstance(item.get("arguments"), str)):
                self._error("malformed_responses_terminal_call")
            previous = self.response_item_keys.get(item_id, item_id)
            self.response_item_keys[item_id] = call_id
            if previous != call_id and previous in self.tool_calls:
                old = self.tool_calls.pop(previous)
                old["key"] = call_id
                self.tool_calls.setdefault(call_id, old)
            self._call(call_id, call_id=call_id, name=item.get("name"),
                arguments=item.get("arguments"), complete=complete)
        elif item_type == "message":
            content = item.get("content")
            if not isinstance(content, list) or item.get("role") != "assistant":
                self._error("malformed_responses_message")
                return
            parts = []
            for part in content:
                if not isinstance(part, dict):
                    self._error("malformed_responses_content")
                elif part.get("type") == "output_text":
                    if isinstance(part.get("text"), str):
                        parts.append(part["text"])
                    else:
                        self._error("malformed_assistant_text")
            self._text(str(item_id or f"response-message-{position}"), "".join(parts), complete=complete)

    def _response_terminal(self, response: Any, event_type: str | None = None) -> None:
        if not isinstance(response, dict):
            self._error("malformed_responses_terminal")
            return
        if not isinstance(response.get("id"), str) or not response["id"]:
            self._error("malformed_responses_terminal_identity")
            return
        status = response.get("status")
        if status not in {"completed", "failed", "incomplete"}:
            self._error("malformed_responses_terminal_status")
            return
        if event_type and event_type != "response." + status:
            self._error("responses_terminal_status_mismatch")
            return
        output = response.get("output")
        if not isinstance(output, list):
            self._error("malformed_responses_terminal_output")
            return
        if self.terminal_seen:
            self._error("multiple_responses_terminals")
        self.terminal_seen, self.model_status = True, status
        self.completed_event = status == "completed"
        # The terminal output is the provider's final snapshot. Incremental
        # items remain in raw events and must not become duplicate final calls.
        self.tool_calls.clear()
        self.assistant_messages.clear()
        for position, item in enumerate(output):
            if not isinstance(item, dict):
                self._error("malformed_responses_output_item")
                continue
            self._response_item(item, complete=True, position=position)

    def _responses_event(self, event: dict[str, Any]) -> None:
        if self.terminal_seen:
            self._error("responses_payload_after_terminal")
            return
        event_type = event.get("type")
        if event_type in {"response.completed", "response.failed", "response.incomplete"}:
            self._response_terminal(event.get("response"), event_type)
        elif event_type in {"response.output_item.added", "response.output_item.done"}:
            if isinstance(event.get("item"), dict):
                self._response_item(event["item"], complete=event_type.endswith("done"))
            else:
                self._error("malformed_responses_output_item")
        elif event_type in {"response.function_call_arguments.delta", "response.function_call_arguments.done"}:
            item_id = event.get("item_id")
            if not isinstance(item_id, str):
                self._error("malformed_responses_argument_identity")
                return
            key = self.response_item_keys.get(item_id, item_id)
            fragment = event_type.endswith("delta")
            self._call(key, call_id=event.get("call_id"),
                arguments=event.get("delta" if fragment else "arguments"), fragment=fragment, complete=not fragment)
        elif event_type == "error":
            self._error("provider_error_event")

    def _chat_event(self, event: dict[str, Any], *, streaming: bool) -> None:
        choices = event.get("choices")
        if not isinstance(choices, list):
            self._error("malformed_chat_choices")
            return
        for position, choice in enumerate(choices):
            if not isinstance(choice, dict):
                self._error("malformed_chat_choice")
                continue
            index = choice.get("index", position)
            if type(index) is not int or index < 0:
                self._error("malformed_chat_choice_index")
                continue
            self.choice_finish_reasons.setdefault(index, None)
            payload = choice.get("delta" if streaming else "message")
            if not isinstance(payload, dict):
                self._error("malformed_chat_message")
                continue
            if self.choice_finish_reasons[index] is not None and payload:
                self._error("chat_delta_after_terminal")
            content = payload.get("content")
            if content is not None:
                self._text(f"chat-choice-{index}", content, fragment=streaming)
            calls = payload.get("tool_calls", [])
            if not isinstance(calls, list):
                self._error("malformed_chat_tool_calls")
                calls = []
            for call_position, call in enumerate(calls):
                if not isinstance(call, dict) or not isinstance(call.get("function"), dict):
                    self._error("malformed_chat_tool_call")
                    continue
                tool_index = call.get("index", call_position)
                if type(tool_index) is not int or tool_index < 0:
                    self._error("malformed_chat_tool_index")
                    continue
                slot = (index, tool_index)
                key = self.chat_indices.setdefault(slot, f"chat-choice-{index}-tool-{tool_index}")
                function = call["function"]
                self._call(key, call_id=call.get("id"), name=function.get("name"),
                    arguments=function.get("arguments"), fragment=streaming)
            finish = choice.get("finish_reason")
            if finish is not None:
                if finish not in {"stop", "tool_calls", "function_call", "length", "content_filter"}:
                    self._error("malformed_chat_finish_reason")
                    continue
                self.choice_finish_reasons[index] = finish
                for slot, key in self.chat_indices.items():
                    if slot[0] == index:
                        self.tool_calls[key]["argument_complete"] = finish in {"stop", "tool_calls", "function_call"}
                        self.tool_calls[key]["finish_reason"] = finish
                message = self.assistant_messages.get(f"chat-choice-{index}")
                if message:
                    message.update(complete=True, finish_reason=finish)

    def _sse_values(self, raw: bytes):
        data = []
        # SSE dispatches a message only at a blank line. An unfinished final
        # frame is evidence of transport truncation even if it looks like JSON.
        for line in raw.replace(b"\r\n", b"\n").replace(b"\r", b"\n").split(b"\n")[:-1]:
            if not line:
                if data:
                    payload = b"\n".join(data)
                    data = []
                    if payload == b"[DONE]":
                        self.done_marker = True
                        continue
                    if self.done_marker:
                        self._error("sse_data_after_done")
                    try:
                        event = _load_json(payload)
                        if not isinstance(event, dict):
                            raise ValueError("event_not_object")
                        yield event
                    except Exception as exc:
                        self._error("malformed_sse_event:" + type(exc).__name__)
            elif line.startswith(b"data:"):
                value = line[5:]
                data.append(value[1:] if value.startswith(b" ") else value)
            elif line == b"data":
                data.append(b"")
        if data or (raw and not raw.endswith((b"\n", b"\r"))):
            self._error("unterminated_sse_frame")

    def parse_response(self, raw: bytes, content_type: str, status: int) -> None:
        streaming = "text/event-stream" in content_type.lower() or raw.lstrip().startswith((b"data:", b"event:"))
        if streaming:
            values = self._sse_values(raw)
        else:
            try:
                value = _load_json(raw)
                if not isinstance(value, dict):
                    raise ValueError("response_not_object")
                values = iter([value])
            except Exception as exc:
                self._error("malformed_response:" + type(exc).__name__)
                values = iter(())
        for event in values:
            if not self._event(event):
                break
            protocol = "chat" if "choices" in event else "responses"
            if self.protocol is not None and self.protocol != protocol:
                self._error("mixed_response_protocols")
            self.protocol = protocol
            if protocol == "chat":
                self._chat_event(event, streaming=streaming)
            elif streaming:
                self._responses_event(event)
            else:
                self._response_terminal(event)
        if self.protocol == "chat":
            self.terminal_seen = bool(self.choice_finish_reasons) and all(reason is not None for reason in self.choice_finish_reasons.values())
            if streaming and not self.done_marker:
                self._error("missing_chat_done_marker")
            self.model_status = "terminal" if self.terminal_seen else None
            for slot, key in self.chat_indices.items():
                call = self.tool_calls[key]
                if self.choice_finish_reasons[slot[0]] in {"stop", "tool_calls", "function_call"} and (
                        not isinstance(call.get("call_id"), str) or not isinstance(call.get("name"), str)):
                    self._error("malformed_chat_terminal_tool_identity")
        if not self.terminal_seen:
            self._error("missing_model_terminal")

    def finish(self, *, status: int, raw: bytes | None = None, content_type: str = "",
               error: str | None = None) -> dict[str, Any]:
        if self._finished is not None:
            return self._finished
        if raw is not None:
            self.feed_response(raw)
        if error:
            self._error(error)
        try:
            self.parse_request()
            self.parse_response(bytes(self.response_buffer), content_type, status)
        except Exception as exc:
            self._error("observer_parse_error:" + type(exc).__name__)
        capture_complete = self.terminal_seen and not self.errors
        record = {
            "schema_version": "openwiki-broker-observation/v2", "sequence": self.sequence,
            "complete": capture_complete and 200 <= status < 300 and self.model_status not in {"failed", "incomplete"},
            "capture_complete": capture_complete, "model_status": self.model_status,
            "choice_finish_reasons": {str(k): v for k, v in self.choice_finish_reasons.items()},
            "context_id": self.context_id, "request_sha256": self.request_sha256,
            "request_bytes": self.request_bytes, "response_sha256": self.response_hasher.hexdigest(),
            "response_bytes": self.response_bytes, "response_status": status, "content_type": content_type,
            "started_at": self.started_at, "finished_at": time.time(), "events": self.events,
            "model_tool_calls": list(self.tool_calls.values()),
            "assistant_messages": list(self.assistant_messages.values()),
            "candidate_reported_tool_outputs": self.tool_outputs, "errors": list(self.errors),
            "done_marker": self.done_marker, "untrusted_candidate_outputs": True,
            "exchange_limit_bytes": self.limit, "event_limit": MAX_EVENTS,
            "interpretation": "Model text/calls and candidate-reported outputs do not prove tool execution. Argument strings are observed verbatim; their JSON validity and task success are not inferred.",
        }
        # Bound all derived fields together, including request-side tool output
        # and the copies of response text used for structural interpretation.
        if len(_encode(record)) > self.limit:
            record["errors"].append("exchange_record_bytes_limit_exceeded")
            record.update(complete=False, capture_complete=False)
            for field in ("events", "candidate_reported_tool_outputs", "assistant_messages", "model_tool_calls"):
                record[field] = []
                if len(_encode(record)) <= self.limit:
                    break
        self.request = b""
        self.response_buffer.clear()
        self._finished = record
        return record


class ObservationStore:
    """Bounded case store. close returns an honest snapshot even with late workers."""

    def __init__(self, directory: Path | None, context_id: str, *, max_records=MAX_EXCHANGES,
                 max_bytes=MAX_CASE_BYTES, max_exchange_bytes=MAX_BYTES) -> None:
        self.context_id = context_id
        self.condition = threading.Condition()
        self.counter = 0
        self.records: list[dict[str, Any]] = []
        self.errors: list[str] = []
        self.max_records, self.max_bytes = max_records, max_bytes
        self.max_exchange_bytes = max_exchange_bytes
        self.stored_bytes = self.reserved_bytes = self.active = self.discarded = 0
        self.closed = self.closing = False
        self.directory = None
        self.source = None
        try:
            self.source = _source_ref()
            if directory is not None:
                self.directory = Path(directory).resolve()
                self.directory.mkdir(parents=True, exist_ok=True)
        except Exception as exc:
            self._error("store_setup_error:" + type(exc).__name__)

    def _error(self, message: str) -> None:
        if message not in self.errors and len(self.errors) < MAX_ERRORS:
            self.errors.append(message)

    def begin(self, request: bytes) -> ExchangeObservation:
        with self.condition:
            self.counter += 1
            reservation = min(self.max_exchange_bytes, max(0, self.max_bytes - self.stored_bytes - self.reserved_bytes))
            unavailable = self.closed or self.closing
            allowed = not unavailable and self.counter <= self.max_records and reservation >= 2048
            # Reserve additional bytes for the persisted path/hash reference.
            exchange = ExchangeObservation(context_id=self.context_id, request=request, limit=reservation - 512 if allowed else 0)
            exchange.sequence = self.counter
            exchange._store_owner = self
            if not unavailable:
                exchange._store_registered = True
                self.active += 1
            if allowed:
                exchange._reservation = reservation
                self.reserved_bytes += reservation
            else:
                self.discarded += 1
                self._error("store_closed" if unavailable else "case_observation_limit_exceeded")
                exchange._error("store_closed" if unavailable else "case_observation_limit_exceeded")
            for error in self.errors:
                exchange._error(error)
            return exchange

    def record(self, exchange: ExchangeObservation | bytes, raw: bytes | None = None, *,
               status: int, content_type: str, error: str | None = None) -> dict[str, Any]:
        if isinstance(exchange, bytes):
            exchange = self.begin(exchange)
        with self.condition:
            if exchange._store_owner is not self:
                self._error("exchange_store_identity_mismatch")
                return {"complete": False, "capture_complete": False, "context_id": self.context_id,
                    "errors": ["exchange_store_identity_mismatch"]}
            if exchange._submitted:
                return {"complete": False, "context_id": self.context_id, "errors": ["exchange_already_recorded"]}
            exchange._submitted = True
        record = None
        record_failure = False
        try:
            record = exchange.finish(status=status, raw=raw, content_type=content_type, error=error)
            encoded = _encode(record)
            with self.condition:
                permitted = not self.closed and exchange._reservation >= len(encoded) + 512
            if not permitted:
                record.update(complete=False, capture_complete=False)
                record["errors"].append("store_closed" if self.closed else "case_observation_limit_exceeded")
            elif self.directory is not None:
                path = self.directory / f"broker-observation-{exchange.sequence:04d}.json"
                # Each issued identity can be persisted only once.
                flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0)
                with os.fdopen(os.open(path, flags, 0o600), "wb") as stream:
                    stream.write(encoded)
                    stream.flush()
                record = {**record, "path": str(path), "sha256": _sha256(encoded)}
        except Exception as exc:
            record_failure = True
            if record is None:
                record = {"complete": False, "capture_complete": False, "sequence": exchange.sequence,
                    "context_id": self.context_id, "errors": []}
            record.update(complete=False, capture_complete=False)
            record["errors"].append("observer_record_error:" + type(exc).__name__)
            with self.condition:
                self._error("observer_record_error:" + type(exc).__name__)
        finally:
            with self.condition:
                self.reserved_bytes -= exchange._reservation
                size = len(_encode(record)) if record is not None else 0
                if record is not None and not self.closed and exchange._reservation and self.stored_bytes + size <= self.max_bytes and len(self.records) < self.max_records:
                    self.records.append(record)
                    self.stored_bytes += max(size, exchange._reservation) if record_failure else size
                elif record is not None:
                    self._error("observation_not_in_closed_or_bounded_snapshot")
                    # A failed record may have left a partial exclusive file.
                    # Retain its reservation in accounting so failures cannot
                    # create an unlimited number of uncounted files.
                    self.stored_bytes = min(self.max_bytes, self.stored_bytes + exchange._reservation)
                if exchange._store_registered:
                    self.active -= 1
                self.condition.notify_all()
        return record

    def close(self, timeout: float = 3) -> dict[str, Any]:
        deadline = time.monotonic() + max(0, timeout)
        with self.condition:
            # Stop new admissions, but allow already-admitted records to finish
            # before freezing the snapshot. No storage I/O runs under this lock.
            self.closing = True
            while self.active and time.monotonic() < deadline:
                self.condition.wait(timeout=max(0, deadline - time.monotonic()))
            self.closed = True
            if self.active:
                self._error("observer_workers_pending_at_close")
            records = list(self.records)
            return {"schema_version": "openwiki-broker-observation-store/v1", "context_id": self.context_id,
                "observer_source": self.source, "complete": not self.active and not self.errors and all(r.get("complete") is True for r in records),
                "sealed": self.active == 0, "pending": self.active, "issued": self.counter,
                "stored_records": len(records), "discarded": self.discarded, "stored_bytes": self.stored_bytes,
                "case_limit_bytes": self.max_bytes, "case_record_limit": self.max_records,
                "exchange_limit_bytes": self.max_exchange_bytes, "errors": list(self.errors), "records": records,
                "limits": {"case_bytes": self.max_bytes, "case_records": self.max_records, "exchange_bytes": self.max_exchange_bytes},
                "storage_accounting": "serialized record bytes; failed writes conservatively consume their full reserved bytes",
                "counters": {"issued": self.counter, "stored": len(records), "discarded": self.discarded, "stored_bytes": self.stored_bytes},
                "record_references": [{key: r[key] for key in ("sequence", "path", "sha256", "complete") if key in r} for r in records]}


def load_observations(output: Path, expected_context_id: str) -> dict[str, Any]:
    """Validate the persisted store snapshot against source and exchange bytes."""
    output = Path(output).resolve()
    summary_path = output / "native-broker-observation.json"
    try:
        if summary_path.is_symlink() or not summary_path.is_file():
            raise ValueError("broker observation summary is not a regular file")
        summary = _load_json(summary_path.read_bytes())
        if not isinstance(summary, dict) or summary.get("schema_version") != "openwiki-broker-observation-store/v1":
            raise ValueError("broker observation summary schema mismatch")
        if summary.get("context_id") != expected_context_id:
            raise ValueError("broker observation context mismatch")
        if summary.get("observer_source") != _source_ref():
            raise ValueError("broker observation source changed")
        records = summary.get("records")
        if not isinstance(records, list) or len(records) > MAX_EXCHANGES:
            raise ValueError("broker observation records are not bounded")
        seen = set()
        for record in records:
            if not isinstance(record, dict) or record.get("context_id") != expected_context_id:
                raise ValueError("broker exchange context mismatch")
            sequence = record.get("sequence")
            if type(sequence) is not int or not 0 < sequence <= MAX_EXCHANGES or sequence in seen:
                raise ValueError("broker exchange sequence mismatch")
            seen.add(sequence)
            expected_path = output / f"broker-observation-{sequence:04d}.json"
            if record.get("path") != str(expected_path) or expected_path.is_symlink() or not expected_path.is_file():
                raise ValueError("broker exchange path is not an owned regular file")
            raw = expected_path.read_bytes()
            if len(raw) > MAX_BYTES or record.get("sha256") != _sha256(raw):
                raise ValueError("broker exchange bytes changed or exceeded limit")
            if _load_json(raw) != {k: v for k, v in record.items() if k not in {"path", "sha256"}}:
                raise ValueError("broker exchange snapshot changed")
        if summary.get("stored_records") != len(records):
            raise ValueError("broker observation count mismatch")
        expected_counters = {"issued": summary.get("issued"), "stored": len(records),
            "discarded": summary.get("discarded"), "stored_bytes": summary.get("stored_bytes")}
        if summary.get("counters") != expected_counters or any(type(v) is not int or v < 0 for v in expected_counters.values()):
            raise ValueError("broker observation counters mismatch")
        limits = summary.get("limits")
        expected_limits = {"case_bytes": summary.get("case_limit_bytes"), "case_records": summary.get("case_record_limit"),
            "exchange_bytes": summary.get("exchange_limit_bytes")}
        if limits != expected_limits or any(type(v) is not int or v <= 0 for v in expected_limits.values()):
            raise ValueError("broker observation limits mismatch")
        if limits["case_bytes"] > MAX_CASE_BYTES or limits["case_records"] > MAX_EXCHANGES or limits["exchange_bytes"] > MAX_BYTES:
            raise ValueError("broker observation limits exceed contract")
        if summary["stored_bytes"] > limits["case_bytes"] or len(records) > limits["case_records"]:
            raise ValueError("broker observation storage exceeded limit")
        refs = [{key: r[key] for key in ("sequence", "path", "sha256", "complete") if key in r} for r in records]
        if summary.get("record_references") != refs:
            raise ValueError("broker observation references mismatch")
        if type(summary.get("pending")) is not int or summary["pending"] < 0 or type(summary.get("sealed")) is not bool:
            raise ValueError("broker observation lifecycle metadata invalid")
        if summary["sealed"] and summary["pending"] != 0:
            raise ValueError("broker observation sealed with pending workers")
        if summary.get("complete") is True and (summary.get("sealed") is not True or summary.get("pending") != 0
                or summary.get("errors") != [] or summary.get("discarded") != 0 or summary.get("issued") != len(records)
                or any(r.get("complete") is not True for r in records)):
            raise ValueError("broker observation completeness mismatch")
        if summary.get("complete") is True and (seen != set(range(1, summary["issued"] + 1))
                or summary["stored_bytes"] != sum(len(_encode(record)) for record in records)):
            raise ValueError("broker observation sequence or byte accounting mismatch")
        return summary
    except (OSError, TypeError, KeyError, UnicodeError, json.JSONDecodeError) as exc:
        raise ValueError("invalid broker observation: " + type(exc).__name__) from exc
