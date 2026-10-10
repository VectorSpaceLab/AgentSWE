from __future__ import annotations

from .buffer import ByteBuffer
from .errors import ProtocolError
from .headers import parse_headers, parse_length, verify_checksum
from .models import Frame


class FrameParser:
    def __init__(
        self,
        max_payload_bytes: int = 1024 * 1024,
        max_header_bytes: int = 8192,
    ) -> None:
        self.max_payload_bytes = max_payload_bytes
        self.max_header_bytes = max_header_bytes
        self.buffer = ByteBuffer(max_payload_bytes + max_header_bytes + 64)

    def feed(self, chunk: bytes) -> list[dict[str, object]]:
        self.buffer.append(chunk)
        frames: list[dict[str, object]] = []
        while len(self.buffer):
            boundary = self.buffer.find(b"\r\n\r\n")
            if boundary < 0:
                if len(self.buffer) > self.max_header_bytes:
                    raise ProtocolError("header_limit", "header block is too large")
                break
            header_block = self.buffer.take(boundary + 4)[:-4]
            lines = header_block.split(b"\r\n")
            length = parse_length(lines[0], self.max_payload_bytes)
            headers = parse_headers(lines[1:])
            if len(self.buffer) < length + 2:
                break
            payload = self.buffer.take(length)
            if self.buffer.take(2) != b"\r\n":
                raise ProtocolError("frame_terminator", "payload lacks trailing CRLF")
            verify_checksum(headers, payload)
            frames.append(Frame(headers, payload).as_dict())
        return frames

    def finish(self) -> None:
        if len(self.buffer):
            raise ProtocolError("truncated_frame", "stream ended mid-frame")
