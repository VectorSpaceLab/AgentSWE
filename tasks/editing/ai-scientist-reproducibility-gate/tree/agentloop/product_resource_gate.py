"""Release a short-lived product only after verifying its actual cgroup."""
from __future__ import annotations
import json
import os
from pathlib import Path
import re
import subprocess
import time

# The product container is created with --rm. Docker 29 removes an exited --rm container asynchronously: for
# several seconds inspect still shows it and `docker rm -f` answers "removal of container ... is already in
# progress". Cleanup waits for that removal before recording container_absent.
REMOVAL_WAIT_SECONDS = 30
REMOVAL_POLL_SECONDS = 0.5

GATE = '''import os,sys,time
from pathlib import Path
root=Path('/runtime-gate')
(root/'ready').write_text(str(os.getpid()))
end=time.monotonic()+20
while not (root/'permit').is_file():
    if time.monotonic()>=end: raise SystemExit(78)
    time.sleep(.01)
os.execvp(sys.argv[1],sys.argv[1:])
'''


def write(path,value):
    with path.open('x') as f:json.dump(value,f,indent=2);f.write('\n');f.flush();os.fsync(f.fileno())


def inspect(cid):
    result=subprocess.run(['docker','inspect',cid],capture_output=True,text=True,timeout=3)
    if result.returncode:return None
    value=json.loads(result.stdout)[0]
    if value.get('Id')!=cid:raise OSError('container identity mismatch')
    return value


def run_gated_product(command, *, output, parent, timeout, **kwargs):
    if not re.fullmatch(r'agentswe_ai_scientist_[0-9a-f]{32}\.slice',parent):
        raise OSError('unverified product aggregate parent')
    output=Path(output).resolve();output.mkdir(parents=True,exist_ok=False)
    bridge=output/'bridge';bridge.mkdir();(output/'gate.py').write_text(GATE)
    cidfile=output/'container.cid'
    index=command.index('python3')-1
    image=command[index];native=command[index+1:]
    before=command[:index]
    assert before[:2]==['docker','run'] and before[before.index('--cgroup-parent')+1]==parent
    creation=['docker','create',*before[2:], '--cidfile',str(cidfile),
        '-v',f'{bridge}:/runtime-gate:rw','-v',f'{output/"gate.py"}:/runtime-gate.py:ro',
        image,'python3','-I','-B','/runtime-gate.py',*native]
    began=time.monotonic();deadline=began+timeout
    process=None;cid=None;observation={'valid':False,'parent':parent,'timeout_seconds':timeout}
    try:
        created=subprocess.run(creation,capture_output=True,text=True,timeout=min(120,timeout))  # 116e: 10 s create budget was hit under host load
        if created.returncode:raise OSError('owned product container creation failed')
        cid=cidfile.read_text().strip()
        if not re.fullmatch('[0-9a-f]{64}',cid):raise OSError('invalid owned product CID')
        process=subprocess.Popen(['docker','start','-a',cid],cwd=kwargs.get('cwd'),env=kwargs.get('env'),
                                 text=True,stdout=subprocess.PIPE,stderr=subprocess.PIPE)
        while not (bridge/'ready').is_file():
            if process.poll() is not None:raise OSError('product exited before cgroup verification')
            if time.monotonic()>=min(deadline,began+15):raise OSError('product cgroup handshake deadline')
            time.sleep(.02)
        actual=inspect(cid)
        pid=actual['State']['Pid']
        cgroup=next(line.split(':',2)[2] for line in Path(f'/proc/{pid}/cgroup').read_text().splitlines() if line.startswith('0::'))
        properties=actual['HostConfig']
        if properties['CgroupParent']!=parent or not cgroup.startswith('/'+parent+'/'):
            raise OSError('actual product process escaped aggregate parent')
        root=Path('/sys/fs/cgroup')/cgroup.lstrip('/')
        aggregate=Path('/sys/fs/cgroup')/parent
        observed={name:(root/name).read_text().strip() for name in ('memory.max','memory.swap.max','cgroup.events')}
        parent_observed={name:(aggregate/name).read_text().strip() for name in ('memory.max','memory.swap.max','memory.events')}
        if observed['memory.max']!='4294967296' or parent_observed['memory.max']!='4294967296' or parent_observed['memory.swap.max']!='0':
            raise OSError('actual product memory contract mismatch')
        if properties['NetworkMode']!='none' or properties['Privileged'] or 'ALL' not in properties['CapDrop']:
            raise OSError('actual product isolation mismatch')
        observation.update(valid=True,cid=cid,host_pid=pid,actual_cgroup=cgroup,child=observed,
            aggregate_before=parent_observed,configured_cgroup_parent=properties['CgroupParent'],
            network_mode=properties['NetworkMode'],cap_drop=properties['CapDrop'],image=actual['Image'])
        write(output/'verified_before_product.json',observation)
        (bridge/'permit').write_text('verified')
        stdout,stderr=process.communicate(timeout=max(.001,deadline-time.monotonic()))
        after=(aggregate/'memory.events').read_text().strip()
        observation['aggregate_memory_events_after']=after
        def counts(text):return dict(line.split() for line in text.splitlines())
        if int(counts(after).get('oom',0))>int(counts(parent_observed['memory.events']).get('oom',0)):
            raise OSError('aggregate_memory_oom_observed')
        return subprocess.CompletedProcess(command,process.returncode,stdout,stderr)
    finally:
        if cid:
            actual=inspect(cid)
            if actual is not None:
                if actual['HostConfig']['CgroupParent']!=parent:raise OSError('refuse cleanup of foreign container')
                removed=subprocess.run(['docker','rm','-f',cid],capture_output=True,text=True,timeout=5)
                if removed.returncode:
                    observation['remove_stderr']=removed.stderr[-500:]
                if removed.returncode and 'already in progress' in (removed.stdout+removed.stderr).lower():
                    observation['removal_in_progress_at_rm']=True
                    wait_until=time.monotonic()+REMOVAL_WAIT_SECONDS
                    while inspect(cid) is not None and time.monotonic()<wait_until:time.sleep(REMOVAL_POLL_SECONDS)
            observation['container_absent']=inspect(cid) is None
        if process is not None and process.poll() is None:
            process.kill();process.communicate(timeout=3)
        observation['elapsed_seconds']=round(time.monotonic()-began,3)
        write(output/'resource_cleanup.json',observation)
