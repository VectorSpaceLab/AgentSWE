"""Small, dependency-free protocol primitives shared by the migration tools."""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import subprocess
from pathlib import Path
from typing import Any

LOWER_MODEL = "deepseek-flash"
LOWER_EFFORT = "high"
PLACEHOLDER_KEY = "broker-only-placeholder"
REQUIRED_DELIVERY = ("solution.patch", "edit_report.json", "run_report.json")


def canonical_json(value: Any) -> bytes:
    return (json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False) + "\n").encode("utf-8")


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def sha256_file(path: Path) -> str:
    return sha256_bytes(path.read_bytes())


def tree_digest(root: Path) -> str:
    """Match the authoritative Create Code judge tree digest."""
    root = root.resolve()
    digest = hashlib.sha256()
    for path in sorted(root.rglob("*"), key=lambda p: p.relative_to(root).as_posix()):
        rel = path.relative_to(root).as_posix().encode()
        if path.is_symlink():
            kind, payload = b"L", os.readlink(path).encode()
        elif path.is_file():
            digest.update(b"F"); digest.update(len(rel).to_bytes(8, "big")); digest.update(rel)
            digest.update(path.stat().st_size.to_bytes(8, "big"))
            with path.open("rb") as handle:
                while chunk := handle.read(1024 * 1024): digest.update(chunk)
            continue
        elif path.is_dir():
            continue
        else:
            kind, payload = b"O", b""
        digest.update(kind); digest.update(len(rel).to_bytes(8, "big")); digest.update(rel)
        digest.update(len(payload).to_bytes(8, "big")); digest.update(payload)
    return digest.hexdigest()


def write_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_bytes(canonical_json(value))
    os.replace(tmp, path)


def read_json(path: Path) -> dict[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError(f"expected JSON object: {path}")
    return value


def changed_paths(patch: Path) -> list[str]:
    paths: list[str] = []
    for line in patch.read_text(encoding="utf-8").splitlines():
        if line.startswith("diff --git a/") and " b/" in line:
            paths.append(line.split(" b/", 1)[1])
        elif line.startswith("+++ b/"):
            paths.append(line[6:].split("\t", 1)[0])
    return sorted(set(paths))


def validate_delivery(root: Path) -> list[str]:
    if not root.is_dir():
        return ["candidate directory missing"]
    names = {item.name for item in root.iterdir()}
    errors = [f"missing {name}" for name in REQUIRED_DELIVERY if name not in names]
    errors.extend(f"unexpected top-level artifact {name}" for name in sorted(names - set(REQUIRED_DELIVERY)))
    patch = root / "solution.patch"
    if patch.is_file() and not patch.read_bytes():
        errors.append("solution.patch is empty")
    for name in REQUIRED_DELIVERY[1:]:
        path = root / name
        if path.is_file():
            try:
                if not isinstance(json.loads(path.read_text(encoding="utf-8")), dict):
                    errors.append(f"{name} is not a JSON object")
            except Exception as exc:
                errors.append(f"{name} invalid JSON: {type(exc).__name__}")
    return errors


def safe_patch_paths(paths: list[str]) -> bool:
    return bool(paths) and all(
        not Path(path).is_absolute() and ".." not in Path(path).parts and "\\" not in path
        for path in paths
    )


def apply_patch(source_repo: Path, patch: Path, destination: Path) -> dict[str, Any]:
    """Materialize and apply a candidate patch without changing the source tree."""
    if destination.exists():
        shutil.rmtree(destination)
    shutil.copytree(source_repo, destination, symlinks=True)
    paths = changed_paths(patch)
    detail: dict[str, Any] = {"changed_paths": paths, "source_digest": tree_digest(source_repo)}
    if not safe_patch_paths(paths):
        detail["failure"] = "unsafe or empty patch paths"
        return detail
    init = subprocess.run(["git", "init", "-q"], cwd=destination, text=True, capture_output=True, check=False)
    if init.returncode:
        detail["failure"] = f"git init failed: {init.stderr[-500:]}"
        return detail
    # The copied benchmark source intentionally has no Git metadata.  Create a
    # local baseline commit in this disposable directory so ``git apply`` can
    # update existing product files as well as add new ones.  No source tree
    # outside ``destination`` is changed.
    baseline = subprocess.run(["git", "add", "-A"], cwd=destination, text=True, capture_output=True, check=False)
    if baseline.returncode:
        detail["failure"] = f"git add baseline failed: {baseline.stderr[-500:]}"
        return detail
    commit = subprocess.run(
        ["git", "-c", "user.name=AgentSWE disposable baseline", "-c", "user.email=agent-swe@example.invalid", "commit", "-qm", "baseline"],
        cwd=destination, text=True, capture_output=True, check=False,
    )
    if commit.returncode:
        detail["failure"] = f"git baseline commit failed: {commit.stderr[-500:]}"
        return detail
    for label, command in (("patch_check", ["git", "apply", "--check", str(patch.resolve())]), ("patch_apply", ["git", "apply", str(patch.resolve())])):
        result = subprocess.run(command, cwd=destination, text=True, capture_output=True, check=False)
        detail[label] = {"exit_code": result.returncode, "stderr": result.stderr[-1000:]}
        if result.returncode:
            detail["failure"] = label
            return detail
    detail["candidate_repo_digest"] = tree_digest(destination)
    return detail
