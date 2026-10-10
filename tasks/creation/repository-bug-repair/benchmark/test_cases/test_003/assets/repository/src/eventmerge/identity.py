from __future__ import annotations

from .models import Event


def identity(event: Event) -> str:
    return event.event_id.casefold()


def first_occurrence(events: list[Event]) -> list[Event]:
    result: list[Event] = []
    keys: list[str] = []
    for event in events:
        key = identity(event)
        if key not in keys:
            keys.append(key)
            result.append(event)
    return result
