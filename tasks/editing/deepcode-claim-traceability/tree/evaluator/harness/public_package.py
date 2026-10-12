#!/usr/bin/env python3
"""Create the physically trimmed Builder-visible package."""
from __future__ import annotations

import argparse
import shutil
from pathlib import Path


def prepare(source: Path, destination: Path) -> Path:
    shutil.rmtree(destination, ignore_errors=True)
    destination.mkdir(parents=True)
    for name in ("README.md", "input", "dev_cases"):
        src = source / name
        if not src.exists():
            raise FileNotFoundError(src)
        shutil.copytree(src, destination / name, symlinks=True) if src.is_dir() else shutil.copy2(src, destination / name)
    # The public package may not contain hidden/evaluator/meta/history material.
    forbidden = (destination / "test_cases", destination / "evaluator", destination / "meta", destination / "loop_runs")
    if any(path.exists() for path in forbidden):
        raise AssertionError("public package contains forbidden evaluator material")
    return destination


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(); parser.add_argument("--source", type=Path, required=True); parser.add_argument("--destination", type=Path, required=True); args = parser.parse_args(argv)
    print(prepare(args.source.resolve(), args.destination.resolve()))
    return 0


if __name__ == "__main__": raise SystemExit(main())
