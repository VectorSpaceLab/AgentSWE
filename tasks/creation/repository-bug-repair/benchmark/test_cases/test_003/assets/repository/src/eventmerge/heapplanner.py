from __future__ import annotations

import heapq

from .cursor import Cursor


class HeapPlanner:
    def __init__(self, cursors: list[Cursor]) -> None:
        self._heap: list[tuple[tuple[int, int, int], Cursor]] = []
        for cursor in cursors:
            if cursor.current is not None:
                heapq.heappush(self._heap, (cursor.ordering_key(), cursor))

    def pop(self) -> Cursor | None:
        if not self._heap:
            return None
        _key, cursor = heapq.heappop(self._heap)
        return cursor

    def push_after_advance(self, cursor: Cursor) -> None:
        cursor.advance()
        if cursor.current is not None:
            heapq.heappush(self._heap, (cursor.ordering_key(), cursor))

    def __len__(self) -> int:
        return len(self._heap)
