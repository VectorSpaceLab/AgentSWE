"""Offline test helpers: a scripted fake provider and an in-process broker.

No network beyond 127.0.0.1, no real credentials (the fake key below is a
test constant that the tests assert never leaks into stats, ledgers or replies).
"""
from __future__ import annotations

import http.server
import json
import tempfile
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path

from agentswe_broker.server import BrokerServer, Options

FAKE_KEY = "sk-test-FAKEKEY-0123456789abcdef"


class Script:
    """One scripted upstream reply."""

    def __init__(self, status=200, body=b"", *, content_type="application/json", chunks=None,
                 delay=0.0, chunk_delay=0.0, headers=None):
        self.status, self.body, self.content_type = status, body, content_type
        self.chunks, self.delay, self.chunk_delay = chunks, delay, chunk_delay
        self.headers = headers or {}


class FakeProvider:
    """Records every request; answers from a FIFO of Script objects."""

    def __init__(self):
        self.requests: list[dict] = []
        self.scripts: list[Script] = []
        self.lock = threading.Lock()
        provider = self

        class Handler(http.server.BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def log_message(self, *_args):
                pass

            def do_POST(self):  # noqa: N802
                length = int(self.headers.get("Content-Length", "0"))
                raw = self.rfile.read(length)
                with provider.lock:
                    provider.requests.append({"path": self.path, "headers": dict(self.headers),
                                              "body": json.loads(raw) if raw else None})
                    script = provider.scripts.pop(0) if provider.scripts else Script(500, b'{"error":"unscripted"}')
                if script.delay:
                    time.sleep(script.delay)
                self.send_response(script.status)
                self.send_header("Content-Type", script.content_type)
                for key, value in script.headers.items():
                    self.send_header(key, value)
                if script.chunks is not None:
                    self.send_header("Transfer-Encoding", "chunked")
                    self.end_headers()
                    for chunk in script.chunks:
                        if script.chunk_delay:
                            time.sleep(script.chunk_delay)
                        self.wfile.write(b"%x\r\n" % len(chunk) + chunk + b"\r\n")
                        self.wfile.flush()
                    self.wfile.write(b"0\r\n\r\n")
                    self.wfile.flush()
                else:
                    self.send_header("Content-Length", str(len(script.body)))
                    self.end_headers()
                    self.wfile.write(script.body)
                    self.wfile.flush()

        self.server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.server.daemon_threads = True
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    @property
    def base(self) -> str:
        return "http://127.0.0.1:%d/v1" % self.server.server_address[1]

    def add(self, *scripts: Script) -> None:
        with self.lock:
            self.scripts.extend(scripts)

    def close(self) -> None:
        self.server.shutdown()
        self.server.server_close()


class RunningBroker:
    def __init__(self, options: Options, key: str = FAKE_KEY):
        self.server = BrokerServer(("127.0.0.1", 0), options, key)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    @property
    def url(self) -> str:
        return "http://127.0.0.1:%d" % self.server.server_address[1]

    def close(self) -> None:
        self.server.shutdown()
        self.server.server_close()

    def post(self, path, body, *, token="broker-only-placeholder", stream_raw=False, headers=None,
             timeout=30):
        data = json.dumps(body).encode()
        all_headers = {"Content-Type": "application/json", "Authorization": "Bearer %s" % token}
        all_headers.update(headers or {})
        request = urllib.request.Request(self.url + path, data=data, headers=all_headers, method="POST")
        try:
            with urllib.request.urlopen(request, timeout=timeout) as response:
                return response.status, dict(response.headers), response.read()
        except urllib.error.HTTPError as exc:
            return exc.code, dict(exc.headers), exc.read()

    def stats(self, token="stats-only-placeholder"):
        request = urllib.request.Request(self.url + "/stats", headers={"Authorization": "Bearer %s" % token})
        with urllib.request.urlopen(request, timeout=10) as response:
            return json.loads(response.read())


def tmpdir() -> Path:
    return Path(tempfile.mkdtemp(prefix="oss-op-broker-test-"))


def sse(*events: dict, done: bool = True) -> bytes:
    out = b""
    for event in events:
        out += ("event: %s\ndata: %s\n\n" % (event["type"], json.dumps(event))).encode()
    if done:
        out += b"data: [DONE]\n\n"
    return out


def responses_completed(text="ok", *, model="deepseek-flash", status="completed", usage=None, with_reasoning=True):
    output = []
    if with_reasoning:
        output.append({"type": "reasoning", "id": "rs_1", "summary": [],
                       "content": [{"type": "reasoning_text", "text": "THINKING-SHOULD-NOT-LEAK"}]})
    output.append({"type": "message", "id": "msg_1", "role": "assistant", "status": "completed",
                   "content": [{"type": "output_text", "text": text, "annotations": []}]})
    return {"id": "resp_up_1", "object": "response", "status": status, "model": model, "output": output,
            "usage": usage or {"input_tokens": 10, "output_tokens": 5, "total_tokens": 15,
                               "input_tokens_details": {"cached_tokens": 4},
                               "output_tokens_details": {"reasoning_tokens": 2}}}


def responses_stream_bytes(text="hi", model="deepseek-flash"):
    final = responses_completed(text, model=model, with_reasoning=False)
    return sse({"type": "response.created", "sequence_number": 0,
                "response": dict(final, status="in_progress", output=[])},
               {"type": "response.output_text.delta", "sequence_number": 1, "item_id": "msg_1",
                "output_index": 0, "content_index": 0, "delta": text},
               {"type": "response.completed", "sequence_number": 2, "response": final})


def chat_chunk(delta=None, finish=None, usage=None, index=0):
    value = {"id": "c1", "object": "chat.completion.chunk", "created": 1, "model": "up",
             "choices": [] if delta is None and finish is None else [
                 {"index": index, "delta": delta or {}, "finish_reason": finish}]}
    if usage is not None:
        value["usage"] = usage
    return b"data: " + json.dumps(value).encode() + b"\n\n"


CHAT_USAGE = {"prompt_tokens": 20, "completion_tokens": 7, "total_tokens": 27,
              "prompt_tokens_details": {"cached_tokens": 3},
              "completion_tokens_details": {"reasoning_tokens": 4}}


def options(role, wire, provider: FakeProvider, workdir: Path, **kw) -> Options:
    route = "/responses" if wire == "responses" else "/chat/completions" if wire == "chat" else ""
    base = dict(role=role, upstream_wire=wire, provider_url=provider.base + route,
                model="deepseek-flash", effort={"builder": "max", "runtime": "high", "judge": "max"}.get(role),
                stats_file=workdir / ("%s_stats.json" % role),
                ledger_dir=None if role == "builder" else workdir / "ledger",
                keepalive_seconds=0.2)
    if role == "builder":
        base["rate_limit_policy"] = "as_503"
    base.update(kw)
    return Options(**base)


def assert_no_key(testcase, *paths: Path, blobs=()):
    for blob in blobs:
        testcase.assertNotIn(FAKE_KEY.encode() if isinstance(blob, bytes) else FAKE_KEY, blob)
    for root in paths:
        for path in Path(root).rglob("*"):
            if path.is_file():
                testcase.assertNotIn(FAKE_KEY.encode(), path.read_bytes(), str(path))
