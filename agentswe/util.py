"""Small helpers shared by doctor, setup and the runners (stdlib only)."""
from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import tarfile
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path


def now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def log(msg: str) -> None:
    print(f"[agentswe] {msg}", flush=True)


def run(cmd: list[str], *, env: dict | None = None, check: bool = True, capture: bool = True,
        timeout: float | None = None, cwd: str | Path | None = None) -> subprocess.CompletedProcess:
    return subprocess.run(cmd, env=env, check=check, text=True, timeout=timeout, cwd=cwd,
                          stdout=subprocess.PIPE if capture else None, stderr=subprocess.STDOUT if capture else None)


def out(cmd: list[str], **kw) -> str:
    try:
        return run(cmd, check=False, **kw).stdout.strip()
    except (OSError, subprocess.TimeoutExpired) as exc:
        return f"<error {type(exc).__name__}>"


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def read_json(path: Path, default=None):
    try:
        return json.loads(Path(path).read_text())
    except (OSError, ValueError):
        return default


def write_json(path: Path, value) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(value, indent=2, sort_keys=False) + "\n")
    os.replace(tmp, path)


def write_secret(path: Path, lines: dict[str, str]) -> None:
    """Write a 0600 dotenv file without ever echoing its values."""
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as f:
        for k, v in lines.items():
            f.write(f"{k}={v}\n")
    os.chmod(path, 0o600)


def download(url: str, dest: Path, *, timeout: float = 60, attempts: int = 3, deadline: float | None = None) -> Path:
    """Download url to dest. `timeout` bounds each socket operation; `deadline` (seconds) bounds the
    whole transfer, so a connection that trickles bytes cannot stall setup indefinitely."""
    dest.parent.mkdir(parents=True, exist_ok=True)
    tmp = dest.with_suffix(dest.suffix + ".part")
    last = None
    for i in range(attempts):
        started = time.monotonic()
        try:
            req = urllib.request.Request(url, headers={"User-Agent": "agentswe-setup/0.1"})
            with urllib.request.urlopen(req, timeout=timeout) as r, open(tmp, "wb") as f:
                while True:
                    chunk = r.read(1 << 20)
                    if not chunk:
                        break
                    f.write(chunk)
                    if deadline is not None and time.monotonic() - started > deadline:
                        raise TimeoutError(f"transfer exceeded {deadline:.0f} s")
            os.replace(tmp, dest)
            return dest
        except Exception as exc:  # noqa: BLE001
            last = exc
            tmp.unlink(missing_ok=True)
            if i + 1 < attempts:
                time.sleep(2 * (i + 1))
    raise RuntimeError(f"download failed: {url}: {type(last).__name__}: {last}")


def reachable(url: str, timeout: float = 10) -> str:
    """HTTP status of a HEAD/GET to url, or the exception class; never sends credentials."""
    try:
        req = urllib.request.Request(url, method="HEAD", headers={"User-Agent": "agentswe-doctor/0.1"})
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return str(r.status)
    except urllib.error.HTTPError as exc:
        return str(exc.code)
    except Exception as exc:  # noqa: BLE001
        return type(exc).__name__


def extract_member(archive: Path, member_suffix: str, dest: Path) -> Path:
    with tarfile.open(archive) as t:
        for m in t.getmembers():
            if m.name.endswith(member_suffix) and m.isfile():
                f = t.extractfile(m)
                dest.parent.mkdir(parents=True, exist_ok=True)
                dest.write_bytes(f.read())
                dest.chmod(0o755)
                return dest
    raise RuntimeError(f"{member_suffix} not found in {archive}")


def pid_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        return True


ONE_STOP_EXIT = re.compile(r"^===== (\S+) one_stop exit (\d+);")


def ended_one_stop(log: Path, lines: int = 20) -> dict:
    """What a background one_stop's log says once the process is gone: the exit status its wrapper logged
    ("===== <time> one_stop exit <rc>; ..."; None when the wrapper never logged one, for example when the process
    group was killed) and the log's last `lines` lines."""
    text = log.read_text(errors="replace").splitlines() if log.is_file() else []
    status = logged_at = None
    for line in reversed(text):
        match = ONE_STOP_EXIT.match(line)
        if match:
            logged_at, status = match.group(1), int(match.group(2))
            break
    return {"exit_status": status, "exit_logged_at": logged_at, "one_stop_log": str(log),
            "one_stop_log_tail": text[-lines:]}


def python() -> str:
    return sys.executable
