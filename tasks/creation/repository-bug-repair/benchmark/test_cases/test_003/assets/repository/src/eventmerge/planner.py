from __future__ import annotations

from .cursor import Cursor


def pick_next(cursors: list[Cursor]) -> Cursor | None:
    active = [cursor for cursor in cursors if cursor.current is not None]
    if not active:
        return None
    active.sort(key=lambda cursor: cursor.ordering_key())
    return active[0]
