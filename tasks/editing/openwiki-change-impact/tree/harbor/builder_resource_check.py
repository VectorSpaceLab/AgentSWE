"""Harbor pre-agent healthcheck; reads the actual container cgroup."""
import json,os,shutil,subprocess,time
from pathlib import Path

def inspect_resources(base=Path('/sys/fs/cgroup')):
    values={name:(base/name).read_text().strip() for name in ['cpu.max','memory.max','cpuset.cpus.effective']}
    quota,period=values['cpu.max'].split()
    valid=quota!='max' and int(period)>0 and int(quota)==8*int(period) and values['memory.max']==str(16*1024**3)
    return {'valid':valid,'controller_files':values,'contract':{'cpus':8,'memory_bytes':16*1024**3},'pid':os.getpid(),'monotonic':time.monotonic(),'epoch':time.time(),'affinity':sorted(os.sched_getaffinity(0))}

def inspect_runtime():
    script='''const fs=require("fs");const root="/builder-dependencies/node_modules/.pnpm";
const package=fs.readdirSync(root).find(p=>p.startsWith("better-sqlite3@"));
const DB=require(root+"/"+package+"/node_modules/better-sqlite3");const db=new DB(":memory:");
db.exec("CREATE TABLE probe (value TEXT); INSERT INTO probe VALUES ('builder-runtime-ok')");
const value=db.prepare("SELECT value FROM probe").get().value;db.close();
console.log(JSON.stringify({node:process.version,value,closed:!db.open}));'''
    executable=shutil.which('node')
    proc=subprocess.run([executable or '/missing/node','-e',script],capture_output=True,text=True,timeout=5)
    try:value=json.loads(proc.stdout)
    except ValueError:value={}
    return {'valid':executable=='/builder-runtime/node/bin/node' and proc.returncode==0
        and value=={'node':'v22.12.0','value':'builder-runtime-ok','closed':True},
        'executable':executable,'result':value,'exit_code':proc.returncode,'stderr_tail':proc.stderr[-600:]}

if __name__=='__main__':
    try:
        proof=inspect_resources()
        if proof['valid']:
            proof['runtime']=inspect_runtime()
            proof['valid']=proof['runtime']['valid']
    except Exception as exc: proof={'valid':False,'error':type(exc).__name__+': '+str(exc)}
    data=json.dumps(proof,sort_keys=True)+'\n'
    Path('/logs/agent/builder_resource_gate.json').write_text(data)
    print(data,end='')
    raise SystemExit(0 if proof['valid'] else 125)
