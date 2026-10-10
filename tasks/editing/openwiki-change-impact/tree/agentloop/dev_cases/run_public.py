#!/usr/bin/env python3
"""Future public lower-agent entry; requires an evaluator broker in pilot phase."""
from __future__ import annotations
import argparse, json
from pathlib import Path
from ..evaluator.dynamic_case_service import issue
from ..evaluator.fixture_service import runtime_case_paths
from ..evaluator.lower_agent_launcher import run_case

def main() -> int:
    parser = argparse.ArgumentParser(); parser.add_argument("--candidate-repository", type=Path, required=True); parser.add_argument("--case-id", choices=("dev_001", "dev_002"), required=True); parser.add_argument("--broker-endpoint", required=True); parser.add_argument("--run-dir", type=Path, required=True); parser.add_argument("--timeout", type=int, default=600); args = parser.parse_args(); root = Path(__file__).resolve().parents[2]; case_root = root / "dev_cases"; templates = json.loads((root / "agentloop/evaluator/case_templates.json").read_text()); task_path = case_root / args.case_id / "input.md"; task = task_path.read_text(encoding="utf-8") if task_path.is_file() else None; issued = issue(args.case_id, templates[args.case_id], args.run_dir / "evaluator", args.run_dir / "candidate", task); runtime = runtime_case_paths(args.case_id, case_root, args.run_dir / "fixture"); result = run_case(args.candidate_repository.resolve(), Path(issued["candidate_request"]), args.run_dir / "lower", args.broker_endpoint, timeout=args.timeout, working_directory=Path(runtime["repository"]),fixture_case_id=args.case_id,fixture_cases_root=case_root,fixture_output=args.run_dir/"fixture"); print(json.dumps({"issued": issued, "runtime": {"repository": str(runtime["repository"]), "manifest": str(runtime["manifest"])}, "result": result}, indent=2, ensure_ascii=False)); return 0 if result.get("valid") else 1

if __name__ == "__main__": raise SystemExit(main())
