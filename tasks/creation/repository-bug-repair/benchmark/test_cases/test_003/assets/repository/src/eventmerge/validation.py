from __future__ import annotations


def summarize(events: list[dict[str, object]]) -> dict[str, object]:
    timestamps = [int(event["timestamp_ms"]) for event in events]
    ids = [str(event["event_id"]) for event in events]
    return {
        "count": len(events),
        "ordered": timestamps == sorted(timestamps),
        "unique_exact_ids": len(ids) == len(set(ids)),
        "first_timestamp": timestamps[0] if timestamps else None,
        "last_timestamp": timestamps[-1] if timestamps else None,
    }
