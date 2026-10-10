#!/usr/bin/env python3
"""Trusted host controller for isolated official τ³ episodes."""

from __future__ import annotations

import argparse
import fcntl
import hashlib
import ipaddress
import json
import os
import re
import shutil
import subprocess
import time
from pathlib import Path


ROOT = Path(__file__).resolve().parent
TAU_ROOT = Path(os.environ.get("AGENTSWE_SNAPSHOTS", str(Path(os.environ.get("AGENTSWE_HOME", ".agentswe")) / "snapshots"))) / "tau2-bench"
PYTHON_ROOT = Path(os.environ.get("AGENTSWE_STANDALONE_PYTHON312", str(Path(os.environ.get("AGENTSWE_HOME", ".agentswe")) / "tools" / "cpython-3.12")))
IMAGE = os.environ.get("AGENTSWE_OPT_BASE_IMAGE", "docker.io/library/ubuntu@sha256:33ceb71981b602c1a7443a53469e4dba065f7503eab3078a2d7a57a2ab987517")
MAX_ATTEMPTS = 3
MAX_RUNTIME_CALLS = 50
MAX_RUNTIME_TOKENS = 150_000
TAU_COMMIT = "668d3bcd135c02aa3438f987ef45735b7c163ee3"
TAU_IPAM_BASE = ipaddress.ip_network(os.environ.get("AGENTSWE_TAU3_POOL", "198.18.0.0/15"))
TAU_IPAM_PREFIX = 28
TAU_IPAM_ROOT = Path(
    os.environ.get(
        "AGENTSWE_TAU3_COORDINATION_ROOT",
        str(Path(os.environ.get("AGENTSWE_HOME", ".agentswe")) / "coordination" / "tau3"),
    )
)
TAU_IPAM_REGISTRY = TAU_IPAM_ROOT / "network_ipam_registry.json"
TAU_IPAM_LOCK = TAU_IPAM_ROOT / ".network_ipam_registry.lock"
TAU_NETWORK_CREATE_LOCK = TAU_IPAM_ROOT / ".network_create.lock"


def write_json(path: Path, value: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def allocate_network_subnet(network_key: str) -> str:
    """Share the evaluator's locked benchmarking-only /28 registry."""
    capacity = 1 << (TAU_IPAM_PREFIX - TAU_IPAM_BASE.prefixlen)
    block_size = 1 << (32 - TAU_IPAM_PREFIX)
    TAU_IPAM_ROOT.mkdir(parents=True, exist_ok=True)
    with TAU_IPAM_LOCK.open("a+", encoding="utf-8") as lock:
        fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
        if TAU_IPAM_REGISTRY.is_file():
            registry = json.loads(TAU_IPAM_REGISTRY.read_text(encoding="utf-8"))
        else:
            registry = {
                "schema_version": "1.0",
                "base_cidr": str(TAU_IPAM_BASE),
                "subnet_prefix": TAU_IPAM_PREFIX,
                "assignments": {},
            }
        if (
            registry.get("base_cidr") != str(TAU_IPAM_BASE)
            or registry.get("subnet_prefix") != TAU_IPAM_PREFIX
            or not isinstance(registry.get("assignments"), dict)
        ):
            raise RuntimeError("tau3 IPAM registry contract mismatch")
        assignments = registry["assignments"]
        current = list(assignments.get(network_key, []))
        if not isinstance(current, list) or any(not isinstance(row, str) for row in current):
            raise RuntimeError("tau3 IPAM registry allocation is invalid")
        if len(current) > 1:
            raise RuntimeError("tau3 IPAM registry allocation count is invalid")
        used = {
            subnet
            for values in assignments.values()
            if isinstance(values, list)
            for subnet in values
            if isinstance(subnet, str)
        }
        for slot in range(capacity):
            if current:
                break
            address = int(TAU_IPAM_BASE.network_address) + slot * block_size
            subnet = str(ipaddress.ip_network((address, TAU_IPAM_PREFIX)))
            if subnet not in used:
                current.append(subnet)
                used.add(subnet)
        if len(current) != 1:
            raise RuntimeError("tau3 IPAM registry exhausted")
        assignments[network_key] = current
        write_json(TAU_IPAM_REGISTRY, registry)
        return current[0]


def create_network(name: str, subnet: str) -> subprocess.CompletedProcess[str]:
    parsed = ipaddress.ip_network(subnet)
    if parsed.prefixlen != TAU_IPAM_PREFIX or not parsed.subnet_of(TAU_IPAM_BASE):
        raise ValueError(f"tau3 subnet outside locked pool: {subnet}")
    command = [
        "docker", "network", "create",
        "--label", "agentswe.tau3.network=1",
        "--label", f"agentswe.tau3.owner={name}",
        "--subnet", str(parsed),
        "--internal",
        name,
    ]
    TAU_IPAM_ROOT.mkdir(parents=True, exist_ok=True)
    with TAU_NETWORK_CREATE_LOCK.open("a+", encoding="utf-8") as lock:
        fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
        try:
            created = run(command)
        except subprocess.TimeoutExpired:
            created = None
        deadline = time.monotonic() + 120
        while time.monotonic() < deadline:
            if network_matches_contract(name, subnet):
                return subprocess.CompletedProcess(command, 0, stdout=name + "\n", stderr="")
            time.sleep(2)
        if created is not None:
            return created
        return subprocess.CompletedProcess(
            command, 1, stdout="", stderr="network create timed out and contract was not observed"
        )


def network_matches_contract(name: str, subnet: str) -> bool:
    try:
        inspected = run(["docker", "network", "inspect", name], timeout=15)
    except subprocess.TimeoutExpired:
        return False
    if inspected.returncode != 0:
        return False
    try:
        rows = json.loads(inspected.stdout)
        row = rows[0]
        labels = row.get("Labels") or {}
        subnets = {
            item.get("Subnet")
            for item in (row.get("IPAM", {}).get("Config") or [])
            if isinstance(item, dict)
        }
        return bool(
            row.get("Internal") is True
            and subnet in subnets
            and labels.get("agentswe.tau3.network") == "1"
            and labels.get("agentswe.tau3.owner") == name
        )
    except (IndexError, KeyError, TypeError, json.JSONDecodeError):
        return False


def docker_object_absent(kind: str, name: str) -> bool:
    command = ["docker", "inspect", name] if kind == "container" else ["docker", "network", "inspect", name]
    try:
        inspected = run(command, timeout=15)
    except subprocess.TimeoutExpired:
        return False
    return inspected.returncode != 0


def remove_docker_object(kind: str, name: str) -> bool:
    command = ["docker", "rm", "-f", name] if kind == "container" else ["docker", "network", "rm", name]
    for _ in range(2):
        try:
            run(command, timeout=30)
        except subprocess.TimeoutExpired:
            pass
        deadline = time.monotonic() + 45
        while time.monotonic() < deadline:
            if docker_object_absent(kind, name):
                return True
            time.sleep(2)
    return docker_object_absent(kind, name)


def docker_error(result: subprocess.CompletedProcess[str]) -> str:
    detail = (result.stderr or result.stdout or "unknown docker error").strip()
    return detail[-1000:]


def prediction(path: Path, case_id: str) -> dict:
    rows = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]
    if len(rows) != 1 or rows[0].get("id") != case_id:
        raise ValueError("prediction row missing or id mismatch")
    agent = rows[0].get("agent")
    if not isinstance(agent, dict) or agent.get("kind") not in {"tau3_half_duplex", "tau3-half-duplex"}:
        raise ValueError("prediction requires tau3_half_duplex agent spec")
    allowed = {"kind", "strategy_prompt"}
    if set(agent) - allowed:
        raise ValueError("unsupported agent spec fields")
    return rows[0]


def run(command: list[str], *, timeout: int = 60) -> subprocess.CompletedProcess[str]:
    return subprocess.run(command, text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=False, timeout=timeout)


def broker_stats(container: str) -> dict:
    result = run([
        "docker", "exec", container, "/python/bin/python3", "-c",
        "import json,urllib.request; r=urllib.request.Request("
        "'http://127.0.0.1:8080/stats',headers={'Authorization':"
        "'Bearer stats-only-placeholder'}); print(urllib.request.urlopen(r).read().decode())",
    ])
    if result.returncode != 0:
        raise RuntimeError("broker_stats_unavailable")
    value = json.loads(result.stdout)
    if not isinstance(value, dict):
        raise RuntimeError("broker_stats_invalid")
    return value


def provider_failed(stats: dict) -> bool:
    runtime = stats.get("runtime")
    return bool(isinstance(runtime, dict) and int(runtime.get("failures", 0) or 0) > 0)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--case-id", required=True)
    parser.add_argument("--domain", choices=("airline", "retail"), required=True)
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
        row = prediction(args.predictions, args.case_id)
    except Exception as exc:
        write_json(args.output_dir / "native_result.json", {
            "benchmark": f"tau3-{args.domain}-half-duplex",
            "official_evaluation": True, "validity_gate": True,
            "infrastructure_failure": False, "ordinary_agent_failure": True,
            "score": 0, "reward": 0, "case_id": args.case_id,
            "domain": args.domain, "task_id": args.task_id,
            "errors": [f"invalid_prediction:{exc}"],
        })
        return 0
    frozen = args.output_dir / "frozen_prediction.json"
    write_json(frozen, row)
    run_token = hashlib.sha256((
        args.case_id + args.domain + args.task_id + json.dumps(row, sort_keys=True)
        + str(args.output_dir)
    ).encode()).hexdigest()[:12]
    attempts: list[dict] = []
    for attempt in range(1, MAX_ATTEMPTS + 1):
        network = f"agentswe_tau3_{run_token}_{attempt}"
        broker = f"agentswe-tau3-broker-{run_token}-{attempt}"
        attempt_dir = args.output_dir / f"attempt_{attempt:03d}"
        attempt_dir.mkdir(parents=True, exist_ok=True)
        subnet = allocate_network_subnet(network)
        created = create_network(network, subnet)
        if created.returncode != 0:
            attempts.append({
                "attempt": attempt,
                "infrastructure_failure": True,
                "error": "network_create_failed",
                "subnet": subnet,
                "docker_error": docker_error(created),
            })
            continue
        try:
            broker_run = run([
                "docker", "run", "-d", "--rm", "--name", broker,
                "--network", "bridge",
                "-v", f"{PYTHON_ROOT}:/python:ro",
                "-v", "/etc/ssl/certs:/etc/ssl/certs:ro",
                "-v", f"{ROOT / 'responses_broker.py'}:/broker.py:ro",
                "-v", f"{args.credential_file.resolve()}:/run/secrets/agentswe.env:ro",
                "-e", "SSL_CERT_FILE=/etc/ssl/certs/ca-certificates.crt",
                IMAGE, "/python/bin/python3", "/broker.py", "--credential-file", "/run/secrets/agentswe.env",
                "--max-runtime-calls", str(MAX_RUNTIME_CALLS),
                "--max-runtime-tokens", str(MAX_RUNTIME_TOKENS),
            ])
            if broker_run.returncode != 0:
                attempts.append({"attempt": attempt, "infrastructure_failure": True, "error": "broker_start_failed"})
                continue
            if run(["docker", "network", "connect", "--alias", "model-broker", network, broker]).returncode != 0:
                attempts.append({"attempt": attempt, "infrastructure_failure": True, "error": "broker_network_failed"})
                continue
            time.sleep(1)
            runtime = run([
                "docker", "run", "--rm", "--network", network,
                "-v", f"{TAU_ROOT}:/tau2:ro",
                "-v", f"{PYTHON_ROOT}:/python:ro",
                "-v", "/etc/ssl/certs:/etc/ssl/certs:ro",
                "-v", f"{ROOT / 'tau3_runtime.py'}:/runtime.py:ro",
                "-v", f"{frozen}:/prediction.json:ro",
                "-v", f"{attempt_dir}:/output",
                "-e", "PYTHONPATH=/tau2/src:/tau2/.venv/lib/python3.12/site-packages",
                "-e", "SSL_CERT_FILE=/etc/ssl/certs/ca-certificates.crt",
                "-e", "OPENAI_API_KEY=broker-only-placeholder",
                "-e", "OPENAI_BASE_URL=http://model-broker:8080/v1",
                IMAGE, "/python/bin/python3", "/runtime.py",
                "--domain", args.domain, "--task-id", args.task_id,
                "--case-id", args.case_id,
                "--prediction", "/prediction.json", "--output-dir", "/output",
            ], timeout=1000)
            (attempt_dir / "runtime.stdout.log").write_text(runtime.stdout[-20000:], encoding="utf-8")
            (attempt_dir / "runtime.stderr.log").write_text(runtime.stderr[-20000:], encoding="utf-8")
            try:
                stats = broker_stats(broker)
                write_json(attempt_dir / "broker_stats.json", stats)
            except Exception:
                attempts.append({"attempt": attempt, "infrastructure_failure": True, "error": "broker_stats_failed"})
                continue
            runtime_stats = stats.get("runtime", {})
            if provider_failed(stats):
                attempts.append({
                    "attempt": attempt,
                    "infrastructure_failure": True,
                    "error": "runtime_provider_failure",
                    "broker_stats": stats,
                })
                continue
            budget_exceeded = bool(
                isinstance(runtime_stats, dict)
                and runtime_stats.get("budget_exceeded") is True
            )
            if budget_exceeded:
                value = {
                    "schema_version": "1.0",
                    "benchmark": f"tau3-{args.domain}-half-duplex",
                    "case_id": args.case_id, "domain": args.domain,
                    "task_id": args.task_id,
                    "official_evaluation": True, "validity_gate": True,
                    "infrastructure_failure": False, "score": 0, "reward": 0,
                    "budget_exceeded": True, "broker_stats": stats,
                    "errors": ["tau3_episode_token_budget_exceeded"],
                    "provider_attempts": attempts,
                    "credential_brokered": True, "runtime_network": "internal-only",
                    "tau_source_commit": TAU_COMMIT,
                    "tau_lock_digest": sha256(TAU_ROOT / "uv.lock"),
                }
                write_json(args.output_dir / "native_result.json", value)
                return 0
            native = attempt_dir / "native_result.json"
            if runtime.returncode == 0 and native.is_file():
                value = json.loads(native.read_text(encoding="utf-8"))
                attempts.append({
                    "attempt": attempt,
                    "official_evaluation": value.get("official_evaluation") is True,
                    "infrastructure_failure": value.get("infrastructure_failure") is True,
                    "result": str(native),
                })
                if value.get("official_evaluation") is True:
                    value["broker_stats"] = stats
                    value["budget_exceeded"] = False
                    value["provider_attempts"] = attempts
                    value["credential_brokered"] = True
                    value["runtime_network"] = "internal-only"
                    value["tau_source_commit"] = TAU_COMMIT
                    value["tau_lock_digest"] = sha256(TAU_ROOT / "uv.lock")
                    write_json(args.output_dir / "native_result.json", value)
                    shutil.copy2(attempt_dir / "trajectory.json", args.output_dir / "trajectory.json")
                    return 0
            else:
                attempts.append({"attempt": attempt, "infrastructure_failure": True, "error": "runtime_process_failed"})
        finally:
            broker_removed = remove_docker_object("container", broker)
            network_removed = remove_docker_object("network", network)
            if not broker_removed or not network_removed:
                raise RuntimeError(
                    f"tau3_cleanup_unverified:broker={broker_removed}:network={network_removed}"
                )
    write_json(args.output_dir / "native_result.json", {
        "schema_version": "1.0", "case_id": args.case_id,
        "domain": args.domain, "task_id": args.task_id,
        "official_evaluation": False, "validity_gate": False,
        "infrastructure_failure": True, "score": 0,
        "errors": ["tau3_provider_or_runtime_failed_after_retries"],
        "provider_attempts": attempts, "credential_brokered": True,
        "runtime_network": "internal-only",
        "tau_source_commit": TAU_COMMIT,
        "tau_lock_digest": sha256(TAU_ROOT / "uv.lock"),
    })
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
