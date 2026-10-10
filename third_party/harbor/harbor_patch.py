#!/usr/bin/env python3
"""Apply / verify the AgentSWE patch profiles on a pristine harbor==0.20.0 install.

Standard library only.  Every operation is pinned by sha256:

* ``apply``   - precondition: the installed ``harbor/`` tree is the pristine
                0.20.0 wheel content, or a known AgentSWE profile of it (so a
                profile switch or a re-run is allowed).  Any unknown byte is a
                hard failure; nothing is written in that case.  Postcondition:
                the whole tree (all 396 files) equals ``expected/<profile>.sha256``.
* ``verify``  - compare an installed tree (``--site-packages``/``--venv``) or any
                directory that contains ``harbor/`` (``--root``, e.g. a copy of a
                paper-run overlay) against ``expected/<profile>.sha256``.
* ``status``  - report which known profile, if any, a tree matches.

The patched files themselves live in ``patches/files/<profile>/harbor/...``;
``patches/manifest.json`` records, per profile and path, the upstream sha256,
the patched sha256 and the purpose.  Pristine copies of every touched file are
shipped as profile ``pristine`` so a tree can be switched or restored offline.

Usage (examples)::

    python3 harbor_patch.py apply  --venv "$AGENTSWE_HOME/harbor/venv-creation" --profile creation
    python3 harbor_patch.py verify --venv "$AGENTSWE_HOME/harbor/venv-creation" --profile creation
    python3 harbor_patch.py verify --root /path/to/old/harbor-overlay --profile creation
    python3 harbor_patch.py status --venv "$AGENTSWE_HOME/harbor/venv-site"

Exit status: 0 on success, 1 on a failed check, 2 on a usage/environment error.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import sys
import tempfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
DEFAULT_MANIFEST = HERE / "patches" / "manifest.json"
UPSTREAM_VERSION = "0.20.0"


class PatchError(RuntimeError):
    """A precondition or postcondition failed; the message says which."""


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1 << 20), b""):
            digest.update(block)
    return digest.hexdigest()


def tree_digests(root: Path) -> dict[str, str]:
    """sha256 of every file under ``root/harbor`` keyed by ``harbor/...`` path.

    Bytecode caches are ignored: they are derived, and Python rebuilds or
    ignores stale ones by source mtime/size.
    """
    package = root / "harbor"
    if not package.is_dir():
        raise PatchError(f"no harbor package under {root}")
    values: dict[str, str] = {}
    for path in sorted(package.rglob("*")):
        if not path.is_file():
            continue
        if "__pycache__" in path.parts or path.suffix == ".pyc":
            continue
        values[path.relative_to(root).as_posix()] = sha256_file(path)
    return values


def read_expected(manifest_dir: Path, profile: str) -> dict[str, str]:
    path = manifest_dir / "expected" / f"{profile}.sha256"
    if not path.is_file():
        raise PatchError(f"unknown profile {profile!r}: {path} is missing")
    values: dict[str, str] = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        digest, rel = line.split("  ", 1)
        values[rel] = digest
    return values


def load_manifest(path: Path) -> dict:
    manifest = json.loads(path.read_text(encoding="utf-8"))
    if manifest.get("schema_version") != "agentswe-harbor-patches/v1":
        raise PatchError(f"unsupported manifest schema in {path}")
    if manifest["upstream"]["version"] != UPSTREAM_VERSION:
        raise PatchError("manifest is not for harbor " + UPSTREAM_VERSION)
    return manifest


def resolve_site_packages(args: argparse.Namespace) -> Path:
    if getattr(args, "site_packages", None):
        return Path(args.site_packages).resolve()
    if getattr(args, "venv", None):
        candidates = sorted(Path(args.venv).resolve().glob("lib/python3*/site-packages"))
        if len(candidates) != 1:
            raise PatchError(f"cannot locate a unique site-packages under {args.venv}")
        return candidates[0]
    if getattr(args, "root", None):
        return Path(args.root).resolve()
    raise PatchError("one of --site-packages, --venv or --root is required")


def check_dist_info(site: Path) -> None:
    infos = sorted(site.glob("harbor-*.dist-info"))
    if not infos:
        raise PatchError(f"no harbor dist-info in {site}; is harbor installed here?")
    versions = [p.name[len("harbor-"):-len(".dist-info")] for p in infos]
    if versions != [UPSTREAM_VERSION]:
        raise PatchError(f"expected exactly harbor {UPSTREAM_VERSION}, found {versions}")


def compare(actual: dict[str, str], expected: dict[str, str]) -> list[str]:
    problems: list[str] = []
    for rel in sorted(set(actual) | set(expected)):
        have, want = actual.get(rel), expected.get(rel)
        if have == want:
            continue
        if have is None:
            problems.append(f"missing   {rel}")
        elif want is None:
            problems.append(f"unexpected {rel} ({have[:16]})")
        else:
            problems.append(f"differs   {rel} have {have[:16]} want {want[:16]}")
    return problems


def known_variants(manifest: dict) -> dict[str, dict[str, str]]:
    """path -> {sha256: profile} for every touched path (incl. pristine)."""
    variants: dict[str, dict[str, str]] = {}
    for profile, spec in manifest["profiles"].items():
        for rel, entry in spec["files"].items():
            variants.setdefault(rel, {})[entry["sha256"]] = profile
    return variants


def plan(manifest: dict, manifest_dir: Path, actual: dict[str, str], profile: str):
    if profile not in manifest["profiles"]:
        raise PatchError(f"unknown profile {profile!r}; known: {sorted(manifest['profiles'])}")
    pristine = read_expected(manifest_dir, "pristine")
    variants = known_variants(manifest)
    target = read_expected(manifest_dir, profile)
    problems: list[str] = []
    for rel in sorted(set(actual) | set(pristine)):
        have = actual.get(rel)
        if rel in variants:
            # A touched file may be pristine or any known AgentSWE variant.
            if have not in variants[rel]:
                problems.append(
                    f"unknown   {rel} ({(have or 'absent')[:16]}): neither pristine "
                    "nor a known AgentSWE variant"
                )
        elif have != pristine.get(rel):
            # Every other file must be byte-identical to the 0.20.0 wheel.
            state = "missing" if have is None else ("unexpected" if rel not in pristine else "modified")
            problems.append(f"{state:10s}{rel}")
    if problems:
        raise PatchError("refusing to patch an unrecognised harbor tree:\n  " + "\n  ".join(problems))
    writes = []
    for rel in sorted(variants):
        want = target[rel]
        if actual.get(rel) == want:
            continue
        source_profile = profile if rel in manifest["profiles"][profile]["files"] else "pristine"
        entry = manifest["profiles"][source_profile]["files"][rel]
        writes.append((rel, manifest_dir / entry["source"], want))
    return writes, target


def atomic_copy(source: Path, destination: Path, expected_sha: str) -> None:
    if sha256_file(source) != expected_sha:
        raise PatchError(f"patch payload {source} does not match its manifest sha256")
    mode = destination.stat().st_mode & 0o7777 if destination.exists() else 0o644
    fd, temporary = tempfile.mkstemp(prefix=".agentswe-", dir=str(destination.parent))
    try:
        with os.fdopen(fd, "wb") as handle, source.open("rb") as src:
            shutil.copyfileobj(src, handle)
        os.chmod(temporary, mode)
        os.replace(temporary, destination)
    except BaseException:
        if os.path.exists(temporary):
            os.unlink(temporary)
        raise
    cache = destination.parent / "__pycache__"
    if cache.is_dir():
        for stale in cache.glob(destination.stem + ".*.pyc"):
            stale.unlink()


def cmd_apply(args: argparse.Namespace) -> int:
    manifest_path = Path(args.manifest).resolve()
    manifest_dir = manifest_path.parent
    manifest = load_manifest(manifest_path)
    site = resolve_site_packages(args)
    check_dist_info(site)
    actual = tree_digests(site)
    writes, target = plan(manifest, manifest_dir, actual, args.profile)
    for rel, source, want in writes:
        verb = "would write" if args.dry_run else "write"
        print(f"{verb} {rel} -> {want[:16]} (from {source.relative_to(manifest_dir).as_posix()})")
    if args.dry_run:
        return 0
    for rel, source, want in writes:
        atomic_copy(source, site / rel, want)
    problems = compare(tree_digests(site), target)
    if problems:
        raise PatchError("postcondition failed:\n  " + "\n  ".join(problems))
    receipt = {
        "schema_version": "agentswe-harbor-patch-receipt/v1",
        "profile": args.profile,
        "site_packages": str(site),
        "manifest_sha256": sha256_file(manifest_path),
        "files_written": [rel for rel, _, _ in writes],
        "tree_files": len(target),
        "tree_sha256": hashlib.sha256(
            "".join(f"{d}  {r}\n" for r, d in sorted(target.items())).encode()
        ).hexdigest(),
    }
    if args.receipt:
        Path(args.receipt).write_text(json.dumps(receipt, indent=2) + "\n", encoding="utf-8")
    print(f"OK profile={args.profile} files_written={len(writes)} tree_files={len(target)}")
    return 0


def cmd_verify(args: argparse.Namespace) -> int:
    manifest_path = Path(args.manifest).resolve()
    load_manifest(manifest_path)
    root = resolve_site_packages(args)
    if not args.root:
        check_dist_info(root)
    problems = compare(tree_digests(root), read_expected(manifest_path.parent, args.profile))
    if problems:
        print(f"MISMATCH profile={args.profile} root={root}")
        for line in problems:
            print("  " + line)
        return 1
    print(f"OK profile={args.profile} root={root}")
    return 0


def cmd_status(args: argparse.Namespace) -> int:
    manifest_path = Path(args.manifest).resolve()
    manifest = load_manifest(manifest_path)
    root = resolve_site_packages(args)
    actual = tree_digests(root)
    matches = []
    for profile in manifest["profiles"]:
        if not compare(actual, read_expected(manifest_path.parent, profile)):
            matches.append(profile)
    if matches:
        print(f"{root}: matches profile(s) {', '.join(matches)}")
        return 0
    pristine = read_expected(manifest_path.parent, "pristine")
    print(f"{root}: matches no known profile; differences against pristine {UPSTREAM_VERSION}:")
    variants = known_variants(manifest)
    for line in compare(actual, pristine):
        rel = line.split()[1]
        tag = ""
        if rel in variants and actual.get(rel) in variants[rel]:
            tag = f"  [= {variants[rel][actual[rel]]} variant]"
        print("  " + line + tag)
    return 1


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    sub = parser.add_subparsers(dest="command", required=True)
    for name in ("apply", "verify", "status"):
        p = sub.add_parser(name)
        p.add_argument("--manifest", default=str(DEFAULT_MANIFEST))
        where = p.add_mutually_exclusive_group(required=True)
        where.add_argument("--site-packages")
        where.add_argument("--venv")
        if name != "apply":
            where.add_argument("--root", help="any directory that contains harbor/ (e.g. a copied overlay)")
        if name != "status":
            p.add_argument("--profile", required=True)
        if name == "apply":
            p.add_argument("--dry-run", action="store_true")
            p.add_argument("--receipt")
    args = parser.parse_args(argv)
    if not hasattr(args, "root"):
        args.root = None
    try:
        return {"apply": cmd_apply, "verify": cmd_verify, "status": cmd_status}[args.command](args)
    except PatchError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1 if args.command == "apply" else 2


if __name__ == "__main__":
    sys.exit(main())
