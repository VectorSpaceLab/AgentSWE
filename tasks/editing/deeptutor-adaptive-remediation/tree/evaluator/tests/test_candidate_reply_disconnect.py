"""Candidate reply disconnect (2026-10-09): the Candidate's own product aborts its model call after the broker
answered, the relay's reply write fails, and the product exits by itself.  The relay test uses a real UDS relay and a
local HTTP broker stand-in; the rest is provider-free."""
import http.server, json, socket, subprocess, sys, tempfile, threading, time, unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
from agentloop import lower_agent_entry as entry  # noqa: E402
from agentloop.lower_transport import UnixHTTPRelay  # noqa: E402

DISCONNECT = {"kind": "relay_error", "error_type": "BrokenPipeError", "phase": "reply_write",
              "upstream_status": 200, "response_bytes": 844}
FORWARDED = {"kind": "forwarded_response", "status": 200}
VALID = {"valid": True}


def completed(n=1):
    return [{"context_id": "ctx", "state": "completed", "request_sha256": str(i) * 64} for i in range(n)]


class RelayRecordsTheReplyWrite(unittest.TestCase):
    def test_a_client_that_left_before_the_reply_is_a_reply_write_disconnect(self):
        class Broker(http.server.BaseHTTPRequestHandler):
            def log_message(self, *_):
                pass

            def do_POST(self):
                self.rfile.read(int(self.headers["Content-Length"]))
                time.sleep(0.5)  # the product gives up before this answer
                raw = json.dumps({"id": "resp_1", "status": "completed", "output": []}).encode()
                self.send_response(200); self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(raw))); self.end_headers(); self.wfile.write(raw)
        broker = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Broker)
        threading.Thread(target=broker.serve_forever, daemon=True).start()
        relay = UnixHTTPRelay(f"http://127.0.0.1:{broker.server_port}/v1/responses", context_id="ctx")
        try:
            body = json.dumps({"model": "deepseek-flash", "reasoning": {"effort": "high"}, "input": "x"}).encode()
            client = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            client.connect(str(relay.path))
            client.sendall(b"POST /v1/responses HTTP/1.1\r\nHost: relay\r\nContent-Type: application/json\r\n"
                           + b"Content-Length: " + str(len(body)).encode() + b"\r\n\r\n" + body)
            client.close()
            deadline = time.monotonic() + 5
            while time.monotonic() < deadline and not any(e.get("kind") == "relay_error" for e in relay.events):
                time.sleep(0.05)
        finally:
            relay.close(); broker.shutdown(); broker.server_close()
        errors = [e for e in relay.events if e.get("kind") == "relay_error"]
        self.assertEqual(len(errors), 1, relay.events)
        self.assertIn(errors[0]["error_type"], entry._DISCONNECT_ERRORS)
        self.assertEqual((errors[0]["phase"], errors[0]["upstream_status"]), ("reply_write", 200))
        self.assertIn(FORWARDED, relay.events)


class CandidateReplyDisconnect(unittest.TestCase):
    def test_candidate_disconnect_shape_is_candidate(self):
        record = entry._candidate_reply_disconnect([FORWARDED, DISCONNECT], VALID, completed())
        self.assertEqual(record["classification_kept"], "candidate")
        self.assertEqual(record["tolerated_relay_events"], [DISCONNECT])  # recorded, not dropped
        reset = dict(DISCONNECT, error_type="ConnectionResetError")
        self.assertIsNotNone(entry._candidate_reply_disconnect([FORWARDED, reset], VALID, completed()))

    def test_relay_error_without_broker_completion_stays_infrastructure(self):
        pending = [{"context_id": "ctx", "state": "submitted_or_unknown"}]
        failed = completed() + [{"context_id": "ctx", "state": "unknown_or_failed"}]
        for name, events, rows in (
                ("broker row pending", [FORWARDED, DISCONNECT], pending),
                ("a failed row besides", [FORWARDED, DISCONNECT], failed),
                ("no broker rows", [FORWARDED, DISCONNECT], []),
                ("broker unreadable", [FORWARDED, DISCONNECT], None),
                ("failure before the broker answered", [{"kind": "relay_error", "error_type": "BrokenPipeError"}],
                 completed()),
                ("broker answered 500", [dict(DISCONNECT, upstream_status=500)], completed())):
            with self.subTest(name):
                self.assertIsNone(entry._candidate_reply_disconnect(events, VALID, rows))

    def test_anything_more_than_the_disconnect_stays_infrastructure(self):
        for name, events, preflight in (
                ("rejected endpoint", [DISCONNECT, {"kind": "rejected_endpoint", "path": "/x"}], VALID),
                ("broker http error", [DISCONNECT, {"kind": "broker_http_error", "status": 502}], VALID),
                ("budget refusal besides", [DISCONNECT, {"kind": "broker_http_error", "status": 429}], VALID),
                ("not a disconnect", [dict(DISCONNECT, error_type="TimeoutError")], VALID),
                ("invalid preflight", [DISCONNECT], {"valid": False})):
            with self.subTest(name):
                self.assertIsNone(entry._candidate_reply_disconnect(events, preflight, completed(2)))

    def test_the_rule_is_consulted_only_after_the_product_exited_by_itself(self):
        source = Path(entry.__file__).read_text(encoding="utf-8")
        run_case = source[source.index("def _run_case("):source.index("def _parse_json_lines(")]
        exited = run_case.index("completed = subprocess.run(")
        self.assertNotIn("_candidate_reply_disconnect", run_case[:exited])
        timeout_branch = run_case[run_case.index("except subprocess.TimeoutExpired", exited):
                                  run_case.index("finally:", exited)]
        self.assertIn("candidate_timeout", timeout_branch)
        self.assertIn("return record", timeout_branch)
        self.assertNotIn("_candidate_reply_disconnect", timeout_branch)


class DeadlineKillKeepsItsRule(unittest.TestCase):
    """DeepTutor's deadline path (the owned 590 s scope, D48) decides from the broker ledger and never read relay
    events; a reply-write disconnect earlier in the case leaves it exactly as it was."""

    def timed_out(self, rows, events):
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory)
            (output / "launcher_result.json").write_text(json.dumps({"executed": True}))
            (output / "logical-context.json").write_text(json.dumps({"context_id": "ctx"}))
            (output / "transport_relay_events.json").write_text(json.dumps({"events": events}))
            stats = {"logical_requests": {str(i): dict(row, context_id="ctx") for i, row in enumerate(rows)}}
            with mock.patch("agentloop.owned_resources.run_owned",
                            return_value=(subprocess.CompletedProcess([], -9, "", ""), {"timed_out": True})), \
                    mock.patch("agentloop.run_hidden.read_broker_stats", return_value=stats):
                return entry.run(output, output / "p.md", output, broker_endpoint="http://127.0.0.1:9/v1/responses",
                                 execute=True, python_executable=sys.executable)

    def test_evaluator_deadline_kill_keeps_the_d48_behaviour(self):
        cut = [{"state": "completed"}, {"state": "submitted_or_unknown"}]
        value = self.timed_out(cut, [FORWARDED])
        self.assertEqual(value["classification"], "candidate_timeout")
        self.assertTrue(value.get("case_budget_exhausted"))

    def test_disconnect_followed_by_a_deadline_kill_is_decided_by_the_deadline_rule_alone(self):
        for rows in ([{"state": "completed"}, {"state": "submitted_or_unknown"}],
                     [{"state": "submitted_or_unknown"}, {"state": "submitted_or_unknown"}]):
            with self.subTest(rows=rows):
                plain = self.timed_out(rows, [FORWARDED])
                disconnected = self.timed_out(rows, [FORWARDED, DISCONNECT, FORWARDED])
                self.assertEqual(plain["classification"], disconnected["classification"])
                self.assertNotIn("candidate_reply_disconnect", disconnected)
        self.assertEqual(self.timed_out([{"state": "submitted_or_unknown"}] * 2, [DISCONNECT])["classification"],
                         "provider_infrastructure_error")


if __name__ == "__main__":
    unittest.main()
