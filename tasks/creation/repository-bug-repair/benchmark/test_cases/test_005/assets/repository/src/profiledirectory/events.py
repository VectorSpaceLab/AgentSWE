from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from threading import RLock


@dataclass(frozen=True)
class ProfileChanged:
    user_id: str
    revision: int


class EventBus:
    def __init__(self) -> None:
        self._lock = RLock()
        self._subscribers: list[Callable[[ProfileChanged], None]] = []

    def subscribe(self, callback: Callable[[ProfileChanged], None]) -> None:
        with self._lock:
            self._subscribers.append(callback)

    def publish(self, event: ProfileChanged) -> None:
        with self._lock:
            subscribers = list(self._subscribers)
        for callback in subscribers:
            callback(event)
