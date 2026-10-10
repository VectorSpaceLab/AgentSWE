#!/usr/bin/env python3
"""Strict declarative agent and action contract for OSWorld episodes."""

from __future__ import annotations

import base64
import hashlib
import json
from dataclasses import dataclass
from typing import Any


AGENT_KIND = "osworld_screenshot_react"
AGENT_FIELDS = {
    "kind",
    "instructions",
    "planning_guidance",
    "recovery_guidance",
    "verification_guidance",
}
TEXT_FIELDS = AGENT_FIELDS - {"kind"}
MAX_FIELD_CHARS = 8_000
MAX_SPEC_CHARS = 20_000
MAX_TYPE_CHARS = 4_000
ALLOWED_KEYS = {
    "enter", "tab", "esc", "escape", "backspace", "delete", "space",
    "home", "end", "pageup", "pagedown", "up", "down", "left", "right",
    "ctrl", "alt", "shift", "win", "command", "f1", "f2", "f3", "f4",
    "f5", "f6", "f7", "f8", "f9", "f10", "f11", "f12",
} | set("abcdefghijklmnopqrstuvwxyz0123456789")
ACTION_KINDS = {
    "click", "double_click", "type", "key", "hotkey", "scroll", "wait", "done"
}
MODEL_ACTION_FIELDS = {
    "kind", "x", "y", "button", "text", "key", "keys", "amount", "seconds", "rationale"
}


class AgentSpecError(ValueError):
    pass


class ActionError(ValueError):
    pass


def _plain_string(value: Any, name: str, *, max_chars: int, allow_empty: bool = False) -> str:
    if not isinstance(value, str):
        raise ValueError(f"{name} must be text")
    value = value.strip()
    if not value and not allow_empty:
        raise ValueError(f"{name} must not be empty")
    if len(value) > max_chars:
        raise ValueError(f"{name} exceeds {max_chars} characters")
    if "\x00" in value:
        raise ValueError(f"{name} contains NUL")
    return value


def validate_agent_spec(row: Any, case_id: str) -> dict[str, Any]:
    if not isinstance(row, dict) or set(row) != {"id", "agent"}:
        raise AgentSpecError("prediction must contain exactly id and agent")
    if row.get("id") != case_id:
        raise AgentSpecError("prediction id mismatch")
    agent = row.get("agent")
    if not isinstance(agent, dict):
        raise AgentSpecError("agent must be an object")
    if set(agent) - AGENT_FIELDS:
        raise AgentSpecError("unsupported agent fields")
    if agent.get("kind") != AGENT_KIND:
        raise AgentSpecError(f"agent kind must be {AGENT_KIND}")
    normalized: dict[str, str] = {"kind": AGENT_KIND}
    for field in sorted(TEXT_FIELDS):
        try:
            normalized[field] = _plain_string(
                agent.get(field, ""), field, max_chars=MAX_FIELD_CHARS,
                allow_empty=field != "instructions",
            )
        except ValueError as exc:
            raise AgentSpecError(str(exc)) from exc
    if len(json.dumps(normalized, ensure_ascii=False)) > MAX_SPEC_CHARS:
        raise AgentSpecError("agent spec exceeds total character limit")
    return {"id": case_id, "agent": normalized}


def load_prediction(path: str, case_id: str) -> dict[str, Any]:
    rows = [json.loads(line) for line in open(path, encoding="utf-8") if line.strip()]
    if len(rows) != 1:
        raise AgentSpecError("prediction must contain exactly one JSONL row")
    return validate_agent_spec(rows[0], case_id)


def action_schema(width: int, height: int) -> dict[str, Any]:
    if width < 1 or height < 1:
        raise ValueError("invalid screen dimensions")
    # The provider gateway's strict-schema mode accepts a single closed nullable object but rejects
    # the equivalent oneOf union. Runtime validation below enforces kind-specific fields.
    action = {
        "type": "object",
        "additionalProperties": False,
        "required": sorted(MODEL_ACTION_FIELDS),
        "properties": {
            "kind": {"type": "string", "enum": sorted(ACTION_KINDS)},
            "x": {"type": ["integer", "null"], "minimum": 0, "maximum": width - 1},
            "y": {"type": ["integer", "null"], "minimum": 0, "maximum": height - 1},
            "button": {"type": ["string", "null"], "enum": ["left", "right", "middle", None]},
            "text": {"type": ["string", "null"], "maxLength": MAX_TYPE_CHARS},
            "key": {"type": ["string", "null"], "enum": sorted(ALLOWED_KEYS) + [None]},
            "keys": {
                "type": ["array", "null"], "minItems": 2, "maxItems": 4,
                "items": {"type": "string", "enum": sorted(ALLOWED_KEYS)},
            },
            "amount": {"type": ["integer", "null"], "minimum": -12, "maximum": 12},
            "seconds": {"type": ["integer", "null"], "minimum": 1, "maximum": 10},
            "rationale": {"type": "string", "maxLength": 1000},
        },
    }
    return {
        "type": "object", "additionalProperties": False,
        "required": ["action"],
        "properties": {"action": action},
    }


def _exact_keys(value: dict[str, Any], required: set[str]) -> None:
    if set(value) != required:
        raise ActionError(f"action fields must be exactly {sorted(required)}")


def validate_action(value: Any, width: int, height: int) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise ActionError("response must be an object")
    _exact_keys(value, {"action"})
    action = value["action"]
    if not isinstance(action, dict):
        raise ActionError("action must be an object")
    if set(action) == MODEL_ACTION_FIELDS:
        kind = action.get("kind")
        used = {
            "click": {"x", "y", "button"},
            "double_click": {"x", "y", "button"},
            "type": {"text"},
            "key": {"key"},
            "hotkey": {"keys"},
            "scroll": {"amount"},
            "wait": {"seconds"},
            "done": set(),
        }.get(kind)
        if used is None:
            raise ActionError("unsupported action kind")
        optional = MODEL_ACTION_FIELDS - {"kind", "rationale"}
        if any(action[field] is not None for field in optional - used):
            raise ActionError("unused model action fields must be null")
        action = {"kind": kind, "rationale": action.get("rationale"), **{field: action[field] for field in used}}
    kind = action.get("kind")
    if kind not in ACTION_KINDS:
        raise ActionError("unsupported action kind")
    rationale = action.get("rationale")
    if not isinstance(rationale, str) or len(rationale) > 1000:
        raise ActionError("invalid rationale")
    if kind in {"click", "double_click"}:
        _exact_keys(action, {"kind", "x", "y", "button", "rationale"})
        x, y = action["x"], action["y"]
        if isinstance(x, bool) or not isinstance(x, int) or not 0 <= x < width:
            raise ActionError("x coordinate out of bounds")
        if isinstance(y, bool) or not isinstance(y, int) or not 0 <= y < height:
            raise ActionError("y coordinate out of bounds")
        if action["button"] not in {"left", "right", "middle"}:
            raise ActionError("invalid mouse button")
    elif kind == "type":
        _exact_keys(action, {"kind", "text", "rationale"})
        if not isinstance(action["text"], str) or len(action["text"]) > MAX_TYPE_CHARS:
            raise ActionError("invalid text")
        if "\x00" in action["text"]:
            raise ActionError("text contains NUL")
    elif kind == "key":
        _exact_keys(action, {"kind", "key", "rationale"})
        if action["key"] not in ALLOWED_KEYS:
            raise ActionError("invalid key")
    elif kind == "hotkey":
        _exact_keys(action, {"kind", "keys", "rationale"})
        keys = action["keys"]
        if not isinstance(keys, list) or not 2 <= len(keys) <= 4:
            raise ActionError("hotkey must contain 2-4 keys")
        if len(set(keys)) != len(keys) or any(key not in ALLOWED_KEYS for key in keys):
            raise ActionError("invalid hotkey")
    elif kind == "scroll":
        _exact_keys(action, {"kind", "amount", "rationale"})
        amount = action["amount"]
        if isinstance(amount, bool) or not isinstance(amount, int) or amount == 0 or not -12 <= amount <= 12:
            raise ActionError("invalid scroll amount")
    elif kind == "wait":
        _exact_keys(action, {"kind", "seconds", "rationale"})
        seconds = action["seconds"]
        if isinstance(seconds, bool) or not isinstance(seconds, int) or not 1 <= seconds <= 10:
            raise ActionError("invalid wait duration")
    else:
        _exact_keys(action, {"kind", "rationale"})
    return action


def action_to_pyautogui(action: dict[str, Any]) -> str:
    """Convert only a previously validated action to fixed pyautogui source."""
    kind = action["kind"]
    if kind == "click":
        return f"pyautogui.click({action['x']}, {action['y']}, button={action['button']!r})"
    if kind == "double_click":
        return f"pyautogui.doubleClick({action['x']}, {action['y']}, interval=0.15, button={action['button']!r})"
    if kind == "type":
        payload = base64.b64encode(action["text"].encode("utf-8")).decode("ascii")
        return (
            "import base64, pyperclip; "
            f"pyperclip.copy(base64.b64decode({payload!r}).decode('utf-8')); "
            "pyautogui.hotkey('ctrl', 'v')"
        )
    if kind == "key":
        return f"pyautogui.press({action['key']!r})"
    if kind == "hotkey":
        return f"pyautogui.hotkey({', '.join(repr(key) for key in action['keys'])})"
    if kind == "scroll":
        return f"pyautogui.scroll({action['amount']})"
    if kind == "wait":
        return f"import time; time.sleep({action['seconds']})"
    if kind == "done":
        return "DONE"
    raise ActionError("unsupported action kind")


def image_digest(image: bytes) -> str:
    return hashlib.sha256(image).hexdigest()


def build_model_input(
    instruction: str,
    agent: dict[str, str],
    screenshot_png: bytes,
    history: list[dict[str, Any]],
    width: int,
    height: int,
) -> list[dict[str, Any]]:
    policy = "\n".join(
        f"{field}: {agent.get(field, '')}" for field in sorted(TEXT_FIELDS)
    )
    recent = [
        {"step": row["step"], "action": row["action"], "result": row.get("result", "ok")}
        for row in history[-8:]
    ]
    prompt = (
        "You control an Ubuntu desktop only through the allowed single-action schema. "
        "Inspect the current screenshot and return exactly one action. Set every field that "
        "does not apply to the selected action kind to null. Do not assume an "
        "action succeeded unless the screenshot confirms it. Use done only after visual "
        "verification; done never determines the official reward.\n\n"
        f"Task: {instruction}\nScreen: {width}x{height}\n{policy}\n"
        f"Recent trusted history: {json.dumps(recent, ensure_ascii=False)}"
    )
    encoded = base64.b64encode(screenshot_png).decode("ascii")
    return [{
        "role": "user",
        "content": [
            {"type": "input_text", "text": prompt},
            {"type": "input_image", "image_url": f"data:image/png;base64,{encoded}"},
        ],
    }]


@dataclass(frozen=True)
class EpisodeLimits:
    max_actions: int = 30
    max_wall_seconds: int = 900
    max_model_tokens: int = 100_000
