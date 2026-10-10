from __future__ import annotations

from dataclasses import dataclass


@dataclass
class PendingFrame:
    length: int
    headers: dict[str, str]

    def remaining_bytes(self, buffered: int) -> int:
        return max(0, self.length + 2 - buffered)

    def ready(self, buffered: int) -> bool:
        return buffered >= self.length + 2

    def describe(self) -> dict[str, object]:
        return {"length": self.length, "headers": dict(self.headers)}


@dataclass
class ParserStats:
    bytes_received: int = 0
    frames_emitted: int = 0
    largest_buffer: int = 0

    def on_feed(self, chunk_size: int, buffer_size: int) -> None:
        self.bytes_received += chunk_size
        self.largest_buffer = max(self.largest_buffer, buffer_size)

    def on_frame(self) -> None:
        self.frames_emitted += 1

    def as_dict(self) -> dict[str, int]:
        return {
            "bytes_received": self.bytes_received,
            "frames_emitted": self.frames_emitted,
            "largest_buffer": self.largest_buffer,
        }
