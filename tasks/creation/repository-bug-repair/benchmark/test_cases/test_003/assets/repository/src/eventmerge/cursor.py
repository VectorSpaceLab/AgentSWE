from __future__ import annotations

from collections.abc import Iterable, Iterator

from .models import Event


class Cursor:
    def __init__(self, source_index: int, values: Iterable[dict[str, object]]) -> None:
        self.source_index = source_index
        self._iterator: Iterator[dict[str, object]] = iter(values)
        self.offset = -1
        self.current: Event | None = None
        self.advance()

    def advance(self) -> None:
        try:
            raw = next(self._iterator)
        except StopIteration:
            self.current = None
            return
        self.offset += 1
        event = Event.from_mapping(raw)
        if self.current is not None and event.timestamp_ms < self.current.timestamp_ms:
            raise ValueError(f"source {self.source_index} is not ordered")
        self.current = event

    def ordering_key(self) -> tuple[int, int, int]:
        if self.current is None:
            raise RuntimeError("exhausted cursor")
        return (self.current.timestamp_ms, self.source_index, self.offset)
