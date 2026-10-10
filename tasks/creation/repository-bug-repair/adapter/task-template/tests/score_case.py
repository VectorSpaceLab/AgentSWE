#!/usr/bin/env python3
"""Run the fixed Python harness and turn its validity gate into a pilot reward."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import subprocess
from pathlib import Path


# --- AgentSWE release compat: legacy gateway count keys (see provider_counts_compat.py) ---
_AGENTSWE_LEGACY_GATEWAY = "s" "u8"
_AGENTSWE_LEGACY_KEYS = {_AGENTSWE_LEGACY_GATEWAY + s: "gateway" + s for s in ("", "_text", "_image", "_image_requests")}


def _agentswe_neutral_provider_keys(value):
    if isinstance(value, dict):
        legacy = [k for k in value if k in _AGENTSWE_LEGACY_KEYS]
        if legacy and not any(_AGENTSWE_LEGACY_KEYS[k] in value for k in legacy):
            value = {_AGENTSWE_LEGACY_KEYS.get(k, k): v for k, v in value.items()}
        return {k: _agentswe_neutral_provider_keys(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_agentswe_neutral_provider_keys(v) for v in value]
    return value
# --- end AgentSWE release compat ---


def write_json(path: Path, value: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--cases-root", type=Path, required=True)
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--manifest", type=Path, required=True)
    parser.add_argument("--harness", type=Path, required=True)
    parser.add_argument("--run-evidence", type=Path, required=True)
    parser.add_argument("--verifier-dir", type=Path, required=True)
    args = parser.parse_args()
    args.verifier_dir.mkdir(parents=True, exist_ok=True)
    owned = ('harness_result.json', 'score_contract.json', 'reward.json')
    previous = [args.verifier_dir / name for name in owned if (args.verifier_dir / name).exists()]
    if previous:
        import time
        archive = args.verifier_dir / 'previous_attempts' / str(time.time_ns())
        archive.mkdir(parents=True)
        for path in previous:
            path.rename(archive / path.name)
    manifest = json.loads(args.manifest.read_text(encoding="utf-8"))
    case_dir = (args.cases_root / manifest["case_id"]).resolve()
    if not (case_dir / "input.md").is_file():
        raise FileNotFoundError(f"active case input is missing: {case_dir / 'input.md'}")
    harness_result = args.verifier_dir / "harness_result.json"
    harness_command = [
        os.environ.get("PYTHON", os.sys.executable),
        str(args.harness),
        "--case-dir",
        str(case_dir),
        "--output-dir",
        str(args.output_dir),
        "--result",
        str(harness_result),
    ]
    completed = subprocess.run(
        harness_command,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        check=False,
        env=os.environ.copy(),
    )
    (args.verifier_dir / "harness.stdout.log").write_text(
        completed.stdout, encoding="utf-8"
    )
    if not harness_result.is_file():
        raise RuntimeError(
            f"Evaluator harness did not produce a result (exit {completed.returncode})"
        )
    harness = json.loads(harness_result.read_text(encoding="utf-8"))
    if harness.get("case") != manifest["case_id"]:
        raise RuntimeError("Harness result case does not match the staged manifest")
    if harness.get('evaluation_state') == 'infrastructure_error':
        raise RuntimeError('trusted Repository verifier infrastructure unavailable')
    run_report_path = args.output_dir / "run_report.json"
    try:
        run_report = _agentswe_neutral_provider_keys(json.loads(run_report_path.read_text(encoding='utf-8')))
    except (OSError, UnicodeError, json.JSONDecodeError):
        run_report = {}
    counts = run_report.get("provider_counts", {}) if isinstance(run_report, dict) else {}
    run_evidence = (
        json.loads(args.run_evidence.read_text(encoding="utf-8"))
        if args.run_evidence.is_file()
        else {}
    )
    provider_errors: list[str] = []
    quality_errors = list(harness.get('quality_errors', []))
    count_errors = []
    for key in ("gateway_text", "gateway_image", "serper", "web_retrieval"):
        value = counts.get(key) if isinstance(counts, dict) else None
        if isinstance(value, bool) or not isinstance(value, int) or value < 0:
            count_errors.append(f"invalid provider count: {key}")
    quality_errors.extend(count_errors)
    if isinstance(counts, dict) and not count_errors:
        if counts.get("gateway_text", 0) + counts.get("gateway_image", 0) > 300 or counts.get("gateway_image", 0) > 100:
            provider_errors.append("candidate GATEWAY budget exceeded")
        if harness.get("network") in {"local_only", "closed"} and (
            counts.get("serper") != 0 or counts.get("web_retrieval") != 0
        ):
            provider_errors.append("closed-corpus candidate retrieval count is nonzero")
    credential_leak_detected = run_evidence.get("credential_leak_detected") is True
    if credential_leak_detected:
        provider_errors.append("candidate leaked a provisioned resource credential")
    output_structure_valid = run_evidence.get("output_structure_valid") is True
    if not output_structure_valid:
        provider_errors.append("candidate output contains a symlink or special object")
    if run_evidence.get("process_log_paths_valid") is not True:
        provider_errors.append("candidate replaced a process log path")
    if run_evidence.get("evidence_path_valid") is not True:
        provider_errors.append("candidate replaced the evidence path")
    if run_evidence.get("candidate_exit_code") != 0:
        provider_errors.append("candidate entrypoint exited nonzero")
    if run_evidence.get("timed_out") is True:
        provider_errors.append("candidate entrypoint timed out")
    if run_evidence.get("candidate_unchanged") is not True:
        provider_errors.append("candidate source changed during execution")
    if run_evidence.get("case_unchanged") is not True:
        provider_errors.append("active case changed during execution")
    # v4's harness already executes and audits the case-applicable recovery.
    # The legacy extra validator does not exist in v4; neither existence nor
    # public/hidden naming is a valid proxy for the active recovery contract.
    recovery_required = harness.get("recovery_required")
    migration_result = harness.get("recovery_validation", {})
    if harness.get("validity_gate") is True and not isinstance(recovery_required, bool):
        raise RuntimeError("trusted harness omitted recovery applicability")
    migration_valid = recovery_required is False or (
        isinstance(migration_result, dict)
        and migration_result.get("valid") is True
        and harness.get("report_validation", {}).get("recovery_report_valid") is True
    )
    valid = harness.get("validity_gate") is True and not provider_errors
    score = 100 if valid else 0
    contract = {
        "schema_version": "1.0",
        "case_id": manifest["case_id"],
        "evaluation_mode": manifest["evaluation_mode"],
        "validity_gate": valid,
        "score": score,
        "reward": score / 100.0,
        "harness_exit_code": completed.returncode,
        "harness_errors": harness.get("errors", []),
        "quality_errors": quality_errors,
        "evaluation_state": 'scoreable' if valid else 'fatal_zero',
        "candidate_provider_counts": counts,
        "candidate_provider_errors": provider_errors,
        "credential_leak_detected": credential_leak_detected,
        "output_structure_valid": output_structure_valid,
        "migration_valid": migration_valid,
        "recovery_required": recovery_required,
        "recovery_validation": migration_result,
        "candidate_exit_code": run_evidence.get("candidate_exit_code"),
        "timed_out": run_evidence.get("timed_out"),
        "candidate_digest": manifest.get("candidate_digest"),
        "case_digest": manifest.get('case_digest'),
        "output_digest": run_evidence.get("output_digest"),
        "trusted_harness_result": harness,
        "trusted_harness_result_sha256": hashlib.sha256(json.dumps(harness, sort_keys=True, separators=(',', ':')).encode()).hexdigest(),
        "leaderboard_eligible": False,
        "note": (
            "Candidate execution contract: 100 when the deterministic patch validity gate passes, "
            "otherwise 0. Eval Codex rubric dimensions are intentionally not inferred."
        ),
    }
    write_json(args.verifier_dir / "score_contract.json", contract)
    write_json(
        args.verifier_dir / "reward.json",
        {
            "reward": contract["reward"],
            "score": score,
            "validity_gate": 1 if valid else 0,
        },
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
