#!/usr/bin/env python3
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import signal
import shutil
import subprocess
import tarfile
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable

TASK = "openhands-effect-recovery-ledger-edit-v1"
ROOT = Path(__file__).resolve().parents[1]
ENV_ROOT = Path(os.environ.get("OPENHANDS_BENCH_ENV", f"@@AGENTSWE_ENVS@@/{TASK}"))
NPM_CACHE = ENV_ROOT / "npm-cache"
TOOL_ROOT = ENV_ROOT / "npm-tooling"
BASELINE_INSTALL = ENV_ROOT / "baseline-install"
MAX_EVIDENCE = 900
PROCESS_TREE_MEMORY_LIMIT_BYTES = 8 * 1024 * 1024 * 1024
PUBLIC_BASELINE_TESTS = (
    "__tests__/api/agent-server-conversation-service.test.ts",
    "__tests__/api/runtime-service/agent-server-runtime-service.test.ts",
    "__tests__/stores/use-event-store.test.ts",
)
SECRET_PATTERNS = (
    re.compile(r"AKIA[A-Z0-9_]{8,}"),
    re.compile(r"(?i)bearer\s+[A-Za-z0-9._-]+"),
    re.compile(r"PUBLIC_FIXTURE_SECRET"),
    re.compile(r"(?i)(?:claim|fencing|grant)[_-]?token[\"']?\s*[:=]\s*[\"']?[A-Za-z0-9._:-]{8,}"),
)
FORBIDDEN_ENV = ("DEEPSEEK_API_KEY", "GATEWAY_API_KEY", "SERPER_TOKEN", "OPENAI_API_KEY", "ANTHROPIC_API_KEY")
ALLOWED_PREFIXES = (
    "src/api/conversation-service/",
    "src/api/runtime-service/",
    "src/api/recovery/",
    "src/stores/",
    "src/types/agent-server/",
    "src/hooks/",
    "src/components/features/conversation/",
    "__tests__/",
)


class HarnessError(RuntimeError):
    pass


def redact(value: str) -> str:
    value = value.replace(str(ENV_ROOT), "<TASK_ENV>")
    value = value.replace(str(ROOT), "<PUBLIC_PACKAGE>")
    for pattern in SECRET_PATTERNS:
        value = pattern.sub("[REDACTED-FIXTURE]", value)
    return value[:MAX_EVIDENCE]


@dataclass
class CommandResult:
    command: list[str]
    cwd: str
    exit_code: int
    duration_seconds: float
    stdout: str
    stderr: str
    peak_tree_memory_bytes: int = 0
    memory_exceeded: bool = False

    def evidence(self) -> dict[str, Any]:
        return {
            "command": " ".join(self.command),
            "cwd": self.cwd,
            "exit_code": self.exit_code,
            "duration_seconds": self.duration_seconds,
            "stdout": redact(self.stdout),
            "stderr": redact(self.stderr),
            "peak_tree_memory_mb": round(self.peak_tree_memory_bytes / 1024 / 1024, 2),
            "memory_limit_mb": PROCESS_TREE_MEMORY_LIMIT_BYTES // 1024 // 1024,
            "memory_exceeded": self.memory_exceeded,
        }


def npm_env() -> dict[str, str]:
    env = dict(os.environ)
    for key in list(env):
        if key.lower() in {"http_proxy", "https_proxy", "all_proxy", "npm_config_proxy", "npm_config_https_proxy"}:
            env.pop(key, None)
    env.update({"npm_config_cache": str(NPM_CACHE), "npm_config_audit":"false",
        "npm_config_fund":"false", "npm_config_update_notifier":"false",
        "NO_PROXY":"localhost,127.0.0.1,::1", "CI":"1",
        "npm_config_offline":"true", "npm_config_fetch_retries":"0"})
    for key in FORBIDDEN_ENV:
        env.pop(key, None)
    return env


def process_tree_pss_bytes(root_pid: int) -> int:
    parents: dict[int, int] = {}
    for entry in Path("/proc").iterdir():
        if not entry.name.isdigit():
            continue
        try:
            tail = (entry / "stat").read_text().rpartition(")")[2].split()
            parents[int(entry.name)] = int(tail[1])
        except (OSError, ValueError, IndexError):
            continue
    selected = {root_pid}
    changed = True
    while changed:
        changed = False
        for pid, parent in parents.items():
            if parent in selected and pid not in selected:
                selected.add(pid)
                changed = True
    total_kib = 0
    for pid in selected:
        try:
            lines = (Path("/proc") / str(pid) / "smaps_rollup").read_text().splitlines()
            total_kib += int(next(line.split()[1] for line in lines if line.startswith("Pss:")))
        except (OSError, ValueError, StopIteration):
            try:
                lines = (Path("/proc") / str(pid) / "status").read_text().splitlines()
                total_kib += int(next(line.split()[1] for line in lines if line.startswith("VmRSS:")))
            except (OSError, ValueError, StopIteration):
                continue
    return total_kib * 1024


def terminate_process_group(process: subprocess.Popen[Any]) -> None:
    try:
        os.killpg(process.pid, signal.SIGTERM)
        process.wait(timeout=2)
    except (ProcessLookupError, subprocess.TimeoutExpired):
        try:
            os.killpg(process.pid, signal.SIGKILL)
            process.wait(timeout=2)
        except ProcessLookupError:
            pass
        except subprocess.TimeoutExpired:
            pass


def run(command: Iterable[str], cwd: Path, *, timeout: int = 600, env: dict[str, str] | None = None) -> CommandResult:
    argv = [str(x) for x in command]
    started = time.monotonic()
    peak = process_tree_pss_bytes(os.getpid())
    memory_exceeded = False
    timed_out = False
    with tempfile.TemporaryFile(mode="w+t", encoding="utf-8") as stdout_file, tempfile.TemporaryFile(mode="w+t", encoding="utf-8") as stderr_file:
        process = subprocess.Popen(
            argv,
            cwd=cwd,
            env=env,
            text=True,
            stdout=stdout_file,
            stderr=stderr_file,
            start_new_session=True,
        )
        while process.poll() is None:
            peak = max(peak, process_tree_pss_bytes(os.getpid()))
            if peak > PROCESS_TREE_MEMORY_LIMIT_BYTES:
                memory_exceeded = True
                terminate_process_group(process)
                break
            if time.monotonic() - started > timeout:
                timed_out = True
                terminate_process_group(process)
                break
            time.sleep(0.1)
        peak = max(peak, process_tree_pss_bytes(os.getpid()))
        exit_code = 137 if memory_exceeded else 124 if timed_out else int(process.returncode or 0)
        stdout_file.seek(0); stderr_file.seek(0)
        stdout = stdout_file.read(); stderr = stderr_file.read()
    if memory_exceeded:
        stderr += f"\nactual process-tree PSS exceeded {PROCESS_TREE_MEMORY_LIMIT_BYTES} bytes"
    if timed_out:
        stderr += f"\ntimeout after {timeout}s"
    return CommandResult(
        argv, str(cwd), exit_code, round(time.monotonic() - started, 3),
        stdout, stderr, peak, memory_exceeded,
    )


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def remove_existing_tree(path: Path) -> None:
    for attempt in range(1, 4):
        if not path.exists():
            return
        try:
            shutil.rmtree(path)
        except FileNotFoundError:
            return
        except OSError as exc:
            if attempt == 3:
                raise HarnessError(f"failed to remove existing worktree: {exc}") from exc
            time.sleep(0.25)
            continue
        if not path.exists():
            return
        if attempt == 3:
            raise HarnessError(f"failed to remove existing worktree: {path}")
        time.sleep(0.25)


def ensure_npm() -> tuple[list[str], list[dict[str, Any]]]:
    NPM_CACHE.mkdir(parents=True, exist_ok=True)
    node = run(["node", "--version"], ROOT, timeout=10)
    log = [node.evidence()]
    match = re.fullmatch(r"v(\d+)\.(\d+)\.(\d+)", node.stdout.strip())
    if node.exit_code or not match or tuple(map(int, match.groups())) < (22, 12, 0):
        raise HarnessError("Node >=22.12.0 is required")
    cli = TOOL_ROOT / "node_modules/npm/bin/npm-cli.js"
    if not cli.is_file():
        raise HarnessError("Evaluator setup is missing pinned npm 10.5.0; public helper never provisions dependencies")
    npm = ["node", str(cli)]
    version = run([*npm, "--version"], ROOT, timeout=10, env=npm_env())
    log.append(version.evidence())
    if version.exit_code or version.stdout.strip() != "10.5.0":
        raise HarnessError("task-local npm version is not exactly 10.5.0")
    return npm, log


def extract_pristine(repository: Path, destination: Path) -> list[dict[str, Any]]:
    remove_existing_tree(destination)
    destination.mkdir(parents=True)
    log: list[dict[str, Any]] = []
    if (repository / ".git").exists():
        archive = destination.parent / "source.tar"
        result = run(["git", "archive", "--format=tar", "-o", str(archive), "HEAD"], repository, timeout=60)
        log.append(result.evidence())
        if result.exit_code:
            raise HarnessError("failed to archive pristine pinned source")
        with tarfile.open(archive) as stream:
            stream.extractall(destination, filter="data")
        archive.unlink()
    else:
        started = time.monotonic()
        shutil.copytree(repository, destination, dirs_exist_ok=True, ignore=shutil.ignore_patterns("node_modules"))
        log.append({"command": "copy pristine pinned source", "cwd": str(repository), "exit_code": 0, "duration_seconds": round(time.monotonic() - started, 3), "stdout": "", "stderr": ""})
    init = run(["git", "init", "-q"], destination, timeout=30)
    log.append(init.evidence())
    if init.exit_code:
        raise HarnessError("failed to initialize worktree")
    return log


def changed_paths(patch: Path) -> list[str]:
    if not patch.is_file() or patch.stat().st_size == 0 or patch.stat().st_size > 5_000_000:
        raise HarnessError("solution.patch is missing, empty, or oversized")
    text = patch.read_text(encoding="utf-8")
    entries = re.findall(r"^diff --git a/(.+?) b/(.+?)$", text, re.M)
    paths = sorted({destination for _source, destination in entries})
    if not paths:
        raise HarnessError("solution.patch contains no Git file entries")
    for path in paths:
        if path.startswith("/") or ".." in Path(path).parts or not path.startswith(ALLOWED_PREFIXES):
            raise HarnessError(f"patch changes forbidden path: {path}")
    required = {
        "src/api/recovery/recovery-evaluator-adapter.ts",
        "src/stores/recovery-store.ts",
        "src/components/features/conversation/recovery-status.tsx",
    }
    if not required.issubset(paths):
        raise HarnessError("patch is missing one or more frozen integration entry paths")
    return paths


def validate_reports(submission: Path, paths: list[str]) -> dict[str, Any]:
    findings: dict[str, Any] = {"valid": True, "errors": []}
    for name in ("edit_report.json", "run_report.json"):
        path = submission / name
        try:
            value = json.loads(path.read_text(encoding="utf-8"))
        except Exception as exc:
            raise HarnessError(f"invalid {name}: {exc}") from exc
        if not isinstance(value, dict) or value.get("schema_version") != "1.0":
            raise HarnessError(f"{name} must use schema_version 1.0")
        if any(pattern.search(json.dumps(value)) for pattern in SECRET_PATTERNS):
            raise HarnessError(f"{name} contains secret-like fixture data")
        findings[name] = value

    edit = findings["edit_report.json"]
    required_edit = {"feature_summary", "changed_paths", "commands_run", "compatibility_notes", "limitations"}
    if not required_edit.issubset(edit):
        raise HarnessError("edit_report.json is missing required fields")
    if sorted(edit["changed_paths"]) != paths:
        raise HarnessError("edit_report.json changed_paths does not match solution.patch")

    run_report = findings["run_report.json"]
    required_run = {"status", "artifact_paths", "errors", "runtime_seconds", "peak_memory_mb", "deepseek", "gateway", "gateway_image", "serper", "web_retrieval"}
    if not required_run.issubset(run_report):
        raise HarnessError("run_report.json is missing required fields")
    for key in ("deepseek", "gateway", "gateway_image", "serper", "web_retrieval"):
        if not isinstance(run_report[key], int) or run_report[key] < 0:
            raise HarnessError(f"run_report.json {key} must be a nonnegative integer")
    return findings


def apply_patch_once(worktree: Path, patch: Path) -> list[dict[str, Any]]:
    log = []
    for command in (["git", "apply", "--check", str(patch)], ["git", "apply", str(patch)]):
        result = run(command, worktree, timeout=60)
        log.append(result.evidence())
        if result.exit_code:
            raise HarnessError("solution.patch does not apply once to pristine source")
    second = run(["git", "apply", "--check", str(patch)], worktree, timeout=60)
    log.append({**second.evidence(), "expected_failure": True})
    if second.exit_code == 0:
        raise HarnessError("solution.patch is unexpectedly applicable twice")
    return log


def prepare_dependencies(worktree: Path, npm: list[str]) -> list[dict[str, Any]]:
    lock_digest = sha256(worktree / "package-lock.json")
    marker = BASELINE_INSTALL / ".benchmark-lock-sha256"
    baseline_modules = BASELINE_INSTALL / "node_modules"
    if not (marker.is_file() and marker.read_text().strip() == lock_digest
            and baseline_modules.is_dir()
            and sha256(BASELINE_INSTALL / "package-lock.json") == lock_digest
            and sha256(BASELINE_INSTALL / "package.json") == sha256(worktree / "package.json")):
        raise HarnessError("Evaluator setup lacks exact-lock prewarmed dependencies; network preparation is not part of this public helper")
    (worktree / "node_modules").mkdir()
    copied = run(["cp", "-a", "--reflink=auto", f"{baseline_modules}/.", str(worktree / "node_modules")], worktree, timeout=600)
    return [{**copied.evidence(), "source": "prewarmed task-local node_modules",
             "lock_digest_matched": True, "dependency_network_attempts": 0}]


def prepare_generated_sources(worktree: Path, npm: list[str]) -> list[dict[str, Any]]:
    log: list[dict[str, Any]] = []
    make_i18n = run([*npm, "run", "make-i18n"], worktree, timeout=120, env=npm_env())
    log.append(make_i18n.evidence())
    if make_i18n.exit_code:
        return log

    typegen = run([str(worktree / "node_modules/.bin/react-router"), "typegen"], worktree, timeout=120, env=npm_env())
    log.append(typegen.evidence())
    if typegen.exit_code:
        return log

    required = (
        worktree / "src/i18n/declaration.ts",
        worktree / "public/locales/en/openhands.json",
        worktree / ".react-router/types",
    )
    missing = [str(path.relative_to(worktree)) for path in required if not path.exists()]
    log.append(
        {
            "command": "validate generated source prerequisites",
            "cwd": str(worktree),
            "exit_code": 1 if missing else 0,
            "duration_seconds": 0,
            "stdout": "generated source prerequisites exist" if not missing else "",
            "stderr": f"missing generated paths: {', '.join(missing)}" if missing else "",
        }
    )
    return log


def run_typecheck_gate(worktree: Path) -> CommandResult:
    return run([str(worktree / "node_modules/.bin/tsc"), "--noEmit", "--skipLibCheck", "--pretty", "false"], worktree, timeout=180, env=npm_env())


def summarize_vitest_report(report: Path) -> dict[str, Any]:
    summary: dict[str, Any] = {"path": str(report), "exists": report.is_file()}
    if not report.is_file():
        return summary
    summary["bytes"] = report.stat().st_size
    try:
        payload = json.loads(report.read_text(encoding="utf-8"))
    except Exception as exc:
        summary["parse_error"] = redact(str(exc))
        return summary

    test_results = payload.get("testResults", [])
    summary.update(
        {
            "success": payload.get("success"),
            "num_total_tests": payload.get("numTotalTests"),
            "num_passed_tests": payload.get("numPassedTests"),
            "num_failed_tests": payload.get("numFailedTests"),
            "num_pending_tests": payload.get("numPendingTests"),
            "num_total_test_suites": payload.get("numTotalTestSuites"),
            "num_passed_test_suites": payload.get("numPassedTestSuites"),
            "num_failed_test_suites": payload.get("numFailedTestSuites"),
            "test_result_files": len(test_results) if isinstance(test_results, list) else None,
        }
    )
    failures: list[dict[str, Any]] = []
    if isinstance(test_results, list):
        for file_result in test_results:
            if not isinstance(file_result, dict):
                continue
            file_name = str(file_result.get("name") or file_result.get("testFilePath") or "")
            for assertion in file_result.get("assertionResults") or []:
                if not isinstance(assertion, dict) or assertion.get("status") in {"passed", "skipped", "todo", "disabled"}:
                    continue
                title = str(assertion.get("fullName") or assertion.get("title") or "")
                detail = "\n".join(str(x) for x in assertion.get("failureMessages") or ["failed"])
                failures.append({"file": redact(file_name), "title": redact(title), "status": assertion.get("status"), "evidence": redact(detail)})
    summary["failures"] = failures[:3]
    summary["failure_evidence_truncated"] = len(failures) > 3
    return summary


def run_vitest_json(worktree: Path, tests: Iterable[str], report: Path, *, timeout: int = 120) -> dict[str, Any]:
    report.parent.mkdir(parents=True, exist_ok=True)
    command = run([str(worktree / "node_modules/.bin/vitest"), "run", *tests, "--reporter=json", f"--outputFile={report}"], worktree, timeout=timeout, env={**npm_env(), "VITEST": "1"})
    evidence = command.evidence()
    evidence["report"] = summarize_vitest_report(report)
    return evidence


def run_public_baseline_vitest_gate(worktree: Path) -> dict[str, Any]:
    report = worktree / "__benchmark_reports__" / "public-baseline-vitest.json"
    return run_vitest_json(worktree, PUBLIC_BASELINE_TESTS, report, timeout=180)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--case", choices=("dev_001", "dev_002"), required=True)
    parser.add_argument("--submission", type=Path, required=True)
    parser.add_argument("--output-dir", type=Path, required=True)
    args = parser.parse_args()

    output = args.output_dir.resolve()
    shutil.rmtree(output, ignore_errors=True)
    output.mkdir(parents=True)
    summary: dict[str, Any] = {"valid": False, "case": args.case, "commands": [], "errors": []}
    started = time.monotonic()

    try:
        submission = args.submission.resolve()
        patch = submission / "solution.patch"
        paths = changed_paths(patch)
        summary["report_validation"] = validate_reports(submission, paths)

        work = output / "worktree"
        summary["commands"] += extract_pristine(ROOT / "input/repository", work)
        summary["commands"] += apply_patch_once(work, patch)

        npm, log = ensure_npm()
        summary["commands"] += log

        dependency_log = prepare_dependencies(work, npm)
        summary["commands"] += dependency_log
        if not dependency_log or dependency_log[-1]["exit_code"] != 0:
            raise HarnessError("isolated npm preparation failed")

        generated_log = prepare_generated_sources(work, npm)
        summary["commands"] += generated_log
        if not generated_log or generated_log[-1]["exit_code"] != 0:
            raise HarnessError("repository generated-source preparation failed")

        target = work / "__benchmark_public__"
        target.mkdir()
        shutil.copy2(ROOT / "dev_cases/helpers.ts", target / "helpers.ts")
        shutil.copy2(
            ROOT / "dev_cases/workspace-helpers.ts",
            target / "workspace-helpers.ts",
        )
        shutil.copy2(
            ROOT / "dev_cases/recovery-contract.type-test.ts",
            target / "recovery-contract.type-test.ts",
        )
        shutil.copytree(ROOT / f"dev_cases/{args.case}", target / args.case)

        typecheck = run_typecheck_gate(work)
        summary["commands"].append(typecheck.evidence())
        if typecheck.exit_code:
            raise HarnessError("focused TypeScript check failed")

        baseline = run_public_baseline_vitest_gate(work)
        summary["commands"].append(baseline)
        if baseline["exit_code"]:
            raise HarnessError("focused baseline Vitest compatibility checks failed")

        test = target / args.case / "public.test.ts"
        result = run_vitest_json(work, [str(test.relative_to(work))], target / f"{args.case}-vitest.json", timeout=120)
        summary["commands"].append(result)
        summary["valid"] = result["exit_code"] == 0
        if result["exit_code"]:
            summary["errors"].append({"type": "PublicCaseFailure", "message": redact(result.get("stderr") or result.get("stdout") or json.dumps(result.get("report", {})))})
    except Exception as exc:
        summary["errors"].append({"type": type(exc).__name__, "message": redact(str(exc))})

    summary["duration_seconds"] = round(time.monotonic() - started, 3)
    summary["peak_tree_memory_mb"] = max(
        (float(item.get("peak_tree_memory_mb", 0)) for item in summary["commands"]),
        default=0,
    )
    summary["memory_limit_mb"] = PROCESS_TREE_MEMORY_LIMIT_BYTES // 1024 // 1024
    (output / "run_summary.json").write_text(json.dumps(summary, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(summary, indent=2))
    return 0 if summary["valid"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
