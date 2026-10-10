#!/usr/bin/env python3
"""The release-asset store: every pinned file it serves, its flat (GitHub release) name, and the upload set.

Usage:
  tools/release_assets.py check                          every pin's flat name; fails on a collision
  tools/release_assets.py build --store DIR --out DIR    the flat upload set from a tree store
  tools/release_assets.py verify DIR [--sizes-only]      a flat upload set against its manifest and the pins

A tree store holds each pinned file at its release path: `release_path` in a task.json `runner_config.assets`, and
editing/env-archives/<name> for the archives an Editing env/env.json pins. A flat store (a GitHub release, whose asset
names have no directories) holds it as `optimization_assets.release_asset_name(sha256, file name)`, which is what
setup requests when AGENTSWE_RELEASE_ASSETS_URL is a GitHub release download URL (docs/ENV.md, Release assets).

`build` hard-links each pinned file from the tree store into the output directory (copies it where a link is not
possible; the tree store is only read), checks its size and sha256 against the pin, and adds the extra assets: a
README for the release, the environment-archive license notes and the GNU license texts (third_party/), SHA256SUMS
(by flat name) and the manifest (flat name, release path, sha256, size of every other asset). The output directory
then holds exactly the files to upload (tools/publish_release_assets.sh).
"""
from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import sys
from dataclasses import dataclass
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from agentswe.runners.editing_agentloop_v1 import ENV_ARCHIVE_RELEASE_DIR, pinned_env_archives  # noqa: E402
from agentswe.runners.optimization_assets import release_asset_name  # noqa: E402
from agentswe.util import sha256_file  # noqa: E402

MANIFEST = "release-assets-manifest.json"
SUMS = "SHA256SUMS"
README = "README.md"
DOCS = {  # extra asset name -> repository file
    "env-archives-README.md": "third_party/env-archives/README.md",
    "GPL-3.0.txt": "third_party/licenses/GPL-3.0.txt",
    "LGPL-3.0.txt": "third_party/licenses/LGPL-3.0.txt",
}
EXTRA = (README, *DOCS, SUMS, MANIFEST)
ASSET_NAME = re.compile(r"^[A-Za-z0-9_-][A-Za-z0-9._-]*[A-Za-z0-9_-]$")
MAX_NAME = 255
GITHUB_MAX_ASSET_BYTES = 2 * 1024 ** 3  # each release asset must be smaller (docs.github.com, About releases)
GITHUB_MAX_ASSETS = 1000  # per release
SCHEMA = "agentswe-release-assets/v1"


@dataclass(frozen=True)
class Pin:
    release_path: str
    sha256: str
    size: int | None
    tasks: tuple[str, ...]

    @property
    def name(self) -> str:
        return release_asset_name(self.sha256, self.release_path.rsplit("/", 1)[-1])


def pins(root: Path = ROOT) -> list[Pin]:
    """Every file the release-asset store serves, once per release path, with the tasks that pin it."""
    found: dict[str, dict] = {}

    def add(rel: str, sha: str, size: int | None, task: str) -> None:
        rec = found.setdefault(rel, {"sha256": sha, "size": size, "tasks": []})
        if rec["sha256"] != sha or (None not in (rec["size"], size) and rec["size"] != size):
            raise SystemExit(f"{rel}: {task} pins it differently from {', '.join(rec['tasks'])}")
        rec["size"] = size if rec["size"] is None else rec["size"]
        if task not in rec["tasks"]:
            rec["tasks"].append(task)

    for path in sorted((root / "tasks").glob("*/*/task.json")):
        data = json.loads(path.read_text())
        for item in ((data.get("runner_config") or {}).get("assets") or {}).get("files", []):
            if item.get("release_path"):
                add(item["release_path"], item["sha256"], item.get("size"), data.get("id") or path.parent.name)
    for path in sorted((root / "tasks" / "editing").glob("*/env/env.json")):
        for name, sha, size, _optional in pinned_env_archives(json.loads(path.read_text())):
            add(f"{ENV_ARCHIVE_RELEASE_DIR}/{name}", sha, size, path.parent.parent.name)
    return [Pin(rel, r["sha256"], r["size"], tuple(r["tasks"])) for rel, r in sorted(found.items())]


def flat_assets(pin_list: list[Pin]) -> dict[str, list[Pin]]:
    """Flat name -> the pins it serves. Two release paths share a name only when their bytes are identical (same
    sha256 and sanitized file name); any other clash, a clash that differs only by letter case, a clash with an extra
    asset, or a name GitHub would rewrite stops here."""
    by_name: dict[str, list[Pin]] = {}
    folded: dict[str, str] = {name.lower(): name for name in EXTRA}
    problems = []
    for pin in pin_list:
        name = pin.name
        if not ASSET_NAME.match(name) or len(name) > MAX_NAME:
            problems.append(f"{pin.release_path}: flat name {name!r} is not a plain GitHub asset name")
        group = by_name.setdefault(name, [])
        if group and group[0].sha256 != pin.sha256:
            problems.append(f"{name}: {pin.release_path} and {group[0].release_path} differ in content")
        group.append(pin)
        other = folded.setdefault(name.lower(), name)
        if other != name:
            problems.append(f"{name} and {other} differ only by letter case")
    if problems:
        raise SystemExit("flat release-asset names collide:\n  " + "\n  ".join(problems))
    return dict(sorted(by_name.items()))


def human(size: int) -> str:
    for unit in ("B", "KB", "MB", "GB"):
        if size < 1000 or unit == "GB":
            return f"{size:.1f} {unit}" if unit != "B" else f"{size} B"
        size /= 1000
    return ""


def readme_text(rows: list[dict], tag: str, repo: str = "<owner>/<repo>") -> str:
    total = sum(r["size"] for r in rows)
    return f"""# AgentSWE release assets ({tag})

The files that `agentswe setup` downloads from the AgentSWE release asset store when they are not already in
`AGENTSWE_HOME/cache/downloads`. Point setup at this release with

    AGENTSWE_RELEASE_ASSETS_URL=https://github.com/{repo}/releases/download/{tag}

Every file is pinned by sha256 (and size) in the repository: `task.json` `runner_config.assets` for Terminal-Bench
and OSWorld, `env/env.json` for the Editing environment archives; setup checks each download against its pin.

A GitHub release has no directories, so each pinned file is published under a flat name: the first 16 hex digits
of its sha256, `__`, and its file name with every character outside `[A-Za-z0-9._-]` replaced by `_`. Setup derives
that name from the pin. `{MANIFEST}` maps each asset to its release path (the path that the pins and
`THIRD_PARTY.md` name), sha256 and size; `{SUMS}` lists the assets by name (`sha256sum -c {SUMS}`).

| Release path | Contents |
|---|---|
| `editing/env-archives/` | pinned environment archives of three Editing tasks (OpenClaw runtime with its content manifest, Dyad dependencies, Codex cargo home) |
| `terminalbench/images/` | the locked dependency image of `write-compressor` (`docker save`) |
| `terminalbench/task-assets/` | repository snapshots, wheels, an Alpine ISO, dataset files and the uv binary that upstream task builds download |
| `osworld/` | OSWorld source at a pinned commit, the VM kernel and initrd, and the task files (setup and evaluator) |

{len(rows)} pinned files, {human(total)} in total. Upstream sources and licenses: `THIRD_PARTY.md` in the repository.
`env-archives-README.md` (the repository's `third_party/env-archives/README.md`) lists what the environment archives
contain; the GNU license texts it refers to are `GPL-3.0.txt` and `LGPL-3.0.txt`.
"""


def _same(path: Path, src: Path) -> bool:
    try:
        return path.is_file() and os.path.samefile(path, src)
    except OSError:
        return False


def _put(src: Path, dest: Path) -> str:
    """dest becomes src's bytes: 'kept' (already the same file), 'linked' or 'copied'."""
    if _same(dest, src):
        return "kept"
    tmp = dest.with_name(dest.name + ".part")
    tmp.unlink(missing_ok=True)
    try:
        os.link(src, tmp)
        how = "linked"
    except OSError:
        shutil.copyfile(src, tmp)
        how = "copied"
    os.replace(tmp, dest)
    return how


def build(store: Path, out: Path, root: Path = ROOT, tag: str = "assets-v1", verify: bool = True,
          repo: str = "<owner>/<repo>") -> dict:
    groups = flat_assets(pins(root))
    if len(groups) + len(EXTRA) > GITHUB_MAX_ASSETS:
        raise SystemExit(f"{len(groups) + len(EXTRA)} assets: a GitHub release holds at most {GITHUB_MAX_ASSETS}")
    out.mkdir(parents=True, exist_ok=True)
    planned = set(groups) | set(EXTRA)
    stray = sorted(p.name for p in out.iterdir() if p.name not in planned and p.name.removesuffix(".part") not in planned)
    if stray:
        raise SystemExit(f"{out} holds files outside the upload set (remove them first): {', '.join(stray)}")
    rows, problems, how = [], [], {"kept": 0, "linked": 0, "copied": 0}
    for name, group in groups.items():
        pin = group[0]
        src = store / pin.release_path
        if not src.is_file():
            problems.append(f"{pin.release_path}: not in {store}")
            continue
        size = src.stat().st_size
        if any(p.size not in (None, size) for p in group):
            problems.append(f"{pin.release_path}: {size} bytes, pinned {pin.size}")
            continue
        if size >= GITHUB_MAX_ASSET_BYTES:
            problems.append(f"{pin.release_path}: {size} bytes, over the GitHub release asset limit")
            continue
        if verify and sha256_file(src) != pin.sha256:
            problems.append(f"{pin.release_path}: sha256 differs from the pin {pin.sha256}")
            continue
        how[_put(src, out / name)] += 1
        rows.append({"name": name, "release_paths": [p.release_path for p in group], "sha256": pin.sha256,
                     "size": size, "kind": "pinned", "tasks": sorted({t for p in group for t in p.tasks})})
    if problems:
        raise SystemExit("the tree store does not match the pins:\n  " + "\n  ".join(problems))
    (out / README).write_text(readme_text(rows, tag, repo))
    for name, rel in DOCS.items():
        shutil.copyfile(root / rel, out / name)
    docs = [{"name": n, "release_paths": [], "sha256": sha256_file(out / n), "size": (out / n).stat().st_size,
             "kind": "doc", "source": DOCS.get(n, "generated by tools/release_assets.py")} for n in (README, *DOCS)]
    listed = sorted(rows + docs, key=lambda r: r["name"])
    (out / SUMS).write_text("".join(f"{r['sha256']}  {r['name']}\n" for r in listed))
    sums = {"name": SUMS, "release_paths": [], "sha256": sha256_file(out / SUMS), "size": (out / SUMS).stat().st_size,
            "kind": "checksums"}
    manifest = {"schema_version": SCHEMA, "tag": tag, "layout": "flat",
                "name_rule": "<first 16 hex digits of sha256>__<file name, characters outside [A-Za-z0-9._-] -> _>",
                "pinned_files": len(rows), "pinned_bytes": sum(r["size"] for r in rows),
                "assets": sorted(listed + [sums], key=lambda r: r["name"])}
    (out / MANIFEST).write_text(json.dumps(manifest, indent=1) + "\n")
    return {"pinned": len(rows), "bytes": manifest["pinned_bytes"], "assets": len(manifest["assets"]) + 1, **how}


def verify(flat: Path, root: Path = ROOT, sizes_only: bool = False) -> int:
    manifest = json.loads((flat / MANIFEST).read_text())
    rows = {r["name"]: r for r in manifest["assets"]}
    present = {p.name for p in flat.iterdir()}
    problems = [f"missing: {n}" for n in sorted(set(rows) - present)]
    problems += [f"not in the manifest: {n}" for n in sorted(present - set(rows) - {MANIFEST})]
    for name, row in rows.items():
        path = flat / name
        if not path.is_file():
            continue
        if path.stat().st_size != row["size"]:
            problems.append(f"{name}: {path.stat().st_size} bytes, manifest {row['size']}")
        elif not sizes_only and sha256_file(path) != row["sha256"]:
            problems.append(f"{name}: sha256 differs from the manifest")
    for name, group in flat_assets(pins(root)).items():
        row = rows.get(name)
        if row is None or row["sha256"] != group[0].sha256:
            problems.append(f"{name}: pinned by {group[0].release_path} but not in the manifest as pinned")
    if problems:
        raise SystemExit(f"{flat} is not the upload set of this checkout:\n  " + "\n  ".join(problems))
    return len(rows) + 1


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("check", help="list every pin's flat name; fail on a collision")
    b = sub.add_parser("build", help="the flat upload set from a tree store")
    b.add_argument("--store", type=Path, required=True, help="tree store (files at their release paths)")
    b.add_argument("--out", type=Path, required=True, help="output directory (the upload set)")
    b.add_argument("--tag", default="assets-v1", help="release tag named in the README (default: assets-v1)")
    b.add_argument("--no-verify", action="store_true", help="check sizes only, not sha256")
    b.add_argument("--repo", default="<owner>/<repo>", help="GitHub owner/repo publishing the release, named in the README")
    v = sub.add_parser("verify", help="a flat upload set against its manifest and the pins")
    v.add_argument("dir", type=Path)
    v.add_argument("--sizes-only", action="store_true")
    a = ap.parse_args(argv)
    if a.cmd == "check":
        pin_list = pins()
        groups = flat_assets(pin_list)
        for name, group in groups.items():
            print(f"{name}\t{', '.join(p.release_path for p in group)}")
        print(f"{len(pin_list)} release paths -> {len(groups)} flat names, no collision "
              f"({sum(' ' in p.release_path for p in pin_list)} release paths with spaces)", file=sys.stderr)
    elif a.cmd == "build":
        r = build(a.store.resolve(), a.out, tag=a.tag, verify=not a.no_verify, repo=a.repo)
        print(f"{r['pinned']} pinned files ({human(r['bytes'])}; {r['linked']} hard-linked, {r['copied']} copied, "
              f"{r['kept']} already in place) and {r['assets'] - r['pinned']} extra assets -> {a.out}")
    else:
        n = verify(a.dir, sizes_only=a.sizes_only)
        print(f"{a.dir}: {n} assets match the manifest and the pins of this checkout")
    return 0


if __name__ == "__main__":
    sys.exit(main())
