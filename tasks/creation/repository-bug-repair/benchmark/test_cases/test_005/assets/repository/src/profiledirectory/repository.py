from __future__ import annotations

import threading

from .models import Profile


class ProfileRepository:
    def __init__(self, profiles: list[Profile] | None = None) -> None:
        self._lock = threading.RLock()
        self._profiles = {
            (profile.tenant_id, profile.user_id): profile
            for profile in (profiles or [])
        }

    def get(self, tenant_id: str, user_id: str) -> Profile | None:
        with self._lock:
            return self._profiles.get((tenant_id, user_id))

    def put(self, profile: Profile, expected_revision: int | None = None) -> Profile:
        key = (profile.tenant_id, profile.user_id)
        with self._lock:
            current = self._profiles.get(key)
            if expected_revision is not None:
                actual = 0 if current is None else current.revision
                if actual != expected_revision:
                    raise ValueError("revision conflict")
            self._profiles[key] = profile
            return profile

    def update(
        self,
        tenant_id: str,
        user_id: str,
        *,
        email: str | None = None,
        display_name: str | None = None,
    ) -> Profile:
        with self._lock:
            key = (tenant_id, user_id)
            current = self._profiles[key]
            updated = current.updated(email=email, display_name=display_name)
            self._profiles[key] = updated
            return updated

    def snapshot(self) -> list[Profile]:
        with self._lock:
            return list(self._profiles.values())
