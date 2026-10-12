#!/usr/bin/env python3
"""Run the case-local deterministic extraction validator and emit a contract."""
from __future__ import annotations
import argparse, json, os, subprocess, hashlib, shutil, time
from pathlib import Path

def write(path: Path, value: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, indent=2, sort_keys=True) + "\n", encoding="utf-8")

def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--cases-root", type=Path, required=True)
    p.add_argument("--output-dir", type=Path, required=True)
    p.add_argument("--manifest", type=Path, required=True)
    p.add_argument("--harness", type=Path, required=True)
    p.add_argument("--run-evidence", type=Path, required=True)
    p.add_argument("--verifier-dir", type=Path, required=True)
    a = p.parse_args(); a.verifier_dir.mkdir(parents=True, exist_ok=True)
    old=[a.verifier_dir/name for name in ('score_contract.json','reward.json','harness_result.json') if (a.verifier_dir/name).exists()]
    if old:
        archive=a.verifier_dir/'previous_attempts'/str(time.time_ns());archive.mkdir(parents=True)
        for path in old:shutil.move(str(path),archive/path.name)
    m = json.loads(a.manifest.read_text()); case = m["case_id"]
    case_dir = (a.cases_root / case).resolve(); result = a.verifier_dir / "harness_result.json"
    cmd = [os.environ.get("PYTHON", os.sys.executable), str(a.harness), "--case-dir", str(case_dir), "--output-dir", str(a.output_dir), "--result", str(result)]
    cp = subprocess.run(cmd, text=True, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, check=False)
    (a.verifier_dir / "harness.stdout.log").write_text(cp.stdout, encoding="utf-8")
    if not result.is_file():
        raise RuntimeError("trusted validator did not produce a result")
    harness = json.loads(result.read_text())
    if harness.get("evaluation_state") == "infrastructure_error":
        raise RuntimeError("trusted validator infrastructure failure: " + repr(harness.get("infrastructure_errors")))
    evidence = json.loads(a.run_evidence.read_text()) if a.run_evidence.is_file() else {}
    counts = {}
    report = a.output_dir / "run_report.json"
    if report.is_file():
        try:
            value = json.loads(report.read_text()); counts = value.get("provider_counts", value.get("usage", {})) if isinstance(value, dict) else {}
        except (ValueError, UnicodeError):
            counts = {}
    errors = list(harness.get("fatal_errors", []))
    quality_errors = list(harness.get("quality_errors", []))
    if not isinstance(counts, dict):
        quality_errors.append('provider counts are not an object')
        counts = {}
    for key in ("gateway", "gateway_text", "gateway_image", "serper", "web_retrieval"):
        if key in counts and (isinstance(counts[key], bool) or not isinstance(counts[key], int) or counts[key] < 0): quality_errors.append("invalid provider count:" + key)
    typed_counts = {key: value for key, value in counts.items() if type(value) is int and value >= 0}
    if typed_counts.get("serper", 0) or typed_counts.get("web_retrieval", 0): errors.append("closed-corpus retrieval count is nonzero")
    if typed_counts.get("gateway", typed_counts.get("gateway_text", 0)) > 300 or typed_counts.get("gateway_image", 0) > 100: errors.append("GATEWAY request budget exceeded")
    for key, expected in (("candidate_exit_code", 0), ("timed_out", False), ("candidate_unchanged", True), ("case_unchanged", True), ("output_structure_valid", True), ("process_log_paths_valid", True), ("evidence_path_valid", True)):
        if evidence.get(key) != expected: errors.append(key + " invalid")
    valid = harness.get("validity_gate") is True and not errors
    contract = {"schema_version":"1.0", "case_id":case, "evaluation_mode":m.get("evaluation_mode"), "validity_gate":valid, "score":100 if valid else 0, "reward":1.0 if valid else 0.0, "harness_exit_code":cp.returncode, "harness_errors":harness.get("errors", []), "candidate_provider_counts":counts, "candidate_provider_errors":errors, "candidate_exit_code":evidence.get("candidate_exit_code"), "timed_out":evidence.get("timed_out"), "candidate_digest":m.get("candidate_digest"), "output_digest":evidence.get("output_digest"), "leaderboard_eligible":False}
    contract.update({"quality_errors": quality_errors, "evaluation_state": "scoreable" if valid else "fatal_zero",
                     "hard_feature_valid": harness.get("hard_feature_valid"), "hard_feature_errors": harness.get("hard_feature_errors", [])})
    contract.update({'fatal_gate':not valid,'fatal_reasons':errors,'case_digest':m.get('case_digest'),'trusted_harness_result':harness,'trusted_harness_result_sha256':hashlib.sha256(json.dumps(harness,sort_keys=True,separators=(',',':')).encode()).hexdigest()})
    write(a.verifier_dir / "score_contract.json", contract); write(a.verifier_dir / "reward.json", {"reward":contract["reward"], "score":contract["score"], "validity_gate":int(valid)})
    return 0
if __name__ == "__main__": raise SystemExit(main())
