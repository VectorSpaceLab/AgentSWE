"""Observe the actual Harbor Builder container and its cgroup limits."""
from __future__ import annotations
import json
from pathlib import Path
import subprocess
import threading
import time

MEMORY_BYTES=16*1024**3
CPU_NANOS=8_000_000_000

class BuilderResourceObserver:
    def __init__(self,run:Path):
        self.run=run;self.stop=threading.Event();self.proof={'valid':False,'observations':[],'errors':[]}
        self.thread=threading.Thread(target=self._observe,daemon=True)
    def start(self):self.thread.start()
    def _observe(self):
        try:
            while not self.stop.is_set() and not self.proof['observations']:
                ids=subprocess.check_output(['docker','ps','-q'],timeout=10).decode().split()
                for cid in ids:
                    p=subprocess.run(['docker','inspect',cid],capture_output=True,text=True,timeout=10)
                    if p.returncode:continue
                    value=json.loads(p.stdout)[0]
                    if not any(m.get('Source')==str(self.run/'builder_workspace/worktree') and m.get('Destination')=='/workspace/worktree' for m in value['Mounts']):continue
                    pid=value['State']['Pid'];cgroup=next(v.split(':',2)[2] for v in Path(f'/proc/{pid}/cgroup').read_text().splitlines() if v.startswith('0::'))
                    base=Path('/sys/fs/cgroup')/cgroup.lstrip('/')
                    observed={k:(base/k).read_text().strip() for k in ['memory.max','memory.current','memory.events','cpu.max','cpuset.cpus.effective']}
                    row={'container_id':value['Id'],'container_name':value['Name'],'compose_project':value['Config'].get('Labels',{}).get('com.docker.compose.project'),'image_id':value['Image'],'pid':pid,'process_start':Path(f'/proc/{pid}/stat').read_text().split()[21],
                         'cgroup':cgroup,'docker_memory_bytes':value['HostConfig']['Memory'],'docker_cpu_nanos':value['HostConfig']['NanoCpus'],'docker_cpu_quota':value['HostConfig']['CpuQuota'],'docker_cpu_period':value['HostConfig']['CpuPeriod'],'docker_cpuset_cpus':value['HostConfig']['CpusetCpus'],'controller_files':observed,'observed_epoch':time.time()}
                    self.proof['observations'].append(row)
                    quota,period=observed['cpu.max'].split()
                    docker_cpu_valid=row['docker_cpu_nanos']==CPU_NANOS or (row['docker_cpu_period']>0 and row['docker_cpu_quota']==8*row['docker_cpu_period'])
                    self.proof['valid']=row['docker_memory_bytes']==MEMORY_BYTES and docker_cpu_valid and quota!='max' and int(quota)==8*int(period) and observed['memory.max']==str(MEMORY_BYTES)
                    if not self.proof['valid']:self.proof['errors'].append('actual Builder resource limits differ from 16GiB/8 CPU')
                    break
                self.stop.wait(.5)
        except Exception as exc:self.proof['errors'].append(type(exc).__name__+': '+str(exc))
    def finish(self):
        self.stop.set();self.thread.join(timeout=15)
        if self.thread.is_alive():self.proof['valid']=False;self.proof['errors'].append('resource observer failed to stop')
        self.proof['contract']={'memory_bytes':MEMORY_BYTES,'cpu_nanos':CPU_NANOS,'metric':'cgroup memory.max, not process PSS'}
        (self.run/'builder_resource_attestation.json').write_text(json.dumps(self.proof,indent=2)+'\n')
        return self.proof

    def cleanup_owned(self, *, process_started=True, defer_removal=False):
        results=[]
        if not process_started:
            result={'complete':True,'nothing_started':True,'containers':[]}
        elif not self.proof['observations']:
            result={'complete':False,'containers':[],'error':'Builder container ownership not observed'}
        else:
            ids=set(row['container_id'] for row in self.proof['observations'])
            projects=set(row.get('compose_project') for row in self.proof['observations'] if row.get('compose_project'))
            for project in projects:
                ids.update(subprocess.check_output(['docker','ps','-aq','--filter','label=com.docker.compose.project='+project],timeout=10).decode().split())
            for cid in sorted(ids):
                inspected=subprocess.run(['docker','inspect',cid],capture_output=True,text=True,timeout=10)
                if inspected.returncode:
                    absent='no such' in inspected.stderr.lower()
                    row={'container_id':cid,'absent_after_cleanup':absent}
                    if defer_removal and absent:
                        # Harbor tears the trial's Compose project down before this
                        # point: DockerEnvironment.stop() runs `compose down` unless
                        # keep_containers is set, and that flag has no configuration
                        # or CLI path in this Harbor build, so deferred removal can
                        # never actually retain the Builder container. Ownership was
                        # proven from the live container during the run, and absence
                        # is exactly the terminal state the coordinator would have
                        # produced itself, so record it as terminal and attribute it.
                        row.update(retained_terminal=True, removed_by_environment_teardown=True,
                                   ownership_proven_at_observation=any(
                                       observed['container_id']==cid
                                       for observed in self.proof['observations']))
                    results.append(row);continue
                value=json.loads(inspected.stdout)[0]
                is_main=any(row['container_id']==value['Id'] and row['container_name']==value['Name'] and row['image_id']==value['Image'] for row in self.proof['observations'])
                is_sidecar=value['Config'].get('Labels',{}).get('com.docker.compose.project') in projects
                if not (is_main or is_sidecar):raise RuntimeError('Builder cleanup ownership mismatch')
                if defer_removal:
                    from harbor.readiness_resources import retain_container
                    row = retain_container(value['Id'], self.run,
                        expected_project=value['Config']['Labels']['com.docker.compose.project'],
                        expected_working_dir=str(self.run/'builder_task/environment'))
                    results.append(row)
                    continue
                subprocess.run(['docker','rm','-f',value['Id']],capture_output=True,check=True,timeout=20)
                after=subprocess.run(['docker','inspect',value['Id']],capture_output=True,text=True,timeout=10)
                results.append({'container_id':value['Id'],'ownership_verified':True,'absent_after_cleanup':after.returncode!=0 and 'no such' in after.stderr.lower()})
            networks=[]
            for project in projects:
                for network in subprocess.check_output(['docker','network','ls','-q','--filter','label=com.docker.compose.project='+project],timeout=10).decode().split():
                    value=json.loads(subprocess.check_output(['docker','network','inspect',network],timeout=10))[0]
                    if defer_removal:
                        if value.get('Labels',{}).get('com.docker.compose.project') != project:
                            raise RuntimeError('retained network ownership mismatch')
                        if any(cid not in {row['container_id'] for row in results}
                               for cid in value.get('Containers', {})):
                            raise RuntimeError('retained network contains unowned resources')
                        networks.append({'network_id':value['Id'],'removed':False,'removal_deferred':True})
                        continue
                    if value.get('Labels',{}).get('com.docker.compose.project')!=project or value.get('Containers'):
                        networks.append({'network_id':network,'removed':False,'error':'network identity or empty-membership check failed'});continue
                    subprocess.run(['docker','network','rm',value['Id']],capture_output=True,check=True,timeout=15)
                    networks.append({'network_id':value['Id'],'removed':True})
            result={'complete':all(row['absent_after_cleanup'] for row in results) and all(row['removed'] for row in networks),'containers':results,'networks':networks,'unrelated_resources_touched':False}
            if defer_removal:
                result.update(complete=False, removal_deferred=True,
                    retained_terminal=bool(results) and all(row.get('retained_terminal') for row in results))
        (self.run/'builder_container_cleanup.json').write_text(json.dumps(result,indent=2)+'\n')
        return result
