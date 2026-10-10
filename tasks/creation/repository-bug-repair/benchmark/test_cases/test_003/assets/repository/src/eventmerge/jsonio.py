from __future__ import annotations

import json
from collections.abc import Iterable


def read_json_lines(lines: Iterable[str]) -> list[dict[str, object]]:
    events = []
    for line_number, line in enumerate(lines, 1):
        if not line.strip():
            continue
        try:
            value = json.loads(line)
        except json.JSONDecodeError as exc:
            raise ValueError(f"invalid JSON at line {line_number}") from exc
        if not isinstance(value, dict):
            raise ValueError(f"line {line_number} is not an object")
        events.append(value)
    return events


def write_json_lines(events: list[dict[str, object]]) -> str:
    return "".join(
        json.dumps(event, sort_keys=True, separators=(",", ":")) + "\n"
        for event in events
    )
