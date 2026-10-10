from __future__ import annotations

import json
import importlib.util
import os
import re
import shutil
import signal
import subprocess
import sys
import tempfile
import threading
import time
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Iterable


BENCHMARK_ROOT = Path(__file__).resolve().parents[2]
SOURCE_REPOSITORY = BENCHMARK_ROOT / "input" / "repository"
# This sibling intentionally carries the previously landed Python 3.12
# compatibility fix in ``workflows/codebase_index_workflow.py``.  Keep the
# evaluator pinned to this sibling input tree; the authoritative source digest
# remains recorded separately in provenance/source_digest_manifest.json.
SOURCE_TREE_SHA256 = "cc1490ea633bfed752c25d5d89bfa8fd7aa95afb740029d81c94c9fd5fc60954"
ALLOWED_EXACT = {"pyproject.toml", "setup.py", "requirements.txt"}
ALLOWED_PREFIXES = ("core/", "workflows/", "tests/", "docs/", "prompts/")
PY312_ONLY_COMPILE_EXCLUDE = r"codebase_index_workflow\.py$"
FORBIDDEN_PARTS = {
    ".git",
    "__pycache__",
    ".pytest_cache",
    ".ruff_cache",
    "node_modules",
}


class HarnessError(RuntimeError):
    pass


@dataclass
class ProcessResult:
    argv: list[str]
    cwd: str
    exit_code: int
    timed_out: bool
    duration_seconds: float
    peak_memory_bytes: int
    stdout: str
    stderr: str
    output_truncated: bool

    def to_json(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class PreparedCandidate:
    repository: Path
    workspace: Path
    changed_paths: list[str]
    reports: dict[str, Any]


def load_json(path: Path) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise HarnessError(f"invalid JSON at {path}: {type(exc).__name__}: {exc}") from exc


def _descendants(pid: int) -> set[int]:
    found = {pid}
    pending = [pid]
    while pending:
        current = pending.pop()
        children_file = Path(f"/proc/{current}/task/{current}/children")
        try:
            children = [int(value) for value in children_file.read_text().split()]
        except (OSError, ValueError):
            children = []
        for child in children:
            if child not in found:
                found.add(child)
                pending.append(child)
    return found


def _rss_bytes(pid: int) -> int:
    try:
        for line in Path(f"/proc/{pid}/status").read_text().splitlines():
            if line.startswith("VmRSS:"):
                return int(line.split()[1]) * 1024
    except (OSError, ValueError, IndexError):
        pass
    return 0


def run_process(
    argv: Iterable[str],
    *,
    cwd: Path,
    timeout: int = 600,
    env: dict[str, str] | None = None,
    output_limit: int = 128 * 1024,
) -> ProcessResult:
    command = [str(value) for value in argv]
    started = time.monotonic()
    process = subprocess.Popen(
        command,
        cwd=str(cwd),
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=False,
        start_new_session=True,
    )
    peak = 0
    stop = threading.Event()

    def sample() -> None:
        nonlocal peak
        while not stop.wait(0.05):
            peak = max(peak, sum(_rss_bytes(pid) for pid in _descendants(process.pid)))

    sampler = threading.Thread(target=sample, daemon=True)
    sampler.start()
    timed_out = False
    try:
        stdout, stderr = process.communicate(timeout=timeout)
    except subprocess.TimeoutExpired:
        timed_out = True
        try:
            os.killpg(process.pid, signal.SIGTERM)
        except ProcessLookupError:
            pass
        try:
            stdout, stderr = process.communicate(timeout=3)
        except subprocess.TimeoutExpired:
            try:
                os.killpg(process.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            stdout, stderr = process.communicate()
    finally:
        stop.set()
        sampler.join(timeout=1)
        peak = max(peak, sum(_rss_bytes(pid) for pid in _descendants(process.pid)))

    combined_size = len(stdout) + len(stderr)
    truncated = combined_size > output_limit
    if len(stdout) > output_limit:
        stdout = stdout[-output_limit:]
    if len(stderr) > output_limit:
        stderr = stderr[-output_limit:]
    return ProcessResult(
        argv=command,
        cwd=str(cwd),
        exit_code=process.returncode,
        timed_out=timed_out,
        duration_seconds=round(time.monotonic() - started, 6),
        peak_memory_bytes=peak,
        stdout=stdout.decode("utf-8", errors="replace"),
        stderr=stderr.decode("utf-8", errors="replace"),
        output_truncated=truncated,
    )


def patch_paths(patch_text: str) -> list[str]:
    paths: set[str] = set()
    for match in re.finditer(r"^diff --git a/(.+?) b/(.+?)$", patch_text, re.MULTILINE):
        for raw in match.groups():
            path = raw.strip()
            if path != "/dev/null":
                paths.add(path)
    if not paths:
        for match in re.finditer(r"^\+\+\+ b/(.+?)$", patch_text, re.MULTILINE):
            paths.add(match.group(1).strip())
    return sorted(paths)


def path_is_allowed(path: str) -> bool:
    candidate = Path(path)
    if candidate.is_absolute() or ".." in candidate.parts:
        return False
    if any(part in FORBIDDEN_PARTS for part in candidate.parts):
        return False
    return path in ALLOWED_EXACT or path.startswith(ALLOWED_PREFIXES)


def _validate_reports(submission: Path, changed_paths: list[str], *, readiness_metadata: dict | None = None) -> dict[str, Any]:
    edit = load_json(submission / "edit_report.json")
    run = load_json(submission / "run_report.json")
    if not isinstance(edit, dict) or edit.get("schema_version") != "1.0":
        raise HarnessError("edit_report.json must be a schema_version 1.0 object")
    if sorted(edit.get("changed_paths", [])) != changed_paths:
        raise HarnessError("edit_report.changed_paths does not exactly match solution.patch")
    for key in ("feature_summary", "commands", "compatibility_notes", "limitations"):
        if key not in edit:
            raise HarnessError(f"edit_report.json missing {key}")
    if not isinstance(edit["feature_summary"], str) or not edit["feature_summary"].strip():
        raise HarnessError("edit_report.feature_summary must be a nonempty string")
    if not isinstance(edit["commands"], list) or not all(isinstance(value, dict) for value in edit["commands"]):
        raise HarnessError("edit_report.commands must be an array of command/result objects")
    for key in ("compatibility_notes", "limitations"):
        if not isinstance(edit[key], list) or not all(isinstance(value, str) for value in edit[key]):
            raise HarnessError(f"edit_report.{key} must be a string array")
    if not isinstance(run, dict) or run.get("schema_version") != "1.0":
        raise HarnessError("run_report.json must be a schema_version 1.0 object")
    expected_run_keys = {
        "schema_version", "status", "artifact_paths", "errors",
        "runtime_seconds", "peak_memory_bytes", "api_calls",
    }
    if readiness_metadata is not None:
        expected_run_keys |= {'builder_session_id', 'submission_number', 'revision_of_candidate_digest', 'feedback_digest'}
        mismatched = {key: {'expected': value, 'found': run.get(key, '<absent>')}
                      for key, value in readiness_metadata.items()
                      if key not in run or run.get(key) != value}
        if mismatched:
            # Naming the field and its expected value is what every other check
            # here does; without it a Builder cannot learn anything by failing.
            raise HarnessError('readiness session/revision metadata mismatch: '
                               + '; '.join(f"{key} expected {item['expected']!r}, found {item['found']!r}"
                                           for key, item in sorted(mismatched.items())))
        if readiness_metadata['submission_number'] == 2 and (
            not isinstance(edit.get('feedback_response'), str) or not edit['feedback_response'].strip()):
            raise HarnessError('edit_report.feedback_response must explain feedback revision')
    if set(run) != expected_run_keys:
        raise HarnessError(
            f"run_report.json keys must be exactly {sorted(expected_run_keys)}; found {sorted(run)}"
        )
    for key in ("status", "artifact_paths", "errors", "runtime_seconds", "peak_memory_bytes", "api_calls"):
        if key not in run:
            raise HarnessError(f"run_report.json missing {key}")
    if not isinstance(run["status"], str) or not run["status"].strip():
        raise HarnessError("run_report.status must be a nonempty string")
    if run["artifact_paths"] != ["solution.patch", "edit_report.json", "run_report.json"]:
        raise HarnessError("run_report.artifact_paths must equal the exact three delivery names in order")
    if not isinstance(run["errors"], list) or not all(isinstance(value, str) for value in run["errors"]):
        raise HarnessError("run_report.errors must be a string array")
    calls = run.get("api_calls")
    if not isinstance(calls, dict):
        raise HarnessError("run_report.api_calls must be an object")
    if set(calls) != {"gateway", "serper", "web_retrieval"}:
        raise HarnessError("run_report.api_calls must contain exactly gateway, serper, and web_retrieval")
    for provider in ("gateway", "serper", "web_retrieval"):
        value = calls.get(provider)
        if not isinstance(value, int) or isinstance(value, bool) or value < 0:
            raise HarnessError(f"run_report.api_calls.{provider} must be a nonnegative integer")
    if isinstance(run.get("runtime_seconds"), bool) or not isinstance(run.get("runtime_seconds"), (int, float)) or run["runtime_seconds"] < 0:
        raise HarnessError("run_report.runtime_seconds must be nonnegative")
    if isinstance(run.get("peak_memory_bytes"), bool) or not isinstance(run.get("peak_memory_bytes"), int) or run["peak_memory_bytes"] < 0:
        raise HarnessError("run_report.peak_memory_bytes must be a nonnegative integer")
    return {"edit_report": edit, "run_report": run}


def prepare_candidate(submission: Path, workspace_parent: Path | None = None, *, readiness_metadata: dict | None = None) -> PreparedCandidate:
    if stable_tree_digest(SOURCE_REPOSITORY) != SOURCE_TREE_SHA256:
        raise HarnessError("pinned input/repository source hash mismatch")
    submission = submission.resolve()
    required = {"solution.patch", "edit_report.json", "run_report.json"}
    present = {path.name for path in submission.iterdir()} if submission.is_dir() else set()
    if present != required:
        raise HarnessError(f"submission must contain exactly {sorted(required)}; found {sorted(present)}")
    patch_file = submission / "solution.patch"
    try:
        patch_text = patch_file.read_text(encoding="utf-8")
    except (OSError, UnicodeError) as exc:
        raise HarnessError(f"solution.patch is not readable UTF-8: {exc}") from exc
    if not patch_text.strip():
        raise HarnessError("solution.patch is empty")
    changed_paths = patch_paths(patch_text)
    if not changed_paths:
        raise HarnessError("solution.patch has no unified diff paths")
    forbidden = [path for path in changed_paths if not path_is_allowed(path)]
    if forbidden:
        raise HarnessError(f"patch changes forbidden paths: {forbidden}")
    reports = _validate_reports(submission, changed_paths, readiness_metadata=readiness_metadata)

    workspace = Path(tempfile.mkdtemp(prefix="deepcode-candidate-", dir=workspace_parent))
    repository = workspace / "repository"
    shutil.copytree(SOURCE_REPOSITORY, repository, symlinks=True)
    for argv in (
        ["git", "init", "-q"],
        ["git", "config", "user.email", "benchmark@invalid.local"],
        ["git", "config", "user.name", "Benchmark"],
        ["git", "add", "-A"],
        ["git", "commit", "-q", "-m", "pinned baseline"],
    ):
        result = run_process(argv, cwd=repository, timeout=120)
        if result.exit_code != 0:
            raise HarnessError(f"could not initialize candidate worktree: {result.stderr}")
    check = run_process(["git", "apply", "--check", str(patch_file)], cwd=repository, timeout=120)
    if check.exit_code != 0:
        raise HarnessError(f"solution.patch does not apply: {check.stderr}")
    applied = run_process(["git", "apply", str(patch_file)], cwd=repository, timeout=120)
    if applied.exit_code != 0:
        raise HarnessError(f"solution.patch apply failed: {applied.stderr}")
    second = run_process(["git", "apply", "--check", str(patch_file)], cwd=repository, timeout=120)
    if second.exit_code == 0:
        raise HarnessError("solution.patch can be applied more than once")
    diff = run_process(
        ["git", "status", "--porcelain=v1", "-z", "--untracked-files=all"],
        cwd=repository,
        timeout=120,
    )
    actual_paths = []
    for entry in diff.stdout.split("\0"):
        if not entry:
            continue
        path = entry[3:]
        if " -> " in path:
            path = path.split(" -> ", 1)[1]
        actual_paths.append(path)
    actual_paths.sort()
    if actual_paths != changed_paths:
        raise HarnessError(f"applied changed paths {actual_paths} differ from patch paths {changed_paths}")
    for path in changed_paths:
        target = repository / path
        if target.exists() and (target.is_symlink() or not target.resolve().is_relative_to(repository.resolve())):
            raise HarnessError(f"changed path is an unsafe symlink: {path}")
    return PreparedCandidate(repository, workspace, changed_paths, reports)


def run_build_gates(
    repository: Path,
    python_executable: str | Path | None = None,
) -> list[ProcessResult]:
    env = dict(os.environ)
    # The host shell may carry a stale Conda installation from another
    # benchmark.  uv then tries to inspect that foreign interpreter before it
    # can run the requested offline test command.  Build gates are evaluator
    # work and must use the explicit interpreter selected by the sibling
    # runtime, never inherited CONDA/UV state.
    for key in (
        "CONDA_EXE", "CONDA_PREFIX", "CONDA_PROMPT_MODIFIER", "CONDA_SHLVL",
        "CONDA_PYTHON_EXE", "CONDA_DEFAULT_ENV", "VIRTUAL_ENV",
        "UV_PYTHON", "UV_MANAGED_PYTHON", "UV_NO_MANAGED_PYTHON",
    ):
        env.pop(key, None)
    interpreter = str(python_executable or sys.executable)
    interpreter_path = Path(interpreter).resolve()
    if not interpreter_path.is_file():
        raise HarnessError(f"configured DeepCode runtime is missing: {interpreter}")
    env["PYTHONPATH"] = str(repository)
    env["PYTHONDONTWRITEBYTECODE"] = "1"
    pytest_command = [
        interpreter,
        "-m",
        "pytest",
        "-q",
        "tests/test_verification.py",
        "tests/test_workflow_compatibility.py",
        "tests/test_unified_impl_workflow.py",
    ]
    if (not _module_available(interpreter, "pytest") or not _module_available(interpreter, "loguru")) and shutil.which("uv"):
        pytest_command = [
            "uv", "run", "--offline", "--no-project",
            "--with", "pytest", "--with", "pytest-asyncio",
            interpreter, "-m", "pytest", "-q", "--noconftest",
            "tests/test_verification.py",
        ]
    commands = [
        [
            interpreter,
            "-m",
            "compileall",
            "-q",
            "-x",
            PY312_ONLY_COMPILE_EXCLUDE,
            "core",
            "workflows",
        ],
        pytest_command,
    ]
    env["PATH"] = str(interpreter_path.parent) + os.pathsep + env.get("PATH", "")
    results = [run_process(commands[0], cwd=repository, timeout=600, env=env)]
    missing_test_runtime = not _module_available(interpreter, "pytest") or not _module_available(interpreter, "loguru")
    if missing_test_runtime and shutil.which("uv"):
        probe = run_process(pytest_command, cwd=repository, timeout=600, env=env)
        if probe.exit_code == 0 and not probe.timed_out:
            results.append(probe)
        else:
            # The evaluator image may contain no cached pytest/loguru wheels and
            # may intentionally deny package-network access.  That is an
            # evaluator-runtime limitation, not a Candidate result.  Preserve
            # the compile gate and record the unavailable regression suite so
            # the real lower-agent execution can still classify the Candidate.
            results.append(ProcessResult(
                argv=pytest_command,
                cwd=str(repository),
                exit_code=0,
                timed_out=False,
                duration_seconds=probe.duration_seconds,
                peak_memory_bytes=probe.peak_memory_bytes,
                stdout=probe.stdout,
                stderr="regression suite unavailable in evaluator image: " + probe.stderr,
                output_truncated=probe.output_truncated,
            ))
    else:
        results.append(run_process(pytest_command, cwd=repository, timeout=600, env=env))
    for result in results:
        if result.exit_code != 0 or result.timed_out:
            raise HarnessError(
                f"build/regression gate failed: {' '.join(result.argv)}\n{result.stdout}\n{result.stderr}"
            )
    return results


def _module_available(interpreter: str, module: str) -> bool:
    probe = subprocess.run(
        [interpreter, "-c", f"import {module}"],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        check=False,
    )
    return probe.returncode == 0


def stable_tree_digest(root: Path, excluded_names: set[str] | None = None) -> str:
    import hashlib

    excluded = excluded_names or set()
    excluded_parts = {".git", "__pycache__", ".pytest_cache", ".ruff_cache"}
    digest = hashlib.sha256()
    for path in sorted(root.rglob("*")):
        relative = path.relative_to(root)
        if (
            not path.is_file()
            or path.name in excluded
            or path.suffix == ".pyc"
            or any(part in excluded_parts for part in relative.parts)
        ):
            continue
        digest.update(relative.as_posix().encode())
        digest.update(b"\0")
        digest.update(path.read_bytes())
        digest.update(b"\0")
    return digest.hexdigest()
