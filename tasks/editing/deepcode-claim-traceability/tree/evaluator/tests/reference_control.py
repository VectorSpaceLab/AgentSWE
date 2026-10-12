from __future__ import annotations

TRACEABILITY_MODULE = r'''from __future__ import annotations
import argparse,hashlib,json,shutil,subprocess,sys
from pathlib import Path

CASES={
 "normalizer.py":{"ids":["EQ-1","ALG-1","CLM-1"],"mappings":["src/normalizer.py","config.json","tests/test_normalizer.py","artifacts/result.json"],"provenance":["paper_fact","code","config","test","artifact"],"status":"complete","artifact":"result.json","command":["python","run_experiment.py","--config","config.json","--input","data/input.json","--output","artifacts/result.json","--seed","0"]},
 "selector.py":{"ids":["ALG-2","GAP-2","CLM-2"],"mappings":["src/selector.py","tests/test_selector.py","artifacts/selection.json"],"provenance":["paper_fact","implementation_assumption","external_evidence","code","test","artifact"],"status":"partial","artifact":"selection.json","command":["python","run_experiment.py","--input","data/candidates.json","--output","artifacts/selection.json","--seed","11"]},
 "decay.py":{"ids":["EQ-B1","ALG-B1","CLM-B1"],"mappings":["src/decay.py","config.json","tests/test_decay.py","artifacts/decay.json"],"provenance":["paper_fact","code","config","test","artifact"],"status":"complete","artifact":"decay.json","command":["python","run_experiment.py","--input","data/times.json","--config","config.json","--output","artifacts/decay.json","--seed","3"]},
 "sampler.py":{"ids":["ALG-S2","GAP-N2","CLM-S2"],"mappings":["src/sampler.py","tests/test_sampler.py","artifacts/samples.json"],"provenance":["paper_fact","implementation_assumption","code","test","artifact"],"status":"partial","artifact":"samples.json","command":["python","run_experiment.py","--input","data/scores.json","--output","artifacts/samples.json","--draws","200","--seeds","7","19","31"]},
 "update.py":{"ids":["HP-LR","EQ-C3","CLM-C3"],"mappings":["src/update.py","config.json","tests/test_update.py","artifacts/update.json"],"provenance":["paper_fact","implementation_assumption","code","config","artifact"],"status":"partial","artifact":"update.json","command":["python","run_experiment.py","--config","config.json","--output","artifacts/update.json","--seed","5"]},
 "pipeline.py":{"ids":["DATA-SPLIT","PREP-ORDER","CLM-D4"],"mappings":["src/pipeline.py","config.json","tests/test_pipeline.py","data/values.csv","artifacts/preprocessing.json"],"provenance":["paper_fact","code","config","test","artifact"],"status":"complete","artifact":"preprocessing.json","command":["python","run_experiment.py","--data","data/values.csv","--config","config.json","--output","artifacts/preprocessing.json","--seed","13"]},
 "model.py":{"ids":["ALG-E5","ABL-GATE","CLM-A5"],"mappings":["src/encoder.py","src/gate.py","src/model.py","configs/full.json","configs/without_gate.json","tests/test_model.py","artifacts/ablation.json"],"provenance":["paper_fact","code","config","test","artifact"],"status":"complete","artifact":"ablation.json","command":["python","run_experiment.py","--input","data/values.json","--full","configs/full.json","--ablation","configs/without_gate.json","--output","artifacts/ablation.json","--seed","23"]},
 "score.py":{"ids":["EQ-K6","CITE-K6","GAP-K6","CLM-K6"],"mappings":["src/score.py","tests/test_score.py"],"provenance":["paper_fact","code","test"],"status":"blocked","artifact":None,"command":None},
}
def dump(path,value):Path(path).write_text(json.dumps(value,indent=2,sort_keys=True)+"\n",encoding="utf-8")
def main():
 ap=argparse.ArgumentParser();ap.add_argument("--request",required=True);ap.add_argument("--output",required=True);a=ap.parse_args();request=Path(a.request).resolve();project=request.parents[1];out=Path(a.output)
 key=next((name for name in CASES if (project/"src"/name).is_file()),None)
 if key is None:raise SystemExit(1)
 spec=CASES[key];tmp=out.with_name(out.name+".tmp-reference")
 if tmp.exists():shutil.rmtree(tmp)
 tmp.mkdir(parents=True);shutil.copytree(project,tmp/"payload",ignore=shutil.ignore_patterns(".deepcode","artifacts","__pycache__"))
 command=spec["command"]
 if command:
  actual=list(command);actual[0]=sys.executable;r=subprocess.run(actual,cwd=tmp/"payload",capture_output=True,text=True)
  if r.returncode:raise SystemExit(1)
 nodes=[{"id":item,"type":"semantic","provenance":"paper_fact","mappings":spec["mappings"]} for item in spec["ids"]]
 edges=[{"source":spec["ids"][i],"target":spec["ids"][i+1],"relation":"supports"} for i in range(len(spec["ids"])-1)]
 dump(tmp/"paper_spec.json",{"schema_version":"1.0","status":spec["status"],"claims":spec["ids"]})
 dump(tmp/"traceability_graph.json",{"schema_version":"1.0","nodes":nodes,"edges":edges,"mappings":spec["mappings"],"provenance":spec["provenance"]})
 dump(tmp/"reproduction_manifest.json",{"schema_version":"1.0","status":spec["status"],"commands":([{"argv":command}] if command else []),"expected_outputs":(["artifacts/"+spec["artifact"]] if spec["artifact"] else []),"blocked_reason":("missing SYN-CITE-17 definition" if spec["status"]=="blocked" else None)})
 dump(tmp/"assumptions.json",{"schema_version":"1.0","items":([{"kind":"implementation_assumption","detail":"documented local choice"}] if spec["status"]=="partial" else [])})
 dump(tmp/"deviations.json",{"schema_version":"1.0","items":[]})
 dump(tmp/"data_manifest.json",{"schema_version":"1.0","files":sorted(p.relative_to(tmp/"payload").as_posix() for p in (tmp/"payload").rglob("*") if p.is_file())})
 (tmp/"environment.lock").write_text("python>=3.12\n",encoding="utf-8")
 replay="""import hashlib,json,subprocess,sys\nfrom pathlib import Path\nr=Path(__file__).parent\nc=json.loads((r/"checksums.json").read_text())["files"]\nfor name,expected in c.items():\n p=r/name\n if not p.is_file() or hashlib.sha256(p.read_bytes()).hexdigest()!=expected: print(json.dumps({"status":"checksum_failed","path":name}));raise SystemExit(3)\nm=json.loads((r/"reproduction_manifest.json").read_text())\nif m["status"]=="blocked": print(json.dumps({"status":"blocked","reason":m.get("blocked_reason")}));raise SystemExit(2)\nfor item in m["commands"]:\n argv=list(item["argv"]);argv[0]=sys.executable\n q=subprocess.run(argv,cwd=r/"payload")\n if q.returncode:raise SystemExit(q.returncode)\nprint(json.dumps({"status":m["status"],"commands":len(m["commands"])}))\n"""
 (tmp/"replay.py").write_text(replay,encoding="utf-8")
 files={}
 for p in sorted(tmp.rglob("*")):
  if p.is_file() and p.name!="checksums.json":files[p.relative_to(tmp).as_posix()]=hashlib.sha256(p.read_bytes()).hexdigest()
 dump(tmp/"checksums.json",{"schema_version":"1.0","files":files})
 if out.exists():shutil.rmtree(out)
 tmp.replace(out);raise SystemExit(0 if spec["status"]=="complete" else 2)
if __name__=="__main__":main()
'''

REVISION_MODULE = r'''from __future__ import annotations
import argparse,copy,fcntl,hashlib,json,os,shutil
from pathlib import Path

FILES=("paper_spec.json","traceability_graph.json","reproduction_manifest.json","assumptions.json","deviations.json","data_manifest.json","checksums.json","environment.lock","replay.py")
def load(p): return json.loads(Path(p).read_text())
def dump(p,v): Path(p).write_text(json.dumps(v,sort_keys=True,indent=2)+"\n")
def root(op): return Path(op).resolve().parents[2]
def lock(store):
    Path(store).mkdir(parents=True,exist_ok=True); f=open(Path(store)/".lock","a+"); fcntl.flock(f,fcntl.LOCK_EX); return f
def digest(path):
    h=hashlib.sha256()
    for p in sorted(path.rglob("*")):
        if p.is_file() and p.name not in {"revision.json"}:
            h.update(p.relative_to(path).as_posix().encode()); h.update(b"\0"); h.update(p.read_bytes()); h.update(b"\0")
    return h.hexdigest()
def state_path(store): return Path(store)/"state.json"
def get_state(store):
    p=state_path(store); return load(p) if p.exists() else {"schema_version":"1.0","head_revision_id":None,"revisions":{},"operations":{},"audit":[],"audit_head":"0"*64}
def save(store,s): dump(state_path(store),s)
def response(op,accepted,record=None,error=None):
    out={k:op.get(k) for k in ("schema_version","operation_id","action","tenant_id","project_id")}; out["accepted"]=accepted
    if record is not None: out["record"]=record
    if error is not None: out["error"]=error
    return out
def conflict(s,op):
    old=s["operations"].get(op.get("operation_id")); body={k:v for k,v in op.items() if k!="operation_id"}
    if old is None: return None
    if old["body"]!=body: return response(op,False,error={"code":"OPERATION_CONFLICT","message":"operation id body differs"})
    return old["response"]
def policy(op):
    p=root(op)/".deepcode"/"traceability_policy.json"; return load(p) if p.exists() else {"policy_version":0,"roles":{},"required_review_roles":[]}
def fail(op,code,msg): return response(op,False,error={"code":code,"message":msg})
def record_operation(s,op,out): s["operations"][op["operation_id"]]={"body":{k:v for k,v in op.items() if k!="operation_id"},"response":out}
def audit_event(s,op,kind,payload):
    body={"sequence":len(s.setdefault("audit",[]))+1,"kind":kind,"operation_id":op.get("operation_id"),"tenant_id":op.get("tenant_id"),"project_id":op.get("project_id"),"payload":payload,"previous_hash":s.get("audit_head","0"*64)}
    body["hash"]=hashlib.sha256(json.dumps(body,sort_keys=True,separators=(",",":")).encode()).hexdigest(); s["audit"].append(body); s["audit_head"]=body["hash"]
def audit_valid(s):
    previous="0"*64
    for index,item in enumerate(s.get("audit",[]),1):
        if not isinstance(item,dict) or item.get("sequence")!=index or item.get("previous_hash")!=previous or not isinstance(item.get("hash"),str): return False
        body={k:item.get(k) for k in ("sequence","kind","operation_id","tenant_id","project_id","payload","previous_hash")}
        if hashlib.sha256(json.dumps(body,sort_keys=True,separators=(",",":")).encode()).hexdigest()!=item["hash"]: return False
        previous=item["hash"]
    return s.get("audit_head","0"*64)==previous
def semantic(path):
    g=load(path/"traceability_graph.json"); nodes={n.get("id"):n for n in g.get("nodes",[]) if isinstance(n,dict) and isinstance(n.get("id"),str)}
    edges={json.dumps({k:e.get(k) for k in ("source","target","relation")},sort_keys=True):e for e in g.get("edges",[]) if isinstance(e,dict)}
    mappings={}
    for node in nodes.values():
        for key in ("claim_id","claim","mapping","code_path","path"):
            if key in node: mappings[f"{node.get('id')}:{key}"]=node.get(key)
    return {"nodes":nodes,"edges":edges,"mappings":mappings}
def diff(a,b):
    out={}
    for key in ("nodes","edges","mappings"):
        av,bv=a[key],b[key]; out[key]={"added":sorted(set(bv)-set(av)),"removed":sorted(set(av)-set(bv)),"changed":sorted(k for k in set(av)&set(bv) if av[k]!=bv[k])}
    return out
def authorized(pol,actor,role): return actor in pol.get("roles",{}).get(role,[]) and role in pol.get("required_review_roles",pol.get("roles",{}))
def main():
    ap=argparse.ArgumentParser(); ap.add_argument("--store",required=True); ap.add_argument("--operation",required=True); a=ap.parse_args(); op=load(a.operation); store=Path(a.store); f=lock(store)
    try:
      s=get_state(store); old=conflict(s,op)
      if old is not None: print(json.dumps(old,sort_keys=True)); return 0 if old.get("accepted") else 2
      action=op.get("action"); pol=policy(a.operation)
      if op.get("schema_version")!="1.0" or not isinstance(op.get("operation_id"),str): out=fail(op,"INVALID_OPERATION","schema or operation id invalid")
      elif action=="register":
        cap=(Path(a.operation).resolve().parent/op.get("capsule_path","")).resolve(); project=root(a.operation).resolve()
        if not cap.is_dir() or not cap.is_relative_to(project) or any(not (cap/x).is_file() for x in FILES) or not (cap/"payload").is_dir(): out=fail(op,"INVALID_CAPSULE","safe complete capsule required")
        elif op.get("expected_head_revision_id")!=s.get("head_revision_id"): out=fail(op,"HEAD_CONFLICT","head changed")
        else:
          d=digest(cap); key=str(op.get("revision_key")); existing=next((r for r in s["revisions"].values() if r["tenant_id"]==op.get("tenant_id") and r["project_id"]==op.get("project_id") and r["revision_key"]==key),None)
          if existing and existing["revision_digest"]!=d: out=fail(op,"REVISION_KEY_CONFLICT","key payload differs")
          elif existing: out=response(op,True,existing)
          else:
            rid="rev-"+d[:20]; dest=store/"revisions"/rid; dest.parent.mkdir(parents=True,exist_ok=True); shutil.copytree(cap,dest)
            parent=op.get("parent_revision_id"); parent_sem=s["revisions"].get(parent,{}).get("semantic") if parent else {"nodes":{},"edges":{},"mappings":{}}
            cur_sem=semantic(dest); capsule_status=load(dest/"reproduction_manifest.json").get("status"); rec={"revision_id":rid,"revision_digest":d,"revision_key":key,"tenant_id":op.get("tenant_id"),"project_id":op.get("project_id"),"parent_revision_id":parent,"review_generation":0,"policy_version":pol.get("policy_version",0),"decisions":{},"semantic":cur_sem,"diff":diff(parent_sem,cur_sem),"integrity":"valid","capsule_status":capsule_status,"promotion":None,"quarantine":{"status":"active","generation":0}}
            s["revisions"][rid]=rec; audit_event(s,op,"revision.register",{"revision_id":rid,"revision_digest":d,"revision_key":key}); out=response(op,True,rec)
      elif action=="audit":
        auditor=op.get("actor_id"); allowed=auditor in pol.get("roles",{}).get("auditor",[]) or auditor in pol.get("roles",{}).get("security",[])
        if not allowed: out=fail(op,"FORBIDDEN","auditor role required")
        elif not audit_valid(s): out=fail(op,"AUDIT_CORRUPT","audit chain is invalid")
        else:
          events=[item for item in s.get("audit",[]) if item.get("tenant_id")==op.get("tenant_id") and item.get("project_id")==op.get("project_id")]
          since=op.get("since_sequence")
          if isinstance(since,int): events=[item for item in events if item.get("sequence",0)>since]
          out=response(op,True,{"events":events,"head_hash":s.get("audit_head","0"*64),"chain_valid":True,"policy_version":pol.get("policy_version",0)})
      elif action in {"quarantine","restore"}:
        rid=op.get("revision_id"); rec=s["revisions"].get(rid); security=op.get("actor_id") in pol.get("roles",{}).get("security",[])
        if not rec or rec.get("tenant_id")!=op.get("tenant_id") or rec.get("project_id")!=op.get("project_id"): out=fail(op,"NOT_FOUND","object unavailable")
        elif not security: out=fail(op,"FORBIDDEN","security role required")
        elif op.get("revision_digest")!=rec.get("revision_digest"): out=fail(op,"DIGEST_STALE","digest changed")
        elif action=="quarantine":
          q=rec.get("quarantine")
          if isinstance(q,dict) and q.get("status")=="quarantined": out=response(op,True,rec)
          else:
            generation=int(rec.get("quarantine_generation",0))+1; rec["quarantine_generation"]=generation; rec["quarantine"]={"status":"quarantined","generation":generation,"reason":str(op.get("reason","security review"))[:500],"actor_id":op.get("actor_id"),"policy_version":pol.get("policy_version",0)}; audit_event(s,op,"revision.quarantine",{"revision_id":rid,"generation":generation}); out=response(op,True,rec)
        elif op.get("expected_quarantine_generation")!=rec.get("quarantine_generation"): out=fail(op,"QUARANTINE_GENERATION_STALE","quarantine boundary changed")
        elif rec.get("integrity")!="valid": out=fail(op,"INTEGRITY_INVALID","revision is not intact")
        else:
          rec["quarantine"]={"status":"active","generation":rec.get("quarantine_generation",0),"restored_by":op.get("actor_id"),"policy_version":pol.get("policy_version",0)}; audit_event(s,op,"revision.restore",{"revision_id":rid,"generation":rec.get("quarantine_generation",0)}); out=response(op,True,rec)
      elif action in {"compare","inspect","review","promote"}:
        rid=op.get("revision_id") or op.get("target_revision_id"); rec=s["revisions"].get(rid)
        if not rec or rec["tenant_id"]!=op.get("tenant_id") or rec["project_id"]!=op.get("project_id"): out=fail(op,"NOT_FOUND","object unavailable")
        elif action=="compare":
          base=s["revisions"].get(op.get("base_revision_id")); out=fail(op,"NOT_FOUND","base unavailable") if not base or base["tenant_id"]!=op.get("tenant_id") else response(op,True,{"base_revision_id":base["revision_id"],"target_revision_id":rec["revision_id"],"diff":diff(base["semantic"],rec["semantic"])})
        elif action=="inspect":
          current=policy(a.operation); view=copy.deepcopy(rec); view.pop("semantic",None); view["policy_evaluation"]={"policy_version":current.get("policy_version"),"required_roles":current.get("required_review_roles",[]),"approved_roles":sorted(rec.get("decisions",{}))}; out=response(op,True,view)
        elif action=="review":
          role=op.get("role"); current=policy(a.operation)
          if op.get("revision_digest")!=rec["revision_digest"]: out=fail(op,"DIGEST_STALE","digest changed")
          elif not authorized(current,op.get("actor_id"),role): out=fail(op,"FORBIDDEN","actor is not assigned to role")
          elif op.get("expected_review_generation")!=rec["review_generation"]: out=fail(op,"REVIEW_GENERATION_STALE","review changed")
          elif op.get("decision") not in {"approve","reject"}: out=fail(op,"INVALID_DECISION","decision invalid")
          else:
            old_dec=rec["decisions"].get(role); rec["decisions"][role]={"decision":op["decision"],"actor_id":op["actor_id"],"note":str(op.get("note",""))[:500]}
            if old_dec!=rec["decisions"][role]: rec["review_generation"]+=1
            rec["policy_version"]=current.get("policy_version",0); audit_event(s,op,"revision.review",{"revision_id":rid,"role":role,"decision":op["decision"],"review_generation":rec["review_generation"]}); out=response(op,True,rec)
        else:
          current=policy(a.operation); required=current.get("required_review_roles",[])
          execution_path=(Path(a.operation).resolve().parent/op.get("execution_store","")).resolve(); execution_state=load(execution_path/"state.json") if (execution_path/"state.json").exists() else {"plans":{}}
          plan=execution_state.get("plans",{}).get(op.get("plan_id"))
          approved=all(rec.get("decisions",{}).get(role,{}).get("decision")=="approve" for role in required)
          good=isinstance(plan,dict) and rec.get("quarantine",{}).get("status")=="active" and approved and rec["integrity"]=="valid" and rec.get("capsule_status")!="blocked" and rec["policy_version"]==current.get("policy_version") and op.get("revision_digest")==rec["revision_digest"] and op.get("expected_review_generation")==rec["review_generation"] and plan.get("tenant_id")==op.get("tenant_id") and plan.get("project_id")==op.get("project_id") and plan.get("revision_id")==rid and plan.get("revision_digest")==rec["revision_digest"] and plan.get("review_generation")==rec["review_generation"] and plan.get("generation")==op.get("plan_generation") and plan.get("status")=="complete" and isinstance(plan.get("attestation"),dict) and plan["attestation"].get("revision_digest")==rec["revision_digest"]
          if not good or op.get("expected_head_revision_id")!=s.get("head_revision_id"): out=fail(op,"PROMOTION_PRECONDITION","review, execution, policy, or head precondition failed")
          else:
            rec["promotion"]={"revision_id":rid,"review_generation":rec["review_generation"],"plan_id":plan.get("plan_id")}; s["head_revision_id"]=rid; audit_event(s,op,"revision.promote",{"revision_id":rid,"plan_id":plan.get("plan_id"),"review_generation":rec["review_generation"]}); out=response(op,True,{"head_revision_id":rid,"promotion":rec["promotion"]})
      elif action=="reconcile":
        affected=[]
        for rid,rec in s["revisions"].items():
          d=store/"revisions"/rid
          if rec["integrity"]=="valid" and (not d.is_dir() or digest(d)!=rec["revision_digest"]): rec["integrity"]="corrupt"; affected.append(rid)
        if s.get("head_revision_id") in affected: s["head_revision_id"]=None
        audit_event(s,op,"revision.reconcile",{"affected_revision_ids":affected}); out=response(op,True,{"affected_revision_ids":affected,"head_revision_id":s.get("head_revision_id"),"counts":{"corrupt":len(affected)}})
      else: out=fail(op,"UNKNOWN_ACTION","unsupported action")
      record_operation(s,op,out); save(store,s); print(json.dumps(out,sort_keys=True)); return 0 if out.get("accepted") else 2
    finally: fcntl.flock(f,fcntl.LOCK_UN); f.close()
if __name__=="__main__": raise SystemExit(main())
'''

EXECUTION_MODULE = r'''from __future__ import annotations
import argparse,fcntl,hashlib,json,os,subprocess,secrets
from pathlib import Path
def load(p): return json.loads(Path(p).read_text())
def dump(p,v): Path(p).write_text(json.dumps(v,sort_keys=True,indent=2)+"\n")
def root(op): return Path(op).resolve().parents[2]
def lock(store): Path(store).mkdir(parents=True,exist_ok=True); f=open(Path(store)/".lock","a+");fcntl.flock(f,fcntl.LOCK_EX);return f
def response(op,accepted,record=None,error=None):
 o={k:op.get(k) for k in ("schema_version","operation_id","action","tenant_id","project_id")}; o["accepted"]=accepted
 if record is not None:o["record"]=record
 if error is not None:o["error"]=error
 return o
def fail(op,c,m):return response(op,False,error={"code":c,"message":m})
def state(store):
 p=Path(store)/"state.json"; return load(p) if p.exists() else {"schema_version":"1.0","plans":{},"operations":{}}
def save(store,s):dump(Path(store)/"state.json",s)
def rev_state(path):
 p=Path(path)/"state.json"; return load(p) if p.exists() else {"revisions":{}}
def main():
 ap=argparse.ArgumentParser();ap.add_argument("--store",required=True);ap.add_argument("--revision-store",required=True);ap.add_argument("--operation",required=True);a=ap.parse_args();op=load(a.operation);store=Path(a.store);f=lock(store)
 try:
  s=state(store); old=s["operations"].get(op.get("operation_id")); body={k:v for k,v in op.items() if k!="operation_id"}
  if old:
   if old["body"]!=body:out=fail(op,"OPERATION_CONFLICT","operation id body differs");print(json.dumps(out));return 2
   print(json.dumps(old["response"]));return 0 if old["response"].get("accepted") else 2
  rs=rev_state(a.revision_store); action=op.get("action"); rid=op.get("revision_id"); rec=rs.get("revisions",{}).get(rid)
  if action=="start":
   if not rec or rec.get("tenant_id")!=op.get("tenant_id") or rec.get("project_id")!=op.get("project_id") or rec.get("revision_digest")!=op.get("revision_digest") or rec.get("review_generation")!=op.get("expected_review_generation"):out=fail(op,"REVISION_BINDING","revision binding invalid")
   elif rec.get("quarantine",{}).get("status")!="active":out=fail(op,"REVISION_QUARANTINED","revision is quarantined")
   elif op.get("actor_id") not in load(root(a.operation)/".deepcode"/"traceability_policy.json").get("roles",{}).get("operator",[]):out=fail(op,"FORBIDDEN","operator role required")
   elif any(p.get("tenant_id")==op.get("tenant_id") and p.get("project_id")==op.get("project_id") and p.get("plan_key")==op.get("plan_key") for p in s["plans"].values()):out=fail(op,"PLAN_KEY_CONFLICT","plan key already exists")
   else:
    rid2="plan-"+hashlib.sha256((op.get("tenant_id","")+op.get("project_id","")+op.get("plan_key","")+rec["revision_digest"]).encode()).hexdigest()[:18]; snap=Path(a.revision_store)/"revisions"/rid; manifest=load(snap/"reproduction_manifest.json"); raw_commands=manifest.get("commands") or []; commands=[item.get("argv") if isinstance(item,dict) else item for item in raw_commands]; p={"plan_id":rid2,"plan_key":op.get("plan_key"),"tenant_id":op.get("tenant_id"),"project_id":op.get("project_id"),"revision_id":rid,"revision_digest":rec["revision_digest"],"review_generation":rec["review_generation"],"commands":commands,"generation":1,"claim_token":secrets.token_urlsafe(18),"status":"running" if commands else "complete","checkpoints":[],"attestation":None}
    if not commands:p["attestation"]={"revision_digest":rec["revision_digest"],"review_generation":rec["review_generation"],"generation":1}
    s["plans"][rid2]=p;out=response(op,True,p)
  elif action in {"advance","pause","resume","cancel","status"}:
   plan=s["plans"].get(op.get("plan_id"));
   if not plan or plan.get("tenant_id")!=op.get("tenant_id") or plan.get("project_id")!=op.get("project_id"):out=fail(op,"NOT_FOUND","plan unavailable")
   elif action=="status":out=response(op,True,plan)
   elif action=="pause":
    if op.get("generation")!=plan["generation"] or op.get("claim_token")!=plan["claim_token"]:out=fail(op,"STALE_GENERATION","claim is stale")
    else:plan["status"]="paused";plan["pause_reason"]=op.get("reason");out=response(op,True,plan)
   elif action=="resume":
    if op.get("expected_generation")!=plan["generation"] or plan["status"]!="paused":out=fail(op,"STALE_GENERATION","resume boundary is stale")
    else:plan["generation"]+=1;plan["claim_token"]=secrets.token_urlsafe(18);plan["status"]="running";out=response(op,True,plan)
   elif action=="cancel":
    if op.get("expected_generation")!=plan["generation"]:out=fail(op,"STALE_GENERATION","cancel boundary is stale")
    else:plan["generation"]+=1;plan["claim_token"]=secrets.token_urlsafe(18);plan["status"]="canceled";plan["cancel_reason"]=op.get("reason");out=response(op,True,plan)
   elif action=="advance":
    if plan["status"]!="running" or op.get("generation")!=plan["generation"] or op.get("claim_token")!=plan["claim_token"]:out=fail(op,"STALE_GENERATION","worker is stale")
    elif len(plan["checkpoints"])>=len(plan["commands"]):plan["status"]="complete";plan["attestation"]={"revision_digest":plan["revision_digest"],"review_generation":plan["review_generation"],"generation":plan["generation"]};out=response(op,True,plan)
    else:
      i=len(plan["checkpoints"]);cmd=plan["commands"][i];
      if not isinstance(cmd,list) or not cmd:out=fail(op,"INVALID_COMMAND","manifest command invalid")
      else:
       actual=list(cmd); actual[0]=os.environ.get("PYTHON",actual[0]);
       try:r=subprocess.run(actual,cwd=Path(a.revision_store)/"revisions"/plan["revision_id"] / "payload",capture_output=True,text=True,timeout=30)
       except Exception as exc:r=type("R",(),{"returncode":1,"stdout":"","stderr":str(exc)})()
       cp={"sequence":i+1,"command":cmd,"exit_code":r.returncode,"output_digest":hashlib.sha256((r.stdout+r.stderr).encode()).hexdigest()};plan["checkpoints"].append(cp)
       if r.returncode!=0:plan["status"]="complete";plan["attestation"]={"revision_digest":plan["revision_digest"],"review_generation":plan["review_generation"],"generation":plan["generation"]}
       elif i+1==len(plan["commands"]):plan["status"]="complete";plan["attestation"]={"revision_digest":plan["revision_digest"],"review_generation":plan["review_generation"],"generation":plan["generation"]}
       out=response(op,True,plan)
  elif action=="reconcile":
   affected=[]
   for pid,plan in s["plans"].items():
    rec=rs.get("revisions",{}).get(plan.get("revision_id")); invalid=not rec or rec.get("integrity")!="valid" or rec.get("revision_digest")!=plan.get("revision_digest") or rec.get("review_generation")!=plan.get("review_generation")
    if invalid and plan.get("status") not in {"invalid","invalidated"}:plan["status"]="invalidated";plan["attestation"]=None;affected.append(pid)
   out=response(op,True,{"affected_plan_ids":sorted(affected),"counts":{"invalidated":len(affected)}})
  else:out=fail(op,"UNKNOWN_ACTION","unsupported action")
  s["operations"][op["operation_id"]]={"body":body,"response":out};save(store,s);print(json.dumps(out,sort_keys=True));return 0 if out.get("accepted") else 2
 finally:fcntl.flock(f,fcntl.LOCK_UN);f.close()
if __name__=="__main__":raise SystemExit(main())
'''


def install_reference(repository):
    (repository / "workflows" / "traceability.py").write_text(TRACEABILITY_MODULE)
    (repository / "workflows" / "traceability_runs.py").write_text(
        "from pathlib import Path\n\n"
        "def verify_project_traceability(project: Path):\n"
        "    request = project / '.deepcode' / 'traceability_request.json'\n"
        "    return None if not request.is_file() else {'traceability': 'ordinary-compatible', 'status': 'complete'}\n"
    )
    (repository / "workflows" / "traceability_revisions.py").write_text(REVISION_MODULE)
    (repository / "workflows" / "traceability_execution.py").write_text(EXECUTION_MODULE)
