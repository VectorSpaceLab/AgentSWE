from __future__ import annotations

import threading
from dataclasses import dataclass

from .models import Profile


@dataclass(frozen=True)
class CacheEntry:
    profile: Profile | None


class ProfileCache:
    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._entries: dict[tuple[str, str], CacheEntry] = {}

    def lookup(self, tenant_id: str, user_id: str) -> tuple[bool, Profile | None]:
        with self._lock:
            entry = self._entries.get((tenant_id, user_id))
            return (False, None) if entry is None else (True, entry.profile)

    def put(self, tenant_id: str, user_id: str, profile: Profile | None) -> None:
        with self._lock:
            self._entries[(tenant_id, user_id)] = CacheEntry(profile)

    def invalidate(self, user_id: str) -> None:
        with self._lock:
            self._entries.pop(user_id, None)

    def size(self) -> int:
        with self._lock:
            return len(self._entries)
