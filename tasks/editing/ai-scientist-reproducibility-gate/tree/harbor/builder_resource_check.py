"""Harbor pre-agent healthcheck; reads the actual container cgroup."""
import json,os,time
from pathlib import Path

def inspect_resources(base=Path('/sys/fs/cgroup')):
    values={name:(base/name).read_text().strip() for name in ['cpu.max','memory.max','cpuset.cpus.effective']}
    quota,period=values['cpu.max'].split()
    valid=quota!='max' and int(period)>0 and int(quota)==8*int(period) and values['memory.max']==str(16*1024**3)
    return {'valid':valid,'controller_files':values,'contract':{'cpus':8,'memory_bytes':16*1024**3},'pid':os.getpid(),'monotonic':time.monotonic(),'epoch':time.time(),'affinity':sorted(os.sched_getaffinity(0))}

if __name__=='__main__':
    try: proof=inspect_resources()
    except Exception as exc: proof={'valid':False,'error':type(exc).__name__+': '+str(exc)}
    data=json.dumps(proof,sort_keys=True)+'\n'
    Path('/logs/agent/builder_resource_gate.json').write_text(data)
    print(data,end='')
    raise SystemExit(0 if proof['valid'] else 125)
