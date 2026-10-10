"""Expose only verified public dependencies and the task's pinned toolchain."""
import hashlib
import os
from pathlib import Path
import subprocess

RUNTIME = Path('@@AGENTSWE_ENVS@@/openclaw-channel-handoff-ledger-edit-v1')
TARGET = '/opt/agentswe-openclaw'

def prepare(run, public, worktree, *, runtime=RUNTIME):
    baseline = runtime / 'baseline/worktree'
    locks = {}
    for name in ('package.json', 'pnpm-lock.yaml', 'pnpm-workspace.yaml'):
        expected = hashlib.sha256((public / 'input/repository' / name).read_bytes()).hexdigest()
        if hashlib.sha256((baseline / name).read_bytes()).hexdigest() != expected:
            raise RuntimeError('Prewarmed dependencies do not match public ' + name)
        locks[name] = expected
    manifests = {}
    for folder, dirs, files in os.walk(public / 'input/repository'):
        dirs[:] = [name for name in dirs if name not in {'.git', 'node_modules'}]
        if 'package.json' not in files:
            continue
        relative = (Path(folder) / 'package.json').relative_to(public / 'input/repository')
        digest = hashlib.sha256((Path(folder) / 'package.json').read_bytes()).hexdigest()
        if hashlib.sha256((baseline / relative).read_bytes()).hexdigest() != digest:
            raise RuntimeError('Prewarmed workspace manifest differs: ' + str(relative))
        manifests[str(relative)] = digest
    node = runtime / 'node-v24.15.0-linux-x64'
    pnpm = runtime / 'lib/node_modules/pnpm'
    versions = [subprocess.check_output([str(node / 'bin/node'), '--version'], text=True).strip(),
        subprocess.check_output([str(node / 'bin/node'), str(pnpm / 'bin/pnpm.mjs'), '--version'], text=True).strip()]
    if versions != ['v24.15.0', '11.15.1']:
        raise RuntimeError('Pinned OpenClaw toolchain is unavailable')
    tools = run / 'builder_task/environment/toolchain-bin'
    tools.mkdir(parents=True, exist_ok=True)
    for name, target in {'node': TARGET+'/node/bin/node', 'npm': TARGET+'/node/bin/npm',
                         'npx': TARGET+'/node/bin/npx'}.items():
        (tools / name).symlink_to(target)
    (tools / 'pnpm').write_text('#!/bin/sh\nexport PATH='+TARGET+'/bin:$PATH\nexec '+TARGET+'/node/bin/node '+TARGET+'/pnpm/bin/pnpm.mjs --config.verify-deps-before-run=false "$@"\n')
    (tools / 'pnpm').chmod(0o755)
    mounts = [{'type':'bind','source':str(source),'target':target,'read_only':True}
        for source,target in [(node,TARGET+'/node'),(pnpm,TARGET+'/pnpm'),(tools,TARGET+'/bin')]]
    modules = []
    for folder, dirs, _ in os.walk(baseline):
        dirs[:] = [name for name in dirs if name not in {'.git','dist','.artifacts','node_modules'}]
        dependencies = Path(folder) / 'node_modules'
        if not dependencies.is_dir():
            continue
        relative = dependencies.relative_to(baseline)
        # No baseline source, verification directory, store, or credential mount.
        target = '/workspace/worktree/' + relative.as_posix()
        mounts.append({'type':'bind','source':str(dependencies),'target':target,'read_only':True})
        modules.append(relative.as_posix())
    if 'node_modules' not in modules:
        raise RuntimeError('Prewarmed root dependencies are unavailable')
    check = run / 'builder_task/environment/builder_dependency_check.py'
    check.write_text('''import json,subprocess\nfrom pathlib import Path\nprefix="/opt/agentswe-openclaw/bin/"\nversions=[subprocess.check_output([prefix+"node","--version"],text=True).strip(),subprocess.check_output([prefix+"pnpm","--version"],text=True).strip()]\nvitest=subprocess.check_output([prefix+"pnpm","exec","vitest","--version"],cwd="/workspace/worktree",text=True,timeout=15).strip()\nvalid=versions==["v24.15.0","11.15.1"] and vitest.startswith("vitest/") and Path("/workspace/worktree/node_modules/.bin/vitest").is_file()\nPath("/logs/agent/builder_dependency_gate.json").write_text(json.dumps({"valid":valid,"versions":versions,"offline_dependencies_present":True})+"\\n")\nraise SystemExit(0 if valid else 125)\n''')
    mounts.append({'type':'bind','source':str(check),'target':TARGET+'/dependency_check.py','read_only':True})
    return mounts, {'public_lock_sha256':locks,'toolchain_versions':versions,
        'workspace_manifest_sha256':manifests,'pnpm_automatic_install_disabled':True,'dependency_mounts':modules,'private_baseline_source_mounted':False,'dependency_network_required':False}
