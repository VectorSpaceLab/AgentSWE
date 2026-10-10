from __future__ import annotations

from pathlib import Path

from .codec import decode_line, decode_v2, encode_record
from .io import atomic_replace, read_complete_lines
from .migrate import migrate_record


class SessionArchive:
    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)

    def read_raw(self) -> list[dict[str, object]]:
        return [
            decode_line(line, line_number)
            for line_number, line in enumerate(read_complete_lines(self.path), 1)
        ]

    def load_all(self) -> list[dict[str, object]]:
        sessions = []
        for raw in self.read_raw():
            sessions.append(decode_v2(migrate_record(raw)).as_dict())
        return sessions

    def append_v2(self, raw: dict[str, object]) -> None:
        record = migrate_record(raw)
        decode_v2(record)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.path.open("ab") as handle:
            handle.write(encode_record(record))
            handle.flush()

    def migrate_file(self) -> int:
        records = self.read_raw()
        migrated = [migrate_record(record) for record in records]
        payload = b"".join(encode_record(record) for record in migrated)
        self.path.write_bytes(payload)
        return len(migrated)

    def restore(self, records: list[dict[str, object]]) -> None:
        payload = b"".join(encode_record(record) for record in records)
        atomic_replace(self.path, payload)
