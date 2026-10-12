from __future__ import annotations

import hashlib


def encode_frame(
    payload: bytes,
    content_type: str = "application/octet-stream",
    request_id: str | None = None,
    checksum: bool = False,
) -> bytes:
    headers = [f"LEN {len(payload)}", f"Content-Type: {content_type}"]
    if request_id is not None:
        headers.append(f"X-Request-ID: {request_id}")
    if checksum:
        headers.append(f"Checksum-SHA256: {hashlib.sha256(payload).hexdigest()}")
    return ("\r\n".join(headers) + "\r\n\r\n").encode("utf-8") + payload + b"\r\n"
