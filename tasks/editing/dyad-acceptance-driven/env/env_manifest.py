#!/usr/bin/env python3
"""env_manifest.py <root> [--out FILE] [--exclude REL ...]: content manifest of a directory tree.

One JSON line per entry under <root> (sorted by path): directories with their mode, files with mode, size and
sha256, symlinks with their target. Prints the sha256 of the manifest, which env.json pins; the paper values were
computed the same way over the paper hosts' environment directories."""
import argparse, hashlib, json, os, stat
from concurrent.futures import ThreadPoolExecutor

ap = argparse.ArgumentParser()
ap.add_argument("root")
ap.add_argument("--out")
ap.add_argument("--exclude", nargs="*", default=[])
a = ap.parse_args()
root = os.path.abspath(a.root)
skip = {e.rstrip("/") for e in a.exclude}
entries = []
for d, dn, fn in os.walk(root):
    rel_d = os.path.relpath(d, root)
    keep = []
    for n in dn:
        rel = os.path.normpath(os.path.join(rel_d, n))
        if rel in skip:
            continue
        p = os.path.join(d, n)
        if os.path.islink(p):
            entries.append(rel)
        else:
            entries.append(rel + "/")
            keep.append(n)
    dn[:] = keep
    for n in fn:
        rel = os.path.normpath(os.path.join(rel_d, n))
        if rel not in skip:
            entries.append(rel)


def row(rel):
    p = os.path.join(root, rel.rstrip("/"))
    st = os.lstat(p)
    if stat.S_ISLNK(st.st_mode):
        return {"path": rel, "kind": "link", "target": os.readlink(p)}
    if stat.S_ISDIR(st.st_mode):
        return {"path": rel, "kind": "dir", "mode": oct(st.st_mode & 0o7777)}
    h = hashlib.sha256()
    with open(p, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return {"path": rel, "kind": "file", "mode": oct(st.st_mode & 0o7777), "size": st.st_size, "sha256": h.hexdigest()}


with ThreadPoolExecutor(32) as ex:
    rows = list(ex.map(row, sorted(set(entries))))
text = "".join(json.dumps(r, sort_keys=True) + "\n" for r in rows)
if a.out:
    with open(a.out, "w") as f:
        f.write(text)
print(hashlib.sha256(text.encode()).hexdigest(), len(rows))
