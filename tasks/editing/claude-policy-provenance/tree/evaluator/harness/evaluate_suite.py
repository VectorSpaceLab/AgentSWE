#!/usr/bin/env python3
"""Apply once, run all six isolated cases, and compute their arithmetic mean."""
from __future__ import annotations

import argparse
import importlib.util
import json
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from typing import Any


def load_case_module() -> Any:
    path = Path(__file__).with_name("evaluate_case.py")
    spec = importlib.util.spec_from_file_location("policy_ledger_case", path)
    if spec is None or spec.loader is None:
        raise RuntimeError("cannot load evaluate_case.py")
    module = importlib.util.module_from_spec(spec); sys.modules[spec.name] = module; spec.loader.exec_module(module)
    return module


def case_execution_zero(case_id: str, exc: Any, elapsed: float) -> dict[str, Any]:
    record = dict(exc.record)
    record["stderr"] = str(record.get("stderr", ""))[:1200]
    return {"case_id": case_id, "valid": True, "score": 0, "raw_score": 0,
            "maximum": 100, "assertions": [], "passed_assertion_ids": [],
            "failed_assertion_ids": ["CASE.EXECUTION_FAILURE"], "local_caps": [],
            "commands": [record], "duration_seconds": round(elapsed, 4),
            "execution_failure": {"kind": str(exc.kind)[:80], "message": "case-local production behavior failed"}}


def validate_case_result(case_id: str, result: Any, module: Any) -> None:
    if not isinstance(result, dict) or result.get("case_id") != case_id or result.get("valid") is not True:
        raise module.HarnessError(f"evaluator-integrity failure: missing or invalid result for {case_id}")
    if result.get("maximum") != 100 or not isinstance(result.get("commands"), list):
        raise module.HarnessError(f"evaluator-integrity failure: malformed result for {case_id}")
    if (not isinstance(result.get("score"), int) or isinstance(result.get("score"), bool)
            or not isinstance(result.get("raw_score"), int) or isinstance(result.get("raw_score"), bool)
            or not 0 <= result["score"] <= result["raw_score"] <= 100
            or not isinstance(result.get("duration_seconds"), (int, float))
            or isinstance(result.get("duration_seconds"), bool) or result["duration_seconds"] < 0):
        raise module.HarnessError(f"evaluator-integrity failure: score/result types for {case_id}")
    for record in result["commands"]:
        if not isinstance(record, dict) or len(str(record.get("stderr", ""))) > 1200:
            raise module.HarnessError(f"evaluator-integrity failure: unbounded command evidence for {case_id}")
        if record.get("memory_exceeded"):
            raise module.ResourceEnforcementError(f"resource enforcement failure in {case_id}", record)
    execution = result.get("execution_failure")
    if execution is not None:
        expected_keys = {"case_id", "valid", "score", "raw_score", "maximum", "assertions",
                         "passed_assertion_ids", "failed_assertion_ids", "local_caps", "commands",
                         "duration_seconds", "execution_failure"}
        if (set(result) != expected_keys or not isinstance(execution, dict)
                or set(execution) != {"kind", "message"}
                or not all(isinstance(execution.get(name), str) for name in ("kind", "message"))
                or result.get("score") != 0 or result.get("raw_score") != 0 or result.get("assertions") != []
                or result.get("failed_assertion_ids") != ["CASE.EXECUTION_FAILURE"]):
            raise module.HarnessError(f"evaluator-integrity failure: invalid execution zero for {case_id}")
        return
    expected_keys = {"case_id", "valid", "score", "raw_score", "maximum", "assertions",
                     "passed_assertion_ids", "failed_assertion_ids", "local_caps", "commands",
                     "duration_seconds", "wall_duration_seconds"}
    if set(result) != expected_keys or not isinstance(result.get("wall_duration_seconds"), (int, float)):
        raise module.HarnessError(f"evaluator-integrity failure: malformed normal result for {case_id}")
    assertions = result.get("assertions")
    if (not isinstance(assertions, list) or not assertions
            or any(not isinstance(item, dict) or set(item) != {"id", "points", "passed", "evidence"}
                   or not isinstance(item.get("id"), str)
                   or not isinstance(item.get("points"), int) or isinstance(item.get("points"), bool)
                   or not isinstance(item.get("passed"), bool)
                   or not isinstance(item.get("evidence"), str) or len(item["evidence"]) > 1200
                   for item in assertions)
            or sum(item.get("points", 0) for item in assertions) != 100):
        raise module.HarnessError(f"evaluator-integrity failure: assertion accounting for {case_id}")
    identifiers = [item.get("id") for item in assertions]
    if len(identifiers) != len(set(identifiers)) or any(not isinstance(item.get("passed"), bool) for item in assertions):
        raise module.HarnessError(f"evaluator-integrity failure: assertion semantics for {case_id}")
    raw = sum(item["points"] for item in assertions if item["passed"])
    caps = result.get("local_caps", [])
    contract = module.assertion_contract()["cases"][case_id]
    if [(item["id"], item["points"]) for item in assertions] != [(item["id"], item["points"]) for item in contract["assertions"]]:
        raise module.HarnessError(f"evaluator-integrity failure: assertion contract mismatch for {case_id}")
    expected_caps = {(item["id"], item["maximum"]) for item in contract["caps"]}
    if (not isinstance(caps, list)
            or any(not isinstance(item, dict) or set(item) != {"id", "maximum", "triggered"}
                   or item.get("triggered") is not True
                   or (item.get("id"), item.get("maximum")) not in expected_caps for item in caps)
            or len({item["id"] for item in caps}) != len(caps)):
        raise module.HarnessError(f"evaluator-integrity failure: local cap semantics for {case_id}")
    expected = min([raw, *(item["maximum"] for item in caps)])
    if result.get("raw_score") != raw or result.get("score") != expected:
        raise module.HarnessError(f"evaluator-integrity failure: score/cap accounting for {case_id}")
    if result.get("passed_assertion_ids") != [item["id"] for item in assertions if item["passed"]]:
        raise module.HarnessError(f"evaluator-integrity failure: passed assertion inventory for {case_id}")
    if result.get("failed_assertion_ids") != [item["id"] for item in assertions if not item["passed"]]:
        raise module.HarnessError(f"evaluator-integrity failure: failed assertion inventory for {case_id}")


def evaluate(args: argparse.Namespace, module: Any) -> dict[str, Any]:
    out = args.output_dir; shutil.rmtree(out, ignore_errors=True); out.mkdir(parents=True)
    started = time.monotonic()
    summary: dict[str, Any] = {"schema_version": 1, "valid": False, "validity": {}, "cases": {}, "score": {}, "errors": []}
    try:
        inventory = sorted(path.name for path in args.cases_root.iterdir() if path.is_dir()) if args.cases_root.is_dir() else []
        missing = [case for case in module.CASE_IDS if not (args.cases_root / case / "input.md").is_file() or not (args.cases_root / case / "assets").is_dir()]
        if inventory != list(module.CASE_IDS) or missing:
            raise module.HarnessError(f"invalid hidden fixture inventory: directories={inventory} missing={missing}")
        module.assertion_contract()
        _work, plugin, build = module.prepare(args, out / "prepared")
        summary["validity"] = {"status": "valid", "patch_applied_once": True, "build_entry": build}
        for case_id in module.CASE_IDS:
            case_started = time.monotonic()
            try:
                summary["cases"][case_id] = module.run_case(case_id, args.cases_root, plugin, out / "runs" / case_id)
            except module.CaseExecutionError as exc:
                summary["cases"][case_id] = case_execution_zero(case_id, exc, time.monotonic() - case_started)
            validate_case_result(case_id, summary["cases"][case_id], module)
            if time.monotonic() - started > module.MAX_SECONDS:
                raise module.HarnessError("suite wall-clock limit exceeded")
        if tuple(summary["cases"]) != tuple(module.CASE_IDS):
            raise module.HarnessError("evaluator-integrity failure: incomplete case inventory")
        values = {case: int(summary["cases"][case]["score"]) for case in module.CASE_IDS}
        summary["score"] = {"case_scores": values, "mean_case_score": round(sum(values.values()) / 6, 2),
            "aggregation": "arithmetic mean of six independent 0..100 case scores"}
        summary["valid"] = True
    except Exception as exc:
        summary["validity"] = {"status": "invalid", "zero_rule": "patch/report/build/entry/resource/evaluator-integrity failure"}
        summary["score"] = {"case_scores": {}, "mean_case_score": 0, "aggregation": "validity zero"}
        summary["errors"].append({"type": type(exc).__name__, "message": module.redact(str(exc))})
    summary["duration_seconds"] = round(time.monotonic() - started, 4)
    (out / "run_summary.json").write_text(json.dumps(summary, indent=2) + "\n", encoding="utf-8")
    return summary


def write_reports(submission: Path, paths: list[str]) -> None:
    submission.mkdir(parents=True, exist_ok=True)
    edit = {"schema_version": "1.0", "feature_summary": "evaluator self-test fixture", "changed_paths": paths,
            "commands_run": [], "compatibility_notes": [], "limitations": ["self-test only"]}
    artifacts = [str(Path("plugins/policy-provenance-ledger") / item) for item in
                 (".claude-plugin/plugin.json", "hooks/hooks.json", "hooks/policy_hook.py", "bin/policy-ledger-inspect", "README.md")]
    run = {"status": "success", "artifact_paths": artifacts, "errors": [], "runtime_seconds": 0,
           "peak_memory_mb": 0, "providers": {"deepseek": 0, "gateway": 0, "gateway_image": 0, "serper": 0, "web_retrieval": 0}}
    (submission / "edit_report.json").write_text(json.dumps(edit) + "\n", encoding="utf-8")
    (submission / "run_report.json").write_text(json.dumps(run) + "\n", encoding="utf-8")


def make_patch(repo: Path, source: Path | None, patch: Path, *, broken: bool = False, baseline: bool = False) -> list[str]:
    subprocess.run(["git", "init", "-q"], cwd=repo, check=True)
    subprocess.run(["git", "config", "user.email", "evaluator@example.invalid"], cwd=repo, check=True)
    subprocess.run(["git", "config", "user.name", "Evaluator Self Test"], cwd=repo, check=True)
    (repo / "plugins" / "existing-control" / ".claude-plugin").mkdir(parents=True)
    (repo / "plugins" / "existing-control" / ".claude-plugin" / "plugin.json").write_text('{"name":"existing-control"}\n', encoding="utf-8")
    subprocess.run(["git", "add", "."], cwd=repo, check=True); subprocess.run(["git", "commit", "-qm", "baseline"], cwd=repo, check=True)
    if broken:
        (repo / "outside-plugin.txt").write_text("forbidden\n", encoding="utf-8")
    elif baseline:
        target = repo / "plugins" / "policy-provenance-ledger"
        (target / ".claude-plugin").mkdir(parents=True)
        (target / "hooks").mkdir()
        (target / "bin").mkdir()
        (target / "README.md").write_text("incomplete pristine baseline\n", encoding="utf-8")
        (target / ".claude-plugin" / "plugin.json").write_text('{"name":"incomplete-control"}\n', encoding="utf-8")
        (target / "hooks" / "hooks.json").write_text("{}\n", encoding="utf-8")
        (target / "hooks" / "policy_hook.py").write_text("raise SystemExit(17)\n", encoding="utf-8")
        inspector = target / "bin" / "policy-ledger-inspect"
        inspector.write_text("#!/bin/sh\nexit 17\n", encoding="utf-8")
        inspector.chmod(0o755)
    else:
        assert source is not None
        shutil.copytree(source, repo / "plugins" / "policy-provenance-ledger")
    subprocess.run(["git", "add", "-A"], cwd=repo, check=True)
    diff = subprocess.run(["git", "diff", "--cached", "--binary"], cwd=repo, text=True, capture_output=True, check=True).stdout
    patch.write_text(diff, encoding="utf-8")
    paths = load_case_module().patch_paths(diff)
    if broken:
        (repo / "outside-plugin.txt").unlink()
    else:
        shutil.rmtree(repo / "plugins" / "policy-provenance-ledger")
    subprocess.run(["git", "reset", "-q"], cwd=repo, check=True)
    return paths


def public_runner_isolated(path: Path) -> bool:
    """Reject direct references from builder-visible runner to hidden material."""
    try:
        text = path.read_text(encoding="utf-8")
    except (OSError, UnicodeError):
        return False
    forbidden = ("from evaluator", "import evaluator", "evaluator/harness",
                 "test_cases/", "evaluate_case.py", "evaluate_suite.py")
    return all(marker not in text for marker in forbidden)


def self_test(module: Any, cases_root: Path, output: Path) -> dict[str, Any]:
    shutil.rmtree(output, ignore_errors=True); output.mkdir(parents=True)
    fixture = Path(__file__).resolve().parent / "selftest_fixture" / "plugin"
    checks: list[dict[str, Any]] = []
    results: dict[str, Any] = {}
    for name, options in (("baseline", {"baseline": True}), ("broken", {"broken": True}), ("calibration", {})):
        root = output / name; repo = root / "repository"; repo.mkdir(parents=True)
        patch = root / "submission" / "solution.patch"; patch.parent.mkdir(parents=True)
        paths = make_patch(repo, fixture if name == "calibration" else None, patch, **options)
        write_reports(patch.parent, paths)
        args = argparse.Namespace(cases_root=cases_root, repository=repo, patch=patch,
                                  submission=patch.parent, output_dir=root / "evaluation")
        results[name] = evaluate(args, module)
    entry_fixture = output / "entry-crash-fixture"
    shutil.copytree(fixture, entry_fixture)
    (entry_fixture / "hooks" / "policy_hook.py").write_text(
        "#!/usr/bin/env python3\nraise SystemExit(17)\n", encoding="utf-8")
    entry_root = output / "entry-crash"; entry_repo = entry_root / "repository"; entry_repo.mkdir(parents=True)
    entry_patch = entry_root / "submission" / "solution.patch"; entry_patch.parent.mkdir(parents=True)
    entry_paths = make_patch(entry_repo, entry_fixture, entry_patch)
    write_reports(entry_patch.parent, entry_paths)
    entry_args = argparse.Namespace(cases_root=cases_root, repository=entry_repo, patch=entry_patch,
        submission=entry_patch.parent, output_dir=entry_root / "evaluation")
    results["entry_crash"] = evaluate(entry_args, module)
    calibration_root = output / "calibration"
    bad_report_root = output / "bad-report"
    bad_report_submission = bad_report_root / "submission"
    shutil.copytree(calibration_root / "submission", bad_report_submission)
    bad_report = json.loads((bad_report_submission / "run_report.json").read_text(encoding="utf-8"))
    bad_report.pop("providers")
    (bad_report_submission / "run_report.json").write_text(json.dumps(bad_report) + "\n", encoding="utf-8")
    bad_report_args = argparse.Namespace(cases_root=cases_root, repository=calibration_root / "repository",
        patch=bad_report_submission / "solution.patch", submission=bad_report_submission,
        output_dir=bad_report_root / "evaluation")
    results["bad_report"] = evaluate(bad_report_args, module)
    bad_artifact_submission = output / "bad-artifact-report" / "submission"
    shutil.copytree(calibration_root / "submission", bad_artifact_submission)
    bad_artifact = json.loads((bad_artifact_submission / "run_report.json").read_text(encoding="utf-8"))
    bad_artifact["artifact_paths"].append("plugins/policy-provenance-ledger/../outside.txt")
    (bad_artifact_submission / "run_report.json").write_text(json.dumps(bad_artifact) + "\n", encoding="utf-8")
    artifact_report_rejected = False
    try:
        module.validate_reports(bad_artifact_submission, module.patch_paths(
            (bad_artifact_submission / "solution.patch").read_text(encoding="utf-8")))
    except module.HarnessError:
        artifact_report_rejected = True
    continuation_args = argparse.Namespace(cases_root=cases_root, repository=calibration_root / "repository",
        patch=calibration_root / "submission" / "solution.patch", submission=calibration_root / "submission",
        output_dir=output / "triple-timeout" / "evaluation")
    original_scenarios = dict(module.SCENARIOS)
    def injected_timeout(runtime: Any) -> dict[str, Any]:
        record = {"command": ["synthetic-timeout"], "exit_code": -9, "duration_seconds": 10.0,
                  "peak_tree_pss_mb": 1.0, "memory_ceiling_mb": 4096,
                  "memory_exceeded": False, "timed_out": True, "stderr": "bounded timeout evidence",
                  "operation": "hook:" + runtime.case_id}
        raise module.CaseExecutionError("hook_timeout", "hook process exceeded 10 seconds", record)
    for case_id in ("test_002", "test_004", "test_005"):
        module.SCENARIOS[case_id] = injected_timeout
    try:
        results["triple_timeout"] = evaluate(continuation_args, module)
    finally:
        module.SCENARIOS.clear(); module.SCENARIOS.update(original_scenarios)

    def injected_crash(runtime: Any) -> dict[str, Any]:
        record = {"command": ["synthetic-crash"], "exit_code": 17, "duration_seconds": 0.1,
                  "peak_tree_pss_mb": 1.0, "memory_ceiling_mb": 4096,
                  "memory_exceeded": False, "timed_out": False, "stderr": "bounded crash evidence",
                  "operation": "hook:" + runtime.case_id}
        raise module.CaseExecutionError("hook_crash", "hook process exited 17", record)
    module.SCENARIOS["test_002"] = injected_crash
    try:
        crash_args = argparse.Namespace(cases_root=cases_root, repository=calibration_root / "repository",
            patch=calibration_root / "submission" / "solution.patch", submission=calibration_root / "submission",
            output_dir=output / "hook-crash" / "evaluation")
        results["hook_crash"] = evaluate(crash_args, module)
    finally:
        module.SCENARIOS.clear(); module.SCENARIOS.update(original_scenarios)

    def injected_inspector_protocol(runtime: Any) -> dict[str, Any]:
        record = {"command": ["synthetic-inspector"], "exit_code": 0, "duration_seconds": 0.1,
                  "peak_tree_pss_mb": 1.0, "memory_ceiling_mb": 4096,
                  "memory_exceeded": False, "timed_out": False, "stderr": "bounded protocol evidence",
                  "operation": "inspector"}
        raise module.CaseExecutionError("inspector_protocol", "inspector emitted malformed JSONL", record)
    module.SCENARIOS["test_005"] = injected_inspector_protocol
    try:
        protocol_args = argparse.Namespace(cases_root=cases_root, repository=calibration_root / "repository",
            patch=calibration_root / "submission" / "solution.patch", submission=calibration_root / "submission",
            output_dir=output / "inspector-protocol" / "evaluation")
        results["inspector_protocol"] = evaluate(protocol_args, module)
    finally:
        module.SCENARIOS.clear(); module.SCENARIOS.update(original_scenarios)

    def injected_evaluator_failure(_runtime: Any) -> dict[str, Any]:
        raise RuntimeError("injected evaluator-integrity failure")
    module.SCENARIOS["test_003"] = injected_evaluator_failure
    try:
        integrity_args = argparse.Namespace(cases_root=cases_root, repository=calibration_root / "repository",
            patch=calibration_root / "submission" / "solution.patch", submission=calibration_root / "submission",
            output_dir=output / "integrity-failure" / "evaluation")
        results["integrity_failure"] = evaluate(integrity_args, module)
    finally:
        module.SCENARIOS.clear(); module.SCENARIOS.update(original_scenarios)

    original_ceiling = module.MAX_TREE_PSS_BYTES
    module.MAX_TREE_PSS_BYTES = 1
    try:
        resource_args = argparse.Namespace(cases_root=cases_root, repository=calibration_root / "repository",
            patch=calibration_root / "submission" / "solution.patch", submission=calibration_root / "submission",
            output_dir=output / "resource-failure" / "evaluation")
        results["resource_failure"] = evaluate(resource_args, module)
    finally:
        module.MAX_TREE_PSS_BYTES = original_ceiling

    omitted_cases = output / "omitted-cases"
    shutil.copytree(cases_root, omitted_cases)
    shutil.rmtree(omitted_cases / "test_004")
    omitted_args = argparse.Namespace(cases_root=omitted_cases, repository=calibration_root / "repository",
        patch=calibration_root / "submission" / "solution.patch", submission=calibration_root / "submission",
        output_dir=output / "omission" / "evaluation")
    results["omission"] = evaluate(omitted_args, module)

    extra_cases = output / "extra-cases"
    shutil.copytree(cases_root, extra_cases)
    (extra_cases / "test_007" / "assets").mkdir(parents=True)
    (extra_cases / "test_007" / "input.md").write_text("evaluator inventory control\n", encoding="utf-8")
    extra_args = argparse.Namespace(cases_root=extra_cases, repository=calibration_root / "repository",
        patch=calibration_root / "submission" / "solution.patch", submission=calibration_root / "submission",
        output_dir=output / "extra-inventory" / "evaluation")
    results["extra_inventory"] = evaluate(extra_args, module)

    original_check = module.check
    def weakened_check(identifier: str, points: int, _passed: bool, evidence: Any) -> dict[str, Any]:
        return original_check(identifier, points, True, evidence)
    module.check = weakened_check
    try:
        weakened_args = argparse.Namespace(cases_root=cases_root, repository=calibration_root / "repository",
            patch=calibration_root / "submission" / "solution.patch", submission=calibration_root / "submission",
            output_dir=output / "weakened" / "evaluation")
        results["weakened"] = evaluate(weakened_args, module)
    finally:
        module.check = original_check

    runner = Path(__file__).resolve().parents[2] / "dev_cases" / "run_public_case.py"
    leaky_runner = output / "leaky-public-runner.py"
    leaky_runner.write_text("from evaluator.harness import evaluate_case\n", encoding="utf-8")
    checks.append({"id": "SELFTEST.BASELINE_ENTRY_FAILURE", "passed": not results["baseline"]["valid"] and results["baseline"]["score"]["mean_case_score"] == 0 and "manifest name" in json.dumps(results["baseline"])})
    checks.append({"id": "SELFTEST.ENTRY_PROBE_CRASH_SUITE_INVALID", "passed": not results["entry_crash"]["valid"] and results["entry_crash"]["score"]["mean_case_score"] == 0 and "installed hook entry" in json.dumps(results["entry_crash"])})
    checks.append({"id": "SELFTEST.RESOURCE_ENFORCEMENT_SUITE_INVALID", "passed": not results["resource_failure"]["valid"] and results["resource_failure"]["score"]["mean_case_score"] == 0 and "PSS ceiling" in json.dumps(results["resource_failure"])})
    checks.append({"id": "SELFTEST.BROKEN_PATH_REJECTED", "passed": not results["broken"]["valid"] and "forbidden patch path" in json.dumps(results["broken"])})
    checks.append({"id": "SELFTEST.MALFORMED_REPORT_REJECTED", "passed": not results["bad_report"]["valid"] and results["bad_report"]["score"]["mean_case_score"] == 0 and "run_report" in json.dumps(results["bad_report"])})
    checks.append({"id": "SELFTEST.ARTIFACT_PATH_REPORT_GATE", "passed": artifact_report_rejected})
    calibration = results["calibration"]
    triple_timeout = results["triple_timeout"]
    timeout_ids = ("test_002", "test_004", "test_005")
    checks.append({"id": "SELFTEST.THREE_TIMEOUTS_CASE_LOCAL", "passed": calibration["valid"] and triple_timeout["valid"] and list(triple_timeout["cases"]) == list(module.CASE_IDS) and all(triple_timeout["cases"][case_id].get("failed_assertion_ids") == ["CASE.EXECUTION_FAILURE"] and triple_timeout["cases"][case_id].get("score") == 0 and triple_timeout["cases"][case_id].get("commands", [{}])[0].get("timed_out") is True for case_id in timeout_ids) and all(triple_timeout["score"]["case_scores"][case_id] == calibration["score"]["case_scores"][case_id] for case_id in ("test_001", "test_003", "test_006"))})
    crash = results["hook_crash"]
    checks.append({"id": "SELFTEST.HOOK_CRASH_CASE_LOCAL", "passed": crash["valid"] and crash["cases"]["test_002"].get("score") == 0 and crash["cases"]["test_002"].get("execution_failure", {}).get("kind") == "hook_crash" and crash["cases"]["test_006"].get("score") == calibration["cases"]["test_006"].get("score")})
    protocol = results["inspector_protocol"]
    checks.append({"id": "SELFTEST.INSPECTOR_PROTOCOL_CASE_LOCAL", "passed": protocol["valid"] and protocol["cases"]["test_005"].get("score") == 0 and protocol["cases"]["test_005"].get("execution_failure", {}).get("kind") == "inspector_protocol" and protocol["cases"]["test_006"].get("score") == calibration["cases"]["test_006"].get("score")})
    checks.append({"id": "SELFTEST.EVALUATOR_INTEGRITY_SUITE_INVALID", "passed": not results["integrity_failure"]["valid"] and results["integrity_failure"]["score"]["mean_case_score"] == 0 and "evaluator-integrity failure" in json.dumps(results["integrity_failure"])})
    serialized = json.dumps(results, sort_keys=True)
    checks.append({"id": "SELFTEST.SECRETS_REDACTED", "passed": all(secret not in serialized for secret in module.SECRET_LITERALS)})
    checks.append({"id": "SELFTEST.ASSERTION_LEVEL_SCORE", "passed": calibration["valid"] and all(value.get("assertions") for value in calibration["cases"].values())})
    checks.append({"id": "SELFTEST.SEMANTIC_WEAKENING_DETECTED", "passed": calibration["valid"] and results["weakened"]["valid"] and results["weakened"].get("score", {}).get("case_scores") != calibration.get("score", {}).get("case_scores") and results["weakened"].get("score", {}).get("mean_case_score", 0) > calibration.get("score", {}).get("mean_case_score", 100)})
    checks.append({"id": "SELFTEST.HIDDEN_CASE_OMISSION_REJECTED", "passed": not results["omission"]["valid"] and results["omission"].get("score", {}).get("mean_case_score") == 0 and "invalid hidden fixture inventory" in json.dumps(results["omission"])})
    checks.append({"id": "SELFTEST.HIDDEN_CASE_DUPLICATION_REJECTED", "passed": not results["extra_inventory"]["valid"] and results["extra_inventory"].get("score", {}).get("mean_case_score") == 0 and "invalid hidden fixture inventory" in json.dumps(results["extra_inventory"])})
    checks.append({"id": "SELFTEST.PATCH_TWICE_REJECTED", "passed": calibration["valid"] and calibration.get("validity", {}).get("build_entry", {}).get("patch_apply_count") == 1 and calibration.get("validity", {}).get("build_entry", {}).get("second_apply_rejected") is True})
    checks.append({"id": "SELFTEST.PUBLIC_RUNNER_LEAKAGE_REJECTED", "passed": public_runner_isolated(runner) and not public_runner_isolated(leaky_runner)})
    public_negative: dict[str, Any] = {}
    benchmark_root = Path(__file__).resolve().parents[2]
    for public_case in ("dev_001", "dev_002"):
        done, record = module.run_command(
            [sys.executable, str(runner), "--case", public_case, "--plugin-root", str(fixture)],
            cwd=benchmark_root, timeout=120,
            env={**__import__("os").environ, "PYTHONDONTWRITEBYTECODE": "1"})
        try:
            payload = json.loads(done.stdout)
        except json.JSONDecodeError:
            payload = {}
        public_negative[public_case] = {"exit_code": done.returncode, "payload": payload,
                                        "record": record}
    checks.append({"id": "SELFTEST.PUBLIC_NEGATIVE_BEHAVIOR", "passed": all(
        item["exit_code"] != 0 and isinstance(item["payload"], dict)
        and item["payload"].get("passed") is False
        and not any("execution failure" in str(value).lower()
                    for value in item["payload"].get("failures", []))
        for item in public_negative.values())})
    input_inventory = sorted(path.name for path in (benchmark_root / "input").glob("*.md"))
    public_inventory = sorted(path.name for path in (benchmark_root / "dev_cases").iterdir() if path.is_dir() and path.name.startswith("dev_"))
    hidden_inventory = sorted(path.name for path in cases_root.iterdir() if path.is_dir() and path.name.startswith("test_"))
    checks.append({"id": "SELFTEST.EXACT_4_2_6_INVENTORY", "passed": input_inventory == ["01_task_goal.md", "02_interface_and_delivery.md", "03_requirements_and_constraints.md", "04_resources.md"] and public_inventory == ["dev_001", "dev_002"] and hidden_inventory == list(module.CASE_IDS) and all((cases_root / case_id / "input.md").is_file() and (cases_root / case_id / "assets").is_dir() for case_id in module.CASE_IDS)})
    semantic = module.finish(argparse.Namespace(case_id="semantic-control", commands=[]),
        [module.check("SEM.PASS", 60, True, "pass"), module.check("SEM.FAIL", 40, False, "fail")],
        [{"id": "CAP.SEMANTIC", "maximum": 10, "triggered": True}])
    bad_weights_rejected = False
    try:
        module.finish(argparse.Namespace(case_id="bad-weight-control", commands=[]),
            [module.check("SEM.BAD", 99, True, "bad")])
    except module.HarnessError:
        bad_weights_rejected = True
    checks.append({"id": "SELFTEST.SCORE_CAP_ACCOUNTING", "passed": semantic["raw_score"] == 60 and semantic["score"] == 10 and semantic["local_caps"][0]["id"] == "CAP.SEMANTIC" and bad_weights_rejected})
    contract = module.assertion_contract()
    bad_contract = json.loads(json.dumps(contract)); bad_contract["cases"]["test_001"]["assertions"][1]["id"] = bad_contract["cases"]["test_001"]["assertions"][0]["id"]
    bad_contract_rejected = False
    try:
        module.validate_assertion_contract(bad_contract)
    except module.HarnessError:
        bad_contract_rejected = True
    checks.append({"id": "SELFTEST.ASSERTION_SEMANTIC_CONTRACT", "passed": bad_contract_rejected and tuple(contract["cases"]) == tuple(module.CASE_IDS) and all(sum(item["points"] for item in case["assertions"]) == 100 for case in contract["cases"].values())})

    cycle8_event = {"session_id": "cycle8-selftest", "hook_event_name": "PreToolUse",
                    "cwd": str(output / "cycle8-protocol-workspace"), "tool_name": "Read",
                    "tool_use_id": "cycle8-protocol", "tool_input": {"file_path": "src/a.py"}}
    cycle8_workspace = output / "cycle8-protocol-workspace"
    (cycle8_workspace / "src").mkdir(parents=True)
    cycle8_state = output / "cycle8-protocol-state"
    cycle8_env = {**__import__("os").environ, "CLAUDE_PROJECT_DIR": str(cycle8_workspace),
                  "CLAUDE_PLUGIN_ROOT": str(fixture),
                  "POLICY_PROVENANCE_STATE_DIR": str(cycle8_state),
                  "PYTHONDONTWRITEBYTECODE": "1"}
    cycle8_done, _cycle8_record = module.run_command(
        [sys.executable, str(fixture / "hooks" / "policy_hook.py")], cwd=cycle8_workspace,
        timeout=10, env=cycle8_env, input_text=json.dumps(cycle8_event))
    try:
        cycle8_response = json.loads(cycle8_done.stdout)
    except json.JSONDecodeError:
        cycle8_response = {}
    cycle8_fields = {"approval_ticket_id", "approval_state", "approval_count",
                     "approval_threshold", "approval_round", "approval_required_groups",
                     "approval_satisfied_groups", "owner_instance_id", "owner_generation",
                     "ownership_state", "handoff_id", "state_format_version", "upgrade_id",
                     "upgrade_state", "effect_id", "effect_generation", "effect_state",
                     "repair_id", "repair_generation", "integrity_state",
                     "session_authority_id", "session_generation", "continuation_id",
                     "continuation_state"}
    removed_fields_valid = []
    for field in cycle8_fields:
        weakened = json.loads(json.dumps(cycle8_response))
        if isinstance(weakened.get("policyReceipt"), dict): weakened["policyReceipt"].pop(field, None)
        removed_fields_valid.append(module.valid_receipt(weakened, cycle8_event))
    checks.append({"id": "SELFTEST.CYCLE8_UNIVERSAL_RECEIPT", "passed": cycle8_done.returncode == 0 and module.valid_receipt(cycle8_response, cycle8_event) and not any(removed_fields_valid) and cycle8_fields.issubset(module.RECEIPT_FIELDS)})
    all_assertions = [item for case in contract["cases"].values()
                      for item in case["assertions"]]
    cycle8_assertions = [item for item in all_assertions
                         if item["id"].split(".", 1)[0].endswith("8")]
    cycle9_assertions = [item for item in all_assertions
                         if item["id"].endswith("9.INTEGRITY_SEAL_AND_PATH_FENCE")]
    cycle8_ids = [item["id"] for item in cycle8_assertions]
    new_markers = ("REPAIR", "QUARANTINE", "ATTEST", "VERIFIER", "CONTINUATION", "SESSION")
    per_case_new_points = [sum(item["points"] for item in case["assertions"]
                               if item["id"].split(".", 1)[0].endswith("8")
                               if any(marker.lower() in (item["id"] + " " + item["semantic"]).lower()
                                      for marker in new_markers))
                           for case in contract["cases"].values()]
    checks.append({"id": "SELFTEST.CYCLE8_SCORING_CONTRACT", "passed":
        len(all_assertions) == 54 and len({item["id"] for item in all_assertions}) == 54
        and len(cycle8_ids) == 48 and len(cycle8_ids) == len(set(cycle8_ids))
        and min(per_case_new_points) >= 70 and len(cycle9_assertions) == 6
        and all(item["points"] == 10 for item in cycle9_assertions)})
    all_caps = [item for case in contract["cases"].values() for item in case["caps"]]
    cycle9_caps = [item for item in all_caps
                   if item["id"].endswith("9_INTEGRITY_BYPASS")]
    cycle8_caps = [item for item in all_caps if item not in cycle9_caps]
    checks.append({"id": "SELFTEST.CYCLE8_CAP_CONTRACT", "passed":
        len(all_caps) == 18 and len({item["id"] for item in all_caps}) == 18
        and all(item["maximum"] == 10 for item in all_caps)
        and len(cycle8_caps) == 12
        and all(any(marker in item["id"] for marker in
                    ("REPAIR", "SESSION", "TOKEN", "SOURCE", "AUTHORITY",
                     "SECRET", "ARCHIVE", "LINEAGE", "QUARANTINE"))
                for item in cycle8_caps)
        and len(cycle9_caps) == 6})
    checks.append({"id": "SELFTEST.CYCLE8_PUBLIC_SCHEMAS_NEGATIVE", "passed": all(not validator({"schema_version": 1}) for validator in (
        module.valid_approval, module.valid_checkpoint, module.valid_checkpoint_view,
        module.valid_handoff_offer, module.valid_handoff_ack, module.valid_handoff_commit,
        module.valid_handoff_view, module.valid_effect, module.valid_effect_claim,
        module.valid_upgrade_manifest, module.valid_upgrade_status,
        module.valid_upgrade_commit, module.valid_upgrade_view,
        module.valid_repair_manifest, module.valid_repair_status,
        module.valid_repair_attestation, module.valid_repair_commit,
        module.valid_repair_view, module.valid_continuation_token,
        module.valid_continuation_view))})
    repair_reject, _ = module.run_command(
        [str(fixture / "bin" / "policy-ledger-inspect"), "--state-dir", str(cycle8_state),
         "--begin-repair"], cwd=cycle8_workspace, timeout=10, env=cycle8_env,
        input_text="{}")
    continuation_reject, _ = module.run_command(
        [str(fixture / "bin" / "policy-ledger-inspect"), "--state-dir", str(cycle8_state),
         "--issue-continuation"], cwd=cycle8_workspace, timeout=10, env=cycle8_env,
        input_text="{}")
    repair_view, _ = module.run_command(
        [str(fixture / "bin" / "policy-ledger-inspect"), "--state-dir", str(cycle8_state),
         "--repairs"], cwd=cycle8_workspace, timeout=10, env=cycle8_env)
    continuation_view, _ = module.run_command(
        [str(fixture / "bin" / "policy-ledger-inspect"), "--state-dir", str(cycle8_state),
         "--continuations"], cwd=cycle8_workspace, timeout=10, env=cycle8_env)
    checks.append({"id": "SELFTEST.CYCLE8_NEGATIVE_COMMAND_BOUNDARY", "passed":
        repair_reject.returncode != 0 and not repair_reject.stdout.strip()
        and continuation_reject.returncode != 0 and not continuation_reject.stdout.strip()
        and repair_view.returncode == 0 and not repair_view.stdout.strip()
        and continuation_view.returncode == 0 and not continuation_view.stdout.strip()})

    def rejected_result(value: Any, expected_case: str = "test_001") -> bool:
        try:
            validate_case_result(expected_case, value, module)
        except module.HarnessError:
            return True
        return False

    valid_sample = json.loads(json.dumps(calibration["cases"]["test_001"]))
    missing_field = json.loads(json.dumps(valid_sample)); missing_field.pop("raw_score")
    duplicate_assertion = json.loads(json.dumps(valid_sample)); duplicate_assertion["assertions"].append(dict(duplicate_assertion["assertions"][0]))
    wrong_identity = json.loads(json.dumps(valid_sample)); wrong_identity["case_id"] = "test_002"
    wrong_score = json.loads(json.dumps(valid_sample)); wrong_score["score"] = 99
    checks.append({"id": "SELFTEST.EVERY_RESULT_VALIDITY_RULE", "passed": all(rejected_result(item) for item in (missing_field, duplicate_assertion, wrong_identity, wrong_score))})

    def records(value: Any) -> list[dict[str, Any]]:
        found: list[dict[str, Any]] = []
        if isinstance(value, dict):
            if "peak_tree_pss_mb" in value: found.append(value)
            for item in value.values(): found.extend(records(item))
        elif isinstance(value, list):
            for item in value: found.extend(records(item))
        return found

    pss_records = records(calibration)
    case_source = (Path(__file__).with_name("evaluate_case.py")).read_text(encoding="utf-8")
    checks.append({"id": "SELFTEST.REALISTIC_PSS_SEMANTICS", "passed": module.MAX_TREE_PSS_BYTES == 4 * 1024 * 1024 * 1024 and bool(pss_records) and max(item.get("peak_tree_pss_mb", 0) for item in pss_records) > 0 and all(not item.get("memory_exceeded") for item in pss_records) and "setrlimit" not in case_source and "preexec_fn" not in case_source and "--jitless" not in case_source})
    report = {"passed": all(item["passed"] for item in checks), "checks": checks,
              "calibration_case_scores": calibration.get("score", {}).get("case_scores", {}),
              "public_negative": public_negative,
              "baseline_error": results["baseline"].get("errors", []), "broken_error": results["broken"].get("errors", []),
              "resource_error": results["resource_failure"].get("errors", [])}
    (output / "self_test_report.json").write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
    return report


def parser() -> argparse.ArgumentParser:
    value = argparse.ArgumentParser(description="Evaluate all six Policy Provenance Ledger cases")
    value.add_argument("--self-test", action="store_true", help="run evaluator-owned calibration and rejection tests")
    value.add_argument("--cases-root", type=Path, default=Path(__file__).resolve().parents[2] / "test_cases")
    value.add_argument("--repository", type=Path)
    value.add_argument("--patch", type=Path)
    value.add_argument("--submission", type=Path)
    value.add_argument("--output-dir", type=Path, required=True)
    return value


def main() -> int:
    args = parser().parse_args(); module = load_case_module()
    args.cases_root = args.cases_root.resolve(); args.output_dir = args.output_dir.resolve()
    if args.self_test:
        report = self_test(module, args.cases_root, args.output_dir); print(json.dumps(report, indent=2)); return 0 if report["passed"] else 1
    missing = [name for name in ("repository", "patch", "submission") if getattr(args, name) is None]
    if missing: parser().error("normal evaluation requires " + ", ".join("--" + name for name in missing))
    for name in ("repository", "patch", "submission"): setattr(args, name, getattr(args, name).resolve())
    summary = evaluate(args, module); print(json.dumps(summary, indent=2)); return 0 if summary["valid"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
