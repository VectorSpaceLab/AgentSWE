"""Durable evaluator-owned product reservations, independent of delivery reports."""
import os
import re
from pathlib import Path
try:
    from .stage_recovery import RecoveryError, immutable_json, read
except ImportError:
    from stage_recovery import RecoveryError, immutable_json, read

class ProductRegistry:
    def __init__(self, root: Path):
        root.mkdir(parents=True, exist_ok=True, mode=0o700)
        if root.is_symlink(): raise RecoveryError('product registry cannot be symlinked')
        self.root=root

    def directory(self, digest):
        if not isinstance(digest,str) or not re.fullmatch('[0-9a-f]{64}',digest):
            raise RecoveryError('invalid stable product identity')
        return self.root/digest

    def lookup(self, digest):
        directory=self.directory(digest)
        if not directory.exists() and not directory.is_symlink():return None
        try:
            if directory.is_symlink():raise RecoveryError('symlinked product reservation')
            value=read(directory/'owner.json')
            if not isinstance(value,dict) or value.get('product_source_digest')!=digest:
                raise RecoveryError('product reservation identity changed')
            return value
        except (RecoveryError,OSError,ValueError):
            return {'product_source_digest':digest,'state':'unknown_reservation'}

    def claim(self, digest, owner):
        directory=self.directory(digest)
        try:directory.mkdir(mode=0o700)
        except FileExistsError:raise RecoveryError('product already reserved; no second execution')
        # A crash even before the owner write remains a blocking reservation.
        for path in (self.root,):
            fd=os.open(path,os.O_RDONLY|os.O_DIRECTORY)
            try:os.fsync(fd)
            finally:os.close(fd)
        immutable_json(directory/'owner.json',owner)
        fd=os.open(directory,os.O_RDONLY|os.O_DIRECTORY)
        try:os.fsync(fd)
        finally:os.close(fd)
