from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class Event:
    timestamp_ms: int
    event_id: str
    payload: dict[str, object]

    @classmethod
    def from_mapping(cls, value: dict[str, object]) -> "Event":
        timestamp = value.get("timestamp_ms")
        event_id = value.get("event_id")
        payload = value.get("payload", {})
        if isinstance(timestamp, bool) or not isinstance(timestamp, int):
            raise ValueError("timestamp_ms must be an integer")
        if not isinstance(event_id, str) or not event_id:
            raise ValueError("event_id must be nonempty text")
        if not isinstance(payload, dict):
            raise ValueError("payload must be an object")
        return cls(timestamp, event_id, dict(payload))

    def as_dict(self) -> dict[str, object]:
        return {
            "timestamp_ms": self.timestamp_ms,
            "event_id": self.event_id,
            "payload": dict(self.payload),
        }
