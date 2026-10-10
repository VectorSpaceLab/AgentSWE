from __future__ import annotations

import threading

from .models import Delivery


class QueueState:
    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._by_id: dict[str, Delivery] = {}
        self._ordered: list[Delivery] = []
        self._next_sequence = 1

    def contains(self, delivery_id: str) -> bool:
        with self._lock:
            return delivery_id in self._by_id

    def next_sequence(self) -> int:
        with self._lock:
            value = self._next_sequence
            self._next_sequence += 1
            return value

    def commit(self, delivery: Delivery) -> None:
        with self._lock:
            self._by_id[delivery.delivery_id] = delivery
            self._ordered.append(delivery)

    def rows(self) -> list[Delivery]:
        with self._lock:
            return list(self._ordered)

    def get(self, delivery_id: str) -> Delivery | None:
        with self._lock:
            return self._by_id.get(delivery_id)

    def size(self) -> int:
        with self._lock:
            return len(self._ordered)

    def reset_next_sequence(self, value: int) -> None:
        if value <= 0:
            raise ValueError("next sequence must be positive")
        with self._lock:
            self._next_sequence = value
