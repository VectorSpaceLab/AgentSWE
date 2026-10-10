from __future__ import annotations

from .cache import ProfileCache
from .events import EventBus, ProfileChanged
from .models import Profile, clean
from .repository import ProfileRepository


class DirectoryService:
    def __init__(
        self,
        repository: ProfileRepository | None = None,
        cache: ProfileCache | None = None,
        events: EventBus | None = None,
    ) -> None:
        self.repository = repository if repository is not None else ProfileRepository()
        self.cache = cache if cache is not None else ProfileCache()
        self.events = events if events is not None else EventBus()
        self.events.subscribe(self._on_profile_changed)

    def _on_profile_changed(self, event: ProfileChanged) -> None:
        self.cache.invalidate(event.user_id)

    def get_user(self, tenant_id: str, user_id: str) -> dict[str, object] | None:
        tenant_id = clean(tenant_id, "tenant_id")
        user_id = clean(user_id, "user_id")
        hit, profile = self.cache.lookup(tenant_id, user_id)
        if not hit:
            profile = self.repository.get(tenant_id, user_id)
            self.cache.put(tenant_id, user_id, profile)
        return None if profile is None else profile.as_dict()

    def create_user(
        self,
        tenant_id: str,
        user_id: str,
        email: str,
        display_name: str,
    ) -> dict[str, object]:
        profile = Profile(
            clean(tenant_id, "tenant_id"),
            clean(user_id, "user_id"),
            clean(email, "email"),
            clean(display_name, "display_name"),
        )
        self.repository.put(profile, expected_revision=0)
        self.events.publish(ProfileChanged(profile.user_id, profile.revision))
        return profile.as_dict()

    def update_user(
        self,
        tenant_id: str,
        user_id: str,
        *,
        email: str | None = None,
        display_name: str | None = None,
    ) -> dict[str, object]:
        updated = self.repository.update(
            clean(tenant_id, "tenant_id"),
            clean(user_id, "user_id"),
            email=email,
            display_name=display_name,
        )
        self.events.publish(ProfileChanged(updated.user_id, updated.revision))
        return updated.as_dict()

    def cache_size(self) -> int:
        return self.cache.size()
