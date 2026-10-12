from __future__ import annotations

import json
import struct
import zlib


LENGTH = struct.Struct(">I")
CHECKSUM = struct.Struct(">I")
MAX_RECORD_BYTES = 4 * 1024 * 1024


def encode_record(record: dict[str, object]) -> bytes:
    payload = json.dumps(
        record, sort_keys=True, separators=(",", ":"), ensure_ascii=False
    ).encode("utf-8")
    if len(payload) > MAX_RECORD_BYTES:
        raise ValueError("record is too large")
    checksum = zlib.crc32(payload) & 0xFFFFFFFF
    return LENGTH.pack(len(payload)) + payload + CHECKSUM.pack(checksum)


def decode_payload(payload: bytes, path: str, offset: int) -> dict[str, object]:
    try:
        value = json.loads(payload.decode("utf-8"))
    except (UnicodeError, json.JSONDecodeError) as exc:
        from .errors import StoreCorruption

        raise StoreCorruption(path, offset, "invalid record JSON") from exc
    if not isinstance(value, dict):
        from .errors import StoreCorruption

        raise StoreCorruption(path, offset, "record is not an object")
    return value
