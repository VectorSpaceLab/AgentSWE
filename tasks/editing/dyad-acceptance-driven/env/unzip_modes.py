#!/usr/bin/env python3
"""unzip_modes.py <zip> <dest>: extract a zip keeping the Unix mode bits and symlinks stored in external_attr
(as Playwright's own extract-zip does); `unzip` is not on every evaluation host."""
import os, stat, sys, zipfile

src, dest = sys.argv[1], sys.argv[2]
os.makedirs(dest, exist_ok=True)
with zipfile.ZipFile(src) as z:
    for info in z.infolist():
        mode = (info.external_attr >> 16) & 0xFFFF
        path = os.path.join(dest, info.filename)
        if not os.path.realpath(path).startswith(os.path.realpath(dest) + os.sep) and os.path.realpath(path) != os.path.realpath(dest):
            raise SystemExit(f"unsafe path in zip: {info.filename}")
        if info.is_dir():
            os.makedirs(path, exist_ok=True)
            if mode:
                os.chmod(path, stat.S_IMODE(mode))
            continue
        os.makedirs(os.path.dirname(path), exist_ok=True)
        if stat.S_ISLNK(mode):
            os.symlink(z.read(info).decode(), path)
            continue
        with z.open(info) as fh, open(path, "wb") as out:
            while True:
                chunk = fh.read(1 << 20)
                if not chunk:
                    break
                out.write(chunk)
        os.chmod(path, stat.S_IMODE(mode) if mode else 0o644)
