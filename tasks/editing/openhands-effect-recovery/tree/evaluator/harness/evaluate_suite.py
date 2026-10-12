#!/usr/bin/env python3
"""Native diagnostic suite; never a formal Agent-loop Result publisher."""
from __future__ import annotations
import argparse, json, shutil, time
from pathlib import Path
from common import (
    CASE_IDS, HarnessError, aggregate_case_results, apply_patch_once, changed_paths, ensure_npm,
    extract_pristine, inject_public_contract_probe, prepare_dependencies, prepare_generated_sources, redact,
    PROCESS_TREE_MEMORY_LIMIT_BYTES, run_case, run_focused_baseline_vitest_gate, run_typecheck_gate,
    validate_reports,
)

def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--repository", type=Path, required=True)
    parser.add_argument("--cases-root", type=Path, required=True)
    parser.add_argument("--submission", type=Path, required=True)
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--claim-formal-result", action="store_true")
    args = parser.parse_args()
    if args.claim_formal_result:
        parser.error("evaluate_suite.py is native diagnostic evidence only and cannot publish formal Result")
    repository, cases_root, submission, output = map(Path.resolve, (args.repository, args.cases_root, args.submission, args.output_dir))
    shutil.rmtree(output, ignore_errors=True); output.mkdir(parents=True)
    worktree = output / "worktree"; started = time.monotonic()
    summary = {"valid": False, "validity": {}, "commands": [], "delivery_reports": {}, "cases": {}, "score": {}, "errors": [], "native_diagnostic_only": True, "formal_result_publishable": False}
    try:
        if any(not (cases_root / case / "input.md").is_file() for case in CASE_IDS):
            raise HarnessError("one or more hidden case inputs are missing")
        patch = submission / "solution.patch"
        paths = changed_paths(patch)
        summary["validity"]["changed_paths"] = paths
        summary["delivery_reports"] = validate_reports(submission, paths)
        summary["commands"] += extract_pristine(repository, worktree)
        summary["commands"] += apply_patch_once(worktree, patch)
        npm, npm_log = ensure_npm(); summary["commands"] += npm_log
        dependency_log = prepare_dependencies(worktree, npm); summary["commands"] += dependency_log
        if not dependency_log or dependency_log[-1]["exit_code"] != 0:
            raise HarnessError("pinned npm ci failed after three attempts in the isolated OpenHands cache")

        generated_log = prepare_generated_sources(worktree, npm); summary["commands"] += generated_log
        if not generated_log or generated_log[-1]["exit_code"] != 0:
            raise HarnessError("repository generated-source preparation failed")

        inject_public_contract_probe(worktree)
        typecheck = run_typecheck_gate(worktree)
        summary["commands"].append(typecheck.evidence())
        if typecheck.exit_code: raise HarnessError("focused TypeScript check failed")

        baseline = run_focused_baseline_vitest_gate(worktree)
        summary["commands"].append(baseline)
        if baseline["exit_code"]: raise HarnessError("focused baseline Vitest compatibility checks failed")

        summary["valid"] = True
        for case in CASE_IDS:
            try:
                summary["cases"][case] = run_case(worktree, case)
            except Exception as exc:
                summary["cases"][case] = {
                    "case_id": case,
                    "case_status": "evaluator_error",
                    "error": {"type": type(exc).__name__, "message": redact(str(exc))},
                }
        try:
            summary["score"] = aggregate_case_results(summary["cases"])
        except HarnessError as exc:
            summary["valid"] = False
            summary["errors"].append({
                "type": "EvaluatorCaseError",
                "message": redact(str(exc)),
            })
            summary["score"] = {"aggregation_valid": False, "reason": "evaluator case error"}
    except Exception as exc:
        summary["valid"] = False
        summary["errors"].append({"type": type(exc).__name__, "message": redact(str(exc))})
        summary["score"] = {"mean_case_score": 0, "zero_rule": "validity gate"}
    summary["duration_seconds"] = round(time.monotonic() - started, 3)
    setup_peaks = [float(item.get("peak_tree_memory_mb", 0)) for item in summary["commands"]]
    case_peaks = [
        float(result.get("command", {}).get("peak_tree_memory_mb", 0))
        for result in summary["cases"].values()
    ]
    summary["peak_tree_memory_mb"] = max([*setup_peaks, *case_peaks], default=0)
    summary["memory_limit_mb"] = PROCESS_TREE_MEMORY_LIMIT_BYTES // 1024 // 1024
    result = output / "run_summary.json"; result.write_text(json.dumps(summary, indent=2) + "\n")
    print(json.dumps(summary, indent=2)); return 0 if summary["valid"] else 1
if __name__ == "__main__": raise SystemExit(main())
