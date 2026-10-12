from __future__ import annotations

import hashlib

from .errors import ProtocolError


def parse_length(line: bytes, maximum: int) -> int:
    if not line.startswith(b"LEN "):
        raise ProtocolError("length_line", "frame must start with LEN")
    token = line[4:]
    if not token or not token.isdigit():
        raise ProtocolError("length_value", "length must be decimal digits")
    value = int(token)
    if value > maximum:
        raise ProtocolError("payload_limit", "payload is too large")
    return value


def parse_headers(lines: list[bytes]) -> dict[str, str]:
    result: dict[str, str] = {}
    for raw in lines:
        if b":" not in raw:
            raise ProtocolError("header_syntax", "header lacks colon")
        name_raw, value_raw = raw.split(b":", 1)
        try:
            name = name_raw.decode("ascii").strip().lower()
            value = value_raw.decode("utf-8").strip()
        except UnicodeError as exc:
            raise ProtocolError("header_encoding", "invalid header encoding") from exc
        if not name or any(char not in "abcdefghijklmnopqrstuvwxyz0123456789-" for char in name):
            raise ProtocolError("header_name", "invalid header name")
        if name in result:
            raise ProtocolError("duplicate_header", f"duplicate header: {name}")
        result[name] = value
    if "content-type" not in result:
        raise ProtocolError("missing_header", "content-type is required")
    return result


def verify_checksum(headers: dict[str, str], payload: bytes) -> None:
    expected = headers.get("checksum-sha256")
    if expected is None:
        return
    actual = hashlib.sha256(payload).hexdigest()
    if len(expected) != 64 or expected.lower() != actual:
        raise ProtocolError("checksum", "payload checksum mismatch")
