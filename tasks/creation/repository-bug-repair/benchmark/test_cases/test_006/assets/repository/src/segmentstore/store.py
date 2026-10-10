from __future__ import annotations

import os
from pathlib import Path

from .format import encode_record
from .manifest import install_manifest, load_manifest, manifest_path, next_manifest_path
from .reader import read_segment


class SegmentStore:
    def __init__(self, root: str | Path) -> None:
        self.root = Path(root)

    def initialize(self) -> None:
        self.root.mkdir(parents=True, exist_ok=True)
        if not manifest_path(self.root).exists():
            segment = self.root / "segment-000000.bin"
            segment.touch()
            install_manifest(self.root, {"generation": 0, "segments": [segment.name]})

    def _manifest(self) -> dict[str, object]:
        self.initialize()
        return load_manifest(manifest_path(self.root))

    def append(self, record: dict[str, object]) -> None:
        manifest = self._manifest()
        segment = self.root / str(manifest["segments"][-1])
        with segment.open("ab") as handle:
            handle.write(encode_record(record))
            handle.flush()
            os.fsync(handle.fileno())

    def load(self) -> list[dict[str, object]]:
        manifest = self._manifest()
        records: list[dict[str, object]] = []
        for name in manifest["segments"]:
            records.extend(read_segment(self.root / str(name)))
        return records

    def compact(self) -> None:
        records = self.load()
        current = self._manifest()
        generation = int(current["generation"]) + 1
        final_name = f"segment-{generation:06d}.bin"
        temporary = self.root / (final_name + ".tmp")
        with temporary.open("wb") as handle:
            for record in records:
                handle.write(encode_record(record))
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, self.root / final_name)
        install_manifest(
            self.root,
            {"generation": generation, "segments": [final_name]},
        )

    def recover(self) -> str:
        pending = next_manifest_path(self.root)
        if pending.exists():
            pending.unlink()
            return "rolled_back"
        return "clean"
