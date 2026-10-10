#!/usr/bin/env python3
"""Stage a physically trimmed Builder package: input plus public dev cases only."""
from __future__ import annotations

import argparse
import shutil
from pathlib import Path

try:
    from agentloop.protocol import tree_digest, write_json
except ModuleNotFoundError:  # direct script execution
    import sys
    sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
    from agentloop.protocol import tree_digest, write_json  # type: ignore


def stage(source: Path, output: Path) -> dict[str, object]:
    shutil.rmtree(output, ignore_errors=True); output.mkdir(parents=True, exist_ok=True)
    shutil.copytree(source / "input", output / "input", symlinks=False, ignore=shutil.ignore_patterns(".git", "__pycache__"))
    shutil.copytree(source / "dev_cases", output / "dev_cases", symlinks=False, ignore=shutil.ignore_patterns("__pycache__"))
    forbidden = {"evaluator", "test_cases", "meta", ".env"}
    leaked = [path.relative_to(output).as_posix() for path in output.rglob("*") if any(part in forbidden or part.endswith(".env") for part in path.relative_to(output).parts)]
    if leaked: raise RuntimeError(f"public package leakage: {leaked[:5]}")
    manifest = {"schema_version": "agentswe-public-package/v1", "visible_roots": ["input", "dev_cases"], "package_digest": tree_digest(output), "hidden_oracle_mounted": False, "evaluator_source_mounted": False}
    write_json(output / "PUBLIC_PACKAGE_MANIFEST.json", manifest); return manifest


def main() -> int:
    parser = argparse.ArgumentParser(); parser.add_argument("--source", type=Path, required=True); parser.add_argument("--output", type=Path, required=True); args = parser.parse_args(); value = stage(args.source.resolve(), args.output.resolve()); print(value); return 0


if __name__ == "__main__": raise SystemExit(main())
