from __future__ import annotations

from pathlib import Path

from .manifest import load_manifest, manifest_path, next_manifest_path


def inspect_layout(root: str | Path) -> dict[str, object]:
    root = Path(root)
    active = manifest_path(root)
    pending = next_manifest_path(root)
    return {
        "active": load_manifest(active) if active.exists() else None,
        "pending": load_manifest(pending) if pending.exists() else None,
        "files": sorted(path.name for path in root.iterdir()) if root.exists() else [],
    }
