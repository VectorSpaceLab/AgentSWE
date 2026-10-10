from __future__ import annotations

import os
from pathlib import Path

from .manifest import load_manifest, manifest_path, next_manifest_path
from .reader import read_segment


def validate_manifest_segments(root: Path, manifest: dict[str, object]) -> None:
    for name in manifest["segments"]:
        path = root / str(name)
        read_segment(path)


def choose_recovery(root: Path) -> str:
    active_path = manifest_path(root)
    pending_path = next_manifest_path(root)
    if not pending_path.exists():
        validate_manifest_segments(root, load_manifest(active_path))
        return "clean"
    pending = load_manifest(pending_path)
    try:
        validate_manifest_segments(root, pending)
    except Exception:
        pending_path.unlink()
        return "rolled_back"
    os.replace(pending_path, active_path)
    return "committed"


def remove_stale_temporaries(root: Path) -> list[str]:
    removed = []
    for path in sorted(root.glob("segment-*.bin.tmp")):
        path.unlink()
        removed.append(path.name)
    return removed
