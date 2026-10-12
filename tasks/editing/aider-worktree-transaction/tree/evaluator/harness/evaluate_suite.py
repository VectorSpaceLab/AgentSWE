#!/usr/bin/env python3
"""Validate/apply once, install once, and average six isolated schema-v3 cases."""
from __future__ import annotations

import argparse
import ast
import json
import os
import shutil
import time
from pathlib import Path
from typing import Any

from common import CASE_IDS, ENTRY_MODULE, HarnessError, read_json, redact, run, run_monitored, write_json
from evaluate_case import evaluate, validate_result_manifest

HERE = Path(__file__).resolve().parent
PACKAGE = HERE.parent.parent
ALLOWED_PREFIXES = ("aider/", "tests/")
FORBIDDEN_PARTS = ("evaluator/", "dev_cases/", "test_cases/", "input/", "meta/", ".github/", "requirements", "pyproject.toml", "pytest.ini")


def changed_paths(work: Path, patch: Path) -> list[str]:
    done = run(["git", "apply", "--numstat", str(patch)], cwd=work)
    if done.returncode:
        raise HarnessError(f"patch cannot be parsed: {done.stderr.strip()}")
    paths = []
    for line in done.stdout.splitlines():
        fields = line.split("\t")
        if len(fields) >= 3:
            path = fields[-1]
            if path.startswith("{") or " => " in path:
                raise HarnessError("delivery reports must enumerate source renames explicitly; patch rename shorthand is unsupported")
            paths.append(path)
    if not paths:
        raise HarnessError("solution.patch is empty")
    return sorted(set(paths))


def validate_paths(paths: list[str]) -> None:
    bad = [path for path in paths if path.startswith("/") or ".." in Path(path).parts or not path.startswith(ALLOWED_PREFIXES) or any(path.startswith(prefix) for prefix in FORBIDDEN_PARTS)]
    if bad:
        raise HarnessError(f"forbidden changed paths: {bad}")
    if "aider/worktree_plan_adapter.py" not in paths:
        raise HarnessError("missing fixed entry aider/worktree_plan_adapter.py")
    if not any(path.startswith("tests/") and path.endswith(".py") for path in paths):
        raise HarnessError("patch must include focused source-adjacent Python tests")


def validate_reports(submission: Path, paths: list[str]) -> dict[str, Any]:
    findings: dict[str, Any] = {"valid": True, "errors": []}
    for name in ("edit_report.json", "run_report.json"):
        path = submission / name
        if not path.is_file():
            findings["valid"] = False
            findings["errors"].append(f"missing {name}")
            continue
        try:
            value = read_json(path)
        except Exception as exc:
            findings["valid"] = False
            findings["errors"].append(f"invalid {name}: {exc}")
            continue
        if not isinstance(value, dict) or value.get("schema_version") != 1:
            findings["valid"] = False
            findings["errors"].append(f"invalid {name} schema")
            continue
        serialized = json.dumps(value)
        if any(token in serialized for token in ("DEEPSEEK_API_KEY", "DEEPSEEK_API_KEY", "SERPER_TOKEN", "fixture-private", "lease_token")):
            findings["valid"] = False
            findings["errors"].append(f"sensitive content in {name}")
        if name == "edit_report.json":
            if sorted(value.get("changed_paths", [])) != paths or not isinstance(value.get("tests"), list) or not isinstance(value.get("summary"), str):
                findings["valid"] = False
                findings["errors"].append("edit_report paths/tests/summary mismatch")
        else:
            required = {"status", "commands", "duration_seconds", "errors", "deepseek", "gateway", "gateway_image", "serper", "web_retrieval"}
            if not required <= set(value) or any(value.get(key) != 0 for key in ("deepseek", "gateway", "gateway_image", "serper", "web_retrieval")):
                findings["valid"] = False
                findings["errors"].append("run_report fields or provider counts invalid")
    return findings


def validate_case_layout(cases_root: Path) -> None:
    actual = {path.name for path in cases_root.iterdir() if path.is_dir() and path.name.startswith("test_")}
    if actual != set(CASE_IDS):
        raise HarnessError(f"hidden case inventory mismatch: expected {CASE_IDS}, got {sorted(actual)}")
    for case_id in CASE_IDS:
        input_path = cases_root / case_id / "input.md"
        scenario_path = cases_root / case_id / "assets" / "scenario.json"
        if not input_path.is_file() or not scenario_path.is_file():
            raise HarnessError(f"hidden case material missing: {case_id}")
        value = read_json(scenario_path)
        if not isinstance(value, dict) or value.get("schema_version") != 3:
            raise HarnessError(f"hidden scenario schema invalid: {case_id}")


def public_isolation_findings(package: Path) -> list[str]:
    errors: list[str] = []
    public = package / "dev_cases"
    forbidden_text = ("evaluator/harness", "test_cases/", "from evaluator", "import evaluator")
    for path in sorted(public.rglob("*")):
        if not path.is_file() or path.suffix not in {".py", ".sh"}:
            continue
        text = path.read_text(encoding="utf-8")
        if any(marker in text for marker in forbidden_text):
            errors.append(f"hidden evaluator reference in {path.relative_to(package)}")
        if path.suffix == ".py":
            try:
                ast.parse(text, filename=str(path))
            except SyntaxError as exc:
                errors.append(f"public runner syntax error in {path.relative_to(package)}: {exc.msg}")
    return errors


def copy_pristine(source: Path, target: Path) -> None:
    shutil.rmtree(target, ignore_errors=True)
    shutil.copytree(source, target, symlinks=True, ignore=shutil.ignore_patterns(".git", "__pycache__", ".pytest_cache", "*.pyc"))
    done = run(["git", "init", "-q"], cwd=target)
    if done.returncode:
        raise HarnessError(done.stderr)
    run(["git", "config", "user.name", "Benchmark Evaluator"], cwd=target)
    run(["git", "config", "user.email", "evaluator@example.invalid"], cwd=target)
    run(["git", "add", "-A"], cwd=target)
    committed = run(["git", "commit", "-q", "-m", "pristine evaluator source"], cwd=target)
    if committed.returncode:
        raise HarnessError(committed.stderr)


def no_external_symlinks(work: Path) -> None:
    for path in work.rglob("*"):
        if path.is_symlink() and not path.resolve().is_relative_to(work.resolve()):
            raise HarnessError(f"external symlink after patch: {path.relative_to(work)}")


def apply_once(work: Path, patch: Path) -> dict[str, int]:
    check = run(["git", "apply", "--check", str(patch)], cwd=work)
    if check.returncode:
        raise HarnessError(f"patch apply check failed: {check.stderr}")
    applied = run(["git", "apply", str(patch)], cwd=work)
    if applied.returncode:
        raise HarnessError(f"patch apply failed: {applied.stderr}")
    second = run(["git", "apply", "--check", str(patch)], cwd=work)
    if second.returncode == 0:
        raise HarnessError("patch remains applicable after first application")
    return {"check_exit_code": check.returncode, "apply_exit_code": applied.returncode, "second_check_exit_code": second.returncode, "applied_count": 1}


def aggregate_results(results: dict[str, dict[str, Any]]) -> dict[str, Any]:
    """Aggregate only a complete inventory of manifest-valid case results."""
    if set(results) != set(CASE_IDS):
        raise HarnessError(f"case result inventory mismatch: {sorted(results)}")
    for case_id in CASE_IDS:
        result = results[case_id]
        if not result.get("valid"):
            raise HarnessError(f"missing or evaluator-invalid result: {case_id}")
        errors = validate_result_manifest(result)
        if errors:
            raise HarnessError("; ".join(errors))
    values = {case_id: int(results[case_id]["score"]) for case_id in CASE_IDS}
    return {"case_scores": values, "mean_case_score": round(sum(values.values()) / len(CASE_IDS), 2), "aggregation": "arithmetic mean of six independent 0..100 cases"}


def prepare(args: argparse.Namespace, summary: dict[str, Any]) -> tuple[Path, Path, list[str]]:
    work = args.output_dir / "patched-source"
    copy_pristine(args.repository, work)
    paths = changed_paths(work, args.patch)
    validate_paths(paths)
    reports = validate_reports(args.submission, paths)
    summary["delivery_reports"] = reports
    if not reports["valid"]:
        raise HarnessError("delivery report validation failed")
    summary["patch"] = {"changed_paths": paths, **apply_once(work, args.patch)}
    no_external_symlinks(work)
    if not args.skip_prepare:
        prepared = run([str(HERE / "prepare_environment.sh"), str(args.repository), str(args.python_env)], cwd=PACKAGE, timeout=600)
        summary["environment"] = {"prepare_exit_code": prepared.returncode, "stderr": redact(prepared.stderr, (args.repository, args.output_dir, args.python_env))}
        if prepared.returncode:
            raise HarnessError("environment preparation failed")
    python = args.python_env / "bin" / "python"
    if not python.is_file():
        raise HarnessError(f"prepared Python missing: {python}")
    env = dict(os.environ)
    env.update({"PIP_CACHE_DIR": str(args.python_env / "pip-cache"), "TMPDIR": str(args.python_env / "tmp"), "PYTHONPYCACHEPREFIX": str(args.output_dir / "pycache")})
    installed = run_monitored([str(python), "-m", "pip", "install", "--no-deps", "-e", str(work)], cwd=work, env=env, timeout=180)
    summary.setdefault("environment", {}).update({"patched_install_exit_code": installed.returncode, "install_peak_pss_bytes": installed.peak_pss_bytes})
    if installed.returncode or installed.memory_exceeded:
        raise HarnessError("patched editable install failed")
    compiled = run_monitored([str(python), "-m", "py_compile", str(work / "aider" / "worktree_plan_adapter.py")], cwd=work, env=env, timeout=60)
    summary["compile_probe"] = {"exit_code": compiled.returncode, "peak_pss_bytes": compiled.peak_pss_bytes}
    if compiled.returncode:
        raise HarnessError("fixed adapter does not compile")
    entry = run([str(python), "-c", f"import importlib.util,sys;sys.exit(0 if importlib.util.find_spec('{ENTRY_MODULE}') else 1)"], cwd=work, env=env)
    summary["entry_probe"] = {"exit_code": entry.returncode}
    if entry.returncode:
        raise HarnessError("fixed adapter module is not importable")
    candidate_tests = [str(work / path) for path in paths if path.startswith("tests/") and path.endswith(".py")]
    tested = run_monitored([str(python), "-m", "pytest", "-q", *candidate_tests], cwd=work, env=env, timeout=180)
    summary["candidate_authored_tests"] = {
        "gating": False,
        "exit_code": tested.returncode,
        "peak_pss_bytes": tested.peak_pss_bytes,
        "stdout_tail": redact(tested.stdout, (work,)),
    }
    baseline_gate = run_monitored([str(python), "-m", "pytest", "-q", str(work / "tests" / "basic" / "test_repo.py")], cwd=work, env=env, timeout=180)
    summary["pinned_compatibility_tests"] = {
        "gating": False,
        "exit_code": baseline_gate.returncode,
        "peak_pss_bytes": baseline_gate.peak_pss_bytes,
        "stdout_tail": redact(baseline_gate.stdout, (work,)),
    }
    return work, python, paths


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--repository", type=Path, required=True)
    parser.add_argument("--patch", type=Path, required=True)
    parser.add_argument("--submission", type=Path, required=True)
    parser.add_argument("--cases-root", type=Path, required=True)
    parser.add_argument("--python-env", type=Path, required=True)
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--skip-prepare", action="store_true", help="self-test only: use an already prepared isolated env")
    args = parser.parse_args()
    for name in ("repository", "patch", "submission", "cases_root", "python_env", "output_dir"):
        setattr(args, name, getattr(args, name).resolve())
    protected = (args.repository, args.patch, args.submission, args.cases_root, args.python_env, PACKAGE)
    if any(args.output_dir == path or args.output_dir in path.parents for path in protected):
        parser.error("output directory must not equal or contain a protected input")
    if args.output_dir.exists() and any(args.output_dir.iterdir()):
        parser.error("output directory must be absent or empty; evaluator never overwrites user data")
    args.output_dir.mkdir(parents=True, exist_ok=True)
    started = time.monotonic()
    summary: dict[str, Any] = {"schema_version": 3, "valid": False, "cases": {}, "score": {}, "errors": []}
    try:
        validate_case_layout(args.cases_root)
        isolation = public_isolation_findings(PACKAGE)
        summary["public_isolation"] = {"valid": not isolation, "errors": isolation}
        if isolation:
            raise HarnessError("public runner imports or references hidden evaluator material")
        work, python, _paths = prepare(args, summary)
        for case_id in CASE_IDS:
            if time.monotonic() - started > 600:
                summary["cases"][case_id] = {"case_id": case_id, "valid": False, "score": 0, "maximum": 100, "assertions": [], "errors": [{"type": "Timeout", "message": "suite 600-second budget exhausted"}]}
                continue
            result = evaluate(case_id, python, work, args.output_dir / "runs" / case_id)
            manifest_errors = validate_result_manifest(result) if result.get("valid") else []
            if manifest_errors:
                result = {"case_id": case_id, "valid": False, "score": 0, "maximum": 100, "assertions": [], "errors": [{"type": "HarnessError", "message": "; ".join(manifest_errors)}]}
            summary["cases"][case_id] = result
        summary["score"] = aggregate_results(summary["cases"])
        summary["valid"] = True
    except Exception as exc:
        summary["errors"].append({"type": type(exc).__name__, "message": redact(str(exc), (args.repository, args.output_dir, args.python_env))})
        summary["score"] = {"case_scores": {case_id: 0 for case_id in CASE_IDS}, "mean_case_score": 0, "zero_rule": "suite validity gate"}
    summary["duration_seconds"] = round(time.monotonic() - started, 3)
    write_json(args.output_dir / "run_summary.json", summary)
    print(json.dumps(summary, indent=2))
    return 0 if summary["valid"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
