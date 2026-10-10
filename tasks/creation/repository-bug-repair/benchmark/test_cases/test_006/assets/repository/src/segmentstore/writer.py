from __future__ import annotations

import os
from pathlib import Path

from .format import encode_record


def append_record(path: Path, record: dict[str, object]) -> int:
    payload = encode_record(record)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("ab") as handle:
        offset = handle.tell()
        handle.write(payload)
        handle.flush()
        os.fsync(handle.fileno())
    return offset


def write_segment(path: Path, records: list[dict[str, object]]) -> int:
    count = 0
    with path.open("wb") as handle:
        for record in records:
            handle.write(encode_record(record))
            count += 1
        handle.flush()
        os.fsync(handle.fileno())
    return count
