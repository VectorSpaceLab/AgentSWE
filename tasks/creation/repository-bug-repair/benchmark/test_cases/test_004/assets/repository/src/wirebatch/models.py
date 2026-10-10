from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class Frame:
    headers: dict[str, str]
    payload: bytes

    def as_dict(self) -> dict[str, object]:
        return {"headers": dict(self.headers), "payload": self.payload}
