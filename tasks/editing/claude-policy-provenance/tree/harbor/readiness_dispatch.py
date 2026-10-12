#!/usr/bin/env python3
"""Claude v2 readiness wrapper; binding validation happens before any live runner."""
from __future__ import annotations
import argparse, hashlib, json, os, subprocess, sys
from pathlib import Path
CONTROL=Path("@@AGENTSWE_EDITING_CONTROL@@")
STAGED_SOURCE=Path(__file__).resolve().parents[1]
PRODUCTION_SOURCE=Path("@@AGENTSWE_EDITING_TASKS@@/claude-policy-provenance/tree")
PROFILE="single-dev-two-round-hidden-smoke-v1"
def verify(binding_file: Path, binding_sha: str) -> dict:
    if binding_file.is_symlink() or not binding_file.is_file() or hashlib.sha256(binding_file.read_bytes()).hexdigest()!=binding_sha:
        raise RuntimeError("binding bytes changed before dispatch")
    sys.path.insert(0,str(CONTROL))
    from readiness_binding import verify_binding
    value=json.loads(binding_file.read_bytes())
    actual=verify_binding(PRODUCTION_SOURCE,value,control_root=CONTROL)
    if actual!=value: raise RuntimeError("binding changed during preflight")
    return actual
def main(argv=None):
    p=argparse.ArgumentParser()
    p.add_argument("--binding-file",type=Path,required=True);p.add_argument("--binding-sha256",required=True)
    p.add_argument("--run-dir",type=Path,required=True);p.add_argument("--hidden-cases-dir",type=Path,required=True)
    p.add_argument("--preflight",action="store_true")
    p.add_argument("--builder-proxy",default="http://127.0.0.1:7890")
    a=p.parse_args(argv); b=verify(a.binding_file,a.binding_sha256)
    result={"schema_version":"agentswe-claude-v2-launch-binding-v1","task":"claude","profile":PROFILE,"binding":b,"binding_file":str(a.binding_file),"binding_sha256":a.binding_sha256,"verified_under_lock":True,"provider_calls":0,"network_calls_before_exec":0,"registry_written":False,"gate_written":False,"run_dir":str(a.run_dir)}
    if a.preflight:
        print(json.dumps(result,indent=2,ensure_ascii=False)); return 0
    if a.run_dir.exists(): raise RuntimeError("fresh run directory already exists")
    os.environ["READINESS_SOURCE_OVERRIDE"] = str(PRODUCTION_SOURCE)
    cmd=[sys.executable,str(STAGED_SOURCE/"harbor/formal_one_stop.py"),"--pilot","--readiness-profile",PROFILE,"--readiness-binding-file",str(a.binding_file),"--readiness-binding-sha256",a.binding_sha256,"--run-dir",str(a.run_dir),"--hidden-cases-dir",str(a.hidden_cases_dir),"--max-dev-rounds","2","--n-concurrent","1","--builder-proxy",a.builder_proxy]
    os.execv(cmd[0],cmd)
if __name__=="__main__": raise SystemExit(main())
