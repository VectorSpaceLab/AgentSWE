#!/usr/bin/env python3
"""Create the physically trimmed Builder-visible package."""
from __future__ import annotations
import argparse, json, shutil
from pathlib import Path

def main()->int:
    p=argparse.ArgumentParser(); p.add_argument("--benchmark",type=Path,default=Path(__file__).resolve().parents[1]); p.add_argument("--output",type=Path,required=True); a=p.parse_args()
    if a.output.exists(): shutil.rmtree(a.output)
    for name in ("input","dev_cases"):
        shutil.copytree(a.benchmark/name,a.output/name,symlinks=True)
    for transient in list(a.output.rglob("__pycache__"))+list(a.output.rglob("*.pyc")):
        if transient.is_dir(): shutil.rmtree(transient,ignore_errors=True)
        else: transient.unlink(missing_ok=True)
    manifest={"schema_version":"agentswe-public-package-v1","visible":["input","dev_cases"],"hidden_mounted":False,"evaluator_mounted":False,"credential_mounted":False}
    (a.output/"package_manifest.json").write_text(json.dumps(manifest,indent=2)+"\n")
    print(json.dumps(manifest,indent=2)); return 0
if __name__=="__main__": raise SystemExit(main())
