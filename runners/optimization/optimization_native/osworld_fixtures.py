#!/usr/bin/env python3
"""Prepare checksum-locked, attempt-local OSWorld fixture caches."""

from __future__ import annotations

import hashlib
import json
import os
import shutil
from pathlib import Path
from typing import Any


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def prepare_fixture_cache(manifest_path: Path, task_id: str, cache_root: Path) -> list[dict[str, Any]]:
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    if not isinstance(manifest, dict) or manifest.get("schema_version") != "1.0":
        raise RuntimeError("fixture_manifest_invalid")
    files = manifest.get("files")
    if not isinstance(files, list):
        raise RuntimeError("fixture_manifest_files_invalid")
    task_dir = cache_root / task_id
    task_dir.mkdir(parents=True, exist_ok=True)
    evidence: list[dict[str, Any]] = []
    names: set[str] = set()
    for row in files:
        if not isinstance(row, dict) or row.get("task_id") != task_id:
            continue
        name = row.get("cache_name")
        if not isinstance(name, str) or not name or Path(name).name != name or name in names:
            raise RuntimeError("fixture_cache_name_invalid")
        names.add(name)
        source = Path(row.get("source_path", ""))
        if not source.is_absolute():  # release: fixtures fetched by setup under the asset root
            source = Path(os.environ.get("AGENTSWE_OSWORLD_ASSETS", str(
                Path(os.environ.get("AGENTSWE_HOME", ".agentswe")) / "assets" / "osworld"))) / source
        if not source.is_file() or source.stat().st_size != row.get("size"):
            raise RuntimeError(f"fixture_source_invalid:{name}")
        digest = sha256(source)
        if digest != row.get("sha256"):
            raise RuntimeError(f"fixture_digest_mismatch:{name}")
        destination = task_dir / name
        try:
            os.link(source, destination)
        except OSError:
            shutil.copy2(source, destination)
        evidence.append({
            "cache_name": name, "role": row.get("role"),
            "size": source.stat().st_size, "sha256": digest,
        })
    return evidence
