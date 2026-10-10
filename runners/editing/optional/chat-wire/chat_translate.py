"""Pure, network-free translation between the OpenAI Responses and Chat wire formats.

This module holds every field mapping used by ``builder_broker_chat_upstream``.
It never touches credentials, sockets, retries or the request ledger, so the
unit tests can replay a captured codex request and a synthetic chat stream
without a provider.

Reference points (measured):
  * codex CLI 0.144.1 refuses ``wire_api="chat"`` in a provider config, so the
    Builder can only speak Responses.  A captured ``POST /v1/responses`` from
    the real image is stored next to this file as ``tests/captured_request.json``.
  * DeepInfra (``https://api.deepinfra.com/v1/openai``) serves only Chat
    Completions; ``/v1/openai/responses`` is 404.
"""
from __future__ import annotations
import os

import json
from typing import Any

# codex 0.144.1 sends these and a Chat upstream has no equivalent; they are
# dropped on purpose and counted in the translation receipt.
IGNORED_REQUEST_FIELDS = (
    "store", "include", "prompt_cache_key", "text", "truncation", "metadata",
    "client_metadata", "service_tier", "safety_identifier", "user", "background",
    "instructions_role", "stream_options", "conversation",
)

# Tool wrappers codex may send that are not plain functions.
CUSTOM_TOOL_INPUT = "input"


class TranslationError(ValueError):
    """The Responses request cannot be represented on a Chat upstream."""


def _text_of(content: Any) -> str:
    """Flatten a Responses content array into one chat text string."""
    if isinstance(content, str):
        return content
    chunks: list[str] = []
    if isinstance(content, list):
        for part in content:
            if not isinstance(part, dict):
                continue
            kind = part.get("type")
            if kind in ("input_text", "output_text", "summary_text", "text", "refusal"):
                value = part.get("text", part.get("refusal", ""))
                if isinstance(value, str):
                    chunks.append(value)
            elif kind in ("input_image", "input_file", "image_url"):
                # A Chat upstream that is text-only cannot carry these; keep a
                # visible placeholder rather than silently losing the turn.
                chunks.append("[unsupported content part: %s]" % kind)
    return "".join(chunks)


def _output_text(value: Any) -> str:
    if isinstance(value, str):
        return value
    if isinstance(value, list):
        return _text_of(value)
    if value is None:
        return ""
    return json.dumps(value, ensure_ascii=False)


def _chat_role(role: str) -> str:
    if role == "developer":
        # DeepInfra's OpenAI shim (vLLM/SGLang) does not accept the developer
        # role; codex's permission block is instruction text, so it becomes a
        # system message immediately after `instructions`.
        return "system"
    if role in ("system", "user", "assistant", "tool"):
        return role
    raise TranslationError("unsupported message role: %r" % (role,))


def map_tools(tools: Any) -> tuple[list[dict], set[str], list[dict]]:
    """Return (chat tools, names declared as Responses ``custom`` tools, dropped).

    * ``{"type":"function", name, description, parameters, strict}``
      -> ``{"type":"function","function":{name, description, parameters, strict}}``
    * ``{"type":"custom", name, description, format}`` (codex's freeform
      ``apply_patch``) -> a function taking one required string ``input``; the
      grammar travels in the description so the model can still produce a patch.
      Its call is translated back to a Responses ``custom_tool_call`` item.
    * ``{"type":"tool_search", ...}`` -> a function named ``tool_search``.
    * ``{"type":"namespace", name, tools:[...]}`` -> its member tools, flattened.
    * anything else (``web_search``, ``local_shell``, ...) is dropped and listed.
    """
    chat: list[dict] = []
    custom: set[str] = set()
    dropped: list[dict] = []
    if not isinstance(tools, list):
        return chat, custom, dropped
    # codex 0.144.1 groups its sub-agent tools in a ``namespace`` wrapper
    # (``multi_agent_v1``); Chat Completions has no nesting, so the namespace is
    # flattened into its member tools, which are ordinary functions.
    flat: list[Any] = []
    for tool in tools:
        if isinstance(tool, dict) and tool.get("type") == "namespace" and isinstance(tool.get("tools"), list):
            flat.extend(tool["tools"])
        else:
            flat.append(tool)
    for tool in flat:
        if not isinstance(tool, dict):
            dropped.append({"tool": repr(tool)[:120], "reason": "not an object"})
            continue
        kind = tool.get("type")
        if kind == "function":
            function: dict[str, Any] = {"name": tool.get("name")}
            if isinstance(tool.get("description"), str):
                function["description"] = tool["description"]
            parameters = tool.get("parameters")
            function["parameters"] = parameters if isinstance(parameters, dict) else {
                "type": "object", "properties": {}, "required": [], "additionalProperties": False}
            if isinstance(tool.get("strict"), bool):
                function["strict"] = tool["strict"]
            chat.append({"type": "function", "function": function})
        elif kind == "custom":
            name = tool.get("name")
            if not isinstance(name, str) or not name:
                dropped.append({"type": kind, "reason": "custom tool without a name"})
                continue
            custom.add(name)
            grammar = ""
            fmt = tool.get("format")
            if isinstance(fmt, dict) and isinstance(fmt.get("definition"), str):
                grammar = ("\n\nThe value of `%s` must match this %s grammar:\n%s"
                           % (CUSTOM_TOOL_INPUT, fmt.get("syntax", "freeform"), fmt["definition"]))
            chat.append({"type": "function", "function": {
                "name": name,
                "description": (tool.get("description") or "") + grammar,
                "parameters": {"type": "object", "properties": {
                    CUSTOM_TOOL_INPUT: {"type": "string",
                                        "description": "The raw freeform payload for this tool."}},
                    "required": [CUSTOM_TOOL_INPUT], "additionalProperties": False},
            }})
        elif kind == "tool_search":
            parameters = tool.get("parameters")
            chat.append({"type": "function", "function": {
                "name": "tool_search",
                "description": tool.get("description") or "Search for deferred tools.",
                "parameters": parameters if isinstance(parameters, dict) else {
                    "type": "object", "properties": {}, "required": []},
            }})
        else:
            dropped.append({"type": kind, "reason": "no Chat Completions equivalent"})
    return chat, custom, dropped


def map_tool_choice(value: Any) -> Any:
    if value in (None, "auto", "none", "required"):
        return value
    if isinstance(value, dict):
        if value.get("type") == "function" and isinstance(value.get("name"), str):
            return {"type": "function", "function": {"name": value["name"]}}
        if value.get("type") == "function" and isinstance(value.get("function"), dict):
            return value
        if value.get("type") in ("custom", "allowed_tools"):
            return "auto"
    raise TranslationError("unsupported tool_choice: %r" % (value,))


def responses_to_chat(body: dict, *, model: str, effort: str | None,
                      effort_policy: str = "lock", reasoning_mode: str = "drop") -> dict:
    """Translate one codex Responses request body into a Chat Completions body.

    ``effort_policy='lock'`` forces ``reasoning_effort`` to the broker's own
    ``effort`` (``None``/empty means: send no ``reasoning_effort`` at all, which
    is how ``Qwen/Qwen3.6-35B-A3B`` is run -- it thinks by default -- and how
    ``deepseek-ai/DeepSeek-V4-Flash-0731`` is run when thinking is not wanted).
    ``'passthrough'`` forwards ``reasoning.effort`` from the request.
    """
    if not isinstance(body, dict):
        raise TranslationError("request is not an object")
    if body.get("previous_response_id") not in (None, ""):
        # codex sets store:false, so it must never chain server-side state; a
        # stateless adapter cannot honour it and must not pretend it did.
        raise TranslationError("previous_response_id is not supported by a stateless chat adapter")

    messages: list[dict[str, Any]] = []
    instructions = body.get("instructions")
    if isinstance(instructions, str) and instructions:
        messages.append({"role": "system", "content": instructions})

    _, custom_names, dropped_tools = map_tools(body.get("tools"))

    def last_assistant_open() -> dict | None:
        """The assistant message a new tool call may still be attached to."""
        if not messages:
            return None
        tail = messages[-1]
        return tail if tail.get("role") == "assistant" else None

    def add_tool_call(call_id: str, name: str, arguments: str) -> None:
        tail = last_assistant_open()
        if tail is None:
            tail = {"role": "assistant", "content": None, "tool_calls": []}
            messages.append(tail)
        tail.setdefault("tool_calls", [])
        tail["tool_calls"].append({"id": call_id, "type": "function",
                                   "function": {"name": name, "arguments": arguments or "{}"}})

    carried_reasoning: str | None = None
    unsupported_items: list[str] = []
    items = body.get("input")
    if isinstance(items, str):
        messages.append({"role": "user", "content": items})
        items = []
    if items is None:
        items = []
    if not isinstance(items, list):
        raise TranslationError("input must be a list of items")

    for item in items:
        if not isinstance(item, dict):
            raise TranslationError("input item is not an object")
        kind = item.get("type", "message")
        if kind == "message":
            role = item.get("role")
            if not isinstance(role, str):
                raise TranslationError("message item without a role")
            text = _text_of(item.get("content"))
            chat_role = _chat_role(role)
            if chat_role == "assistant":
                message: dict[str, Any] = {"role": "assistant", "content": text}
                if carried_reasoning and reasoning_mode == "carry":
                    message["reasoning_content"] = carried_reasoning
                carried_reasoning = None
                messages.append(message)
            else:
                messages.append({"role": chat_role, "content": text})
        elif kind == "function_call":
            call_id = item.get("call_id") or item.get("id")
            name = item.get("name")
            if not isinstance(call_id, str) or not isinstance(name, str):
                raise TranslationError("function_call without call_id/name")
            arguments = item.get("arguments")
            if not isinstance(arguments, str):
                arguments = json.dumps(arguments if arguments is not None else {}, ensure_ascii=False)
            add_tool_call(call_id, name, arguments)
            if carried_reasoning and reasoning_mode == "carry":
                messages[-1].setdefault("reasoning_content", carried_reasoning)
            carried_reasoning = None
        elif kind == "custom_tool_call":
            call_id = item.get("call_id") or item.get("id")
            name = item.get("name")
            if not isinstance(call_id, str) or not isinstance(name, str):
                raise TranslationError("custom_tool_call without call_id/name")
            add_tool_call(call_id, name,
                          json.dumps({CUSTOM_TOOL_INPUT: item.get("input") or ""}, ensure_ascii=False))
        elif kind in ("function_call_output", "custom_tool_call_output"):
            call_id = item.get("call_id")
            if not isinstance(call_id, str):
                raise TranslationError("tool output without a call_id")
            messages.append({"role": "tool", "tool_call_id": call_id,
                             "content": _output_text(item.get("output"))})
        elif kind == "reasoning":
            summary = _text_of(item.get("summary")) or _text_of(item.get("content"))
            carried_reasoning = summary or None
            # Dropped by default: DeepInfra has no server-side reasoning state,
            # and codex's `include:["reasoning.encrypted_content"]` cannot be
            # honoured, so replaying a summary as a user-visible turn would
            # change the transcript the Builder is measured on.
        else:
            unsupported_items.append(str(kind))

    if not messages:
        raise TranslationError("request produced no chat messages")

    chat_tools, custom_names, dropped_tools = map_tools(body.get("tools"))
    out: dict[str, Any] = {"model": model, "messages": messages, "stream": True,
                           "stream_options": {"include_usage": True}}
    if chat_tools:
        out["tools"] = chat_tools
        choice = map_tool_choice(body.get("tool_choice"))
        if choice is not None:
            out["tool_choice"] = choice
        if isinstance(body.get("parallel_tool_calls"), bool):
            out["parallel_tool_calls"] = body["parallel_tool_calls"]
    if isinstance(body.get("max_output_tokens"), int) and body["max_output_tokens"] > 0:
        out["max_tokens"] = body["max_output_tokens"]
    else:
        # codex 0.144.1 never sends max_output_tokens; without an explicit cap DeepInfra applies its own
        # (small) default and long reasoning turns end with finish_reason=length -> response.incomplete ->
        # codex turn.failed (observed on Qwen3.6-35B-A3B, 178, 2026-09-22). Both models accept 131072.
        _default = int(os.environ.get("AGENTSWE_CHAT_BROKER_DEFAULT_MAX_TOKENS", "131072"))
        if _default > 0:
            out["max_tokens"] = _default
    for name in ("temperature", "top_p", "frequency_penalty", "presence_penalty", "seed", "stop"):
        if body.get(name) is not None:
            out[name] = body[name]

    chosen_effort = effort
    if effort_policy == "passthrough":
        reasoning = body.get("reasoning")
        chosen_effort = reasoning.get("effort") if isinstance(reasoning, dict) else None
    elif effort_policy != "lock":
        raise TranslationError("unknown effort policy: %r" % (effort_policy,))
    if chosen_effort:
        out["reasoning_effort"] = chosen_effort

    receipt = {"custom_tools": sorted(custom_names), "dropped_tools": dropped_tools,
               "unsupported_input_items": sorted(set(unsupported_items)),
               "ignored_request_fields": sorted(
                   name for name in IGNORED_REQUEST_FIELDS if name in body),
               "messages": len(messages), "tools": len(chat_tools),
               "reasoning_effort_sent": out.get("reasoning_effort"),
               "reasoning_mode": reasoning_mode}
    return {"body": out, "receipt": receipt, "custom_tool_names": sorted(custom_names)}


# ---------------------------------------------------------------------------
# chat stream -> Responses SSE
# ---------------------------------------------------------------------------

def map_usage(usage: Any) -> dict[str, Any] | None:
    """DeepInfra chat usage -> Responses usage (plus the cost the ledger keeps)."""
    if not isinstance(usage, dict):
        return None
    prompt = usage.get("prompt_tokens")
    completion = usage.get("completion_tokens")
    total = usage.get("total_tokens")
    if not all(isinstance(value, int) for value in (prompt, completion, total)):
        return None
    prompt_details = usage.get("prompt_tokens_details") or {}
    completion_details = usage.get("completion_tokens_details") or {}
    cached = prompt_details.get("cached_tokens") if isinstance(prompt_details, dict) else None
    reasoning = completion_details.get("reasoning_tokens") if isinstance(completion_details, dict) else None
    value = {
        "input_tokens": prompt,
        "input_tokens_details": {"cached_tokens": cached if isinstance(cached, int) else 0},
        "output_tokens": completion,
        "output_tokens_details": {"reasoning_tokens": reasoning if isinstance(reasoning, int) else 0},
        "total_tokens": total,
    }
    cost = usage.get("estimated_cost")
    if isinstance(cost, (int, float)):
        value["estimated_cost"] = float(cost)
    return value


# Chat-template end-of-turn tokens that a provider must consume but sometimes
# leaks into the text stream instead.  Observed in an aider smoke: DeepInfra's
# Qwen/Qwen3.6-35B-A3B ended a Builder turn with 20 completion tokens, all of
# them reasoning_content, no content, no tool_calls, finish_reason "stop", and
# the final reasoning delta was the literal "<|im_end|>" in the middle of a
# sentence ("...to understand the full scope.<|im_end|>").  codex saw a
# completed turn with no message and no tool call and ended the Builder.
END_OF_TURN_TOKENS = ("<|im_end|>", "<|endoftext|>", "<|eot_id|>", "<|end|>",
                      "<|im_start|>", "</s>")


def degenerate_reasoning_only(*, finish_reason, tool_calls, content, reasoning,
                              policy="end-token"):
    """Is this response a provider artefact that produced no usable turn?

    A response is degenerate when the model stopped without a tool call and
    without any assistant content, yet produced reasoning text.  Such a response
    cannot advance an agent turn: codex renders the reasoning and ends the turn.

    ``policy``:
      * ``off``        - never flag (the pre-115b behaviour)
      * ``end-token``  - flag only when the reasoning tail carries a raw
                         chat-template end token, i.e. the measured signature
      * ``any``        - flag every reasoning-only stop

    This never invents content: the caller fails the response so the client
    retries the identical request.
    """
    if policy == "off":
        return None
    if finish_reason not in (None, "stop"):
        return None
    if tool_calls or (content or "").strip() or not (reasoning or "").strip():
        return None
    if policy == "any":
        return "reasoning_only_stop"
    tail = reasoning[-64:]
    for token in END_OF_TURN_TOKENS:
        if token in tail:
            return "chat_template_end_token_in_reasoning:" + token
    return None


class ResponsesEmitter:
    """Build the Responses SSE event stream codex 0.144.1 consumes.

    Events are produced incrementally as chat chunks arrive.  Output items are
    numbered in arrival order: the reasoning item (if any) first, then the
    assistant message (if any text), then one item per tool call.
    """

    def __init__(self, response_id: str, model: str, created: int, *,
                 custom_tool_names=(), emit_reasoning: bool = True):
        self.response_id = response_id
        self.model = model
        self.created = created
        self.custom = set(custom_tool_names)
        self.emit_reasoning = emit_reasoning
        self.sequence = 0
        self.output: list[dict] = []
        self.index = 0
        self.reasoning_open = False
        self.reasoning_text = ""
        self.message_open = False
        self.message_text = ""
        self.message_index: int | None = None
        self.reasoning_index: int | None = None
        self.calls: dict[int, dict] = {}
        self.tool_calls_seen = 0
        self.usage: dict | None = None
        self.finished = False

    # -- low level ---------------------------------------------------------
    def _event(self, kind: str, payload: dict) -> bytes:
        payload = dict(payload, type=kind, sequence_number=self.sequence)
        self.sequence += 1
        return ("event: %s\ndata: %s\n\n" % (kind, json.dumps(payload, ensure_ascii=False))).encode("utf-8")

    def _envelope(self, status: str, extra: dict | None = None) -> dict:
        value = {"id": self.response_id, "object": "response", "created_at": self.created,
                 "status": status, "model": self.model, "output": list(self.output),
                 "parallel_tool_calls": True, "tool_choice": "auto", "tools": [],
                 "store": False, "error": None, "incomplete_details": None,
                 "instructions": None, "metadata": {}}
        if extra:
            value.update(extra)
        return value

    def start(self) -> bytes:
        return (self._event("response.created", {"response": self._envelope("in_progress")})
                + self._event("response.in_progress", {"response": self._envelope("in_progress")}))

    def keepalive(self) -> bytes:
        return b": keepalive\n\n"

    # -- reasoning ---------------------------------------------------------
    def reasoning_delta(self, text: str) -> bytes:
        if not self.emit_reasoning or not text:
            return b""
        out = b""
        if not self.reasoning_open:
            self.reasoning_open = True
            self.reasoning_index = self.index
            self.index += 1
            item = {"id": "rs_%s" % self.response_id, "type": "reasoning",
                    "summary": [], "content": []}
            out += self._event("response.output_item.added",
                               {"output_index": self.reasoning_index, "item": item})
            out += self._event("response.reasoning_summary_part.added",
                               {"item_id": item["id"], "output_index": self.reasoning_index,
                                "summary_index": 0, "part": {"type": "summary_text", "text": ""}})
        self.reasoning_text += text
        out += self._event("response.reasoning_summary_text.delta",
                           {"item_id": "rs_%s" % self.response_id,
                            "output_index": self.reasoning_index,
                            "summary_index": 0, "delta": text})
        return out

    def _close_reasoning(self) -> bytes:
        if not self.reasoning_open:
            return b""
        self.reasoning_open = False
        item_id = "rs_%s" % self.response_id
        item = {"id": item_id, "type": "reasoning",
                "summary": [{"type": "summary_text", "text": self.reasoning_text}], "content": []}
        out = self._event("response.reasoning_summary_text.done",
                          {"item_id": item_id, "output_index": self.reasoning_index,
                           "summary_index": 0, "text": self.reasoning_text})
        out += self._event("response.reasoning_summary_part.done",
                           {"item_id": item_id, "output_index": self.reasoning_index,
                            "summary_index": 0,
                            "part": {"type": "summary_text", "text": self.reasoning_text}})
        out += self._event("response.output_item.done",
                           {"output_index": self.reasoning_index, "item": item})
        self.output.append(item)
        return out

    # -- assistant text ----------------------------------------------------
    def text_delta(self, text: str) -> bytes:
        if not text:
            return b""
        out = self._close_reasoning()
        if not self.message_open:
            self.message_open = True
            self.message_index = self.index
            self.index += 1
            item_id = "msg_%s" % self.response_id
            out += self._event("response.output_item.added",
                               {"output_index": self.message_index,
                                "item": {"id": item_id, "type": "message", "role": "assistant",
                                         "status": "in_progress", "content": []}})
            out += self._event("response.content_part.added",
                               {"item_id": item_id, "output_index": self.message_index,
                                "content_index": 0,
                                "part": {"type": "output_text", "text": "", "annotations": []}})
        self.message_text += text
        out += self._event("response.output_text.delta",
                           {"item_id": "msg_%s" % self.response_id,
                            "output_index": self.message_index, "content_index": 0,
                            "delta": text})
        return out

    def _close_message(self) -> bytes:
        if not self.message_open:
            return b""
        self.message_open = False
        item_id = "msg_%s" % self.response_id
        item = {"id": item_id, "type": "message", "role": "assistant", "status": "completed",
                "content": [{"type": "output_text", "text": self.message_text, "annotations": []}]}
        out = self._event("response.output_text.done",
                          {"item_id": item_id, "output_index": self.message_index,
                           "content_index": 0, "text": self.message_text})
        out += self._event("response.content_part.done",
                           {"item_id": item_id, "output_index": self.message_index,
                            "content_index": 0,
                            "part": {"type": "output_text", "text": self.message_text,
                                     "annotations": []}})
        out += self._event("response.output_item.done",
                           {"output_index": self.message_index, "item": item})
        self.output.append(item)
        return out

    # -- tool calls --------------------------------------------------------
    def tool_call_delta(self, index: int, call_id: str | None, name: str | None,
                        arguments: str | None) -> bytes:
        out = b""
        call = self.calls.get(index)
        if call is None:
            out += self._close_reasoning()
            out += self._close_message()
            call = {"output_index": self.index, "call_id": "call_%d" % index,
                    "name": "", "arguments": "", "added": False}
            self.calls[index] = call
            self.tool_calls_seen += 1
            self.index += 1
        if call_id:
            call["call_id"] = call_id
        if name:
            call["name"] += name
        if not call["added"] and call["name"]:
            call["added"] = True
            custom = call["name"] in self.custom
            item = ({"id": "ctc_%s_%d" % (self.response_id, index), "type": "custom_tool_call",
                     "call_id": call["call_id"], "name": call["name"], "input": "",
                     "status": "in_progress"} if custom else
                    {"id": "fc_%s_%d" % (self.response_id, index), "type": "function_call",
                     "call_id": call["call_id"], "name": call["name"], "arguments": "",
                     "status": "in_progress"})
            call["item_id"] = item["id"]
            call["custom"] = custom
            out += self._event("response.output_item.added",
                               {"output_index": call["output_index"], "item": item})
        if arguments:
            call["arguments"] += arguments
            if call.get("added") and not call.get("custom"):
                out += self._event("response.function_call_arguments.delta",
                                   {"item_id": call["item_id"],
                                    "output_index": call["output_index"], "delta": arguments})
        return out

    def _close_calls(self) -> bytes:
        out = b""
        for index in sorted(self.calls):
            call = self.calls[index]
            if not call.get("added"):
                # A tool call whose name never arrived cannot be executed.
                continue
            arguments = call["arguments"] or "{}"
            if call["custom"]:
                raw = arguments
                try:
                    decoded = json.loads(arguments)
                    if isinstance(decoded, dict) and isinstance(decoded.get(CUSTOM_TOOL_INPUT), str):
                        raw = decoded[CUSTOM_TOOL_INPUT]
                except ValueError:
                    pass
                item = {"id": call["item_id"], "type": "custom_tool_call",
                        "call_id": call["call_id"], "name": call["name"], "input": raw,
                        "status": "completed"}
                out += self._event("response.custom_tool_call_input.delta",
                                   {"item_id": call["item_id"],
                                    "output_index": call["output_index"], "delta": raw})
                out += self._event("response.custom_tool_call_input.done",
                                   {"item_id": call["item_id"],
                                    "output_index": call["output_index"], "input": raw})
            else:
                item = {"id": call["item_id"], "type": "function_call",
                        "call_id": call["call_id"], "name": call["name"],
                        "arguments": arguments, "status": "completed"}
                out += self._event("response.function_call_arguments.done",
                                   {"item_id": call["item_id"],
                                    "output_index": call["output_index"], "arguments": arguments})
            out += self._event("response.output_item.done",
                               {"output_index": call["output_index"], "item": item})
            self.output.append(item)
        self.calls = {}
        return out

    # -- terminal ----------------------------------------------------------
    def complete(self, usage: dict | None, *, incomplete_reason: str | None = None) -> bytes:
        out = self._close_reasoning() + self._close_message() + self._close_calls()
        self.usage = usage
        response = self._envelope("completed", {"usage": usage} if usage else {})
        if incomplete_reason:
            response = self._envelope("incomplete", {
                "usage": usage, "incomplete_details": {"reason": incomplete_reason}})
            out += self._event("response.incomplete", {"response": response})
        else:
            out += self._event("response.completed", {"response": response})
        self.finished = True
        return out + b"data: [DONE]\n\n"

    def failed(self, code: str, message: str, *, status: int | None = None) -> bytes:
        error = {"code": code, "message": message}
        if status is not None:
            error["upstream_status"] = status
        out = self._event("error", dict(error, param=None))
        out += self._event("response.failed",
                           {"response": self._envelope("failed", {"error": error})})
        self.finished = True
        return out
