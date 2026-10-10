from __future__ import annotations

from collections import Counter

from .service import DispatchQueue


def queue_metrics(queue: DispatchQueue) -> dict[str, object]:
    rows = queue.snapshot()
    workers = Counter(str(row["worker"]) for row in rows)
    payload_keys = Counter(
        key
        for row in rows
        for key in dict(row.get("payload", {})).keys()
    )
    return {
        "deliveries": len(rows),
        "workers": dict(sorted(workers.items())),
        "payload_keys": dict(sorted(payload_keys.items())),
        "audit_rows": len(queue.audit_snapshot()),
    }
