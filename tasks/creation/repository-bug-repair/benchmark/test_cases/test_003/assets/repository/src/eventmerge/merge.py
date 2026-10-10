from __future__ import annotations

from collections.abc import Iterable

from .cursor import Cursor
from .identity import first_occurrence
from .planner import pick_next


def merge_events(
    sources: Iterable[Iterable[dict[str, object]]],
) -> list[dict[str, object]]:
    cursors = [Cursor(index, source) for index, source in enumerate(sources)]
    ordered = []
    while True:
        cursor = pick_next(cursors)
        if cursor is None:
            break
        assert cursor.current is not None
        ordered.append(cursor.current)
        cursor.advance()
    return [event.as_dict() for event in first_occurrence(ordered)]
