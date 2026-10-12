#!/usr/bin/env python3
"""Run one independent xhigh Result judge for a completed smoke case.

This helper is intentionally separate from sibling pilot/native scoring.  It
starts a fresh evaluator-owned broker, sends one logical request through the
shared Result judge, records broker usage, and verifies ownership-backed
cleanup.  It never creates or changes an agent artifact.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parent
BROKER_SCRIPT = ROOT / "responses_broker_xhigh.py"
BROKER_IMAGE = "agentswe/create-agent-builder-codex:0.144.1-node24.6.0-0812"
PLACEHOLDER = "judge-only-placeholder"
STATS_PLACEHOLDER = "stats-only-placeholder"


def write_json(path: Path, value: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(value, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    os.replace(temporary, path)


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        while chunk := handle.read(1024 * 1024):
            digest.update(chunk)
    return digest.hexdigest()


def free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as listener:
        listener.bind(("127.0.0.1", 0))
        return int(listener.getsockname()[1])


def get_json(url: str, *, token: str | None = None, timeout: int = 5) -> dict[str, Any]:
    headers = {"Authorization": f"Bearer {token}"} if token else {}
    request = urllib.request.Request(url, headers=headers)
    with urllib.request.urlopen(request, timeout=timeout) as response:
        value = json.loads(response.read())
    if not isinstance(value, dict):
        raise RuntimeError(f"broker response is not an object: {url}")
    return value


def wait_health(endpoint: str, timeout: int = 45) -> dict[str, Any]:
    deadline = time.monotonic() + timeout
    last: str = "unavailable"
    while time.monotonic() < deadline:
        try:
            value = get_json(endpoint.removesuffix("/v1/responses") + "/healthz", timeout=3)
            if value.get("status") == "ok" or value.get("ok") is True:
                return value
            last = repr(value)
        except (OSError, ValueError, urllib.error.URLError) as exc:
            last = f"{type(exc).__name__}: {exc}"
        time.sleep(0.5)
    raise RuntimeError(f"Result-judge broker health timeout: {last}")


def broker_stats(endpoint: str) -> dict[str, Any]:
    return get_json(endpoint.removesuffix("/v1/responses") + "/stats", token=STATS_PLACEHOLDER, timeout=10)


def runtime_and_judge(stats: dict[str, Any]) -> tuple[dict[str, Any], dict[str, Any]]:
    runtime = stats.get("runtime") if isinstance(stats.get("runtime"), dict) else {}
    judge = stats.get("judge") if isinstance(stats.get("judge"), dict) else {}
    return runtime, judge


def inspect_present(container_id: str) -> bool:
    return subprocess.run(["docker", "inspect", container_id], capture_output=True, text=True, check=False).returncode == 0


def cleanup(run: Path, *, role: str, cidfile: Path, container_id: str | None, startup_attempted: bool) -> dict[str, Any]:
    result: dict[str, Any] = {
        "role": role,
        "cidfile": str(cidfile),
        "container_id": container_id,
        "startup_attempted": startup_attempted,
        "ownership_proven": bool(container_id),
        "cleanup_attempted": bool(container_id),
        "remove_exit_code": None,
        "inspect_exit_code": None,
        "absent_after_cleanup": False,
        "unrelated_containers_touched": False,
    }
    if not startup_attempted:
        result.update({"status": "not_started", "absent_after_cleanup": True})
        return result
    if not container_id:
        result.update({"status": "ownership_unproven", "cleanup_error": "no current-run container ID captured"})
        return result
    removed = subprocess.run(["docker", "rm", "-f", container_id], capture_output=True, text=True, check=False)
    inspected = subprocess.run(["docker", "inspect", container_id], capture_output=True, text=True, check=False)
    result.update({
        "remove_exit_code": removed.returncode,
        "inspect_exit_code": inspected.returncode,
        "absent_after_cleanup": inspected.returncode != 0,
        "status": "absent" if inspected.returncode != 0 else "present_after_cleanup",
    })
    if removed.returncode != 0 and inspected.returncode == 0:
        result["cleanup_error"] = removed.stderr[-500:]
    return result


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--case-id", required=True)
    parser.add_argument("--task-input", type=Path, required=True)
    parser.add_argument("--rubric", type=Path, required=True)
    parser.add_argument("--agent-artifact", type=Path, required=True)
    parser.add_argument("--trajectory", type=Path, required=True)
    parser.add_argument("--native-evidence", type=Path, required=True)
    parser.add_argument("--oracle-summary", type=Path, required=True)
    parser.add_argument("--credential-file", type=Path, required=True)
    parser.add_argument("--run-dir", type=Path, required=True)
    parser.add_argument("--timeout", type=int, default=900)
    args = parser.parse_args()

    run = args.run_dir.resolve()
    run.mkdir(parents=True, exist_ok=True)
    required = {
        "task_input": args.task_input.resolve(),
        "rubric": args.rubric.resolve(),
        "agent_artifact": args.agent_artifact.resolve(),
        "trajectory": args.trajectory.resolve(),
        "native_evidence": args.native_evidence.resolve(),
        "oracle_summary": args.oracle_summary.resolve(),
        "credential_file": args.credential_file.resolve(),
    }
    missing = [name for name, path in required.items() if not path.is_file()]
    if missing:
        raise SystemExit("missing required smoke judge input: " + ", ".join(missing))

    port = free_port()
    endpoint = f"http://127.0.0.1:{port}/v1/responses"
    name = "agentswe-edit-smoke-result-" + hashlib.sha256(str(run).encode()).hexdigest()[:12]
    cidfile = run / "result_judge_broker.cid"
    cidfile.unlink(missing_ok=True)
    command = [
        "docker", "run", "-d", "--rm", "--name", name, "--network", "host",
        "-v", f"{BROKER_SCRIPT.resolve()}:/broker.py:ro",
        "-v", f"{required['credential_file']}:/run/secrets/agentswe.env:ro",
        "-v", "/etc/ssl/certs:/etc/ssl/certs:ro",
        "-e", "SSL_CERT_FILE=/etc/ssl/certs/ca-certificates.crt",
        "--cidfile", str(cidfile), BROKER_IMAGE, "python3", "/broker.py",
        "--credential-file", "/run/secrets/agentswe.env", "--bind", "0.0.0.0", "--port", str(port),
    ]
    startup_attempted = True
    completed = subprocess.run(command, capture_output=True, text=True, check=False)
    container_id = cidfile.read_text(encoding="ascii").strip() if cidfile.is_file() else None
    initial: dict[str, Any] | None = None
    judge_exit = 1
    judge_error: str | None = None
    try:
        if completed.returncode != 0:
            raise RuntimeError(f"Result-judge broker startup failed: {completed.stderr[-1000:]}")
        wait_health(endpoint)
        initial = broker_stats(endpoint)
        runtime, judge = runtime_and_judge(initial)
        if any(int(runtime.get(key, 0) or 0) != 0 for key in ("calls", "failures", "tokens")):
            raise RuntimeError("fresh Result-judge broker runtime counters are not zero")
        if any(int(judge.get(key, 0) or 0) != 0 for key in ("calls", "failures", "tokens")):
            raise RuntimeError("fresh Result-judge broker judge counters are not zero")
        write_json(run / "result_judge_broker_initial.json", {
            "schema_version": "agentswe-edit-smoke-result-judge-broker-initial-v1",
            "model": "deepseek-flash", "reasoning_effort": "max",
            "endpoint": endpoint, "broker_instance_name": name,
            "container_id": container_id, "stats": initial,
            "credential_values_recorded": False,
        })
        output_dir = run / "result_judge" / args.case_id
        judge_command = [
            sys.executable, str(ROOT / "result_judge.py"),
            "--case-id", args.case_id,
            "--task-input", str(required["task_input"]),
            "--rubric", str(required["rubric"]),
            "--agent-artifact", str(required["agent_artifact"]),
            "--trajectory", str(required["trajectory"]),
            "--native-evidence", str(required["native_evidence"]),
            "--oracle-summary", str(required["oracle_summary"]),
            "--broker-endpoint", endpoint,
            "--broker-placeholder", PLACEHOLDER,
            "--output-dir", str(output_dir),
            "--timeout", str(args.timeout),
        ]
        judged = subprocess.run(judge_command, capture_output=True, text=True, check=False)
        judge_exit = judged.returncode
        if judge_exit != 0:
            judge_error = judged.stderr[-2000:] or judged.stdout[-2000:]
        final_stats = broker_stats(endpoint)
        write_json(run / "result_judge_broker_stats.json", {
            "schema_version": "agentswe-edit-smoke-result-judge-broker-stats-v1",
            "model": "deepseek-flash", "reasoning_effort": "max",
            "endpoint": endpoint, "broker_instance_name": name,
            "container_id": container_id, "stats": final_stats,
            "credential_values_recorded": False,
        })
        write_json(run / "result_judge_runner.json", {
            "schema_version": "agentswe-edit-smoke-result-judge-runner-v1",
            "case_id": args.case_id, "command_exit_code": judge_exit,
            "output_dir": str(output_dir),
            "judge_model": "deepseek-flash", "judge_effort": "max",
            "logical_requests": 1 if judge_exit == 0 else None,
            "completed_responses": 1 if judge_exit == 0 else None,
            "stderr_tail": judge_error,
        })
    except Exception as exc:
        judge_error = f"{type(exc).__name__}: {exc}"
        write_json(run / "result_judge_runner.json", {
            "schema_version": "agentswe-edit-smoke-result-judge-runner-v1",
            "case_id": args.case_id, "command_exit_code": None,
            "output_dir": str(run / "result_judge" / args.case_id),
            "judge_model": "deepseek-flash", "judge_effort": "max",
            "logical_requests": None, "completed_responses": None,
            "stderr_tail": judge_error,
        })
    finally:
        cleanup_result = cleanup(run, role="result_judge", cidfile=cidfile,
                                container_id=container_id, startup_attempted=startup_attempted)
        cleanup_attestation = {
            "schema_version": "agentswe-edit-smoke-result-judge-cleanup-v1",
            "completed": bool(cleanup_result.get("absent_after_cleanup")),
            "unrelated_containers_touched": False,
            "cleanup_results": [cleanup_result],
            "judge_error": judge_error,
        }
        write_json(run / "result_judge_cleanup_attestation.json", cleanup_attestation)

    if judge_exit != 0:
        print(json.dumps({"ok": False, "error": judge_error}, ensure_ascii=False))
        return 2
    print(json.dumps({"ok": True, "case_id": args.case_id, "run_dir": str(run)}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
