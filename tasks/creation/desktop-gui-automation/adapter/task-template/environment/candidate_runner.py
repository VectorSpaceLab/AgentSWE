#!/usr/bin/env python3
"""Root-owned supervisor for one unprivileged frozen GUI Candidate.

The supervisor and Candidate share a container only so Harbor can use a normal
Compose service.  The Candidate is dropped to uid/gid 65534, while the command
queue remains mode 0700 and root-owned.  No fixture, browser, bridge token, or
trusted evidence volume is mounted in this service.
"""
from __future__ import annotations

import datetime as dt
import json
import os
import signal
import subprocess
import time
import traceback
from pathlib import Path
from typing import Any


CONTROL = Path("/run-control")
REQUEST = CONTROL / "request.json"
RESULT = CONTROL / "result.json"
OUTPUT = Path("/candidate-output")
CANDIDATE_UID = 65534
CANDIDATE_GID = 65534
PASSTHROUGH = {
    "AGENTSWE_RESPONSES_BASE_URL", "GATEWAY_RESPONSES_ENDPOINT",
    "AGENTSWE_GATEWAY_ENDPOINT", "OPENAI_BASE_URL", "OPENAI_API_KEY",
    "GATEWAY_API_KEY", "AGENTSWE_REQUIRED_MODEL",
    "AGENTSWE_REQUIRED_REASONING_EFFORT", "AGENTSWE_RUNTIME_PREFIX",
    "AGENTSWE_EVALUATION_ID", "AGENTSWE_CASE_ID", "SSL_CERT_FILE",
    "SSL_CERT_DIR", "LD_LIBRARY_PATH", "FONTCONFIG_PATH",
    "FONTCONFIG_FILE", "AGENTSWE_FONT_DIRS",
}


def now() -> str:
    return dt.datetime.now(dt.timezone.utc).isoformat().replace("+00:00", "Z")


def atomic_json(path: Path, value: Any, mode: int = 0o600) -> None:
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    temporary.write_text(json.dumps(value, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    os.chmod(temporary, mode)
    os.replace(temporary, path)


def namespace(name: str) -> str:
    try:
        return os.readlink(f"/proc/self/ns/{name}")
    except OSError as exc:
        return f"unavailable:{type(exc).__name__}"


def process_tree(root: int) -> tuple[set[int], int]:
    parents: dict[int, list[int]] = {}
    rss: dict[int, int] = {}
    for entry in Path("/proc").iterdir():
        if not entry.name.isdigit():
            continue
        try:
            fields = (entry / "stat").read_text().split()
            status = (entry / "status").read_text().splitlines()
            pid, ppid = int(fields[0]), int(fields[3])
            value = next((int(line.split()[1]) for line in status if line.startswith("VmRSS:")), 0)
        except (OSError, ValueError, IndexError):
            continue
        parents.setdefault(ppid, []).append(pid)
        rss[pid] = value
    pending, seen = [root], set()
    total = 0
    while pending:
        pid = pending.pop()
        if pid in seen:
            continue
        seen.add(pid)
        total += rss.get(pid, 0)
        pending.extend(parents.get(pid, ()))
    return seen, total


def drop_candidate_privileges() -> None:
    os.setgroups([])
    os.setgid(CANDIDATE_GID)
    os.setuid(CANDIDATE_UID)
    os.umask(0o077)


def candidate_environment(capability: dict[str, Any]) -> dict[str, str]:
    runtime = Path(os.environ["CANDIDATE_RUNTIME_PREFIX"])
    home = Path("/tmp/candidate-home")
    for child in ("cache", "config", "data", "tmp", "pycache"):
        (home / child).mkdir(parents=True, exist_ok=True)
        os.chmod(home / child, 0o700)
        # 0919: the candidate runs as CANDIDATE_UID after drop_candidate_privileges(); a root-owned 0700
        # HOME/TMPDIR makes every mkdtemp fail with EACCES (breaks any candidate that launches a browser).
        os.chown(home / child, CANDIDATE_UID, CANDIDATE_GID)
    os.chown(home, CANDIDATE_UID, CANDIDATE_GID)
    environment = {
        key: value for key, value in os.environ.items()
        if key in PASSTHROUGH and isinstance(value, str)
    }
    environment.update({
        "PATH": os.pathsep.join((str(runtime / "bin"), "/usr/local/bin", "/usr/bin", "/bin")),
        "HOME": str(home), "TMPDIR": str(home / "tmp"), "TMP": str(home / "tmp"),
        "TEMP": str(home / "tmp"), "XDG_CACHE_HOME": str(home / "cache"),
        "XDG_CONFIG_HOME": str(home / "config"), "XDG_DATA_HOME": str(home / "data"),
        "PYTHONPYCACHEPREFIX": str(home / "pycache"), "PYTHONNOUSERSITE": "1",
        "PYTHONDONTWRITEBYTECODE": "1", "PYTHONUNBUFFERED": "1",
        "LANG": "C.UTF-8", "LC_ALL": "C.UTF-8", "TZ": "UTC",
        "GUI_CONTROL_URL": str(capability["url"]),
        "GUI_FIXTURE_URL": str(capability["url"]),
        "GUI_CONTROL_TOKEN": str(capability["token"]),
    })
    # Browser binaries are intentionally not advertised.  The evaluator-owned
    # browser is reachable only through GUI_CONTROL_URL.
    for key in tuple(environment):
        if key.startswith("PLAYWRIGHT_") or key in {"CHROMIUM", "DISPLAY"}:
            environment.pop(key, None)
    return environment


def freeze_candidate_output() -> None:
    """Make the completed Candidate tree readable but immutable to peers.

    Candidate files are intentionally created with umask 077.  The trusted GUI
    service has no DAC-bypass capability, so after the Candidate process exits
    the root-owned supervisor transfers only filesystem ownership and modes.
    It does not read or rewrite artifact contents.
    """
    paths = sorted(OUTPUT.rglob("*"), key=lambda path: len(path.parts), reverse=True)
    for path in paths:
        os.chown(path, 0, 0, follow_symlinks=False)
        if path.is_symlink():
            continue
        os.chmod(path, 0o555 if path.is_dir() else 0o444)
    os.chown(OUTPUT, 0, 0)
    os.chmod(OUTPUT, 0o555)


def run_once(request: dict[str, Any]) -> dict[str, Any]:
    timeout_limit = min(max(float(request.get("timeout_seconds", 600)), 1), 660)  # v2-lite: 1500 s/case
    memory_limit_kib = min(max(int(request.get("memory_limit_mib", 4096)), 128), 4096) * 1024
    capability_path = CONTROL / "gui-capability.json"
    deadline = time.monotonic() + 60
    while not capability_path.is_file():
        if time.monotonic() >= deadline:
            raise RuntimeError("GUI capability was not provisioned")
        time.sleep(0.05)
    capability = json.loads(capability_path.read_text(encoding="utf-8"))
    if set(capability) != {"protocol", "url", "token"}:
        raise RuntimeError("invalid GUI capability document")

    OUTPUT.mkdir(parents=True, exist_ok=True)
    os.chmod(OUTPUT, 0o777)
    for path in OUTPUT.iterdir():
        raise RuntimeError(f"candidate output volume is not empty: {path.name}")

    runtime = Path(os.environ["CANDIDATE_RUNTIME_PREFIX"])
    python = next((path for path in (runtime / "bin/python", runtime / "bin/python3") if path.is_file()), None)
    if python is None:
        raise RuntimeError("Candidate runtime Python is unavailable")
    command = [str(python), "run_agent.py", "--input", "/candidate-input/input.md", "--output", str(OUTPUT)]
    stdout_path, stderr_path = CONTROL / "candidate.stdout", CONTROL / "candidate.stderr"
    started_utc, started = now(), time.monotonic()
    timed_out = memory_violation = False
    peak_rss_kib = 0
    with stdout_path.open("wb") as stdout, stderr_path.open("wb") as stderr:
        process = subprocess.Popen(
            command, cwd="/submission", env=candidate_environment(capability),
            stdin=subprocess.DEVNULL, stdout=stdout, stderr=stderr,
            start_new_session=True, preexec_fn=drop_candidate_privileges,
        )
        observed_pids: set[int] = set()
        while process.poll() is None:
            pids, rss = process_tree(process.pid)
            observed_pids.update(pids)
            peak_rss_kib = max(peak_rss_kib, rss)
            timed_out = time.monotonic() - started > timeout_limit
            memory_violation = peak_rss_kib > memory_limit_kib
            if timed_out or memory_violation:
                try:
                    os.killpg(process.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
                break
            time.sleep(0.10)
        process.wait()
    freeze_candidate_output()
    return {
        "schema_version": "1.0", "request_id": request.get("request_id"),
        "case_id": request.get("case_id"), "start_utc": started_utc,
        "end_utc": now(), "wall_seconds": round(time.monotonic() - started, 3),
        "exit_code": process.returncode, "timeout": timed_out,
        "timeout_limit_seconds": timeout_limit, "memory_violation": memory_violation,
        "memory_limit_kib": memory_limit_kib, "peak_process_tree_rss_kib": peak_rss_kib,
        "observed_candidate_process_count": len(observed_pids),
        "candidate_uid": CANDIDATE_UID, "candidate_gid": CANDIDATE_GID,
        "runner_pid_namespace": namespace("pid"), "runner_mount_namespace": namespace("mnt"),
        "runner_network_namespace": namespace("net"),
        "candidate_visible_mount_contract": [
            "/submission:ro", "/candidate-input/input.md:ro", "/candidate-output:rw",
            "/candidate-runtime:ro", "/run-control:root-only",
        ],
        "fixture_mount_present": False, "trusted_evidence_mount_present": False,
        "browser_debug_endpoint_present": False, "evaluator_token_present": False,
        "stdout_path": str(stdout_path), "stderr_path": str(stderr_path),
    }


def main() -> int:
    CONTROL.mkdir(parents=True, exist_ok=True)
    os.chmod(CONTROL, 0o700)
    for stale in (REQUEST, RESULT, CONTROL / "runner-error.json"):
        stale.unlink(missing_ok=True)
    atomic_json(CONTROL / "runner-ready.json", {
        "ready": True, "pid_namespace": namespace("pid"),
        "mount_namespace": namespace("mnt"), "network_namespace": namespace("net"),
        "started_at_utc": now(),
    })
    while not REQUEST.is_file():
        time.sleep(0.05)
    try:
        request = json.loads(REQUEST.read_text(encoding="utf-8"))
        if not isinstance(request, dict) or not request.get("request_id"):
            raise ValueError("invalid run request")
        atomic_json(RESULT, run_once(request))
        return 0
    except Exception as exc:
        atomic_json(CONTROL / "runner-error.json", {
            "evaluation_state": "infrastructure_error", "score_publishable": False,
            "error": f"{type(exc).__name__}: {exc}", "traceback": traceback.format_exc(),
        })
        return 70


if __name__ == "__main__":
    raise SystemExit(main())
