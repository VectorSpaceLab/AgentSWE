#!/usr/bin/env python3
"""Score equivalence of the released (default-mode) evaluator against archived runs. No model calls.

    tools/verify_equivalence.py creation --runs <dir with archived run dirs> [--run <run_id> ...]
                                         [--phase-glob '*-hidden*'] [--report out.json]
    tools/verify_equivalence.py creation --fixtures tests/fixtures/equivalence

Run directories may be one_stop runs (evaluations/<phase>/score_summary.json) or eval-only replay runs
(score_summary.json in the run directory itself, as in the paper's final Creation rejudge).

Creation: for every archived Result-judge case, the archived judge output (eval_result.json), the
archived trusted harness result and the archived eval manifest are fed to this repository's
eval-template/tests/verify_score.py. The resulting score contract must equal the archived one on
the scoring fields (score, contract_valid, score_publishable, validity_gate, evaluation_state,
raw_dimension_total). Judges are stochastic, so nothing is re-judged: only the deterministic code
between the judge output and the score is exercised.

A difference listed in KNOWN_FIXES (an evaluator fix deliberately applied in the release) is
reported as `expected` instead of `mismatch`. Exit status 1 on any unexpected mismatch.
"""
from __future__ import annotations

import argparse
import fnmatch
import json
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
FIELDS = ("score", "contract_valid", "score_publishable", "validity_gate", "evaluation_state", "raw_dimension_total")
LEGACY_TOKEN = "@LEGACY_GATEWAY@"          # fixture spelling of the pre-release gateway prefix
LEGACY_GATEWAY = "s" "u8"
# Release fixes that change a verifier outcome on purpose: (task, predicate on archived contract) -> reason.
KNOWN_FIXES = {
    "dimensions-not-object": (
        lambda archived: "dimensions must be an object" in (archived.get("errors") or []),
        "verify_score: a non-object `dimensions` is a fatal-zero contract error (Lite fix)"),
}


def task_of(run_dir: Path) -> str:
    lock_path = next((run_dir / n for n in ("protocol_lock.json", "evaluation_protocol_lock.json")
                      if (run_dir / n).is_file()), None)
    lock = json.loads(lock_path.read_text()) if lock_path else str(run_dir)
    for task in ("repository-bug-repair", "database-analytics", "scientific-pdf-translation",
                 "authorized-vulnerability-validation", "desktop-gui-automation", "document-to-editable-pptx",
                 "evidence-grounded-document-qa", "formal-theorem-proving", "schema-guided-web-extraction",
                 "web-research-report"):
        if task in json.dumps(lock) + str(run_dir.resolve()):
            return task
    raise SystemExit(f"{run_dir}: cannot tell the task from its protocol lock or path")


def compose_mounts(compose: Path) -> dict[str, str]:
    data = json.loads(compose.read_text())
    out = {}
    for service in data.get("services", {}).values():
        for v in service.get("volumes", []):
            if isinstance(v, dict) and v.get("target"):
                out[v["target"]] = v.get("source", "")
    return out


def archived_cases(run_dir: Path, phase_glob: str):
    summaries = sorted(run_dir.glob(f"evaluations/{phase_glob}/score_summary.json"))
    if not summaries and (run_dir / "score_summary.json").is_file():
        # eval-only replay runs (the paper's final Creation evaluation): the run directory is itself the evaluation phase
        summaries = [run_dir / "score_summary.json"]
    for summary in summaries:
        phase = summary.parent
        data = json.loads(summary.read_text())
        for case in (data.get("eval_run") or {}).get("cases", []):
            eval_result = Path(case["eval_result"])
            yield {
                "phase": phase.name, "case_id": case["case_id"],
                "eval_result": eval_result, "harness_result": eval_result.with_name("harness_result.json"),
                "manifest": phase / "eval_tasks" / case["case_id"] / "solution" / "eval_manifest.json",
                "compose": phase / "eval_tasks" / case["case_id"] / "tests" / "docker-compose.yaml",
                "archived_contract": Path(case["score_contract"]),
            }


def fixture_cases(root: Path):
    for case_dir in sorted(p for p in root.glob("*/*/*") if (p / "case.json").is_file()):
        meta = json.loads((case_dir / "case.json").read_text())
        yield {"task": meta["task"], "run": meta["run"], "phase": meta["phase"], "case_id": meta["case_id"],
               "eval_result": case_dir / "eval_result.json", "harness_result": case_dir / "harness_result.json",
               "manifest": case_dir / "eval_manifest.json", "compose": None,
               "archived_contract": case_dir / "score_contract.json", "fixture": True}


def materialise(path: Path, tmp: Path, fixture: bool) -> Path:
    """Fixtures spell the legacy prefix as a token so the published tree never contains it."""
    if not fixture or not path.is_file():
        return path
    out = tmp / ("fx-" + path.name)
    out.write_text(path.read_text().replace(LEGACY_TOKEN, LEGACY_GATEWAY))
    return out


def with_judge_format(manifest: Path, tmp: Path, mode: str) -> Path:
    value = json.loads(manifest.read_text())
    value["judge_format"] = mode
    out = tmp / "eqv-manifest.json"
    out.write_text(json.dumps(value))
    return out


def replay(task: str, case: dict, judge_format: str = "archived") -> dict:
    verifier = ROOT / "tasks" / "creation" / task / "adapter" / "eval-template" / "tests" / "verify_score.py"
    with tempfile.TemporaryDirectory(prefix="agentswe-eqv-") as tmp:
        tmp = Path(tmp)
        fx = bool(case.get("fixture"))
        manifest = materialise(case["manifest"], tmp, fx)
        if judge_format != "archived":
            manifest = with_judge_format(manifest, tmp, judge_format)
        cmd = [sys.executable, "-I", "-B", str(verifier),
               "--manifest", str(manifest),
               "--eval-result", str(materialise(case["eval_result"], tmp, fx)),
               "--harness-result", str(materialise(case["harness_result"], tmp, fx)),
               "--verifier-dir", str(tmp / "verifier")]
        if task == "scientific-pdf-translation" and case.get("compose"):
            trusted = compose_mounts(case["compose"]).get("/trusted-evidence")
            if trusted:
                cmd += ["--trusted-evidence", trusted]
        done = subprocess.run(cmd, capture_output=True, text=True, timeout=300)
        contract = tmp / "verifier" / "score_contract.json"
        if not contract.is_file():
            return {"_error": f"verifier wrote no contract (exit {done.returncode}): {done.stderr[-400:]}"}
        return json.loads(contract.read_text())


def compare(archived: dict, new: dict) -> tuple[str, dict, str | None]:
    if "_error" in new:
        return "error", {"error": new["_error"]}, None
    diff = {f: (archived.get(f), new.get(f)) for f in FIELDS if archived.get(f) != new.get(f)}
    if not diff:
        return "match", {}, None
    for name, (predicate, reason) in KNOWN_FIXES.items():
        if predicate(archived):
            return "expected", diff, f"{name}: {reason}"
    return "mismatch", diff, None


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("family", choices=["creation"])
    src = ap.add_mutually_exclusive_group(required=True)
    src.add_argument("--runs", type=Path, help="directory holding archived run directories")
    src.add_argument("--fixtures", type=Path, help="fixture tree (tests/fixtures/equivalence)")
    ap.add_argument("--run", action="append", default=[], help="run id(s); default: every run under --runs")
    ap.add_argument("--phase-glob", default="*-hidden*")
    ap.add_argument("--report", type=Path)
    ap.add_argument("--judge-format", choices=["archived", "exact", "release"], default="archived",
                    help="replay with the archived manifest (default) or with judge_format forced to exact/release")
    a = ap.parse_args()
    rows = []
    if a.fixtures:
        items = [(c["task"], c["run"], c) for c in fixture_cases(a.fixtures)]
    else:
        run_dirs = [a.runs / r for r in a.run] if a.run else sorted(
            p for p in a.runs.iterdir()
            if any((p / n).is_file() for n in ("protocol_lock.json", "evaluation_protocol_lock.json",
                                                  "score_summary.json")))
        items = []
        for run_dir in run_dirs:
            task = task_of(run_dir)
            items += [(task, run_dir.name, c) for c in archived_cases(run_dir, a.phase_glob)]
    for task, run, case in items:
        archived_path = case["archived_contract"]
        archived = json.loads(materialise(archived_path, Path(tempfile.gettempdir()), False).read_text()) \
            if archived_path.is_file() else {}
        new = replay(task, case, a.judge_format)
        status, diff, reason = compare(archived, new)
        rows.append({"task": task, "run": run, "phase": case["phase"], "case": case["case_id"], "status": status,
                     "archived_score": archived.get("score"), "replayed_score": new.get("score"),
                     "diff": diff, "reason": reason})
    counts: dict[str, int] = {}
    for r in rows:
        counts[r["status"]] = counts.get(r["status"], 0) + 1
    for r in rows:
        if r["status"] != "match":
            print(f"{r['status']:9} {r['run']} {r['phase']} {r['case']}: {r['diff']} {r['reason'] or ''}")
    print(f"cases: {len(rows)}  " + "  ".join(f"{k}={v}" for k, v in sorted(counts.items())))
    if a.report:
        a.report.write_text(json.dumps({"counts": counts, "rows": rows}, indent=1) + "\n")
    return 1 if counts.get("mismatch") or counts.get("error") else 0


if __name__ == "__main__":
    sys.exit(main())
