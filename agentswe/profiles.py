"""Per-task profile overlays: tasks/<family>/<id>/profiles/<profile>/ holds the files whose bytes differ in that
profile, laid out like the task directory. `staged()` returns a task subdirectory with the overlay applied.

A task can also keep rows encrypted in the repository (task.json `encrypted_rows`: {"sub", "bundle"}), as upstream
BrowseComp ships them; `staged()` then writes the decrypted files into the staged copy under $AGENTSWE_HOME, so the
plaintext never exists in the checkout."""
from __future__ import annotations

import base64
import hashlib
import json
import os
import shutil
from pathlib import Path

from . import util
from .config import Config
from .registry import Task


def overlay(task: Task, profile: str, sub: str = "") -> Path:
    return task.dir / "profiles" / profile / sub if sub else task.dir / "profiles" / profile


def _digest(root: Path) -> str:
    h = hashlib.sha256()
    for p in sorted(root.rglob("*")):
        if p.is_file():
            h.update(p.relative_to(root).as_posix().encode() + b"\0" + hashlib.sha256(p.read_bytes()).digest())
    return h.hexdigest()


def _link_or_copy(src: str, dst: str) -> None:
    try:
        os.link(src, dst)
    except OSError:
        shutil.copy2(src, dst)


def decrypt_rows(bundle: Path, dest: Path) -> list[str]:
    """Write the plaintext files of an encrypted row bundle under dest (upstream BrowseComp scheme: base64 of the
    UTF-8 text XORed with SHA-256(canary) repeated). Each file is checked against the bundle's plaintext_sha256.
    Returns the relative paths written."""
    data = json.loads(bundle.read_text(encoding="utf-8"))
    if data.get("schema_version") != "agentswe-encrypted-rows/v1":
        raise SystemExit(f"{bundle}: unknown encrypted-rows schema")
    key = hashlib.sha256(data["canary"].encode()).digest()

    def plain(ciphertext: str) -> str:
        raw = base64.b64decode(ciphertext)
        return bytes(b ^ key[i % len(key)] for i, b in enumerate(raw)).decode("utf-8")

    files: dict[str, bytes] = {}
    gold = {}
    for row in data["rows"]:
        split_dir = {"dev": "dev_cases", "test": "test_cases"}[row["split"]]
        files[f"{split_dir}/{row['case_id']}/input.md"] = (plain(row["problem"]) + "\n").encode("utf-8")
        gold[row["case_id"]] = {"answer": plain(row["answer"]), "category": row["category"],
                                "source_id": row["source_id"], "upstream_id": row["upstream_id"]}
    files["evaluator/gold.json"] = (json.dumps(gold, indent=2, ensure_ascii=False) + "\n").encode("utf-8")
    want = data["plaintext_sha256"]
    if sorted(want) != sorted(files):
        raise SystemExit(f"{bundle}: decrypted file set differs from plaintext_sha256")
    for rel, payload in files.items():
        if hashlib.sha256(payload).hexdigest() != want[rel]:
            raise SystemExit(f"{bundle}: {rel} does not decrypt to its recorded sha256")
    for rel, payload in sorted(files.items()):
        target = dest / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        target.unlink(missing_ok=True)  # never write through a hard link into the checkout
        target.write_bytes(payload)
    return sorted(files)


def staged(cfg: Config, task: Task, sub: str) -> Path:
    """task.dir/<sub> as the selected profile sees it. Without an overlay for <sub> (and without encrypted rows for
    it) that is the task directory itself; otherwise a copy under $AGENTSWE_HOME/profiles/<profile>/<task id>/<sub>
    (hard links where possible; overlay and decrypted files replace their links, never write through them), rebuilt
    whenever the source, overlay or encrypted bundle changes."""
    source = task.dir / sub
    ov = overlay(task, cfg.profile, sub)
    enc = task.data.get("encrypted_rows") or {}
    bundle = task.dir / enc["bundle"] if enc.get("sub") == sub else None
    if not ov.is_dir() and bundle is None:
        return source
    dest = cfg.home / "profiles" / cfg.profile / task.id / sub
    stamp = dest.parent / f".{sub}.digest"
    digest = _digest(source) + ":" + (_digest(ov) if ov.is_dir() else "-")
    if bundle is not None:
        digest += ":" + hashlib.sha256(bundle.read_bytes()).hexdigest()
    if dest.is_dir() and stamp.is_file() and stamp.read_text() == digest:
        return dest
    if dest.exists():
        shutil.rmtree(dest)
    dest.parent.mkdir(parents=True, exist_ok=True)
    shutil.copytree(source, dest, symlinks=True, copy_function=_link_or_copy)
    for f in sorted(ov.rglob("*")) if ov.is_dir() else ():
        if f.is_file():
            target = dest / f.relative_to(ov)
            target.parent.mkdir(parents=True, exist_ok=True)
            target.unlink(missing_ok=True)
            shutil.copy2(f, target)
    if bundle is not None:
        decrypt_rows(bundle, dest)
    stamp.write_text(digest)
    util.log(f"{task.id}: {sub} staged for profile {cfg.profile} at {dest}")
    return dest
