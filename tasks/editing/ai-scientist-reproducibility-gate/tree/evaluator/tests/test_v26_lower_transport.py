"""Real localhost HTTP/worker checks; never call a real model provider."""
import copy
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
from lower_transport import CASE_DEADLINE, snapshot_valid
from evaluator import lower_responses_broker as broker


class Provider(BaseHTTPRequestHandler):
    def log_message(self, *_): pass
    def do_POST(self):
        request = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        self.server.requests.append(request)
        mode = self.server.mode
        if mode == "disconnect":
            self.connection.shutdown(socket.SHUT_RDWR); self.connection.close(); return
        if mode == "stall":
            self.server.release.wait(3); return
        if mode in {"retry", "always429", "redirect"}:
            if mode != "retry" or len(self.server.requests) == 1:
                self.send_response(307 if mode == "redirect" else 429)
                self.send_header("Location", "http://127.0.0.1:9/forbidden")
                self.send_header("Content-Length", "2"); self.end_headers()
                self.wfile.write(b"{}"); return
        response = {"id": "synthetic-response", "model": "gpt-5.6-sol", "status": "completed",
                    "output_text": json.dumps({"kind": "finish", "operation": None, "rationale": "diagnostic"}),
                    "usage": {"input_tokens": 2, "output_tokens": 3, "total_tokens": 5}}
        if mode == "wrong_model": response["model"] = "wrong-model"
        if mode == "bad_content": response["output_text"] = "not-json"
        if mode == "unknown_usage": response["usage"] = None
        if mode == "incomplete": response["status"] = "incomplete"
        raw = json.dumps(response).encode() if mode != "truncated" else b'{"model":'
        self.send_response(200); self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(raw))); self.end_headers()
        self.wfile.write(raw)


class LowerTransportTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(); self.path = Path(self.temp.name)
        self.provider = ThreadingHTTPServer(("127.0.0.1", 0), Provider)
        self.provider.requests = []; self.provider.mode = "normal"; self.provider.release = threading.Event()
        self.provider.daemon_threads = True
        self.pt = threading.Thread(target=self.provider.serve_forever, kwargs={"poll_interval": .02}, daemon=True); self.pt.start()
        self.server = broker.LowerServer(("127.0.0.1", 0),
            endpoint=f"http://127.0.0.1:{self.provider.server_port}/v1/responses",
            key="SYNTHETIC-NONSECRET", stats_path=self.path / "stats.json", timeout=5)
        self.bt = threading.Thread(target=self.server.serve_forever, kwargs={"poll_interval": .02}, daemon=True); self.bt.start()
        self.endpoint = f"http://127.0.0.1:{self.server.server_port}/v1/responses"

    def tearDown(self):
        self.provider.release.set()
        self.server.stop_owned_workers(); self.server.shutdown(); self.server.server_close(); self.bt.join(1)
        self.provider.shutdown(); self.provider.server_close(); self.pt.join(1)
        for event in self.server.state.snapshot()["attempts"]:
            self.assertTrue(event.get("worker_reaped", False))
            pid = event.get("worker_pid")
            if pid:
                self.assertFalse(Path(f"/proc/{pid}").exists(), "owned worker survived")
        self.temp.cleanup()

    def request(self, mode="normal"):
        self.provider.mode = mode
        return launcher._request_model_json(self.endpoint, "synthetic task", "action")

    def state(self):
        deadline = time.monotonic() + 2
        while self.server.state.snapshot()["runtime"]["in_flight_calls"] and time.monotonic() < deadline:
            time.sleep(.01)
        return self.server.state.snapshot()

    def test_real_payload_lock_response_and_usage(self):
        before = launcher.broker_stats(self.endpoint)
        self.assertTrue(snapshot_valid(before, fresh=True))
        value, _ = self.request()
        self.assertEqual(value["kind"], "finish")
        after = self.state(); delta = launcher.broker_delta(before, after)
        self.assertEqual(delta["upstream_attempts_delta"], 1)
        self.assertEqual(delta["tokens_delta"], 5)
        self.assertIsNone(delta["transport_error"])
        sent = self.provider.requests[0]
        self.assertEqual(sent["model"], "gpt-5.6-sol")
        self.assertEqual(sent["reasoning"], {"effort": "high"})
        self.assertTrue(sent["stream"])

    def test_explicit_503_has_one_upstream_and_no_resample(self):
        token = CASE_DEADLINE.set(time.monotonic() + 4)
        try:
            with self.assertRaises(urllib.error.HTTPError):self.request("retry")
        finally: CASE_DEADLINE.reset(token)
        state = self.state()
        self.assertEqual(len(self.provider.requests), 1)
        self.assertEqual(state["runtime"]["upstream_attempts"], 1)
        self.assertEqual(len({e["deadline_monotonic"] for e in state["attempts"]}), 1)

    def test_explicit_429_is_not_automatically_resubmitted(self):
        with self.assertRaises(urllib.error.HTTPError): self.request("always429")
        self.assertEqual(len(self.provider.requests), 1)

    def test_unknown_disconnect_not_resampled(self):
        with self.assertRaises(urllib.error.URLError): self.request("disconnect")
        self.assertEqual(len(self.provider.requests), 1)
        self.assertEqual(self.state()["runtime"]["usage_unknown_calls"], 1)

    def test_wrong_model_is_provider_failure_not_content_failure(self):
        with self.assertRaises(urllib.error.URLError): self.request("wrong_model")
        self.assertEqual(len(self.provider.requests), 1)

    def test_malformed_envelope_is_not_candidate_json_failure(self):
        with self.assertRaises(urllib.error.URLError): self.request("truncated")
        self.assertEqual(len(self.provider.requests), 1)

    def test_completed_bad_model_content_is_not_resampled(self):
        with self.assertRaises(launcher.ModelContentError): self.request("bad_content")
        self.assertEqual(len(self.provider.requests), 1)
        self.assertEqual(self.state()["runtime"]["successful_calls"], 1)

    def test_known_incomplete_response_not_resampled(self):
        with self.assertRaises(launcher.ModelContentError): self.request("incomplete")
        self.assertEqual(len(self.provider.requests), 1)

    def test_missing_usage_remains_unknown(self):
        before = self.state(); self.request("unknown_usage")
        self.assertEqual(launcher.broker_delta(before, self.state())["usage_unknown_calls_delta"], 1)
        self.assertIsNone(launcher.broker_delta(before,self.state())["tokens_delta"])
        self.assertFalse(launcher.broker_delta(before,self.state())["usage_complete"])

    def test_deadline_bounds_real_stalled_worker(self):
        token = CASE_DEADLINE.set(time.monotonic() + .4)
        started = time.monotonic()
        try:
            with self.assertRaises((urllib.error.URLError, TimeoutError, OSError)): self.request("stall")
        finally: CASE_DEADLINE.reset(token)
        self.assertLess(time.monotonic() - started, 2)
        self.assertEqual(len(self.provider.requests), 1)

    def test_expired_deadline_never_dispatches(self):
        token = CASE_DEADLINE.set(time.monotonic() - 1)
        try:
            with self.assertRaises(TimeoutError): self.request()
        finally: CASE_DEADLINE.reset(token)
        self.assertEqual(self.provider.requests, [])

    def test_redirect_not_followed(self):
        with self.assertRaises(urllib.error.HTTPError) as error: self.request("redirect")
        self.assertEqual(error.exception.code, 307)
        self.assertEqual(len(self.provider.requests), 1)

    def test_old_or_inconsistent_stats_rejected(self):
        value = self.state(); self.assertTrue(snapshot_valid(value, fresh=True))
        for key in ("model", "reasoning_effort", "inner_retries"):
            altered = copy.deepcopy(value); altered["protocol"][key] = "wrong"
            self.assertFalse(snapshot_valid(altered))
        altered = copy.deepcopy(value); altered["runtime"].pop("usage_unknown_calls")
        self.assertFalse(snapshot_valid(altered))
        altered = copy.deepcopy(value); altered["runtime"]["calls"] = 1
        self.assertFalse(snapshot_valid(altered))

    def test_changed_instance_and_inflight_boundaries_rejected(self):
        before = self.state(); after = copy.deepcopy(before); after["broker_instance_id"] = "foreign"
        self.assertIsNotNone(launcher.broker_delta(before, after)["transport_error"])
        after = copy.deepcopy(before); after["runtime"].update(calls=1, in_flight_calls=1)
        self.assertIsNotNone(launcher.broker_delta(before, after)["transport_error"])


if __name__ == "__main__": unittest.main()
