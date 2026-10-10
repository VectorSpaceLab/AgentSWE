#!/usr/bin/env python3
"""Resolve the pinned, task-local OpenClaw runtime without touching its cache."""
from __future__ import annotations

import hashlib
import json
import os
import subprocess
from pathlib import Path


DEFAULT_RUNTIME = Path(
    "@@AGENTSWE_ENVS@@/"
    "openclaw-channel-handoff-ledger-edit-v1"
).resolve()


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _version(executable: Path, flag: str) -> str:
    result = subprocess.run(
        [str(executable), flag], capture_output=True, text=True, check=False, timeout=20
    )
    if result.returncode != 0:
        raise RuntimeError(f"{executable} {flag} failed: {result.stderr[-500:]}")
    return (result.stdout or result.stderr).strip().splitlines()[-1]


def resolve(runtime: Path | None = None, product: Path | None = None) -> dict[str, object]:
    root = (runtime or Path(os.environ.get("OPENCLAW_TASK_RUNTIME", DEFAULT_RUNTIME))).resolve()
    node = root / "bin" / "node"
    pnpm = root / "bin" / "pnpm"
    node_modules = root / "baseline" / "worktree" / "node_modules"
    prebuilt_dist = root / "baseline" / "worktree" / "dist"
    if not node.is_file() or not pnpm.is_file():
        raise FileNotFoundError(f"pinned node/pnpm runtime missing below {root}")
    if not node_modules.is_dir() or not prebuilt_dist.is_dir():
        raise FileNotFoundError(f"prewarmed dependencies/build missing below {root}")
    product_root = (product or Path(__file__).resolve().parents[1] / "input" / "repository").resolve()
    lock = product_root / "pnpm-lock.yaml"
    if not lock.is_file():
        raise FileNotFoundError(f"product lockfile missing: {lock}")
    return {
        "schema_version": "openclaw-task-runtime-v1",
        "root": str(root),
        "node": str(node),
        "node_version": _version(node, "--version"),
        "pnpm": str(pnpm),
        "pnpm_version": _version(pnpm, "--version"),
        "node_modules": str(node_modules),
        "prebuilt_dist": str(prebuilt_dist),
        "product": str(product_root),
        "lock_sha256": sha256_file(lock),
        "cache_read_only": True,
    }


def write_manifest(path: Path, value: dict[str, object]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")


if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser()
    parser.add_argument("--runtime", type=Path)
    parser.add_argument("--product", type=Path)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    result = resolve(args.runtime, args.product)
    if args.output:
        write_manifest(args.output, result)
    print(json.dumps(result, indent=2, ensure_ascii=False))
