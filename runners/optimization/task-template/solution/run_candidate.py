#!/usr/bin/env python3
from __future__ import annotations

import argparse
import hashlib
import json
import os
import signal
import subprocess
import sys
from pathlib import Path


def digest(root: Path) -> str:
    h = hashlib.sha256()
    for path in sorted(root.rglob("*"), key=lambda p: p.relative_to(root).as_posix()):
        if path.is_file() and not path.is_symlink():
            h.update(b"F" + path.relative_to(root).as_posix().encode() + path.read_bytes())
    return h.hexdigest()


def load_credentials(path: Path | None) -> dict[str, str]:
    """Load a small dotenv file without exposing its values in evidence."""
    values: dict[str, str] = {}
    if path is None or not path.is_file():
        return values
    for raw in path.read_text(encoding="utf-8", errors="replace").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        if line.startswith("export "):
            line = line[7:].lstrip()
        key, value = line.split("=", 1)
        key = key.strip()
        if not key or not key.replace("_", "a").isalnum() or key[0].isdigit():
            continue
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in {"'", '"'}:
            value = value[1:-1]
        values[key] = value
    return values


def redact_bytes(data: bytes, secrets: tuple[bytes, ...]) -> tuple[bytes, bool]:
    redacted = data
    leaked = False
    for secret in secrets:
        if secret and secret in redacted:
            leaked = True
            redacted = redacted.replace(secret, b"[REDACTED_RESOURCE_SECRET]")
    return redacted, leaked


def sanitize_file(path: Path, secrets: tuple[bytes, ...]) -> bool:
    if not path.is_file() or path.is_symlink():
        return False
    redacted, leaked = redact_bytes(path.read_bytes(), secrets)
    if leaked:
        path.write_bytes(redacted)
    return leaked


def terminate_process_group(pid: int) -> None:
    try:
        os.killpg(pid, signal.SIGTERM)
    except ProcessLookupError:
        return
    try:
        os.killpg(pid, 0)
    except ProcessLookupError:
        return
    try:
        os.killpg(pid, signal.SIGKILL)
    except ProcessLookupError:
        pass


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--submission", type=Path, required=True)
    parser.add_argument("--case", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--run-dir", type=Path, required=True)
    parser.add_argument("--case-id", required=True)
    parser.add_argument("--timeout", type=int, default=900)
    args = parser.parse_args()
    args.output.mkdir(parents=True, exist_ok=True)
    args.run_dir.mkdir(parents=True, exist_ok=True)
    command = [sys.executable, str(args.submission / "run_harness.py"), "--input", str(args.case / "input.jsonl"), "--output", str(args.output / "predictions.jsonl"), "--run-dir", str(args.run_dir)]
    credential_path = os.environ.get("HARNESS_CREDENTIAL_FILE")
    credentials = load_credentials(Path(credential_path) if credential_path else None)
    secrets = tuple(
        value.encode("utf-8") for value in credentials.values() if value
    )
    candidate_env = os.environ.copy()
    candidate_env.update(credentials)
    before = digest(args.submission)
    timed_out = False
    process = subprocess.Popen(
        command,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        env=candidate_env,
        start_new_session=True,
    )
    try:
        stdout, _ = process.communicate(timeout=args.timeout)
        exit_code = process.returncode
    except subprocess.TimeoutExpired as exc:
        timed_out = True
        exit_code = 124
        stdout = (exc.stdout or "") if isinstance(exc.stdout, str) else ""
    finally:
        terminate_process_group(process.pid)
        if process.poll() is None:
            remaining, _ = process.communicate(timeout=10)
            stdout += remaining or ""
    stdout_bytes, stdout_leak = redact_bytes(stdout.encode("utf-8", errors="replace"), secrets)
    (args.run_dir / "stdout.log").write_bytes(stdout_bytes)
    prediction_leak = sanitize_file(args.output / "predictions.jsonl", secrets)
    report_leak = sanitize_file(args.run_dir / "run_report.json", secrets)
    evidence = {"schema_version": "1.2", "case_id": args.case_id, "exit_code": exit_code, "timed_out": timed_out, "submission_digest_before": before, "submission_digest_after": digest(args.submission), "predictions": str(args.output / "predictions.jsonl"), "credentials_injected": bool(credentials), "credential_leak_detected": stdout_leak or prediction_leak or report_leak, "process_logs_secret_redacted": True, "resource_mode": os.environ.get("HARNESS_RESOURCE_MODE"), "resource_evidence_owner": "harbor-sidecar-collect"}
    (args.run_dir / "candidate_evidence.json").write_text(json.dumps(evidence, indent=2) + "\n", encoding="utf-8")
    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())
