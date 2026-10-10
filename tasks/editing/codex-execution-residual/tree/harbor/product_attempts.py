"""Private durable product execution ledger, separate from Edit delivery identity."""
import hashlib
import json
import os
from pathlib import Path
import re


class ProductReplayError(RuntimeError):
    pass


def product_source_digest(root: Path) -> str:
    """Applied source before compilation; ignore only repository-root Git metadata."""
    h = hashlib.sha256()
    for path in sorted(root.rglob('*'), key=lambda p: str(p.relative_to(root))):
        rel = path.relative_to(root)
        if rel.parts[0] == '.git':
            continue
        name = str(rel).encode()
        if path.is_symlink():
            kind, data = b'L', os.readlink(path).encode()
        elif path.is_file():
            kind, data = b'F', path.read_bytes()
            kind += b'X' if path.stat().st_mode & 0o111 else b'-'
        else:
            kind, data = b'D', b''
        h.update(len(name).to_bytes(8, 'big')); h.update(name)
        h.update(kind); h.update(len(data).to_bytes(8, 'big')); h.update(data)
    return h.hexdigest()


def write_once(path: Path, value) -> None:
    """A partial or empty existing file is a blocker, never permission to replay."""
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor = os.open(path.parent.parent, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)
    encoded = (json.dumps(value, indent=2, ensure_ascii=False) + '\n').encode()
    with path.open('xb') as output:
        output.write(encoded); output.flush(); os.fsync(output.fileno())
    descriptor = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


class ProductAttempt:
    def __init__(self, root: Path, digest: str, owner: dict):
        if not re.fullmatch('[0-9a-f]{64}', digest):
            raise ValueError('missing materialized product source digest')
        root.mkdir(parents=True, exist_ok=True)
        descriptor = os.open(root.parent, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)
        self.path = root / digest
        try:
            self.path.mkdir()
        except FileExistsError as exc:
            raise ProductReplayError('this materialized product has a prior execution intent; original completed/unknown evidence is preserved and never resampled') from exc
        # mkdir itself reserves the identity; a crash before the complete
        # owner record still leaves a fail-closed reservation.
        write_once(self.path / 'owner.json', {'schema': 'codex-product-attempt/v1',
            'product_source_digest': digest, **owner})

    def checkpoint(self, case: str, phase: str, value: dict) -> None:
        if not re.fullmatch(r'[a-z0-9_]+', case) or not re.fullmatch(r'[a-z0-9_]+', phase):
            raise ValueError('invalid checkpoint identity')
        write_once(self.path / case / (phase + '.json'), value)
