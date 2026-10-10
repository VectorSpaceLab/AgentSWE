"""Optimization evaluator brokers take their provider from the runner's credential file.

    python3 -m unittest tests/test_optimization_provider_config.py   (standard library only; no model calls)

* responses_broker.py (tau3, PinchBench, TerminalBench runtimes) locks the Responses URL, model and
  efforts written next to the RUNTIME key; without them it keeps its Lite values. --chat-role runtime
  treats the chat-completions path as an agent runtime (runtime effort, no judge output floor).
* browsecomp_broker.py locks model and effort the same way and forwards searches over the configured
  wire: "legacy-proxy" (the paper's {query,page,search_type,token} body) or "serper" ({q,page} plus
  X-API-KEY at <base>/search). Candidates authenticate with the placeholder only.
"""
from __future__ import annotations

import http.server
import json
import os
import socket
import subprocess
import sys
import tempfile
import threading
import time
import unittest
import urllib.error
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
NATIVE = ROOT / "runners" / "optimization" / "optimization_native"


class FakeUpstream:
    """Records every POST; answers like a Responses provider or a search API."""

    def __init__(self) -> None:
        self.requests: list[dict] = []
        outer = self

        class Handler(http.server.BaseHTTPRequestHandler):
            def do_POST(self):  # noqa: N802
                body = json.loads(self.rfile.read(int(self.headers.get("Content-Length", "0"))))
                outer.requests.append({"path": self.path, "headers": dict(self.headers), "body": body})
                if "responses" in self.path:
                    reply = {"output": [{"type": "message", "content": [{"type": "output_text", "text": "ok"}]}],
                             "usage": {"input_tokens": 3, "output_tokens": 2, "total_tokens": 5}}
                else:
                    reply = {"organic": [{"title": "t", "link": "https://example.org", "snippet": "s"}]}
                data = json.dumps(reply).encode()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

            def log_message(self, *args):
                return

        self.server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.url = f"http://127.0.0.1:{self.server.server_address[1]}"
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def close(self) -> None:
        self.server.shutdown()


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def post(url: str, body: dict, token: str) -> tuple[int, dict]:
    request = urllib.request.Request(url, data=json.dumps(body).encode(), method="POST",
                                     headers={"Content-Type": "application/json", "Authorization": f"Bearer {token}"})
    try:
        with urllib.request.urlopen(request, timeout=20) as response:
            return response.status, json.loads(response.read())
    except urllib.error.HTTPError as exc:
        return exc.code, {}


class Broker:
    def __init__(self, script: str, credentials: dict[str, str], extra: list[str] | None = None) -> None:
        self.dir = tempfile.TemporaryDirectory()
        self.cred = Path(self.dir.name) / "evaluator.env"
        self.cred.write_text("".join(f"{k}={v}\n" for k, v in credentials.items()))
        self.port = free_port()
        env = {k: v for k, v in os.environ.items() if not k.startswith("AGENTSWE_")}
        self.proc = subprocess.Popen([sys.executable, str(NATIVE / script), "--credential-file", str(self.cred),
                                      "--bind", "127.0.0.1", "--port", str(self.port), *(extra or [])],
                                     env=env, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
        for _ in range(100):
            try:
                socket.create_connection(("127.0.0.1", self.port), timeout=0.2).close()
                return
            except OSError:
                time.sleep(0.05)
        raise RuntimeError("broker did not start: " + self.proc.stderr.read().decode()[-500:])

    def url(self, path: str) -> str:
        return f"http://127.0.0.1:{self.port}{path}"

    def close(self) -> None:
        self.proc.terminate()
        self.proc.wait(timeout=10)
        self.dir.cleanup()


class ResponsesBroker(unittest.TestCase):
    def setUp(self):
        self.up = FakeUpstream()

    def tearDown(self):
        self.up.close()

    def test_credential_file_provider_is_locked(self):
        b = Broker("responses_broker.py", {
            "AGENTSWE_RUNTIME_API_KEY": "real-runtime-key", "AGENTSWE_RUNTIME_RESPONSES_URL": self.up.url + "/v1/responses",
            "AGENTSWE_RUNTIME_MODEL": "paper-model", "AGENTSWE_RUNTIME_EFFORT": "medium", "AGENTSWE_JUDGE_EFFORT": "low"})
        try:
            status, _ = post(b.url("/v1/responses"), {"model": "anything", "input": "hi"}, "runtime-only-placeholder")
            self.assertEqual(status, 200)
            status, _ = post(b.url("/v1/responses"), {"model": "anything", "input": "grade"}, "judge-only-placeholder")
            self.assertEqual(status, 200)
        finally:
            b.close()
        runtime, judge = self.up.requests
        self.assertEqual((runtime["body"]["model"], runtime["body"]["reasoning"]), ("paper-model", {"effort": "medium"}))
        self.assertEqual((judge["body"]["model"], judge["body"]["reasoning"]), ("paper-model", {"effort": "low"}))
        self.assertEqual(runtime["headers"]["Authorization"], "Bearer real-runtime-key")

    def test_lite_values_without_provider_lines(self):
        """Only the key in the file: the broker keeps its Lite upstream, model and efforts."""
        import importlib.util
        spec = importlib.util.spec_from_file_location("rb", NATIVE / "responses_broker.py")
        rb = importlib.util.module_from_spec(spec)
        env = {k: os.environ.pop(k) for k in list(os.environ) if k.startswith("AGENTSWE_")}
        try:
            spec.loader.exec_module(rb)
        finally:
            os.environ.update(env)
        self.assertEqual((rb.UPSTREAM, rb.MODEL, rb.RUNTIME_EFFORT, rb.JUDGE_EFFORT),
                         ("https://api.deepseek.com/v1/responses", "deepseek-flash", "high", "max"))


    def chat(self, extra):
        b = Broker("responses_broker.py", {
            "AGENTSWE_RUNTIME_API_KEY": "k", "AGENTSWE_RUNTIME_RESPONSES_URL": self.up.url + "/v1/responses",
            "AGENTSWE_RUNTIME_MODEL": "m", "AGENTSWE_RUNTIME_EFFORT": "medium", "AGENTSWE_JUDGE_EFFORT": "max"}, extra)
        try:
            status, _ = post(b.url("/v1/chat/completions"), {"model": "x", "messages": [{"role": "user", "content": "hi"}]},
                             "runtime-only-placeholder")
            self.assertEqual(status, 200)
        finally:
            b.close()
        return self.up.requests[-1]["body"]

    def test_chat_role_default_is_judge(self):
        body = self.chat([])
        self.assertEqual(body["reasoning"], {"effort": "max"})
        self.assertGreaterEqual(body["max_output_tokens"], 32000)

    def test_chat_role_runtime(self):
        body = self.chat(["--chat-role", "runtime"])
        self.assertEqual(body["reasoning"], {"effort": "medium"})
        self.assertNotIn("max_output_tokens", body)


class BrowseCompBroker(unittest.TestCase):
    def setUp(self):
        self.up = FakeUpstream()

    def tearDown(self):
        self.up.close()

    def creds(self, wire: str, base: str) -> dict[str, str]:
        return {"AGENTSWE_RUNTIME_API_KEY": "real-runtime-key", "AGENTSWE_RUNTIME_RESPONSES_URL": self.up.url + "/v1/responses",
                "AGENTSWE_RUNTIME_MODEL": "paper-model", "AGENTSWE_RUNTIME_EFFORT": "medium",
                "AGENTSWE_SEARCH_API_KEY": "real-search-key", "AGENTSWE_SEARCH_BASE_URL": base, "AGENTSWE_SEARCH_WIRE": wire}

    def run_case(self, wire: str, base: str) -> dict:
        b = Broker("browsecomp_broker.py", self.creds(wire, base))
        try:
            self.assertEqual(post(b.url("/serp_search_v1"), {"query": "q", "page": 1, "search_type": "search"}, "wrong")[0], 401)
            status, _ = post(b.url("/v1/responses"), {"model": "x", "input": [{"role": "user", "content": "hi"}]},
                             "broker-only-placeholder")
            self.assertEqual(status, 200)
            status, value = post(b.url("/serp_search_v1"), {"query": "q", "page": 2, "search_type": "search"},
                                 "broker-only-placeholder")
            self.assertEqual(status, 200)
            self.assertEqual(value["organic"][0]["title"], "t")
            stats = urllib.request.Request(b.url("/stats"), headers={"Authorization": "Bearer stats-only-placeholder"})
            with urllib.request.urlopen(stats, timeout=10) as response:
                return json.loads(response.read())
        finally:
            b.close()

    def test_legacy_proxy_wire(self):
        stats = self.run_case("legacy-proxy", self.up.url + "/serp_search_v1")
        model, search = self.up.requests
        self.assertEqual((model["body"]["model"], model["body"]["reasoning"]), ("paper-model", {"effort": "medium"}))
        self.assertEqual(model["headers"]["Authorization"], "Bearer real-runtime-key")
        self.assertEqual(search["path"], "/serp_search_v1")
        self.assertEqual(search["body"], {"query": "q", "page": 2, "search_type": "search", "token": "real-search-key"})
        self.assertEqual((stats["model"], stats["model_effort"], stats["search_wire"]), ("paper-model", "medium", "legacy-proxy"))
        self.assertEqual(stats["limits"], {"model": 20, "search": 20, "visit": 20})
        self.assertEqual(stats["resources"]["search"]["calls"], 1)

    def test_serper_wire(self):
        self.run_case("serper", self.up.url)
        search = self.up.requests[1]
        self.assertEqual(search["path"], "/search")
        self.assertEqual(search["body"], {"q": "q", "page": 2})
        headers = {k.lower(): v for k, v in search["headers"].items()}  # HTTP header names are case-insensitive
        self.assertEqual(headers["x-api-key"], "real-search-key")


class BrowseCompReasoningStrip(unittest.TestCase):
    """Reasoning text a provider returns is not relayed to the Candidate; message items and usage are."""

    def setUp(self):
        import importlib.util
        spec = importlib.util.spec_from_file_location("bb", NATIVE / "browsecomp_broker.py")
        self.bb = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(self.bb)

    def test_reasoning_item_with_text_removed(self):
        message = {"type": "message", "content": [{"type": "output_text", "text": "Exact Answer: X"}]}
        raw = json.dumps({"output": [{"type": "reasoning", "content": [{"type": "reasoning_text", "text": "draft"}]},
                                     message], "usage": {"total_tokens": 9}}).encode()
        out, items, chars = self.bb.strip_reasoning_text(raw)
        value = json.loads(out)
        self.assertEqual((items, chars), (1, 5))
        self.assertEqual(value["output"], [message])
        self.assertEqual(value["usage"], {"total_tokens": 9})

    def test_text_free_reasoning_and_plain_results_relayed_byte_for_byte(self):
        for raw in (json.dumps({"output": [{"type": "reasoning", "summary": []},
                                           {"type": "message", "content": [{"type": "output_text", "text": "a"}]}]}).encode(),
                    json.dumps({"output": [{"type": "message", "content": [{"type": "output_text", "text": "a"}]}]}).encode(),
                    b"not json"):
            self.assertEqual(self.bb.strip_reasoning_text(raw), (raw, 0, 0))


class RunnerEvaluatorEnv(unittest.TestCase):
    """The runner writes the provider lines next to the key; SEARCH only for tasks that declare it."""

    def test_written_lines(self):
        source = (ROOT / "agentswe" / "runners" / "optimization_native_v1.py").read_text()
        self.assertIn('{"AGENTSWE_RUNTIME_API_KEY": rt.api_key, **provider, **search}', source)
        for key in ("AGENTSWE_RUNTIME_RESPONSES_URL", "AGENTSWE_RUNTIME_MODEL", "AGENTSWE_RUNTIME_EFFORT",
                    "AGENTSWE_JUDGE_EFFORT", "AGENTSWE_SEARCH_API_KEY", "AGENTSWE_SEARCH_BASE_URL", "AGENTSWE_SEARCH_WIRE"):
            self.assertIn(key, source)
        self.assertIn('"search" in task.data.get("services", [])', source)


if __name__ == "__main__":
    unittest.main()
