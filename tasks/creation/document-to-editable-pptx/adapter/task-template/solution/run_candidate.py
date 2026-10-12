#!/usr/bin/env python3
"""Execute one frozen candidate and write evaluator-owned process evidence."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import signal
import subprocess
import time
from datetime import datetime, timezone
from pathlib import Path


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def tree_digest(root: Path) -> str:
    root = root.resolve()
    digest = hashlib.sha256()
    for path in sorted(root.rglob("*"), key=lambda item: item.relative_to(root).as_posix()):
        relative = path.relative_to(root).as_posix().encode("utf-8")
        if path.is_symlink():
            kind = b"L"
            payload = os.readlink(path).encode("utf-8")
        elif path.is_file():
            digest.update(b"F")
            digest.update(len(relative).to_bytes(8, "big"))
            digest.update(relative)
            digest.update(path.stat().st_size.to_bytes(8, "big"))
            with path.open("rb") as handle:
                while chunk := handle.read(1024 * 1024):
                    digest.update(chunk)
            continue
        elif path.is_dir():
            continue
        else:
            kind = b"O"
            payload = b""
        digest.update(kind)
        digest.update(len(relative).to_bytes(8, "big"))
        digest.update(relative)
        digest.update(len(payload).to_bytes(8, "big"))
        digest.update(payload)
    return digest.hexdigest()


def write_json(path: Path, value: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def secret_values(path: Path) -> tuple[bytes, ...]:
    values: list[bytes] = []
    if path.is_file():
        for raw in path.read_text(encoding="utf-8", errors="replace").splitlines():
            line = raw.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            value = line.split("=", 1)[1].strip().strip('"').strip("'")
            if value:
                values.append(value.encode("utf-8"))
    return tuple(values)


def load_resource_environment(path: Path) -> dict[str, str]:
    """Load only declared resource variables without exposing their values."""
    values: dict[str, str] = {}
    if not path.is_file():
        return values
    for raw in path.read_text(encoding="utf-8", errors="replace").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        if key not in {"GATEWAY_API_KEY", "SERPER_TOKEN"}:
            continue
        values[key] = value.strip().strip('"').strip("'")
    return values


def contains_secret(path: Path, values: tuple[bytes, ...]) -> bool:
    if not values:
        return False
    paths = (
        [path]
        if path.is_file() and not path.is_symlink()
        else [p for p in path.rglob("*") if p.is_file() and not p.is_symlink()]
    )
    for file_path in paths:
        overlap = max((len(value) for value in values), default=1) - 1
        tail = b""
        with file_path.open("rb") as handle:
            while chunk := handle.read(1024 * 1024):
                data = tail + chunk
                if any(value in data for value in values):
                    return True
                tail = data[-overlap:] if overlap else b""
    return False


def redact_file(
    path: Path, values: tuple[bytes, ...],
    oversized_marker: str = "[PROCESS LOG OMITTED: exceeded 16 MiB safety limit]\n",
) -> None:
    if not path.is_file():
        return
    if path.stat().st_size > 16 * 1024 * 1024:
        path.write_text(oversized_marker, encoding="utf-8")
        return
    data = path.read_bytes()
    redacted = data
    for value in values:
        redacted = redacted.replace(value, b"[REDACTED_RESOURCE_SECRET]")
    if redacted != data:
        path.write_bytes(redacted)


def sanitize_log_path(path: Path, secrets: tuple[bytes, ...]) -> tuple[bool, bool]:
    """Return (secret_seen, unsafe_path) and leave a safe regular log file."""
    if path.is_symlink() or (path.exists() and not path.is_file()):
        if path.is_dir() and not path.is_symlink():
            shutil.rmtree(path)
        else:
            path.unlink(missing_ok=True)
        path.write_text("[PROCESS LOG OMITTED: unsafe log path]\n", encoding="utf-8")
        return True, True
    secret_seen = contains_secret(path, secrets)
    redact_file(path, secrets)
    return secret_seen, False


def unsafe_output_entries(root: Path) -> list[str]:
    if root.is_symlink() or not root.is_dir():
        return ["."]
    unsafe: list[str] = []
    for path in root.rglob("*"):
        relative = path.relative_to(root).as_posix()
        # 0916 rejudge: candidate scratch directories (top-level dot-prefixed, e.g. a bootstrapped
        # .agentswe_venv) are not deliverables. Frozen Candidates from the 0825 protocol created such
        # venvs inside the then-writable private prefix; the read-only replay mount pushes them into
        # the output directory. Symlinks there are still deleted by sanitize_output before archiving.
        if relative.split("/", 1)[0].startswith("."):
            continue
        if path.is_symlink() or (not path.is_file() and not path.is_dir()):
            unsafe.append(relative)
    return unsafe


def redact_label(value: str, secrets: tuple[bytes, ...]) -> str:
    encoded = value.encode("utf-8", errors="replace")
    for secret in secrets:
        encoded = encoded.replace(secret, b"[REDACTED_RESOURCE_SECRET]")
    return encoded.decode("utf-8", errors="replace")


def sanitize_output(
    root: Path, secrets: tuple[bytes, ...]
) -> tuple[bool, list[str]]:
    unsafe = unsafe_output_entries(root)
    credential_leak = False
    if root.is_symlink() or not root.is_dir():
        if root.is_symlink() or root.exists():
            root.unlink()
        root.mkdir(parents=True, exist_ok=True)
    paths = sorted(
        root.rglob("*"), key=lambda path: len(path.relative_to(root).parts),
        reverse=True,
    )
    for path in paths:
        relative = path.relative_to(root).as_posix()
        name_has_secret = any(
            secret in relative.encode("utf-8", errors="replace")
            for secret in secrets
        )
        is_unsafe = path.is_symlink() or (not path.is_file() and not path.is_dir())
        if name_has_secret:
            credential_leak = True
        if is_unsafe or name_has_secret:
            if path.is_symlink() or not path.is_dir():
                path.unlink(missing_ok=True)
            else:
                shutil.rmtree(path)
            continue
        if path.is_file() and contains_secret(path, secrets):
            credential_leak = True
            redact_file(
                path,
                secrets,
                "[CANDIDATE ARTIFACT OMITTED: secret leak and size limit]\n",
            )
    marker = root / ".agentswe_security_violation.json"
    if marker.is_symlink() or marker.is_file():
        marker.unlink(missing_ok=True)
    elif marker.is_dir():
        shutil.rmtree(marker)
    if credential_leak or unsafe:
        write_json(
            marker,
            {
                "credential_leak_detected": credential_leak,
                "unsafe_output_entries": [
                    redact_label(value, secrets) for value in unsafe
                ],
            },
        )
    return credential_leak, [redact_label(value, secrets) for value in unsafe]


def stop_process_group(process: subprocess.Popen[object]) -> None:
    try:
        os.killpg(process.pid, signal.SIGTERM)
    except ProcessLookupError:
        return
    time.sleep(0.2)
    try:
        os.killpg(process.pid, signal.SIGKILL)
    except ProcessLookupError:
        pass


def container_pids() -> set[int]:
    return {
        int(path.name)
        for path in Path("/proc").iterdir()
        if path.name.isdigit()
    }


def kill_new_container_processes(baseline: set[int]) -> list[int]:
    killed: set[int] = set()
    for _ in range(3):
        current = container_pids()
        targets = current - baseline - {os.getpid()}
        if not targets:
            break
        for pid in targets:
            try:
                os.kill(pid, signal.SIGKILL)
                killed.add(pid)
            except ProcessLookupError:
                pass
        time.sleep(0.1)
    return sorted(killed)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--manifest", type=Path, required=True)
    parser.add_argument("--submission", type=Path, required=True)
    parser.add_argument("--cases-root", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--evidence", type=Path, required=True)
    parser.add_argument("--stdout", type=Path, required=True)
    parser.add_argument("--stderr", type=Path, required=True)
    parser.add_argument("--timeout", type=int, required=True)
    args = parser.parse_args()

    manifest = json.loads(args.manifest.read_text(encoding="utf-8"))
    case_dir = (args.cases_root / manifest["case_id"]).resolve()
    input_path = case_dir / "input.md"
    if not input_path.is_file():
        raise FileNotFoundError(f"active case input is missing: {input_path}")
    candidate_before = tree_digest(args.submission)
    case_before = tree_digest(case_dir)
    if candidate_before != manifest["candidate_digest"]:
        raise RuntimeError("candidate digest differs from the staged manifest")
    if case_before != manifest["case_digest"]:
        raise RuntimeError("active case digest differs from the staged manifest")

    args.output.mkdir(parents=True, exist_ok=True)
    args.stdout.parent.mkdir(parents=True, exist_ok=True)
    command = [
        str(Path(manifest["container_env_prefix"]) / "bin" / "python"),
        str(args.submission / "run_agent.py"),
        "--input",
        str(input_path),
        "--output",
        str(args.output),
    ]
    started_at = utc_now()
    started = time.monotonic()
    timed_out = False
    baseline_pids = container_pids()
    with args.stdout.open("w", encoding="utf-8") as stdout, args.stderr.open(
        "w", encoding="utf-8"
    ) as stderr:
        try:
            resource_env = load_resource_environment(
                Path(str(manifest["credential_path"]))
            )
            child_env = os.environ.copy()
            child_env.update(resource_env)
            child_env["PYTHONNOUSERSITE"] = "1"
            process = subprocess.Popen(
                command,
                cwd=args.submission,
                stdout=stdout,
                stderr=stderr,
                text=True,
                env=child_env,
                start_new_session=True,
            )
        except OSError as exc:
            exit_code = 127
            stderr.write(f"Candidate interpreter could not start: {type(exc).__name__}\n")
        else:
            try:
                exit_code = process.wait(timeout=args.timeout)
            except subprocess.TimeoutExpired:
                timed_out = True
                exit_code = 124
                stop_process_group(process)
                process.wait()
                stderr.write(f"Candidate timed out after {args.timeout} seconds.\n")
            else:
                # A candidate may leave background children after its entrypoint exits.
                # End the entire isolated process group before digesting outputs.
                stop_process_group(process)
    killed_residual_pids = kill_new_container_processes(baseline_pids)

    candidate_after = tree_digest(args.submission)
    case_after = tree_digest(case_dir)
    secrets = secret_values(Path(str(manifest["credential_path"])))
    output_credential_leak, unsafe_entries = sanitize_output(args.output, secrets)
    log_results = [sanitize_log_path(path, secrets) for path in (args.stdout, args.stderr)]
    log_credential_leak = any(result[0] for result in log_results)
    unsafe_log_paths = any(result[1] for result in log_results)
    credential_leak_detected = output_credential_leak or log_credential_leak
    evidence_path_unsafe = (
        args.evidence.is_symlink()
        or (args.evidence.exists() and not args.evidence.is_file())
    )
    if evidence_path_unsafe:
        if args.evidence.is_dir() and not args.evidence.is_symlink():
            shutil.rmtree(args.evidence)
        else:
            args.evidence.unlink(missing_ok=True)
    evidence = {
        "schema_version": "1.0",
        "case_id": manifest["case_id"],
        "evaluation_mode": manifest["evaluation_mode"],
        "command": command,
        "cwd": str(args.submission),
        "started_at": started_at,
        "finished_at": utc_now(),
        "runtime_seconds": round(time.monotonic() - started, 3),
        "candidate_exit_code": exit_code,
        "timed_out": timed_out,
        "candidate_process_group_terminated": True,
        "candidate_residual_process_count": len(killed_residual_pids),
        "timeout_seconds": args.timeout,
        "candidate_digest_before": candidate_before,
        "candidate_digest_after": candidate_after,
        "candidate_unchanged": candidate_before == candidate_after,
        "case_digest_before": case_before,
        "case_digest_after": case_after,
        "case_unchanged": case_before == case_after,
        "output_digest": tree_digest(args.output),
        "credential_leak_detected": credential_leak_detected,
        "process_logs_secret_redacted": True,
        "output_structure_valid": not unsafe_entries,
        "unsafe_output_entries": unsafe_entries,
        "process_log_paths_valid": not unsafe_log_paths,
        "evidence_path_valid": not evidence_path_unsafe,
        "network_policy": "public:evaluator-owned-model-broker+serper+authorized-public-http",
        "model_protocol": {"model": "gpt-5.6-sol", "reasoning_effort": "medium", "transport_brokered": True},
        "resource_environment_present": {
            key: bool(load_resource_environment(Path(str(manifest["credential_path"]))).get(key))
            for key in ("GATEWAY_API_KEY", "SERPER_TOKEN")
        },
        "python_no_user_site": True,
        "stdout": str(args.stdout),
        "stderr": str(args.stderr),
    }
    write_json(args.evidence, evidence)
    # Always return success so Harbor proceeds to the independent verifier.
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
