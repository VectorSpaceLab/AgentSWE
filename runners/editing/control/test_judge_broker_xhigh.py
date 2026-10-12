"""Real localhost HTTP: judge -> new broker -> mock upstream, never provider."""
import contextlib
import http.server
import json
import os
from pathlib import Path
import signal
import socket
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

import requests

import judge_broker_xhigh as broker
import result_judge as judge


class Upstream(http.server.BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, *_args):
        pass

    def do_POST(self):
        raw = self.rfile.read(int(self.headers["Content-Length"]))
        self.server.requests.append({"body": json.loads(raw), "auth": self.headers.get("Authorization")})
        mode = self.server.mode
        try:
            if mode in ("stall", "trickle", "interim"):
                if mode == "trickle":
                    self.send_response(200)
                    self.send_header("Content-Length", "100000")
                    self.end_headers()
                until = time.monotonic() + 3
                while time.monotonic() < until:
                    if mode == "trickle":
                        self.wfile.write(b" ")
                    elif mode == "interim":
                        self.wfile.write(b"HTTP/1.1 100 Continue\r\n\r\n")
                    self.wfile.flush()
                    time.sleep(.02)
                return
            if mode == "disconnect":
                self.connection.shutdown(socket.SHUT_RDWR)
                self.connection.close()
                return
            status = 200
            body = {"id": "mock-only", "model": broker.MODEL, "status": "completed",
                "output_text": "{}", "usage": {"input_tokens": 7, "output_tokens": 3, "total_tokens": 10}}
            if hasattr(self.server, "output_text"):
                body["output_text"] = self.server.output_text
            if mode == "retry" and len(self.server.requests) == 1:
                status, body = 502, {"error": "real upstream 502"}
            elif mode == "invalid":
                body["status"] = "incomplete"
            elif mode == "echo":
                body["output_text"] = self.server.test_key
            elif mode == "redirect":
                status = 307
            data = b"not-json" if mode == "malformed" else json.dumps(body).encode()
            self.send_response(status)
            self.send_header("Content-Length", str(len(data)))
            self.send_header("Retry-After", "0")
            if status == 307:
                self.send_header("Location", self.server.url)
            self.end_headers()
            self.wfile.write(data)
        except (OSError, ValueError):
            pass


@contextlib.contextmanager
def servers(mode="valid", timeout=2):
    with tempfile.TemporaryDirectory() as tmp:
        upstream = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Upstream)
        upstream.daemon_threads = True
        upstream.requests, upstream.mode = [], mode
        upstream.test_key = "synthetic-never-real-key"
        upstream.url = f"http://127.0.0.1:{upstream.server_port}/v1/responses"
        state = Path(tmp) / "stats.json"
        proxy = broker.Server(("127.0.0.1", 0), key=upstream.test_key, endpoint=upstream.url,
            stats_path=state, timeout=timeout, keepalive=.05)
        threads = [threading.Thread(target=s.serve_forever, kwargs={"poll_interval": .02}, daemon=True)
                   for s in (upstream, proxy)]
        for thread in threads:
            thread.start()
        try:
            yield upstream, proxy, state, f"http://127.0.0.1:{proxy.server_port}/v1/responses"
        finally:
            for server in (proxy, upstream):
                server.shutdown()
                server.server_close()
            for thread in threads:
                thread.join(2)


def payload():
    return {"model": broker.MODEL, "reasoning": {"effort": "max"}, "max_output_tokens": 64000,
            "stream": False, "input": [{"role": "user", "content": "synthetic"}]}


class BrokerTests(unittest.TestCase):
    def wait_stats(self, path, calls=1):
        deadline = time.monotonic() + 3
        while time.monotonic() < deadline:
            value = json.loads(path.read_text())
            if value["runtime"]["completed_calls"] == calls:
                for event in value["attempts"]:
                    self.assertTrue(event["worker_reaped"])
                    with self.assertRaises(ProcessLookupError):
                        os.kill(event["worker_pid"], 0)
                return value
            time.sleep(.02)
        self.fail("owned broker worker did not reach terminal state")

    def call(self, endpoint, timeout=2):
        return judge.call_judge("synthetic prompt", "broker-only-placeholder", timeout, 4, endpoint=endpoint)

    def test_success_exact_one_upstream_fixed_model_usage_and_raw(self):
        with servers() as (upstream, proxy, state, endpoint):
            result = self.call(endpoint)
            self.assertEqual(result[1], 1)
            stats = self.wait_stats(state)
            self.assertEqual(len(upstream.requests), 1)
            self.assertEqual(upstream.requests[0]["body"]["reasoning"], {"effort": "max"})
            self.assertEqual(upstream.requests[0]["auth"], "Bearer " + upstream.test_key)
            self.assertEqual(stats["runtime"]["upstream_attempts"], 1)
            self.assertEqual(stats["runtime"]["tokens"], 10)
            self.assertEqual(stats["runtime"]["usage_unknown_calls"], 0)
            self.assertTrue(Path(stats["attempts"][0]["provider_response_path"]).is_file())
            self.assertNotIn(upstream.test_key, state.read_text())

    def test_explicit_502_only_outer_retries_no_hidden_retry(self):
        with servers("retry") as (upstream, proxy, state, endpoint):
            result = self.call(endpoint)
            self.assertEqual(result[1], 2)
            stats = self.wait_stats(state, 2)
            self.assertEqual(len(upstream.requests), 2)
            self.assertEqual([e["upstream_attempts"] for e in stats["attempts"]], [1, 1])
            self.assertEqual([e["upstream_http_status"] for e in stats["attempts"]], [502, 200])

    def test_invalid_completed_or_json_never_resampled(self):
        for mode in ("invalid", "malformed"):
            with self.subTest(mode=mode), servers(mode) as (upstream, proxy, state, endpoint):
                with self.assertRaises(judge.ReceivedResponseFailure):
                    self.call(endpoint)
                self.assertEqual(len(upstream.requests), 1)
                self.assertEqual(self.wait_stats(state)["runtime"]["upstream_attempts"], 1)

    def test_broker_deadline_handles_stall_trickle_and_interim(self):
        for mode in ("stall", "trickle", "interim"):
            with self.subTest(mode=mode), servers(mode, timeout=.3) as (upstream, proxy, state, endpoint):
                start = time.monotonic()
                with self.assertRaisesRegex(judge.TransportFailure, "HTTP 598"):
                    self.call(endpoint)
                self.assertLess(time.monotonic()-start, 1.5)
                stats = self.wait_stats(state)
                self.assertEqual(len(upstream.requests), 1)
                self.assertEqual(stats["runtime"]["usage_unknown_calls"], 1)

    def test_outer_absolute_deadline_handles_direct_trickle_and_interim(self):
        for mode in ("trickle", "interim"):
            with self.subTest(mode=mode), servers(mode) as (upstream, proxy, state, endpoint):
                previous = signal.getsignal(signal.SIGALRM)
                start = time.monotonic()
                with self.assertRaisesRegex(judge.TransportFailure, "absolute response deadline"):
                    self.call(upstream.url, timeout=.3)
                self.assertLess(time.monotonic()-start, 1)
                self.assertEqual(len(upstream.requests), 1)
                self.assertEqual(signal.getitimer(signal.ITIMER_REAL), (0.0, 0.0))
                self.assertEqual(signal.getsignal(signal.SIGALRM), previous)

    def test_disconnect_is_not_synthesized_retryable_502(self):
        with servers("disconnect") as (upstream, proxy, state, endpoint):
            with self.assertRaisesRegex(judge.TransportFailure, "HTTP 598"):
                self.call(endpoint)
            self.assertEqual(len(upstream.requests), 1)
            self.assertEqual(self.wait_stats(state)["runtime"]["upstream_attempts"], 1)

    def test_client_disconnect_reaps_owned_worker_without_retry(self):
        with servers("trickle") as (upstream, proxy, state, endpoint):
            request = json.dumps(payload()).encode()
            connection = socket.create_connection(("127.0.0.1", proxy.server_port))
            connection.sendall(b"POST /v1/responses HTTP/1.1\r\nHost: localhost\r\n"
                b"Authorization: Bearer broker-only-placeholder\r\nContent-Length: "
                + str(len(request)).encode() + b"\r\n\r\n" + request)
            until = time.monotonic() + 2
            while not upstream.requests and time.monotonic() < until:
                time.sleep(.01)
            self.assertEqual(len(upstream.requests), 1)
            connection.shutdown(socket.SHUT_RDWR)
            connection.close()
            stats = self.wait_stats(state)
            self.assertIn("client_disconnected", stats["attempts"][0]["transport_abort_reason"])
            self.assertEqual(stats["runtime"]["upstream_attempts"], 1)

    def test_redirects_are_terminal_no_implicit_post_replay(self):
        with servers("redirect") as (upstream, proxy, state, endpoint):
            with self.assertRaisesRegex(judge.TransportFailure, "HTTP 307"):
                self.call(endpoint)
            self.assertEqual(len(upstream.requests), 1)
            self.wait_stats(state)

    def test_reject_wrong_role_model_effort_stream_limit_without_upstream(self):
        with servers() as (upstream, proxy, state, endpoint):
            for field, value in (("model", "wrong"), ("reasoning", {"effort": "high"}),
                                 ("stream", 'true'), ("max_output_tokens", 24000), ("tools", [])):
                with self.subTest(field=field):
                    response = requests.post(endpoint, json={**payload(), field: value},
                        headers={"Authorization": "Bearer broker-only-placeholder"}, timeout=2)
                    self.assertEqual(response.status_code, 400)
            self.assertEqual(requests.post(endpoint, json=payload(), timeout=2).status_code, 401)
            self.assertEqual(len(upstream.requests), 0)
            self.assertEqual(json.loads(state.read_text())["runtime"]["calls"], 0)

    def test_raw_response_redacts_echoed_credential(self):
        with servers("echo") as (upstream, proxy, state, endpoint):
            result = self.call(endpoint)
            self.assertNotIn(upstream.test_key, result[0])
            stats = self.wait_stats(state)
            self.assertNotIn(upstream.test_key, Path(stats["attempts"][0]["provider_response_path"]).read_text())

    def test_old_stats_never_overwritten(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "stats.json"
            path.write_text("preserve")
            with self.assertRaises(FileExistsError):
                broker.State(path)
            self.assertEqual(path.read_text(), "preserve")

    def test_unsupported_thread_fails_before_network(self):
        failures = []
        def run():
            try:
                judge.call_judge("x", "synthetic", 2, 1, endpoint="http://localhost:1")
            except RuntimeError as exc:
                failures.append(str(exc))
        with patch.object(judge.requests, "post") as post:
            thread = threading.Thread(target=run)
            thread.start()
            thread.join()
            self.assertEqual(post.call_count, 0)
        self.assertIn("main-thread", failures[0])

    def test_actual_broker_cli_and_result_cli_plus_idempotency(self):
        with servers() as (upstream, unused, state, endpoint), tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            credential = root / "synthetic.env"
            credential.write_text("DEEPSEEK_API_KEY=" + upstream.test_key + "\n")
            with socket.socket() as sock:
                sock.bind(("127.0.0.1", 0))
                port = sock.getsockname()[1]
            stats_path = root / "broker-stats.json"
            process = subprocess.Popen([sys.executable, str(Path(broker.__file__).resolve()),
                "--credential-file", str(credential), "--bind", "127.0.0.1", "--port", str(port),
                "--upstream", upstream.url, "--stats-path", str(stats_path)],
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            try:
                deadline = time.monotonic() + 3
                health = f"http://127.0.0.1:{port}/healthz"
                while time.monotonic() < deadline:
                    try:
                        if requests.get(health, timeout=.2).status_code == 200:
                            break
                    except requests.RequestException:
                        time.sleep(.02)
                else:
                    self.fail("broker CLI did not become healthy")
                value = {"case_id": "test_001", "result_state": "scoreable", "result_score": 60,
                    "task_completion": {"score": 30, "max": 50, "evidence": "synthetic"},
                    "evidence_grounding": {"score": 18, "max": 30, "evidence": "synthetic"},
                    "recovery_and_safety": {"score": 12, "max": 20, "evidence": "synthetic"},
                    "major_errors": [], "assessment": "mocked protocol test, not a real score"}
                upstream.output_text = json.dumps(value)
                args = [sys.executable, str(Path(judge.__file__).resolve()), "--case-id", "test_001",
                    "--broker-endpoint", f"http://127.0.0.1:{port}/v1/responses", "--output-dir", str(root / "out")]
                for name in ("task-input", "rubric", "agent-artifact", "trajectory", "native-evidence", "oracle-summary"):
                    path = root / (name + ".txt")
                    path.write_text("synthetic protocol regression input")
                    args.extend(["--" + name, str(path)])
                completed = subprocess.run(args, capture_output=True, text=True, timeout=5)
                self.assertEqual(completed.returncode, 0, completed.stdout+completed.stderr)
                contract_path = root / "out/result_score_contract.json"
                original = contract_path.read_bytes()
                contract = json.loads(original)
                self.assertTrue(contract["contract_valid"])
                self.assertEqual(contract["provider_usage"]["transport_attempts"], 1)
                self.assertEqual(contract["provider_usage"]["completed_responses"], 1)
                second = subprocess.run(args, capture_output=True, text=True, timeout=5)
                self.assertEqual(second.returncode, 2)
                self.assertEqual(original, contract_path.read_bytes())
                self.assertEqual(len(upstream.requests), 1)
                self.wait_stats(stats_path)
                # A second independent synthetic request is interrupted by an
                # operator SIGTERM. It must not leave the network child alive.
                upstream.mode = "trickle"
                request_body = json.dumps(payload()).encode()
                connection = socket.create_connection(("127.0.0.1", port))
                connection.sendall(b"POST /v1/responses HTTP/1.1\r\nHost: localhost\r\n"
                    b"Authorization: Bearer broker-only-placeholder\r\nContent-Length: "
                    + str(len(request_body)).encode() + b"\r\n\r\n" + request_body)
                until = time.monotonic() + 2
                while len(upstream.requests) < 2 and time.monotonic() < until:
                    time.sleep(.01)
                self.assertEqual(len(upstream.requests), 2)
                process.terminate()
                process.wait(timeout=4)
                self.wait_stats(stats_path, 2)
                connection.close()
            finally:
                if process.poll() is None:
                    process.terminate()
                process.wait(timeout=5)


if __name__ == "__main__":
    unittest.main()
