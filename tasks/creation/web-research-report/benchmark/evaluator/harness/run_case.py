#!/usr/bin/env python3
"""Protocol-v1 runner: probe providers, execute once, and preserve process facts."""

from __future__ import annotations

import argparse
import os
import shutil
import signal
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

from common import PROTOCOL_VERSION, write_json


def load_env(path: Path, python_bin: Path, runtime_root: Path) -> dict[str, str]:
    directories = {
        "HOME": runtime_root / "home",
        "TMPDIR": runtime_root / "tmp",
        "XDG_CACHE_HOME": runtime_root / "cache",
        "XDG_CONFIG_HOME": runtime_root / "config",
        "XDG_DATA_HOME": runtime_root / "data",
        "PYTHONPYCACHEPREFIX": runtime_root / "pycache",
        "MPLCONFIGDIR": runtime_root / "matplotlib",
    }
    for directory in directories.values():
        directory.mkdir(parents=True, exist_ok=True)
    env = {
        "PATH": os.pathsep.join(
            (str(python_bin.resolve().parent), "/usr/local/bin", "/usr/bin", "/bin")
        ),
        "LANG": os.environ.get("LANG", "C.UTF-8"),
        "LC_ALL": "C.UTF-8",
        "TZ": os.environ.get("TZ", "UTC"),
        "PYTHONNOUSERSITE": "1",
        "PYTHONDONTWRITEBYTECODE": "1",
        **{key: str(value) for key, value in directories.items()},
    }
    env["TMP"] = env["TMPDIR"]
    env["TEMP"] = env["TMPDIR"]
    if os.environ.get("SSL_CERT_FILE"):
        env["SSL_CERT_FILE"] = os.environ["SSL_CERT_FILE"]
    if not path.is_file():
        return env
    for line in path.read_text(encoding="utf-8").splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#") or "=" not in stripped:
            continue
        key, value = stripped.split("=", 1)
        key = key.strip()
        if key in {"GATEWAY_API_KEY", "SERPER_TOKEN"}:
            env[key] = value.strip().strip("\"'")
    return env


def child_pids(root_pid: int) -> list[int]:
    seen: set[int] = set()
    pending = [root_pid]
    while pending:
        pid = pending.pop()
        if pid in seen:
            continue
        seen.add(pid)
        result = subprocess.run(
            ["pgrep", "-P", str(pid)], check=False, capture_output=True, text=True
        )
        pending.extend(
            int(line) for line in result.stdout.splitlines() if line.strip().isdigit()
        )
    return sorted(seen)


def tree_rss_kib(root_pid: int) -> int:
    pids = child_pids(root_pid)
    if not pids:
        return 0
    result = subprocess.run(
        ["ps", "-o", "rss=", "-p", ",".join(str(pid) for pid in pids)],
        check=False,
        capture_output=True,
        text=True,
    )
    return sum(
        int(line) for line in result.stdout.splitlines() if line.strip().isdigit()
    )


def redact(text: str, env: dict[str, str]) -> str:
    result = text
    for name in ("GATEWAY_API_KEY", "SERPER_TOKEN"):
        secret = env.get(name, "")
        if secret:
            result = result.replace(secret, f"<{name}_REDACTED>")
    return result


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--python", type=Path, required=True)
    parser.add_argument("--submission", type=Path, required=True)
    parser.add_argument("--case-input", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--evidence-dir", type=Path, required=True)
    parser.add_argument("--env-file", type=Path, required=True)
    parser.add_argument("--timeout", type=int, default=600)
    parser.add_argument("--memory-limit-kib", type=int, default=4 * 1024 * 1024)
    args = parser.parse_args()
    evidence = args.evidence_dir.resolve()
    evidence.mkdir(parents=True, exist_ok=True)
    output = args.output.resolve()
    if output.exists():
        shutil.rmtree(output)
    output.mkdir(parents=True, exist_ok=True)
    runtime_root = evidence / "runtime"
    env = load_env(args.env_file.resolve(), args.python.resolve(), runtime_root)
    probe_script = Path(__file__).with_name("probe_providers.py")
    provider_health = evidence / "provider_health.json"
    try:
        subprocess.run([sys.executable, str(probe_script), "--output", str(provider_health)], env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=120, check=False)
    except subprocess.TimeoutExpired:
        pass
    if not provider_health.is_file():
        write_json(provider_health, {"protocol_version": PROTOCOL_VERSION, "observed_at": datetime.now(timezone.utc).isoformat(), "providers": [{"provider": "gateway", "status": "probe_failed", "attempts": []}, {"provider": "serper", "status": "probe_failed", "attempts": []}]})
    command = [str(args.python.resolve()), "run_agent.py", "--input", str(args.case_input.resolve()), "--output", str(output)]
    (evidence / "command.txt").write_text("<ENV_PREFIX>/bin/python run_agent.py --input <ACTIVE_CASE>/input.md --output <FRESH_OUTPUT>\n", encoding="utf-8")
    started_wall = datetime.now(timezone.utc)
    started = time.monotonic()
    timed_out = False
    memory_exceeded = False
    peak_process_tree_rss_kib = 0
    memory_sample_count = 0
    exit_code: int | None = None
    process: subprocess.Popen[str] | None = None
    candidate_launched = False
    launch_error: str | None = None
    try:
        process = subprocess.Popen(
            command,
            cwd=args.submission.resolve(),
            env=env,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            start_new_session=True,
        )
        candidate_launched = True
        while process.poll() is None:
            current_rss = tree_rss_kib(process.pid)
            memory_sample_count += 1
            peak_process_tree_rss_kib = max(
                peak_process_tree_rss_kib, current_rss
            )
            if current_rss > args.memory_limit_kib:
                memory_exceeded = True
                os.killpg(process.pid, signal.SIGKILL)
                break
            if time.monotonic() - started > args.timeout:
                timed_out = True
                os.killpg(process.pid, signal.SIGKILL)
                break
            time.sleep(0.25)
        stdout, stderr = process.communicate()
        exit_code = process.returncode
        (evidence / "stdout.txt").write_text(redact(stdout, env), encoding="utf-8")
        (evidence / "stderr.txt").write_text(redact(stderr, env), encoding="utf-8")
    except OSError as exc:
        exit_code = 125
        launch_error = f"{type(exc).__name__}: {exc}"
        (evidence / "stdout.txt").write_text("", encoding="utf-8")
        (evidence / "stderr.txt").write_text(redact(launch_error, env), encoding="utf-8")
    ended_wall = datetime.now(timezone.utc)
    write_json(evidence / "process_observation.json", {
        "protocol_version": PROTOCOL_VERSION,
        "candidate_launched": candidate_launched,
        "launch_error": launch_error,
        "start_utc": started_wall.isoformat(),
        "end_utc": ended_wall.isoformat(),
        "wall_seconds": round(time.monotonic() - started, 3),
        "exit_code": exit_code,
        "timed_out": timed_out,
        "memory_limit_kib": args.memory_limit_kib,
        "memory_exceeded": memory_exceeded,
        "peak_process_tree_rss_kib": peak_process_tree_rss_kib or None,
        "memory_sample_count": memory_sample_count,
        "output_dir": str(output),
        "environment_keys": sorted(env),
        "credential_environment_names": sorted(
            key for key in env if key in {"GATEWAY_API_KEY", "SERPER_TOKEN"}
        ),
        "runtime_root": str(runtime_root),
    })
    return int(exit_code or 0)


if __name__ == "__main__":
    raise SystemExit(main())
