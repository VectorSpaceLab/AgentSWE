from __future__ import annotations

import json
import os
import re
import urllib.error
import urllib.request
from typing import Any

MODEL_URL = os.environ.get("HARNESS_RESPONSES_URL", "")
SEARCH_URL = os.environ.get("HARNESS_SEARCH_URL", "")
VISIT_URL = os.environ.get("HARNESS_VISIT_URL", "")
API_KEY = os.environ.get("HARNESS_API_KEY", "")
MAX_STEPS = 8

SYSTEM = """You are a careful BrowseComp research worker. Use search and visit tools when evidence is needed. Work in a ReAct loop: inspect the question, search for high-value clues, visit authoritative pages, reconcile conflicting evidence, then answer. Never invent sources. Return exactly:\nExplanation: ...\nExact Answer: ...\nConfidence: N%."""


def _post(url: str, payload: dict[str, Any]) -> dict[str, Any]:
    request = urllib.request.Request(url, data=json.dumps(payload).encode(), headers={"Content-Type": "application/json", "Authorization": f"Bearer {API_KEY}"}, method="POST")
    with urllib.request.urlopen(request, timeout=120) as response:
        return json.loads(response.read().decode())


def _search(query: str) -> str:
    if not SEARCH_URL:
        return "Search unavailable."
    return json.dumps(_post(SEARCH_URL, {"query": query, "page": 1, "search_type": "search"}), ensure_ascii=False)[:12000]


def _visit(url: str) -> str:
    if not VISIT_URL:
        return "Visit unavailable."
    return json.dumps(_post(VISIT_URL, {"url": url}), ensure_ascii=False)[:16000]


def _model(messages: list[dict[str, Any]]) -> tuple[str, dict[str, int]]:
    if not MODEL_URL:
        raise RuntimeError("model endpoint unavailable")
    body = _post(MODEL_URL, {"model": "gpt-5.6-sol", "reasoning": {"effort": "medium"}, "input": messages})
    text = str(body.get("output_text", ""))
    if not text:
        for output in body.get("output", []) or []:
            for content in output.get("content", []) or []:
                if isinstance(content, dict) and isinstance(content.get("text"), str):
                    text += content["text"]
    usage = body.get("usage") if isinstance(body.get("usage"), dict) else {}
    return text, {"total_tokens": int(usage.get("total_tokens", 0) or usage.get("input_tokens", 0) + usage.get("output_tokens", 0) or 0)}


def _tool_request(text: str) -> tuple[str, str] | None:
    match = re.search(r"(?:SEARCH|search)\s*:\s*(.+)", text)
    if match:
        return "search", match.group(1).strip()
    match = re.search(r"(?:VISIT|visit)\s*:\s*(https?://\S+)", text)
    if match:
        return "visit", match.group(1).rstrip(".,)")
    return None


def _format_final(text: str) -> str:
    answer = re.search(r"(?:Exact Answer|Final Answer|Answer)\s*:\s*(.+)", text, re.I)
    explanation = re.search(r"Explanation\s*:\s*(.+?)(?:\n|$)", text, re.I)
    confidence = re.search(r"Confidence\s*:\s*(\d{1,3})", text, re.I)
    return f"Explanation: {(explanation.group(1).strip() if explanation else 'Answer synthesized from gathered evidence.')[:1000]}\nExact Answer: {(answer.group(1).strip() if answer else text.strip().splitlines()[-1] if text.strip() else 'No answer found')[:500]}\nConfidence: {max(0, min(100, int(confidence.group(1)))) if confidence else 50}%"


def answer(item: dict[str, Any]) -> tuple[str, dict[str, int]]:
    question = str(item.get("question") or item.get("instruction") or "")
    messages: list[dict[str, Any]] = [{"role": "system", "content": SYSTEM}, {"role": "user", "content": question}]
    usage = {"model_calls": 0, "search_calls": 0, "visit_calls": 0}
    evidence = ""
    for _ in range(MAX_STEPS):
        text, tokens = _model(messages + ([{"role": "user", "content": "Evidence gathered:\n" + evidence}] if evidence else []))
        usage["model_calls"] += 1
        usage["total_tokens"] = usage.get("total_tokens", 0) + tokens.get("total_tokens", 0)
        tool = _tool_request(text)
        if tool is None or re.search(r"(?:Exact Answer|Final Answer)\s*:", text, re.I):
            return _format_final(text), usage
        kind, value = tool
        result = _search(value) if kind == "search" else _visit(value)
        usage[f"{kind}_calls"] += 1
        evidence += f"\n[{kind} {value}]\n{result}\n"
    text, tokens = _model(messages + [{"role": "user", "content": "Synthesize the final answer from this evidence:\n" + evidence}])
    usage["model_calls"] += 1
    return _format_final(text), usage
