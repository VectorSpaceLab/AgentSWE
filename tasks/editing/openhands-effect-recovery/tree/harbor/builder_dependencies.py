"""Verified offline OpenHands tools and dependencies for the public Builder."""
import hashlib
import json
from pathlib import Path
import shutil
import subprocess

RUNTIME = Path('@@AGENTSWE_ENVS@@/openhands-effect-recovery-ledger-edit-v1')
TARGET = '/opt/agentswe-openhands'


def prepare(run, public, worktree, *, runtime=RUNTIME):
    baseline=runtime/'baseline-install';npm=runtime/'npm-tooling/node_modules/npm'
    locks={}
    for name in ('package.json','package-lock.json'):
        expected=hashlib.sha256((public/'input/repository'/name).read_bytes()).hexdigest()
        if hashlib.sha256((baseline/name).read_bytes()).hexdigest()!=expected:
            raise RuntimeError('Prewarmed OpenHands dependencies differ from public '+name)
        locks[name]=expected
    marker=baseline/'.benchmark-lock-sha256'
    if marker.read_text().strip()!=locks['package-lock.json'] or not (baseline/'node_modules').is_dir():
        raise RuntimeError('Exact-lock prewarmed dependencies are unavailable')
    version=subprocess.check_output(['node',str(npm/'bin/npm-cli.js'),'--version'],text=True,timeout=10).strip()
    if version!='10.5.0':raise RuntimeError('Pinned npm 10.5.0 is unavailable')
    bundle=run/'builder_public_runtime';bundle.mkdir()
    supplied=bundle/'baseline-install';supplied.mkdir()
    for name in ('package.json','package-lock.json','.benchmark-lock-sha256'):
        shutil.copyfile(baseline/name,supplied/name)
    # Only the npm package and dependencies are mounted from prewarm; no source.
    (supplied/'node_modules').mkdir()
    (bundle/'npm-tooling/node_modules/npm').mkdir(parents=True)
    (bundle/'npm-cache').mkdir()
    tools=run/'builder_task/environment/toolchain-bin';tools.mkdir()
    wrapper=tools/'npm';wrapper.write_text('#!/bin/sh\nexec node '+TARGET+'/env/npm-tooling/node_modules/npm/bin/npm-cli.js "$@"\n');wrapper.chmod(0o755)
    for name in ('npm-cache',): (run/'builder_workspace'/name).mkdir()
    target=worktree/'node_modules'
    if target.exists():raise RuntimeError('Builder worktree already contains unverified dependencies')
    target.mkdir()
    copied=subprocess.run(['cp','-a','--reflink=auto',str(baseline/'node_modules')+'/.',str(target)],capture_output=True,text=True,timeout=600)
    if copied.returncode:raise RuntimeError('Unable to copy verified dependencies into writable Builder worktree')
    check=run/'builder_task/environment/builder_dependency_check.py'
    check.write_text('''import hashlib,json,os,re,subprocess\nfrom pathlib import Path\nroot=Path('/opt/agentswe-openhands/env');work=Path('/workspace/worktree')\nnode=subprocess.check_output(['node','--version'],text=True).strip()\nmatch=re.fullmatch(r'v(\\d+)\\.(\\d+)\\.(\\d+)',node)\nnpm=subprocess.check_output(['/opt/agentswe-openhands/bin/npm','--version'],text=True).strip()\nvitest=subprocess.check_output([str(work/'node_modules/.bin/vitest'),'--version'],cwd=work,text=True,timeout=15).strip()\nsha=lambda p:hashlib.sha256(p.read_bytes()).hexdigest()\nlock=sha(work/'package-lock.json');base=root/'baseline-install'\nvalid=bool(match and tuple(map(int,match.groups()))>=(22,12,0) and npm=='10.5.0' and vitest.startswith('vitest/') and sha(base/'package-lock.json')==lock== (base/'.benchmark-lock-sha256').read_text().strip() and sha(work/'package.json')==sha(base/'package.json'))\nPath('/logs/agent/builder_dependency_gate.json').write_text(json.dumps({'valid':valid,'node_version':node,'npm_version':npm,'vitest':vitest,'public_lock_sha256':lock,'offline_dependencies_present':True})+'\\n')\nraise SystemExit(0 if valid else 125)\n''')
    mounts=[{'type':'bind','source':str(src),'target':dst,'read_only':readonly} for src,dst,readonly in [
        (bundle,TARGET+'/env',True),(baseline/'node_modules',TARGET+'/env/baseline-install/node_modules',True),
        (npm,TARGET+'/env/npm-tooling/node_modules/npm',True),(tools,TARGET+'/bin',True),
        (run/'builder_workspace/npm-cache',TARGET+'/env/npm-cache',False),
        (check,TARGET+'/dependency_check.py',True)]]
    return mounts, {'public_lock_sha256':locks,'npm_version':version,'writable_worktree_dependency_copy':True,
        'prewarm_source_mounted':False,'external_dependency_network_required':False,
        'public_environment_target':TARGET+'/env'}
