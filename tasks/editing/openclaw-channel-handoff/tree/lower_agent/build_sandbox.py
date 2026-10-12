"""Offline Candidate build mount/net/PID boundary, without model transports.

Only the disposable product and explicitly pinned read-only toolchain/store
are exposed. Resource ownership belongs to the surrounding build scope.
"""
from __future__ import annotations
import json
import os
from pathlib import Path
import signal
import subprocess
import time


def build_command(command, *, product, pinned, extra_environment=None, cache=None):
    product=Path(product).resolve()
    root=Path(str(pinned['root'])).resolve()
    node=Path(str(pinned['node'])).resolve(strict=True)
    store=root/'pnpm-store'
    if len(product.parts)<5 or not product.is_dir() or not node.is_relative_to(root):
        raise ValueError('invalid disposable build product or pinned toolchain')
    mounts=[root/'bin',node.parent.parent,root/'lib',store]
    if any(not path.is_dir() or not path.resolve().is_relative_to(root) for path in mounts):
        raise ValueError('missing or escaping pinned build dependency mount')
    if any(product==path or product.is_relative_to(path) or path.is_relative_to(product) for path in mounts):
        raise ValueError('build product and pinned mounts overlap')
    argv=['/usr/bin/bwrap','--unshare-pid','--unshare-ipc','--unshare-uts','--unshare-cgroup',
          '--unshare-net','--die-with-parent','--new-session','--cap-drop','ALL','--clearenv',
          '--ro-bind','/usr','/usr']
    for path in ('/bin','/lib','/lib64'):
        if Path(path).exists():argv+=['--ro-bind',path,path]
    argv+=['--proc','/proc','--dev','/dev','--tmpfs','/tmp','--dir','/etc']
    for path in ('/etc/hosts','/etc/nsswitch.conf','/etc/localtime','/etc/passwd','/etc/group'):
        if Path(path).exists():argv+=['--ro-bind',path,path]
    for path in mounts:argv+=['--ro-bind',str(path),str(path)]
    argv+=['--bind',str(product),str(product)]
    if cache is not None:
        cache=Path(cache).resolve(strict=True)
        if not cache.is_dir() or len(cache.parts)<5 or cache.is_relative_to(product) or product.is_relative_to(cache):
            raise ValueError('build metadata cache must be a disjoint private directory')
        if cache.is_relative_to(root) or root.is_relative_to(cache):
            raise ValueError('writable metadata cache cannot be the pinned shared cache')
        argv+=['--bind',str(cache),'/tmp/build-cache']
    environment={'PATH':str(root/'bin')+':/usr/bin:/bin','LANG':'C.UTF-8','LC_ALL':'C.UTF-8',
        'HOME':'/tmp/build-home','XDG_CACHE_HOME':'/tmp/build-cache','XDG_DATA_HOME':'/tmp/build-data',
        'XDG_STATE_HOME':'/tmp/build-state','TMPDIR':'/tmp','CI':'true',
        'npm_config_offline':'true','NO_PROXY':'localhost,127.0.0.1,::1',
        'RAYON_NUM_THREADS':'16','MALLOC_ARENA_MAX':'1'}
    # No arbitrary inherited settings, NODE_OPTIONS, preload or API variables.
    if extra_environment:
        if set(extra_environment)-{'OPENCLAW_BUILD_VERBOSE'}:
            raise ValueError('unapproved build environment setting')
        environment.update(extra_environment)
    for key,value in sorted(environment.items()):argv+=['--setenv',key,value]
    argv+=['--chdir',str(product),'--',*map(str,command)]
    return argv


def run_build_command(command, *, product, pinned, evidence, deadline, cache=None):
    evidence=Path(evidence).resolve()
    product=Path(product).resolve()
    if evidence.is_relative_to(product) or product.is_relative_to(evidence):
        raise ValueError('private build evidence cannot overlap the product')
    evidence.mkdir(parents=True,exist_ok=False)
    remaining=deadline-time.monotonic()
    if remaining<=0:raise TimeoutError('build budget exhausted before dispatch')
    argv=build_command(command,product=product,pinned=pinned,cache=cache)
    read_fd,write_fd=os.pipe()
    position=argv.index('--')
    argv[position:position]=['--info-fd',str(write_fd)]
    process=None
    started=time.monotonic()
    value={'command':list(map(str,command)),'namespace_verified':False,'dispatched':False,
           'timed_out':False,'network':'new namespace, no broker or other evaluator sockets',
           'credential_environment':'explicit clean environment, no model credentials',
           'readonly_store':str(Path(str(pinned['root']))/'pnpm-store'),
           'private_metadata_cache':str(cache) if cache is not None else None}
    try:
        with (evidence/'stdout.log').open('x') as out,(evidence/'stderr.log').open('x') as err:
            process=subprocess.Popen(argv,cwd=product,env={'PATH':'/usr/bin:/bin'},stdout=out,stderr=err,
                                     pass_fds=(write_fd,),start_new_session=True)
            os.close(write_fd);write_fd=-1
            value['dispatched']=True
            try:process.wait(timeout=max(.001,deadline-time.monotonic()))
            except subprocess.TimeoutExpired:
                value['timed_out']=True
                os.killpg(process.pid,signal.SIGKILL);process.wait(timeout=3)
        # Kernel/bwrap metadata arrives through an inherited dedicated fd,
        # not Candidate stdout. No subsequent process control uses this PID.
        raw=os.read(read_fd,65536)
        if raw:
            info=json.loads(raw)
            value['namespace']=info
            value['namespace_verified']=(info.get('net-namespace') not in (None,os.stat('/proc/self/ns/net').st_ino)
                and info.get('pid-namespace') not in (None,os.stat('/proc/self/ns/pid').st_ino))
        value['exit_code']=process.returncode
        value['wrapper_reaped']=process.poll() is not None
        value['elapsed_seconds']=round(time.monotonic()-started,3)
        value['valid']=value['namespace_verified'] and value['wrapper_reaped']
        return value
    finally:
        if process is not None and process.poll() is None:
            os.killpg(process.pid,signal.SIGKILL);process.wait(timeout=3)
        os.close(read_fd)
        if write_fd>=0:os.close(write_fd)
        (evidence/'command.json').write_text(json.dumps(value,indent=2)+'\n')
