"""Capture usage from the actual Create Code-judge HTTP response.

The authoritative Create ``code_eval.py`` intentionally keeps only the text
of a judge response when it parses Responses SSE.  This evaluator-only
``sitecustomize`` observes the same response object inside the short-lived
Code-judge container and records the provider-reported usage separately.  It
does not alter the request, response text, scoring logic, or authoritative
Create source, and it never records credentials.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any


CAPTURE = Path(os.environ.get("CODE_JUDGE_USAGE_CAPTURE", "/output/provider_usage_capture.json"))


def _usage(value: Any) -> dict[str, int] | None:
    if not isinstance(value, dict):
        return None
    candidate = value.get("usage")
    if not isinstance(candidate, dict) and isinstance(value.get("response"), dict):
        candidate = value["response"].get("usage")
    if not isinstance(candidate, dict):
        return None
    result: dict[str, int] = {}
    for name in ("input_tokens", "output_tokens", "total_tokens"):
        raw = candidate.get(name)
        if isinstance(raw, (int, float)) and not isinstance(raw, bool) and raw >= 0:
            result[name] = int(raw)
    return result if result else None


def _record(value: Any) -> None:
    usage = _usage(value)
    if usage is None:
        return
    CAPTURE.parent.mkdir(parents=True, exist_ok=True)
    current: dict[str, Any] = {}
    try:
        parsed = json.loads(CAPTURE.read_text(encoding="utf-8"))
        if isinstance(parsed, dict):
            current = parsed
    except (OSError, json.JSONDecodeError):
        pass
    current["input_tokens"] = usage.get("input_tokens", current.get("input_tokens", 0))
    current["output_tokens"] = usage.get("output_tokens", current.get("output_tokens", 0))
    current["total_tokens"] = usage.get("total_tokens", current.get("total_tokens", 0))
    current["observations"] = int(current.get("observations", 0) or 0) + 1
    CAPTURE.write_text(json.dumps(current, sort_keys=True) + "\n", encoding="utf-8")


try:
    import requests

    _post = requests.post

    def _wrapped_post(*args: Any, **kwargs: Any) -> Any:
        response = _post(*args, **kwargs)
        original_json = response.json

        def wrapped_json(*json_args: Any, **json_kwargs: Any) -> Any:
            value = original_json(*json_args, **json_kwargs)
            _record(value)
            return value

        response.json = wrapped_json
        original_iter_lines = response.iter_lines

        def wrapped_iter_lines(*line_args: Any, **line_kwargs: Any):
            for line in original_iter_lines(*line_args, **line_kwargs):
                raw = line.decode("utf-8", "replace") if isinstance(line, bytes) else str(line)
                if raw.strip().startswith("data:"):
                    payload = raw.split(":", 1)[1].strip()
                    if payload and payload != "[DONE]":
                        try:
                            _record(json.loads(payload))
                        except json.JSONDecodeError:
                            pass
                yield line

        response.iter_lines = wrapped_iter_lines
        return response

    requests.post = _wrapped_post
except Exception:
    # The judge will still fail closed if no actual usage is captured.
    pass
