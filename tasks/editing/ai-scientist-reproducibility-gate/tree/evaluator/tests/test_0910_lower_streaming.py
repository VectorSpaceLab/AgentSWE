"""Actual localhost SSE → broker worker → existing lower consumer; no external API."""
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
import socket
import sys
import tempfile
import threading
import time
import unittest
import urllib.error
import urllib.request

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT)); sys.path.insert(0, str(ROOT / "agentloop"))
import lower_agent_launcher as launcher
from lower_transport import CASE_DEADLINE
from evaluator import lower_responses_broker as broker


class SSEProvider(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, *_):
        pass

    def do_POST(self):
        request = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        self.server.requests.append(request)
        mode = self.server.mode
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Connection", "close")
        self.end_headers()
        response = {"id": "synthetic-sse", "model": "gpt-5.6-sol", "status": "completed",
            "output_text": json.dumps({"kind": "finish", "operation": None,
                                        "rationale": "observed diagnostic"}),
            "usage": {"input_tokens": 2, "output_tokens": 3, "total_tokens": 5}}
        try:
            if mode == "long":
                # Existing 300/600 second budgets allow a response past 60 seconds.
                until = time.monotonic() + 62
                while time.monotonic() < until:
                    self.wfile.write(b": observed keepalive\n\n"); self.wfile.flush()
                    if self.server.release.wait(.5):
                        return
            created = {"type": "response.created", "response":
                       {"id": "synthetic-sse", "status": "in_progress"}}
            self.wfile.write(b"data: " + json.dumps(created).encode() + b"\n\n")
            self.wfile.flush()
            if mode == "stall":
                self.server.release.wait(5)
                return
            delta = {"type": "response.output_text.delta", "delta": "not authoritative"}
            self.wfile.write(b"data: " + json.dumps(delta).encode() + b"\n\n")
            self.wfile.flush()
            if mode == "disconnect":
                self.connection.shutdown(socket.SHUT_RDWR)
                return
            if mode == "delta_only":
                return
            if mode == "incomplete":
                response["status"] = "incomplete"
            if mode == "wrong_model":
                response["model"] = "other-model"
            if mode == "wrong_identity":
                response["id"] = "different-response"
            if mode == "unknown_usage":
                response.pop("usage")
            if mode == "bad_content":
                response["output_text"] = "not strict JSON"
            kind = "response.incomplete" if mode == "incomplete" else "response.completed"
            if mode == "mismatched_status":
                response["status"] = "in_progress"
            terminal = b"data: " + json.dumps({"type": kind, "response": response}).encode() + b"\n\n"
            # Split the terminal itself across actual socket reads.
            self.wfile.write(terminal[:37]); self.wfile.flush()
            time.sleep(.06)
            self.wfile.write(terminal[37:]); self.wfile.flush()
            if mode in {"completed", "long"}:
                self.server.release.wait(3)  # complete envelope must not wait for EOF
        except (BrokenPipeError, ConnectionResetError, OSError):
            pass


class StreamingTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.path = Path(self.temp.name)
        self.provider = ThreadingHTTPServer(("127.0.0.1", 0), SSEProvider)
        self.provider.requests = []; self.provider.mode = "completed"
        self.provider.release = threading.Event(); self.provider.daemon_threads = True
        self.pt = threading.Thread(target=self.provider.serve_forever,
            kwargs={"poll_interval": .02}, daemon=True); self.pt.start()
        self.server = broker.LowerServer(("127.0.0.1", 0),
            endpoint=f"http://127.0.0.1:{self.provider.server_port}/v1/responses",
            key="SYNTHETIC-NONSECRET", stats_path=self.path / "stats.json", timeout=72)
        self.bt = threading.Thread(target=self.server.serve_forever,
            kwargs={"poll_interval": .02}, daemon=True); self.bt.start()
        self.endpoint = f"http://127.0.0.1:{self.server.server_port}/v1/responses"

    def tearDown(self):
        self.provider.release.set()
        self.server.stop_owned_workers(); self.server.shutdown(); self.server.server_close()
        self.provider.shutdown(); self.provider.server_close()
        self.bt.join(1); self.pt.join(1)
        for event in self.server.state.snapshot()["attempts"]:
            self.assertTrue(event.get("worker_reaped"))
            if event.get("worker_pid"):
                self.assertFalse(Path(f"/proc/{event['worker_pid']}").exists())
        self.temp.cleanup()

    def request(self, mode):
        self.provider.mode = mode
        return launcher._request_model_json(self.endpoint, "synthetic task", "action")

    def event(self):
        for _ in range(100):
            state = self.server.state.snapshot()
            if not state["runtime"]["in_flight_calls"]:
                return state["attempts"][0]
            time.sleep(.01)
        self.fail("worker did not complete")

    def test_completed_sse_normalized_for_actual_nonstream_consumer_before_eof(self):
        start = time.monotonic(); value, _ = self.request("completed")
        self.assertEqual(value["kind"], "finish")
        self.assertLess(time.monotonic() - start, 2)
        self.assertTrue(self.provider.requests[0]["stream"])
        event = self.event()
        self.assertTrue(event["upstream_stream"]); self.assertFalse(event["downstream_stream"])
        self.assertEqual(event["response_phase"], "terminal")
        self.assertEqual(event["upstream_content_type"], "text/event-stream")
        self.assertEqual(event["usage"]["total_tokens"], 5)
        raw = Path(event["provider_response_path"]).read_bytes()
        self.assertIn(b"response.output_text.delta", raw)
        self.assertIn(b"response.completed", raw)
        self.assertNotIn(b"SYNTHETIC-NONSECRET", raw)

    def test_requested_sse_remains_sse(self):
        payload = json.dumps({"input": "synthetic", "stream": True}).encode()
        request = urllib.request.Request(self.endpoint, data=payload, headers={
            "Authorization": "Bearer broker-only-placeholder", "Content-Type": "application/json",
            broker.LOWER_DEADLINE_HEADER: str(time.monotonic() + 5)})
        with urllib.request.urlopen(request, timeout=5) as response:
            self.assertEqual(response.headers["Content-Type"], "text/event-stream")
            self.assertIn(b"response.completed", response.read())
        self.assertTrue(self.event()["downstream_stream"])

    def test_typed_incomplete_is_known_not_resampled(self):
        with self.assertRaises(launcher.ModelContentError): self.request("incomplete")
        event = self.event()
        self.assertEqual(event["response_status"], "incomplete")
        self.assertEqual(event["usage"]["total_tokens"], 5)
        self.assertEqual(len(self.provider.requests), 1)

    def test_completed_invalid_content_remains_candidate_content_error(self):
        with self.assertRaises(launcher.ModelContentError): self.request("bad_content")
        self.assertTrue(self.event()["model_response_available"])

    def test_missing_usage_stays_unknown(self):
        self.request("unknown_usage")
        self.assertTrue(self.event()["usage_unknown"])

    def test_disconnect_never_assembles_delta_into_result(self):
        with self.assertRaises(urllib.error.URLError): self.request("disconnect")
        event = self.event()
        self.assertFalse(event["model_response_available"])
        self.assertTrue(event["usage_unknown"])
        self.assertEqual(event["upstream_attempts"], 1)
        self.assertEqual(len(self.provider.requests), 1)

    def test_delta_only_is_not_completed(self):
        with self.assertRaises(urllib.error.URLError): self.request("delta_only")
        self.assertTrue(self.event()["usage_unknown"])

    def test_identity_change_is_protocol_failure(self):
        with self.assertRaises(urllib.error.URLError): self.request("wrong_identity")
        self.assertFalse(self.event()["model_response_available"])

    def test_typed_status_mismatch_is_protocol_failure(self):
        with self.assertRaises(urllib.error.URLError): self.request("mismatched_status")
        self.assertFalse(self.event()["model_response_available"])

    def test_wrong_model_is_provider_failure(self):
        with self.assertRaises(urllib.error.URLError): self.request("wrong_model")
        self.assertFalse(self.event()["model_response_available"])

    def test_absolute_case_deadline_still_bounds_stream(self):
        token = CASE_DEADLINE.set(time.monotonic() + .4)
        start = time.monotonic()
        try:
            with self.assertRaises((urllib.error.URLError, OSError, TimeoutError)):
                self.request("stall")
        finally:
            CASE_DEADLINE.reset(token)
        self.assertLess(time.monotonic() - start, 2)
        self.assertEqual(len(self.provider.requests), 1)
        self.assertTrue(self.event()["usage_unknown"])

    @unittest.skipUnless(os.environ.get("AGENTSWE_TEST_LONG_LOCAL_SSE") == "1",
                         "explicit 62 second localhost-only transport control")
    def test_real_sse_can_finish_after_sixty_seconds_without_new_attempt(self):
        start = time.monotonic(); value, _ = self.request("long")
        elapsed = time.monotonic() - start
        self.assertEqual(value["kind"], "finish")
        self.assertGreaterEqual(elapsed, 62); self.assertLess(elapsed, 70)
        self.assertEqual(len(self.provider.requests), 1)
        self.assertEqual(self.event()["usage"]["total_tokens"], 5)


if __name__ == "__main__": unittest.main()
