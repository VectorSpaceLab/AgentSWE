#!/usr/bin/env python3
"""Apply a candidate patch in isolation and execute v4 behavioral checks."""

from __future__ import annotations

import argparse
import contextlib
import json
import math
import os
import re
import resource
import shutil
import signal
import stat
import subprocess
import sys
import tempfile
import time
from pathlib import Path, PurePosixPath

REPAIR_FIELDS = {
    "schema_version",
    "issue_summary",
    "diagnosis",
    "files_changed",
    "reproduction",
    "validation",
    "compatibility_notes",
    "limitations",
}
RUN_FIELDS = {
    "status",
    "artifact_paths",
    "errors",
    "runtime_seconds",
    "peak_memory_mb",
    "provider_counts",
}
RECOVERY_FIELDS = {
    "schema_version",
    "format",
    "artifact",
    "command",
    "pre_state",
    "post_state",
    "interruption_observation",
    "retry_observation",
    "compatibility_observation",
    "rollback_observation",
}
CONTRACT_BASE_FIELDS = {
    "schema_version",
    "repository",
    "allowed_paths",
    "public_test_command",
    "network",
    "recovery",
    "max_patch_bytes",
}
PROVIDERS = {"gateway_text", "gateway_image", "serper", "web_retrieval"}
PROBE_CHECKS = {
    "pre_state",
    "post_state",
    "interruption",
    "retry",
    "compatibility",
    "rollback",
}
RECOVERY_FORMATS = {
    "sessionarchive-jsonl-v1-v2",
    "sqlite-tenantconfig-v1-v3",
    "segmentstore-v1-manifest-segments",
}
PUBLIC_TEST_COMMAND = [
    "{python}",
    "-m",
    "unittest",
    "discover",
    "-s",
    "tests",
    "-v",
]
CONTRACT_RE = re.compile(
    r"^```repair_contract[ \t]*\r?\n(?P<body>.*?)\r?\n```[ \t]*$",
    re.MULTILINE | re.DOTALL,
)
MAX_REPORT_BYTES = 2_000_000


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


def limited_process() -> None:
    memory = 4 * 1024 * 1024 * 1024
    resource.setrlimit(resource.RLIMIT_AS, (memory, memory))
    resource.setrlimit(resource.RLIMIT_CORE, (0, 0))
    resource.setrlimit(resource.RLIMIT_NOFILE, (256, 256))
    resource.setrlimit(resource.RLIMIT_FSIZE, (128 * 1024 * 1024, 128 * 1024 * 1024))


def run(command: list[str], cwd: Path, timeout: int = 90, env=None) -> dict[str, object]:
    started = time.monotonic()
    process = subprocess.Popen(
        command,
        cwd=cwd,
        env=env,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        preexec_fn=limited_process,
        start_new_session=True,
    )
    try:
        output, _ = process.communicate(timeout=timeout)
        exit_code = process.returncode
    except subprocess.TimeoutExpired as exc:
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        tail, _ = process.communicate()
        prior = exc.stdout if isinstance(exc.stdout, str) else ""
        output = prior + (tail or "") + "\nEVALUATOR TIMEOUT"
        exit_code = 124
    return {
        "command": command,
        "exit_code": exit_code,
        "elapsed_seconds": round(time.monotonic() - started, 4),
        "output": (output or "")[-30000:],
        "output_prefix": (output or "")[:4096],
    }


def safe_relative(value: object, field: str, *, directory_prefix: bool = False) -> str:
    if not isinstance(value, str) or not value or "\\" in value or "\0" in value:
        raise ValueError(f"{field} must be a nonempty POSIX relative path")
    if any(ord(char) < 32 for char in value):
        raise ValueError(f"{field} contains a control character")
    trailing = value.endswith("/")
    if directory_prefix != trailing:
        suffix = " ending in /" if directory_prefix else " without a trailing /"
        raise ValueError(f"{field} must be a relative path{suffix}")
    raw = value[:-1] if trailing else value
    path = PurePosixPath(raw)
    if (
        not raw
        or path.is_absolute()
        or raw != path.as_posix()
        or any(part in {"", ".", "..", ".git"} for part in path.parts)
    ):
        raise ValueError(f"{field} is unsafe or non-canonical")
    return value


def resolve_beneath(root: Path, relative: str, field: str) -> Path:
    candidate = root / relative
    try:
        resolved = candidate.resolve(strict=True)
        resolved.relative_to(root.resolve(strict=True))
    except (OSError, ValueError) as exc:
        raise ValueError(f"{field} does not resolve beneath the active case") from exc
    if candidate.is_symlink():
        raise ValueError(f"{field} must not be a symlink")
    return resolved


def parse_contract(case_dir: Path) -> dict[str, object]:
    text = (case_dir / "input.md").read_text(encoding="utf-8")
    matches = list(CONTRACT_RE.finditer(text))
    if len(matches) != 1:
        raise ValueError("input.md must contain exactly one repair_contract block")
    try:
        value = json.loads(matches[0].group("body"))
    except json.JSONDecodeError as exc:
        raise ValueError("repair_contract is not valid JSON") from exc
    if not isinstance(value, dict):
        raise ValueError("repair_contract must be an object")
    recovery = value.get("recovery")
    expected = set(CONTRACT_BASE_FIELDS)
    if recovery == "required":
        expected.add("recovery_format")
    if set(value) != expected:
        missing = sorted(expected - set(value))
        extra = sorted(set(value) - expected)
        raise ValueError(f"repair_contract fields mismatch; missing={missing}, extra={extra}")
    if value["schema_version"] != "1.0":
        raise ValueError("unsupported repair_contract schema")
    safe_relative(value["repository"], "repository")
    allowed = value["allowed_paths"]
    if not isinstance(allowed, list) or not allowed:
        raise ValueError("allowed_paths must be a nonempty array")
    if len(set(allowed)) != len(allowed):
        raise ValueError("allowed_paths contains duplicates")
    for path in allowed:
        safe_relative(path, "allowed path", directory_prefix=True)
    normalized = [PurePosixPath(path[:-1]).parts for path in allowed]
    for index, left in enumerate(normalized):
        for right in normalized[index + 1 :]:
            if left == right[: len(left)] or right == left[: len(right)]:
                raise ValueError("allowed_paths must not overlap")
    command = value["public_test_command"]
    if command != PUBLIC_TEST_COMMAND:
        raise ValueError("public_test_command does not match the benchmark command")
    if value["network"] not in {"closed", "local_only"}:
        raise ValueError("invalid network policy")
    if recovery not in {"none", "required"}:
        raise ValueError("invalid recovery policy")
    if recovery == "required" and value["recovery_format"] not in RECOVERY_FORMATS:
        raise ValueError("unsupported recovery_format")
    patch_limit = value["max_patch_bytes"]
    if isinstance(patch_limit, bool) or not isinstance(patch_limit, int) or not 1 <= patch_limit <= 2_000_000:
        raise ValueError("invalid max_patch_bytes")
    return value


def load_json(path: Path) -> tuple[dict[str, object] | None, str | None]:
    try:
        if path.is_symlink() or not path.is_file():
            return None, "missing or symlink"
        if path.stat().st_size > MAX_REPORT_BYTES:
            return None, "oversized"
        value = json.loads(path.read_text(encoding="utf-8"))
        return (value, None) if isinstance(value, dict) else (None, "top level is not an object")
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        return None, str(exc)


def validate_observations(value: object, field: str) -> list[str]:
    if not isinstance(value, list):
        return [f"{field} must be an array"]
    errors: list[str] = []
    for index, item in enumerate(value):
        if not isinstance(item, dict) or set(item) != {"command", "exit_code", "observation"}:
            errors.append(f"{field}[{index}] has wrong shape")
            continue
        command = item["command"]
        if (
            not isinstance(command, list)
            or not command
            or any(not isinstance(part, str) or not part or "\0" in part for part in command)
        ):
            errors.append(f"{field}[{index}].command invalid")
        if isinstance(item["exit_code"], bool) or not isinstance(item["exit_code"], int):
            errors.append(f"{field}[{index}].exit_code invalid")
        if not isinstance(item["observation"], str) or not item["observation"].strip():
            errors.append(f"{field}[{index}].observation invalid")
    return errors


def _finite_nonnegative(value: object) -> bool:
    return (
        not isinstance(value, bool)
        and isinstance(value, (int, float))
        and math.isfinite(value)
        and value >= 0
    )


def _validate_artifact_paths(output_dir: Path, paths: object, recovery_required: bool) -> list[str]:
    errors: list[str] = []
    if not isinstance(paths, list) or any(not isinstance(item, str) for item in paths):
        return ["artifact_paths invalid"]
    if len(set(paths)) != len(paths):
        errors.append("artifact_paths contains duplicates")
    required = {"solution.patch", "repair_report.json", "run_report.json"}
    if recovery_required:
        required.add("migration_report.json")
    if not required.issubset(paths):
        errors.append("artifact_paths omits a required output")
    for item in paths:
        try:
            safe_relative(item, "artifact path")
            path = output_dir / item
            resolved_parent = path.parent.resolve(strict=True)
            resolved_parent.relative_to(output_dir.resolve(strict=True))
            if path.is_symlink() or not path.is_file():
                errors.append(f"artifact path is missing or unsafe: {item}")
        except (OSError, ValueError) as exc:
            errors.append(f"artifact path is unsafe: {item}: {exc}")
    return errors


def validate_reports(output_dir: Path, contract: dict[str, object]) -> dict[str, object]:
    repair, repair_error = load_json(output_dir / "repair_report.json")
    run_report, run_error = load_json(output_dir / "run_report.json")
    run_report = _agentswe_neutral_provider_keys(run_report)
    errors: list[str] = []
    repair_valid = False
    run_valid = False
    if repair_error:
        errors.append("repair_report.json: " + repair_error)
    elif set(repair or {}) != REPAIR_FIELDS or repair.get("schema_version") != "1.0":
        errors.append("repair_report.json schema mismatch")
    else:
        for field in ("issue_summary", "diagnosis"):
            if not isinstance(repair.get(field), str) or not str(repair[field]).strip():
                errors.append(f"repair_report.json {field} invalid")
        for field in ("compatibility_notes", "limitations"):
            if not isinstance(repair.get(field), str):
                errors.append(f"repair_report.json {field} invalid")
        errors += validate_observations(repair.get("reproduction"), "reproduction")
        errors += validate_observations(repair.get("validation"), "validation")
        changed = repair.get("files_changed")
        if not isinstance(changed, list) or not changed or any(not isinstance(item, str) for item in changed):
            errors.append("files_changed invalid")
        else:
            if len(set(changed)) != len(changed):
                errors.append("files_changed contains duplicates")
            for item in changed:
                try:
                    safe_relative(item, "files_changed entry")
                except ValueError as exc:
                    errors.append(str(exc))
        repair_valid = not any(error.startswith(("repair_report", "reproduction", "validation", "files_changed")) for error in errors)
    run_errors: list[str] = []
    if run_error:
        run_errors.append("run_report.json: " + run_error)
    elif set(run_report or {}) != RUN_FIELDS or run_report.get("status") != "success":
        run_errors.append("run_report.json schema/status mismatch")
    else:
        counts = run_report.get("provider_counts")
        if (
            not isinstance(counts, dict)
            or set(counts) != PROVIDERS
            or any(isinstance(counts[name], bool) or not isinstance(counts[name], int) or counts[name] < 0 for name in PROVIDERS)
        ):
            run_errors.append("provider_counts invalid")
        if not isinstance(run_report.get("errors"), list) or run_report["errors"]:
            run_errors.append("success run_report errors must be an empty array")
        if not _finite_nonnegative(run_report.get("runtime_seconds")):
            run_errors.append("runtime_seconds invalid")
        if not _finite_nonnegative(run_report.get("peak_memory_mb")):
            run_errors.append("peak_memory_mb invalid")
        run_errors += _validate_artifact_paths(
            output_dir,
            run_report.get("artifact_paths"),
            contract["recovery"] == "required",
        )
    errors += run_errors
    run_valid = not run_errors
    recovery = None
    recovery_report_valid = contract["recovery"] == "none"
    if contract["recovery"] == "required":
        recovery, migration_error = load_json(output_dir / "migration_report.json")
        migration_errors: list[str] = []
        if migration_error:
            migration_errors.append("migration_report.json: " + migration_error)
        elif (
            set(recovery or {}) != RECOVERY_FIELDS
            or recovery.get("schema_version") != "1.0"
            or recovery.get("format") != contract["recovery_format"]
        ):
            migration_errors.append("migration_report.json schema/format mismatch")
        else:
            for field in RECOVERY_FIELDS - {"schema_version", "format", "artifact", "command"}:
                if not isinstance(recovery.get(field), str) or not str(recovery[field]).strip():
                    migration_errors.append(f"migration_report.json {field} invalid")
        errors += migration_errors
        recovery_report_valid = not migration_errors
    return {
        "valid": not errors,
        "core_valid": repair is not None,
        "repair_valid": repair_valid,
        "run_valid": run_valid,
        "recovery_report_valid": recovery_report_valid,
        "errors": errors,
        "repair": repair,
        "run_report": run_report,
        "recovery": recovery,
    }


def path_allowed(path: str, allowed: list[str]) -> bool:
    try:
        safe_relative(path, "changed path")
    except ValueError:
        return False
    parts = PurePosixPath(path).parts
    return any(parts[: len(PurePosixPath(prefix[:-1]).parts)] == PurePosixPath(prefix[:-1]).parts for prefix in allowed)


def safe_copy(source: Path, destination: Path) -> None:
    if source.is_symlink() or not source.is_dir():
        raise ValueError("repository asset root is missing, unsafe, or not a directory")
    for entry in source.rglob("*"):
        if entry.is_symlink():
            raise ValueError(f"asset symlink: {entry.relative_to(source)}")
        mode = entry.stat().st_mode
        if not (stat.S_ISDIR(mode) or stat.S_ISREG(mode)):
            raise ValueError(f"asset special file: {entry.relative_to(source)}")
    shutil.copytree(source, destination)


AUDIT_BOOTSTRAP = r'''
import json
import os
import sys

_log = os.environ.pop("RECOVERY_AUDIT_LOG", "")
_work = os.path.realpath(os.environ.pop("RECOVERY_AUDIT_WORK", ""))
_busy = False

def _path(value):
    if isinstance(value, int):
        return None
    try:
        value = os.fsdecode(os.fspath(value))
    except TypeError:
        return None
    if not os.path.isabs(value):
        value = os.path.abspath(value)
    return os.path.realpath(value)

def _inside(value):
    return value == _work or value.startswith(_work + os.sep)

def _hook(event, args):
    global _busy
    if _busy or not _log:
        return
    if event not in {"open", "os.rename", "os.remove", "os.rmdir", "os.mkdir", "sqlite3.connect"}:
        return
    paths = [path for path in (_path(arg) for arg in args) if path and _inside(path)]
    if not paths:
        return
    record = {"event": event, "paths": paths}
    if event == "open" and len(args) > 1:
        record["mode"] = str(args[1])
    _busy = True
    try:
        descriptor = os.open(_log, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
        try:
            os.write(descriptor, (json.dumps(record, sort_keys=True) + "\n").encode("utf-8"))
        finally:
            os.close(descriptor)
    finally:
        _busy = False

sys.addaudithook(_hook)
'''


def _load_audit(path: Path) -> list[dict[str, object]]:
    if not path.is_file():
        return []
    events: list[dict[str, object]] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        try:
            value = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(value, dict):
            events.append(value)
    return events


def isolated_run(script: Path, argv: list[str], repo: Path, work: Path, env: dict[str, str], audit_dir: Path, *, trace=False) -> dict[str, object]:
    audit_dir.mkdir(parents=True, exist_ok=True)
    root = audit_dir.parent
    root.chmod(0o755)
    for directory in (work, Path(env['HOME']), Path(env['TMPDIR'])):
        directory.chmod(0o777)
    config = {'script': str(script), 'argv': argv, 'repository': str(repo),
              'write_paths': [str(work), env['HOME'], env['TMPDIR']]}
    config_path = audit_dir / 'worker.json'
    config_path.write_text(json.dumps(config))
    command = [sys.executable, '-I', str(Path(__file__).with_name('repository_worker.py')), str(config_path)]
    trace_path = audit_dir / 'syscalls.log'
    if trace:
        strace = shutil.which('strace', path=env.get('PATH', ''))
        if not strace:
            return {'exit_code': 125, 'output': '', 'infrastructure_error': 'trusted syscall tracer unavailable'}
        command = [strace, '-f', '-s', '4096', '-e', 'trace=%file', '-o', str(trace_path), *command]
    observed = run(command, repo, timeout=90, env=env)
    first = str(observed.get('output_prefix', observed['output'])).partition('\n')[0]
    remaining = str(observed['output']).partition('\n')[2] if str(observed['output']).startswith(first) else str(observed['output'])
    try:
        ready = json.loads(first)
    except json.JSONDecodeError:
        ready = {}
    if not isinstance(ready, dict) or not isinstance(ready.get('sandbox_ready'), dict):
        observed['infrastructure_error'] = 'trusted repository isolation unavailable: ' + str(ready.get('infrastructure_error', first))
    else:
        observed['isolation'] = ready['sandbox_ready']
        observed['output'] = remaining
    if trace_path.is_file():
        observed['syscall_audit'] = trace_path.read_text()[-250000:]
    return observed


def syscall_events(text: str, work: Path) -> list[dict[str, object]]:
    events = []
    for line in text.splitlines():
        if '= -1' in line:
            continue
        match = re.search(r'\b(openat|open|renameat2|renameat|rename|unlinkat|unlink|mkdirat|mkdir)\((.*)', line)
        if not match:
            continue
        paths = []
        for value in re.findall(r'"([^"\\]*(?:\\.[^"\\]*)*)"', match.group(2)):
            try:
                value = json.loads('"' + value + '"')
                path = Path(value)
                if path.is_absolute() and path.is_relative_to(work):
                    paths.append(str(path))
            except (ValueError, TypeError):
                continue
        if not paths:
            continue
        name = match.group(1)
        event = 'open' if name.startswith('open') else 'os.rename' if name.startswith('rename') else 'os.remove' if name.startswith('unlink') else 'os.mkdir'
        mode = 'w' if any(flag in line for flag in ('O_WRONLY', 'O_RDWR', 'O_CREAT')) else 'r'
        events.append({'event': event, 'paths': paths, 'mode': mode, 'source': 'external_strace'})
        if name.startswith('open') and any(p.endswith(('.db', '.sqlite', '.sqlite3')) for p in paths):
            events.append({'event': 'sqlite3.connect', 'paths': paths, 'source': 'external_database_open'})
    return events


def _audit_recovery(format_name: str, events: list[dict[str, object]]) -> tuple[bool, dict[str, object]]:
    paths = sorted({path for event in events for path in event.get("paths", []) if isinstance(path, str)})
    writes = [
        event
        for event in events
        if event.get("event") in {"os.rename", "os.remove", "os.mkdir"}
        or (event.get("event") == "open" and any(flag in str(event.get("mode", "")) for flag in ("w", "a", "x", "+")))
    ]
    renames = [event for event in events if event.get("event") == "os.rename"]
    sqlite_connections = [event for event in events if event.get("event") == "sqlite3.connect"]
    lower_paths = [path.lower() for path in paths]
    reasons: list[str] = []
    if not writes and not sqlite_connections:
        reasons.append("recovery self-test made no audited writes beneath {work}")
    if format_name == "sessionarchive-jsonl-v1-v2":
        if not any(path.endswith(".jsonl") for path in lower_paths):
            reasons.append("JSONL recovery self-test did not exercise a .jsonl archive")
        if not any(".bak" in path for path in lower_paths):
            reasons.append("JSONL recovery self-test did not exercise a rollback copy")
        if not renames:
            reasons.append("JSONL recovery self-test did not exercise atomic replacement")
    elif format_name == "sqlite-tenantconfig-v1-v3":
        if len(sqlite_connections) < 3 or not any(path.endswith((".db", ".sqlite", ".sqlite3")) for path in lower_paths):
            reasons.append("SQLite recovery self-test did not execute repeated database probes")
    elif format_name == "segmentstore-v1-manifest-segments":
        if not any(path.endswith(".bin") for path in lower_paths):
            reasons.append("segment recovery self-test did not exercise binary segments")
        if not any("manifest" in Path(path).name.lower() for path in paths):
            reasons.append("segment recovery self-test did not exercise manifests")
        if not renames:
            reasons.append("segment recovery self-test did not exercise manifest installation")
    else:
        reasons.append("unknown recovery format")
    return not reasons, {
        "event_count": len(events),
        "write_event_count": len(writes),
        "rename_event_count": len(renames),
        "sqlite_connection_count": len(sqlite_connections),
        "paths": paths[-40:],
        "errors": reasons,
    }


def recovery_probe(
    report: object,
    repo: Path,
    work: Path,
    env: dict[str, str],
    allowed: list[str],
    changed: list[str],
    audit_dir: Path,
) -> dict[str, object]:
    errors: list[str] = []
    if not isinstance(report, dict):
        return {"required": True, "valid": False, "errors": ["missing report"]}
    artifact = report.get("artifact")
    command = report.get("command")
    try:
        safe_relative(artifact, "artifact")
    except ValueError as exc:
        errors.append(str(exc))
    if isinstance(artifact, str):
        if not artifact.endswith(".py"):
            errors.append("recovery artifact must be a Python file")
        if not path_allowed(artifact, allowed):
            errors.append("recovery artifact outside allowed paths")
        if artifact not in changed:
            errors.append("recovery artifact is not included in the patch")
    if not isinstance(command, list) or len(command) < 4 or any(not isinstance(item, str) or not item for item in command):
        errors.append("recovery command invalid")
    elif command[0] != "{python}" or command[1] != "{artifact}" or "--self-test" not in command or command.count("{work}") != 1:
        errors.append("recovery command must execute {artifact} self-test with one {work}")
    else:
        for item in command[2:]:
            if item in {"{work}", "{repository}"} or re.fullmatch(r"--?[A-Za-z0-9][A-Za-z0-9_-]*", item):
                continue
            errors.append(f"unsafe recovery command argument: {item}")
    if errors:
        return {"required": True, "valid": False, "errors": errors}
    artifact_path = repo / str(artifact)
    if artifact_path.is_symlink() or not artifact_path.is_file():
        return {"required": True, "valid": False, "errors": ["recovery artifact missing"]}
    resolved = [sys.executable, str(artifact_path)] + [
        str(work) if item == "{work}" else str(repo) if item == "{repository}" else item
        for item in command[2:]
    ]
    observed = isolated_run(artifact_path, resolved[1:], repo, work, env, audit_dir, trace=True)
    if observed.get('infrastructure_error'):
        return {'required': True, 'valid': False, 'errors': [], 'infrastructure_error': observed['infrastructure_error'], 'execution': observed}
    parsed = None
    if observed["exit_code"] == 0:
        try:
            parsed = json.loads(str(observed["output"]).strip().splitlines()[-1])
        except (json.JSONDecodeError, IndexError):
            errors.append("recovery probe did not print JSON")
    else:
        errors.append("recovery probe failed")
    if parsed is not None:
        checks = parsed.get("checks") if isinstance(parsed, dict) else None
        if (
            not isinstance(parsed, dict)
            or parsed.get("schema_version") != "1.0"
            or parsed.get("status") != "ok"
            or parsed.get("format") != report.get("format")
        ):
            errors.append("recovery probe result metadata mismatch")
        if not isinstance(checks, dict) or set(checks) != PROBE_CHECKS or any(checks[name] is not True for name in PROBE_CHECKS):
            errors.append("recovery probe checks incomplete")
    audit_events = syscall_events(str(observed.get('syscall_audit', '')), work)
    audit_valid, audit_summary = _audit_recovery(str(report.get("format")), audit_events)
    if not audit_valid:
        errors += list(audit_summary["errors"])
    symlinks = [str(path.relative_to(work)) for path in work.rglob("*") if path.is_symlink()]
    if symlinks:
        errors.append("recovery self-test created symlinks beneath {work}")
    return {
        "required": True,
        "valid": not errors,
        "errors": errors,
        "execution": observed,
        "probe": parsed,
        "audit": audit_summary,
        "work_symlinks": symlinks,
    }


def _nul_paths(output: object) -> list[str]:
    return [item for item in str(output).split("\0") if item]


@contextlib.contextmanager
def evaluation_directory(prefix: str):
    """Scratch tree for one case whose removal can never cost the case its result.

    Submitted code runs as uid 65534 and may create directories under {work},
    HOME or TMPDIR. The verifier has no CAP_DAC_OVERRIDE or CAP_FOWNER, so it
    can neither empty nor chmod such a directory, and TemporaryDirectory's
    cleanup raises PermissionError from its chmod retry even with
    ignore_cleanup_errors=True. A cleanup that fails is reported on stderr and
    whatever remains is left to the disposable verifier container.
    """
    temporary = tempfile.TemporaryDirectory(prefix=prefix, ignore_cleanup_errors=True)
    try:
        yield temporary.name
    finally:
        try:
            temporary.cleanup()
        except Exception as exc:
            try:
                shutil.rmtree(temporary.name, ignore_errors=True)
            except Exception:
                pass
            print(json.dumps({
                "evaluation_directory_cleanup": "incomplete",
                "path": temporary.name,
                "remaining": os.path.lexists(temporary.name),
                "error": f"{type(exc).__name__}: {exc}",
            }), file=sys.stderr, flush=True)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--case-dir", required=True, type=Path)
    parser.add_argument("--output-dir", required=True, type=Path)
    parser.add_argument("--result", required=True, type=Path)
    args = parser.parse_args()
    case_dir = args.case_dir.resolve()
    output_dir = args.output_dir.resolve()
    args.result.parent.mkdir(parents=True, exist_ok=True)
    result: dict[str, object] = {"case": case_dir.name, "validity_gate": False, "errors": []}
    try:
        contract = parse_contract(case_dir)
        source_repo = resolve_beneath(case_dir, str(contract["repository"]), "repository")
        if not source_repo.is_dir():
            raise ValueError("repository is not a directory")
    except Exception as exc:
        result["errors"].append(f"case contract invalid: {exc}")
        result["evaluation_state"] = "infrastructure_error"
        args.result.write_text(json.dumps(result, indent=2) + "\n", encoding="utf-8")
        return 2
    result["recovery_required"] = contract["recovery"] == "required"
    result["network"] = contract["network"]
    reports = validate_reports(output_dir, contract)
    result["report_validation"] = {
        "valid": reports["valid"],
        "repair_valid": reports["repair_valid"],
        "run_valid": reports["run_valid"],
        "recovery_report_valid": reports["recovery_report_valid"],
        "errors": reports["errors"],
    }
    result['quality_errors'] = list(reports['errors'])
    if not reports['core_valid']:
        result['errors'].append('repair_report.json is missing, corrupt, or not an object')
        args.result.write_text(json.dumps(result, indent=2) + '\n', encoding='utf-8')
        return 2
    patch = output_dir / "solution.patch"
    if patch.is_symlink() or not patch.is_file() or not 0 < patch.stat().st_size <= contract["max_patch_bytes"]:
        result["errors"].append("solution.patch missing, symlink, empty, or oversized")
        args.result.write_text(json.dumps(result, indent=2) + "\n", encoding="utf-8")
        return 2
    try:
        patch_bytes = patch.read_bytes()
        patch_text = patch_bytes.decode("utf-8")
        if b"\0" in patch_bytes or "GIT binary patch" in patch_text or "Binary files " in patch_text:
            raise ValueError("binary content")
    except Exception as exc:
        result["errors"].append(f"unsafe patch: {exc}")
        args.result.write_text(json.dumps(result, indent=2) + "\n", encoding="utf-8")
        return 2
    with evaluation_directory("repair-v4-") as temporary_name:
        temp = Path(temporary_name)
        repo = temp / "repository"
        work = temp / "recovery-work"
        try:
            safe_copy(source_repo, repo)
        except Exception as exc:
            result["errors"].append(f"unsafe repository assets: {exc}")
            args.result.write_text(json.dumps(result, indent=2) + "\n", encoding="utf-8")
            return 2
        work.mkdir()
        for command in [
            ["git", "init", "-q"],
            ["git", "config", "user.email", "eval@example.invalid"],
            ["git", "config", "user.name", "Evaluator"],
            ["git", "add", "-A"],
            ["git", "commit", "-qm", "pristine"],
        ]:
            observed = run(command, repo, 20)
            if observed["exit_code"]:
                result["errors"].append("git initialization failed")
                args.result.write_text(json.dumps(result, indent=2) + "\n", encoding="utf-8")
                return 2
        check = run(["git", "apply", "--check", str(patch)], repo, 20)
        result["patch_check"] = check
        if check["exit_code"]:
            result["errors"].append("patch does not apply")
            args.result.write_text(json.dumps(result, indent=2) + "\n", encoding="utf-8")
            return 2
        applied = run(["git", "apply", str(patch)], repo, 20)
        if applied["exit_code"]:
            result["errors"].append("patch application failed after successful check")
            args.result.write_text(json.dumps(result, indent=2) + "\n", encoding="utf-8")
            return 2
        names = run(["git", "diff", "--name-only", "-z", "--no-renames"], repo, 20)
        untracked = run(["git", "ls-files", "--others", "--exclude-standard", "-z"], repo, 20)
        if names["exit_code"] or untracked["exit_code"]:
            result["errors"].append("changed-path inventory failed")
            args.result.write_text(json.dumps(result, indent=2) + "\n", encoding="utf-8")
            return 2
        changed = sorted(set(_nul_paths(names["output"])) | set(_nul_paths(untracked["output"])))
        symlinks = [str(path.relative_to(repo)) for path in repo.rglob("*") if path.is_symlink()]
        special_files: list[str] = []
        for item in changed:
            candidate = repo / item
            if candidate.exists() and not candidate.is_symlink() and not stat.S_ISREG(candidate.stat().st_mode):
                special_files.append(item)
        scope_ok = (
            bool(changed)
            and all(path_allowed(item, contract["allowed_paths"]) for item in changed)
            and not symlinks
            and not special_files
        )
        result["scope"] = {
            "allowed": contract["allowed_paths"],
            "changed_files": changed,
            "scope_ok": scope_ok,
            "symlinks": symlinks,
            "special_files": special_files,
        }
        if not scope_ok:
            result["errors"].append("patch scope violation")
            args.result.write_text(json.dumps(result, indent=2) + "\n", encoding="utf-8")
            return 2
        report_files = reports["repair"].get("files_changed") if isinstance(reports["repair"], dict) else None
        result["report_file_list_matches_patch"] = isinstance(report_files, list) and sorted(report_files) == changed
        env = {
            "PATH": os.environ.get("PATH", ""),
            "HOME": str(temp / "home"),
            "TMPDIR": str(temp / "process-tmp"),
            "LANG": "C.UTF-8",
            "LC_ALL": "C.UTF-8",
            "PYTHONPATH": str(repo / "src"),
            "PYTHONDONTWRITEBYTECODE": "1",
            "NO_PROXY": "",
            "no_proxy": "",
            "HTTP_PROXY": "http://127.0.0.1:9",
            "HTTPS_PROXY": "http://127.0.0.1:9",
            "ALL_PROXY": "http://127.0.0.1:9",
            "http_proxy": "http://127.0.0.1:9",
            "https_proxy": "http://127.0.0.1:9",
            "all_proxy": "http://127.0.0.1:9",
        }
        (temp / "home").mkdir()
        (temp / "process-tmp").mkdir()
        public_command = [sys.executable if item == "{python}" else item for item in contract["public_test_command"]]
        public_launcher = temp / 'public_runner.py'
        public_launcher.write_text('import unittest\nunittest.main(module=None)\n')
        public = isolated_run(public_launcher, [str(public_launcher), 'discover', '-s', 'tests', '-v'], repo, work, env, temp / 'public-audit')
        hidden_script = Path(__file__).with_name('hidden_tests') / (case_dir.name + '.py')
        hidden = isolated_run(hidden_script, [str(hidden_script)], repo, work, env, temp / 'hidden-audit')
        recovery: dict[str, object] = {"required": False, "valid": True, "errors": []}
        if contract["recovery"] == "required":
            recovery = recovery_probe(
                reports["recovery"],
                repo,
                work,
                env,
                contract["allowed_paths"],
                changed,
                temp / "audit-bootstrap",
            )
        result.update(
            {
                "public_regression": public,
                "hidden_checks": hidden,
                "recovery_validation": recovery,
            }
        )
        infra = [value['infrastructure_error'] for value in (public, hidden, recovery) if value.get('infrastructure_error')]
        if infra:
            result['evaluation_state'] = 'infrastructure_error'
            result['infrastructure_errors'] = infra
            result['validity_gate'] = False
            args.result.write_text(json.dumps(result, indent=2) + '\n', encoding='utf-8')
            return 2
        result["checks"] = {
            "repair_report_valid": reports["repair_valid"],
            "run_report_valid": reports["run_valid"],
            "recovery_report_valid": reports["recovery_report_valid"],
            "patch_applies": True,
            "scope_ok": True,
            "public_pass": public["exit_code"] == 0,
            "hidden_pass": hidden["exit_code"] == 0,
            "recovery_pass": recovery["valid"],
            "file_list_matches": result["report_file_list_matches_patch"],
        }
        result["validity_gate"] = bool(reports["core_valid"])
        result['evaluation_state'] = 'scoreable' if result['validity_gate'] else 'fatal_zero'
    args.result.write_text(json.dumps(result, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
