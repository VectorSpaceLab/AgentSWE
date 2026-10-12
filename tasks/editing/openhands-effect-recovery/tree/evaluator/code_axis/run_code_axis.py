#!/usr/bin/env python3
"""Independent, deterministic Code-axis contract runner.

This smoke runner checks delivery evidence and emits a scoreable contract only
when a frozen candidate is supplied. It is deliberately separate from the
Agent-loop Result evaluator.
"""
from __future__ import annotations
import argparse, hashlib, json
from pathlib import Path

WEIGHTS = {"interface_lifecycle":15,"requirement_mechanism_coverage":20,"analysis_evidence_integrity":15,"safety_privacy_side_effects":15,"recovery_honest_failure":10,"testability_observability":10,"maintainability_generalization":10,"resource_discipline":5}

def main() -> int:
    ap=argparse.ArgumentParser(); ap.add_argument("--candidate",type=Path,required=True); ap.add_argument("--freeze-manifest",type=Path,required=True); ap.add_argument("--output",type=Path,required=True); args=ap.parse_args()
    candidate=args.candidate.resolve(); manifest=json.loads(args.freeze_manifest.read_text(encoding="utf-8")); errors=[]
    if manifest.get("schema_version") != "agentswe-freeze-manifest/v1": errors.append("bad freeze schema")
    source_submission = manifest.get("source_submission")
    accepted = manifest.get("accepted_submission_count", source_submission)
    if not isinstance(source_submission, int) or not 1 <= source_submission <= 10 or source_submission != accepted or not manifest.get("hidden_allowed"): errors.append("freeze is not the latest accepted Candidate")
    dims={key: None for key in WEIGHTS}
    if candidate.is_dir() and not errors:
        # No subjective code judgment is fabricated by the smoke runner.
        evidence_digest=hashlib.sha256(str(candidate).encode()).hexdigest()
        result={"schema_version":"agentswe-code-score-contract/v1","evaluation_state":"review_required","contract_valid":True,"raw_dimensions":dims,"weights":WEIGHTS,"raw_score":None,"result_axis":"separate","evidence_digest":evidence_digest,"note":"independent rubric loaded; human/approved judge scoring remains required"}
    else:
        result={"schema_version":"agentswe-code-score-contract/v1","evaluation_state":"infrastructure-invalid","contract_valid":False,"errors":errors or ["candidate missing"],"result_axis":"separate"}
    args.output.parent.mkdir(parents=True,exist_ok=True); args.output.write_text(json.dumps(result,indent=2)+"\n"); print(json.dumps(result,indent=2)); return 0 if result["contract_valid"] else 1

if __name__ == "__main__": raise SystemExit(main())
