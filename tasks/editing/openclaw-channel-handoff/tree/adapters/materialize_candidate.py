#!/usr/bin/env python3
"""Materialize and optionally build a Candidate in a private runtime copy."""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import subprocess
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from lower_agent.runtime_resolver import resolve, write_manifest  # noqa: E402


def tree_digest(root: Path) -> str:
    digest = hashlib.sha256()
    ignored = {".git", "node_modules", "dist", ".artifacts", "__pycache__"}
    for path in sorted(root.rglob("*"), key=lambda p: p.relative_to(root).as_posix()):
        rel = path.relative_to(root)
        if any(part in ignored for part in rel.parts):
            continue
        name = rel.as_posix().encode()
        digest.update(len(name).to_bytes(8, "big")); digest.update(name)
        if path.is_symlink():
            kind, data = b"L", os.readlink(path).encode()
        elif path.is_file():
            kind, data = b"F", path.read_bytes()
        else:
            kind, data = b"D", b""
        digest.update(kind); digest.update(len(data).to_bytes(8, "big")); digest.update(data)
    return digest.hexdigest()


def assert_no_secrets(root: Path) -> None:
    forbidden = ("OPENAI_API_KEY=sk-", "AGENTSWE_UPSTREAM_API_KEY=", "Authorization: Bearer sk-")
    for path in root.rglob("*"):
        if path.is_file() and path.stat().st_size < 4_000_000:
            text = path.read_text(errors="ignore")
            if any(marker in text for marker in forbidden):
                raise RuntimeError(f"credential material in candidate: {path}")


def materialize(candidate: Path, runtime: Path, prewarm: dict[str, object]) -> None:
    shutil.rmtree(runtime, ignore_errors=True)
    shutil.copytree(
        candidate,
        runtime,
        symlinks=True,
        ignore=shutil.ignore_patterns(".git", "node_modules", "dist", ".artifacts", "__pycache__"),
    )
    # Dependencies and the optional initial dist are copied into the private
    # runtime; the prewarm tree remains read-only and is never used as scratch.
    shutil.copytree(Path(str(prewarm["node_modules"])), runtime / "node_modules", symlinks=True)
    shutil.copytree(Path(str(prewarm["prebuilt_dist"])), runtime / "dist", symlinks=True)
    prewarm_product = Path(str(prewarm["prebuilt_dist"])).parent
    for base in ("packages", "extensions"):
        source_base = prewarm_product / base
        target_base = runtime / base
        if not source_base.is_dir() or not target_base.is_dir():
            continue
        for compiled in source_base.glob("*/dist"):
            target = target_base / compiled.parent.name / "dist"
            if target.parent.is_dir() and not target.exists():
                shutil.copytree(compiled, target, symlinks=True)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--candidate", type=Path, required=True)
    parser.add_argument("--snapshot", type=Path, required=True)
    parser.add_argument("--report", type=Path, required=True)
    parser.add_argument("--runtime", type=Path)
    parser.add_argument("--build", action="store_true")
    args = parser.parse_args()

    candidate = args.candidate.resolve()
    snapshot = args.snapshot.resolve()
    if not candidate.is_dir():
        raise SystemExit(f"candidate directory missing: {candidate}")
    assert_no_secrets(candidate)
    prewarm = resolve(args.runtime, candidate)
    materialize(candidate, snapshot, prewarm)
    digest = tree_digest(snapshot)
    env = dict(os.environ)
    env["PATH"] = f"{prewarm['root']}/bin:{env.get('PATH', '')}"
    env["npm_config_store_dir"] = str(Path(str(prewarm["root"])) / "pnpm-store" / "v11")
    env["CI"] = "true"
    build: dict[str, object] = {"requested": args.build, "status": "not-run"}
    if args.build:
        command = [str(prewarm["pnpm"]), "build:strict-smoke"]
        proc = subprocess.run(command, cwd=snapshot, env=env, capture_output=True, text=True, check=False, timeout=1800)
        build = {
            "requested": True,
            "status": "passed" if proc.returncode == 0 else "failed",
            "exit_code": proc.returncode,
            "command": command,
            "stdout_tail": proc.stdout[-4000:],
            "stderr_tail": proc.stderr[-4000:],
        }
    result = {
        "schema_version": "openclaw-candidate-materialize-v2",
        "candidate_digest": digest,
        "snapshot": str(snapshot),
        "runtime": prewarm,
        "build": build,
        "credential_mode": "placeholder-only",
        "source_untouched": True,
    }
    write_manifest(args.report, result)
    print(json.dumps(result, indent=2, ensure_ascii=False))
    return 0 if build["status"] != "failed" else 1


if __name__ == "__main__":
    raise SystemExit(main())
