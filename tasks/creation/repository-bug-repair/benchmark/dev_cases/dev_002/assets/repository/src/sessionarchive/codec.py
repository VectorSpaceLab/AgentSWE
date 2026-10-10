from __future__ import annotations

import json

from .model import Session, ensure_mapping


CORE_FIELDS = {
    "version",
    "session_id",
    "user_id",
    "expires",
    "expires_at_ms",
    "metadata",
}


def encode_record(record: dict[str, object]) -> bytes:
    return (
        json.dumps(record, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
        + "\n"
    ).encode("utf-8")


def decode_line(payload: bytes, line_number: int) -> dict[str, object]:
    try:
        value = json.loads(payload.decode("utf-8"))
    except (UnicodeError, json.JSONDecodeError) as exc:
        raise ValueError(f"invalid session record at line {line_number}") from exc
    if not isinstance(value, dict):
        raise ValueError(f"session record at line {line_number} is not an object")
    return value


def decode_v2(record: dict[str, object]) -> Session:
    if record.get("version") != 2:
        raise ValueError(f"unsupported session version: {record.get('version')!r}")
    expires = record.get("expires_at_ms")
    if isinstance(expires, bool) or not isinstance(expires, int) or expires < 0:
        raise ValueError("expires_at_ms must be a nonnegative integer")
    extension = {key: value for key, value in record.items() if key not in CORE_FIELDS}
    return Session(
        session_id=str(record["session_id"]),
        user_id=str(record["user_id"]),
        expires_at_ms=expires,
        metadata=ensure_mapping(record.get("metadata"), "metadata"),
        extension=extension,
    )
