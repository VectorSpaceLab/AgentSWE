from __future__ import annotations

from .errors import ProtocolError


def error_record(error: ProtocolError) -> dict[str, str]:
    return {"code": error.code, "message": str(error)}


def frame_summary(frame: dict[str, object]) -> dict[str, object]:
    headers = frame.get("headers", {})
    payload = frame.get("payload", b"")
    return {
        "header_names": sorted(dict(headers).keys()),
        "payload_bytes": len(payload),
        "content_type": dict(headers).get("content-type"),
    }
