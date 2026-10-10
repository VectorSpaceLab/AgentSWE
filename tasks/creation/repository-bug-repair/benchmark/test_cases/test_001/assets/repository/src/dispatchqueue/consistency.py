from __future__ import annotations

from .service import DispatchQueue


def audit_consistency(queue: DispatchQueue) -> dict[str, object]:
    rows = queue.snapshot()
    audit = queue.audit_snapshot()
    row_ids = [str(row["delivery_id"]) for row in rows]
    audit_ids = [str(row["delivery_id"]) for row in audit]
    return {
        "row_count": len(rows),
        "audit_count": len(audit),
        "duplicate_rows": len(row_ids) - len(set(row_ids)),
        "duplicate_audit": len(audit_ids) - len(set(audit_ids)),
        "same_order": row_ids == audit_ids,
    }
