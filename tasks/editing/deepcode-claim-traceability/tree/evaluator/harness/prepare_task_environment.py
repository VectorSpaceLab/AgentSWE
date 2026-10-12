#!/usr/bin/env python3
"""Prepare and attest the writable Python runtime used by DeepCode smoke runs."""
from __future__ import annotations

import argparse
import hashlib
import json
import platform
import subprocess
from pathlib import Path


def sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--environment-root", type=Path, required=True)
    parser.add_argument("--requirements", type=Path, default=Path(__file__).with_name("deepcode-runtime-requirements.txt"))
    parser.add_argument("--python312", type=Path, default=Path("@@AGENTSWE_PYTHON312_PREFIX@@/bin/python3.12"))
    parser.add_argument("--skip-install", action="store_true")
    args = parser.parse_args(argv)
    root = args.environment_root.resolve()
    root.mkdir(parents=True, exist_ok=True)
    base_python = args.python312.resolve()
    if not base_python.is_file():
        raise SystemExit(f"Python 3.12 runtime missing: {base_python}")
    venv = root / "venv"
    runtime = venv / "bin" / "python"
    if not runtime.is_file():
        subprocess.run([str(base_python), "-m", "venv", str(venv)], check=True)
    if not args.skip_install:
        subprocess.run([str(runtime), "-m", "pip", "install", "-r", str(args.requirements.resolve())], check=True)
    probe = subprocess.run([str(runtime), "-c", "import aiohttp,httpx,openai,pydantic_settings,yaml,rich; print('READY')"], text=True, capture_output=True, check=False)
    if probe.returncode != 0:
        raise SystemExit(probe.stderr[-2000:])
    manifest = {
        "schema_version": "deepcode-task-environment-v1",
        "python": str(runtime),
        "python_version": platform.python_version(),
        "requirements": str(args.requirements.resolve()),
        "requirements_sha256": sha256(args.requirements.resolve()),
        "probe": probe.stdout.strip(),
        "candidate_writable": True,
        "isolated_per_case": ["HOME", "XDG_CACHE_HOME", "TMPDIR", "XDG_CONFIG_HOME"],
    }
    (root / "environment_manifest.json").write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(manifest, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
