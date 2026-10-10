from __future__ import annotations

from collections import OrderedDict
from threading import RLock

from .models import Quote


class QuoteCache:
    def __init__(self, capacity: int = 128) -> None:
        if capacity <= 0:
            raise ValueError("capacity must be positive")
        self.capacity = capacity
        self._lock = RLock()
        self._entries: OrderedDict[tuple[object, ...], Quote] = OrderedDict()

    def get(self, key: tuple[object, ...]) -> Quote | None:
        with self._lock:
            quote = self._entries.get(key)
            if quote is not None:
                self._entries.move_to_end(key)
            return quote

    def put(self, key: tuple[object, ...], quote: Quote) -> None:
        with self._lock:
            self._entries[key] = quote
            self._entries.move_to_end(key)
            while len(self._entries) > self.capacity:
                self._entries.popitem(last=False)

    def clear(self) -> None:
        with self._lock:
            self._entries.clear()

    def __len__(self) -> int:
        with self._lock:
            return len(self._entries)
