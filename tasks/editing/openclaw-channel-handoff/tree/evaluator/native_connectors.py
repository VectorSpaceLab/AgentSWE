"""Native external service boundaries, not a Gateway scenario runner.

The four adapters originate from the read-only original OpenClaw harness.
Only the service classes are retained: no Recorder, weighted assertions,
GatewayRuntime, action sequence, repair helper, or reference solution.
Transport is explicitly supplied by the evaluator world, with no TCP fallback.
The original scenario orchestrator is never imported.
"""
from __future__ import annotations
import base64
import hashlib
import json
import threading
from http.server import BaseHTTPRequestHandler
from typing import Any, Callable


class EffectSink:
    """Evaluator-owned idempotent loopback sink with a post-accept crash hook."""

    def __init__(self, server_factory: Callable[[type[BaseHTTPRequestHandler]], Any]) -> None:
        self.requests: list[str] = []
        self.accepted: dict[str, str] = {}
        self.accepted_payloads: dict[str, dict[str, Any]] = {}
        self.reject_ids: set[str] = set()
        self.crash_ids: set[str] = set()
        self.drop_next_response = False
        self.kill_callback: Callable[[], None] | None = None
        self.lock = threading.Lock()
        sink = self

        class Handler(BaseHTTPRequestHandler):
            def do_POST(self) -> None:  # noqa: N802 - stdlib callback name
                try:
                    length = int(self.headers.get("Content-Length", "0"))
                    payload = json.loads(self.rfile.read(length))
                    if not isinstance(payload, dict) or set(payload) != {"task_id", "effect_id", "kind", "payload"}:
                        raise ValueError("effect envelope mismatch")
                    effect_id = payload.get("effect_id")
                    if not isinstance(effect_id, str) or not effect_id:
                        raise ValueError("effect identity missing")
                except Exception:
                    self.send_error(400)
                    return
                if self.path != "/effects" or self.headers.get("Idempotency-Key") != effect_id:
                    self.send_error(400)
                    return
                with sink.lock:
                    sink.requests.append(effect_id)
                    if effect_id in sink.accepted_payloads and sink.accepted_payloads[effect_id] != payload:
                        self.send_error(409)
                        return
                    if effect_id in sink.reject_ids:
                        self.send_error(503)
                        return
                    duplicate = effect_id in sink.accepted
                    sink.accepted_payloads.setdefault(effect_id, payload)
                    receipt = sink.accepted.setdefault(effect_id, f"receipt-{effect_id}")
                    crash = effect_id in sink.crash_ids and not duplicate
                    drop = sink.drop_next_response and not duplicate
                    if drop:
                        sink.drop_next_response = False
                if crash and sink.kill_callback is not None:
                    sink.kill_callback()
                    self.close_connection = True
                    return
                if drop:
                    self.close_connection = True
                    return
                body = json.dumps({
                    "ok": True, "effect_id": effect_id,
                    "receipt_id": receipt, "duplicate": duplicate,
                }).encode("utf-8")
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, _format: str, *_args: Any) -> None:
                return

        self.server = server_factory(Handler)
        self.thread = threading.Thread(target=self.server.serve_forever, kwargs={'poll_interval': .05}, daemon=True)

    def start(self) -> None:
        self.thread.start()

    def close(self) -> None:
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=3)


class MediaProvider:
    """Evaluator-owned resumable upload adapter with response-loss controls."""

    def __init__(self, server_factory: Callable[[type[BaseHTTPRequestHandler]], Any]) -> None:
        self.token = "evaluator-media-connector-token"
        self.requests: list[dict[str, Any]] = []
        self.uploads: dict[str, dict[str, Any]] = {}
        self.drop_once: set[str] = set()
        self.drop_stage_once: set[str] = set()
        self.dropped: set[str] = set()
        self.invalid_status_offset: int | None = None
        self.lock = threading.Lock()
        provider = self

        class Handler(BaseHTTPRequestHandler):
            def _payload(self) -> dict[str, Any] | None:
                try:
                    length = int(self.headers.get("Content-Length", "0"))
                    value = json.loads(self.rfile.read(length))
                    return value if isinstance(value, dict) else None
                except Exception:
                    return None

            def _json(self, status: int, value: dict[str, Any]) -> None:
                body = json.dumps(value, separators=(",", ":")).encode("utf-8")
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def _drop(self, upload_id: str, stage_name: str) -> bool:
                # upload_id is opaque and may itself contain colons.
                stage = f"{upload_id}:{stage_name}"
                generic_key = f"stage:{stage_name}"
                should_drop = stage in provider.drop_once or stage_name in provider.drop_stage_once
                marker = stage if stage in provider.drop_once else generic_key
                if should_drop and marker not in provider.dropped:
                    provider.dropped.add(marker)
                    self.close_connection = True
                    return True
                return False

            def do_POST(self) -> None:  # noqa: N802 - stdlib callback name
                payload = self._payload()
                if payload is None or self.headers.get("Authorization") != f"Bearer {provider.token}":
                    self.send_error(400)
                    return
                upload_id = payload.get("upload_id")
                if not isinstance(upload_id, str) or not 0 < len(upload_id.encode()) <= 128:
                    self.send_error(400)
                    return
                with provider.lock:
                    if self.path == "/v1/uploads":
                        expected = {
                            "upload_id", "media_id", "manifest_id", "content_sha256", "size",
                            "media_type", "chunk_size", "chunk_count", "chunks",
                        }
                        if set(payload) != expected or self.headers.get("Idempotency-Key") != upload_id:
                            self.send_error(400)
                            return
                        chunks = payload.get("chunks")
                        if (not isinstance(chunks, list) or type(payload.get("chunk_count")) is not int
                            or len(chunks) != payload["chunk_count"]
                            or type(payload.get("size")) is not int or not 0 <= payload["size"] <= 65536
                            or type(payload.get("chunk_size")) is not int or not 1024 <= payload["chunk_size"] <= 16384):
                            self.send_error(400)
                            return
                        manifest = {
                            key: payload[key] for key in (
                                "media_id", "content_sha256", "size", "media_type",
                                "chunk_size", "chunk_count", "chunks",
                            )
                        }
                        canonical = json.dumps(
                            manifest, sort_keys=True, separators=(",", ":"), ensure_ascii=False,
                        ).encode("utf-8")
                        expected_manifest_id = f"sha256:{hashlib.sha256(canonical).hexdigest()}"
                        next_offset = 0
                        chunks_valid = True
                        for index, item in enumerate(chunks):
                            if (
                                not isinstance(item, dict)
                                or set(item) != {"index", "offset", "size", "sha256"}
                                or type(item.get("index")) is not int
                                or type(item.get("offset")) is not int
                                or item.get("index") != index
                                or item.get("offset") != next_offset
                                or type(item.get("size")) is not int
                                or item["size"] <= 0
                                or item["size"] > payload.get("chunk_size", 0)
                                or not isinstance(item.get("sha256"), str)
                            ):
                                chunks_valid = False
                                break
                            next_offset += item["size"]
                        if (
                            payload.get("manifest_id") != expected_manifest_id
                            or not isinstance(payload.get("chunk_size"), int)
                            or not 1024 <= payload["chunk_size"] <= 16384
                            or not chunks_valid
                            or next_offset != payload.get("size")
                        ):
                            self.send_error(409)
                            return
                        duplicate = upload_id in provider.uploads
                        current = provider.uploads.get(upload_id)
                        if current is not None and current["init"] != payload:
                            self.send_error(409)
                            return
                        current = provider.uploads.setdefault(upload_id, {
                            "init": payload, "session": f"upload-session-{upload_id}",
                            "chunks": {}, "next_offset": 0, "finalized": False,
                            "provider_media_id": f"provider-media-{upload_id}",
                        })
                        provider.requests.append({"stage": "init", "payload": payload})
                        if self._drop(upload_id, "init"):
                            return
                        response = {
                            "ok": True, "upload_id": upload_id,
                            "manifest_id": payload["manifest_id"],
                            "upload_session_id": current["session"],
                            "next_offset": 0, "duplicate": duplicate,
                        }
                    elif self.path == "/v1/upload-chunks":
                        expected = {
                            "upload_id", "upload_session_id", "manifest_id", "index", "offset",
                            "size", "chunk_sha256", "content_base64",
                        }
                        index = payload.get("index")
                        expected_key = f"{upload_id}:chunk:{index}"
                        current = provider.uploads.get(upload_id)
                        if set(payload) != expected or self.headers.get("Idempotency-Key") != expected_key or current is None:
                            self.send_error(400)
                            return
                        manifest_chunks = current["init"]["chunks"]
                        if (type(index) is not int or index < 0 or index >= len(manifest_chunks)
                            or type(payload.get("offset")) is not int or type(payload.get("size")) is not int):
                            self.send_error(400)
                            return
                        manifest_chunk = manifest_chunks[index]
                        try:
                            raw = base64.b64decode(str(payload.get("content_base64", "")), validate=True)
                        except ValueError:
                            self.send_error(400)
                            return
                        valid = (
                            payload.get("upload_session_id") == current["session"]
                            and payload.get("manifest_id") == current["init"]["manifest_id"]
                            and payload.get("offset") == manifest_chunk.get("offset")
                            and payload.get("size") == manifest_chunk.get("size") == len(raw)
                            and payload.get("chunk_sha256") == manifest_chunk.get("sha256")
                            == hashlib.sha256(raw).hexdigest()
                        )
                        if not valid:
                            self.send_error(409)
                            return
                        prior = current["chunks"].get(index)
                        duplicate = prior is not None
                        if prior is not None and prior != payload:
                            self.send_error(409)
                            return
                        if prior is None and int(payload["offset"]) != current["next_offset"]:
                            self.send_error(409)
                            return
                        current["chunks"][index] = payload
                        current["next_offset"] = max(
                            int(current["next_offset"]), int(payload["offset"]) + int(payload["size"]),
                        )
                        provider.requests.append({"stage": f"chunk:{index}", "payload": payload})
                        if self._drop(upload_id, f"chunk:{index}"):
                            return
                        response = {
                            "ok": True, "upload_id": upload_id,
                            "upload_session_id": current["session"],
                            "manifest_id": current["init"]["manifest_id"],
                            "accepted_offset": payload["offset"],
                            "next_offset": current["next_offset"], "duplicate": duplicate,
                        }
                    elif self.path == "/v1/upload-finalize":
                        expected = {"upload_id", "upload_session_id", "manifest_id", "chunk_count"}
                        current = provider.uploads.get(upload_id)
                        if set(payload) != expected or self.headers.get("Idempotency-Key") != f"{upload_id}:finalize" or current is None:
                            self.send_error(400)
                            return
                        if (
                            type(payload.get("chunk_count")) is not int
                            or payload.get("upload_session_id") != current["session"]
                            or payload.get("manifest_id") != current["init"]["manifest_id"]
                            or payload.get("chunk_count") != current["init"]["chunk_count"]
                            or len(current["chunks"]) != current["init"]["chunk_count"]
                            or current["next_offset"] != current["init"]["size"]
                        ):
                            self.send_error(409)
                            return
                        reconstructed = b"".join(base64.b64decode(current["chunks"][i]["content_base64"], validate=True)
                                                 for i in range(current["init"]["chunk_count"]))
                        if hashlib.sha256(reconstructed).hexdigest() != current["init"]["content_sha256"]:
                            self.send_error(409)
                            return
                        duplicate = bool(current["finalized"])
                        current["finalized"] = True
                        provider.requests.append({"stage": "finalize", "payload": payload})
                        if self._drop(upload_id, "finalize"):
                            return
                        response = {
                            "ok": True, "upload_id": upload_id,
                            "upload_session_id": current["session"],
                            "manifest_id": current["init"]["manifest_id"],
                            "provider_media_id": current["provider_media_id"],
                            "complete": True, "duplicate": duplicate,
                        }
                    elif self.path == "/v1/upload-status":
                        expected = {"upload_id", "manifest_id"}
                        current = provider.uploads.get(upload_id)
                        if set(payload) != expected or self.headers.get("Idempotency-Key") != f"{upload_id}:status" or current is None:
                            self.send_error(400)
                            return
                        if payload.get("manifest_id") != current["init"]["manifest_id"]:
                            self.send_error(409)
                            return
                        state = "uploaded" if current["finalized"] else ("uploading" if current["chunks"] else "initialized")
                        next_offset = provider.invalid_status_offset
                        if next_offset is None:
                            next_offset = current["next_offset"]
                        provider.requests.append({"stage": "status", "payload": payload})
                        response = {
                            "ok": True, "upload_id": upload_id,
                            "manifest_id": current["init"]["manifest_id"],
                            "upload_session_id": current["session"],
                            "state": state, "next_offset": next_offset,
                        }
                        if current["finalized"]:
                            response["provider_media_id"] = current["provider_media_id"]
                    else:
                        self.send_error(404)
                        return
                self._json(200, response)

            def log_message(self, _format: str, *_args: Any) -> None:
                return

        self.server = server_factory(Handler)
        self.thread = threading.Thread(target=self.server.serve_forever, kwargs={'poll_interval': .05}, daemon=True)

    def start(self) -> None:
        self.thread.start()

    def close(self) -> None:
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=3)

    def upload_ids_for(self, media_id: str) -> list[str]:
        return [key for key, value in self.uploads.items() if value["init"].get("media_id") == media_id]

    def exact_upload(self, upload_id: str, content: bytes) -> bool:
        current = self.uploads.get(upload_id)
        if current is None or not current["finalized"]:
            return False
        chunks = [current["chunks"].get(index) for index in range(current["init"]["chunk_count"])]
        if any(not isinstance(item, dict) for item in chunks):
            return False
        rebuilt = b"".join(base64.b64decode(item["content_base64"]) for item in chunks)
        return (
            rebuilt == content
            and current["init"]["content_sha256"] == hashlib.sha256(content).hexdigest()
            and current["next_offset"] == len(content)
        )


class ChannelConnector:
    """Synthetic human-channel adapter with separate accept and verify steps."""

    def __init__(self, server_factory: Callable[[type[BaseHTTPRequestHandler]], Any]) -> None:
        self.token = "evaluator-channel-connector-token"
        self.message_requests: list[dict[str, Any]] = []
        self.receipt_requests: list[str] = []
        self.accepted: dict[str, dict[str, Any]] = {}
        self.drop_response_once: set[str] = set()
        self.drop_next_response = False
        self.dropped: set[str] = set()
        self.outcomes: dict[str, str] = {}
        self.empty_platform_ids: set[str] = set()
        self.partial_media_once: set[str] = set()
        self.media_receipt_polls: dict[str, int] = {}
        self.receipt_schedule: tuple[str, ...] = ()
        self.partial_media_first = False
        self.lock = threading.Lock()
        connector = self

        class Handler(BaseHTTPRequestHandler):
            def _payload(self) -> dict[str, Any] | None:
                try:
                    length = int(self.headers.get("Content-Length", "0"))
                    value = json.loads(self.rfile.read(length))
                    return value if isinstance(value, dict) else None
                except Exception:
                    return None

            def _json(self, status: int, value: dict[str, Any]) -> None:
                body = json.dumps(value, separators=(",", ":")).encode("utf-8")
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def do_POST(self) -> None:  # noqa: N802 - stdlib callback name
                payload = self._payload()
                dispatch_id = (payload or {}).get("dispatch_id")
                if (
                    payload is None
                    or not isinstance(dispatch_id, str) or not 0 < len(dispatch_id.encode()) <= 128
                    or self.headers.get("Authorization") != f"Bearer {connector.token}"
                    or self.headers.get("Idempotency-Key") != dispatch_id
                ):
                    self.send_error(400)
                    return
                if self.path == "/v1/messages":
                    base_fields = {
                        "dispatch_id", "delivery_id", "route", "intent", "disclosure",
                        "media_type", "content_base64", "content_sha256",
                    }
                    if set(payload) not in (
                        base_fields, base_fields | {"media"},
                        base_fields | {"reply_to"}, base_fields | {"media", "reply_to"},
                    ):
                        self.send_error(400)
                        return
                    media = payload.get("media", [])
                    if not isinstance(media, list) or any(not isinstance(item, dict) for item in media):
                        self.send_error(400)
                        return
                    with connector.lock:
                        connector.message_requests.append(payload)
                        duplicate = dispatch_id in connector.accepted
                        if duplicate and connector.accepted[dispatch_id]["payload"] != payload:
                            self.send_error(409)
                            return
                        accepted = connector.accepted.setdefault(dispatch_id, {
                            "payload": payload,
                            "provider_receipt_id": f"provider-receipt-{dispatch_id}",
                        })
                        drop = (
                            (connector.drop_next_response or dispatch_id in connector.drop_response_once)
                            and dispatch_id not in connector.dropped
                        )
                        if drop:
                            connector.dropped.add(dispatch_id)
                            connector.drop_next_response = False
                    if drop:
                        self.close_connection = True
                        return
                    response = {
                        "ok": True,
                        "dispatch_id": dispatch_id,
                        "provider_receipt_id": accepted["provider_receipt_id"],
                        "accepted": True,
                        "duplicate": duplicate,
                    }
                    if media:
                        response["media_receipts"] = [{
                            "media_id": item.get("media_id"),
                            "manifest_id": item.get("manifest_id"),
                            "provider_media_id": item.get("provider_media_id"),
                            "provider_attachment_receipt_id": f"provider-attachment-{dispatch_id}-{item.get('ordinal')}",
                        } for item in media]
                        accepted["media_receipts"] = response["media_receipts"]
                    self._json(202, response)
                    return
                if self.path == "/v1/receipts":
                    base_fields = {"dispatch_id", "delivery_id", "provider_receipt_id"}
                    if set(payload) not in (base_fields, base_fields | {"media_receipts"}):
                        self.send_error(400)
                        return
                    with connector.lock:
                        connector.receipt_requests.append(dispatch_id)
                        accepted = connector.accepted.get(dispatch_id)
                        if accepted is None:
                            self.send_error(404)
                            return
                        if (
                            payload.get("delivery_id")
                            != accepted["payload"].get("delivery_id")
                            or payload.get("provider_receipt_id")
                            != accepted["provider_receipt_id"]
                        ):
                            self.send_error(409)
                            return
                        expected_media = accepted.get("media_receipts", [])
                        if expected_media and payload.get("media_receipts") != expected_media:
                            self.send_error(409)
                            return
                        outcome = connector.outcomes.get(dispatch_id, "verified")
                        platform_id = "" if dispatch_id in connector.empty_platform_ids else f"platform-message-{dispatch_id}"
                        polls = connector.media_receipt_polls.get(dispatch_id, 0)
                        connector.media_receipt_polls[dispatch_id] = polls + 1
                        if connector.receipt_schedule:
                            scheduled = connector.receipt_schedule[min(polls, len(connector.receipt_schedule) - 1)]
                            if scheduled == "empty_platform_id":
                                outcome, platform_id = "verified", ""
                            else:
                                outcome = scheduled
                        returned_media = list(reversed(expected_media))
                        if (dispatch_id in connector.partial_media_once or connector.partial_media_first) and polls == 0:
                            returned_media = returned_media[: max(0, len(returned_media) - 1)]
                            outcome = "unknown"
                            platform_id = ""
                    response = {
                        "ok": True,
                        "dispatch_id": dispatch_id,
                        "delivery_id": payload.get("delivery_id"),
                        "provider_receipt_id": accepted["provider_receipt_id"],
                        "outcome": outcome,
                        "platform_message_id": platform_id if outcome == "verified" else "",
                    }
                    if expected_media:
                        response["media_receipts"] = [{
                            **item,
                            "platform_attachment_id": f"platform-attachment-{dispatch_id}-{item.get('media_id')}",
                        } for item in returned_media]
                    self._json(200, response)
                    return
                self.send_error(404)

            def log_message(self, _format: str, *_args: Any) -> None:
                return

        self.server = server_factory(Handler)
        self.thread = threading.Thread(target=self.server.serve_forever, kwargs={'poll_interval': .05}, daemon=True)

    def start(self) -> None:
        self.thread.start()

    def close(self) -> None:
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=3)

    def dispatch_ids_for(self, delivery_id: str) -> list[str]:
        return [
            dispatch_id for dispatch_id, value in self.accepted.items()
            if value["payload"].get("delivery_id") == delivery_id
        ]

    def exact_request(self, dispatch_id: str, *, delivery_id: str,
                      channel: dict[str, Any], destination_type: str,
                      content: bytes) -> bool:
        accepted = self.accepted.get(dispatch_id)
        if accepted is None:
            return False
        payload = accepted["payload"]
        expected_route = {
            "provider": channel["provider"],
            "account_id": channel["account_id"],
            "peer_id": channel["peer_id"],
            "destination_type": destination_type,
        }
        return (
            payload.get("delivery_id") == delivery_id
            and payload.get("route") == expected_route
            and payload.get("intent") == "user_result"
            and payload.get("content_base64") == base64.b64encode(content).decode("ascii")
            and payload.get("content_sha256") == hashlib.sha256(content).hexdigest()
        )

    def stable_retries(self, dispatch_id: str) -> bool:
        requests = [item for item in self.message_requests if item.get("dispatch_id") == dispatch_id]
        return len(requests) >= 2 and all(item == requests[0] for item in requests[1:])

    def exact_reply(self, dispatch_id: str, interaction_id: str,
                    correlation_delivery_id: str, content: bytes) -> bool:
        accepted = self.accepted.get(dispatch_id)
        if accepted is None:
            return False
        payload = accepted["payload"]
        return (
            payload.get("reply_to") == {
                "interaction_id": interaction_id,
                "correlation_delivery_id": correlation_delivery_id,
            }
            and payload.get("content_base64") == base64.b64encode(content).decode("ascii")
            and payload.get("intent") == "user_result"
        )

    def exact_media_request(self, dispatch_id: str, expected: list[dict[str, Any]]) -> bool:
        accepted = self.accepted.get(dispatch_id)
        if accepted is None:
            return False
        media = accepted["payload"].get("media")
        return media == expected and [item.get("ordinal") for item in media] == list(range(len(expected)))


class InteractionAdapter:
    """Evaluator-owned inbound callback verifier with ambiguous acceptance."""

    def __init__(self, server_factory: Callable[[type[BaseHTTPRequestHandler]], Any]) -> None:
        self.token = "evaluator-interaction-connector-token"
        self.valid_callback_tokens: set[str] = set()
        self.requests: list[dict[str, Any]] = []
        self.status_requests: list[dict[str, Any]] = []
        self.accepted: dict[str, dict[str, Any]] = {}
        self.drop_next_response = False
        self.dropped: set[str] = set()
        self.lock = threading.Lock()
        adapter = self

        class Handler(BaseHTTPRequestHandler):
            def _json(self, status: int, value: dict[str, Any]) -> None:
                body = json.dumps(value, separators=(",", ":")).encode("utf-8")
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def _payload(self) -> dict[str, Any] | None:
                try:
                    length = int(self.headers.get("Content-Length", "0"))
                    value = json.loads(self.rfile.read(length))
                    return value if isinstance(value, dict) else None
                except Exception:
                    return None

            def do_POST(self) -> None:  # noqa: N802
                payload = self._payload()
                if payload is None or self.headers.get("Authorization") != f"Bearer {adapter.token}":
                    self.send_error(400)
                    return
                ingest_id = payload.get("ingest_id")
                if not isinstance(ingest_id, str) or not 0 < len(ingest_id.encode()) <= 128:
                    self.send_error(400)
                    return
                with adapter.lock:
                    if self.path == "/v1/interactions/verify":
                        expected = {
                            "ingest_id", "provider_event_id", "correlation_delivery_id", "kind",
                            "action_id", "route", "content_base64", "content_sha256",
                            "media_type", "destination_type", "callback_token", "occurred_at_ms",
                        }
                        if set(payload) != expected or self.headers.get("Idempotency-Key") != ingest_id:
                            self.send_error(400)
                            return
                        if payload.get("callback_token") not in adapter.valid_callback_tokens:
                            self.send_error(403)
                            return
                        try:
                            body = base64.b64decode(str(payload["content_base64"]), validate=True)
                        except ValueError:
                            self.send_error(400)
                            return
                        if payload.get("content_sha256") != hashlib.sha256(body).hexdigest():
                            self.send_error(409)
                            return
                        prior = adapter.accepted.get(ingest_id)
                        if prior is not None and prior["payload"] != payload:
                            self.send_error(409)
                            return
                        duplicate = prior is not None
                        prior = adapter.accepted.setdefault(ingest_id, {
                            "payload": payload,
                            "provider_interaction_id": f"provider-interaction-{ingest_id}",
                        })
                        adapter.requests.append(payload)
                        if adapter.drop_next_response and ingest_id not in adapter.dropped:
                            adapter.drop_next_response = False
                            adapter.dropped.add(ingest_id)
                            self.close_connection = True
                            return
                        self._json(202, {
                            "ok": True, "ingest_id": ingest_id,
                            "provider_event_id": payload["provider_event_id"],
                            "provider_interaction_id": prior["provider_interaction_id"],
                            "accepted": True, "duplicate": duplicate,
                        })
                        return
                    if self.path == "/v1/interactions/status":
                        if set(payload) != {"ingest_id", "provider_event_id"} or self.headers.get("Idempotency-Key") != f"{ingest_id}:status":
                            self.send_error(400)
                            return
                        prior = adapter.accepted.get(ingest_id)
                        if prior is None or prior["payload"]["provider_event_id"] != payload["provider_event_id"]:
                            self.send_error(404)
                            return
                        adapter.status_requests.append(payload)
                        self._json(200, {
                            "ok": True, "ingest_id": ingest_id,
                            "provider_event_id": payload["provider_event_id"],
                            "state": "accepted",
                            "provider_interaction_id": prior["provider_interaction_id"],
                        })
                        return
                self.send_error(404)

            def log_message(self, _format: str, *_args: Any) -> None:
                return

        self.server = server_factory(Handler)
        self.thread = threading.Thread(target=self.server.serve_forever, kwargs={'poll_interval': .05}, daemon=True)

    def start(self) -> None:
        self.thread.start()

    def close(self) -> None:
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=3)

    def accepted_ids_for(self, provider_event_id: str) -> list[str]:
        return [key for key, value in self.accepted.items() if value["payload"].get("provider_event_id") == provider_event_id]

    def exact(self, ingest_id: str, *, event_id: str, correlation: str,
              route: dict[str, Any], kind: str, action_id: str, body: bytes) -> bool:
        accepted = self.accepted.get(ingest_id)
        if accepted is None:
            return False
        payload = accepted["payload"]
        return (
            payload.get("provider_event_id") == event_id
            and payload.get("correlation_delivery_id") == correlation
            and payload.get("route") == route
            and payload.get("kind") == kind
            and payload.get("action_id") == action_id
            and payload.get("content_base64") == base64.b64encode(body).decode("ascii")
            and payload.get("content_sha256") == hashlib.sha256(body).hexdigest()
        )

    def stable_retries(self, ingest_id: str) -> bool:
        values = [item for item in self.requests if item.get("ingest_id") == ingest_id]
        return len(values) >= 2 and all(item == values[0] for item in values[1:])
