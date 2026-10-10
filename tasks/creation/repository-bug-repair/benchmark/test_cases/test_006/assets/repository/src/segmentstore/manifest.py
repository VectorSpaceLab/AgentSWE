from __future__ import annotations

import json
import os
from pathlib import Path

from .errors import StoreCorruption


def manifest_path(root: Path) -> Path:
    return root / "MANIFEST.json"


def next_manifest_path(root: Path) -> Path:
    return root / "MANIFEST.next"


def load_manifest(path: Path) -> dict[str, object]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise StoreCorruption(str(path), 0, "invalid manifest") from exc
    if not isinstance(value, dict):
        raise StoreCorruption(str(path), 0, "manifest is not an object")
    generation = value.get("generation")
    segments = value.get("segments")
    if isinstance(generation, bool) or not isinstance(generation, int) or generation < 0:
        raise StoreCorruption(str(path), 0, "invalid generation")
    if not isinstance(segments, list) or any(not isinstance(item, str) for item in segments):
        raise StoreCorruption(str(path), 0, "invalid segment list")
    return {"generation": generation, "segments": list(segments)}


def write_manifest(path: Path, value: dict[str, object]) -> None:
    payload = (
        json.dumps(value, sort_keys=True, separators=(",", ":")) + "\n"
    ).encode("utf-8")
    with path.open("wb") as handle:
        handle.write(payload)
        handle.flush()
        os.fsync(handle.fileno())


def install_manifest(root: Path, value: dict[str, object]) -> None:
    temporary = next_manifest_path(root)
    write_manifest(temporary, value)
    os.replace(temporary, manifest_path(root))
