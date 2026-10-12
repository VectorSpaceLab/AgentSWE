from __future__ import annotations

from collections.abc import Iterable

from .parser import FrameParser


def parse_chunks(
    chunks: Iterable[bytes],
    max_payload_bytes: int = 1024 * 1024,
    max_header_bytes: int = 8192,
) -> list[dict[str, object]]:
    parser = FrameParser(max_payload_bytes, max_header_bytes)
    frames: list[dict[str, object]] = []
    for chunk in chunks:
        frames.extend(parser.feed(chunk))
    parser.finish()
    return frames


def fixed_chunks(payload: bytes, size: int) -> list[bytes]:
    if size <= 0:
        raise ValueError("chunk size must be positive")
    return [payload[index:index + size] for index in range(0, len(payload), size)]
