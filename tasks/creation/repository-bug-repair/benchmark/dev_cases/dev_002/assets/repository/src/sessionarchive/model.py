from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class Session:
    session_id: str
    user_id: str
    expires_at_ms: int
    metadata: dict[str, object]
    extension: dict[str, object]

    def as_dict(self) -> dict[str, object]:
        value = {
            "session_id": self.session_id,
            "user_id": self.user_id,
            "expires_at_ms": self.expires_at_ms,
            "metadata": dict(self.metadata),
        }
        value.update(self.extension)
        return value


def ensure_mapping(value: object, field: str) -> dict[str, object]:
    if value is None:
        return {}
    if not isinstance(value, dict):
        raise ValueError(f"{field} must be an object")
    return dict(value)
