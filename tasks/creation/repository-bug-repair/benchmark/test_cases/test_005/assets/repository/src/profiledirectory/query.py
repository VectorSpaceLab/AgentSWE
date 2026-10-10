from __future__ import annotations

from .models import Profile
from .repository import ProfileRepository


def list_tenant(repository: ProfileRepository, tenant_id: str) -> list[dict[str, object]]:
    profiles = [
        profile
        for profile in repository.snapshot()
        if profile.tenant_id == tenant_id
    ]
    profiles.sort(key=lambda profile: profile.user_id)
    return [profile.as_dict() for profile in profiles]


def revisions(repository: ProfileRepository, tenant_id: str) -> dict[str, int]:
    return {
        profile.user_id: profile.revision
        for profile in repository.snapshot()
        if profile.tenant_id == tenant_id
    }


def find_email(repository: ProfileRepository, tenant_id: str, email: str) -> Profile | None:
    matches = [
        profile
        for profile in repository.snapshot()
        if profile.tenant_id == tenant_id and profile.email == email
    ]
    if len(matches) > 1:
        raise ValueError("email is not unique within tenant")
    return matches[0] if matches else None
