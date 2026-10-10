#!/usr/bin/env python3
"""Shared black-box support for the fenced schema-v3 repository-set transaction."""
from __future__ import annotations

import hashlib
import json
import os
import re
import signal
import subprocess
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable

from jsonschema import Draft202012Validator

CASE_IDS = tuple(f"test_{number:03d}" for number in range(1, 7))
ENTRY_MODULE = "aider.worktree_plan_adapter"
MEMORY_LIMIT_BYTES = 8 * 1024**3
FORBIDDEN_ENV = (
    "OPENAI_API_KEY",
    "ANTHROPIC_API_KEY",
    "DEEPSEEK_API_KEY",
    "DEEPSEEK_API_KEY",
    "SERPER_TOKEN",
    "HTTP_PROXY",
    "HTTPS_PROXY",
    "ALL_PROXY",
)
REDACTIONS = (
    "fixture-private",
    "FOREIGN_LEDGER_SECRET",
    "USER_SENTINEL_PRIVATE",
    "LEASE_TOKEN_PRIVATE",
)
PACKAGE = Path(__file__).resolve().parents[2]
SCHEMA_ROOT = PACKAGE / "input" / "schemas"
REQUEST_VALIDATOR = Draft202012Validator(json.loads((SCHEMA_ROOT / "request.schema.json").read_text(encoding="utf-8")))
RESPONSE_VALIDATOR = Draft202012Validator(json.loads((SCHEMA_ROOT / "response.schema.json").read_text(encoding="utf-8")))


class HarnessError(RuntimeError):
    pass


def redact(value: str, roots: Iterable[Path] = ()) -> str:
    text = value
    for marker in REDACTIONS:
        text = text.replace(marker, "[REDACTED]")
    for root in roots:
        text = text.replace(str(root), f"<{root.name}>")
    text = re.sub(r"/(?:tmp|share|home|var)/[^\s,;'\"\]\}]+", "<absolute-path>", text)
    text = re.sub(r"\bAKIA[A-Z0-9]{12,}\b", "[REDACTED-CREDENTIAL-LIKE]", text)
    text = re.sub(r"(?i)(api[_-]?key|token|secret)\s*[:=]\s*[^\s,;]+", r"\1=[REDACTED]", text)
    return text[-3000:]


def run(command: list[str], *, cwd: Path, env: dict[str, str] | None = None, timeout: int = 30, input_text: str | None = None) -> subprocess.CompletedProcess[str]:
    return subprocess.run(command, cwd=cwd, env=env, input=input_text, text=True, capture_output=True, timeout=timeout, check=False)


def process_tree_pss_bytes(root_pid: int) -> int:
    """Return sampled Linux PSS for a root and all currently live descendants."""
    parents: dict[int, int] = {}
    for entry in Path("/proc").iterdir():
        if not entry.name.isdigit():
            continue
        try:
            fields = (entry / "stat").read_text().split()
            parents[int(entry.name)] = int(fields[3])
        except (OSError, ValueError, IndexError):
            continue
    members = {root_pid}
    changed = True
    while changed:
        changed = False
        for pid, parent in parents.items():
            if parent in members and pid not in members:
                members.add(pid)
                changed = True
    total = 0
    for pid in members:
        try:
            for line in Path(f"/proc/{pid}/smaps_rollup").read_text().splitlines():
                if line.startswith("Pss:"):
                    total += int(line.split()[1]) * 1024
                    break
        except (OSError, ValueError, IndexError):
            try:
                for line in Path(f"/proc/{pid}/status").read_text().splitlines():
                    if line.startswith("VmRSS:"):
                        total += int(line.split()[1]) * 1024
                        break
            except (OSError, ValueError, IndexError):
                pass
    return total


@dataclass
class MonitoredResult:
    returncode: int
    stdout: str
    stderr: str
    duration_seconds: float
    peak_pss_bytes: int
    memory_exceeded: bool
    timed_out: bool


def run_monitored(command: list[str], *, cwd: Path, env: dict[str, str] | None = None, timeout: int = 120, memory_limit: int = MEMORY_LIMIT_BYTES) -> MonitoredResult:
    started = time.monotonic()
    with tempfile.TemporaryFile() as stdout_file, tempfile.TemporaryFile() as stderr_file:
        process = subprocess.Popen(command, cwd=cwd, env=env, stdout=stdout_file, stderr=stderr_file, start_new_session=True)
        peak = 0
        memory_exceeded = timed_out = False
        while process.poll() is None:
            peak = max(peak, process_tree_pss_bytes(process.pid))
            if peak > memory_limit:
                memory_exceeded = True
                os.killpg(process.pid, signal.SIGKILL)
                break
            if time.monotonic() - started > timeout:
                timed_out = True
                os.killpg(process.pid, signal.SIGKILL)
                break
            time.sleep(0.02)
        returncode = process.wait()
        peak = max(peak, process_tree_pss_bytes(process.pid))
        stdout_file.seek(0)
        stderr_file.seek(0)
        stdout = stdout_file.read(2 * 1024 * 1024).decode("utf-8", errors="replace")
        stderr = stderr_file.read(2 * 1024 * 1024).decode("utf-8", errors="replace")
    return MonitoredResult(returncode, stdout, stderr, time.monotonic() - started, peak, memory_exceeded, timed_out)


def git(repo: Path, *args: str, check: bool = True, env: dict[str, str] | None = None, input_text: str | None = None) -> str:
    done = run(["git", *args], cwd=repo, env=env, input_text=input_text)
    if check and done.returncode:
        raise HarnessError(f"git {' '.join(args)} failed: {done.stderr.strip()}")
    return done.stdout


def write_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def read_json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def file_digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def fixture_command(python: Path, worker: Path, mode: str, spec: Path) -> dict[str, list[str]]:
    return {"argv": [str(python), str(worker), mode, str(spec)]}


def make_repo(root: Path, files: dict[str, str | bytes], executable: set[str] | None = None) -> tuple[Path, Path, str]:
    executable = executable or set()
    origin, seed, base = root / "origin.git", root / "seed", root / "base"
    root.mkdir(parents=True, exist_ok=True)
    run(["git", "init", "--bare", "-q", str(origin)], cwd=root)
    run(["git", "init", "-q", "-b", "main", str(seed)], cwd=root)
    git(seed, "config", "user.name", "Evaluator Fixture")
    git(seed, "config", "user.email", "fixture@example.invalid")
    for name, content in files.items():
        target = seed / name
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(content if isinstance(content, bytes) else content.encode())
        if name in executable:
            target.chmod(0o755)
    git(seed, "add", "-A")
    git(seed, "commit", "-q", "-m", "fixture base")
    git(seed, "remote", "add", "origin", str(origin))
    git(seed, "push", "-q", "-u", "origin", "main")
    git(origin, "symbolic-ref", "HEAD", "refs/heads/main")
    done = run(["git", "clone", "-q", str(origin), str(base)], cwd=root)
    if done.returncode:
        raise HarnessError(done.stderr)
    git(base, "config", "user.name", "Evaluator Fixture")
    git(base, "config", "user.email", "fixture@example.invalid")
    return origin, base, git(base, "rev-parse", "HEAD").strip()


def tracked_tree(repo: Path, revision: str = "HEAD") -> dict[str, dict[str, str]]:
    output = git(repo, "ls-tree", "-r", "-z", revision)
    result: dict[str, dict[str, str]] = {}
    for record in output.split("\0"):
        if not record:
            continue
        metadata, name = record.split("\t", 1)
        mode, kind, oid = metadata.split()
        result[name] = {"mode": mode, "type": kind, "oid": oid}
    return result


def path_record(path: Path) -> dict[str, Any]:
    mode = oct(path.lstat().st_mode & 0o777)
    if path.is_symlink():
        return {"kind": "symlink", "target": os.readlink(path), "mode": mode}
    return {"kind": "file", "sha256": file_digest(path), "mode": mode, "bytes": path.stat().st_size}


def filesystem_state(repo: Path) -> dict[str, dict[str, Any]]:
    result: dict[str, dict[str, Any]] = {}
    for directory, names, files in os.walk(repo, topdown=True, followlinks=False):
        current = Path(directory)
        if current == repo and ".git" in names:
            names.remove(".git")
        for name in list(names):
            path = current / name
            if path.is_symlink():
                result[path.relative_to(repo).as_posix()] = path_record(path)
                names.remove(name)
        for name in files:
            path = current / name
            result[path.relative_to(repo).as_posix()] = path_record(path)
    return result


def untracked(repo: Path) -> dict[str, dict[str, Any]]:
    output = git(repo, "ls-files", "--others", "--exclude-standard", "-z")
    return {name: path_record(repo / name) for name in filter(None, output.split("\0"))}


def refs(repo: Path) -> dict[str, str]:
    output = git(repo, "for-each-ref", "--format=%(refname)%00%(objectname)")
    return dict(line.split("\0", 1) for line in output.splitlines() if "\0" in line)


def worktrees(repo: Path) -> list[dict[str, str]]:
    records: list[dict[str, str]] = []
    current: dict[str, str] = {}
    for line in git(repo, "worktree", "list", "--porcelain").splitlines() + [""]:
        if not line:
            if current:
                records.append(current)
                current = {}
        else:
            key, _, value = line.partition(" ")
            current[key] = value
    return records


def repo_state(repo: Path) -> dict[str, Any]:
    return {
        "head": git(repo, "rev-parse", "HEAD").strip(),
        "head_ref": git(repo, "symbolic-ref", "-q", "HEAD", check=False).strip() or None,
        "tree": tracked_tree(repo),
        "index": git(repo, "ls-files", "--stage", "-z"),
        "index_tree": git(repo, "write-tree").strip(),
        "status": git(repo, "status", "--porcelain=v2", "-z"),
        "filesystem": filesystem_state(repo),
        "untracked": untracked(repo),
        "refs": refs(repo),
        "worktrees": worktrees(repo),
    }


def log_events(path: Path) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    result = []
    for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
        try:
            item = json.loads(line)
            if isinstance(item, dict):
                result.append(item)
        except json.JSONDecodeError:
            continue
    return result


def response_shape(value: Any, operation: str, plan_id: str) -> list[str]:
    if not isinstance(value, dict):
        return ["response is not an object"]
    errors = [f"schema {error.json_path}: {error.message}" for error in sorted(RESPONSE_VALIDATOR.iter_errors(value), key=lambda item: item.json_path)]
    if value.get("operation") != operation or value.get("plan_id") != plan_id:
        errors.append("response identity mismatch")
    return errors


@dataclass
class Invocation:
    operation: str
    returncode: int
    response: dict[str, Any] | None
    shape_errors: list[str]
    stdout: str
    stderr: str
    duration_seconds: float
    peak_pss_bytes: int
    memory_exceeded: bool


class Adapter:
    def __init__(self, python: Path, source: Path, evidence: Path, command_log: Path):
        self.python, self.source, self.evidence, self.command_log = python, source, evidence, command_log
        self.counter = 0
        self.env = dict(os.environ)
        self.env.update({"PYTHONPATH": str(source), "PYTHONDONTWRITEBYTECODE": "1", "NO_PROXY": "localhost,127.0.0.1,::1", "AIDER_COMMAND_LOG": str(command_log)})
        for key in FORBIDDEN_ENV:
            self.env.pop(key, None)

    def invoke(self, value: dict[str, Any], expected: tuple[int, ...] = (0,), timeout: int = 120, response_required: bool = True) -> Invocation:
        request_errors = list(REQUEST_VALIDATOR.iter_errors(value))
        if request_errors:
            raise HarnessError(f"evaluator generated invalid request: {request_errors[0].message}")
        self.counter += 1
        operation = value["operation"]
        request_path = self.evidence / f"{self.counter:03d}-{operation}-request.json"
        response_path = self.evidence / f"{self.counter:03d}-{operation}-response.json"
        write_json(request_path, value)
        done = run_monitored([str(self.python), "-m", ENTRY_MODULE, "--request", str(request_path), "--response", str(response_path)], cwd=Path(value["repo"]), env=self.env, timeout=timeout)
        parsed = None
        shape_errors: list[str] = []
        if done.memory_exceeded:
            shape_errors.append(f"process-tree PSS exceeded {MEMORY_LIMIT_BYTES} bytes")
        if done.timed_out:
            shape_errors.append("adapter timeout")
        if done.returncode not in expected:
            shape_errors.append(f"unexpected exit {done.returncode}; expected {expected}")
        if response_path.is_file():
            try:
                value_read = read_json(response_path)
                parsed = value_read if isinstance(value_read, dict) else None
            except (OSError, json.JSONDecodeError) as exc:
                shape_errors.append(f"response parse: {exc}")
        elif response_required:
            shape_errors.append("response file missing")
        if parsed is not None:
            shape_errors.extend(response_shape(parsed, operation, value["plan_id"]))
            try:
                if json.loads(done.stdout) != parsed:
                    shape_errors.append("stdout differs from response file")
            except json.JSONDecodeError:
                shape_errors.append("stdout is not one JSON object")
        return Invocation(operation, done.returncode, parsed, shape_errors, done.stdout, done.stderr, done.duration_seconds, done.peak_pss_bytes, done.memory_exceeded)


def coordinator(identifier: str, token: str | None = None, fence: int | None = None) -> dict[str, Any]:
    return {"id": identifier, "token": token, "fence": fence}


def active_owner(response: dict[str, Any], fallback_id: str) -> dict[str, Any]:
    lease = response.get("coordination", {})
    return coordinator(str(lease.get("owner_id") or fallback_id), lease.get("lease_token"), lease.get("fence"))


def request(operation: str, repo: Path, state: Path, plan_id: str, *, plan: dict[str, Any] | None = None, max_workers: int | None = None, crash: dict[str, Any] | None = None, owner: dict[str, Any] | None = None, lease_seconds: int | None = None) -> dict[str, Any]:
    return {"schema_version": 3, "operation": operation, "repo": str(repo), "state_dir": str(state), "plan_id": plan_id, "plan": plan, "max_workers": max_workers, "crash": crash, "coordinator": owner, "lease_seconds": lease_seconds}


def plan(
    repositories: list[dict[str, Any]],
    subtasks: list[dict[str, Any]],
    integration_tests: list[dict[str, Any]],
    *,
    integration_order: list[str] | None = None,
    targets: list[dict[str, Any]] | None = None,
    participant_order: list[str] | None = None,
    links: list[dict[str, str]] | None = None,
) -> dict[str, Any]:
    order = participant_order or [item["id"] for item in repositories]
    return {
        "repositories": repositories,
        "integration_order": integration_order or [item["id"] for item in subtasks],
        "subtasks": subtasks,
        "integration_tests": integration_tests,
        "publication": {
            "targets": targets or [],
            "participant_order": order,
            "links": links or [],
            "commit_messages": [{"repository_id": identifier, "message": f"evaluator repository-set {identifier}"} for identifier in order],
            "on_ref_drift": "abort",
            "after_decision": "roll_forward",
        },
    }


def subtask(identifier: str, repository_id: str, dependencies: list[str], allowed: list[str], worker: dict[str, list[str]], test: dict[str, list[str]]) -> dict[str, Any]:
    return {"id": identifier, "repository_id": repository_id, "depends_on": dependencies, "allowed_paths": allowed, "worker": worker, "test": test}


def assertion(assertion_id: str, points: int, passed: bool, evidence: str) -> dict[str, Any]:
    return {"id": assertion_id, "points": points, "earned": points if passed else 0, "passed": bool(passed), "evidence": redact(evidence)}


def scored(case_id: str, assertions: list[dict[str, Any]], *, cap: int | None = None, cap_reason: str | None = None, commands: list[dict[str, Any]] | None = None, duration: float = 0.0) -> dict[str, Any]:
    if sum(item["points"] for item in assertions) != 100:
        raise HarnessError(f"{case_id} assertion weights do not total 100")
    raw = sum(item["earned"] for item in assertions)
    score = min(raw, cap) if cap is not None else raw
    return {"case_id": case_id, "valid": True, "raw_score": raw, "score": score, "maximum": 100, "local_integrity_cap": cap, "cap_reason": cap_reason, "assertions": assertions, "commands": commands or [], "duration_seconds": round(duration, 3), "errors": []}
