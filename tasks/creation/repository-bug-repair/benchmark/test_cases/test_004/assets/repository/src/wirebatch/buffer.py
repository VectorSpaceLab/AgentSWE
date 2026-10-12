from __future__ import annotations

from .errors import ProtocolError


class ByteBuffer:
    def __init__(self, limit: int) -> None:
        self.limit = limit
        self._data = bytearray()

    def append(self, chunk: bytes) -> None:
        if not isinstance(chunk, bytes):
            raise TypeError("feed expects bytes")
        self._data.extend(chunk)
        if len(self._data) > self.limit:
            raise ProtocolError("buffer_limit", "parser buffer limit exceeded")

    def find(self, marker: bytes) -> int:
        return self._data.find(marker)

    def take(self, count: int) -> bytes:
        if count < 0 or count > len(self._data):
            raise RuntimeError("invalid buffer take")
        value = bytes(self._data[:count])
        del self._data[:count]
        return value

    def peek(self, count: int | None = None) -> bytes:
        if count is None:
            return bytes(self._data)
        return bytes(self._data[:count])

    def __len__(self) -> int:
        return len(self._data)
