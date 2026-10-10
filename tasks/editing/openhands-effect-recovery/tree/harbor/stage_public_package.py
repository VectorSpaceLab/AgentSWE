#!/usr/bin/env python3
"""Create the physically trimmed, Builder-visible OpenHands package."""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def digest(root: Path) -> str:
    value = hashlib.sha256()
    for path in sorted(root.rglob("*"), key=lambda item: item.relative_to(root).as_posix()):
        relative = path.relative_to(root)
        if "__pycache__" in relative.parts or path.suffix == ".pyc":
            continue
        name = relative.as_posix().encode()
        value.update(len(name).to_bytes(8, "big")); value.update(name)
        if path.is_symlink():
            kind, payload = b"L", os.readlink(path).encode()
        elif path.is_file():
            kind, payload = b"F", path.read_bytes()
        else:
            kind, payload = b"D", b""
        value.update(kind); value.update(len(payload).to_bytes(8, "big")); value.update(payload)
    return value.hexdigest()


def stage(benchmark: Path, output: Path) -> dict[str, object]:
    if output.exists():
        shutil.rmtree(output)
    for name in ("input", "dev_cases"):
        shutil.copytree(
            benchmark / name, output / name, symlinks=True,
            ignore=shutil.ignore_patterns("__pycache__", "*.pyc", "node_modules", ".git"),
        )
    visible_files = sorted(
        path.relative_to(output).as_posix() for path in output.rglob("*") if path.is_file()
    )
    forbidden = [path for path in visible_files if path.startswith(("evaluator/", "test_cases/", "meta/", ".runs/"))]
    if forbidden:
        raise RuntimeError(f"trimmed package leaked forbidden paths: {forbidden}")
    manifest: dict[str, object] = {
        "schema_version": "agentswe-openhands-builder-public-package/v1",
        "visible_roots": ["input", "dev_cases"],
        "visible_files": visible_files,
        "package_digest": digest(output),
        "hidden_mounted": False,
        "evaluator_mounted": False,
        "prior_runs_mounted": False,
        "credential_mounted": False,
    }
    (output / "package_manifest.json").write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")
    return manifest


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--benchmark", type=Path, default=ROOT)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    manifest = stage(args.benchmark.resolve(), args.output.resolve())
    print(json.dumps(manifest, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
