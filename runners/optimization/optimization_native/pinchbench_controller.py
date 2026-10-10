#!/usr/bin/env python3
"""Trusted host controller for native PinchBench/OpenClaw evaluation."""

from __future__ import annotations

import argparse
import fcntl
import hashlib
import ipaddress
import json
import os
import shutil
import subprocess
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

from pinchbench_runtime import (
    MAX_RUNTIME_CALLS,
    MAX_RUNTIME_TOKENS,
    OPENCLAW_COMMIT,
    OPENCLAW_VERSION,
    PINCH_COMMIT,
    PINCH_ROOT,
    load_prediction,
    load_task,
    openclaw_config,
    prepare_workspace,
    sha256,
    tree_digest,
    write_json,
)


ROOT = Path(__file__).resolve().parent
SNAPSHOTS = Path(os.environ.get("AGENTSWE_SNAPSHOTS", str(Path(os.environ.get("AGENTSWE_HOME", ".agentswe")) / "snapshots")))
NODE_ROOT = SNAPSHOTS / "node-runtime"
OPENCLAW_ROOT = SNAPSHOTS / "openclaw-runtime"
PYTHON_ROOT = Path(os.environ.get("AGENTSWE_STANDALONE_PYTHON312", str(Path(os.environ.get("AGENTSWE_HOME", ".agentswe")) / "tools" / "cpython-3.12")))
YAML_SITE = SNAPSHOTS / "pyyaml-site"
IMAGE = os.environ.get("AGENTSWE_OPT_BASE_IMAGE", "docker.io/library/ubuntu@sha256:33ceb71981b602c1a7443a53469e4dba065f7503eab3078a2d7a57a2ab987517")
MAX_ATTEMPTS = 3
RUNTIME_TIMEOUT_CAP = 600
PINCH_IPAM_BASE = ipaddress.ip_network(os.environ.get("AGENTSWE_PINCHBENCH_POOL", "100.64.0.0/10"))
PINCH_IPAM_PREFIX = 28
PINCH_IPAM_ROOT = Path(
    os.environ.get(
        "AGENTSWE_PINCHBENCH_COORDINATION_ROOT",
        str(Path(os.environ.get("AGENTSWE_HOME", ".agentswe")) / "coordination" / "pinchbench"),
    )
)
PINCH_IPAM_REGISTRY = PINCH_IPAM_ROOT / "pinchbench_network_ipam_registry.json"
PINCH_IPAM_LOCK = PINCH_IPAM_ROOT / ".pinchbench_network_ipam_registry.lock"
PINCH_NETWORK_CREATE_LOCK = PINCH_IPAM_ROOT / ".pinchbench_network_create.lock"
BROKER_STATS_SCRIPT = (
    "import json,urllib.request; "
    "r=urllib.request.Request('http://127.0.0.1:8080/stats',"
    "headers={'Authorization':'Bearer stats-only-placeholder'}); "
    "print(urllib.request.urlopen(r).read().decode())"
)


def run(command: list[str], *, timeout: int = 60) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        command,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
        timeout=timeout,
    )


def wait_container_absent(name: str, seconds: float = 30) -> bool:
    """The broker runs with --rm: if it had already exited, Docker 29 is still removing it after `docker rm -f`
    ("removal ... already in progress"), and its networks cannot be removed until it is gone."""
    deadline = time.monotonic() + seconds
    while True:
        try:
            if run(["docker", "inspect", name], timeout=15).returncode != 0:
                return True
        except subprocess.TimeoutExpired:
            pass
        if time.monotonic() >= deadline:
            return False
        time.sleep(1)


def allocate_network_subnets(network_key: str, count: int) -> tuple[str, ...]:
    """Assign stable, non-overlapping /28s without using Docker's default pool."""
    if count <= 0:
        raise ValueError("network subnet count must be positive")
    capacity = 1 << (PINCH_IPAM_PREFIX - PINCH_IPAM_BASE.prefixlen)
    block_size = 1 << (32 - PINCH_IPAM_PREFIX)
    PINCH_IPAM_ROOT.mkdir(parents=True, exist_ok=True)
    with PINCH_IPAM_LOCK.open("a+", encoding="utf-8") as lock:
        fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
        if PINCH_IPAM_REGISTRY.is_file():
            registry = json.loads(PINCH_IPAM_REGISTRY.read_text(encoding="utf-8"))
        else:
            registry = {
                "schema_version": "1.0",
                "base_cidr": str(PINCH_IPAM_BASE),
                "subnet_prefix": PINCH_IPAM_PREFIX,
                "allocations": {},
            }
        if (
            registry.get("base_cidr") != str(PINCH_IPAM_BASE)
            or registry.get("subnet_prefix") != PINCH_IPAM_PREFIX
            or not isinstance(registry.get("allocations"), dict)
        ):
            raise RuntimeError("pinchbench IPAM registry contract mismatch")
        allocations = registry["allocations"]
        current = allocations.get(network_key, [])
        if not isinstance(current, list) or any(not isinstance(row, str) for row in current):
            raise RuntimeError("pinchbench IPAM registry allocation is invalid")
        used = {
            subnet
            for values in allocations.values()
            if isinstance(values, list)
            for subnet in values
            if isinstance(subnet, str)
        }
        for slot in range(capacity):
            if len(current) >= count:
                break
            address = int(PINCH_IPAM_BASE.network_address) + slot * block_size
            subnet = str(ipaddress.ip_network((address, PINCH_IPAM_PREFIX)))
            if subnet not in used:
                current.append(subnet)
                used.add(subnet)
        if len(current) != count:
            raise RuntimeError("pinchbench IPAM registry exhausted")
        allocations[network_key] = current
        temporary = PINCH_IPAM_REGISTRY.with_suffix(".json.tmp")
        temporary.write_text(json.dumps(registry, indent=2, sort_keys=True) + "\n", encoding="utf-8")
        temporary.replace(PINCH_IPAM_REGISTRY)
        return tuple(current)


def create_network(name: str, subnet: str, *, internal: bool) -> subprocess.CompletedProcess[str]:
    parsed = ipaddress.ip_network(subnet)
    if parsed.prefixlen != PINCH_IPAM_PREFIX or not parsed.subnet_of(PINCH_IPAM_BASE):
        raise ValueError(f"pinchbench subnet outside locked pool: {subnet}")
    command = [
        "docker", "network", "create",
        "--label", "agentswe.pinchbench.network=1",
        "--label", f"agentswe.pinchbench.owner={name}",
        "--subnet", str(parsed),
    ]
    if internal:
        command.append("--internal")
    command.append(name)
    PINCH_IPAM_ROOT.mkdir(parents=True, exist_ok=True)
    with PINCH_NETWORK_CREATE_LOCK.open("a+", encoding="utf-8") as lock:
        fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
        return run(command)


def docker_error(result: subprocess.CompletedProcess[str]) -> str:
    detail = (result.stderr or result.stdout or "unknown docker error").strip()
    return detail[-1000:]


def broker_stats(container: str) -> dict[str, Any]:
    result = run(
        [
            "docker",
            "exec",
            container,
            "/python/bin/python3",
            "-c",
            BROKER_STATS_SCRIPT,
        ]
    )
    if result.returncode != 0:
        raise RuntimeError("broker_stats_unavailable")
    try:
        value = json.loads(result.stdout)
    except json.JSONDecodeError as exc:
        raise RuntimeError("broker_stats_invalid") from exc
    if not isinstance(value, dict):
        raise RuntimeError("broker_stats_invalid")
    return value


def infrastructure_result(
    *, case_id: str, task_id: str, errors: list[str], attempts: list[dict[str, Any]]
) -> dict[str, Any]:
    return {
        "schema_version": "1.0",
        "case_id": case_id,
        "native_task_id": task_id,
        "official_evaluation": False,
        "validity_gate": False,
        "infrastructure_failure": True,
        "score": 0,
        "reward": 0,
        "errors": errors,
        "provider_attempts": attempts,
        "credential_brokered": True,
        "runtime_network": "public-plus-locked-model-broker",
        "grader_network": "internal-only",
        "pinchbench_source_commit": PINCH_COMMIT,
        "openclaw_source_commit": OPENCLAW_COMMIT,
        "openclaw_version": OPENCLAW_VERSION,
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--case-id", required=True)
    parser.add_argument("--task-id", required=True)
    parser.add_argument("--predictions", type=Path, required=True)
    parser.add_argument("--credential-file", type=Path, required=True)
    parser.add_argument("--output-dir", type=Path, required=True)
    args = parser.parse_args()
    args.predictions = args.predictions.resolve()
    args.credential_file = args.credential_file.resolve()
    args.output_dir = args.output_dir.resolve()
    args.output_dir.mkdir(parents=True, exist_ok=True)

    try:
        task = load_task(args.task_id)
    except Exception as exc:
        write_json(
            args.output_dir / "native_result.json",
            infrastructure_result(
                case_id=args.case_id,
                task_id=args.task_id,
                errors=[f"pinchbench_task_load_or_source_failure:{type(exc).__name__}"],
                attempts=[],
            ),
        )
        return 0
    try:
        row, agent = load_prediction(args.predictions, args.case_id)
    except Exception as exc:
        write_json(
            args.output_dir / "native_result.json",
            {
                "schema_version": "1.0",
                "case_id": args.case_id,
                "native_task_id": args.task_id,
                "official_evaluation": True,
                "validity_gate": True,
                "infrastructure_failure": False,
                "ordinary_agent_failure": True,
                "score": 0,
                "reward": 0,
                "errors": [f"invalid_prediction_or_task:{exc}"],
                "pinchbench_source_commit": PINCH_COMMIT,
                "openclaw_source_commit": OPENCLAW_COMMIT,
                "openclaw_version": OPENCLAW_VERSION,
            },
        )
        return 0

    frozen = args.output_dir / "frozen_prediction.json"
    write_json(frozen, row)
    prediction_digest = sha256(frozen)
    task_digest = sha256(PINCH_ROOT / "tasks" / f"{args.task_id}.md")
    token = hashlib.sha256(
        (args.case_id + args.task_id + prediction_digest + str(args.output_dir)).encode("utf-8")
    ).hexdigest()[:12]
    attempts: list[dict[str, Any]] = []

    for attempt in range(1, MAX_ATTEMPTS + 1):
        attempt_dir = args.output_dir / f"attempt_{attempt:03d}"
        workspace = attempt_dir / "workspace"
        state = attempt_dir / "state"
        runtime_output = attempt_dir / "runtime"
        grader_output = attempt_dir / "grade.json"
        public_network = f"agentswe_pinch_public_{token}_{attempt}"
        grader_network = f"agentswe_pinch_grader_{token}_{attempt}"
        broker = f"agentswe-pinch-broker-{token}-{attempt}"
        public_subnet, grader_subnet = allocate_network_subnets(
            f"{public_network}:{grader_network}", 2
        )
        attempt_dir.mkdir(parents=True, exist_ok=True)
        prepare_workspace(task, workspace, agent)
        state.mkdir(parents=True, exist_ok=True)
        write_json(state / "openclaw.json", openclaw_config(agent))
        prompt = attempt_dir / "task_prompt.md"
        prompt.write_text(task.prompt + "\n", encoding="utf-8")
        public_create = create_network(public_network, public_subnet, internal=False)
        if public_create.returncode != 0:
            attempts.append({
                "attempt": attempt,
                "error": "public_network_create_failed",
                "subnet": public_subnet,
                "docker_error": docker_error(public_create),
            })
            continue
        grader_create = create_network(grader_network, grader_subnet, internal=True)
        if grader_create.returncode != 0:
            run(["docker", "network", "rm", public_network], timeout=30)
            attempts.append({
                "attempt": attempt,
                "error": "grader_network_create_failed",
                "subnet": grader_subnet,
                "docker_error": docker_error(grader_create),
            })
            continue
        try:
            broker_run = run(
                [
                    "docker",
                    "run",
                    "-d",
                    "--rm",
                    "--name",
                    broker,
                    "--network",
                    public_network,
                    "--network-alias",
                    "model-broker",
                    "-v",
                    f"{PYTHON_ROOT}:/python:ro",
                    "-v",
                    "/etc/ssl/certs:/etc/ssl/certs:ro",
                    "-v",
                    f"{ROOT / 'responses_broker.py'}:/broker.py:ro",
                    "-v",
                    f"{args.credential_file.resolve()}:/run/secrets/agentswe.env:ro",
                    "-e",
                    "SSL_CERT_FILE=/etc/ssl/certs/ca-certificates.crt",
                    IMAGE,
                    "/python/bin/python3",
                    "/broker.py",
                    "--credential-file",
                    "/run/secrets/agentswe.env",
                    "--max-runtime-calls",
                    str(MAX_RUNTIME_CALLS),
                    "--max-runtime-tokens",
                    str(MAX_RUNTIME_TOKENS),
                ],
                timeout=60,
            )
            if broker_run.returncode != 0:
                attempts.append({"attempt": attempt, "error": "broker_start_failed"})
                continue
            if run(
                ["docker", "network", "connect", "--alias", "model-broker", grader_network, broker]
            ).returncode != 0:
                attempts.append({"attempt": attempt, "error": "broker_internal_network_failed"})
                continue
            time.sleep(1)
            runtime = run(
                [
                    "docker",
                    "run",
                    "--rm",
                    "--network",
                    public_network,
                    "-v",
                    f"{NODE_ROOT}:/node:ro",
                    "-v",
                    f"{OPENCLAW_ROOT}:/openclaw:ro",
                    "-v",
                    f"{ROOT / 'pinchbench_runtime.py'}:/runtime.py:ro",
                    "-v",
                    f"{workspace}:/workspace",
                    "-v",
                    f"{state}:/state",
                    "-v",
                    f"{prompt}:/task_prompt.md:ro",
                    "-v",
                    f"{runtime_output}:/output",
                    "-e",
                    "PATH=/node/bin:/openclaw/node_modules/.bin:/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin",
                    "-e",
                    "OPENCLAW_STATE_DIR=/state",
                    IMAGE,
                    "/node/bin/node",
                    "/openclaw/node_modules/openclaw/openclaw.mjs",
                    "agent",
                    "--help",
                ],
                timeout=120,
            )
            (attempt_dir / "runtime.probe.stdout.log").write_text(
                runtime.stdout[-20_000:], encoding="utf-8"
            )
            (attempt_dir / "runtime.probe.stderr.log").write_text(
                runtime.stderr[-20_000:], encoding="utf-8"
            )
            # The help probe proves mounted runtime integrity before the Python entrypoint.
            help_text = runtime.stdout + "\n" + runtime.stderr
            if (
                runtime.returncode != 0
                or "Usage: openclaw agent" not in help_text
                or "--local" not in help_text
            ):
                attempts.append({"attempt": attempt, "error": "openclaw_runtime_probe_failed"})
                continue
            runtime_timeout = min(RUNTIME_TIMEOUT_CAP, max(60, int(task.timeout_seconds)))
            runtime = run(
                [
                    "docker", "run", "--rm", "--network", public_network,
                    "-v", f"{NODE_ROOT}:/node:ro",
                    "-v", f"{OPENCLAW_ROOT}:/openclaw:ro",
                    "-v", f"{PYTHON_ROOT}:/python:ro",
                    "-v", f"{ROOT / 'pinchbench_runtime.py'}:/runtime.py:ro",
                    "-v", f"{workspace}:/workspace",
                    "-v", f"{state}:/state",
                    "-v", f"{prompt}:/task_prompt.md:ro",
                    "-v", f"{runtime_output}:/output",
                    "-e", "PATH=/node/bin:/openclaw/node_modules/.bin:/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin",
                    "-e", "OPENCLAW_STATE_DIR=/state",
                    IMAGE, "/python/bin/python3", "/runtime.py",
                    "--case-id", args.case_id, "--task-id", args.task_id,
                    "--prompt", "/task_prompt.md", "--workspace", "/workspace",
                    "--state", "/state", "--output-dir", "/output",
                    "--timeout", str(runtime_timeout),
                ],
                timeout=runtime_timeout + 90,
            )
            (attempt_dir / "runtime.container.stdout.log").write_text(runtime.stdout[-20_000:], encoding="utf-8")
            (attempt_dir / "runtime.container.stderr.log").write_text(runtime.stderr[-20_000:], encoding="utf-8")
            execution_path = runtime_output / "execution_result.json"
            if runtime.returncode != 0 or not execution_path.is_file():
                attempts.append({"attempt": attempt, "error": "openclaw_runtime_process_failed"})
                continue
            execution = json.loads(execution_path.read_text(encoding="utf-8"))
            try:
                stats_before_grade = broker_stats(broker)
            except RuntimeError:
                attempts.append({"attempt": attempt, "error": "broker_stats_failed_before_grade"})
                continue
            if stats_before_grade.get("runtime", {}).get("failures", 0):
                attempts.append({"attempt": attempt, "error": "runtime_provider_failure", "broker_stats": stats_before_grade})
                continue
            if execution.get("status") != "success":
                # A clean timeout or tool/runtime error is an ordinary agent outcome if the provider worked.
                ordinary_runtime_failure = True
            else:
                ordinary_runtime_failure = False

            grader = run(
                [
                    "docker", "run", "--rm", "--network", grader_network,
                    "-v", f"{PYTHON_ROOT}:/python:ro",
                    "-v", f"{YAML_SITE}:/yaml-site:ro",
                    "-v", f"{PINCH_ROOT}:/pinchbench:ro",
                    "-v", f"{ROOT / 'pinchbench_grader.py'}:/grader.py:ro",
                    "-v", f"{execution_path}:/execution_result.json:ro",
                    "-v", f"{workspace}:/workspace:ro",
                    "-v", f"{attempt_dir}:/output",
                    "-e", "PYTHONPATH=/yaml-site:/pinchbench/scripts",
                    IMAGE, "/python/bin/python3", "/grader.py",
                    "--task-id", args.task_id,
                    "--execution-result", "/execution_result.json",
                    "--output", "/output/grade.json",
                ],
                timeout=700,
            )
            (attempt_dir / "grader.stdout.log").write_text(grader.stdout[-20_000:], encoding="utf-8")
            (attempt_dir / "grader.stderr.log").write_text(grader.stderr[-20_000:], encoding="utf-8")
            if grader.returncode != 0 or not grader_output.is_file():
                attempts.append({"attempt": attempt, "error": "official_grader_process_failed"})
                continue
            grade = json.loads(grader_output.read_text(encoding="utf-8"))
            try:
                stats = broker_stats(broker)
            except RuntimeError:
                attempts.append({"attempt": attempt, "error": "broker_stats_failed_after_grade"})
                continue
            if grade.get("judge_infrastructure_failure") is True or stats.get("judge", {}).get("failures", 0):
                attempts.append({"attempt": attempt, "error": "judge_provider_failure", "broker_stats": stats})
                continue
            score = max(0, min(100, round(float(grade.get("score", 0)) * 100)))
            result = {
                "schema_version": "1.0",
                "case_id": args.case_id,
                "native_task_id": args.task_id,
                "native_task_digest": task_digest,
                "prediction_digest": prediction_digest,
                "official_evaluation": True,
                "validity_gate": True,
                "infrastructure_failure": False,
                "score": score,
                "reward": score / 100,
                "errors": ["openclaw_execution_failed"] if ordinary_runtime_failure else [],
                "grading_type": grade.get("grading_type"),
                "grade_breakdown": grade.get("breakdown", {}),
                "grade_notes": grade.get("notes", ""),
                "official_grade_task": grade.get("official_grade_task") is True,
                "execution_status": execution.get("status"),
                "workspace_digest": execution.get("workspace_digest"),
                "trajectory_digest": execution.get("transcript_digest"),
                "usage": execution.get("usage", {}),
                "broker_stats": stats,
                "provider_attempts": attempts + [{"attempt": attempt, "ok": True}],
                "credential_brokered": True,
                "runtime_network": "public-plus-locked-model-broker",
                "grader_network": "internal-only",
                "pinchbench_source_commit": PINCH_COMMIT,
                "openclaw_source_commit": OPENCLAW_COMMIT,
                "openclaw_version": OPENCLAW_VERSION,
                "openclaw_package_digest": sha256(OPENCLAW_ROOT / "node_modules/openclaw/package.json"),
                "node_version": "24.19.0",
                "node_binary_digest": sha256(NODE_ROOT / "bin/node"),
                "workspace_isolated": True,
                "grader_evaluator_owned": True,
            }
            write_json(args.output_dir / "native_result.json", result)
            if (runtime_output / "transcript.jsonl").is_file():
                shutil.copyfile(runtime_output / "transcript.jsonl", args.output_dir / "transcript.jsonl")
            return 0
        finally:
            run(["docker", "rm", "-f", broker], timeout=30)
            wait_container_absent(broker)
            run(["docker", "network", "rm", public_network], timeout=30)
            run(["docker", "network", "rm", grader_network], timeout=30)

    write_json(
        args.output_dir / "native_result.json",
        infrastructure_result(
            case_id=args.case_id,
            task_id=args.task_id,
            errors=["pinchbench_provider_or_runtime_failed_after_retries"],
            attempts=attempts,
        ),
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
