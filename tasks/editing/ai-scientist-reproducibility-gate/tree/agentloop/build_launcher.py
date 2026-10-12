"""Verify the image-derived compiler inside the owned scope, then exec bwrap."""
from __future__ import annotations
import argparse,hashlib,json,os,stat,sys
from pathlib import Path

def verify(runtime, expected, image):
    runtime=Path(runtime).absolute()
    for path in (runtime,*runtime.parents):
        if path.is_symlink():raise ValueError('compiler runtime root has symlink component')
    manifest=runtime/'manifest.json'
    if not stat.S_ISREG(manifest.lstat().st_mode):raise ValueError('compiler manifest is not regular')
    raw=manifest.read_bytes()
    if hashlib.sha256(raw).hexdigest()!=expected:raise ValueError('compiler manifest identity mismatch')
    value=json.loads(raw)
    if value.get('image_id')!=image or value.get('probe_returncode')!=0 or not value.get('python_version','').startswith('3.11.16 '):
        raise ValueError('compiler image/version prerequisite mismatch')
    files=value.get('files')
    if not isinstance(files,dict) or not files:raise ValueError('compiler files missing')
    actual={}
    for directory,dirs,names in os.walk(runtime,followlinks=False):
        for name in dirs+names:
            path=Path(directory)/name
            mode=path.lstat().st_mode
            if stat.S_ISDIR(mode):continue
            if not stat.S_ISREG(mode):raise ValueError('compiler runtime contains link/special file')
            relative=path.relative_to(runtime).as_posix()
            if relative=='manifest.json':continue
            actual[relative]=hashlib.sha256(path.read_bytes()).hexdigest()
    if actual!=files:raise ValueError('compiler runtime byte inventory mismatch')

def main():
    p=argparse.ArgumentParser();p.add_argument('--runtime',required=True);p.add_argument('--manifest-sha256',required=True);p.add_argument('--image-id',required=True);p.add_argument('command',nargs=argparse.REMAINDER);a=p.parse_args()
    try:verify(a.runtime,a.manifest_sha256,a.image_id)
    except (OSError,ValueError,TypeError) as exc:
        print('compiler_prerequisite_failure: '+type(exc).__name__+': '+str(exc)[:400],file=sys.stderr)
        return 78
    command=a.command[1:] if a.command[:1]==['--'] else a.command
    os.execvpe(command[0],command,os.environ)

if __name__=='__main__':raise SystemExit(main())
