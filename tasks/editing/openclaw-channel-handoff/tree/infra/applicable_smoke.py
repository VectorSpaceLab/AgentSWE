#!/usr/bin/env python3
"""Print the applicable language/build smoke plan; execute only with --execute."""
from __future__ import annotations
import argparse, json, subprocess
from pathlib import Path

ROOT=Path(__file__).resolve().parents[1]
COMMANDS=[
    ["python3","-m","py_compile","broker/responses_broker.py","adapters/materialize_candidate.py","lower_agent/launcher.py","controller/two_round_controller.py","evaluator/case_service.py","evaluator/hidden_executor.py","evaluator/public_runner.py","evaluator/test_agentloop_invariants.py","evaluator/code_score_runner.py","infra/self_test.py"],
    ["node","--check","lower_agent/launcher.mjs"],
    ["pnpm","install","--offline","--frozen-lockfile"],
    ["pnpm","build:strict-smoke"],
]
def main()->int:
    p=argparse.ArgumentParser(); p.add_argument("--execute",action="store_true"); p.add_argument("--output",type=Path,default=ROOT/"infra"/"applicable_smoke_plan.json"); a=p.parse_args(); records=[]
    for cmd in COMMANDS:
        item={"command":cmd,"executed":a.execute}
        if a.execute:
            r=subprocess.run(cmd,cwd=ROOT,text=True,capture_output=True,check=False,timeout=1800); item.update({"exit_code":r.returncode,"stdout_tail":r.stdout[-2000:],"stderr_tail":r.stderr[-2000:]})
        records.append(item)
    value={"schema_version":"openclaw-applicable-smoke-v1","status":"executed" if a.execute else "plan-only","commands":records,"warning":"Build/API/Docker smoke is intentionally not run during Stage A."}
    a.output.parent.mkdir(parents=True,exist_ok=True); a.output.write_text(json.dumps(value,indent=2)+"\n"); print(json.dumps(value,indent=2)); return 0
if __name__=="__main__": raise SystemExit(main())
