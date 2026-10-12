"""Stable identity of all materialized product source, before evaluator build.

Only the evaluator-created root .git is omitted. Delivery reports are outside
this tree. Dependencies, generated build output and the original freeze digest
retain their existing contracts; this additional identity prevents resampling.
"""
from __future__ import annotations
import hashlib,json,os,stat
from pathlib import Path

def product_source_identity(root: Path) -> dict:
    if root.is_symlink():raise ValueError('product source root is a symlink')
    root=root.resolve()
    if not root.is_dir():raise ValueError('product source root is missing')
    git=root/'.git'
    if git.is_symlink() or (git.exists() and not git.is_dir()):
        raise ValueError('evaluator Git metadata is not a regular directory')
    rows=[]
    for directory,dirs,files in os.walk(root,followlinks=False):
        base=Path(directory)
        if base==root:dirs[:]=[name for name in dirs if name!='.git']
        for name in sorted(set(dirs+files)):
            path=base/name;relative=path.relative_to(root).as_posix()
            if path.is_symlink():
                target=os.readlink(path);resolved=(path.parent/target).resolve()
                if resolved!=root and root not in resolved.parents:raise ValueError('external product source symlink: '+relative)
                row={'path':relative,'kind':'symlink','target':target}
            elif path.is_file():
                row={'path':relative,'kind':'file','sha256':hashlib.sha256(path.read_bytes()).hexdigest(),
                     'executable_bits':stat.S_IMODE(path.stat().st_mode)&0o111}
            elif path.is_dir():row={'path':relative,'kind':'directory'}
            else:raise ValueError('unsupported product source entry: '+relative)
            rows.append(row)
    rows.sort(key=lambda row:row['path'])
    payload={'schema_version':'openwiki-product-source-identity/v1','excluded':['.git'],
             'exclusion_reason':'evaluator-created root Git metadata only','entries':rows}
    data=(json.dumps(payload,sort_keys=True,separators=(',',':'),ensure_ascii=False)+'\n').encode()
    return {**payload,'product_source_digest':hashlib.sha256(data).hexdigest()}
