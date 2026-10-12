"""Dependency-free protocol primitives for the OpenWiki Agent-loop sibling."""
from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import stat
import subprocess
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath
from typing import Any

LOWER_MODEL = "deepseek-flash"
LOWER_EFFORT = "high"
BUILDER_MODEL = "deepseek-flash"
BUILDER_EFFORT = "max"
PLACEHOLDER_KEY = "broker-only-placeholder"
REQUIRED_DELIVERY = ("solution.patch", "edit_report.json", "run_report.json")
DEV_CASES = ("dev_001", "dev_002")
HIDDEN_CASES = tuple(f"test_{i:03d}" for i in range(1, 7))

INFRASTRUCTURE_CLASSIFICATIONS = frozenset({
    "broker_failure",
    "provider_failure",
    "credential_mount_failure",
    "evaluator_failure",
    "mount_isolation_failure",
    "docker_failure",
    "timeout_before_product_start",
    "native_binding_failure",
    "launcher_failure",
})

def canonical_json(value: Any) -> bytes:
    return (json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False) + "\n").encode()

def write_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + ".tmp")
    temporary.write_bytes(canonical_json(value))
    os.replace(temporary, path)

def read_json(path: Path) -> dict[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError(f"expected JSON object: {path}")
    return value

def sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()

def file_sha256(path: Path) -> str:
    return sha256_bytes(path.read_bytes())

def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()

def parse_utc_timestamp(value: object, field: str) -> datetime:
    if not isinstance(value, str) or not value:
        raise ValueError(f"{field} must be a non-empty ISO-8601 timestamp")
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        raise ValueError(f"{field} must include a timezone")
    return parsed.astimezone(timezone.utc)

def tree_digest(root: Path) -> str:
    """Hash names, entry kinds and bytes without following symlinks."""
    root = root.resolve()
    digest = hashlib.sha256()
    for path in sorted(root.rglob("*"), key=lambda item: item.relative_to(root).as_posix()):
        relative = path.relative_to(root).as_posix().encode()
        if path.is_symlink():
            kind, payload = b"L", os.readlink(path).encode()
        elif path.is_file():
            kind, payload = b"F", path.read_bytes()
        else:
            kind, payload = b"D", b""
        digest.update(len(relative).to_bytes(8, "big")); digest.update(relative)
        digest.update(kind); digest.update(len(payload).to_bytes(8, "big")); digest.update(payload)
    return digest.hexdigest()

def materialize_external_symlinks(root: Path) -> None:
    """Validate frozen links without dereferencing Candidate-selected host paths.

    Approved dependency snapshots are copied by the evaluator before build.
    Freeze cannot turn an untrusted external link into a host read capability.
    Internal package-manager links remain intact and covered by the digest.
    """
    validate_internal_symlinks(root)

def validate_internal_symlinks(root: Path) -> None:
    root = root.resolve()
    for path in root.rglob("*"):
        if path.is_symlink():
            target = (path.parent / os.readlink(path)).resolve()
            if target != root and root not in target.parents:
                raise ValueError(f"frozen Candidate symlink escapes repository: {path}")

def make_tree_read_only(root: Path) -> None:
    """Remove all write bits while preserving execute/read bits."""
    paths = sorted(root.rglob("*"), key=lambda item: len(item.parts), reverse=True)
    for path in paths:
        if path.is_symlink():
            continue
        mode = stat.S_IMODE(path.stat().st_mode)
        path.chmod(mode & ~0o222)
    mode = stat.S_IMODE(root.stat().st_mode)
    root.chmod(mode & ~0o222)

def make_tree_writable(root: Path) -> None:
    """Make a disposable runtime copy writable without following links."""
    for path in [root, *root.rglob("*")]:
        if path.is_symlink():
            continue
        mode = stat.S_IMODE(path.stat().st_mode)
        path.chmod(mode | 0o200)

def tree_is_read_only(root: Path) -> bool:
    for path in [root, *root.rglob("*")]:
        if path.is_symlink():
            continue
        if stat.S_IMODE(path.stat().st_mode) & 0o222:
            return False
    return True

def changed_paths(patch: Path) -> list[str]:
    paths: set[str] = set()
    for line in patch.read_text(encoding="utf-8").splitlines():
        if line.startswith("diff --git a/") and " b/" in line:
            paths.update((line.split(" a/", 1)[1].split(" b/", 1)[0], line.split(" b/", 1)[1]))
        elif line.startswith("+++ b/"):
            paths.add(line[6:].split("\t", 1)[0])
    return sorted(paths)

def safe_relative_path(raw: str) -> str:
    value = PurePosixPath(raw.replace("\\", "/"))
    if value.is_absolute() or not value.parts or ".." in value.parts:
        raise ValueError(f"unsafe relative path: {raw!r}")
    return value.as_posix()

def validate_delivery(candidate: Path) -> list[str]:
    if not candidate.is_dir(): return ["candidate delivery directory missing"]
    names = {item.name for item in candidate.iterdir()}
    errors = [f"missing {name}" for name in REQUIRED_DELIVERY if name not in names]
    errors += [f"unexpected top-level artifact {name}" for name in sorted(names - set(REQUIRED_DELIVERY))]
    patch = candidate / "solution.patch"
    if patch.is_file() and not patch.read_text(encoding="utf-8", errors="strict").strip(): errors.append("solution.patch is empty")
    for name in ("edit_report.json", "run_report.json"):
        path = candidate / name
        if path.is_file():
            try:
                if not isinstance(json.loads(path.read_text(encoding="utf-8")), dict): errors.append(f"{name} must be a JSON object")
            except Exception as exc: errors.append(f"{name} invalid JSON: {type(exc).__name__}")
    return errors

def apply_patch(source: Path, patch: Path, output: Path) -> dict[str, Any]:
    errors = validate_delivery(patch.parent)
    if errors: return {"valid": False, "classification": "candidate_delivery_failure", "errors": errors}
    if output.exists():raise RuntimeError("fresh Candidate build destination already exists")
    shutil.copytree(source, output, symlinks=True,ignore=shutil.ignore_patterns(".git"))
    git_env={"PATH":"/usr/bin:/bin","LANG":"C.UTF-8","GIT_CONFIG_NOSYSTEM":"1","GIT_CONFIG_GLOBAL":"/dev/null","HOME":str(output)}
    paths = changed_paths(patch)
    try:
        for item in paths: safe_relative_path(item)
        if not paths: raise ValueError("patch has no changed paths")
        subprocess.run(["git", "-c", "core.hooksPath=/dev/null", "-C", str(output), "init", "-q"], check=True, capture_output=True, text=True,env=git_env)
        subprocess.run(["git", "-c", "core.hooksPath=/dev/null", "-C", str(output), "config", "user.email", "benchmark@example.invalid"], check=True,env=git_env)
        subprocess.run(["git", "-c", "core.hooksPath=/dev/null", "-C", str(output), "config", "user.name", "OpenWiki Benchmark"], check=True,env=git_env)
        subprocess.run(["git", "-c", "core.hooksPath=/dev/null", "-C", str(output), "add", "."], check=True, capture_output=True,env=git_env)
        check = subprocess.run(["git", "-c", "core.hooksPath=/dev/null", "-C", str(output), "apply", "--check", str(patch)], capture_output=True, text=True,env=git_env)
        if check.returncode: return {"valid": False, "classification": "candidate_build_failure", "failure": "patch_check", "stderr": check.stderr[-3000:], "changed_paths": paths}
        applied = subprocess.run(["git", "-c", "core.hooksPath=/dev/null", "-C", str(output), "apply", str(patch)], capture_output=True, text=True,env=git_env)
        if applied.returncode: return {"valid": False, "classification": "candidate_build_failure", "failure": "patch_apply", "stderr": applied.stderr[-3000:], "changed_paths": paths}
    except (OSError, subprocess.SubprocessError, ValueError) as exc:
        return {"valid": False, "classification": "candidate_build_failure", "failure": f"{type(exc).__name__}: {exc}", "changed_paths": paths}
    return {"valid": True, "classification": "candidate_materialized", "changed_paths": paths, "source_digest": tree_digest(source), "candidate_repo_digest": tree_digest(output)}
