#!/usr/bin/env python3
"""Prepare a writable OpenHands task environment from a read-only prewarm cache.

This intentionally performs no package installation and no network access.  It
is the reusable environment boundary for Edit pilots/formal runs: the source
dependency tree is a cache input, while the resulting prefix belongs to the
current task/run and may be written by Vite, npm, and the lower runtime.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shutil
import subprocess
import time
from pathlib import Path
from typing import Any


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def command_output(command: list[str]) -> tuple[int, str, str]:
    result = subprocess.run(command, text=True, capture_output=True, check=False)
    return result.returncode, result.stdout.strip(), result.stderr.strip()


def make_writable(root: Path) -> None:
    for path in root.rglob("*"):
        try:
            mode = path.stat().st_mode
            path.chmod(mode | (0o700 if path.is_dir() else 0o600))
        except OSError:
            pass


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--repository", type=Path, required=True)
    ap.add_argument("--prewarmed-node-modules", type=Path, required=True)
    ap.add_argument("--output-root", type=Path, required=True)
    args = ap.parse_args()
    repository = args.repository.resolve()
    prewarmed = args.prewarmed_node_modules.resolve()
    output_root = args.output_root.resolve()
    baseline = output_root / "baseline-install"
    destination = baseline / "node_modules"
    lockfile = repository / "package-lock.json"
    if not lockfile.is_file():
        raise SystemExit(f"repository package-lock.json missing: {lockfile}")
    if not prewarmed.is_dir():
        raise SystemExit(f"prewarmed node_modules missing: {prewarmed}")
    lock_digest = sha256(lockfile)
    source_marker = prewarmed.parent / ".benchmark-lock-sha256"
    if source_marker.is_file() and source_marker.read_text(encoding="utf-8").strip() != lock_digest:
        raise SystemExit("prewarmed dependency marker does not match repository package-lock.json")
    shutil.rmtree(output_root, ignore_errors=True)
    baseline.mkdir(parents=True, exist_ok=True)
    for name in ("package.json", "package-lock.json"):
        source = repository / name
        if source.is_file(): shutil.copy2(source, baseline / name)
    started = time.monotonic()
    copy = subprocess.run(["cp", "-r", "--reflink=auto", f"{prewarmed}/.", str(destination)], text=True, capture_output=True, check=False)
    if copy.returncode:
        raise SystemExit(f"dependency copy failed: {copy.stderr[-1000:]}")
    make_writable(destination)
    node_code, node_version, node_error = command_output(["node", "--version"])
    vitest = destination / ".bin" / "vitest"
    vitest_code, vitest_version, vitest_error = command_output([str(vitest), "--version"])
    node_match = re.fullmatch(r"v(\d+)\.(\d+)\.(\d+)", node_version)
    if node_code or not node_match or tuple(map(int, node_match.groups())) < (22, 12, 0):
        raise SystemExit(f"Node >=22.12.0 required: {node_version or node_error}")
    if vitest_code or not vitest_version:
        raise SystemExit(f"prepared Vitest is not executable: {vitest_error or vitest_version}")
    marker = baseline / ".benchmark-lock-sha256"
    marker.write_text(lock_digest + "\n", encoding="utf-8")
    manifest: dict[str, Any] = {
        "schema_version": "agentswe-openhands-task-environment/v1",
        "repository": str(repository),
        "repository_lock_sha256": lock_digest,
        "prewarmed_node_modules": str(prewarmed),
        "writable_node_modules": str(destination),
        "copy_policy": "cp -r --reflink=auto without preserved ownership, followed by writable normalization",
        "node_version": node_version,
        "vitest_version": vitest_version,
        "required_node": ">=22.12.0",
        "network_install_performed": False,
        "source_marker_verified": source_marker.is_file(),
        "elapsed_seconds": round(time.monotonic() - started, 3),
        "writable_prefix_owner": f"{os.getuid()}:{os.getgid()}",
    }
    (output_root / "environment_manifest.json").write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(manifest, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
