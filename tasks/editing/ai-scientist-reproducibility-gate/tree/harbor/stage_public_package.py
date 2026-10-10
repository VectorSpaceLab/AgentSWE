#!/usr/bin/env python3
"""Stage only the Builder-visible public package for this sibling."""

from __future__ import annotations

import argparse
import json
import shutil
from pathlib import Path


def stage(benchmark: Path, output: Path) -> dict[str, object]:
    if output.exists():
        shutil.rmtree(output)
    output.mkdir(parents=True)
    for name in ("input", "dev_cases"):
        shutil.copytree(benchmark / name, output / name, symlinks=True)
    for item in list(output.rglob("__pycache__")) + list(output.rglob("*.pyc")):
        if item.is_dir():
            shutil.rmtree(item, ignore_errors=True)
        else:
            item.unlink(missing_ok=True)
    manifest: dict[str, object] = {
        "schema_version": "agentswe-ai-scientist-public-package-v1",
        "visible": ["input", "dev_cases"],
        "hidden_mounted": False,
        "evaluator_mounted": False,
        "credentials_mounted": False,
        "prior_runs_mounted": False,
    }
    (output / "package_manifest.json").write_text(
        json.dumps(manifest, indent=2, ensure_ascii=False) + "\n", encoding="utf-8"
    )
    return manifest


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--benchmark", type=Path, default=Path(__file__).resolve().parents[1])
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    print(json.dumps(stage(args.benchmark.resolve(), args.output.resolve()), indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
