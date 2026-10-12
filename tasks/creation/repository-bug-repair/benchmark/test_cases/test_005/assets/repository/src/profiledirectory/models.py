from __future__ import annotations

from dataclasses import dataclass, field, replace


def clean(value: str, field: str) -> str:
    if not isinstance(value, str):
        raise TypeError(f"{field} must be text")
    value = value.strip()
    if not value:
        raise ValueError(f"{field} must be nonempty")
    return value


@dataclass(frozen=True)
class Profile:
    tenant_id: str
    user_id: str
    email: str
    display_name: str
    revision: int = 1
    attributes: dict[str, object] = field(default_factory=dict)

    def updated(self, *, email: str | None = None, display_name: str | None = None) -> "Profile":
        return replace(
            self,
            email=clean(email, "email") if email is not None else self.email,
            display_name=clean(display_name, "display_name") if display_name is not None else self.display_name,
            revision=self.revision + 1,
        )

    def as_dict(self) -> dict[str, object]:
        return {
            "tenant_id": self.tenant_id,
            "user_id": self.user_id,
            "email": self.email,
            "display_name": self.display_name,
            "revision": self.revision,
            "attributes": dict(self.attributes),
        }
