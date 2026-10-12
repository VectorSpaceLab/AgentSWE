from __future__ import annotations

from dataclasses import dataclass

from .models import Event


@dataclass
class MergeResult:
    events: list[Event]
    consumed: int = 0
    duplicates: int = 0

    def observe(self, event: Event, duplicate: bool) -> None:
        self.consumed += 1
        if duplicate:
            self.duplicates += 1
        else:
            self.events.append(event)

    def dictionaries(self) -> list[dict[str, object]]:
        return [event.as_dict() for event in self.events]

    def stats(self) -> dict[str, int]:
        return {
            "consumed": self.consumed,
            "emitted": len(self.events),
            "duplicates": self.duplicates,
        }
