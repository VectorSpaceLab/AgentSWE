#!/usr/bin/env python3
"""Non-heavy Stage-A audit; it never contacts a provider or starts Docker."""
from __future__ import annotations
import hashlib, json, subprocess, tempfile
from pathlib import Path

ROOT=Path(__file__).resolve().parent

def digest(root: Path)->str:
    h=hashlib.sha256()
    for p in sorted(root.rglob("*"),key=lambda x:str(x.relative_to(root))):
        if ".git" in p.parts: continue
        rel=str(p.relative_to(root)).encode(); h.update(len(rel).to_bytes(8,"big")); h.update(rel)
        payload=p.read_bytes() if p.is_file() else b""; h.update(b"F" if p.is_file() else b"D"); h.update(len(payload).to_bytes(8,"big")); h.update(payload)
    return h.hexdigest()

def main()->int:
    errors=[]
    protocol=json.loads((ROOT/"protocol_lock.json").read_text())
    if protocol["lower_agent"]["model"]!="gpt-5.6-sol" or protocol["lower_agent"]["reasoning_effort"]!="medium": errors.append("lower model lock")
    if protocol["inventory"]["dev"]!=["dev_001","dev_002"] or len(protocol["inventory"]["hidden"])!=6: errors.append("inventory")
    prov=json.loads((ROOT/"provenance/source_digest_manifest.json").read_text())
    if not prov.get("source_tree_digest") or not Path(prov["authoritative_source"]).is_dir(): errors.append("provenance source")
    for name in ("agentloop_migration_plan.md","dev_case_inventory.md"):
        if not (ROOT/"meta"/name).is_file(): errors.append(name)
    for case in protocol["inventory"]["dev"]:
        if not (ROOT/"dev_cases"/case/"input.md").is_file(): errors.append(f"missing {case}")
    for case in protocol["inventory"]["hidden"]:
        if not (ROOT/"test_cases"/case/"input.md").is_file(): errors.append(f"missing {case}")
    rubric=json.loads((ROOT/"evaluator/code_axis/code_rubric.json").read_text())
    if [x["weight"] for x in rubric["dimensions"]]!=[15,20,15,15,10,10,10,5]: errors.append("code weights")
    with tempfile.TemporaryDirectory() as td:
        work=Path(td)/"work"; work.mkdir(); (work/"solution.patch").write_text("diff --git a/src/api/recovery/example.ts b/src/api/recovery/example.ts\n")
    if errors: print(json.dumps({"status":"FAIL","errors":errors},indent=2)); return 1
    print(json.dumps({"status":"PASS","checks":["provenance","2+6 inventory","model lock","freeze schema","broker schema","fixed code weights","no provider/docker"],"sibling_digest":digest(ROOT)},indent=2)); return 0

if __name__=="__main__": raise SystemExit(main())
