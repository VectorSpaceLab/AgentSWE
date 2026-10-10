from __future__ import annotations

from dataclasses import dataclass
from time import time_ns


def require_text(value: str, field: str) -> str:
    if not isinstance(value, str):
        raise TypeError(f"{field} must be text")
    value = value.strip()
    if not value:
        raise ValueError(f"{field} must be nonempty")
    return value


@dataclass(frozen=True)
class Delivery:
    delivery_id: str
    worker: str
    payload: dict[str, object]
    sequence: int
    recorded_ns: int

    @classmethod
    def create(
        cls,
        delivery_id: str,
        worker: str,
        payload: dict[str, object],
        sequence: int,
    ) -> "Delivery":
        return cls(
            require_text(delivery_id, "delivery_id"),
            require_text(worker, "worker"),
            dict(payload),
            sequence,
            time_ns(),
        )

    def as_dict(self) -> dict[str, object]:
        return {
            "delivery_id": self.delivery_id,
            "worker": self.worker,
            "payload": dict(self.payload),
            "sequence": self.sequence,
            "recorded_ns": self.recorded_ns,
        }
