"""Chat-only provider: Responses<->Chat translation over real HTTP (fake provider)."""
from __future__ import annotations

import json
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from fakes import (CHAT_USAGE, FAKE_KEY, FakeProvider, RunningBroker, Script, assert_no_key,  # noqa: E402
                   chat_chunk, options, tmpdir)
from agentswe_broker import chat_translate as T  # noqa: E402
from agentswe_broker.responses_stream import ResponseEvents  # noqa: E402

CAPTURED = json.loads((Path(__file__).resolve().parent / "captured_request.json").read_bytes())


def sse_events(payload: bytes) -> list[dict]:
    out = []
    for block in payload.decode().split("\n\n"):
        for line in block.splitlines():
            if line.startswith("data: ") and line[6:] != "[DONE]":
                out.append(json.loads(line[6:]))
    return out


class ChatBase(unittest.TestCase):
    def setUp(self):
        self.provider = FakeProvider()
        self.work = tmpdir()
        self.brokers = []

    def tearDown(self):
        for item in self.brokers:
            item.close()
        self.provider.close()

    def broker(self, role, **kw):
        running = RunningBroker(options(role, "chat", self.provider, self.work, **kw))
        self.brokers.append(running)
        return running


class BuilderOverChat(ChatBase):
    def test_codex_turn_with_apply_patch_then_reasoning_is_carried(self):
        patch = "*** Begin Patch\n*** Add File: a.py\n+print(1)\n*** End Patch\n"
        first = [chat_chunk({"reasoning_content": "I should add a.py"}),
                 chat_chunk({"tool_calls": [{"index": 0, "id": "call_ap", "type": "function",
                                             "function": {"name": "apply_patch", "arguments": ""}}]}),
                 chat_chunk({"tool_calls": [{"index": 0, "function": {"arguments": json.dumps({"input": patch})}}]}),
                 chat_chunk(finish="tool_calls"), chat_chunk(usage=CHAT_USAGE), b"data: [DONE]\n\n"]
        second = [chat_chunk({"content": "done"}), chat_chunk(finish="stop"),
                  chat_chunk(usage=CHAT_USAGE), b"data: [DONE]\n\n"]
        self.provider.add(Script(200, content_type="text/event-stream", chunks=first),
                          Script(200, content_type="text/event-stream", chunks=second))
        broker = self.broker("builder", model="Qwen/Qwen3.6-35B-A3B", effort="max")
        status, _, payload = broker.post("/v1/responses", CAPTURED)
        self.assertEqual(status, 200)
        decoder = ResponseEvents()
        decoder.feed(payload)
        final = decoder.finish()
        self.assertEqual(final["status"], "completed")
        items = final["output"]
        self.assertEqual([i["type"] for i in items], ["reasoning", "custom_tool_call"])
        self.assertEqual(items[1]["name"], "apply_patch")
        self.assertEqual(items[1]["input"], patch)
        sent = self.provider.requests[0]["body"]
        self.assertEqual(sent["model"], "Qwen/Qwen3.6-35B-A3B")
        self.assertEqual(sent["reasoning_effort"], "max")
        self.assertEqual(sent["max_tokens"], T.DEFAULT_MAX_TOKENS)
        self.assertIn("apply_patch", [t["function"]["name"] for t in sent["tools"]])
        # Next turn: codex replays history including the reasoning item and the custom call.
        follow = dict(CAPTURED)
        follow["input"] = list(CAPTURED["input"]) + [
            items[0], {"type": "custom_tool_call", "call_id": items[1]["call_id"], "name": "apply_patch",
                       "input": patch},
            {"type": "custom_tool_call_output", "call_id": items[1]["call_id"], "output": "Success"}]
        broker.post("/v1/responses", follow)
        history = self.provider.requests[1]["body"]["messages"]
        carried = [m for m in history if m.get("role") == "assistant" and m.get("tool_calls")]
        self.assertEqual(carried[-1]["reasoning_content"], "I should add a.py")
        self.assertEqual(carried[-1]["tool_calls"][0]["function"]["name"], "apply_patch")
        stats = broker.stats()
        self.assertEqual(stats["runtime"]["successful_calls"], 2)
        self.assertEqual(stats["protocol"]["upstream_wire"], "chat")
        self.assertEqual(stats["runtime"]["known_cached_input_tokens"], 6)
        receipt_dirs = list((self.work / "builder_requests").iterdir())
        receipts = [json.loads((d / "translation_receipt.json").read_text()) for d in receipt_dirs
                    if (d / "translation_receipt.json").exists()]
        self.assertTrue(all(r["reasoning_mode"] == "carry" for r in receipts))
        assert_no_key(self, self.work, blobs=[payload])

    def test_qwen_end_token_degeneration_fails_the_turn(self):
        chunks = [chat_chunk({"reasoning_content": "to understand the full scope.<|im_end|>"}),
                  chat_chunk(finish="stop"), chat_chunk(usage=CHAT_USAGE), b"data: [DONE]\n\n"]
        self.provider.add(Script(200, content_type="text/event-stream", chunks=chunks))
        broker = self.broker("builder", model="Qwen/Qwen3.6-35B-A3B")
        _, _, payload = broker.post("/v1/responses", CAPTURED)
        self.assertEqual(sse_events(payload)[-1]["type"], "response.failed")
        stats = broker.stats()
        self.assertEqual(stats["runtime"]["degenerate_responses"], 1)
        self.assertEqual(stats["runtime"]["successful_calls"], 0)


class EvaluatorOverChat(ChatBase):
    def test_nonstream_judge_gets_one_responses_object(self):
        chunks = [chat_chunk({"reasoning_content": "hidden"}), chat_chunk({"content": '{"score": 3}'}),
                  chat_chunk(finish="stop"), chat_chunk(usage=CHAT_USAGE), b"data: [DONE]\n\n"]
        self.provider.add(Script(200, content_type="text/event-stream", chunks=chunks))
        broker = self.broker("judge", model="deepseek-ai/DeepSeek-V4-Flash-0731", effort="high")
        body = {"model": "deepseek-flash", "reasoning": {"effort": "max"}, "max_output_tokens": 64000,
                "stream": False, "input": [{"type": "message", "role": "user",
                                            "content": [{"type": "input_text", "text": "judge this"}]}],
                "text": {"format": {"type": "json_schema", "name": "result_judge_verdict", "strict": True,
                                    "schema": {"type": "object", "additionalProperties": False,
                                               "properties": {"score": {"type": "integer"}}}}}}
        status, _, payload = broker.post("/v1/responses", body, token="judge-only-placeholder")
        self.assertEqual(status, 200)
        value = json.loads(payload)
        self.assertEqual(value["status"], "completed")
        self.assertEqual(value["model"], "deepseek-ai/DeepSeek-V4-Flash-0731")
        message = [i for i in value["output"] if i["type"] == "message"][0]
        self.assertEqual(message["content"][0]["text"], '{"score": 3}')
        self.assertEqual(value["usage"]["total_tokens"], 27)
        sent = self.provider.requests[0]["body"]
        self.assertEqual(sent["max_tokens"], 64000)
        self.assertEqual(sent["reasoning_effort"], "high")
        self.assertEqual(sent["response_format"]["type"], "json_schema")
        self.assertEqual(sent["response_format"]["json_schema"]["name"], "result_judge_verdict")
        stats = broker.stats()
        self.assertEqual((stats["runtime"]["calls"], stats["runtime"]["successful_calls"]), (1, 1))

    def test_length_finish_is_incomplete_not_successful(self):
        chunks = [chat_chunk({"content": "partial"}), chat_chunk(finish="length"),
                  chat_chunk(usage=CHAT_USAGE), b"data: [DONE]\n\n"]
        self.provider.add(Script(200, content_type="text/event-stream", chunks=chunks))
        broker = self.broker("judge")
        _, _, payload = broker.post("/v1/responses", {"input": "q"}, token="judge-only-placeholder")
        value = json.loads(payload)
        self.assertEqual(value["status"], "incomplete")
        self.assertEqual(value["incomplete_details"], {"reason": "max_output_tokens"})
        stats = broker.stats()
        self.assertEqual((stats["runtime"]["successful_calls"], stats["runtime"]["failures"]), (0, 1))

    def test_images_reach_a_vision_chat_provider(self):
        chunks = [chat_chunk({"content": "a cat"}), chat_chunk(finish="stop"),
                  chat_chunk(usage=CHAT_USAGE), b"data: [DONE]\n\n"]
        self.provider.add(Script(200, content_type="text/event-stream", chunks=chunks))
        broker = self.broker("runtime")
        body = {"input": [{"type": "message", "role": "user", "content": [
            {"type": "input_text", "text": "what is this"},
            {"type": "input_image", "image_url": "data:image/png;base64,AAAA", "detail": "high"}]}]}
        broker.post("/v1/responses", body)
        message = self.provider.requests[0]["body"]["messages"][-1]
        self.assertEqual(message["content"], [
            {"type": "text", "text": "what is this"},
            {"type": "image_url", "image_url": {"url": "data:image/png;base64,AAAA", "detail": "high"}}])
        event = json.loads((self.work / "ledger" / "runtime-events.jsonl").read_text())
        self.assertEqual(event["translation"]["image_parts"], 1)

    def test_streaming_evaluator_client_gets_strict_sse(self):
        chunks = [chat_chunk({"content": "hello"}), chat_chunk(finish="stop"),
                  chat_chunk(usage=CHAT_USAGE), b"data: [DONE]\n\n"]
        self.provider.add(Script(200, content_type="text/event-stream", chunks=chunks))
        broker = self.broker("runtime")
        _, _, payload = broker.post("/v1/responses", {"input": "q", "stream": True})
        decoder = ResponseEvents()
        decoder.feed(payload)
        self.assertEqual(decoder.finish()["status"], "completed")

    def test_chat_client_on_chat_provider_is_passthrough_with_pins(self):
        reply = {"id": "x", "object": "chat.completion", "model": "up",
                 "choices": [{"index": 0, "message": {"role": "assistant", "content": "ok"}, "finish_reason": "stop"}],
                 "usage": {"prompt_tokens": 3, "completion_tokens": 1, "total_tokens": 4}}
        self.provider.add(Script(200, json.dumps(reply).encode()))
        broker = self.broker("runtime", effort="high")
        status, _, payload = broker.post("/v1/chat/completions", {
            "model": "gpt-4.1-2025-04-14", "messages": [{"role": "user", "content": "q"}]})
        self.assertEqual(status, 200)
        self.assertEqual(json.loads(payload)["choices"][0]["message"]["content"], "ok")
        sent = self.provider.requests[0]["body"]
        self.assertEqual((sent["model"], sent["reasoning_effort"]), ("deepseek-flash", "high"))
        self.assertEqual(broker.stats()["runtime"]["total_tokens"], 4)

    def test_provider_error_relayed_with_redaction(self):
        self.provider.add(Script(401, b'{"error":"bad key %s"}' % FAKE_KEY.encode()))
        broker = self.broker("runtime")
        status, _, payload = broker.post("/v1/responses", {"input": "q"})
        self.assertEqual(status, 401)
        assert_no_key(self, self.work, blobs=[payload])
        self.assertEqual(broker.stats()["runtime"]["classifications"], {"provider_failure": 1})


class TranslationAdditions(unittest.TestCase):
    def test_text_only_user_message_stays_a_string(self):
        out = T.responses_to_chat({"input": [{"type": "message", "role": "user",
                                              "content": [{"type": "input_text", "text": "hi"}]}]},
                                  model="m", effort=None)
        self.assertEqual(out["body"]["messages"][0]["content"], "hi")
        self.assertNotIn("response_format", out["body"])

    def test_json_object_format(self):
        out = T.responses_to_chat({"input": "x", "text": {"format": {"type": "json_object"}}},
                                  model="m", effort=None)
        self.assertEqual(out["body"]["response_format"], {"type": "json_object"})
        self.assertEqual(out["receipt"]["response_format_sent"], "json_object")

    def test_verbosity_alone_is_still_ignored(self):
        out = T.responses_to_chat({"input": "x", "text": {"verbosity": "low"}}, model="m", effort=None)
        self.assertNotIn("response_format", out["body"])
        self.assertIn("text", out["receipt"]["ignored_request_fields"])

    def test_deepseek_cache_hit_tokens(self):
        usage = T.map_usage({"prompt_tokens": 100, "completion_tokens": 5, "total_tokens": 105,
                             "prompt_cache_hit_tokens": 64, "prompt_cache_miss_tokens": 36})
        self.assertEqual(usage["input_tokens_details"]["cached_tokens"], 64)

    def test_final_response_exposed(self):
        emitter = T.ResponsesEmitter("r", "m", 1)
        emitter.start()
        emitter.text_delta("x")
        emitter.complete({"input_tokens": 1, "output_tokens": 1, "total_tokens": 2})
        self.assertEqual(emitter.final_response["status"], "completed")
        self.assertEqual(emitter.final_response["output"][0]["content"][0]["text"], "x")


if __name__ == "__main__":
    unittest.main()
