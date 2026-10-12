#!/usr/bin/env python3
"""Assemble runtime-dependencies-v1 and trusted-browser-v1 from an Ubuntu 24.04
stage (apt packages pinned by runtime-dependencies.manifest.json), the Chrome for
Testing build and the official Node.js build.  Library bytes are verified against
the paper-era manifest; trusted-browser-v1 gets a fresh bundle-manifest.json in
the format evaluator/trusted_browser.py verifies."""
import hashlib, json, os, shutil, sys
from pathlib import Path

man = json.loads(Path(sys.argv[1]).read_text())
deps, tb = Path(sys.argv[2]), Path(sys.argv[3])
chrome, node, node_version = Path(sys.argv[4]), Path(sys.argv[5]), sys.argv[6]
sha = lambda p: hashlib.sha256(p.read_bytes()).hexdigest()
bad = []
for rel, info in sorted(man["files"].items()):
    out = deps / rel
    out.parent.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(Path(info["source"]).resolve(), out)
    os.chmod(out, 0o644)
    if sha(out) != info["sha256"]:
        bad.append(rel)
# fontconfig configuration + DejaVu fonts, as the paper-era runtime-dependencies-v1/fonts
shutil.copytree("/fonts/etc", deps / "fonts/etc", symlinks=True)
shutil.copytree("/fonts/share", deps / "fonts/share", symlinks=True)
# keep exactly the font files of the paper-era bundle (fonts.manifest.json: name -> sha256 /
# symlink target); the jammy packages also ship a few language-selector files it lacked.
fman = json.loads(Path(sys.argv[7]).read_text())
for p in sorted((deps / "fonts").rglob("*"), reverse=True):
    rel = str(p.relative_to(deps))
    if (p.is_file() or p.is_symlink()) and rel not in fman and p.name != ".uuid":
        p.unlink()
for rel, info in fman.items():
    p = deps / rel
    ok = (p.is_symlink() and os.readlink(p) == info.get("link")) if "link" in info else (p.is_file() and not p.is_symlink() and sha(p) == info["sha256"])
    if not ok:
        bad.append(rel)
shutil.copyfile(sys.argv[1], deps / "manifest.json")
# trusted-browser-v1 (trusted_browser.prepare(): copytree follows symlinks)
shutil.copytree(chrome, tb / "chrome")
shutil.copytree(deps / "browser/lib", tb / "lib")
shutil.copytree(deps / "fonts", tb / "fonts")
shutil.copyfile(node, tb / "node"); os.chmod(tb / "node", 0o555)
conf = tb / "fonts/etc/fonts.conf"
conf.write_text(conf.read_text().replace("/usr/share/fonts", "/opt/agentswe-trusted-browser/fonts/share"))
def entries(root):
    res = {}
    for p in sorted(root.rglob("*")):
        if p.is_symlink(): res[str(p.relative_to(root))] = {"link": os.readlink(p)}
        elif p.is_file() and p != root / "bundle-manifest.json":
            res[str(p.relative_to(root))] = {"sha256": sha(p), "mode": p.stat().st_mode & 0o777}
    return res
(tb / "bundle-manifest.json").write_text(json.dumps({
    "scope": "evaluator-only Chromium/Node/fonts/shared-library copy",
    "node_version": node_version,
    "sources": {"chrome": "Chrome for Testing linux64 (Playwright chromium-1228)", "node": "nodejs.org official linux-x64 build",
                "fonts": "Ubuntu 22.04 fontconfig-config + fonts-dejavu-core/-extra", "libraries": "Ubuntu 24.04 packages pinned in runtime-dependencies-v1/manifest.json"},
    "files": entries(tb)}, indent=2) + "\n")
if bad:
    sys.exit(f"{len(bad)} libraries differ from the paper-era manifest: {bad[:6]}")
print(f"runtime-dependencies-v1: {len(man['files'])} libraries verified; trusted-browser-v1 assembled")
