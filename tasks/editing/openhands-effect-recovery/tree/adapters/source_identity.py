"""Stable patched-source identity; never replaces the execution/Code digest."""
import hashlib
import os
from pathlib import Path


def source_identity(root: Path) -> str:
    value = hashlib.sha256()
    # These are evaluator-created runtime trees, not editable product source.
    excluded = {'.git', 'node_modules', '.react-router'}
    paths = []
    for folder, directories, files in os.walk(root, followlinks=False):
        parent = Path(folder)
        if parent == root:
            directories[:] = [name for name in directories if name not in excluded]
        paths.extend(parent / name for name in files)
        paths.extend(parent / name for name in directories if (parent / name).is_symlink())
    for path in sorted(paths, key=lambda p: p.relative_to(root).as_posix()):
        relative = path.relative_to(root)
        if relative.parts[0] in excluded or path.is_dir() and not path.is_symlink():
            continue
        name = relative.as_posix().encode()
        if path.is_symlink():
            kind, data = b'L', os.readlink(path).encode()
        elif path.is_file():
            kind, data = b'F', path.read_bytes()
        else:
            raise ValueError('Product source contains an unsupported file type')
        value.update(kind); value.update(len(name).to_bytes(8, 'big')); value.update(name)
        value.update(len(data).to_bytes(8, 'big')); value.update(data)
    return value.hexdigest()
