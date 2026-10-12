from __future__ import annotations

from .service import DirectoryService


def update_many(
    service: DirectoryService,
    tenant_id: str,
    changes: list[dict[str, str]],
) -> list[dict[str, object]]:
    results = []
    for change in changes:
        results.append(
            service.update_user(
                tenant_id,
                change["user_id"],
                email=change.get("email"),
                display_name=change.get("display_name"),
            )
        )
    return results
