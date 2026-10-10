"""Stable materialized product identity, independent of evaluator Git metadata."""
import hashlib
import os
from pathlib import Path

def product_source_digest(root: Path) -> str:
    entries = []
    for directory, dirs, files in os.walk(root, followlinks=False):
        base = Path(directory)
        if base == root:
            dirs[:] = [name for name in dirs if name != '.git']
            files = [name for name in files if name != '.git']
        entries.extend(base / name for name in [*dirs, *files])
    digest = hashlib.sha256()
    for path in sorted(entries, key=lambda item: item.relative_to(root).as_posix()):
        relative = path.relative_to(root).as_posix().encode()
        if path.is_symlink(): kind, payload = b'L', os.readlink(path).encode()
        elif path.is_file(): kind, payload = b'F', path.read_bytes()
        else: kind, payload = b'D', b''
        digest.update(len(relative).to_bytes(8, 'big')); digest.update(relative)
        digest.update(kind); digest.update(len(payload).to_bytes(8, 'big')); digest.update(payload)
    return digest.hexdigest()
