from __future__ import annotations

from .service import DirectoryService


def check_cached_users(
    service: DirectoryService,
    keys: list[tuple[str, str]],
) -> dict[str, object]:
    mismatches = []
    for tenant_id, user_id in keys:
        cached = service.get_user(tenant_id, user_id)
        stored = service.repository.get(tenant_id, user_id)
        stored_value = None if stored is None else stored.as_dict()
        if cached != stored_value:
            mismatches.append({"tenant_id": tenant_id, "user_id": user_id})
    return {
        "checked": len(keys),
        "mismatches": mismatches,
        "coherent": not mismatches,
    }
