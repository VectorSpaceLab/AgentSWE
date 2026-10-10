"""Responses-native provider: passthrough, pinning, single attempt, accounting gates."""
from __future__ import annotations

import json
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from fakes import (FAKE_KEY, FakeProvider, RunningBroker, Script, assert_no_key, options,  # noqa: E402
                   responses_completed, responses_stream_bytes, tmpdir)
from agentswe_broker.request_ledger import RequestLedger  # noqa: E402
from agentswe_broker.responses_stream import ResponseEvents  # noqa: E402


def broker_runtime_counter(stats, field):
    """Verbatim logic of formal_axes_shared.broker_runtime_counter (Editing control plane)."""
    runtime = stats.get("runtime") if isinstance(stats.get("runtime"), dict) else stats
    value = runtime.get(field, 0) if isinstance(runtime, dict) else 0
    return int(value) if isinstance(value, int) and not isinstance(value, bool) else 0


class Base(unittest.TestCase):
    role = "runtime"

    def setUp(self):
        self.provider = FakeProvider()
        self.work = tmpdir()

    def tearDown(self):
        for item in getattr(self, "_brokers", []):
            item.close()
        self.provider.close()

    def broker(self, role=None, wire="responses", **kw):
        running = RunningBroker(options(role or self.role, wire, self.provider, self.work, **kw))
        self._brokers = getattr(self, "_brokers", []) + [running]
        return running


class BuilderStreaming(Base):
    role = "builder"

    def test_stream_bytes_relayed_and_pinned(self):
        upstream_bytes = responses_stream_bytes("hello")
        self.provider.add(Script(200, content_type="text/event-stream",
                                 chunks=[upstream_bytes[:37], upstream_bytes[37:]]))
        broker = self.broker()
        status, headers, payload = broker.post("/v1/responses", {
            "model": "gpt-5.5", "stream": True, "reasoning": {"effort": "low", "summary": "auto"},
            "input": [{"type": "message", "role": "user", "content": [{"type": "input_text", "text": "x"}]}]})
        self.assertEqual(status, 200)
        self.assertEqual(payload.replace(b": keepalive\n\n", b""), upstream_bytes)
        sent = self.provider.requests[0]
        self.assertEqual(sent["path"], "/v1/responses")
        self.assertEqual(sent["body"]["model"], "deepseek-flash")
        self.assertEqual(sent["body"]["reasoning"], {"effort": "max", "summary": "auto"})
        self.assertEqual(sent["headers"]["Authorization"], "Bearer " + FAKE_KEY)
        stats = broker.stats()
        self.assertEqual(stats["schema_version"], "agentswe-builder-broker-stats/v2")
        self.assertEqual(stats["runtime"]["successful_calls"], 1)
        self.assertEqual(stats["runtime"]["actual_upstream_requests"], 1)
        self.assertEqual(stats["runtime"]["total_tokens"], 15)
        self.assertTrue(stats["runtime"]["usage_complete"])
        # The immutable ledger is recoverable by the evaluator's own recover().
        ledger_root = self.work / "builder_requests"
        broker.close()
        self._brokers = []
        summary = RequestLedger(ledger_root).recover()
        self.assertEqual((summary["calls"], summary["completed"], summary["total_tokens"]), (1, 1, 15))
        assert_no_key(self, self.work, blobs=[payload])

    def test_keepalive_only_at_event_boundaries(self):
        upstream_bytes = responses_stream_bytes("slow")
        cut = upstream_bytes.index(b"\n\n") + 2
        self.provider.add(Script(200, content_type="text/event-stream", chunk_delay=0.5,
                                 chunks=[upstream_bytes[:cut - 5], upstream_bytes[cut - 5:cut], upstream_bytes[cut:]]))
        broker = self.broker()
        status, _, payload = broker.post("/v1/responses", {"stream": True, "input": "x"})
        self.assertEqual(status, 200)
        self.assertEqual(payload.replace(b": keepalive\n\n", b""), upstream_bytes)
        decoder = ResponseEvents()
        decoder.feed(payload)
        self.assertEqual(decoder.finish()["status"], "completed")

    def test_upstream_429_reaches_codex_as_503_once(self):
        self.provider.add(Script(429, b'{"error":{"message":"slow down %s"}}' % FAKE_KEY.encode()))
        broker = self.broker()
        status, _, payload = broker.post("/v1/responses", {"stream": True, "input": "x"})
        self.assertEqual(status, 503)
        self.assertEqual(len(self.provider.requests), 1)
        stats = broker.stats()
        self.assertEqual(stats["upstream"]["status_counts"], {"429": 1})
        self.assertEqual(stats["runtime"]["provider_failures"], 1)
        assert_no_key(self, self.work, blobs=[payload])

    def test_identical_body_replays_from_ledger_without_upstream(self):
        self.provider.add(Script(200, content_type="text/event-stream", chunks=[responses_stream_bytes("a")]))
        broker = self.broker()
        body = {"stream": True, "input": "same"}
        first = broker.post("/v1/responses", body)
        second = broker.post("/v1/responses", body)
        self.assertEqual(first[2], second[2])
        self.assertEqual(len(self.provider.requests), 1)
        self.assertEqual(broker.stats()["runtime"]["cache_queries"], 1)

    def test_truncated_stream_is_a_provider_failure(self):
        upstream_bytes = responses_stream_bytes("cut")
        self.provider.add(Script(200, content_type="text/event-stream",
                                 chunks=[upstream_bytes[:upstream_bytes.index(b"response.completed")]]))
        broker = self.broker()
        # The broker ends the chunked body without a terminator: the shape of a dropped
        # provider stream, which is what Codex's reconnect ladder is built for.
        with self.assertRaises(Exception):
            broker.post("/v1/responses", {"stream": True, "input": "x"})
        stats = broker.stats()
        self.assertEqual(stats["runtime"]["successful_calls"], 0)
        self.assertEqual(stats["runtime"]["provider_failures"], 1)

    def test_wrong_client_token_never_reaches_upstream(self):
        broker = self.broker()
        status, _, _ = broker.post("/v1/responses", {"input": "x"}, token=FAKE_KEY)
        self.assertEqual(status, 401)
        self.assertEqual(self.provider.requests, [])


class EvaluatorNonStreaming(Base):
    role = "judge"

    def test_judge_body_passthrough_with_pinned_spelling(self):
        self.provider.add(Script(200, json.dumps(responses_completed('{"score": 7}')).encode()))
        broker = self.broker(effort="xhigh")  # provider spells the protocol's "max" differently
        body = {"model": "deepseek-flash", "reasoning": {"effort": "max"}, "max_output_tokens": 64000,
                "stream": False, "input": [{"type": "message", "role": "user",
                                            "content": [{"type": "input_text", "text": "judge"}]}],
                "text": {"format": {"type": "json_schema", "name": "result_judge_verdict", "strict": True,
                                    "schema": {"type": "object", "additionalProperties": False}}}}
        status, headers, payload = broker.post("/v1/responses", body, token="judge-only-placeholder")
        self.assertEqual(status, 200)
        sent = self.provider.requests[0]["body"]
        self.assertEqual(sent["max_output_tokens"], 64000)          # protocol constant untouched
        self.assertEqual(sent["reasoning"], {"effort": "xhigh"})
        self.assertEqual(sent["text"], body["text"])
        self.assertEqual(json.loads(payload)["status"], "completed")
        self.assertEqual(headers.get("X-AgentSWE-Upstream-Attempts"), "1")

    def test_editing_judge_gate_arithmetic_holds(self):
        """formal_axes_shared: successful-call delta == judged cases; call delta == transport attempts."""
        broker = self.broker()
        before = broker.stats()
        judged, transport_attempts = 0, 0
        # case 1: one clean attempt
        self.provider.add(Script(200, json.dumps(responses_completed("a")).encode()))
        # case 2: first attempt truncated by max_output_tokens (resampled), second completes
        self.provider.add(Script(200, json.dumps(responses_completed("b", status="incomplete")).encode()))
        self.provider.add(Script(200, json.dumps(responses_completed("b2")).encode()))
        # case 3: provider 503 then success (the judge client's own retry)
        self.provider.add(Script(503, b'{"error":"busy"}'))
        self.provider.add(Script(200, json.dumps(responses_completed("c")).encode()))
        for attempts in ((1,), (1, 1), (1, 1)):
            for _ in attempts:
                broker.post("/v1/responses", {"input": "q", "reasoning": {"effort": "max"}},
                            token="judge-only-placeholder")
                transport_attempts += 1
            judged += 1
        after = broker.stats()
        successful_delta = (broker_runtime_counter(after, "successful_calls")
                            - broker_runtime_counter(before, "successful_calls"))
        logical_delta = broker_runtime_counter(after, "calls") - broker_runtime_counter(before, "calls")
        self.assertEqual(successful_delta, judged)
        self.assertEqual(logical_delta, transport_attempts)
        self.assertEqual(len(self.provider.requests), transport_attempts)  # no hidden retries
        self.assertEqual(after["runtime"]["upstream_attempts"], transport_attempts)
        self.assertEqual(after["runtime"]["classifications"],
                         {"incomplete": 1, "provider_failure": 1, "success": 3})

    def test_event_ledger_has_no_bodies_or_keys(self):
        self.provider.add(Script(200, json.dumps(responses_completed("secret answer")).encode()))
        broker = self.broker()
        broker.post("/v1/responses", {"input": "PROMPT-TEXT"}, token="judge-only-placeholder")
        lines = (self.work / "ledger" / "judge-events.jsonl").read_text().splitlines()
        self.assertEqual(len(lines), 1)
        event = json.loads(lines[0])
        self.assertEqual(event["classification"], "success")
        self.assertEqual(event["usage"], {"input_tokens": 10, "output_tokens": 5, "total_tokens": 15})
        blob = "\n".join(lines)
        self.assertNotIn("PROMPT-TEXT", blob)
        self.assertNotIn("secret answer", blob)
        assert_no_key(self, self.work)

    def test_slow_provider_gets_interim_keepalives_and_one_final_response(self):
        self.provider.add(Script(200, json.dumps(responses_completed("late")).encode(), delay=1.0))
        broker = self.broker(role="runtime")
        status, _, payload = broker.post("/v1/responses", {"input": "q"}, timeout=0.6)
        self.assertEqual(status, 200)  # urllib skips 100 Continue; per-read timeout never fires
        self.assertEqual(json.loads(payload)["output"][-1]["content"][0]["text"], "late")

    def test_reported_model_mismatch_is_recorded_and_optionally_normalized(self):
        self.provider.add(Script(200, json.dumps(responses_completed("x", model="deepseek-v4-flash-0731")).encode()))
        self.provider.add(Script(200, json.dumps(responses_completed("x", model="deepseek-v4-flash-0731")).encode()))
        plain = self.broker(role="runtime")
        _, headers, payload = plain.post("/v1/responses", {"input": "q"})
        self.assertEqual(json.loads(payload)["model"], "deepseek-v4-flash-0731")
        self.assertEqual(headers.get("X-AgentSWE-Upstream-Model"), "deepseek-v4-flash-0731")
        normalized = self.broker(role="judge", normalize_reported_model=True,
                                 stats_file=self.work / "judge2.json", ledger_dir=self.work / "ledger2")
        _, _, payload = normalized.post("/v1/responses", {"input": "q"}, token="judge-only-placeholder")
        self.assertEqual(json.loads(payload)["model"], "deepseek-flash")
        event = json.loads((self.work / "ledger2" / "judge-events.jsonl").read_text())
        self.assertEqual(event["reported_model"], "deepseek-v4-flash-0731")

    def test_chat_client_over_responses_provider_never_returns_reasoning_text(self):
        self.provider.add(Script(200, json.dumps(responses_completed("final answer")).encode()))
        broker = self.broker(role="runtime")
        status, _, payload = broker.post("/v1/chat/completions", {
            "model": "gpt-4.1-2025-04-14", "messages": [{"role": "user", "content": "q"}], "stream": False})
        self.assertEqual(status, 200)
        value = json.loads(payload)
        self.assertEqual(value["object"], "chat.completion")
        self.assertEqual(value["choices"][0]["message"]["content"], "final answer")
        self.assertNotIn(b"THINKING-SHOULD-NOT-LEAK", payload)
        self.assertEqual(self.provider.requests[0]["body"]["model"], "deepseek-flash")

    def test_upstream_error_status_is_relayed_not_retried(self):
        self.provider.add(Script(500, b'{"error":"boom"}'))
        broker = self.broker(role="runtime")
        status, _, _ = broker.post("/v1/responses", {"input": "q"})
        self.assertEqual(status, 500)
        self.assertEqual(len(self.provider.requests), 1)
        stats = broker.stats()
        self.assertEqual((stats["runtime"]["calls"], stats["runtime"]["failures"]), (1, 1))

    def test_stats_require_the_stats_token(self):
        broker = self.broker(role="runtime")
        with self.assertRaises(Exception):
            broker.stats(token="broker-only-placeholder")

    def test_streaming_evaluator_client_relay(self):
        upstream_bytes = responses_stream_bytes("streamed")
        self.provider.add(Script(200, content_type="text/event-stream", chunks=[upstream_bytes]))
        broker = self.broker(role="runtime")
        status, _, payload = broker.post("/v1/responses", {"input": "q", "stream": True})
        self.assertEqual(status, 200)
        self.assertEqual(payload.replace(b": keepalive\n\n", b""), upstream_bytes)
        self.assertEqual(broker.stats()["runtime"]["successful_calls"], 1)


if __name__ == "__main__":
    unittest.main()
