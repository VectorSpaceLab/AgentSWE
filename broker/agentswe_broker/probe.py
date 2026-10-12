"""Capability probe for one role, through the exact broker code path (for `agentswe doctor`).

Starts the role's broker in-process on loopback (ledger in a temporary directory),
sends up to three tiny requests and reports, per check: HTTP status, terminal
status, whether the provider echoed the pinned model name, whether usage came
back, and the error class.  Three to four short calls per role; nothing is
printed but the report (no key, no prompt, no completion text beyond a verdict).
"""
from __future__ import annotations

import base64
import json
import struct
import tempfile
import threading
import urllib.error
import urllib.request
import zlib
from pathlib import Path

from .server import BrokerServer, Options

def png_1x1() -> bytes:
    """A valid 1x1 white RGBA PNG, built with correct CRCs."""
    def chunk(kind: bytes, data: bytes) -> bytes:
        return struct.pack(">I", len(data)) + kind + data + struct.pack(">I", zlib.crc32(kind + data) & 0xFFFFFFFF)
    header = struct.pack(">IIBBBBB", 1, 1, 8, 6, 0, 0, 0)
    return (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", header)
            + chunk(b"IDAT", zlib.compress(b"\x00\xff\xff\xff\xff")) + chunk(b"IEND", b""))


PNG_1X1 = base64.b64encode(png_1x1()).decode()
# Reasoning at maximum effort can spend thousands of tokens even on a one-word
# answer (DeepSeek at max returned `incomplete` under small caps), so the probe
# leaves generous room; the prompts are tiny, so the cost stays negligible.
PROBE_MAX_OUTPUT_TOKENS = 16000


def _post(url: str, body: dict, token: str, timeout: float) -> tuple[int, dict | None, dict]:
    request = urllib.request.Request(url, data=json.dumps(body).encode(), method="POST",
                                     headers={"Authorization": "Bearer " + token, "Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return response.status, json.loads(response.read() or b"null"), dict(response.headers)
    except urllib.error.HTTPError as exc:
        try:
            value = json.loads(exc.read() or b"null")
        except ValueError:
            value = None
        return exc.code, value, dict(exc.headers)
    except Exception as exc:  # noqa: BLE001 - reported, never raised
        return 0, {"error": type(exc).__name__}, {}


def _message_text(response: dict) -> str:
    for item in response.get("output") or []:
        if isinstance(item, dict) and item.get("type") == "message":
            return "".join(part.get("text", "") for part in item.get("content") or [] if isinstance(part, dict))
    return ""


def run_probe(options: Options, key: str, *, image: bool = False, timeout: float = 300.0,
              minimal: bool = False) -> dict:
    """minimal: only the basic check (one request), for `agentswe probe-roles`."""
    work = Path(tempfile.mkdtemp(prefix="agentswe-probe-"))
    options.stats_file = work / "stats.json"
    options.ledger_dir = work / "ledger"
    requested_role = options.role
    options.role = "judge" if options.role == "builder" else options.role  # probe never needs the Builder ledger
    server = BrokerServer(("127.0.0.1", 0), options, key)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    url = "http://127.0.0.1:%d/v1/responses" % server.server_address[1]
    token = options.client_tokens[0]
    checks: dict[str, dict] = {}
    try:
        def check(name: str, body: dict, verdict) -> None:
            status, value, headers = _post(url, body, token, timeout)
            result = {"http_status": status}
            if isinstance(value, dict) and status == 200:
                result.update({"response_status": value.get("status"),
                               "model_echo": value.get("model") == options.model,
                               "reported_model": value.get("model") if value.get("model") != options.model else None,
                               "usage_present": isinstance(value.get("usage"), dict)})
                result["ok"] = value.get("status") == "completed" and verdict(_message_text(value))
            else:
                result.update({"ok": False, "error": (value or {}).get("error") if isinstance(value, dict) else None})
            checks[name] = result

        base_input = [{"type": "message", "role": "user",
                       "content": [{"type": "input_text", "text": "Reply with the single word OK."}]}]
        check("basic", {"input": base_input, "max_output_tokens": PROBE_MAX_OUTPUT_TOKENS}, lambda text: "ok" in text.lower())
        if minimal:
            return {"role": requested_role, "model": options.model, "effort": options.effort,
                    "upstream_wire": options.upstream_wire, "checks": checks,
                    "ok": all(item.get("ok") for item in checks.values())}
        schema = {"type": "object", "additionalProperties": False, "required": ["ok"],
                  "properties": {"ok": {"type": "boolean"}}}
        check("json_schema", {"input": [{"type": "message", "role": "user", "content": [
            {"type": "input_text", "text": 'Return the JSON object {"ok": true}.'}]}],
            "max_output_tokens": PROBE_MAX_OUTPUT_TOKENS,
            "text": {"format": {"type": "json_schema", "name": "probe", "strict": True, "schema": schema}}},
            lambda text: _json_ok(text))
        if image:
            check("image_input", {"input": [{"type": "message", "role": "user", "content": [
                {"type": "input_text", "text": "Is there an image attached? Answer yes or no."},
                {"type": "input_image", "image_url": "data:image/png;base64," + PNG_1X1}]}],
                "max_output_tokens": PROBE_MAX_OUTPUT_TOKENS}, lambda text: bool(text.strip()))
    finally:
        server.shutdown()
        server.server_close()
    return {"role": requested_role, "model": options.model, "effort": options.effort,
            "upstream_wire": options.upstream_wire, "checks": checks,
            "ok": all(item.get("ok") for item in checks.values())}


def _json_ok(text: str) -> bool:
    try:
        value = json.loads(text.strip().strip("`").removeprefix("json").strip())
    except ValueError:
        return False
    return isinstance(value, dict) and value.get("ok") is True


def run_search_probe(options: Options, key: str, *, timeout: float = 120.0) -> dict:
    """One search request through the search role's broker (legacy-proxy or serper wire)."""
    work = Path(tempfile.mkdtemp(prefix="agentswe-probe-"))
    options.stats_file = work / "stats.json"
    options.ledger_dir = work / "ledger"
    server = BrokerServer(("127.0.0.1", 0), options, key)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    url = "http://127.0.0.1:%d/serp_search_v1" % server.server_address[1]
    body = {"query": "Python programming language", "page": 1, "search_type": "search",
            "token": options.client_tokens[0]}
    try:
        request = urllib.request.Request(url, data=json.dumps(body).encode(), method="POST",
                                         headers={"Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(request, timeout=timeout) as response:
                status, value = response.status, json.loads(response.read() or b"null")
        except urllib.error.HTTPError as exc:
            status, value = exc.code, None
        except Exception as exc:  # noqa: BLE001 - reported, never raised
            status, value = 0, {"error": type(exc).__name__}
    finally:
        server.shutdown()
        server.server_close()
    organic = value.get("organic") if isinstance(value, dict) else None
    result = {"http_status": status, "organic_results": len(organic) if isinstance(organic, list) else None}
    result["ok"] = status == 200 and bool(organic)
    if not result["ok"] and isinstance(value, dict) and value.get("error"):
        result["error"] = value.get("error")
    return {"role": "search", "upstream_wire": options.upstream_wire, "checks": {"search": result}, "ok": result["ok"]}
