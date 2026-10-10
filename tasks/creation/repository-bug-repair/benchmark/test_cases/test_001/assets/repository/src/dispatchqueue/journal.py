from __future__ import annotations

import json
import threading

from .models import Delivery


class AuditJournal:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._lines: list[bytes] = []

    def append(self, delivery: Delivery) -> None:
        payload = json.dumps(
            delivery.as_dict(), sort_keys=True, separators=(",", ":")
        ).encode("utf-8")
        with self._lock:
            self._lines.append(payload)

    def lines(self) -> list[bytes]:
        with self._lock:
            return list(self._lines)

    def replay(self) -> list[dict[str, object]]:
        records: list[dict[str, object]] = []
        for line_number, payload in enumerate(self.lines(), 1):
            try:
                value = json.loads(payload.decode("utf-8"))
            except (UnicodeError, json.JSONDecodeError) as exc:
                raise ValueError(f"invalid audit row {line_number}") from exc
            if not isinstance(value, dict):
                raise ValueError(f"audit row {line_number} is not an object")
            records.append(value)
        return records

    def __len__(self) -> int:
        with self._lock:
            return len(self._lines)
