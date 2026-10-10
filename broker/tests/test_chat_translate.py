#!/usr/bin/env python3
"""Unit tests carried over from the AgentSWE-Lite chat broker (38 tests, assertions unchanged).

Only the imports were adapted to the ``agentswe_broker`` package layout.
Original header: unit tests for package 114: request mapping and SSE event shape.

Run:  python3 tests/test_chat_translate.py
No network, no credentials, no docker.  ``captured_request.json`` is a real
``POST /v1/responses`` body emitted by codex CLI 0.144.1 in the Builder image
against a logging echo server (probe/capture_server.py).
"""
from __future__ import annotations

import json
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from agentswe_broker import chat_translate as T  # noqa: E402
from agentswe_broker.server import ChatChunks, apply_chunk  # noqa: E402

CAPTURED = json.loads((Path(__file__).resolve().parent / "captured_request.json").read_bytes())


def events(payload: bytes) -> list[dict]:
    out = []
    for block in payload.decode("utf-8").split("\n\n"):
        for line in block.splitlines():
            if line.startswith("data: ") and line[6:] != "[DONE]":
                out.append(json.loads(line[6:]))
    return out


class RequestMapping(unittest.TestCase):
    def test_captured_codex_request(self):
        result = T.responses_to_chat(CAPTURED, model="Qwen/Qwen3.6-35B-A3B", effort=None)
        body = result["body"]
        self.assertEqual(body["model"], "Qwen/Qwen3.6-35B-A3B")
        self.assertTrue(body["stream"])
        self.assertEqual(body["stream_options"], {"include_usage": True})
        self.assertNotIn("reasoning_effort", body)
        # instructions -> first system message, verbatim
        self.assertEqual(body["messages"][0]["role"], "system")
        self.assertEqual(body["messages"][0]["content"], CAPTURED["instructions"])
        # codex's developer permissions block becomes a system message
        self.assertEqual(body["messages"][1]["role"], "system")
        self.assertIn("permissions instructions", body["messages"][1]["content"])
        self.assertEqual([m["role"] for m in body["messages"]], ["system", "system", "user", "user"])
        # tools
        names = [t["function"]["name"] for t in body["tools"]]
        self.assertIn("exec_command", names)
        self.assertIn("apply_patch", names)          # Responses custom tool -> function
        self.assertIn("tool_search", names)
        self.assertNotIn(None, names)
        self.assertEqual(result["custom_tool_names"], ["apply_patch"])
        patch = [t for t in body["tools"] if t["function"]["name"] == "apply_patch"][0]
        self.assertEqual(patch["function"]["parameters"]["required"], ["input"])
        self.assertIn("*** Begin Patch", patch["function"]["description"])
        # every function tool is a valid chat tool object
        for tool in body["tools"]:
            self.assertEqual(tool["type"], "function")
            self.assertIsInstance(tool["function"]["name"], str)
            self.assertIsInstance(tool["function"]["parameters"], dict)
        self.assertEqual(body["tool_choice"], "auto")
        self.assertTrue(body["parallel_tool_calls"])
        # dropped / ignored, recorded rather than silent
        receipt = result["receipt"]
        self.assertEqual([d["type"] for d in receipt["dropped_tools"]], ["web_search"])
        for name in ("store", "include", "prompt_cache_key", "text", "client_metadata"):
            self.assertIn(name, receipt["ignored_request_fields"])
        # nothing that could smuggle a key or server-side state survives
        for name in ("store", "include", "prompt_cache_key", "text", "client_metadata",
                     "previous_response_id", "instructions", "input", "reasoning"):
            self.assertNotIn(name, body)

    def test_namespace_tools_are_flattened(self):
        chat, custom, dropped = T.map_tools([
            {"type": "namespace", "name": "multi_agent_v1", "description": "subagents",
             "tools": [{"type": "function", "name": "spawn_agent", "parameters": {}},
                       {"type": "function", "name": "close_agent", "parameters": {}}]},
            {"type": "function", "name": "exec_command", "parameters": {}}])
        self.assertEqual([t["function"]["name"] for t in chat],
                         ["spawn_agent", "close_agent", "exec_command"])
        self.assertEqual(dropped, [])

    def test_effort_lock_and_passthrough(self):
        locked = T.responses_to_chat(CAPTURED, model="m", effort="high")["body"]
        self.assertEqual(locked["reasoning_effort"], "high")
        through = T.responses_to_chat(CAPTURED, model="m", effort="high",
                                      effort_policy="passthrough")["body"]
        self.assertEqual(through["reasoning_effort"], CAPTURED["reasoning"]["effort"])

    def test_tool_call_round_trip(self):
        body = {"instructions": "sys", "input": [
            {"type": "message", "role": "user", "content": [{"type": "input_text", "text": "hi"}]},
            {"type": "reasoning", "summary": [{"type": "summary_text", "text": "thinking"}]},
            {"type": "message", "role": "assistant",
             "content": [{"type": "output_text", "text": "let me look"}]},
            {"type": "function_call", "call_id": "call_a", "name": "exec_command",
             "arguments": '{"cmd":"ls"}'},
            {"type": "function_call_output", "call_id": "call_a", "output": "a.py"},
            {"type": "custom_tool_call", "call_id": "call_b", "name": "apply_patch",
             "input": "*** Begin Patch\n*** End Patch\n"},
            {"type": "custom_tool_call_output", "call_id": "call_b", "output": "ok"},
        ], "tools": [
            {"type": "function", "name": "exec_command", "description": "run",
             "parameters": {"type": "object", "properties": {}}, "strict": False},
            {"type": "custom", "name": "apply_patch", "description": "patch",
             "format": {"type": "grammar", "syntax": "lark", "definition": "start: x"}},
        ]}
        result = T.responses_to_chat(body, model="m", effort=None, reasoning_mode="drop")
        messages = result["body"]["messages"]
        self.assertEqual([m["role"] for m in messages],
                         ["system", "user", "assistant", "tool", "assistant", "tool"])
        # the assistant text message and the following function_call fuse into
        # one assistant turn carrying tool_calls, as chat requires
        self.assertEqual(messages[2]["content"], "let me look")
        self.assertEqual(messages[2]["tool_calls"][0]["id"], "call_a")
        self.assertEqual(messages[2]["tool_calls"][0]["function"],
                         {"name": "exec_command", "arguments": '{"cmd":"ls"}'})
        self.assertEqual(messages[3], {"role": "tool", "tool_call_id": "call_a", "content": "a.py"})
        self.assertEqual(json.loads(messages[4]["tool_calls"][0]["function"]["arguments"]),
                         {"input": "*** Begin Patch\n*** End Patch\n"})
        self.assertNotIn("reasoning_content", messages[2])

    def test_reasoning_carry_mode(self):
        body = {"input": [
            {"type": "reasoning", "summary": [{"type": "summary_text", "text": "because"}]},
            {"type": "function_call", "call_id": "c", "name": "f", "arguments": "{}"},
        ]}
        messages = T.responses_to_chat(body, model="m", effort=None,
                                       reasoning_mode="carry")["body"]["messages"]
        self.assertEqual(messages[0]["reasoning_content"], "because")

    def test_lite_default_carries_reasoning_on_all_turns(self):
        body = {"input": [
            {"type": "message", "role": "user", "content": [{"type": "input_text", "text": "fix it"}]},
            {"type": "reasoning", "summary": [{"type": "summary_text", "text": "look first"}]},
            {"type": "function_call", "call_id": "a", "name": "exec_command", "arguments": "{}"},
            {"type": "function_call_output", "call_id": "a", "output": "x.py"},
            {"type": "reasoning", "summary": [{"type": "summary_text", "text": "part one"}]},
            {"type": "reasoning", "summary": [{"type": "summary_text", "text": "part two"}]},
            {"type": "custom_tool_call", "call_id": "b", "name": "apply_patch", "input": "*** Begin Patch\n*** End Patch\n"},
            {"type": "custom_tool_call_output", "call_id": "b", "output": "ok"},
            {"type": "reasoning", "summary": [{"type": "summary_text", "text": "done"}]},
            {"type": "message", "role": "assistant", "content": [{"type": "output_text", "text": "fixed"}]},
        ]}
        messages = T.responses_to_chat(body, model="m", effort=None)["body"]["messages"]
        roles = [m["role"] for m in messages]
        self.assertEqual(roles, ["user", "assistant", "tool", "assistant", "tool", "assistant"])
        self.assertEqual(messages[1]["reasoning_content"], "look first")
        self.assertEqual(messages[3]["reasoning_content"], "part one\n\npart two")
        self.assertEqual(messages[3]["tool_calls"][0]["function"]["name"], "apply_patch")
        self.assertEqual(messages[4], {"role": "tool", "tool_call_id": "b", "content": "ok"})
        self.assertEqual(messages[5]["reasoning_content"], "done")
        self.assertEqual(messages[5]["content"], "fixed")

    def test_parallel_calls_group_into_one_assistant_message(self):
        body = {"input": [
            {"type": "function_call", "call_id": "a", "name": "f", "arguments": "{}"},
            {"type": "function_call", "call_id": "b", "name": "f", "arguments": "{}"},
            {"type": "function_call_output", "call_id": "a", "output": "1"},
            {"type": "function_call_output", "call_id": "b", "output": "2"},
        ]}
        messages = T.responses_to_chat(body, model="m", effort=None)["body"]["messages"]
        self.assertEqual([m["role"] for m in messages], ["assistant", "tool", "tool"])
        self.assertEqual([c["id"] for c in messages[0]["tool_calls"]], ["a", "b"])

    def test_previous_response_id_refused(self):
        with self.assertRaises(T.TranslationError):
            T.responses_to_chat({"previous_response_id": "resp_1", "input": []}, model="m", effort=None)

    def test_max_output_tokens_and_tool_choice(self):
        body = {"input": [{"type": "message", "role": "user", "content": "x"}],
                "max_output_tokens": 4096,
                "tools": [{"type": "function", "name": "f", "parameters": {}}],
                "tool_choice": {"type": "function", "name": "f"}}
        out = T.responses_to_chat(body, model="m", effort=None)["body"]
        self.assertEqual(out["max_tokens"], 4096)
        self.assertEqual(out["tool_choice"], {"type": "function", "function": {"name": "f"}})


class DefaultMaxTokens(unittest.TestCase):
    """codex 0.144.1 never sends max_output_tokens (see the captured request)."""

    def test_the_captured_codex_request_has_no_cap_of_its_own(self):
        self.assertNotIn("max_output_tokens", CAPTURED)

    def test_broker_default_is_applied_when_codex_sends_none(self):
        result = T.responses_to_chat(CAPTURED, model="m", effort=None)
        self.assertEqual(result["body"]["max_tokens"], T.DEFAULT_MAX_TOKENS)
        self.assertEqual(result["receipt"]["max_tokens_source"], "broker_default")
        self.assertEqual(result["receipt"]["max_tokens_sent"], T.DEFAULT_MAX_TOKENS)

    def test_explicit_broker_value_wins(self):
        result = T.responses_to_chat(CAPTURED, model="m", effort=None,
                                     default_max_tokens=131072)
        self.assertEqual(result["body"]["max_tokens"], 131072)
        self.assertEqual(result["receipt"]["max_tokens_source"], "broker_default")

    def test_zero_restores_the_provider_default(self):
        result = T.responses_to_chat(CAPTURED, model="m", effort=None, default_max_tokens=0)
        self.assertNotIn("max_tokens", result["body"])
        self.assertEqual(result["receipt"]["max_tokens_source"], "provider_default")
        self.assertIsNone(result["receipt"]["max_tokens_sent"])

    def test_a_request_that_carries_its_own_cap_is_untouched(self):
        body = dict(CAPTURED, max_output_tokens=4096)
        result = T.responses_to_chat(body, model="m", effort=None, default_max_tokens=65536)
        self.assertEqual(result["body"]["max_tokens"], 4096)
        self.assertEqual(result["receipt"]["max_tokens_source"], "request")

    def test_environment_override(self):
        import os
        previous = os.environ.get(T.DEFAULT_MAX_TOKENS_ENV)
        os.environ[T.DEFAULT_MAX_TOKENS_ENV] = "1234"
        try:
            self.assertEqual(T.default_max_tokens(), 1234)
            # an explicit value still wins over the environment
            self.assertEqual(T.default_max_tokens(4096), 4096)
        finally:
            if previous is None:
                del os.environ[T.DEFAULT_MAX_TOKENS_ENV]
            else:
                os.environ[T.DEFAULT_MAX_TOKENS_ENV] = previous

    def test_negative_is_refused(self):
        with self.assertRaises(T.TranslationError):
            T.default_max_tokens(-1)

    def test_length_finish_still_reports_incomplete(self):
        """The failure mode this default prevents, and what it looked like."""
        em = T.ResponsesEmitter("resp_len", "Qwen/Qwen3.6-35B-A3B", 1)
        payload = em.start() + em.reasoning_delta("thinking and thinking")
        payload += em.complete({"input_tokens": 1, "input_tokens_details": {"cached_tokens": 0},
                                "output_tokens": 2,
                                "output_tokens_details": {"reasoning_tokens": 2},
                                "total_tokens": 3},
                               incomplete_reason="max_output_tokens")
        terminal = events(payload)[-1]
        self.assertEqual(terminal["type"], "response.incomplete")
        self.assertEqual(terminal["response"]["incomplete_details"],
                         {"reason": "max_output_tokens"})


class UsageMapping(unittest.TestCase):
    def test_deepinfra_usage(self):
        value = T.map_usage({"prompt_tokens": 392, "total_tokens": 522, "completion_tokens": 130,
                             "estimated_cost": 4.692e-05,
                             "prompt_tokens_details": {"cached_tokens": 7, "cache_write_tokens": None},
                             "completion_tokens_details": {"reasoning_tokens": 24}})
        self.assertEqual(value, {"input_tokens": 392, "input_tokens_details": {"cached_tokens": 7},
                                 "output_tokens": 130, "output_tokens_details": {"reasoning_tokens": 24},
                                 "total_tokens": 522, "estimated_cost": 4.692e-05})

    def test_qwen_usage_without_details(self):
        value = T.map_usage({"prompt_tokens": 303, "total_tokens": 442, "completion_tokens": 139,
                             "estimated_cost": 0.00016235, "prompt_tokens_details": None})
        self.assertEqual(value["input_tokens_details"], {"cached_tokens": 0})
        self.assertEqual(value["output_tokens_details"], {"reasoning_tokens": 0})

    def test_unknown_usage(self):
        self.assertIsNone(T.map_usage(None))
        self.assertIsNone(T.map_usage({"prompt_tokens": 1}))


def chunk(**delta):
    return {"id": "chatcmpl-x", "object": "chat.completion.chunk", "model": "m",
            "choices": [{"index": 0, "delta": delta, "finish_reason": None}], "usage": None}


class StreamMapping(unittest.TestCase):
    def emit(self, chunks, custom=(), emit_reasoning=True):
        em = T.ResponsesEmitter("resp_t", "m", 1, custom_tool_names=custom,
                                emit_reasoning=emit_reasoning)
        payload = bytearray(em.start())
        usage = None
        finish = None
        for value in chunks:
            data, chunk_usage, chunk_finish = apply_chunk(em, value)
            payload += data
            usage = chunk_usage or usage
            finish = chunk_finish or finish
        payload += em.complete(usage, incomplete_reason="max_output_tokens" if finish == "length" else None)
        return em, events(bytes(payload))

    def test_text_only(self):
        em, ev = self.emit([chunk(role="assistant", content=""), chunk(content="he"),
                            chunk(content="llo"),
                            {"choices": [], "usage": {"prompt_tokens": 1, "completion_tokens": 2,
                                                      "total_tokens": 3, "estimated_cost": 0.5}}])
        self.assertEqual([e["type"] for e in ev], [
            "response.created", "response.in_progress", "response.output_item.added",
            "response.content_part.added", "response.output_text.delta", "response.output_text.delta",
            "response.output_text.done", "response.content_part.done", "response.output_item.done",
            "response.completed"])
        self.assertEqual([e["sequence_number"] for e in ev], list(range(len(ev))))
        done = ev[-1]["response"]
        self.assertEqual(done["status"], "completed")
        self.assertEqual(done["output"][0]["content"][0]["text"], "hello")
        self.assertEqual(done["usage"]["total_tokens"], 3)
        self.assertEqual(done["usage"]["estimated_cost"], 0.5)

    def test_reasoning_then_text(self):
        em, ev = self.emit([chunk(reasoning_content="why"), chunk(reasoning_content=" not"),
                            chunk(content="ok")])
        kinds = [e["type"] for e in ev]
        self.assertEqual(kinds[2:4], ["response.output_item.added",
                                      "response.reasoning_summary_part.added"])
        self.assertIn("response.reasoning_summary_text.delta", kinds)
        self.assertIn("response.reasoning_summary_text.done", kinds)
        output = ev[-1]["response"]["output"]
        self.assertEqual(output[0]["type"], "reasoning")
        self.assertEqual(output[0]["summary"][0]["text"], "why not")
        self.assertEqual(output[1]["type"], "message")
        # reasoning is item 0 and the message is item 1
        added = [e for e in ev if e["type"] == "response.output_item.added"]
        self.assertEqual([e["output_index"] for e in added], [0, 1])

    def test_reasoning_suppressed(self):
        em, ev = self.emit([chunk(reasoning_content="why"), chunk(content="ok")],
                           emit_reasoning=False)
        self.assertEqual([o["type"] for o in ev[-1]["response"]["output"]], ["message"])

    def test_two_tool_calls(self):
        chunks = [
            chunk(role="assistant", content=""),
            chunk(reasoning_content="plan"),
            chunk(tool_calls=[{"index": 0, "id": "t0", "type": "function",
                               "function": {"name": "exec_command", "arguments": ""}}]),
            chunk(tool_calls=[{"index": 0, "function": {"arguments": '{"cmd":'}}]),
            chunk(tool_calls=[{"index": 0, "function": {"arguments": '"ls"}'}}]),
            chunk(tool_calls=[{"index": 1, "id": "t1", "type": "function",
                               "function": {"name": "apply_patch", "arguments": ""}}]),
            chunk(tool_calls=[{"index": 1, "function": {"arguments": '{"input": "PATCH"}'}}]),
            {"choices": [{"index": 0, "delta": {}, "finish_reason": "tool_calls"}],
             "usage": {"prompt_tokens": 10, "completion_tokens": 4, "total_tokens": 14}},
        ]
        em, ev = self.emit(chunks, custom=("apply_patch",))
        kinds = [e["type"] for e in ev]
        self.assertIn("response.function_call_arguments.delta", kinds)
        self.assertIn("response.function_call_arguments.done", kinds)
        self.assertIn("response.custom_tool_call_input.done", kinds)
        output = ev[-1]["response"]["output"]
        self.assertEqual([o["type"] for o in output],
                         ["reasoning", "function_call", "custom_tool_call"])
        self.assertEqual(output[1], {"id": "fc_resp_t_0", "type": "function_call",
                                     "call_id": "t0", "name": "exec_command",
                                     "arguments": '{"cmd":"ls"}', "status": "completed"})
        self.assertEqual(output[2]["call_id"], "t1")
        self.assertEqual(output[2]["input"], "PATCH")
        # output_index is contiguous and matches the item order
        added = [e for e in ev if e["type"] == "response.output_item.added"]
        self.assertEqual([e["output_index"] for e in added], [0, 1, 2])

    def test_empty_arguments_become_an_object(self):
        em, ev = self.emit([chunk(tool_calls=[{"index": 0, "id": "t", "type": "function",
                                               "function": {"name": "get_goal", "arguments": ""}}])])
        self.assertEqual(ev[-1]["response"]["output"][0]["arguments"], "{}")

    def test_length_finish_is_incomplete(self):
        em, ev = self.emit([chunk(content="partial"),
                            {"choices": [{"index": 0, "delta": {}, "finish_reason": "length"}],
                             "usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2}}])
        self.assertEqual(ev[-1]["type"], "response.incomplete")
        self.assertEqual(ev[-1]["response"]["incomplete_details"], {"reason": "max_output_tokens"})

    def test_failed_event(self):
        em = T.ResponsesEmitter("resp_e", "m", 1)
        payload = em.start() + em.failed("upstream_error", "429 rate limited", status=429)
        ev = events(payload)
        self.assertEqual([e["type"] for e in ev][-2:], ["error", "response.failed"])
        self.assertEqual(ev[-1]["response"]["status"], "failed")
        self.assertEqual(ev[-1]["response"]["error"]["upstream_status"], 429)

    def test_done_marker_and_terminal_present(self):
        em = T.ResponsesEmitter("resp_d", "m", 1)
        payload = em.start() + em.text_delta("x") + em.complete(
            {"input_tokens": 1, "input_tokens_details": {"cached_tokens": 0}, "output_tokens": 1,
             "output_tokens_details": {"reasoning_tokens": 0}, "total_tokens": 2})
        self.assertTrue(payload.endswith(b"data: [DONE]\n\n"))
        # the evaluator's own strict Responses decoder must accept it verbatim
        sys.path.insert(0, str(ROOT))
        from agentswe_broker.responses_stream import ResponseEvents
        decoder = ResponseEvents()
        decoder.feed(payload)
        response = decoder.finish()
        self.assertEqual(response["status"], "completed")
        self.assertEqual(response["usage"]["total_tokens"], 2)


class DegenerateResponses(unittest.TestCase):
    """The DeepInfra/Qwen chat-template artefact measured 2026-09-22."""

    # verbatim from the Editing smoke run aider/0922-dq-r-001, builder broker ledger
    #   53a9587733.../upstream.raw   (last two data: lines, truncated)
    CAPTURED_TAIL = (
        "This is a complex task. Let me read the other input files "
        "to understand the full scope.<|im_end|>")

    def test_measured_signature_is_flagged(self):
        reason = T.degenerate_reasoning_only(
            finish_reason="stop", tool_calls=0, content="",
            reasoning=self.CAPTURED_TAIL)
        self.assertEqual(reason, "chat_template_end_token_in_reasoning:<|im_end|>")

    def test_healthy_tool_call_turn_is_not_flagged(self):
        self.assertIsNone(T.degenerate_reasoning_only(
            finish_reason="tool_calls", tool_calls=3, content="",
            reasoning="Let me start by reading the input files.\n"))

    def test_plain_answer_is_not_flagged(self):
        self.assertIsNone(T.degenerate_reasoning_only(
            finish_reason="stop", tool_calls=0, content="All done.",
            reasoning="thinking<|im_end|>"))

    def test_reasoning_only_without_an_end_token_is_not_flagged_by_default(self):
        self.assertIsNone(T.degenerate_reasoning_only(
            finish_reason="stop", tool_calls=0, content="",
            reasoning="I think the task is finished."))

    def test_policy_any_flags_every_reasoning_only_stop(self):
        self.assertEqual(T.degenerate_reasoning_only(
            finish_reason="stop", tool_calls=0, content="",
            reasoning="I think the task is finished.", policy="any"),
            "reasoning_only_stop")

    def test_policy_off_never_flags(self):
        self.assertIsNone(T.degenerate_reasoning_only(
            finish_reason="stop", tool_calls=0, content="",
            reasoning=self.CAPTURED_TAIL, policy="off"))

    def test_length_stop_is_never_degenerate(self):
        self.assertIsNone(T.degenerate_reasoning_only(
            finish_reason="length", tool_calls=0, content="",
            reasoning=self.CAPTURED_TAIL))

    def test_emitter_counts_tool_calls_for_the_detector(self):
        em = T.ResponsesEmitter("resp_d", "m", 1)
        self.assertEqual(em.tool_calls_seen, 0)
        em.tool_call_delta(0, "t0", "exec_command", "{}")
        em.tool_call_delta(0, None, None, "")
        em.tool_call_delta(1, "t1", "exec_command", "{}")
        self.assertEqual(em.tool_calls_seen, 2)

    def test_replayed_stream_reproduces_the_failure_path(self):
        """Feed the captured shape through the emitter and assert the outcome."""
        em = T.ResponsesEmitter("resp_r", "Qwen/Qwen3.6-35B-A3B", 1)
        payload = bytearray(em.start())
        for piece in ("This is a complex task. Let me read the other input files ",
                      "to understand the full scope.", "<|im_end|>"):
            payload += em.reasoning_delta(piece)
        self.assertEqual(em.tool_calls_seen, 0)
        self.assertEqual(em.message_text, "")
        reason = T.degenerate_reasoning_only(
            finish_reason="stop", tool_calls=em.tool_calls_seen,
            content=em.message_text, reasoning=em.reasoning_text)
        self.assertTrue(reason)
        payload += em.failed("degenerate_reasoning_only_response", reason, status=200)
        kinds = [e["type"] for e in events(bytes(payload))]
        self.assertEqual(kinds[-2:], ["error", "response.failed"])
        self.assertNotIn("response.completed", kinds)


class ChatDecoder(unittest.TestCase):
    def test_split_across_reads(self):
        decoder = ChatChunks()
        raw = b'data: {"a": 1}\n\ndata: {"b": 2}\n\ndata: [DONE]\n\n'
        got = []
        for i in range(0, len(raw), 5):
            got.extend(decoder.feed(raw[i:i + 5]))
        self.assertEqual(got, [{"a": 1}, {"b": 2}])
        self.assertTrue(decoder.done)


if __name__ == "__main__":
    unittest.main(verbosity=2)
