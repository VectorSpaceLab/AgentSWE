#!/usr/bin/env python3
"""Copy the AI-Scientist bwrap "compiler runtime" (CPython 3.11.16 + its shared
libraries) out of the edit-candidate-python311 image into <dest>, exactly as
listed in compiler-runtime.manifest.json, and verify every file's sha256.
Symlinked sources are copied as regular files (the runtime is mounted at
/compiler and executed through /compiler/lib64/ld-linux-x86-64.so.2)."""
import hashlib, json, os, shutil, stat, sys
from pathlib import Path

manifest_path, dest = Path(sys.argv[1]), Path(sys.argv[2])
manifest = json.loads(manifest_path.read_text())
bad = []
for rel, digest in sorted(manifest["files"].items()):
    src = Path("/") / rel
    out = dest / rel
    out.parent.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(src.resolve(), out)
    mode = os.stat(src.resolve()).st_mode
    os.chmod(out, 0o555 if mode & stat.S_IXUSR else 0o444)
    if hashlib.sha256(out.read_bytes()).hexdigest() != digest:
        bad.append(rel)
shutil.copyfile(manifest_path, dest / "manifest.json")
os.chmod(dest / "manifest.json", 0o444)
if bad:
    sys.exit(f"{len(bad)} files differ from the manifest, e.g. {bad[:5]}")
print(f"compiler runtime: {len(manifest['files'])} files verified")
