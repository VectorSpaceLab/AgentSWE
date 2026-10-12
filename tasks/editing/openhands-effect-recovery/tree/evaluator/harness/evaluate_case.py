#!/usr/bin/env python3
"""Standalone case runner; suite mode is authoritative for aggregate scoring."""
from __future__ import annotations
import argparse, json, shutil, time
from pathlib import Path
from common import (
    HarnessError, apply_patch_once, changed_paths, ensure_npm, extract_pristine,
    inject_public_contract_probe, prepare_dependencies, prepare_generated_sources, redact, run_case,
    PROCESS_TREE_MEMORY_LIMIT_BYTES, run_focused_baseline_vitest_gate, run_typecheck_gate, validate_reports,
)

def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--case-dir", type=Path, required=True)
    parser.add_argument("--repository", type=Path, required=True)
    parser.add_argument("--submission", type=Path, required=True)
    parser.add_argument("--output-dir", type=Path, required=True)
    args = parser.parse_args(); case_id = args.case_dir.resolve().name
    output = args.output_dir.resolve(); shutil.rmtree(output, ignore_errors=True); output.mkdir(parents=True)
    summary = {"valid": False, "case": None, "commands": [], "errors": []}; started = time.monotonic()
    try:
        if not (args.case_dir.resolve() / "input.md").is_file(): raise HarnessError("case input is missing")
        patch = args.submission.resolve() / "solution.patch"; paths = changed_paths(patch)
        validate_reports(args.submission.resolve(), paths)
        worktree = output / "worktree"; summary["commands"] += extract_pristine(args.repository.resolve(), worktree)
        summary["commands"] += apply_patch_once(worktree, patch)
        npm, log = ensure_npm(); summary["commands"] += log
        dependency_log = prepare_dependencies(worktree, npm); summary["commands"] += dependency_log
        if not dependency_log or dependency_log[-1]["exit_code"] != 0: raise HarnessError("isolated npm preparation failed")
        generated_log = prepare_generated_sources(worktree, npm); summary["commands"] += generated_log
        if not generated_log or generated_log[-1]["exit_code"] != 0: raise HarnessError("repository generated-source preparation failed")
        inject_public_contract_probe(worktree)
        typecheck = run_typecheck_gate(worktree); summary["commands"].append(typecheck.evidence())
        if typecheck.exit_code: raise HarnessError("focused TypeScript check failed")
        baseline = run_focused_baseline_vitest_gate(worktree); summary["commands"].append(baseline)
        if baseline["exit_code"]: raise HarnessError("focused baseline Vitest compatibility checks failed")
        summary["case"] = run_case(worktree, case_id); summary["valid"] = True
    except Exception as exc: summary["errors"].append({"type": type(exc).__name__, "message": redact(str(exc))})
    summary["duration_seconds"] = round(time.monotonic() - started, 3)
    setup_peaks = [float(item.get("peak_tree_memory_mb", 0)) for item in summary["commands"]]
    case_peak = float((summary.get("case") or {}).get("command", {}).get("peak_tree_memory_mb", 0))
    summary["peak_tree_memory_mb"] = max([*setup_peaks, case_peak], default=0)
    summary["memory_limit_mb"] = PROCESS_TREE_MEMORY_LIMIT_BYTES // 1024 // 1024
    (output / "result.json").write_text(json.dumps(summary, indent=2) + "\n"); print(json.dumps(summary, indent=2))
    return 0 if summary["valid"] else 1
if __name__ == "__main__": raise SystemExit(main())
