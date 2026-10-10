#!/usr/bin/env python3
"""Evaluator-owned BrowseComp model, search, and page-visit broker.

The provider endpoints, model, reasoning effort and keys come from the evaluator credential file
(written per run by the optimization runner and mounted only into this sidecar), falling back to
the process environment and then to the defaults below:

  AGENTSWE_RUNTIME_API_KEY / AGENTSWE_RUNTIME_RESPONSES_URL / AGENTSWE_RUNTIME_MODEL /
  AGENTSWE_RUNTIME_EFFORT          the locked candidate model (paper: gpt-5.6-sol, effort medium)
  AGENTSWE_SEARCH_API_KEY / AGENTSWE_SEARCH_BASE_URL / AGENTSWE_SEARCH_WIRE
                                   search upstream; wire "legacy-proxy" posts the paper's
                                   {query,page,search_type,token} body to the base URL as given,
                                   wire "serper" posts {q,page} to <base>/search with X-API-KEY.

The candidate-facing protocol, budgets, visit policy and statistics are unchanged from the paper.
"""

from __future__ import annotations

import argparse
import http.server
import http.client
import ipaddress
import json
import os
import socket
import ssl
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path


RESPONSES_UPSTREAM = os.environ.get("AGENTSWE_RUNTIME_RESPONSES_URL", "https://api.deepseek.com/v1/responses")
SEARCH_UPSTREAM = os.environ.get("AGENTSWE_SEARCH_BASE_URL", "https://google.serper.dev")
SEARCH_WIRE = os.environ.get("AGENTSWE_SEARCH_WIRE", "serper")
SEARCH_WIRES = ("serper", "legacy-proxy")
MODEL = os.environ.get("AGENTSWE_RUNTIME_MODEL", "deepseek-flash")
MODEL_EFFORT = os.environ.get("AGENTSWE_RUNTIME_EFFORT", "medium")


def _effort_field(value):
    """Same reading as agentswe_broker.config.normalize_effort (kept in step by tests/test_broker_effort_mapping.py):
    none/off/unset/empty leave the effort out; explicit-none sends the literal "none"; anything else as given."""
    if value is None:
        return None
    text = str(value).strip()
    if text.lower() in ("", "none", "off", "unset"):
        return None
    if text.lower() == "explicit-none":
        return "none"
    return text
CANDIDATE_TOKEN = "broker-only-placeholder"
STATS_TOKEN = "stats-only-placeholder"
MAX_BODY = 8 * 1024 * 1024
MAX_VISIT_BODY = 2 * 1024 * 1024
MAX_TRANSPORT_ATTEMPTS = 5
LIMITS = {"model": 20, "search": 20, "visit": 20}


def dotenv(path: Path) -> dict[str, str]:
    result: dict[str, str] = {}
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if line and not line.startswith("#") and "=" in line:
            key, value = line.split("=", 1)
            result[key.strip()] = value.strip().strip("'\"")
    return result


def response_tokens(payload: bytes) -> int:
    try:
        value = json.loads(payload)
    except (UnicodeDecodeError, json.JSONDecodeError):
        return 0
    usage = value.get("usage") if isinstance(value, dict) else None
    if not isinstance(usage, dict):
        return 0
    total = usage.get("total_tokens") or usage.get("totalTokens")
    if isinstance(total, (int, float)):
        return int(total)
    inputs = usage.get("input_tokens") or usage.get("inputTokens") or 0
    outputs = usage.get("output_tokens") or usage.get("outputTokens") or 0
    if isinstance(inputs, (int, float)) and isinstance(outputs, (int, float)):
        return int(inputs + outputs)
    return 0


def strip_reasoning_text(payload: bytes) -> tuple[bytes, int, int]:
    """Drop the provider's reasoning text from a Responses result relayed to the Candidate.

    Some providers return their reasoning as text (a "reasoning" output item with reasoning_text or
    summary_text parts); the paper provider returned none, so paper Candidates saw only the answer
    items. Reasoning items that carry text are removed, and reasoning_text parts elsewhere are
    dropped; message items and usage keep their values. A result without reasoning text is relayed
    byte for byte. Returns (payload, items removed, characters removed).
    """
    try:
        value = json.loads(payload)
    except (UnicodeDecodeError, json.JSONDecodeError):
        return payload, 0, 0
    output = value.get("output") if isinstance(value, dict) else None
    if not isinstance(output, list):
        return payload, 0, 0

    def text_parts(item: dict, key: str) -> list[dict]:
        parts = item.get(key)
        return [x for x in parts if isinstance(x, dict) and isinstance(x.get("text"), str) and x["text"]] if isinstance(parts, list) else []

    kept: list[object] = []
    items = chars = 0
    for item in output:
        if isinstance(item, dict) and item.get("type") == "reasoning":
            texts = text_parts(item, "content") + text_parts(item, "summary")
            if texts:
                items += 1
                chars += sum(len(x["text"]) for x in texts)
                continue
        if isinstance(item, dict) and isinstance(item.get("content"), list):
            parts = [x for x in item["content"] if not (isinstance(x, dict) and x.get("type") == "reasoning_text")]
            if len(parts) != len(item["content"]):
                chars += sum(len(x.get("text", "")) for x in item["content"]
                             if isinstance(x, dict) and x.get("type") == "reasoning_text")
                item = dict(item, content=parts)
        kept.append(item)
    if not items and not chars:
        return payload, 0, 0
    value["output"] = kept
    return json.dumps(value, ensure_ascii=False).encode("utf-8"), items, chars


def locked_model_input(value: object) -> str | list[dict[str, object]]:
    if isinstance(value, str):
        if len(value.encode("utf-8")) > MAX_BODY:
            raise ValueError("model input is too large")
        return value
    if not isinstance(value, list) or not value:
        raise ValueError("model input must be text or non-empty messages")
    messages: list[dict[str, object]] = []
    for item in value:
        if not isinstance(item, dict) or item.get("type") not in {None, "message"}:
            raise ValueError("model input supports messages only")
        role = item.get("role")
        if role not in {"user", "assistant", "developer", "system"}:
            raise ValueError("unsupported model message role")
        content = item.get("content")
        if isinstance(content, str):
            locked_content: object = content
        elif isinstance(content, list) and content:
            locked_parts: list[dict[str, str]] = []
            for part in content:
                if not isinstance(part, dict) or part.get("type") not in {"input_text", "output_text"} or not isinstance(part.get("text"), str):
                    raise ValueError("model content supports text only")
                locked_parts.append({"type": str(part["type"]), "text": str(part["text"])})
            locked_content = locked_parts
        else:
            raise ValueError("model message content must be text")
        messages.append({"type": "message", "role": role, "content": locked_content})
    return messages


def public_url(value: object) -> str:
    if not isinstance(value, str) or len(value) > 8192:
        raise ValueError("visit URL must be a string")
    parsed = urllib.parse.urlsplit(value)
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        raise ValueError("visit URL must use http or https")
    if parsed.username or parsed.password:
        raise ValueError("visit URL must not contain userinfo")
    try:
        addresses = socket.getaddrinfo(parsed.hostname, parsed.port, type=socket.SOCK_STREAM)
    except socket.gaierror as exc:
        raise ValueError("visit hostname did not resolve") from exc
    if not addresses:
        raise ValueError("visit hostname did not resolve")
    for address in addresses:
        ip = ipaddress.ip_address(address[4][0])
        if not ip.is_global:
            raise ValueError("visit URL resolves to a non-public address")
    return value


def public_addresses(url: str) -> tuple[urllib.parse.SplitResult, list[str]]:
    if not isinstance(url, str) or len(url) > 8192:
        raise ValueError("visit URL must be a string")
    parsed = urllib.parse.urlsplit(url)
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        raise ValueError("visit URL must use http or https")
    if parsed.username or parsed.password:
        raise ValueError("visit URL must not contain userinfo")
    addresses = socket.getaddrinfo(
        parsed.hostname, parsed.port or (443 if parsed.scheme == "https" else 80),
        type=socket.SOCK_STREAM,
    )
    result: list[str] = []
    for address in addresses:
        value = address[4][0]
        if not ipaddress.ip_address(value).is_global:
            raise ValueError("visit URL resolves to a non-public address")
        if value not in result:
            result.append(value)
    return parsed, result


class PinnedHTTPConnection(http.client.HTTPConnection):
    def __init__(self, host: str, connect_ip: str, port: int, timeout: float) -> None:
        super().__init__(host, port=port, timeout=timeout)
        self.connect_ip = connect_ip

    def connect(self) -> None:
        self.sock = socket.create_connection(
            (self.connect_ip, self.port), self.timeout, self.source_address
        )


class PinnedHTTPSConnection(http.client.HTTPSConnection):
    def __init__(self, host: str, connect_ip: str, port: int, timeout: float) -> None:
        super().__init__(host, port=port, timeout=timeout)
        self.connect_ip = connect_ip

    def connect(self) -> None:
        sock = socket.create_connection(
            (self.connect_ip, self.port), self.timeout, self.source_address
        )
        self.sock = self._context.wrap_socket(sock, server_hostname=self.host)


def visit_page(url: str, timeout: float = 30.0) -> dict[str, object]:
    current = url
    for _ in range(6):
        parsed, addresses = public_addresses(current)
        response: http.client.HTTPResponse | None = None
        last_error: OSError | None = None
        port = parsed.port or (443 if parsed.scheme == "https" else 80)
        target = urllib.parse.urlunsplit(("", "", parsed.path or "/", parsed.query, ""))
        for address in addresses:
            connection = (
                PinnedHTTPSConnection(parsed.hostname or "", address, port, timeout)
                if parsed.scheme == "https"
                else PinnedHTTPConnection(parsed.hostname or "", address, port, timeout)
            )
            try:
                connection.request(
                    "GET", target,
                    headers={
                        "Accept": "text/html,text/plain,application/xhtml+xml,application/json;q=0.8,*/*;q=0.2",
                        "User-Agent": "AgentSWE-BrowseComp-Visit/1.0",
                    },
                )
                response = connection.getresponse()
                break
            except OSError as exc:
                last_error = exc
                connection.close()
        if response is None:
            raise last_error or OSError("all validated public addresses failed")
        try:
            location = response.headers.get("Location")
            if response.status in {301, 302, 303, 307, 308} and location:
                current = urllib.parse.urljoin(current, location)
                continue
            payload = response.read(MAX_VISIT_BODY + 1)
            truncated = len(payload) > MAX_VISIT_BODY
            payload = payload[:MAX_VISIT_BODY]
            content_type = response.headers.get("Content-Type", "application/octet-stream")
            charset = response.headers.get_content_charset() or "utf-8"
            text = payload.decode(charset, errors="replace")
            return {
                "url": current,
                "status": response.status,
                "content_type": content_type,
                "text": text,
                "truncated": truncated,
            }
        finally:
            response.close()
    raise ValueError("visit redirect limit exceeded")


class Handler(http.server.BaseHTTPRequestHandler):
    server_version = "AgentSWEBrowseCompBroker/1.0"

    def do_POST(self) -> None:  # noqa: N802
        if self.path.rstrip("/") == "/finalize":
            client = ipaddress.ip_address(self.client_address[0])
            if not client.is_loopback or self.headers.get("Authorization", "") != f"Bearer {STATS_TOKEN}":
                self.send_error(401)
                return
            self.server.finalize()  # type: ignore[attr-defined]
            self._json(200, self.server.public_stats())  # type: ignore[attr-defined]
        elif self.path.rstrip("/") == "/v1/responses":
            self._responses()
        elif self.path.rstrip("/") == "/serp_search_v1":
            self._search()
        elif self.path.rstrip("/") == "/visit":
            self._visit()
        else:
            self.send_error(404)

    def do_GET(self) -> None:  # noqa: N802
        parsed = urllib.parse.urlsplit(self.path)
        if parsed.path == "/healthz":
            self._json(200, {"status": "ok"})
            return
        if parsed.path == "/stats":
            if self.headers.get("Authorization", "") != f"Bearer {STATS_TOKEN}":
                self.send_error(401)
                return
            self._json(200, self.server.public_stats())  # type: ignore[attr-defined]
            return
        if parsed.path == "/visit":
            values = urllib.parse.parse_qs(parsed.query)
            self._visit(values.get("url", [""])[0])
            return
        self.send_error(404)

    def _candidate_authorized(self, body: dict[str, object] | None = None) -> bool:
        auth = self.headers.get("Authorization", "")
        if auth == f"Bearer {CANDIDATE_TOKEN}":
            return True
        return bool(body and body.get("token") == CANDIDATE_TOKEN)

    def _read_json(self) -> dict[str, object] | None:
        try:
            length = int(self.headers.get("Content-Length", "0"))
        except ValueError:
            length = 0
        if length <= 0 or length > MAX_BODY:
            self.send_error(413)
            return None
        try:
            value = json.loads(self.rfile.read(length))
        except (UnicodeDecodeError, json.JSONDecodeError):
            self.send_error(400)
            return None
        if not isinstance(value, dict):
            self.send_error(400)
            return None
        return value

    def _reserve(self, kind: str) -> bool:
        allowed = self.server.reserve(kind)  # type: ignore[attr-defined]
        if not allowed:
            self._json(429, {"error": {"type": "budget_exceeded", "message": f"{kind}_budget_exceeded"}})
        return allowed

    def _responses(self) -> None:
        body = self._read_json()
        if body is None:
            return
        if not self._candidate_authorized(body):
            self.send_error(401)
            return
        if "input" not in body:
            self.server.client_error("model")  # type: ignore[attr-defined]
            self.send_error(400)
            return
        try:
            locked_input = locked_model_input(body["input"])
        except ValueError:
            self.server.client_error("model")  # type: ignore[attr-defined]
            self.send_error(400)
            return
        if not self._reserve("model"):
            return
        body = {"model": MODEL, "input": locked_input}
        effort = _effort_field(MODEL_EFFORT)
        if effort is not None:
            body["reasoning"] = {"effort": effort}
        self._forward(
            kind="model",
            endpoint=RESPONSES_UPSTREAM,
            body=body,
            authorization=f"Bearer {self.server.runtime_key}",  # type: ignore[attr-defined]
            token_counter=response_tokens,
        )

    def _search(self) -> None:
        body = self._read_json()
        if body is None:
            return
        if not self._candidate_authorized(body):
            self.send_error(401)
            return
        query = body.get("query")
        if not isinstance(query, str) or not query.strip():
            self.server.client_error("search")  # type: ignore[attr-defined]
            self.send_error(400)
            return
        page = body.get("page", 1)
        search_type = body.get("search_type", "search")
        if not isinstance(page, int) or not 1 <= page <= 20 or search_type != "search":
            self.server.client_error("search")  # type: ignore[attr-defined]
            self.send_error(400)
            return
        if not self._reserve("search"):
            return
        key = self.server.search_key  # type: ignore[attr-defined]
        if SEARCH_WIRE == "serper":
            self._forward(kind="search", endpoint=SEARCH_UPSTREAM.rstrip("/") + "/search",
                          body={"q": query, "page": page}, extra_headers={"X-API-KEY": key})
        else:
            body = {"query": query, "page": page, "search_type": "search", "token": key}
            self._forward(kind="search", endpoint=SEARCH_UPSTREAM, body=body)

    def _visit(self, explicit_url: str | None = None) -> None:
        body: dict[str, object] = {}
        if explicit_url is None:
            value = self._read_json()
            if value is None:
                return
            body = value
            explicit_url = str(body.get("url", ""))
        if not self._candidate_authorized(body):
            self.send_error(401)
            return
        if not self._reserve("visit"):
            return
        started = time.monotonic()
        try:
            value = visit_page(explicit_url)
        except (ValueError, urllib.error.URLError, TimeoutError, OSError, ssl.SSLError) as exc:
            self.server.tool_error("visit")  # type: ignore[attr-defined]
            self.server.finish("visit", 0, None)  # type: ignore[attr-defined]
            self._json(422, {"error": {"type": "visit_error", "message": type(exc).__name__}})
            return
        self.server.finish("visit", 0, None)  # type: ignore[attr-defined]
        value["runtime_seconds"] = round(time.monotonic() - started, 3)
        self._json(200, value)

    def _forward(
        self,
        *,
        kind: str,
        endpoint: str,
        body: dict[str, object],
        authorization: str | None = None,
        token_counter=None,
        extra_headers: dict[str, str] | None = None,
    ) -> None:
        headers = {"Content-Type": "application/json", "Accept": "application/json", "User-Agent": "AgentSWE-BrowseComp-Broker/1.0"}
        if authorization:
            headers["Authorization"] = authorization
        headers.update(extra_headers or {})
        encoded = json.dumps(body, ensure_ascii=False).encode("utf-8")
        last_error = "unknown"
        payload: bytes | None = None
        status = 502
        content_type = "application/json"
        infrastructure_failure = False
        for attempt in range(1, MAX_TRANSPORT_ATTEMPTS + 1):
            request = urllib.request.Request(endpoint, data=encoded, headers=headers, method="POST")
            try:
                with urllib.request.urlopen(request, timeout=240) as response:
                    payload = response.read(MAX_BODY)
                    status = response.status
                    content_type = response.headers.get("Content-Type", "application/json")
                break
            except urllib.error.HTTPError as exc:
                last_error = f"http_{exc.code}"
                if exc.code in {400, 404, 409, 422}:
                    payload = exc.read(MAX_BODY)
                    status = exc.code
                    content_type = exc.headers.get("Content-Type", "application/json")
                    self.server.client_error(kind)  # type: ignore[attr-defined]
                    break
                infrastructure_failure = True
                if exc.code not in {429, 500, 502, 503, 504}:
                    break
            except urllib.error.URLError as exc:
                infrastructure_failure = True
                last_error = f"transport_{type(exc.reason).__name__}"
            except (TimeoutError, OSError, ssl.SSLError) as exc:
                infrastructure_failure = True
                last_error = f"transport_{type(exc).__name__}"
            if attempt < MAX_TRANSPORT_ATTEMPTS:
                time.sleep(min(8.0, float(2 ** (attempt - 1))))
        if payload is None:
            self.server.finish(kind, 0, last_error if infrastructure_failure else None, attempt)  # type: ignore[attr-defined]
            self._json(502, {"error": {"type": "upstream_error", "message": last_error, "attempts": attempt}})
            return
        tokens = token_counter(payload) if token_counter else 0
        if kind == "model" and status == 200:
            payload, stripped_items, stripped_chars = strip_reasoning_text(payload)
            self.server.stripped(stripped_items, stripped_chars)  # type: ignore[attr-defined]
        self.server.finish(kind, tokens, None, attempt)  # type: ignore[attr-defined]
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def _json(self, status: int, value: object) -> None:
        payload = json.dumps(value, ensure_ascii=False, sort_keys=True).encode("utf-8") + b"\n"
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def log_message(self, format: str, *args: object) -> None:
        return


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--credential-file", type=Path)
    parser.add_argument("--finalize-output", type=Path)
    parser.add_argument("--case-id", default=os.environ.get("CASE_ID", ""))
    parser.add_argument("--bind", default="0.0.0.0")
    parser.add_argument("--port", type=int, default=8080)
    args = parser.parse_args()
    if args.finalize_output is not None:
        request = urllib.request.Request(
            f"http://127.0.0.1:{args.port}/finalize",
            data=b"{}",
            headers={"Authorization": f"Bearer {STATS_TOKEN}"},
            method="POST",
        )
        with urllib.request.urlopen(request, timeout=310) as response:
            stats = json.loads(response.read())
        args.finalize_output.parent.mkdir(parents=True, exist_ok=True)
        args.finalize_output.write_text(
            json.dumps({
                "schema_version": "1.0",
                "case_id": args.case_id,
                "resource_mode": "brokered-v1",
                "broker_stats": stats,
                "broker_stats_error": None,
                "evidence_owner": "harbor-sidecar-collect",
            }, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        return 0
    if args.credential_file is None:
        raise SystemExit("--credential-file is required in server mode")
    credentials = dotenv(args.credential_file)
    global RESPONSES_UPSTREAM, SEARCH_UPSTREAM, SEARCH_WIRE, MODEL, MODEL_EFFORT
    RESPONSES_UPSTREAM = credentials.get("AGENTSWE_RUNTIME_RESPONSES_URL") or RESPONSES_UPSTREAM
    MODEL = credentials.get("AGENTSWE_RUNTIME_MODEL") or MODEL
    MODEL_EFFORT = credentials.get("AGENTSWE_RUNTIME_EFFORT") or MODEL_EFFORT
    SEARCH_UPSTREAM = credentials.get("AGENTSWE_SEARCH_BASE_URL") or SEARCH_UPSTREAM
    SEARCH_WIRE = (credentials.get("AGENTSWE_SEARCH_WIRE") or SEARCH_WIRE).lower()
    if SEARCH_WIRE not in SEARCH_WIRES:
        raise SystemExit(f"AGENTSWE_SEARCH_WIRE must be one of {SEARCH_WIRES}")
    runtime_key = credentials.get("AGENTSWE_RUNTIME_API_KEY", "")
    search_key = credentials.get("AGENTSWE_SEARCH_API_KEY", "")
    if not runtime_key or not search_key:
        raise SystemExit("required BrowseComp credentials missing")

    class BrokerServer(http.server.ThreadingHTTPServer):
        def __init__(self, address: tuple[str, int]) -> None:
            super().__init__(address, Handler)
            self.lock = threading.Lock()
            self.condition = threading.Condition(self.lock)
            self.finalized = False
            self.stats = {
                kind: {"calls": 0, "tokens": 0, "failures": 0, "client_errors": 0, "tool_errors": 0, "budget_exceeded": False, "last_error": None, "transport_attempts": 0, "inflight": 0}
                for kind in LIMITS
            }
            self.reasoning_stripped = {"items": 0, "chars": 0, "responses": 0}

        def reserve(self, kind: str) -> bool:
            with self.lock:
                current = self.stats[kind]
                if self.finalized:
                    current["budget_exceeded"] = True
                    return False
                if current["calls"] >= LIMITS[kind]:
                    current["budget_exceeded"] = True
                    return False
                current["calls"] += 1
                current["inflight"] += 1
                return True

        def finalize(self) -> None:
            with self.condition:
                self.finalized = True
                self.condition.notify_all()

        def finish(self, kind: str, tokens: int, error: str | None, attempts: int = 1) -> None:
            with self.lock:
                self.stats[kind]["tokens"] += tokens
                self.stats[kind]["transport_attempts"] += attempts
                if error:
                    self.stats[kind]["failures"] += 1
                    self.stats[kind]["last_error"] = error
                self.stats[kind]["inflight"] = max(0, self.stats[kind]["inflight"] - 1)
                self.condition.notify_all()

        def stripped(self, items: int, chars: int) -> None:
            if items or chars:
                with self.lock:
                    self.reasoning_stripped["items"] += items
                    self.reasoning_stripped["chars"] += chars
                    self.reasoning_stripped["responses"] += 1

        def client_error(self, kind: str) -> None:
            with self.lock:
                self.stats[kind]["client_errors"] += 1

        def tool_error(self, kind: str) -> None:
            with self.lock:
                self.stats[kind]["tool_errors"] += 1

        def public_stats(self) -> dict[str, object]:
            deadline = time.monotonic() + 300.0
            with self.condition:
                while any(value["inflight"] for value in self.stats.values()):
                    remaining = deadline - time.monotonic()
                    if remaining <= 0:
                        break
                    self.condition.wait(timeout=remaining)
                return {
                    "schema_version": "1.0",
                    "model": MODEL,
                    "model_effort": MODEL_EFFORT,
                    "search_wire": SEARCH_WIRE,
                    "candidate_reasoning_text_stripped": dict(self.reasoning_stripped),
                    "limits": dict(LIMITS),
                    "resources": {kind: dict(value) for kind, value in self.stats.items()},
                    "all_requests_settled": not any(value["inflight"] for value in self.stats.values()),
                    "finalized": self.finalized,
                    "credential_brokered": True,
                    "candidate_secret_exposed": False,
                }

    server = BrokerServer((args.bind, args.port))
    server.runtime_key = runtime_key  # type: ignore[attr-defined]
    server.search_key = search_key  # type: ignore[attr-defined]
    server.serve_forever()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
