#!/usr/bin/env python3
"""Public-only helpers. This module contains no hidden fixture or assertion."""

from __future__ import annotations

import json
import os
import re
import shlex
import shutil
import signal
import socket
import subprocess
import tempfile
import time
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

PATCH_LIMIT = 4 * 1024 * 1024
REPORT_LIMIT = 1024 * 1024
MEMORY_LIMIT_BYTES = 8 * 1024 * 1024 * 1024
TIMEOUT_SECONDS = 600
ALLOWED_PREFIXES = ("src/", "drizzle/", "e2e-tests/", "testing/fake-llm-server/")
ALLOWED_FILES = {"package.json", "package-lock.json"}
FORBIDDEN_PREFIXES = (".git/", ".github/", ".claude/", "rules/", "out/", "node_modules/")
FORBIDDEN_FILES = {"AGENTS.md", "LICENSE", "src/pro/LICENSE"}
TEST_RE = re.compile(r"(^|/)(?:__tests__|tests?|e2e-tests)(?:/|$)|\.(?:test|spec)\.[cm]?[jt]sx?$|(?:^|/)snapshots?/")


class PublicRunError(RuntimeError):
    pass


def load_json(path: Path, limit: int = REPORT_LIMIT) -> dict[str, Any]:
    if not path.is_file() or path.is_symlink() or path.stat().st_size > limit:
        raise PublicRunError(f"{path.name} is missing, a symlink, or oversized")
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise PublicRunError(f"{path.name} is not valid UTF-8 JSON: {exc}") from exc
    if not isinstance(value, dict):
        raise PublicRunError(f"{path.name} must contain an object")
    return value


def changed_paths(patch: Path) -> list[str]:
    if not patch.is_file() or patch.is_symlink() or not 0 < patch.stat().st_size <= PATCH_LIMIT:
        raise PublicRunError("solution.patch is missing, empty, a symlink, or oversized")
    try:
        text = patch.read_text(encoding="utf-8")
    except UnicodeDecodeError as exc:
        raise PublicRunError("solution.patch is not UTF-8") from exc
    paths: list[str] = []
    for line in text.splitlines():
        if line.startswith("diff --git "):
            parts = shlex.split(line)
            if len(parts) != 4 or not parts[2].startswith("a/") or not parts[3].startswith("b/"):
                raise PublicRunError("malformed patch header")
            before, after = parts[2][2:], parts[3][2:]
            if before != after or Path(after).is_absolute() or "\\" in after or ".." in Path(after).parts:
                raise PublicRunError(f"unsafe or renamed patch path: {after}")
            paths.append(after)
    if not paths or len(paths) != len(set(paths)):
        raise PublicRunError("patch has no unique file entries")
    return sorted(paths)


def validate_submission(repository: Path, submission: Path) -> tuple[Path, list[str]]:
    if not submission.is_dir() or sorted(path.name for path in submission.iterdir()) != [
        "edit_report.json",
        "run_report.json",
        "solution.patch",
    ]:
        raise PublicRunError("submission directory must contain exactly the three delivery files")
    patch = submission / "solution.patch"
    paths = changed_paths(patch)
    production = False
    for item in paths:
        if item in FORBIDDEN_FILES or item.startswith(FORBIDDEN_PREFIXES):
            raise PublicRunError(f"forbidden patch path: {item}")
        if item not in ALLOWED_FILES and not item.startswith(ALLOWED_PREFIXES):
            raise PublicRunError(f"path outside allowed edit surface: {item}")
        if TEST_RE.search(item) and (repository / item).exists():
            raise PublicRunError(f"existing test/snapshot modified: {item}")
        production |= item.startswith("src/") and not TEST_RE.search(item)
        production |= item.startswith("drizzle/") or item in ALLOWED_FILES
    if not production:
        raise PublicRunError("patch contains no production change")
    edit = load_json(submission / "edit_report.json")
    run = load_json(submission / "run_report.json")
    if edit.get("schema_version") != 1 or edit.get("changed_paths") != paths:
        raise PublicRunError("edit_report schema or changed_paths is invalid")
    if not isinstance(edit.get("feature_summary"), str) or not edit["feature_summary"].strip():
        raise PublicRunError("edit_report feature_summary is missing")
    if not all(key in edit for key in ("commands", "compatibility_notes", "limitations")):
        raise PublicRunError("edit_report is incomplete")
    required_run = {"schema_version", "status", "artifact_paths", "errors",
                    "runtime_seconds", "peak_memory_bytes", "api_calls"}
    # The readiness profile requires four further fields in run_report.json, and
    # this compared key sets for exact equality -- so a delivery carrying them
    # would be refused here while finalize refuses the delivery without them.
    # The seven stay required and anything outside these eleven is still
    # refused; only the four the profile mandates are tolerated.
    readiness_run = {"builder_session_id", "submission_number",
                     "revision_of_candidate_digest", "feedback_digest"}
    unexpected = set(run) - required_run - readiness_run
    if not required_run <= set(run) or unexpected or run.get("schema_version") != "1.0":
        raise PublicRunError(
            "run_report schema 1.0 is invalid; required %s, optional %s, found %s"
            % (sorted(required_run), sorted(readiness_run), sorted(run)))
    if not isinstance(run.get("status"), str) or not run["status"].strip():
        raise PublicRunError("run_report status must be a nonempty string")
    if run.get("artifact_paths") != ["solution.patch", "edit_report.json", "run_report.json"]:
        raise PublicRunError("run_report artifact_paths is invalid")
    if not isinstance(run.get("errors"), list) or any(not isinstance(item, str) for item in run["errors"]):
        raise PublicRunError("run_report errors must be a string array")
    for key in ("runtime_seconds", "peak_memory_bytes"):
        value = run.get(key)
        if key == "peak_memory_bytes":
            valid = isinstance(value, int) and not isinstance(value, bool) and value >= 0
        else:
            valid = isinstance(value, (int, float)) and not isinstance(value, bool) and value >= 0
        if not valid:
            raise PublicRunError(f"run_report {key} must be nonnegative")
    api = run.get("api_calls")
    if not isinstance(api, dict) or set(api) != {"gateway", "serper", "web_retrieval"} or any(not isinstance(value, int) or isinstance(value, bool) or value < 0 for value in api.values()):
        raise PublicRunError("run_report api_calls is invalid")
    return patch, paths


def copy_apply(repository: Path, patch: Path, destination: Path) -> None:
    if destination.exists():
        shutil.rmtree(destination)
    shutil.copytree(
        repository,
        destination,
        symlinks=True,
        ignore=shutil.ignore_patterns(".git", "node_modules", "out"),
    )
    env = {**os.environ, "GIT_CONFIG_GLOBAL": os.devnull, "GIT_CONFIG_SYSTEM": os.devnull}
    for command in (
        ["git", "init", "-q"],
        ["git", "config", "user.email", "benchmark@example.invalid"],
        ["git", "config", "user.name", "Benchmark"],
        ["git", "add", "-A"],
        ["git", "commit", "-qm", "pinned baseline"],
        ["git", "apply", "--check", str(patch)],
        ["git", "apply", str(patch)],
    ):
        proc = subprocess.run(command, cwd=destination, env=env, text=True, capture_output=True)
        if proc.returncode:
            raise PublicRunError(f"{' '.join(command)} failed: {(proc.stderr or proc.stdout)[-1200:]}")
    second = subprocess.run(["git", "apply", "--check", str(patch)], cwd=destination, env=env, capture_output=True)
    if second.returncode == 0:
        raise PublicRunError("patch applies more than once")
    if (repository / "node_modules").is_dir():
        os.symlink(repository / "node_modules", destination / "node_modules", target_is_directory=True)
    nested = repository / "testing/fake-llm-server/node_modules"
    if nested.is_dir():
        target = destination / "testing/fake-llm-server/node_modules"
        target.parent.mkdir(parents=True, exist_ok=True)
        os.symlink(nested, target, target_is_directory=True)


def _tree(pid: int) -> set[int]:
    found: set[int] = set()
    pending = [pid]
    while pending:
        current = pending.pop()
        if current in found:
            continue
        found.add(current)
        try:
            pending.extend(int(value) for value in Path(f"/proc/{current}/task/{current}/children").read_text().split())
        except (FileNotFoundError, PermissionError, ValueError):
            pass
    return found


def _rss(pid: int) -> int:
    try:
        for line in Path(f"/proc/{pid}/status").read_text().splitlines():
            if line.startswith("VmRSS:"):
                return int(line.split()[1]) * 1024
    except (FileNotFoundError, PermissionError, ValueError, IndexError):
        pass
    return 0


def command(command_value: list[str], cwd: Path, env: dict[str, str] | None = None, timeout: int = TIMEOUT_SECONDS) -> dict[str, Any]:
    started = time.monotonic()
    with tempfile.TemporaryFile(mode="w+", encoding="utf-8") as stdout_file, tempfile.TemporaryFile(mode="w+", encoding="utf-8") as stderr_file:
        proc = subprocess.Popen(command_value, cwd=cwd, env={**os.environ, **(env or {})}, text=True, stdout=stdout_file, stderr=stderr_file, start_new_session=True)
        peak = 0
        timed_out = False
        memory_breached = False
        while proc.poll() is None:
            peak = max(peak, sum(_rss(pid) for pid in _tree(proc.pid)))
            if peak > MEMORY_LIMIT_BYTES or time.monotonic() - started > timeout:
                memory_breached = peak > MEMORY_LIMIT_BYTES
                timed_out = not memory_breached
                try:
                    os.killpg(proc.pid, signal.SIGTERM)
                    proc.wait(timeout=5)
                except (ProcessLookupError, subprocess.TimeoutExpired):
                    try:
                        os.killpg(proc.pid, signal.SIGKILL)
                    except ProcessLookupError:
                        pass
                break
            time.sleep(0.1)
        proc.wait()
        stdout_file.seek(0)
        stderr_file.seek(0)
        stdout, stderr = stdout_file.read(), stderr_file.read()
    return {
        "command": command_value,
        "exit_code": proc.returncode,
        "duration_seconds": round(time.monotonic() - started, 3),
        "stdout_tail": stdout[-6000:],
        "stderr_tail": stderr[-6000:],
        "peak_process_tree_rss_bytes": peak,
        "timed_out": timed_out,
        "memory_breached": memory_breached,
    }


def offline_env(extra: dict[str, str]) -> dict[str, str]:
    return {
        "CI": "true",
        "PLAYWRIGHT_HTML_OPEN": "never",
        "HTTP_PROXY": "http://127.0.0.1:9",
        "HTTPS_PROXY": "http://127.0.0.1:9",
        "ALL_PROXY": "http://127.0.0.1:9",
        "NO_PROXY": "127.0.0.1,localhost,::1",
        **extra,
    }


def install_env() -> dict[str, str]:
    """Clear only unreachable loopback proxies during lockfile setup."""
    environment = {
        "npm_config_audit": "false",
        "npm_config_fund": "false",
    }
    for key in (
        "HTTP_PROXY",
        "HTTPS_PROXY",
        "ALL_PROXY",
        "http_proxy",
        "https_proxy",
        "all_proxy",
        "npm_config_proxy",
        "npm_config_https_proxy",
    ):
        value = os.environ.get(key)
        if not value:
            continue
        parsed = urlparse(value)
        if parsed.hostname not in {"127.0.0.1", "localhost", "::1"} or parsed.port is None:
            continue
        try:
            with socket.create_connection((parsed.hostname, parsed.port), timeout=0.2):
                pass
        except OSError:
            environment[key] = ""
    return environment


def dependencies_ready(worktree: Path) -> bool:
    return all(
        path.is_file()
        for path in (
            worktree / "node_modules/.bin/vitest",
            worktree / "node_modules/.bin/tsgo",
            worktree / "testing/fake-llm-server/node_modules/express/package.json",
        )
    )


def score_public(observation: dict[str, Any], scenario: dict[str, Any]) -> dict[str, Any]:
    snapshot = observation.get("snapshot") if isinstance(observation.get("snapshot"), dict) else {}
    requirements = snapshot.get("requirements") if isinstance(snapshot.get("requirements"), list) else []
    runs = snapshot.get("testRuns") if isinstance(snapshot.get("testRuns"), list) else []
    evidence = snapshot.get("evidence") if isinstance(snapshot.get("evidence"), list) else []
    assertions: list[dict[str, Any]] = []

    def add(assertion_id: str, passed: bool, detail: Any) -> None:
        assertions.append({"id": assertion_id, "passed": bool(passed), "detail": detail})

    add("PUB-ENTRY-01", observation.get("entry") is True and snapshot.get("schemaVersion") == 1, snapshot.get("status"))
    ids = [item.get("id") for item in requirements if isinstance(item, dict)]
    add("PUB-LEDGER-01", bool(ids) and len(ids) == len(set(ids)) and all(isinstance(value, str) and value.startswith("REQ-") for value in ids), ids)
    add("PUB-TEST-01", observation.get("generatedTestExists") is True and "expect(" in str(observation.get("generatedTestContent", "")) and "page." in str(observation.get("generatedTestContent", "")), scenario.get("testFile"))
    phases = [item.get("phase") for item in runs if isinstance(item, dict)]
    add("PUB-LOOP-01", phases[:3] == ["mutation", "focused", "regression"] or all(value in phases for value in ("mutation", "focused", "regression")), phases)
    statuses = [(item.get("phase"), item.get("result")) for item in runs if isinstance(item, dict)]
    add("PUB-MUTATION-01", ("mutation", "failed") in statuses and ("focused", "passed") in statuses, statuses)
    linked = [item for item in requirements if isinstance(item, dict) and item.get("testRefs") and item.get("evidenceRefs") and item.get("codeRefs")]
    add("PUB-TRACE-01", bool(linked) and any(isinstance(item, dict) and item.get("kind") == "code_change" for item in evidence), {"linked": len(linked), "evidence": len(evidence)})
    budget = snapshot.get("repairBudget") if isinstance(snapshot.get("repairBudget"), dict) else {}
    add("PUB-BUDGET-01", isinstance(budget.get("maxIterations"), int) and 1 <= budget.get("maxIterations", 0) <= 6 and 0 <= budget.get("usedIterations", -1) <= budget.get("maxIterations", 0), budget)
    add("PUB-HONEST-01", snapshot.get("status") in {"unresolved", "infrastructure_error"}, snapshot.get("status"))
    channels = set(observation.get("handlerChannels", []))
    add(
        "PUB-CONTROL-CONTRACT-01",
        {"acceptance:control-run", "acceptance:get-control-receipt"}.issubset(channels),
        sorted(channels),
    )
    first = observation.get("controlFirstEnvelope")
    retry = observation.get("controlRetryEnvelope")
    first_value = first.get("value") if isinstance(first, dict) and first.get("ok") is True else None
    retry_value = retry.get("value") if isinstance(retry, dict) and retry.get("ok") is True else None
    add(
        "PUB-CONTROL-RECEIPT-01",
        isinstance(first_value, dict)
        and first_value.get("schemaVersion") == 1
        and first_value.get("operationId") == scenario.get("operationId")
        and first_value.get("command") == scenario.get("controlCommand")
        and first_value.get("outcome") == "applied",
        first_value,
    )
    add("PUB-CONTROL-REPLAY-01", first_value is not None and first_value == retry_value, retry_value)
    before = observation.get("snapshotBeforeControl")
    after_envelope = observation.get("snapshotAfterRetryEnvelope")
    after = after_envelope.get("value") if isinstance(after_envelope, dict) and after_envelope.get("ok") is True else None
    preserved = isinstance(before, dict) and isinstance(after, dict) and all(
        before.get(key) == after.get(key)
        for key in ("runId", "requirements", "evidence", "testRuns", "changedPaths", "repairBudget")
    )
    add(
        "PUB-CONTROL-ATOMIC-01",
        preserved
        and after.get("generation") == before.get("generation", -1) + 1
        and after.get("controlSequence") == first_value.get("sequence") if isinstance(after, dict) and isinstance(first_value, dict) else False,
        {"before": before, "after": after},
    )
    restarted_receipt = observation.get("restartedReceiptEnvelope")
    restarted_run = observation.get("restartedRunEnvelope")
    add(
        "PUB-CONTROL-RESTART-01",
        isinstance(restarted_receipt, dict)
        and restarted_receipt.get("ok") is True
        and restarted_receipt.get("value") == first_value
        and isinstance(restarted_run, dict)
        and restarted_run.get("ok") is True
        and isinstance(after, dict)
        and restarted_run.get("value") == after,
        {"receipt": restarted_receipt, "run": restarted_run},
    )
    foreign = observation.get("foreignReceiptEnvelope")
    add("PUB-CONTROL-ISOLATION-01", isinstance(foreign, dict) and foreign.get("ok") is False, foreign)
    if scenario.get("controlCommand") == "cancel":
        conflict = observation.get("conflictEnvelope")
        add(
            "PUB-CONTROL-CONFLICT-01",
            isinstance(conflict, dict) and conflict.get("ok") is False,
            conflict,
        )
    preview = observation.get("previewSession") if isinstance(observation.get("previewSession"), dict) else {}
    retry_preview = observation.get("previewRetrySession") if isinstance(observation.get("previewRetrySession"), dict) else {}
    final_preview = observation.get("previewAfterTerminal") if isinstance(observation.get("previewAfterTerminal"), dict) else {}
    attestation = observation.get("attestation") if isinstance(observation.get("attestation"), dict) else {}
    add("PUB-PREVIEW-CONTRACT-01", bool(preview) and preview.get("presentation") == "preview" and final_preview.get("status") in {"passed", "cancelled", "invalidated", "infrastructure"}, final_preview)
    add("PUB-PREVIEW-REPLAY-01", bool(preview) and preview.get("sessionId") == retry_preview.get("sessionId") and observation.get("runnerCalls") == 1, retry_preview)
    add("PUB-PREVIEW-PROGRESS-01", observation.get("statusHistory") == ["queued", "setup", "running", final_preview.get("status")], observation.get("statusHistory"))
    add("PUB-ATTESTATION-01", bool(attestation) and attestation.get("schemaVersion") == 1 and attestation.get("sessionId") == preview.get("sessionId"), attestation)
    add("PUB-PREVIEW-GATE-01", observation.get("crossSurfaceGate") is True, observation.get("crossSurfaceGate"))
    return {
        "case_id": scenario["caseId"],
        "passed": sum(1 for item in assertions if item["passed"]),
        "total": len(assertions),
        "assertions": assertions,
    }
