#!/usr/bin/env python3
from __future__ import annotations
import argparse, json
WEIGHTS={"interface_lifecycle":15,"requirement_mechanism_coverage":20,"analysis_evidence_integrity":15,"safety_privacy_side_effects":15,"recovery_honest_failure":10,"testability_observability":10,"maintainability_generalization":10,"resource_discipline":5}
def score(values:dict[str,int])->dict[str,object]:
    if set(values)!=set(WEIGHTS): raise ValueError("exactly eight Code dimensions required")
    if any(not isinstance(v,int) or v<0 or v>w for (k,v),w in zip(values.items(),WEIGHTS.values())): raise ValueError("dimension out of range")
    return {"dimensions":values,"total":sum(values.values()),"separate_from_result":True}
def main()->int:
    p=argparse.ArgumentParser(); p.add_argument("--scores",type=argparse.FileType("r"),required=True); p.add_argument("--output",type=argparse.FileType("w"),required=True); a=p.parse_args(); json.dump(score(json.load(a.scores)),a.output,indent=2); a.output.write("\n"); return 0
if __name__=="__main__": raise SystemExit(main())
