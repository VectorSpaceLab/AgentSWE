#!/usr/bin/env python3
"""Run fixed document-QA artifact validators and write a deterministic contract."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import subprocess
from pathlib import Path


RETRIEVAL_DEPTH_KEYS = ("full_page", "partial_page", "search_snippet")


def write_json(path: Path, value: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def retrieval_count(value: object) -> tuple[int | None, str | None]:
    """Normalize benchmark-native retrieval values without losing closed-corpus checks.

    Count known numeric counter shapes, not the truthiness of their container.
    Unknown shapes are reporting errors, never invented retrievals. Explicit
    aggregates and their component counts are not added twice.
    """
    if isinstance(value, bool):
        return None, "web_retrieval must be a non-negative integer or structured record"
    if isinstance(value, int):
        if value < 0:
            return None, "web_retrieval must be non-negative"
        return value, None
    if isinstance(value, (list, tuple, set)):
        return len(value), None
    if isinstance(value, dict):
        if not value:
            return 0, None
        aggregate_keys = {"total", "count", "requests"}
        component_keys = set(RETRIEVAL_DEPTH_KEYS) | {
            "browser_network", "followed_links", "followed_link", "scrape", "search",
        }
        if set(value) - aggregate_keys - component_keys:
            return None, "unsupported web_retrieval counter shape"
        components, aggregates = [], []
        for key, item in value.items():
            count, error = retrieval_count(item)
            if error is not None or count is None:
                return None, error or "invalid web_retrieval counter"
            (aggregates if key in aggregate_keys else components).append(count)
        return max([sum(components), *aggregates]), None
    return None, "web_retrieval must be a non-negative integer or structured record"


def integer_count(value: object, name: str) -> tuple[int | None, str | None]:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        return None, f"invalid provider count: {name}"
    return int(value), None


def provider_counts(report: object) -> tuple[dict[str, int], list[str]]:
    """Normalize both the benchmark-native and richer optional count schema."""
    errors: list[str] = []
    if not isinstance(report, dict):
        return {}, ["run_report.json must be an object"]
    if "provider_counts" in report:
        rich = report.get("provider_counts")
        if not isinstance(rich, dict):
            return {}, ["provider_counts must be an object"]
        counts: dict[str, int] = {}
        for key in ("deepseek", "gateway_text", "gateway_image", "serper"):
            value, error = integer_count(rich.get(key, 0) if key == "deepseek" else rich.get(key), key)
            if error is not None or value is None:
                errors.append(error or f"invalid provider count: {key}")
            else:
                counts[key] = value
        retrieval, error = retrieval_count(rich.get("web_retrieval"))
        if error is not None or retrieval is None:
            errors.append(error or "invalid provider count: web_retrieval")
        else:
            counts["web_retrieval"] = retrieval
        if errors:
            return {}, errors
        counts["combined_llm"] = counts["deepseek"] + counts["gateway_text"] + counts["gateway_image"]
        return counts, errors

    counts: dict[str, int] = {}
    for key, field in (
        ("combined_llm", "llm_requests"),
        ("gateway_image", "gateway_image_requests"),
        ("serper", "serper_requests"),
    ):
        value, error = integer_count(report.get(field), field)
        if error is not None or value is None:
            errors.append(f"invalid benchmark-native provider count: {field}")
        else:
            counts[key] = value
    retrieval, error = retrieval_count(report.get("web_retrieval"))
    if error is not None or retrieval is None:
        errors.append("invalid benchmark-native provider count: web_retrieval")
    else:
        counts["web_retrieval"] = retrieval
    return (counts if not errors else {}), errors


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
    if harness.get("evaluation_state") == "infrastructure_error":
        raise RuntimeError("trusted QA validation infrastructure is unavailable")
    run_report_path = args.output_dir / "run_report.json"
    try:
        run_report = json.loads(run_report_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError):
        run_report = {}
    counts, quality_errors = provider_counts(run_report)
    provider_errors: list[str] = []
    if not isinstance(run_report, dict) or run_report.get("status") not in {"success", "ok"}:
        quality_errors.append("run_report status is neither success nor ok")
    run_evidence = (
        json.loads(args.run_evidence.read_text(encoding="utf-8"))
        if args.run_evidence.is_file()
        else {}
    )
    if counts:
        if counts.get("combined_llm", 0) > 300:
            provider_errors.append("candidate combined DeepSeek/GATEWAY budget exceeded")
        if counts.get("gateway_image", 0) > 100:
            provider_errors.append("candidate image-bearing GATEWAY budget exceeded")
        if counts.get("serper") != 0 or counts.get("web_retrieval") != 0:
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
    # hard_feature_valid is intentionally not part of this core execution gate.
    # The source-native viewer is a scoreable feature: Eval must still run and
    # apply the rubric's 20-point ceiling when it is missing or invalid.
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
        "hard_feature_valid": harness.get("hard_feature_valid") is True,
        "hard_feature_errors": harness.get("hard_feature_errors", []),
        "candidate_provider_counts": counts,
        "candidate_provider_errors": provider_errors,
        "quality_errors": quality_errors,
        "evaluation_state": "scoreable" if valid else "fatal_zero",
        "credential_leak_detected": credential_leak_detected,
        "output_structure_valid": output_structure_valid,
        "candidate_exit_code": run_evidence.get("candidate_exit_code"),
        "timed_out": run_evidence.get("timed_out"),
        "candidate_digest": manifest.get("candidate_digest"),
        "case_digest": manifest.get("case_digest"),
        "output_digest": run_evidence.get("output_digest"),
        "trusted_harness_result": harness,
        "trusted_harness_result_sha256": hashlib.sha256(
            json.dumps(harness, sort_keys=True, separators=(',', ':')).encode()
        ).hexdigest(),
        "leaderboard_eligible": False,
        "note": (
            "Execution-contract score only: 100 when core deterministic document-QA gates pass, "
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
