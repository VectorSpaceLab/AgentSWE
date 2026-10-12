#!/usr/bin/env python3
"""Credential-free Chat-Completions facade over evaluator Responses transport.

DeepTutor owns planning, function selection, tool dispatch, state mutation, and
the final answer. This adapter only translates the wire contract expected by
DeepTutor's existing agent loop.
"""

from __future__ import annotations

import asyncio
import json
import http.client
import os
import socket
from types import SimpleNamespace
from typing import Any
import urllib.request

# This file is the only transport implementation mounted into the lower
# sandbox. It must not import evaluator protocol/case modules.
LOWER_MODEL = "deepseek-flash"
LOWER_EFFORT = "high"
PLACEHOLDER_TOKEN = "broker-only-placeholder"

class UnixHTTPConnection(http.client.HTTPConnection):
    def __init__(self, path: str, timeout: int = 240):
        super().__init__("lower-broker.invalid", timeout=timeout)
        self.unix_path = path

    def connect(self):
        self.sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.sock.settimeout(self.timeout)
        self.sock.connect(self.unix_path)

def transport_health():
    path = os.environ.get("AGENTSWE_BROKER_UDS", "")
    if not path: raise RuntimeError("evaluator UDS broker transport is required")
    connection = UnixHTTPConnection(path, timeout=10)
    try:
        connection.request("GET", "/agentswe/transport-health")
        response = connection.getresponse(); value = json.loads(response.read())
        if response.status != 200 or value.get("ok") is not True: raise RuntimeError("UDS relay health failed")
        return value
    finally: connection.close()


def _content_text(content: object) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts: list[str] = []
        for item in content:
            if isinstance(item, dict) and isinstance(item.get("text"), str):
                parts.append(item["text"])
        return "\n".join(parts)
    return str(content or "")


def chat_messages_to_responses(messages: object) -> list[dict[str, Any]]:
    if not isinstance(messages, list):
        raise ValueError("messages must be an array")
    result: list[dict[str, Any]] = []
    for message in messages:
        if not isinstance(message, dict):
            raise ValueError("message must be an object")
        role = str(message.get("role") or "")
        content = _content_text(message.get("content"))
        if role in {"system", "developer", "user"}:
            if content:
                result.append(
                    {
                        "type": "message",
                        "role": role,
                        "content": [{"type": "input_text", "text": content}],
                    }
                )
        elif role == "assistant":
            if content:
                result.append(
                    {
                        "type": "message",
                        "role": "assistant",
                        "content": [{"type": "output_text", "text": content}],
                    }
                )
            for call in message.get("tool_calls") or []:
                function = call.get("function") if isinstance(call, dict) else None
                if not isinstance(function, dict):
                    continue
                result.append(
                    {
                        "type": "function_call",
                        "call_id": str(call.get("id") or ""),
                        "name": str(function.get("name") or ""),
                        "arguments": str(function.get("arguments") or "{}"),
                    }
                )
        elif role == "tool":
            result.append(
                {
                    "type": "function_call_output",
                    "call_id": str(message.get("tool_call_id") or ""),
                    "output": content,
                }
            )
        else:
            raise ValueError(f"unsupported chat role: {role}")
    return result


def chat_tools_to_responses(tools: object) -> list[dict[str, Any]]:
    if not isinstance(tools, list):
        return []
    result: list[dict[str, Any]] = []
    for item in tools:
        function = item.get("function") if isinstance(item, dict) else None
        if not isinstance(function, dict) or not function.get("name"):
            continue
        result.append(
            {
                "type": "function",
                "name": str(function["name"]),
                "description": str(function.get("description") or ""),
                "parameters": function.get("parameters") or {"type": "object", "properties": {}},
                "strict": bool(function.get("strict", False)),
            }
        )
    return result


def parse_responses_output(value: dict[str, Any]) -> tuple[str, list[dict[str, str]]]:
    text_parts: list[str] = []
    calls: list[dict[str, str]] = []
    if isinstance(value.get("output_text"), str):
        text_parts.append(value["output_text"])
    for item in value.get("output") or []:
        if not isinstance(item, dict):
            continue
        if item.get("type") == "message":
            for part in item.get("content") or []:
                if isinstance(part, dict) and part.get("type") in {"output_text", "text"}:
                    if isinstance(part.get("text"), str):
                        text_parts.append(part["text"])
        elif item.get("type") == "function_call":
            calls.append(
                {
                    "id": str(item.get("call_id") or item.get("id") or ""),
                    "name": str(item.get("name") or ""),
                    "arguments": str(item.get("arguments") or "{}"),
                }
            )
    return "".join(text_parts), calls


def _chunk(
    *,
    content: str | None = None,
    tool_call: dict[str, str] | None = None,
    index: int = 0,
    finish_reason: str | None = None,
    usage: Any = None,
) -> Any:
    tool_calls = None
    if tool_call is not None:
        tool_calls = [
            SimpleNamespace(
                index=index,
                id=tool_call["id"],
                type="function",
                function=SimpleNamespace(
                    name=tool_call["name"], arguments=tool_call["arguments"]
                ),
            )
        ]
    return SimpleNamespace(
        choices=[
            SimpleNamespace(
                delta=SimpleNamespace(
                    content=content,
                    tool_calls=tool_calls,
                    reasoning_content=None,
                    reasoning=None,
                ),
                finish_reason=finish_reason,
            )
        ],
        usage=usage,
    )


class StaticResponseStream:
    def __init__(self, chunks: list[Any]) -> None:
        self._chunks = chunks
        self._index = 0

    def __aiter__(self) -> "StaticResponseStream":
        return self

    async def __anext__(self) -> Any:
        if self._index >= len(self._chunks):
            raise StopAsyncIteration
        value = self._chunks[self._index]
        self._index += 1
        return value

    async def close(self) -> None:
        return None


class ResponsesChatAdapter:
    """Object compatible with ``client.chat.completions.create``."""

    def __init__(self, endpoint: str, token: str = PLACEHOLDER_TOKEN, timeout: int = 240) -> None:
        self.endpoint = endpoint
        self.token = token
        self.timeout = timeout
        self.chat = SimpleNamespace(completions=SimpleNamespace(create=self.create))

    def _post(self, body: dict[str, Any]) -> dict[str, Any]:
        uds = os.environ.get("AGENTSWE_BROKER_UDS", "")
        if uds:
            connection = UnixHTTPConnection(uds, timeout=self.timeout)
            try:
                connection.request("POST", "/v1/responses", json.dumps(body).encode(),
                    {"Authorization": f"Bearer {self.token}", "Content-Type": "application/json"})
                response = connection.getresponse(); raw = response.read()
                if response.status != 200: raise RuntimeError(f"evaluator_transport_http_{response.status}: {raw[:300]!r}")
                value = json.loads(raw)
                if not isinstance(value, dict): raise RuntimeError("broker response must be object")
                return value
            finally: connection.close()
        if os.environ.get("AGENTSWE_RESPONSES_ADAPTER") == "1":
            raise RuntimeError("evaluator transport is missing its required isolated UDS relay")
        request = urllib.request.Request(
            self.endpoint,
            data=json.dumps(body, ensure_ascii=False).encode("utf-8"),
            method="POST",
            headers={
                "Authorization": f"Bearer {self.token}",
                "Content-Type": "application/json",
                "Accept": "application/json",
            },
        )
        with urllib.request.urlopen(request, timeout=self.timeout) as response:
            value = json.loads(response.read())
        if not isinstance(value, dict):
            raise RuntimeError("broker response must be a JSON object")
        if isinstance(value.get("error"), dict):
            raise RuntimeError(str(value["error"].get("type") or "broker_error"))
        return value

    async def create(self, **kwargs: Any) -> StaticResponseStream:
        body: dict[str, Any] = {
            "model": LOWER_MODEL,
            "input": chat_messages_to_responses(kwargs.get("messages")),
            "reasoning": {"effort": LOWER_EFFORT},
            "stream": False,
        }
        tools = chat_tools_to_responses(kwargs.get("tools"))
        if tools:
            body["tools"] = tools
            body["tool_choice"] = "auto"
        max_tokens = kwargs.get("max_completion_tokens", kwargs.get("max_tokens"))
        if isinstance(max_tokens, int) and max_tokens > 0:
            body["max_output_tokens"] = max_tokens
        response = await asyncio.to_thread(self._post, body)
        text, calls = parse_responses_output(response)
        chunks: list[Any] = []
        if text:
            chunks.append(_chunk(content=text))
        for index, call in enumerate(calls):
            chunks.append(_chunk(tool_call=call, index=index))
        usage_value = response.get("usage") if isinstance(response.get("usage"), dict) else {}
        usage = SimpleNamespace(
            prompt_tokens=int(usage_value.get("input_tokens", 0) or 0),
            completion_tokens=int(usage_value.get("output_tokens", 0) or 0),
            total_tokens=int(usage_value.get("total_tokens", 0) or 0),
        )
        chunks.append(
            _chunk(
                finish_reason="tool_calls" if calls else "stop",
                usage=usage,
            )
        )
        return StaticResponseStream(chunks)


async def responses_text_stream(
    *,
    endpoint: str,
    token: str = PLACEHOLDER_TOKEN,
    prompt: str = "",
    system_prompt: str | None = None,
    messages: object = None,
    max_tokens: int | None = None,
    **_: Any,
):
    """Yield plain text through the same locked Responses transport.

    DeepTutor's main agentic loop uses ``build_openai_client`` while a few
    product-owned auxiliary features, including post-turn session titles, use
    the services-layer ``stream`` function.  Both are legitimate product LLM
    calls and must traverse the evaluator-owned Responses broker.  This small
    adapter preserves the services-layer async-generator contract without
    adding a Chat Completions endpoint or hiding broker failures.
    """
    request_messages = messages if isinstance(messages, list) else None
    if request_messages is None:
        request_messages = []
        if system_prompt:
            request_messages.append({"role": "system", "content": system_prompt})
        if prompt:
            request_messages.append({"role": "user", "content": prompt})
    client = ResponsesChatAdapter(endpoint=endpoint, token=token)
    stream = await client.create(messages=request_messages, max_tokens=max_tokens)
    async for chunk in stream:
        choices = getattr(chunk, "choices", None) or []
        if not choices:
            continue
        delta = getattr(choices[0], "delta", None)
        content = getattr(delta, "content", None) if delta is not None else None
        if isinstance(content, str) and content:
            yield content


__all__ = [
    "ResponsesChatAdapter",
    "responses_text_stream",
    "chat_messages_to_responses",
    "chat_tools_to_responses",
    "parse_responses_output",
]
