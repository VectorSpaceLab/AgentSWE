#!/usr/bin/env python3
"""Candidate materialization/build adapter for the DeepCode product tree."""
from __future__ import annotations

import hashlib
import os
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any


IGNORED_TREE_PARTS = {".git", "__pycache__", ".pytest_cache", ".ruff_cache"}


def tree_digest(root: Path) -> str:
    """Hash a Candidate tree without following links or generated caches.

    The digest is used as a lifecycle identity and a freeze fence, so file
    contents alone are insufficient: a file replaced by a symlink must change
    the digest as well.  Permission bits are intentionally excluded because
    freezing removes write bits after the immutable copy is created.
    """
    root = root.resolve()
    if not root.is_dir():
        raise ValueError(f"tree digest root is not a directory: {root}")
    digest = hashlib.sha256()
    for path in sorted(root.rglob("*"), key=lambda item: item.relative_to(root).as_posix()):
        rel = path.relative_to(root)
        if any(part in IGNORED_TREE_PARTS for part in rel.parts) or path.suffix == ".pyc":
            continue
        encoded = rel.as_posix().encode()
        if path.is_symlink():
            kind, payload = b"L", os.readlink(path).encode()
        elif path.is_file():
            kind, payload = b"F", path.read_bytes()
        elif path.is_dir():
            kind, payload = b"D", b""
        else:
            kind, payload = b"O", b""
        digest.update(len(encoded).to_bytes(8, "big")); digest.update(encoded)
        digest.update(kind)
        digest.update(len(payload).to_bytes(8, "big")); digest.update(payload)
    return digest.hexdigest()


def materialize(candidate: Path, destination: Path, *, base_repository: Path | None = None) -> Path:
    """Copy a repository candidate, or apply a delivery patch to the base."""
    shutil.rmtree(destination, ignore_errors=True); destination.mkdir(parents=True)
    if (candidate / "solution.patch").is_file():
        if base_repository is None: raise ValueError("patch delivery requires base_repository")
        shutil.copytree(base_repository, destination, symlinks=True, dirs_exist_ok=True)
        check = subprocess.run(["git", "apply", "--check", str(candidate / "solution.patch")], cwd=destination, text=True, capture_output=True, check=False)
        if check.returncode: raise RuntimeError(f"Candidate patch rejected: {check.stderr[-800:]}")
        applied = subprocess.run(["git", "apply", str(candidate / "solution.patch")], cwd=destination, text=True, capture_output=True, check=False)
        if applied.returncode: raise RuntimeError(f"Candidate patch failed: {applied.stderr[-800:]}")
    else:
        shutil.copytree(candidate, destination, symlinks=True, dirs_exist_ok=True)
    return destination


def build(repository: Path, output: Path, *, readiness_profile: str | None = None) -> dict[str, Any]:
    output.mkdir(parents=True, exist_ok=True)
    python_executable = Path(os.environ.get("DEEPCODE_PYTHON", sys.executable)).absolute()
    command = [str(python_executable), "-I", "-m", "compileall", "-q", "deepcode.py", "cli", "core"]
    before = tree_digest(repository)
    proc = subprocess.run(command, cwd=repository, text=True, capture_output=True, check=False)
    result = {"command": command, "exit_code": proc.returncode, "stdout": proc.stdout[-2000:], "stderr": proc.stderr[-2000:], "digest": tree_digest(repository)}
    if readiness_profile:
        result['readiness_source_immutability'] = {'before': before, 'after': result['digest'], 'unchanged': before == result['digest']}
        if before != result['digest']:
            result.update(exit_code=1, classification='infrastructure_invalid', reason='build changed product source')
    (output / "build_result.json").write_text(__import__("json").dumps(result, indent=2) + "\n", encoding="utf-8")
    return result
