#!/usr/bin/env python3
"""Safe Candidate materialization/build adapter for the Dyad product."""
from __future__ import annotations

import hashlib
import json
import shutil
import subprocess
from pathlib import Path
from typing import Any

ALLOWED = ("src/", "drizzle/", "e2e-tests/", "testing/fake-llm-server/")
ALLOWED_FILES = {"package.json", "package-lock.json"}


def tree_digest(root: Path) -> str:
    digest = hashlib.sha256()
    for path in sorted(root.rglob("*"), key=lambda p: str(p.relative_to(root))):
        rel = str(path.relative_to(root)).encode()
        digest.update(len(rel).to_bytes(8, "big")); digest.update(rel)
        if path.is_symlink():
            kind, data = b"L", path.readlink().as_posix().encode()
        elif path.is_file():
            kind, data = b"F", path.read_bytes()
        else:
            kind, data = b"D", b""
        digest.update(kind); digest.update(len(data).to_bytes(8, "big")); digest.update(data)
    return digest.hexdigest()


def patch_paths(patch: Path) -> list[str]:
    paths: list[str] = []
    for line in patch.read_text(encoding="utf-8").splitlines():
        # Count one entry per diff header; the +++ marker is not a second file.
        if line.startswith("diff --git "):
            fields = line.split()
            if len(fields) != 4 or not fields[2].startswith("a/") or not fields[3].startswith("b/"):
                raise ValueError("malformed diff header")
            before, after = fields[2][2:], fields[3][2:]
            if before != after:
                raise ValueError("renames are not allowed")
            paths.append(after)
    if not paths or len(paths) != len(set(paths)):
        raise ValueError("patch has no unique paths")
    if any(path.startswith("/") or ".." in Path(path).parts for path in paths):
        raise ValueError("patch path escapes repository")
    if any(path not in ALLOWED_FILES and not path.startswith(ALLOWED) for path in paths):
        raise ValueError("patch changes outside the Dyad allowed edit surface")
    return sorted(paths)


def materialize(repository: Path, submission: Path, destination: Path) -> dict[str, Any]:
    patch = submission / "solution.patch"
    paths = patch_paths(patch)
    shutil.rmtree(destination, ignore_errors=True)
    shutil.copytree(repository, destination, symlinks=True, ignore=shutil.ignore_patterns(".git", "node_modules", "out"))
    env = {**__import__("os").environ, "GIT_CONFIG_GLOBAL": "/dev/null", "GIT_CONFIG_SYSTEM": "/dev/null"}
    commands = [
        ["git", "init", "-q"],
        ["git", "add", "-A"],
        ["git", "-c", "user.email=benchmark@example.invalid", "-c", "user.name=Benchmark", "commit", "-qm", "baseline"],
        ["git", "apply", "--check", str(patch.resolve())],
        ["git", "apply", str(patch.resolve())],
    ]
    results: list[dict[str, Any]] = []
    for command in commands:
        proc = subprocess.run(command, cwd=destination, env={**env}, text=True, capture_output=True, check=False)
        results.append({"command": command, "exit_code": proc.returncode, "stderr_tail": proc.stderr[-1000:]})
        if proc.returncode:
            raise RuntimeError(f"Candidate materialization failed at {command[0]}")
    return {"changed_paths": paths, "candidate_digest": tree_digest(destination), "commands": results}


def build_type_gate(repository: Path, *, skip_install: bool = False) -> dict[str, Any]:
    commands: list[dict[str, Any]] = []
    if not skip_install:
        install = subprocess.run(["npm", "ci", "--no-audit", "--no-fund", "--prefer-offline"], cwd=repository, text=True, capture_output=True, check=False, timeout=900)
        commands.append({"command": "npm ci", "exit_code": install.returncode, "stderr_tail": install.stderr[-2000:]})
        if install.returncode:
            return {"ok": False, "failure_class": "build_failure", "commands": commands}
    gate = subprocess.run(["npm", "run", "ts"], cwd=repository, text=True, capture_output=True, check=False, timeout=900)
    commands.append({"command": "npm run ts", "exit_code": gate.returncode, "stderr_tail": gate.stderr[-2000:]})
    return {"ok": gate.returncode == 0, "failure_class": None if gate.returncode == 0 else "build_failure", "commands": commands}


if __name__ == "__main__":
    raise SystemExit("Use materialize() from the controller")
