from __future__ import annotations

from .errors import ProtocolError


def validate_limits(max_payload_bytes: int, max_header_bytes: int) -> None:
    for value, name in [
        (max_payload_bytes, "max_payload_bytes"),
        (max_header_bytes, "max_header_bytes"),
    ]:
        if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
            raise ValueError(f"{name} must be a positive integer")
    if max_header_bytes > 1024 * 1024:
        raise ValueError("max_header_bytes is unreasonably large")


def validate_terminator(value: bytes) -> None:
    if value != b"\r\n":
        raise ProtocolError("frame_terminator", "payload lacks trailing CRLF")


def validate_finished(buffered: int, pending: bool) -> None:
    if buffered or pending:
        raise ProtocolError("truncated_frame", "stream ended mid-frame")
