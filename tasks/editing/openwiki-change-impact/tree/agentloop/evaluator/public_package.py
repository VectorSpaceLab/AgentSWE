#!/usr/bin/env python3
"""Stage only Builder-visible input and public dev cases."""
from __future__ import annotations
import argparse, hashlib, shutil
from pathlib import Path
from ..protocol import DEV_CASES, tree_digest, write_json, validate_internal_symlinks

def stage(source: Path, output: Path, dev_cases: tuple[str, ...] = DEV_CASES) -> dict[str, object]:
    if output.is_symlink(): raise ValueError("public package destination is a symlink")
    for root in (source / "input", *(source / "dev_cases" / case_id for case_id in dev_cases)):
        if root.is_symlink(): raise ValueError("public package source root is a symlink")
        validate_internal_symlinks(root)
    shutil.rmtree(output, ignore_errors=True); output.mkdir(parents=True)
    shutil.copytree(source / "input", output / "input", symlinks=False, ignore=shutil.ignore_patterns(".git", "__pycache__"))
    (output / "dev_cases").mkdir()
    for case_id in dev_cases:
        if case_id not in DEV_CASES:
            raise ValueError(f"unknown public case: {case_id}")
        shutil.copytree(source / "dev_cases" / case_id, output / "dev_cases" / case_id, symlinks=False, ignore=shutil.ignore_patterns("__pycache__"))
    visible = {path.relative_to(output).parts[0] for path in output.iterdir()}
    if visible != {"input", "dev_cases"}: raise RuntimeError(f"unexpected public package roots: {sorted(visible)}")
    leaked = [path.relative_to(output).as_posix() for path in output.rglob("*") if any(part in {"evaluator", "test_cases", "meta", ".env"} for part in path.relative_to(output).parts)]
    if leaked: raise RuntimeError(f"public package leakage: {leaked[:5]}")
    manifest = {"schema_version": "agentswe-openwiki-public-package/v1", "visible_roots": ["input", "dev_cases"], "dev_cases": list(dev_cases), "package_digest": tree_digest(output), "hidden_oracle_mounted": False, "evaluator_source_mounted": False, "credential_mounted": False}
    manifest["visible_file_sha256"] = {p.relative_to(output).as_posix(): hashlib.sha256(p.read_bytes()).hexdigest() for p in sorted(output.rglob("*")) if p.is_file()}
    manifest["source_copy_policy"] = "All input and selected public dev files; only git and Python cache metadata excluded; external symlinks rejected before copying"
    write_json(output / "PUBLIC_PACKAGE_MANIFEST.json", manifest); return manifest

def main() -> int:
    parser = argparse.ArgumentParser(); parser.add_argument("--source", type=Path, required=True); parser.add_argument("--output", type=Path, required=True); args = parser.parse_args(); print(stage(args.source.resolve(), args.output.resolve())); return 0

if __name__ == "__main__": raise SystemExit(main())
