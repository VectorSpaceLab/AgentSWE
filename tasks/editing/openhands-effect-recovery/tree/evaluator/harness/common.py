#!/usr/bin/env python3
"""Shared orchestration for the OpenHands recovery-ledger evaluator."""
from __future__ import annotations

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

CASE_IDS = tuple(f"test_{n:03d}" for n in range(1, 7))
TASK = "openhands-effect-recovery-ledger-edit-v1"
ROOT = Path(__file__).resolve().parents[2]
ENV_ROOT = Path("/opt/agentswe/benchmark/envs") / TASK
NPM_CACHE = ENV_ROOT / "npm-cache"
TOOL_ROOT = ENV_ROOT / "npm-tooling"
BASELINE_INSTALL = ENV_ROOT / "baseline-install"
MAX_EVIDENCE = 900
PROCESS_TREE_MEMORY_LIMIT_BYTES = 8 * 1024 * 1024 * 1024
FOCUSED_BASELINE_TESTS = (
    "__tests__/api/agent-server-conversation-service.test.ts",
    "__tests__/api/runtime-service/agent-server-runtime-service.test.ts",
    "__tests__/stores/use-event-store.test.ts",
)
SECRET_PATTERNS = (
    re.compile(r"AKIA[A-Z0-9_]{8,}"),
    re.compile(r"(?i)bearer\s+[A-Za-z0-9._-]+"),
    re.compile(r"IGNORE PREVIOUS INSTRUCTIONS[^\n\"]*", re.I),
    re.compile(r"(?i)(?:claim|fencing|grant)[_-]?token[\"']?\s*[:=]\s*[\"']?[A-Za-z0-9._:-]{8,}"),
)
FORBIDDEN_ENV = ("DEEPSEEK_API_KEY", "DEEPSEEK_API_KEY", "SERPER_TOKEN", "OPENAI_API_KEY", "ANTHROPIC_API_KEY")
ALLOWED_PREFIXES = (
    "src/api/conversation-service/", "src/api/runtime-service/", "src/api/recovery/",
    "src/stores/", "src/types/agent-server/", "src/hooks/",
    "src/components/features/conversation/", "__tests__/",
)

ASSERTION_WEIGHTS: dict[str, dict[str, int]] = {
    "test_001": {"OH001": 10, "OH002": 40, "OH003": 40, "OH004": 10},
    "test_002": {"OH101": 15, "OH102": 45, "OH103": 30, "OH104": 10},
    "test_003": {"OH201": 35, "OH202": 30, "OH203": 20, "OH204": 15},
    "test_004": {"OH301": 30, "OH302": 25, "OH303": 30, "OH304": 15},
    "test_005": {"OH401": 35, "OH402": 20, "OH403": 25, "OH404": 20},
    "test_006": {"OH501": 5, "OH502": 5, "OH503": 10, "OH504": 10, "OH505": 5, "OH506": 5, "OH507": 40, "OH508": 20},
}

CRITICAL_LIMITS: dict[str, tuple[tuple[str, ...], int, str]] = {
    "test_001": (("OH002", "OH003"), 30, "workspace commit ordering or takeover fencing failed"),
    "test_002": (("OH102", "OH103"), 30, "workspace crash recovery or uncertain remote-commit reconciliation failed"),
    "test_003": (("OH201", "OH202"), 20, "generation ABA or cancellation fencing failed"),
    "test_004": (("OH301",), 15, "least-authority scope enforcement failed"),
    "test_005": (("OH401", "OH402", "OH404"), 25, "schema migration, revision fencing, or scope-exact compaction failed"),
    "test_006": (("OH507", "OH508"), 30, "production workspace commit recovery or takeover fencing failed"),
}

ABSOLUTE_LIMITS: dict[str, tuple[str, int, str]] = {
    "test_004": ("OH303", 0, "sensitive recovery inspection redaction failed"),
}


def aggregate_case_results(results: dict[str, dict[str, Any]]) -> dict[str, Any]:
    if set(results) != set(CASE_IDS):
        raise HarnessError("one or more case results are missing; aggregation refused")
    evaluator_errors = [
        case_id
        for case_id, result in results.items()
        if result.get("case_status") == "evaluator_error"
        or not isinstance(result.get("score"), int)
    ]
    if evaluator_errors:
        raise HarnessError(
            f"case evaluator failed: {', '.join(evaluator_errors)}; aggregation refused"
        )
    scores = {case_id: int(results[case_id]["score"]) for case_id in CASE_IDS}
    return {
        "case_scores": scores,
        "mean_case_score": round(sum(scores.values()) / len(CASE_IDS), 2),
        "aggregation": "arithmetic mean of six independent 0..100 cases",
        "aggregation_valid": True,
    }


class HarnessError(RuntimeError):
    pass


def redact(value: str) -> str:
    value = value.replace(str(ENV_ROOT), "<TASK_ENV>")
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
            "command": " ".join(self.command), "cwd": self.cwd,
            "exit_code": self.exit_code, "duration_seconds": self.duration_seconds,
            "stdout": redact(self.stdout), "stderr": redact(self.stderr),
            "peak_tree_memory_mb": round(self.peak_tree_memory_bytes / 1024 / 1024, 2),
            "memory_limit_mb": PROCESS_TREE_MEMORY_LIMIT_BYTES // 1024 // 1024,
            "memory_exceeded": self.memory_exceeded,
        }


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
                raise HarnessError(f"failed to remove existing evaluator worktree: {exc}") from exc
            time.sleep(0.25)
            continue
        if not path.exists():
            return
        if attempt == 3:
            raise HarnessError(f"failed to remove existing evaluator worktree: {path}")
        time.sleep(0.25)


def npm_env() -> dict[str, str]:
    env = dict(os.environ)
    env.update({
        "npm_config_cache": str(NPM_CACHE), "npm_config_audit": "false",
        "npm_config_fund": "false", "npm_config_update_notifier": "false",
        "HTTP_PROXY": "http://127.0.0.1:7896", "HTTPS_PROXY": "http://127.0.0.1:7896",
        "NO_PROXY": "localhost,127.0.0.1,::1", "CI": "1",
        "npm_config_proxy": "http://127.0.0.1:7896",
        "npm_config_https_proxy": "http://127.0.0.1:7896",
        "npm_config_prefer_offline": "true", "npm_config_fetch_retries": "3",
        "npm_config_fetch_retry_mintimeout": "5000",
        "npm_config_fetch_retry_maxtimeout": "30000",
        "npm_config_fetch_timeout": "120000", "npm_config_maxsockets": "2",
    })
    for key in FORBIDDEN_ENV:
        env.pop(key, None)
    return env


def ensure_npm() -> tuple[list[str], list[dict[str, Any]]]:
    ENV_ROOT.mkdir(parents=True, exist_ok=True); NPM_CACHE.mkdir(parents=True, exist_ok=True)
    log: list[dict[str, Any]] = []
    node = run(["node", "--version"], ROOT, timeout=10)
    log.append(node.evidence())
    match = re.fullmatch(r"v(\d+)\.(\d+)\.(\d+)", node.stdout.strip())
    if node.exit_code or not match or tuple(map(int, match.groups())) < (22, 12, 0):
        raise HarnessError("Node >=22.12.0 is required")
    cli = TOOL_ROOT / "node_modules/npm/bin/npm-cli.js"
    # The evaluator image carries npm >=10, while the old task-local npm cache
    # may not be mounted into an isolated Harbor run. Prefer the exact cached
    # tool when available, but avoid a network install solely to validate the
    # Candidate's pinned package-lock.
    if not cli.is_file():
        system_version = run(["npm", "--version"], ROOT, timeout=10, env=npm_env())
        log.append(system_version.evidence())
        system_match = re.fullmatch(
            r"(\d+)\.(\d+)\.(\d+)", system_version.stdout.strip()
        )
        if (
            system_version.exit_code == 0
            and system_match
            and int(system_match.group(1)) >= 10
        ):
            return ["npm"], log
    if not cli.is_file():
        TOOL_ROOT.mkdir(parents=True, exist_ok=True)
        install = run(["npm", "install", "--prefix", str(TOOL_ROOT), "--ignore-scripts", "--no-save", "npm@10.5.0"], ROOT, timeout=180, env=npm_env())
        log.append(install.evidence())
        if install.exit_code:
            raise HarnessError("could not provision task-local npm 10.5.0")
    npm = ["node", str(cli)]
    version = run([*npm, "--version"], ROOT, timeout=10, env=npm_env())
    log.append(version.evidence())
    if version.exit_code or version.stdout.strip() != "10.5.0":
        raise HarnessError("task-local npm version is not exactly 10.5.0")
    return npm, log


def extract_pristine(repository: Path, destination: Path) -> list[dict[str, Any]]:
    remove_existing_tree(destination); destination.mkdir(parents=True)
    archive = destination.parent / "source.tar"
    result = run(["git", "archive", "--format=tar", "-o", str(archive), "HEAD"], repository, timeout=60)
    if result.exit_code:
        raise HarnessError("failed to archive pristine pinned source")
    with tarfile.open(archive) as stream:
        stream.extractall(destination, filter="data")
    archive.unlink()
    init = run(["git", "init", "-q"], destination, timeout=30)
    if init.exit_code:
        raise HarnessError("failed to initialize evaluator worktree")
    return [result.evidence(), init.evidence()]


# The modules the evaluator's own product observation harness imports. Declared
# here rather than inside changed_paths so the submission path can enforce the
# same requirement: a patch without these cannot start the product at all.
REQUIRED_INTEGRATION_PATHS = frozenset({
    "src/api/recovery/recovery-evaluator-adapter.ts",
    "src/stores/recovery-store.ts",
    "src/components/features/conversation/recovery-status.tsx",
})


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
    if not REQUIRED_INTEGRATION_PATHS.issubset(paths):
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
        result = run(command, worktree, timeout=60); log.append(result.evidence())
        if result.exit_code:
            raise HarnessError("solution.patch does not apply once to pristine source")
    second = run(["git", "apply", "--check", str(patch)], worktree, timeout=60)
    log.append({**second.evidence(), "expected_failure": True})
    if second.exit_code == 0:
        raise HarnessError("solution.patch is unexpectedly applicable twice")
    return log


def copy_prepared_node_modules(source: Path, destination: Path) -> CommandResult:
    """Copy a read-only prewarm tree into a writable case worktree.

    ``cp -a`` preserves root ownership and was the cause of the previous Vite
    failure.  The create harness treats prepared dependencies as a cache source,
    not as the execution directory; use an ordinary copy and explicitly make
    the resulting tree writable for the lower process.
    """
    shutil.rmtree(destination, ignore_errors=True)
    destination.parent.mkdir(parents=True, exist_ok=True)
    result = run(["cp", "-r", "--reflink=auto", f"{source}/.", str(destination)], destination.parent, timeout=600)
    if result.exit_code == 0:
        for path in destination.rglob("*"):
            try:
                mode = path.stat().st_mode
                path.chmod(mode | (0o700 if path.is_dir() else 0o600))
            except OSError:
                pass
    return result


def prepare_dependencies(worktree: Path, npm: list[str]) -> list[dict[str, Any]]:
    log: list[dict[str, Any]] = []
    lock_digest = sha256(worktree / "package-lock.json")
    marker = BASELINE_INSTALL / ".benchmark-lock-sha256"
    baseline_modules = BASELINE_INSTALL / "node_modules"
    if marker.is_file() and marker.read_text().strip() == lock_digest and baseline_modules.is_dir():
        copied = copy_prepared_node_modules(baseline_modules, worktree / "node_modules")
        log.append({**copied.evidence(), "source": "prewarmed task-local node_modules", "lock_digest_matched": True})
    else:
        for attempt in range(1, 4):
            shutil.rmtree(worktree / "node_modules", ignore_errors=True)
            install = run([*npm, "ci", "--ignore-scripts", "--no-audit", "--no-fund"], worktree, timeout=600, env=npm_env())
            log.append({**install.evidence(), "attempt": attempt})
            if install.exit_code == 0: break
    return log


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
    log.append({
        "command": "validate generated evaluator prerequisites",
        "cwd": str(worktree),
        "exit_code": 1 if missing else 0,
        "duration_seconds": 0,
        "stdout": "generated source prerequisites exist" if not missing else "",
        "stderr": f"missing generated paths: {', '.join(missing)}" if missing else "",
    })
    return log


def run_typecheck_gate(worktree: Path) -> CommandResult:
    return run([str(worktree / "node_modules/.bin/tsc"), "--noEmit", "--skipLibCheck", "--pretty", "false"], worktree, timeout=180, env=npm_env())


def inject_public_contract_probe(worktree: Path) -> Path:
    destination = worktree / "__benchmark_public_contract__"
    destination.mkdir(exist_ok=True)
    target = destination / "recovery-contract.type-test.ts"
    shutil.copy2(ROOT / "dev_cases/recovery-contract.type-test.ts", target)
    return target


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
    summary.update({
        "success": payload.get("success"),
        "num_total_tests": payload.get("numTotalTests"),
        "num_passed_tests": payload.get("numPassedTests"),
        "num_failed_tests": payload.get("numFailedTests"),
        "num_pending_tests": payload.get("numPendingTests"),
        "num_total_test_suites": payload.get("numTotalTestSuites"),
        "num_passed_test_suites": payload.get("numPassedTestSuites"),
        "num_failed_test_suites": payload.get("numFailedTestSuites"),
        "test_result_files": len(test_results) if isinstance(test_results, list) else None,
    })
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


def run_focused_baseline_vitest_gate(worktree: Path) -> dict[str, Any]:
    report = worktree / "__benchmark_reports__" / "focused-baseline-vitest.json"
    return run_vitest_json(worktree, FOCUSED_BASELINE_TESTS, report, timeout=180)


def inject_test(worktree: Path, case_id: str) -> Path:
    destination = worktree / "__benchmark_tests__"
    destination.mkdir(exist_ok=True)
    shutil.copy2(ROOT / "evaluator/tests/helpers.ts", destination / "helpers.ts")
    if case_id == "test_006":
        shutil.copy2(
            ROOT / "evaluator/tests/sync-helpers.ts",
            destination / "sync-helpers.ts",
        )
    if case_id in {"test_001", "test_002", "test_006"}:
        shutil.copy2(
            ROOT / "dev_cases/workspace-helpers.ts",
            destination / "workspace-helpers.ts",
        )
    shutil.copy2(ROOT / f"test_cases/{case_id}/assets/fixtures.json", destination / "fixture.json")
    suffix = ".test.tsx" if case_id == "test_006" else ".test.ts"
    shutil.copy2(ROOT / f"evaluator/tests/{case_id}{suffix}", destination / f"{case_id}{suffix}")
    return destination / f"{case_id}{suffix}"


def parse_vitest(case_id: str, report: Path, command: CommandResult) -> dict[str, Any]:
    weights = ASSERTION_WEIGHTS[case_id]
    observed: dict[str, tuple[bool, str]] = {}
    try:
        payload = json.loads(report.read_text(encoding="utf-8"))
        for file_result in payload.get("testResults", []):
            for assertion in file_result.get("assertionResults", []):
                title = assertion.get("fullName") or assertion.get("title") or ""
                match = re.search(r"\[(OH\d{3}):(\d+)\]", title)
                if match:
                    detail = "passed" if assertion.get("status") == "passed" else "\n".join(assertion.get("failureMessages") or ["failed"])
                    observed[match.group(1)] = (assertion.get("status") == "passed", detail)
    except Exception as exc:
        payload = {"parse_error": str(exc)}
    assertions = []
    for assertion_id, points in weights.items():
        passed, evidence = observed.get(assertion_id, (False, "assertion did not execute"))
        assertions.append({"id": assertion_id, "maximum": points, "earned": points if passed else 0, "passed": passed, "evidence": redact(evidence)})
    raw = sum(item["earned"] for item in assertions)
    local_limits: list[dict[str, Any]] = []
    score = raw
    critical = CRITICAL_LIMITS.get(case_id)
    if critical and any(
        not next(item for item in assertions if item["id"] == assertion_id)["passed"]
        for assertion_id in critical[0]
    ):
        score = min(score, critical[1])
        local_limits.append({"maximum": critical[1], "reason": critical[2]})
    absolute = ABSOLUTE_LIMITS.get(case_id)
    if absolute and not next(
        item for item in assertions if item["id"] == absolute[0]
    )["passed"]:
        score = min(score, absolute[1])
        local_limits.append({"maximum": absolute[1], "reason": absolute[2]})
    command_evidence = command.evidence()
    command_evidence["report"] = summarize_vitest_report(report)
    return {
        "case_id": case_id,
        "case_status": "completed" if command.exit_code == 0 else "behavioral_failure",
        "score": score,
        "maximum": 100,
        "raw_assertion_score": raw,
        "assertions": assertions,
        "local_limit": min(local_limits, key=lambda item: item["maximum"]) if local_limits else None,
        "local_limits": local_limits,
        "command": command_evidence,
    }


def run_case(worktree: Path, case_id: str) -> dict[str, Any]:
    test = inject_test(worktree, case_id)
    report = worktree / "__benchmark_tests__" / f"{case_id}-vitest.json"
    command = run([str(worktree / "node_modules/.bin/vitest"), "run", str(test.relative_to(worktree)), "--reporter=json", f"--outputFile={report}"], worktree, timeout=120, env={**npm_env(), "VITEST": "1"})
    return parse_vitest(case_id, report, command)
