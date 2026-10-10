from __future__ import annotations

from collections.abc import Iterable

from .cursor import Cursor


def build_cursors(
    sources: Iterable[Iterable[dict[str, object]]],
    maximum_sources: int = 256,
) -> list[Cursor]:
    cursors: list[Cursor] = []
    for index, source in enumerate(sources):
        if index >= maximum_sources:
            raise ValueError("too many event sources")
        cursors.append(Cursor(index, source))
    return cursors


def active_count(cursors: list[Cursor]) -> int:
    return sum(cursor.current is not None for cursor in cursors)


def cursor_positions(cursors: list[Cursor]) -> list[dict[str, object]]:
    return [
        {
            "source_index": cursor.source_index,
            "offset": cursor.offset,
            "active": cursor.current is not None,
            "timestamp_ms": None if cursor.current is None else cursor.current.timestamp_ms,
        }
        for cursor in cursors
    ]
