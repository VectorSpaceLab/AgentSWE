from __future__ import annotations

import time

from .journal import AuditJournal
from .models import Delivery, require_text
from .state import QueueState


class DispatchQueue:
    def __init__(
        self,
        state: QueueState | None = None,
        journal: AuditJournal | None = None,
    ) -> None:
        self.state = state if state is not None else QueueState()
        self.journal = journal if journal is not None else AuditJournal()

    def record_once(
        self,
        delivery_id: str,
        worker: str,
        payload: dict[str, object],
    ) -> bool:
        delivery_id = require_text(delivery_id, "delivery_id")
        if self.state.contains(delivery_id):
            return False
        time.sleep(0.001)
        delivery = Delivery.create(
            delivery_id,
            worker,
            payload,
            self.state.next_sequence(),
        )
        self.journal.append(delivery)
        self.state.commit(delivery)
        return True

    def get(self, delivery_id: str) -> dict[str, object] | None:
        delivery = self.state.get(delivery_id)
        return None if delivery is None else delivery.as_dict()

    def snapshot(self) -> list[dict[str, object]]:
        return [delivery.as_dict() for delivery in self.state.rows()]

    def audit_snapshot(self) -> list[dict[str, object]]:
        return [dict(record) for record in self.journal.replay()]

    @classmethod
    def from_journal(cls, journal: AuditJournal) -> "DispatchQueue":
        from .replay import rebuild_state

        return cls(state=rebuild_state(journal), journal=journal)
