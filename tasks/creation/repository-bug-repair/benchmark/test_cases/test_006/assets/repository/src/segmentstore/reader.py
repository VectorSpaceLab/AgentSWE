from __future__ import annotations

import zlib
from pathlib import Path

from .errors import StoreCorruption
from .format import CHECKSUM, LENGTH, MAX_RECORD_BYTES, decode_payload


def read_segment(path: Path) -> list[dict[str, object]]:
    if not path.exists():
        raise StoreCorruption(str(path), 0, "segment is missing")
    data = path.read_bytes()
    records: list[dict[str, object]] = []
    offset = 0
    while offset < len(data):
        record_offset = offset
        if len(data) - offset < LENGTH.size:
            raise StoreCorruption(str(path), offset, "truncated length prefix")
        length = LENGTH.unpack_from(data, offset)[0]
        offset += LENGTH.size
        if length > MAX_RECORD_BYTES:
            raise StoreCorruption(str(path), record_offset, "record length exceeds limit")
        if len(data) - offset < length + CHECKSUM.size:
            raise StoreCorruption(str(path), record_offset, "truncated final record")
        payload = data[offset : offset + length]
        offset += length
        expected = CHECKSUM.unpack_from(data, offset)[0]
        offset += CHECKSUM.size
        actual = zlib.crc32(payload) & 0xFFFFFFFF
        if actual != expected:
            raise StoreCorruption(str(path), record_offset, "checksum mismatch")
        records.append(decode_payload(payload, str(path), record_offset))
    return records
