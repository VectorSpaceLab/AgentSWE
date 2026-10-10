#!/usr/bin/env python3
"""Validate an evaluator-owned independent eight-axis Code score contract."""
from __future__ import annotations
import argparse,json
from pathlib import Path

WEIGHTS={"interface_lifecycle":15,"requirement_mechanism_coverage":20,"analysis_evidence_integrity":15,"safety_privacy_side_effects":15,"recovery_honest_failure":10,"testability_observability":10,"maintainability_generalization":10,"resource_discipline":5}

def main()->int:
    p=argparse.ArgumentParser(); p.add_argument("--contract",type=Path,required=True); p.add_argument("--output",type=Path,required=True); a=p.parse_args(); value=json.loads(a.contract.read_text(encoding="utf-8")); dims=value.get("dimensions",{}) if isinstance(value,dict) else {}; errors=[]
    if set(dims)!=set(WEIGHTS): errors.append("dimension set differs from fixed rubric")
    total=0; scores={}
    for key,weight in WEIGHTS.items():
        item=dims.get(key,{}); score=item.get("score") if isinstance(item,dict) else None
        if not isinstance(score,int) or not 0<=score<=weight: errors.append(f"{key}: score must be integer 0..{weight}"); score=0
        scores[key]={"weight":weight,"score":score}; total+=score
    result={"schema_version":"agentswe-code-rubric-result-v1","contract_valid":not errors,"code_score_publishable":not errors,"dimensions":scores,"total":total,"errors":errors}; a.output.parent.mkdir(parents=True,exist_ok=True); a.output.write_text(json.dumps(result,indent=2)+"\n"); print(json.dumps(result,indent=2)); return 0 if not errors else 2

if __name__=="__main__": raise SystemExit(main())
