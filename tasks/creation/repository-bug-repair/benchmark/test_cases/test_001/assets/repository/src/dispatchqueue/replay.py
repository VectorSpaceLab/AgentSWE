from __future__ import annotations

from .journal import AuditJournal
from .models import Delivery
from .state import QueueState


def delivery_from_record(record: dict[str, object]) -> Delivery:
    payload = record.get("payload", {})
    if not isinstance(payload, dict):
        raise ValueError("audit payload must be an object")
    sequence = record.get("sequence")
    recorded_ns = record.get("recorded_ns")
    if isinstance(sequence, bool) or not isinstance(sequence, int) or sequence <= 0:
        raise ValueError("audit sequence must be positive")
    if isinstance(recorded_ns, bool) or not isinstance(recorded_ns, int):
        raise ValueError("audit recorded_ns must be an integer")
    return Delivery(
        delivery_id=str(record["delivery_id"]),
        worker=str(record["worker"]),
        payload=dict(payload),
        sequence=sequence,
        recorded_ns=recorded_ns,
    )


def rebuild_state(journal: AuditJournal) -> QueueState:
    state = QueueState()
    expected_sequence = 1
    seen: set[str] = set()
    for record in journal.replay():
        delivery = delivery_from_record(record)
        if delivery.sequence != expected_sequence:
            raise ValueError("audit sequence is not contiguous")
        if delivery.delivery_id in seen:
            raise ValueError("audit contains a duplicate delivery")
        state.commit(delivery)
        seen.add(delivery.delivery_id)
        expected_sequence += 1
    state.reset_next_sequence(expected_sequence)
    return state
