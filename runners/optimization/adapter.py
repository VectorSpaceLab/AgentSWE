#!/usr/bin/env python3
"""Harbor Candidate/Eval adapter for the five paper Optimization cases."""

from __future__ import annotations

import argparse
import concurrent.futures
import fcntl
import hashlib
import ipaddress
import json
import os
import re
import shutil
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve()
ADAPTER_DIR = ROOT.parent
# This copy lives one directory below the aligned campaign root, so the
# original ``ROOT.parents[1]`` calculation would point at the campaign rather
# than the Harbor installation.  Resolve the shared Harbor root explicitly
# from the copied adapter's location.
HARBOR_ROOT = Path(os.environ.get("AGENTSWE_HARBOR_ROOT", str(Path(os.environ.get("AGENTSWE_HOME", ".agentswe")) / "harbor")))
# Keep the native controller snapshot inside this campaign. In particular,
# its evaluator-owned IPAM locks must not depend on the read-only legacy
# ${AGENTSWE_HOME}/harbor/optimization-0812 directory. The directory name is also
# the Python package name used by the TerminalBench controller.
NATIVE_ROOT = ADAPTER_DIR / "optimization_native"
PYTHON_PREFIX_HOST = Path(os.environ.get("AGENTSWE_HOME", ".agentswe")) / "envs" / "optimization-python311"
PYTHON_PREFIX_CONTAINER = Path("/opt/agentswe/benchmark/envs/formal-theorem-proving-agent-v2")
CONTAINER_ENV_PREFIX = PYTHON_PREFIX_CONTAINER
CONTAINER_CREDENTIAL_FILE = Path("/run/secrets/agentswe.env")
BROWSECOMP_BROKER_CREDENTIAL_FILE = Path("/run/secrets/browsecomp.env")
BROWSECOMP_BROKER_URL = "http://browsecomp-broker:8080"
BROWSECOMP_CANDIDATE_TOKEN = "broker-only-placeholder"
BROWSECOMP_STATS_TOKEN = "stats-only-placeholder"
MAX_ROW_INFRASTRUCTURE_RETRIES = 3
COMPOSE_IPAM_BASE = ipaddress.ip_network(os.environ.get("AGENTSWE_OPTIMIZATION_POOL", "198.18.0.0/15"))
COMPOSE_IPAM_PREFIX = 28
# The campaign has its own run/artifact directory, but compose subnet
# allocation must remain daemon-wide.  Reuse the already locked registry from
# the parent aligned campaign so this campaign cannot allocate a subnet that is
# simultaneously used by another Harbor run.
COMPOSE_COORDINATION_ROOT = Path(
    os.environ.get(
        "AGENTSWE_COMPOSE_COORDINATION_ROOT",
        str(Path(os.environ.get("AGENTSWE_HOME", ".agentswe")) / "coordination" / "optimization"),
    )
)
COMPOSE_IPAM_REGISTRY = COMPOSE_COORDINATION_ROOT / "network_ipam_registry.json"
COMPOSE_IPAM_LOCK = COMPOSE_COORDINATION_ROOT / ".network_ipam_registry.lock"
COMPOSE_NETWORK_CREATE_LOCK = COMPOSE_COORDINATION_ROOT / ".network_create.lock"
_DOCKER_POOL_SUBNETS: set[str] = set()
BENCHMARK_ROOT = Path(os.environ.get("AGENTSWE_OPTIMIZATION_BENCHMARK_ROOT", str(Path(os.environ.get("AGENTSWE_HOME", ".agentswe")) / "benchmarks" / "optimization")))
# These are retained only as compatibility defaults for old smoke artifacts.
# Formal runs resolve the split from each benchmark's task_contract.json.
DEV_CASES = tuple(f"dev_{i:03d}" for i in range(1, 11))
HIDDEN_CASES = tuple(f"test_{i:03d}" for i in range(1, 26))
DEFAULT_CASES = HIDDEN_CASES
DEFAULT_ENV_PREFIX = PYTHON_PREFIX_HOST
DEFAULT_CREDENTIAL_FILE = Path(os.environ.get("AGENTSWE_HOME", ".agentswe")) / "judge.env"
DEFAULT_ROW_CACHE = ADAPTER_DIR / "row_cache"
BENCHMARKS = {
    "browsecomp-search-agent-optimization-v1": BENCHMARK_ROOT / "browsecomp-search-agent-optimization-v1",
    "terminalbench-code-agent-optimization-v1": BENCHMARK_ROOT / "terminalbench-code-agent-optimization-v1",
    "pinchbench-openclaw-agent-optimization-v1": BENCHMARK_ROOT / "pinchbench-openclaw-agent-optimization-v1",
    "osworld-desktop-agent-optimization-v1": BENCHMARK_ROOT / "osworld-desktop-agent-optimization-v1",
    "tau3-tool-agent-optimization-v1": BENCHMARK_ROOT / "tau3-tool-agent-optimization-v1",
}
BROWSECOMP_ID = "browsecomp-search-agent-optimization-v1"
OSWORLD_ID = "osworld-desktop-agent-optimization-v1"
OSWORLD_RUNTIME_PYTHON = Path(os.environ.get("AGENTSWE_OSWORLD_PYTHON", str(Path(os.environ.get("AGENTSWE_HOME", ".agentswe")) / "envs" / "osworld-venv" / "bin" / "python")))
DEFAULT_BENCHMARK = BENCHMARKS["browsecomp-search-agent-optimization-v1"]


class InfrastructureEvaluationError(RuntimeError):
    """An evaluator failure caused by infrastructure rather than the agent."""

    infrastructure_failure = True


class DeterministicInfrastructureError(InfrastructureEvaluationError):
    """A validated infrastructure defect that replay cannot repair."""

    retryable = False


_INFRASTRUCTURE_ERROR_TEXT = re.compile(
    r"(?:\b(?:402|408|429|500|502|503|504)\b|"
    r"payment required|insufficient wallet balance|wallet balance|"
    r"rate.?limit|too many requests|usage limit|quota exceeded|"
    r"api error|overloaded|connection closed|stream closed|"
    r"response stalled|could not resolve host|connection refused|"
    r"connection timed out|request timed out|network|transport|"
    r"docker|image pull|image.*(?:build|pull)|qemu|kvm|vm capacity|"
    r"infrastructure_failure|infrastructure failure)",
    re.IGNORECASE,
)


def is_infrastructure_error(value: BaseException | str) -> bool:
    """Classify adapter failures that are safe to replay.

    This deliberately accepts the explicit exception type first and otherwise
    uses the same conservative provider/container/network vocabulary emitted
    by the evaluator controllers.  Capability-level invalid contracts do not
    match this predicate and therefore fail closed.
    """

    if isinstance(value, InfrastructureEvaluationError):
        return True
    return bool(_INFRASTRUCTURE_ERROR_TEXT.search(str(value)))


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def refresh_docker_pool_subnets() -> set[str]:
    """Snapshot every Docker subnet inside the locked benchmark pool.

    The registry is coordinated by our adapter, but older/unlabelled Docker
    networks can exist outside it.  A phase-level snapshot prevents the
    allocator from selecting one of those subnets.  The create path also has
    a replacement retry for a network that appears after this snapshot.
    """
    global _DOCKER_POOL_SUBNETS
    listed = subprocess.run(
        ["docker", "network", "ls", "-q"],
        text=True, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
        check=False, timeout=60,
    )
    ids = [x.strip() for x in listed.stdout.splitlines() if x.strip()]
    if not ids:
        _DOCKER_POOL_SUBNETS = set()
        return _DOCKER_POOL_SUBNETS
    inspected = subprocess.run(
        ["docker", "network", "inspect", *ids],
        text=True, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
        check=False, timeout=120,
    )
    try:
        networks = json.loads(inspected.stdout)
    except json.JSONDecodeError:
        networks = []
    reserved: set[str] = set()
    for network in networks if isinstance(networks, list) else []:
        ipam = network.get("IPAM") if isinstance(network, dict) else None
        configs = ipam.get("Config") if isinstance(ipam, dict) else None
        for config in configs if isinstance(configs, list) else []:
            subnet = config.get("Subnet") if isinstance(config, dict) else None
            if not isinstance(subnet, str):
                continue
            try:
                parsed = ipaddress.ip_network(subnet)
            except ValueError:
                continue
            if parsed.subnet_of(COMPOSE_IPAM_BASE):
                reserved.add(str(parsed))
    _DOCKER_POOL_SUBNETS = reserved
    return reserved


def write_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, indent=2, ensure_ascii=False, sort_keys=True) + "\n", encoding="utf-8")


def read_json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def file_digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def allocate_compose_subnets(network_key: str, count: int) -> tuple[str, ...]:
    """Allocate stable, non-overlapping small bridge networks across all runs.

    Docker's daemon-wide default address pools only provide a few dozen large
    bridge networks on this host.  Five benchmarks at ten-way concurrency can
    exceed that even though each trial only needs a handful of addresses.  A
    locked registry assigns /28 networks from the benchmarking-only 198.18/15
    range so independent Harbor jobs can run concurrently without sharing a
    network or competing for the daemon default pool.
    """
    if count < 1:
        raise ValueError("compose subnet count must be positive")
    subnet_capacity = 1 << (COMPOSE_IPAM_PREFIX - COMPOSE_IPAM_BASE.prefixlen)
    block_size = 1 << (32 - COMPOSE_IPAM_PREFIX)
    COMPOSE_IPAM_LOCK.parent.mkdir(parents=True, exist_ok=True)
    with COMPOSE_IPAM_LOCK.open("a+", encoding="utf-8") as lock:
        fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
        try:
            if COMPOSE_IPAM_REGISTRY.is_file():
                registry = read_json(COMPOSE_IPAM_REGISTRY)
            else:
                registry = {
                    "schema_version": "1.0",
                    "base_cidr": str(COMPOSE_IPAM_BASE),
                    "subnet_prefix": COMPOSE_IPAM_PREFIX,
                    "assignments": {},
                }
            if (
                registry.get("base_cidr") != str(COMPOSE_IPAM_BASE)
                or registry.get("subnet_prefix") != COMPOSE_IPAM_PREFIX
                or not isinstance(registry.get("assignments"), dict)
            ):
                raise RuntimeError("compose IPAM registry contract mismatch")
            assignments = registry["assignments"]
            current = list(assignments.get(network_key, []))
            used = {
                subnet
                for values in assignments.values()
                if isinstance(values, list)
                for subnet in values
            }
            # Include daemon-owned networks that predate or bypass the
            # registry.  Registry assignments remain the source of truth for
            # our own in-flight allocations.
            used.update(_DOCKER_POOL_SUBNETS)
            for slot in range(subnet_capacity):
                if len(current) >= count:
                    break
                address = int(COMPOSE_IPAM_BASE.network_address) + slot * block_size
                subnet = str(ipaddress.ip_network((address, COMPOSE_IPAM_PREFIX)))
                if subnet not in used:
                    current.append(subnet)
                    used.add(subnet)
            if len(current) < count:
                raise RuntimeError("compose IPAM registry exhausted")
            assignments[network_key] = current
            write_json(COMPOSE_IPAM_REGISTRY, registry)
            return tuple(current[:count])
        finally:
            fcntl.flock(lock.fileno(), fcntl.LOCK_UN)


def replace_compose_subnet(network_key: str, old_subnet: str) -> str:
    """Replace one registry allocation after Docker reports an overlap."""
    with COMPOSE_IPAM_LOCK.open("a+", encoding="utf-8") as lock:
        fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
        try:
            registry = read_json(COMPOSE_IPAM_REGISTRY)
            assignments = registry.get("assignments", {})
            current = [x for x in assignments.get(network_key, []) if x != old_subnet]
            used = {
                subnet
                for values in assignments.values()
                if isinstance(values, list)
                for subnet in values
                if subnet != old_subnet
            }
            used.update(_DOCKER_POOL_SUBNETS)
            block_size = 1 << (32 - COMPOSE_IPAM_PREFIX)
            subnet_capacity = 1 << (COMPOSE_IPAM_PREFIX - COMPOSE_IPAM_BASE.prefixlen)
            for slot in range(subnet_capacity):
                address = int(COMPOSE_IPAM_BASE.network_address) + slot * block_size
                candidate = str(ipaddress.ip_network((address, COMPOSE_IPAM_PREFIX)))
                if candidate not in used:
                    current.append(candidate)
                    assignments[network_key] = current
                    write_json(COMPOSE_IPAM_REGISTRY, registry)
                    return candidate
            raise RuntimeError("compose IPAM registry exhausted while replacing overlap")
        finally:
            fcntl.flock(lock.fileno(), fcntl.LOCK_UN)


def release_compose_subnets(rows: list[dict[str, Any]]) -> dict[str, Any]:
    """Release allocations whose Docker networks were confirmed removed.

    Allocation records are reservations for live phases, not permanent
    history.  The old adapter only removed the Docker network and left the
    reservation in the daemon-wide registry, so enough completed phases could
    make the registry report exhaustion even though the address space was
    free.  This function is deliberately driven by cleanup evidence: callers
    pass only rows whose network was observed absent after ``docker network
    rm`` (or was already absent), and all registry mutation remains protected
    by the shared lock.
    """
    requested: dict[str, set[str]] = {}
    for row in rows:
        if not isinstance(row, dict):
            continue
        network_key = row.get("network_key")
        subnet = row.get("subnet")
        if isinstance(network_key, str) and isinstance(subnet, str):
            requested.setdefault(network_key, set()).add(subnet)
    if not requested:
        return {"requested_keys": 0, "requested_subnets": 0, "released_subnets": 0}

    with COMPOSE_IPAM_LOCK.open("a+", encoding="utf-8") as lock:
        fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
        try:
            if not COMPOSE_IPAM_REGISTRY.is_file():
                return {
                    "requested_keys": len(requested),
                    "requested_subnets": sum(len(v) for v in requested.values()),
                    "released_subnets": 0,
                    "registry_missing": True,
                }
            registry = read_json(COMPOSE_IPAM_REGISTRY)
            assignments = registry.get("assignments")
            if not isinstance(assignments, dict):
                raise RuntimeError("compose IPAM registry contract mismatch")
            released = 0
            for network_key, subnets in requested.items():
                values = assignments.get(network_key)
                if not isinstance(values, list):
                    continue
                retained = [value for value in values if value not in subnets]
                released += len(values) - len(retained)
                if retained:
                    assignments[network_key] = retained
                else:
                    assignments.pop(network_key, None)
            if released:
                write_json(COMPOSE_IPAM_REGISTRY, registry)
            return {
                "requested_keys": len(requested),
                "requested_subnets": sum(len(v) for v in requested.values()),
                "released_subnets": released,
            }
        finally:
            fcntl.flock(lock.fileno(), fcntl.LOCK_UN)


def compose_ipam_network(subnet: str) -> dict[str, Any]:
    parsed = ipaddress.ip_network(subnet)
    if parsed.prefixlen != COMPOSE_IPAM_PREFIX or not parsed.subnet_of(COMPOSE_IPAM_BASE):
        raise ValueError(f"compose subnet outside locked pool: {subnet}")
    return {"ipam": {"config": [{"subnet": str(parsed)}]}}


def provision_compose_network(
    *, network_key: str, ordinal: int, subnet: str, internal: bool = False
) -> tuple[dict[str, Any], dict[str, Any]]:
    """Create one owned bridge network before Harbor starts the trial.

    Docker can finish a network create after the CLI request times out. Treat
    that as a recoverable delayed acknowledgement: poll inspect, validate the
    eventual network contract, and only retry create when the network truly
    did not appear.

    Subnets are allocated under the shared IPAM registry lock, and each task
    receives a unique network name and subnet. Therefore Docker API calls do
    not need a campaign-wide lock. Holding one while waiting for Docker makes
    otherwise independent 10-way case workers queue behind a single slow
    create/inspect operation.
    """
    suffix = hashlib.sha256(network_key.encode("utf-8")).hexdigest()[:20]
    name = f"agentswe-opt-{suffix}-{ordinal}"

    def inspect_network(attempts: int, delay_seconds: float) -> dict[str, Any] | None:
        for attempt in range(1, attempts + 1):
            try:
                inspected = subprocess.run(
                    ["docker", "network", "inspect", name],
                    text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                    check=False, timeout=30,
                )
            except subprocess.TimeoutExpired:
                inspected = None
            if inspected is not None and inspected.returncode == 0:
                value = read_json_from_output(inspected.stdout)
                if isinstance(value, list) and value:
                    return value[0]
            if attempt < attempts:
                time.sleep(delay_seconds)
        return None

    details = inspect_network(1, 0.0)
    created = details is None
    create_attempts = 0
    create_timeouts = 0
    late_create_recovered = False
    last_error = ""
    if details is None:
        for create_attempt in range(1, 4):
            create_attempts = create_attempt
            timed_out = False
            command = [
                "docker", "network", "create",
                "--driver", "bridge",
                "--label", "agentswe.optimization.network=1",
                "--label", f"agentswe.optimization.key={suffix}",
                "--subnet", subnet,
            ]
            if internal:
                command.append("--internal")
            command.append(name)
            try:
                completed = subprocess.run(
                    command, text=True, stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE, check=False, timeout=120,
                )
                if completed.returncode != 0:
                    last_error = completed.stderr[-1000:]
            except subprocess.TimeoutExpired as exc:
                timed_out = True
                create_timeouts += 1
                last_error = str(exc)
            details = inspect_network(18, 5.0)
            if details is not None:
                late_create_recovered = timed_out
                break
            if "overlap" in last_error.lower() and create_attempt < 3:
                refresh_docker_pool_subnets()
                subnet = replace_compose_subnet(network_key, subnet)
            if details is None and create_attempt < 3:
                continue
        if details is None:
            raise RuntimeError(
                f"failed to provision compose network {name} after "
                f"{create_attempts} attempts: {last_error}"
            )
    compose_ipam_network(subnet)
    actual_subnets = {
        row.get("Subnet") for row in details.get("IPAM", {}).get("Config", [])
    }
    if subnet not in actual_subnets or bool(details.get("Internal")) != internal:
        raise RuntimeError(f"compose network contract mismatch: {name}")
    evidence = {
        "name": name,
        "subnet": subnet,
        "internal": internal,
        "created": created,
        "create_attempts": create_attempts,
        "create_timeouts": create_timeouts,
        "late_create_recovered": late_create_recovered,
        "network_key": network_key,
    }
    return {"external": True, "name": name}, evidence


def read_json_from_output(source: str) -> Any:
    try:
        return json.loads(source)
    except json.JSONDecodeError as exc:
        raise RuntimeError("docker returned malformed JSON") from exc


def tree_digest(root: Path) -> str:
    root = root.resolve()
    h = hashlib.sha256()
    for path in sorted(root.rglob("*"), key=lambda p: p.relative_to(root).as_posix()):
        rel = path.relative_to(root).as_posix().encode()
        if path.is_symlink():
            h.update(b"L" + rel + os.readlink(path).encode())
        elif path.is_file():
            h.update(b"F" + rel + path.read_bytes())
    return h.hexdigest()


def benchmark_contract(benchmark: Path) -> dict[str, Any]:
    """Load the evaluator-owned Track-A contract for a benchmark package."""
    path = benchmark / "task_contract.json"
    if not path.is_file():
        raise FileNotFoundError(f"task contract missing: {path}")
    value = read_json(path)
    if not isinstance(value, dict) or value.get("schema_version") != "3.0":
        raise RuntimeError(f"unsupported task contract: {path}")
    splits = value.get("splits")
    if not isinstance(splits, dict):
        raise RuntimeError(f"task contract has no splits: {path}")
    for role in ("dev", "hidden"):
        rows = splits.get(role, {}).get("cases") if isinstance(splits.get(role), dict) else None
        if not isinstance(rows, list) or not rows:
            raise RuntimeError(f"task contract {role} split is empty: {path}")
        ids = [row.get("case_id") for row in rows if isinstance(row, dict)]
        if len(ids) != len(rows) or len(set(ids)) != len(ids):
            raise RuntimeError(f"task contract {role} contains duplicate/malformed case IDs")
    return value


def split_cases(benchmark: Path, role: str) -> tuple[str, ...]:
    contract = benchmark_contract(benchmark)
    rows = contract["splits"][role]["cases"]
    return tuple(str(row["case_id"]) for row in rows)


def contract_case_metadata(benchmark: Path, case_id: str) -> dict[str, Any]:
    contract = benchmark_contract(benchmark)
    for role in ("dev", "hidden"):
        for row in contract["splits"][role]["cases"]:
            if row.get("case_id") == case_id:
                return {"role": role, **row}
    raise KeyError(f"case {case_id} is not in task contract")


def source_digests() -> dict[str, str]:
    paths = [ROOT]
    for directory in (ADAPTER_DIR / "task-template", ADAPTER_DIR / "eval-template", ADAPTER_DIR / "builder-template"):
        paths.extend(p for p in directory.rglob("*") if p.is_file() and "__pycache__" not in p.parts)
    paths.extend(
        p for p in NATIVE_ROOT.rglob("*")
        if p.is_file() and "__pycache__" not in p.parts
    )
    values: dict[str, str] = {}
    for path in sorted(set(paths)):
        try:
            key = path.relative_to(ADAPTER_DIR).as_posix()
        except ValueError:
            key = "../" + path.relative_to(HARBOR_ROOT).as_posix()
        values[key] = file_digest(path)
    return values


def protocol_digest() -> str:
    return hashlib.sha256(
        json.dumps(source_digests(), sort_keys=True).encode("utf-8")
    ).hexdigest()


def row_cache_key(
    *, candidate_digest: str, case_id: str, benchmark_digest: str,
    protocol_source_digest: str,
) -> str:
    return hashlib.sha256(
        json.dumps(
            {
                "candidate_digest": candidate_digest,
                "case_id": case_id,
                "benchmark_digest": benchmark_digest,
                "protocol_source_digest": protocol_source_digest,
            },
            sort_keys=True,
        ).encode("utf-8")
    ).hexdigest()


def load_cached_row(
    *, cache_dir: Path, candidate_digest: str, case_id: str,
    benchmark_digest: str, protocol_source_digest: str, run_dir: Path,
) -> dict[str, Any] | None:
    key = row_cache_key(
        candidate_digest=candidate_digest,
        case_id=case_id,
        benchmark_digest=benchmark_digest,
        protocol_source_digest=protocol_source_digest,
    )
    path = cache_dir / key[:2] / f"{key}.json"
    if not path.is_file():
        return None
    try:
        value = read_json(path)
    except (OSError, json.JSONDecodeError):
        return None
    expected = {
        "cache_key": key,
        "candidate_digest": candidate_digest,
        "case_id": case_id,
        "benchmark_digest": benchmark_digest,
        "protocol_source_digest": protocol_source_digest,
    }
    if not isinstance(value, dict) or any(value.get(k) != v for k, v in expected.items()):
        return None
    case = value.get("case")
    evaluation = value.get("eval_result")
    if not isinstance(case, dict) or not isinstance(evaluation, dict):
        return None
    if not (
        case.get("contract_valid") is True
        and case.get("validity_gate") is True
        and case.get("official_evaluation") is True
        and case.get("infrastructure_failure") is False
    ):
        return None
    materialized = run_dir / "row_cache_hits" / case_id
    write_json(materialized / "eval_result.json", evaluation)
    contract = dict(case)
    contract.update({"schema_version": "3.0", "cache_key": key, "cache_hit": True})
    write_json(materialized / "score_contract.json", contract)
    result = dict(case)
    result.update({
        "trial": "row-cache",
        "contract": str(materialized / "score_contract.json"),
        "eval_result": str(materialized / "eval_result.json"),
        "cache_hit": True,
        "cache_key": key,
    })
    return result


def store_cached_row(
    *, cache_dir: Path, candidate_digest: str, case: dict[str, Any],
    benchmark_digest: str, protocol_source_digest: str,
) -> str | None:
    if not (
        case.get("contract_valid") is True
        and case.get("validity_gate") is True
        and case.get("official_evaluation") is True
        and case.get("infrastructure_failure") is False
    ):
        return None
    case_id = str(case["case_id"])
    eval_path = Path(str(case.get("eval_result", "")))
    if not eval_path.is_file():
        return None
    evaluation = read_json(eval_path)
    if not isinstance(evaluation, dict):
        return None
    key = row_cache_key(
        candidate_digest=candidate_digest,
        case_id=case_id,
        benchmark_digest=benchmark_digest,
        protocol_source_digest=protocol_source_digest,
    )
    stable_case = {
        name: case.get(name)
        for name in (
            "case_id", "score", "contract_valid", "validity_gate",
            "official_evaluation", "infrastructure_failure", "native_task_available",
        )
    }
    path = cache_dir / key[:2] / f"{key}.json"
    write_json(path, {
        "schema_version": "1.0",
        "cache_key": key,
        "candidate_digest": candidate_digest,
        "case_id": case_id,
        "benchmark_digest": benchmark_digest,
        "protocol_source_digest": protocol_source_digest,
        "created_at": utc_now(),
        "case": stable_case,
        "eval_result": evaluation,
    })
    return key


def harbor_env() -> dict[str, str]:
    env = os.environ.copy()
    # The migrated Harbor installation is intentionally read-only at its
    # top-level root for the evaluation user.  Docker only needs a writable
    # client configuration directory, so keep that run-local instead of
    # attempting to create HARBOR_ROOT/docker-config.
    overlay = env.get("OPTIMIZATION_HARBOR_OVERLAY", "")
    overlay_enabled = env.get("OPTIMIZATION_INFRA_RESUME", "").lower() in {
        "1", "true", "yes"
    }
    pythonpath_values = [
        overlay if overlay_enabled and overlay else "",
        str(ADAPTER_DIR),
        str(HARBOR_ROOT),
        env.get("PYTHONPATH", ""),
    ]
    pythonpath = os.pathsep.join(value for value in pythonpath_values if value)
    env.update({"HARBOR_ROOT": str(HARBOR_ROOT), "UV_TOOL_DIR": str(HARBOR_ROOT / "uv-tools"), "UV_TOOL_BIN_DIR": str(HARBOR_ROOT / "bin"), "HARBOR_TELEMETRY": "off", "DOCKER_CONFIG": os.environ.get("AGENTSWE_DOCKER_CONFIG", str(HARBOR_ROOT / "docker-config")), "PYTHONPATH": pythonpath, "PATH": str(HARBOR_ROOT / "bin") + os.pathsep + env.get("PATH", "")})
    return env


def harbor_environment() -> dict[str, str]:
    return harbor_env()


def run_capture(command: list[str]) -> str:
    completed = subprocess.run(command, text=True, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, check=False, env=harbor_env())
    if completed.returncode != 0:
        raise RuntimeError(f"command failed ({completed.returncode}): {command!r}\n{completed.stdout}")
    return completed.stdout.strip()


def run_harbor(config: Path, run_dir: Path) -> dict[str, Any]:
    run_dir.mkdir(parents=True, exist_ok=True)
    out = run_dir / "harbor.stdout.log"
    err = run_dir / "harbor.stderr.log"
    command = [str(HARBOR_ROOT / "bin/harbor"), "run", "--config", str(config)]
    with out.open("w") as stdout, err.open("w") as stderr:
        p = subprocess.run(command, stdout=stdout, stderr=stderr, text=True, env=harbor_env(), check=False)
    return {"exit_code": p.returncode, "stdout": str(out), "stderr": str(err)}


def execute_harbor(config_path: Path, run_dir: Path) -> dict[str, Any]:
    """Run a Harbor Job and persist process-level evidence.

    The one-stop Builder controller uses this function for the Builder Job,
    while ``run_harbor`` remains the small helper used by the lower-level
    Candidate/Eval adapter.  Keeping one implementation here makes the
    Builder and frozen-candidate phases use the same Harbor environment and
    gives both phases an auditable process record.
    """
    command = [str(HARBOR_ROOT / "bin" / "harbor"), "run", "--config", str(config_path)]
    run_dir.mkdir(parents=True, exist_ok=True)
    stdout_path = run_dir / "harbor.stdout.log"
    stderr_path = run_dir / "harbor.stderr.log"
    started_at = utc_now()
    started = time.monotonic()
    with stdout_path.open("w", encoding="utf-8") as stdout, stderr_path.open(
        "w", encoding="utf-8"
    ) as stderr:
        completed = subprocess.run(
            command,
            cwd=HARBOR_ROOT,
            env=harbor_environment(),
            text=True,
            stdout=stdout,
            stderr=stderr,
            check=False,
        )
    process = {
        "command": command,
        "started_at": started_at,
        "finished_at": utc_now(),
        "runtime_seconds": round(time.monotonic() - started, 3),
        "exit_code": completed.returncode,
        "stdout": str(stdout_path),
        "stderr": str(stderr_path),
    }
    write_json(run_dir / "harbor_process.json", process)
    return process


def materialize_registry(task_dir: Path) -> None:
    """Point the run-local template copy's docker.io base images at AGENTSWE_DOCKER_REGISTRY when set.

    The templates pin docker.io/library/ubuntu by digest (the paper templates named a Docker Hub mirror).
    On a host that reaches Docker Hub only through a mirror, BuildKit's metadata lookup for a docker.io
    reference stalls past Harbor's environment start timeout even when the image is already local. The
    digest pins the bytes, so the mirror serves the identical image. Only the copy under the run
    directory changes; the digested templates stay as they are.
    """
    registry = os.environ.get("AGENTSWE_DOCKER_REGISTRY", "").strip().rstrip("/")
    if not registry or registry == "docker.io":
        return
    for dockerfile in task_dir.rglob("Dockerfile"):
        text = dockerfile.read_text(encoding="utf-8")
        new = re.sub(r"(?m)^(FROM\s+)docker\.io/", lambda m: m.group(1) + registry + "/", text)
        if new != text:
            dockerfile.write_text(new, encoding="utf-8")


def task_toml(kind: str) -> str:
    source = (ADAPTER_DIR / "task-template/task.toml").read_text()
    source = source.replace("local/optimization-candidate", f"local/optimization-{kind}")
    if kind == BROWSECOMP_ID:
        source = source.replace(
            'schema_version = "1.4"',
            'schema_version = "1.4"\nartifacts = [{ source = "/broker-evidence/resource_evidence.json", destination = "browsecomp_broker/resource_evidence.json", service = "browsecomp-broker" }]',
        )
        source += f'''\n[[verifier.collect]]
command = "{PYTHON_PREFIX_CONTAINER}/bin/python /broker.py --finalize-output /broker-evidence/resource_evidence.json --port 8080"
service = "browsecomp-broker"
timeout_sec = 320.0
user = "root"
'''
    return source


def eval_task_toml(kind: str) -> str:
    source = (ADAPTER_DIR / "eval-template/task.toml").read_text()
    return source.replace("local/optimization-eval", f"local/optimization-eval-{kind}")


def compose_candidate(candidate: Path, active_case: Path, case_id: str, candidate_digest: str, credential_file: Path | None = None, network_subnet: str | None = None, external_network: dict[str, Any] | None = None) -> dict[str, Any]:
    environment = {"CASE_ID": case_id, "CANDIDATE_DIGEST": candidate_digest, "OPTIMIZATION_PYTHON": str(PYTHON_PREFIX_CONTAINER / "bin/python")}
    volumes = [
        {"type": "bind", "source": str(candidate.resolve()), "target": "/submission", "read_only": True},
        {"type": "bind", "source": str(active_case.resolve()), "target": f"/active-case/{active_case.name}", "read_only": True},
        {"type": "bind", "source": str(PYTHON_PREFIX_HOST.resolve()), "target": str(PYTHON_PREFIX_CONTAINER), "read_only": True},
    ]
    if credential_file is not None:
        environment["HARNESS_CREDENTIAL_FILE"] = str(CONTAINER_CREDENTIAL_FILE)
        volumes.append({"type": "bind", "source": str(credential_file.resolve()), "target": str(CONTAINER_CREDENTIAL_FILE), "read_only": True})
    compose = {"services": {"main": {"environment": environment, "volumes": volumes}}}
    if external_network is not None:
        compose["networks"] = {"default": external_network}
    elif network_subnet is not None:
        compose["networks"] = {"default": compose_ipam_network(network_subnet)}
    return compose


def compose_browsecomp_candidate(
    candidate: Path,
    active_case: Path,
    case_id: str,
    candidate_digest: str,
    credential_file: Path,
    network_subnets: tuple[str, str] | None = None,
    external_networks: tuple[dict[str, Any], dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """Expose only broker placeholders; the real dotenv stays in the sidecar."""
    # The model name shown to the Candidate is the configured RUNTIME model (non-secret, written by
    # the runner into the credential file); the sidecar locks the same model on every call.
    runtime_model = os.environ.get("AGENTSWE_RUNTIME_MODEL", "")
    for raw in credential_file.read_text(encoding="utf-8").splitlines():
        key, sep, value = raw.strip().partition("=")
        if sep and key.strip() == "AGENTSWE_RUNTIME_MODEL" and value.strip():
            runtime_model = value.strip().strip("'\"")
    runtime_model = runtime_model or "deepseek-flash"
    environment = {
        "CASE_ID": case_id,
        "CANDIDATE_DIGEST": candidate_digest,
        "OPTIMIZATION_PYTHON": str(PYTHON_PREFIX_CONTAINER / "bin/python"),
        "HARNESS_RESOURCE_MODE": "brokered-v1",
        "HARNESS_RESPONSES_URL": f"{BROWSECOMP_BROKER_URL}/v1/responses",
        "GATEWAY_RESPONSES_URL": f"{BROWSECOMP_BROKER_URL}/v1/responses",
        "OPENAI_BASE_URL": f"{BROWSECOMP_BROKER_URL}/v1",
        "HARNESS_SEARCH_URL": f"{BROWSECOMP_BROKER_URL}/serp_search_v1",
        "SERPER_URL": f"{BROWSECOMP_BROKER_URL}/serp_search_v1",
        "HARNESS_VISIT_URL": f"{BROWSECOMP_BROKER_URL}/visit",
        "GATEWAY_API_KEY": BROWSECOMP_CANDIDATE_TOKEN,
        "OPENAI_API_KEY": BROWSECOMP_CANDIDATE_TOKEN,
        "SERPER_TOKEN": BROWSECOMP_CANDIDATE_TOKEN,
        "HARNESS_API_KEY": BROWSECOMP_CANDIDATE_TOKEN,
        "HARNESS_MODEL": runtime_model,
        "GATEWAY_MODEL": runtime_model,
    }
    volumes = [
        {"type": "bind", "source": str(candidate.resolve()), "target": "/submission", "read_only": True},
        {"type": "bind", "source": str(active_case.resolve()), "target": f"/active-case/{active_case.name}", "read_only": True},
        {"type": "bind", "source": str(PYTHON_PREFIX_HOST.resolve()), "target": str(PYTHON_PREFIX_CONTAINER), "read_only": True},
    ]
    broker = {
        "image": "docker.io/library/ubuntu@sha256:561618e2c15bf2397621dd04f96926663a3b5616c189cf7e38db7e82f5c538ea",
        "pull_policy": "never",
        "command": [
            str(PYTHON_PREFIX_CONTAINER / "bin/python"), "/broker.py", "--credential-file", str(BROWSECOMP_BROKER_CREDENTIAL_FILE),
            "--bind", "0.0.0.0", "--port", "8080",
        ],
        # The shared Python env is mounted at the paper path, not the prefix it was built for, so its
        # compiled-in OpenSSL CA path does not exist in the container; name its CA bundle explicitly.
        "environment": {"CASE_ID": case_id, "SSL_CERT_FILE": str(PYTHON_PREFIX_CONTAINER / "ssl" / "cacert.pem")},
        "volumes": [
            {"type": "bind", "source": str((NATIVE_ROOT / "browsecomp_broker.py").resolve()), "target": "/broker.py", "read_only": True},
            {"type": "bind", "source": str(credential_file.resolve()), "target": str(BROWSECOMP_BROKER_CREDENTIAL_FILE), "read_only": True},
            {"type": "bind", "source": str(PYTHON_PREFIX_HOST.resolve()), "target": str(PYTHON_PREFIX_CONTAINER), "read_only": True},
        ],
        "healthcheck": {
            "test": ["CMD", str(PYTHON_PREFIX_CONTAINER / "bin/python"), "-c", "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8080/healthz', timeout=2).read()"],
            "interval": "1s", "timeout": "3s", "retries": 30,
        },
        "networks": ["broker-public", "candidate-private"],
    }
    networks: dict[str, Any] = {
        "candidate-private": {"internal": True},
        "broker-public": {},
    }
    if external_networks is not None:
        networks["candidate-private"] = external_networks[0]
        networks["broker-public"] = external_networks[1]
    elif network_subnets is not None:
        networks["candidate-private"].update(compose_ipam_network(network_subnets[0]))
        networks["broker-public"].update(compose_ipam_network(network_subnets[1]))
    return {
        "services": {
            "main": {
                "environment": environment,
                "volumes": volumes,
                "depends_on": {"browsecomp-broker": {"condition": "service_healthy"}},
                "networks": ["candidate-private"],
            },
            "browsecomp-broker": broker,
        },
        "networks": networks,
    }


def compose_candidate_verifier(active_case: Path, case_id: str, candidate_digest: str, network_subnet: str | None = None, external_network: dict[str, Any] | None = None) -> dict[str, Any]:
    # The separate verifier only sees active case and Harbor artifact mounts.
    compose = {"services": {"main": {"environment": {"CASE_ID": case_id, "CANDIDATE_DIGEST": candidate_digest, "OPTIMIZATION_PYTHON": str(PYTHON_PREFIX_CONTAINER / "bin/python")}, "volumes": [{"type": "bind", "source": str(active_case.resolve()), "target": f"/active-case/{active_case.name}", "read_only": True}, {"type": "bind", "source": str(PYTHON_PREFIX_HOST.resolve()), "target": str(PYTHON_PREFIX_CONTAINER), "read_only": True}]}}}
    if external_network is not None:
        compose["networks"] = {"default": external_network}
    elif network_subnet is not None:
        compose["networks"] = {"default": compose_ipam_network(network_subnet)}
    return compose


def compose_eval(candidate_output: Path, case_dir: Path, gold: Path | None, benchmark_id: str, case_id: str, credential_file: Path | None = None, native_task: Path | None = None, native_result: Path | None = None, network_subnet: str | None = None, external_network: dict[str, Any] | None = None) -> dict[str, Any]:
    environment = {"BENCHMARK_ID": benchmark_id, "CASE_ID": case_id, "OPTIMIZATION_PYTHON": str(PYTHON_PREFIX_CONTAINER / "bin/python"),
                   # HTTPS from the eval container (the BrowseComp grader): see compose_browsecomp_candidate.
                   "SSL_CERT_FILE": str(PYTHON_PREFIX_CONTAINER / "ssl" / "cacert.pem")}
    volumes = [
        {"type": "bind", "source": str(candidate_output.resolve()), "target": "/candidate-output", "read_only": True},
        {"type": "bind", "source": str(case_dir.resolve()), "target": f"/active-case/{case_dir.name}", "read_only": True},
        {"type": "bind", "source": str(PYTHON_PREFIX_HOST.resolve()), "target": str(PYTHON_PREFIX_CONTAINER), "read_only": True},
    ]
    if gold is not None:
        volumes.append({"type": "bind", "source": str(gold.resolve()), "target": "/evaluator/gold.json", "read_only": True})
    if native_task is not None and native_task.is_dir():
        volumes.append({"type": "bind", "source": str(native_task.resolve()), "target": "/evaluator/native-task", "read_only": True})
    if native_result is not None and native_result.is_file():
        volumes.append({"type": "bind", "source": str(native_result.resolve()), "target": "/evaluator/native-result.json", "read_only": True})
    if credential_file is not None:
        environment["HARNESS_CREDENTIAL_FILE"] = str(CONTAINER_CREDENTIAL_FILE)
        volumes.append({"type": "bind", "source": str(credential_file.resolve()), "target": str(CONTAINER_CREDENTIAL_FILE), "read_only": True})
    compose = {"services": {"main": {"environment": environment, "volumes": volumes}}}
    if external_network is not None:
        compose["networks"] = {"default": external_network}
    elif network_subnet is not None:
        compose["networks"] = {"default": compose_ipam_network(network_subnet)}
    return compose


def compose_eval_verifier(network_subnet: str | None = None, external_network: dict[str, Any] | None = None) -> dict[str, Any]:
    compose = {"services": {"main": {"environment": {"OPTIMIZATION_PYTHON": str(PYTHON_PREFIX_CONTAINER / "bin/python")}, "volumes": [{"type": "bind", "source": str(PYTHON_PREFIX_HOST.resolve()), "target": str(PYTHON_PREFIX_CONTAINER), "read_only": True}]}}}
    if external_network is not None:
        compose["networks"] = {"default": external_network}
    elif network_subnet is not None:
        compose["networks"] = {"default": compose_ipam_network(network_subnet)}
    return compose


def make_request(case_dir: Path, case_id: str, destination: Path) -> Path:
    destination.mkdir(parents=True, exist_ok=True)
    shutil.copy2(case_dir / "input.md", destination / "input.md")
    text = (case_dir / "input.md").read_text(encoding="utf-8")
    (destination / "input.jsonl").write_text(json.dumps({"id": case_id, "source": "agentswe-optimization", "case_id": case_id, "instruction": text}, ensure_ascii=False) + "\n", encoding="utf-8")
    if (case_dir / "assets").is_dir():
        shutil.copytree(case_dir / "assets", destination / "assets")
    return destination


def native_task_id(benchmark: Path, case_id: str) -> str | None:
    """Resolve the frozen upstream task name without exposing metadata to Builder."""
    try:
        upstream = contract_case_metadata(benchmark, case_id).get("upstream_id")
        if upstream is not None:
            return str(upstream)
    except (FileNotFoundError, RuntimeError, KeyError):
        # Compatibility for old generated smoke packages.
        pass
    input_path = benchmark / ("test_cases" if case_id.startswith("test_") else "dev_cases") / case_id / "input.md"
    if not input_path.is_file():
        return None
    text = input_path.read_text(encoding="utf-8", errors="replace")
    match = re.search(r"(?:Optimization Task|Airline Task) `([^`]+)`", text)
    return match.group(1) if match else None


def stage_candidate_task(*, benchmark_id: str, benchmark: Path, candidate: Path, case_id: str, task_root: Path, candidate_digest: str, credential_file: Path | None = None) -> Path:
    task_dir = task_root / case_id
    if task_dir.exists():
        shutil.rmtree(task_dir)
    shutil.copytree(ADAPTER_DIR / "task-template", task_dir)
    materialize_registry(task_dir)
    active = task_dir / "input" / "active-case" / case_id
    case_root = "test_cases" if case_id.startswith("test_") else "dev_cases"
    make_request(benchmark / case_root / case_id, case_id, active)
    (task_dir / "task.toml").write_text(task_toml(benchmark_id), encoding="utf-8")
    (task_dir / "instruction.md").write_text(f"Run `/solution/solve.sh` once for optimization case `{case_id}`. The submission is frozen and the evaluator owns the result.\n", encoding="utf-8")
    write_json(task_dir / "solution" / "case_manifest.json", {"benchmark_id": benchmark_id, "case_id": case_id, "candidate_digest": candidate_digest})
    (task_dir / "environment" ).mkdir(exist_ok=True)
    subnet_count = 3 if benchmark_id == BROWSECOMP_ID else 2
    network_key = f"candidate:{task_dir.resolve()}"
    subnets = allocate_compose_subnets(network_key, subnet_count)
    network_rows: list[dict[str, Any]] = []
    if benchmark_id == BROWSECOMP_ID:
        if credential_file is None:
            raise RuntimeError("BrowseComp candidate requires the authorized broker credential file")
        private, private_row = provision_compose_network(
            network_key=network_key, ordinal=0, subnet=subnets[0], internal=True
        )
        public, public_row = provision_compose_network(
            network_key=network_key, ordinal=1, subnet=subnets[1]
        )
        verifier, verifier_row = provision_compose_network(
            network_key=network_key, ordinal=2, subnet=subnets[2]
        )
        network_rows.extend((private_row, public_row, verifier_row))
        compose = compose_browsecomp_candidate(
            candidate, active, case_id, candidate_digest, credential_file,
            external_networks=(private, public),
        )
        verifier_network = verifier
    else:
        environment, environment_row = provision_compose_network(
            network_key=network_key, ordinal=0, subnet=subnets[0]
        )
        verifier, verifier_row = provision_compose_network(
            network_key=network_key, ordinal=1, subnet=subnets[1]
        )
        network_rows.extend((environment_row, verifier_row))
        compose = compose_candidate(
            candidate, active, case_id, candidate_digest, credential_file,
            external_network=environment,
        )
        verifier_network = verifier
    write_json(task_dir / "environment/docker-compose.yaml", compose)
    write_json(
        task_dir / "tests/docker-compose.yaml",
        compose_candidate_verifier(
            active, case_id, candidate_digest, external_network=verifier_network
        ),
    )
    write_json(task_dir / "compose_networks.json", {"networks": network_rows})
    return task_dir


def stage_eval_task(*, benchmark_id: str, benchmark: Path, case_id: str, candidate_output: Path, task_root: Path, candidate_digest: str, credential_file: Path | None = None, native_result: Path | None = None) -> Path:
    task_dir = task_root / case_id
    if task_dir.exists():
        shutil.rmtree(task_dir)
    shutil.copytree(ADAPTER_DIR / "eval-template", task_dir)
    materialize_registry(task_dir)
    case_dir = benchmark / "test_cases" / case_id if case_id.startswith("test_") else benchmark / "dev_cases" / case_id
    staged_case = task_dir / "input" / "active-case" / case_id
    make_request(case_dir, case_id, staged_case)
    staged_output = task_dir / "input" / "candidate-output"
    shutil.copytree(candidate_output, staged_output)
    (task_dir / "task.toml").write_text(eval_task_toml(benchmark_id), encoding="utf-8")
    (task_dir / "instruction.md").write_text(f"Run `/solution/solve.sh` once for independent Eval case `{case_id}`. The Candidate source is unavailable.\n", encoding="utf-8")
    write_json(task_dir / "solution/eval_manifest.json", {"benchmark_id": benchmark_id, "case_id": case_id, "candidate_digest": candidate_digest})
    gold = benchmark / "evaluator/gold.json"
    (task_dir / "environment").mkdir(exist_ok=True)
    upstream_id = native_task_id(benchmark, case_id)
    native_task = benchmark / "evaluator" / "native_tasks" / upstream_id if upstream_id else None
    network_key = f"eval:{task_dir.resolve()}"
    subnets = allocate_compose_subnets(network_key, 2)
    environment, environment_row = provision_compose_network(
        network_key=network_key, ordinal=0, subnet=subnets[0]
    )
    verifier, verifier_row = provision_compose_network(
        network_key=network_key, ordinal=1, subnet=subnets[1]
    )
    write_json(task_dir / "environment/docker-compose.yaml", compose_eval(staged_output, staged_case, gold if gold.is_file() else None, benchmark_id, case_id, credential_file, native_task, native_result, external_network=environment))
    write_json(task_dir / "tests/docker-compose.yaml", compose_eval_verifier(external_network=verifier))
    write_json(task_dir / "compose_networks.json", {"networks": [environment_row, verifier_row]})
    return task_dir


def stage_job(*, benchmark_id: str, benchmark: Path, candidate: Path, cases: tuple[str, ...], run_dir: Path, jobs_dir: Path, eval_phase: bool = False, candidate_job: Path | None = None, credential_file: Path | None = None, native_results: dict[str, Path] | None = None, n_concurrent: int = 1) -> tuple[Path, Path]:
    refresh_docker_pool_subnets()
    candidate_digest = tree_digest(candidate)
    root_name = "eval_tasks" if eval_phase else "tasks"
    tasks_root = run_dir / root_name
    task_dirs = []
    for case_id in cases:
        if eval_phase:
            assert candidate_job is not None
            trial = next(p for p in candidate_job.iterdir() if p.is_dir() and p.name.startswith(case_id + "__"))
            output = trial / "artifacts/logs/artifacts/candidate_output"
            task_dirs.append(stage_eval_task(benchmark_id=benchmark_id, benchmark=benchmark, case_id=case_id, candidate_output=output, task_root=tasks_root, candidate_digest=candidate_digest, credential_file=credential_file, native_result=(native_results or {}).get(case_id)))
        else:
            task_dirs.append(stage_candidate_task(benchmark_id=benchmark_id, benchmark=benchmark, candidate=candidate, case_id=case_id, task_root=tasks_root, candidate_digest=candidate_digest, credential_file=credential_file))
    job_name = f"optimization-{('eval-' if eval_phase else '')}{benchmark_id}-{run_dir.name}"
    network_rows = [
        row
        for task_dir in task_dirs
        if (task_dir / "compose_networks.json").is_file()
        for row in read_json(task_dir / "compose_networks.json").get("networks", [])
    ]
    write_json(
        run_dir / ("eval_compose_networks.json" if eval_phase else "candidate_compose_networks.json"),
        {"schema_version": "1.0", "networks": network_rows},
    )
    config = {"job_name": job_name, "jobs_dir": str(jobs_dir.resolve()), "n_attempts": 1, "n_concurrent_trials": min(n_concurrent, len(task_dirs)), "quiet": True, "retry": {"max_retries": 0}, "environment": {"type": "docker", "delete": True, "force_build": False}, "agents": [{"name": "oracle"}], "tasks": [{"path": str(p.resolve())} for p in task_dirs]}
    path = run_dir / ("eval_job_config.json" if eval_phase else "job_config.json")
    write_json(path, config)
    return path, jobs_dir / job_name


def cleanup_job_networks(run_dir: Path, *, eval_phase: bool) -> dict[str, Any]:
    manifest = run_dir / (
        "eval_compose_networks.json" if eval_phase else "candidate_compose_networks.json"
    )
    if not manifest.is_file():
        return {"networks": [], "all_removed": True}
    rows = read_json(manifest).get("networks", [])
    results: list[dict[str, Any]] = []
    owned_task_root = (
        run_dir / ("eval_tasks" if eval_phase else "tasks")
    ).resolve()

    def docker_inspect(kind: str, identifier: str) -> dict[str, Any] | None:
        completed = subprocess.run(
            ["docker", kind, "inspect", identifier],
            text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            check=False, timeout=30,
        )
        if completed.returncode != 0:
            if "No such" in completed.stderr or "not found" in completed.stderr.lower():
                return None
            raise RuntimeError(
                f"docker {kind} inspect failed for {identifier}: "
                f"{completed.stderr[-1000:]}"
            )
        value = read_json_from_output(completed.stdout)
        if not isinstance(value, list) or not value or not isinstance(value[0], dict):
            raise RuntimeError(f"docker {kind} inspect returned an invalid payload")
        return value[0]

    def is_owned_compose_container(details: dict[str, Any]) -> bool:
        labels = details.get("Config", {}).get("Labels", {})
        if not isinstance(labels, dict):
            return False
        candidates: list[str] = []
        working_dir = labels.get("com.docker.compose.project.working_dir")
        if isinstance(working_dir, str) and working_dir:
            candidates.append(working_dir)
        config_files = labels.get("com.docker.compose.project.config_files")
        if isinstance(config_files, str):
            candidates.extend(value for value in config_files.split(",") if value)
        for candidate in candidates:
            path = Path(candidate).resolve()
            if path == owned_task_root or owned_task_root in path.parents:
                return True
        return False

    def cleanup_owned_endpoints(name: str) -> tuple[list[dict[str, Any]], str]:
        network = docker_inspect("network", name)
        if network is None:
            return [], ""
        attached = network.get("Containers", {})
        if not isinstance(attached, dict):
            return [], "network inspect Containers payload is invalid"
        endpoint_results: list[dict[str, Any]] = []
        for container_id, endpoint in sorted(attached.items()):
            container = docker_inspect("container", str(container_id))
            container_name = (
                endpoint.get("Name") if isinstance(endpoint, dict) else None
            )
            if container is None:
                endpoint_results.append({
                    "container_id": str(container_id),
                    "container_name": container_name,
                    "owned": True,
                    "removed": True,
                    "already_absent": True,
                })
                continue
            owned = is_owned_compose_container(container)
            record = {
                "container_id": str(container_id),
                "container_name": container_name,
                "owned": owned,
                "removed": False,
            }
            if not owned:
                record["error"] = "endpoint container is outside the phase task root"
                endpoint_results.append(record)
                return endpoint_results, str(record["error"])
            removed = subprocess.run(
                ["docker", "rm", "-f", str(container_id)],
                text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                check=False, timeout=90,
            )
            if removed.returncode != 0 and "No such" not in removed.stderr:
                record["error"] = removed.stderr[-1000:]
                endpoint_results.append(record)
                return endpoint_results, str(record["error"])
            for poll in range(1, 11):
                if docker_inspect("container", str(container_id)) is None:
                    record["removed"] = True
                    record["polls"] = poll
                    break
                time.sleep(1)
            if not record["removed"]:
                record["error"] = "container remained after docker rm -f"
                endpoint_results.append(record)
                return endpoint_results, str(record["error"])
            endpoint_results.append(record)
        return endpoint_results, ""

    # Each network is owned by this phase's task root and has a unique name.
    # Do not hold the campaign-wide lock while waiting for Docker rm/inspect;
    # one slow cleanup must not block unrelated case workers.
    for row in rows:
        name = str(row["name"])
        removed = False
        error = ""
        endpoint_cleanup: list[dict[str, Any]] = []
        timeout_count = 0
        for attempt in range(1, 6):
            try:
                completed = subprocess.run(
                    ["docker", "network", "rm", name],
                    text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                    check=False, timeout=60,
                )
            except subprocess.TimeoutExpired as exc:
                timeout_count += 1
                error = f"docker network rm timed out: {exc}"
                # Docker may commit the deletion even though the CLI
                # acknowledgement exceeded its timeout. Confirm authoritative
                # daemon state before retrying.
                try:
                    details = docker_inspect("network", name)
                except subprocess.TimeoutExpired:
                    details = None
                    error = "docker network rm and follow-up inspect timed out"
                if details is None:
                    removed = True
                    break
                attached = details.get("Containers", {})
                if isinstance(attached, dict) and attached:
                    endpoint_cleanup, endpoint_error = cleanup_owned_endpoints(name)
                    if endpoint_error:
                        error = endpoint_error
                        break
                if attempt < 5:
                    time.sleep(min(attempt, 3))
                continue
            if (
                completed.returncode == 0
                or "no such network" in completed.stderr.lower()
                or (
                    "network " in completed.stderr.lower()
                    and " not found" in completed.stderr.lower()
                )
            ):
                removed = True
                break
            error = completed.stderr[-1000:]
            if "active endpoints" in completed.stderr.lower():
                endpoint_cleanup, endpoint_error = cleanup_owned_endpoints(name)
                if endpoint_error:
                    error = endpoint_error
                    break
            time.sleep(min(attempt, 3))
        results.append({
            "name": name,
            "removed": removed,
            "error": error,
            "endpoint_cleanup": endpoint_cleanup,
            "timeout_count": timeout_count,
        })
    removed_names = {
        row["name"] for row in results if row["removed"]
    }
    released_rows = [
        row for row in rows if str(row.get("name")) in removed_names
    ]
    registry_release = release_compose_subnets(released_rows)
    result = {
        "networks": results,
        "all_removed": all(row["removed"] for row in results),
        "registry_release": registry_release,
    }
    write_json(
        run_dir / ("eval_network_cleanup.json" if eval_phase else "candidate_network_cleanup.json"),
        result,
    )
    if not result["all_removed"]:
        raise RuntimeError("owned compose network cleanup failed")
    return result


def run_terminalbench_native(
    *, benchmark: Path, candidate_job: Path, cases: tuple[str, ...], run_dir: Path,
    jobs_dir: Path, credential_file: Path, n_concurrent: int = 1,
) -> dict[str, Path]:
    """Run evaluator-owned nested Harbor tasks from the trusted host controller."""
    controller = NATIVE_ROOT / "terminalbench_controller.py"
    def run_case(case_id: str) -> tuple[str, Path]:
        trial = next(p for p in candidate_job.iterdir() if p.is_dir() and p.name.startswith(case_id + "__"))
        predictions = trial / "artifacts/logs/artifacts/candidate_output/predictions.jsonl"
        task_id = native_task_id(benchmark, case_id)
        task_metadata = contract_case_metadata(benchmark, case_id)
        native_task = benchmark / "evaluator/native_tasks" / str(task_id or "")
        output = run_dir / "native_eval" / case_id
        output.mkdir(parents=True, exist_ok=True)
        command = [
            sys.executable, str(controller), "--case-id", case_id,
            "--predictions", str(predictions), "--native-task", str(native_task),
            "--output-dir", str(output), "--jobs-dir", str(jobs_dir),
            "--credential-file", str(credential_file),
            "--expected-task-digest", str(task_metadata["native_task_digest"]),
        ]
        completed = subprocess.run(
            command, cwd=HARBOR_ROOT, env=harbor_env(), text=True,
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=False,
        )
        (output / "controller.stdout.log").write_text(completed.stdout, encoding="utf-8")
        (output / "controller.stderr.log").write_text(completed.stderr, encoding="utf-8")
        result = output / "native_result.json"
        if completed.returncode != 0 or not result.is_file():
            write_json(result, {
                "official_evaluation": False, "infrastructure_failure": True,
                "validity_gate": False, "score": 0,
                "errors": ["terminalbench_native_controller_failed"],
                "case_id": case_id,
            })
        return case_id, result
    with concurrent.futures.ThreadPoolExecutor(
        max_workers=min(n_concurrent, len(cases))
    ) as executor:
        return dict(executor.map(run_case, cases))


def run_tau3_native(
    *, benchmark: Path, candidate_job: Path, cases: tuple[str, ...], run_dir: Path,
    credential_file: Path, n_concurrent: int = 1,
) -> dict[str, Path]:
    """Run fresh-DB official τ³ episodes through the trusted brokered controller."""
    controller = NATIVE_ROOT / "tau3_controller.py"
    def run_case(case_id: str) -> tuple[str, Path]:
        trial = next(p for p in candidate_job.iterdir() if p.is_dir() and p.name.startswith(case_id + "__"))
        predictions = trial / "artifacts/logs/artifacts/candidate_output/predictions.jsonl"
        task_metadata = contract_case_metadata(benchmark, case_id)
        task_id = task_metadata.get("upstream_id")
        domain = task_metadata.get("domain")
        output = run_dir / "native_eval" / case_id
        output.mkdir(parents=True, exist_ok=True)
        command = [
            sys.executable, str(controller), "--case-id", case_id,
            "--domain", str(domain or ""), "--task-id", str(task_id or ""),
            "--predictions", str(predictions),
            "--credential-file", str(credential_file), "--output-dir", str(output),
        ]
        completed = subprocess.run(
            command, cwd=HARBOR_ROOT, env=harbor_env(), text=True,
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=False,
        )
        (output / "controller.stdout.log").write_text(completed.stdout, encoding="utf-8")
        (output / "controller.stderr.log").write_text(completed.stderr, encoding="utf-8")
        result = output / "native_result.json"
        if completed.returncode != 0 or not result.is_file():
            write_json(result, {
                "official_evaluation": False, "infrastructure_failure": True,
                "validity_gate": False, "score": 0,
                "errors": ["tau3_native_controller_failed"], "case_id": case_id,
            })
        return case_id, result
    with concurrent.futures.ThreadPoolExecutor(
        max_workers=min(n_concurrent, len(cases))
    ) as executor:
        return dict(executor.map(run_case, cases))


def run_pinchbench_native(
    *, benchmark: Path, candidate_job: Path, cases: tuple[str, ...], run_dir: Path,
    credential_file: Path, n_concurrent: int = 1,
) -> dict[str, Path]:
    """Run pinned OpenClaw tasks and official PinchBench graders through a broker."""
    controller = NATIVE_ROOT / "pinchbench_controller.py"
    def run_case(case_id: str) -> tuple[str, Path]:
        trial = next(p for p in candidate_job.iterdir() if p.is_dir() and p.name.startswith(case_id + "__"))
        predictions = trial / "artifacts/logs/artifacts/candidate_output/predictions.jsonl"
        task_id = native_task_id(benchmark, case_id)
        output = run_dir / "native_eval" / case_id
        output.mkdir(parents=True, exist_ok=True)
        command = [
            sys.executable, str(controller), "--case-id", case_id,
            "--task-id", str(task_id or ""), "--predictions", str(predictions),
            "--credential-file", str(credential_file), "--output-dir", str(output),
        ]
        completed = subprocess.run(
            command, cwd=HARBOR_ROOT, env=harbor_env(), text=True,
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=False,
        )
        (output / "controller.stdout.log").write_text(completed.stdout, encoding="utf-8")
        (output / "controller.stderr.log").write_text(completed.stderr, encoding="utf-8")
        result = output / "native_result.json"
        if completed.returncode != 0 or not result.is_file():
            write_json(result, {
                "official_evaluation": False, "infrastructure_failure": True,
                "validity_gate": False, "score": 0,
                "errors": ["pinchbench_native_controller_failed"], "case_id": case_id,
            })
        return case_id, result
    with concurrent.futures.ThreadPoolExecutor(
        max_workers=min(n_concurrent, len(cases))
    ) as executor:
        return dict(executor.map(run_case, cases))


def run_osworld_native(
    *, benchmark: Path, candidate_job: Path, cases: tuple[str, ...], run_dir: Path,
    credential_file: Path, n_concurrent: int = 1,
) -> dict[str, Path]:
    """Run isolated official OSWorld desktop episodes through the trusted KVM controller."""
    controller = NATIVE_ROOT / "osworld_controller.py"
    images = NATIVE_ROOT / "osworld_images"
    manifest_path = images / "case_manifest.json"
    manifest = read_json(manifest_path)
    mapping = manifest.get("cases", {}) if isinstance(manifest, dict) else {}
    expected_cases = set(split_cases(benchmark, "dev")) | set(split_cases(benchmark, "hidden"))
    smoke_copy = "smoke" in benchmark_contract(benchmark)  # truncated AgentSWE smoke copy of the split
    if set(mapping) != expected_cases and not (smoke_copy and expected_cases <= set(mapping)):
        raise RuntimeError("OSWorld case manifest differs from the frozen benchmark split")
    if not OSWORLD_RUNTIME_PYTHON.is_file():
        raise RuntimeError("OSWorld locked runtime Python is missing")
    def run_case(case_id: str) -> tuple[str, Path]:
        trial = next(
            path for path in candidate_job.iterdir()
            if path.is_dir() and path.name.startswith(case_id + "__")
        )
        predictions = trial / "artifacts/logs/artifacts/candidate_output/predictions.jsonl"
        row = mapping.get(case_id, {})
        task_id = row.get("task_id") if isinstance(row, dict) else None
        output = run_dir / "native_eval" / case_id
        output.mkdir(parents=True, exist_ok=True)
        command = [
            str(OSWORLD_RUNTIME_PYTHON), str(controller),
            "--case-id", case_id, "--task-id", str(task_id or ""),
            "--predictions", str(predictions),
            "--credential-file", str(credential_file),
            "--vm-manifest", str(images / "vm_manifest.json"),
            "--runtime-python", str(OSWORLD_RUNTIME_PYTHON),
            "--output-dir", str(output),
        ]
        completed = subprocess.run(
            command, cwd=HARBOR_ROOT, env=harbor_env(), text=True,
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=False,
        )
        (output / "controller.stdout.log").write_text(completed.stdout, encoding="utf-8")
        (output / "controller.stderr.log").write_text(completed.stderr, encoding="utf-8")
        result = output / "native_result.json"
        if completed.returncode != 0 or not result.is_file():
            write_json(result, {
                "official_evaluation": False, "infrastructure_failure": True,
                "validity_gate": False, "score": 0,
                "errors": ["osworld_native_controller_failed"], "case_id": case_id,
            })
        return case_id, result
    with concurrent.futures.ThreadPoolExecutor(
        max_workers=min(n_concurrent, len(cases))
    ) as executor:
        return dict(executor.map(run_case, cases))


def collect_candidate(job: Path, cases: tuple[str, ...], candidate_digest: str) -> dict[str, Any]:
    result = read_json(job / "result.json")
    stats = result.get("stats", {})
    if int(stats.get("n_errored_trials", 0)) > 0:
        failures = []
        for case_id in cases:
            trial = next(p for p in job.iterdir() if p.is_dir() and p.name.startswith(case_id + "__"))
            trial_result = read_json(trial / "result.json")
            exception_message = "trial_result_missing_or_null"
            if isinstance(trial_result, dict):
                exception_info = trial_result.get("exception_info")
                if isinstance(exception_info, dict):
                    exception_message = str(
                        exception_info.get("exception_message")
                        or exception_info.get("message")
                        or exception_message
                    )
            failures.append({
                "case_id": case_id,
                "trial": trial.name,
                "exception": exception_message,
            })
        raise RuntimeError(f"Candidate Harbor infrastructure failure: {failures}")
    values = []
    for case_id in cases:
        trial = next(p for p in job.iterdir() if p.is_dir() and p.name.startswith(case_id + "__"))
        sidecar_evidence = trial / "artifacts/browsecomp_broker/resource_evidence.json"
        candidate_output = trial / "artifacts/logs/artifacts/candidate_output"
        if sidecar_evidence.is_file() and candidate_output.is_dir():
            shutil.copy2(sidecar_evidence, candidate_output / "resource_evidence.json")
        execution_evidence = trial / "artifacts/logs/artifacts/candidate_run/candidate_evidence.json"
        if execution_evidence.is_file() and candidate_output.is_dir():
            shutil.copy2(execution_evidence, candidate_output / "candidate_execution_evidence.json")
        contract = read_json(trial / "verifier/score_contract.json")
        values.append({"case_id": case_id, "score": int(contract.get("score", 0)), "contract_valid": bool(contract.get("contract_valid")), "trial": trial.name, "contract": str(trial / "verifier/score_contract.json")})
    return {"cases": values, "total_score": sum(v["score"] for v in values), "mean_score": sum(v["score"] for v in values) / len(values), "harbor_stats": result.get("stats"), "candidate_digest": candidate_digest}


def eval_trial_exception(trial: Path, job_stats: Any) -> str | None:
    """The exception Harbor recorded for an Eval trial, as "Type: message", or None when it recorded none.

    Harbor writes it to the trial's result.json (exception_info) and lists the trial under the job's
    exception_stats; either one marks the trial as errored.
    """
    try:
        trial_result = read_json(trial / "result.json")
    except (OSError, ValueError):
        trial_result = None
    info = trial_result.get("exception_info") if isinstance(trial_result, dict) else None
    if isinstance(info, dict):
        kind = str(info.get("exception_type") or "HarborTrialError")
        message = str(info.get("exception_message") or info.get("message") or "")
        return f"{kind}: {message}" if message else kind
    if info:
        return str(info)
    evals = job_stats.get("evals") if isinstance(job_stats, dict) else None
    for row in (evals.values() if isinstance(evals, dict) else ()):
        exceptions = row.get("exception_stats") if isinstance(row, dict) else None
        for kind, trials in (exceptions.items() if isinstance(exceptions, dict) else ()):
            if isinstance(trials, list) and trial.name in trials:
                return str(kind)
    return None


def collect_eval(job: Path, cases: tuple[str, ...]) -> dict[str, Any]:
    result = read_json(job / "result.json")
    # An Eval trial that Harbor ended with an exception before the evaluator wrote its score contract (environment
    # start or verifier timeout, a failed compose command) was never scored: that is an infrastructure failure of the
    # evaluation, as in collect_candidate, so the phase is replayed instead of failing on the missing contract.
    # A trial whose contract exists is read as before, whatever Harbor recorded after the verifier finished.
    failures = []
    for case_id in cases:
        trial = next(p for p in job.iterdir() if p.is_dir() and p.name.startswith(case_id + "__"))
        if (trial / "verifier/score_contract.json").is_file():
            continue
        exception = eval_trial_exception(trial, result.get("stats"))
        if exception is not None:
            failures.append({"case_id": case_id, "trial": trial.name, "exception": exception})
    if failures:
        raise InfrastructureEvaluationError(f"Eval Harbor infrastructure failure: {failures}")
    values = []
    for case_id in cases:
        trial = next(p for p in job.iterdir() if p.is_dir() and p.name.startswith(case_id + "__"))
        contract = read_json(trial / "verifier/score_contract.json")
        values.append({
            "case_id": case_id,
            "score": int(contract.get("score", 0)),
            "contract_valid": bool(contract.get("contract_valid")),
            "validity_gate": bool(contract.get("validity_gate")),
            "official_evaluation": bool(contract.get("official_evaluation")),
            "infrastructure_failure": bool(contract.get("infrastructure_failure")),
            "deterministic_infrastructure_failure": bool(
                contract.get("deterministic_infrastructure_failure")
            ),
            "native_task_available": bool(contract.get("native_task_available")),
            "trial": trial.name,
            "contract": str(trial / "verifier/score_contract.json"),
            "eval_result": str(trial / "artifacts/logs/artifacts/eval/eval_result.json"),
        })
    return {"cases": values, "total_score": sum(v["score"] for v in values), "mean_score": sum(v["score"] for v in values) / len(values), "harbor_stats": result.get("stats")}


def retry_browsecomp_infrastructure_rows(
    *, benchmark: Path, candidate: Path, initial_summary: dict[str, Any],
    run_dir: Path, jobs_dir: Path, credential_file: Path, n_concurrent: int = 1,
) -> dict[str, Any]:
    """Retry only invalid BrowseComp rows through a fresh Candidate and Eval pair."""
    ordered_ids = [str(row["case_id"]) for row in initial_summary.get("cases", [])]
    current = {
        str(row["case_id"]): dict(row)
        for row in initial_summary.get("cases", [])
        if isinstance(row, dict) and row.get("case_id") is not None
    }
    evidence: list[dict[str, Any]] = []
    for retry_index in range(1, MAX_ROW_INFRASTRUCTURE_RETRIES + 1):
        pending = tuple(
            case_id for case_id in ordered_ids
            if current[case_id].get("contract_valid") is not True
            or current[case_id].get("infrastructure_failure") is True
        )
        if not pending:
            break
        attempt_name = f"{run_dir.name}-row-retry-{retry_index:03d}"
        attempt_dir = run_dir / "row_infrastructure_retries" / attempt_name
        record: dict[str, Any] = {
            "attempt": retry_index,
            "cases": list(pending),
            "run_dir": str(attempt_dir),
        }
        candidate_config, candidate_job = stage_job(
            benchmark_id=BROWSECOMP_ID, benchmark=benchmark, candidate=candidate,
            cases=pending, run_dir=attempt_dir, jobs_dir=jobs_dir,
            credential_file=credential_file, n_concurrent=n_concurrent,
        )
        try:
            candidate_process = run_harbor(candidate_config, attempt_dir / "harbor-run")
            record["candidate_process"] = candidate_process
            record["candidate_job"] = str(candidate_job)
            if candidate_process["exit_code"] != 0:
                record["error"] = "candidate_harbor_failed"
                evidence.append(record)
                continue
            try:
                record["candidate_summary"] = collect_candidate(
                    candidate_job, pending, tree_digest(candidate)
                )
            except Exception as exc:
                record["error"] = f"candidate_collection_failed:{type(exc).__name__}"
                evidence.append(record)
                continue
        finally:
            record["candidate_network_cleanup"] = cleanup_job_networks(
                attempt_dir, eval_phase=False
            )
        eval_config, eval_job = stage_job(
            benchmark_id=BROWSECOMP_ID, benchmark=benchmark, candidate=candidate,
            cases=pending, run_dir=attempt_dir, jobs_dir=jobs_dir,
            eval_phase=True, candidate_job=candidate_job,
            credential_file=credential_file, n_concurrent=n_concurrent,
        )
        try:
            eval_process = run_harbor(eval_config, attempt_dir / "eval-run")
            record["eval_process"] = eval_process
            record["eval_job"] = str(eval_job)
            if eval_process["exit_code"] != 0:
                record["error"] = "eval_harbor_failed"
                evidence.append(record)
                continue
            retry_summary = collect_eval(eval_job, pending)
        finally:
            record["eval_network_cleanup"] = cleanup_job_networks(
                attempt_dir, eval_phase=True
            )
        record["eval_summary"] = retry_summary
        for row in retry_summary["cases"]:
            current[str(row["case_id"])] = dict(row)
        record["remaining_infrastructure_cases"] = [
            case_id for case_id in pending
            if current[case_id].get("contract_valid") is not True
            or current[case_id].get("infrastructure_failure") is True
        ]
        evidence.append(record)
    result = dict(initial_summary)
    result["cases"] = [current[case_id] for case_id in ordered_ids]
    result["total_score"] = sum(int(row.get("score", 0)) for row in result["cases"])
    result["mean_score"] = (
        result["total_score"] / len(result["cases"]) if result["cases"] else 0.0
    )
    result["row_infrastructure_retries"] = evidence
    result["unresolved_infrastructure_cases"] = [
        case_id for case_id in ordered_ids
        if current[case_id].get("contract_valid") is not True
        or current[case_id].get("infrastructure_failure") is True
    ]
    return result


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--benchmark-id", choices=sorted(BENCHMARKS))
    parser.add_argument("--benchmark", type=Path)
    parser.add_argument("--env-prefix", type=Path)
    parser.add_argument("--credential-file", type=Path)
    parser.add_argument("--candidate", type=Path, required=True)
    parser.add_argument("--run-id", required=True)
    parser.add_argument("--runs-dir", type=Path, default=ADAPTER_DIR / "runs")
    parser.add_argument("--jobs-dir", type=Path, default=HARBOR_ROOT / "jobs")
    parser.add_argument(
        "--cases",
        nargs="+",
        default=None,
        help="Explicit case IDs; defaults to dev cases, or hidden cases with --hidden.",
    )
    parser.add_argument("--stage-only", action="store_true")
    parser.add_argument("--hidden", action="store_true")
    parser.add_argument("--n-concurrent", type=int, default=1)
    parser.add_argument("--row-cache-dir", type=Path, default=DEFAULT_ROW_CACHE)
    parser.add_argument("--no-row-cache", action="store_true")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    if not 1 <= args.n_concurrent <= 20:
        raise SystemExit("--n-concurrent must be between 1 and 20")
    if args.benchmark_id is None:
        if args.benchmark is None:
            raise SystemExit("--benchmark-id or --benchmark is required")
        benchmark = args.benchmark.resolve()
        matches = [key for key, value in BENCHMARKS.items() if value.resolve() == benchmark]
        if len(matches) != 1:
            raise SystemExit(f"benchmark path is not one of the locked optimization cases: {benchmark}")
        args.benchmark_id = matches[0]
    benchmark = BENCHMARKS[args.benchmark_id].resolve()
    candidate = args.candidate.resolve()
    if args.cases and args.hidden:
        raise SystemExit("--cases and --hidden are mutually exclusive")
    dev_cases = split_cases(benchmark, "dev")
    hidden_cases = split_cases(benchmark, "hidden")
    cases = tuple(args.cases) if args.cases else (hidden_cases if args.hidden else dev_cases)
    expected = set(dev_cases + hidden_cases)
    if not set(cases) <= expected:
        raise SystemExit(f"unknown case: {sorted(set(cases) - expected)}")
    run_dir = args.runs_dir.resolve() / args.run_id
    if run_dir.exists():
        raise SystemExit(f"run exists: {run_dir}")
    run_dir.mkdir(parents=True)
    benchmark_digest = tree_digest(benchmark)
    adapter_source_digest = protocol_digest()
    candidate_digest = tree_digest(candidate)
    lock = {"schema_version": "1.0", "benchmark_id": args.benchmark_id, "benchmark": str(benchmark), "benchmark_digest": benchmark_digest, "adapter_source_digest": adapter_source_digest, "candidate_digest": candidate_digest, "cases": list(cases), "n_concurrent": args.n_concurrent, "created_at": utc_now()}
    write_json(run_dir / "evaluation_protocol_lock.json", lock)
    # one_stop.py checks this evaluator-owned preflight after every phase so a
    # phase cannot silently switch benchmark trees or case sets.
    write_json(run_dir / "preflight.json", {
        "schema_version": "1.0",
        "benchmark_id": args.benchmark_id,
        "benchmark": str(benchmark),
        "benchmark_digest": lock["benchmark_digest"],
        "adapter_source_digest": lock["adapter_source_digest"],
        "cases": list(cases),
        "case_role": "hidden" if args.hidden else ("explicit" if args.cases else "dev"),
        "task_contract_digest": file_digest(benchmark / "task_contract.json"),
        "dev_case_count": len(dev_cases),
        "hidden_case_count": len(hidden_cases),
        "n_concurrent": args.n_concurrent,
        "created_at": utc_now(),
    })
    credential_file = args.credential_file.resolve() if args.credential_file else None
    if credential_file is not None and not credential_file.is_file():
        raise FileNotFoundError(f"Credential file is missing: {credential_file}")
    # Only BrowseComp candidate harnesses need direct search/model resources.
    # TerminalBench candidates execute in the native task container, while τ³
    # and PinchBench model access is isolated behind trusted brokers.
    candidate_credential_file = (
        credential_file if args.benchmark_id == BROWSECOMP_ID else None
    )
    cache_dir = args.row_cache_dir.resolve()
    cached_cases: dict[str, dict[str, Any]] = {}
    if not args.no_row_cache and not args.stage_only:
        for case_id in cases:
            cached = load_cached_row(
                cache_dir=cache_dir,
                candidate_digest=candidate_digest,
                case_id=case_id,
                benchmark_digest=benchmark_digest,
                protocol_source_digest=adapter_source_digest,
                run_dir=run_dir,
            )
            if cached is not None:
                cached_cases[case_id] = cached
    cases_to_run = tuple(case_id for case_id in cases if case_id not in cached_cases)
    write_json(run_dir / "row_cache_manifest.json", {
        "enabled": not args.no_row_cache,
        "cache_dir": str(cache_dir),
        "candidate_digest": candidate_digest,
        "benchmark_digest": benchmark_digest,
        "protocol_source_digest": adapter_source_digest,
        "hits": list(cached_cases),
        "misses": list(cases_to_run),
    })
    if not cases_to_run:
        ordered = [cached_cases[case_id] for case_id in cases]
        summary = {
            "schema_version": "1.1", "benchmark_id": args.benchmark_id,
            "run_id": args.run_id, "candidate_run": {"cache_only": True},
            "eval_run": {"cache_only": True}, "cases": ordered,
            "total_score": sum(int(row["score"]) for row in ordered),
            "max_total_score": 100 * len(ordered),
            "mean_score": sum(int(row["score"]) for row in ordered) / len(ordered),
            "row_cache_hits": len(ordered), "row_cache_misses": 0,
        }
        write_json(run_dir / "score_summary.json", summary)
        print(json.dumps(summary, indent=2))
        return 0
    config, job = stage_job(
        benchmark_id=args.benchmark_id, benchmark=benchmark,
        candidate=candidate, cases=cases_to_run, run_dir=run_dir,
        jobs_dir=args.jobs_dir, credential_file=candidate_credential_file,
        n_concurrent=args.n_concurrent,
    )
    if args.stage_only:
        write_json(run_dir / "stage_result.json", {"status": "staged", "config": str(config), "expected_job": str(job)})
        print(run_dir)
        return 0
    try:
        process = run_harbor(config, run_dir / "harbor-run")
        if process["exit_code"] != 0:
            raise RuntimeError(f"Candidate Harbor job failed; see {process['stderr']}")
        candidate_summary = collect_candidate(job, cases_to_run, candidate_digest)
    finally:
        cleanup_job_networks(run_dir, eval_phase=False)
    native_results = None
    if args.benchmark_id == "terminalbench-code-agent-optimization-v1":
        native_results = run_terminalbench_native(
            benchmark=benchmark, candidate_job=job, cases=cases_to_run,
            run_dir=run_dir, jobs_dir=args.jobs_dir,
            credential_file=credential_file, n_concurrent=args.n_concurrent,
        )
    elif args.benchmark_id == "tau3-tool-agent-optimization-v1":
        if credential_file is None:
            raise RuntimeError("tau3 native evaluator requires the authorized credential file")
        native_results = run_tau3_native(
            benchmark=benchmark, candidate_job=job, cases=cases_to_run,
            run_dir=run_dir, credential_file=credential_file,
            n_concurrent=args.n_concurrent,
        )
    elif args.benchmark_id == "pinchbench-openclaw-agent-optimization-v1":
        if credential_file is None:
            raise RuntimeError("pinchbench native evaluator requires the authorized credential file")
        native_results = run_pinchbench_native(
            benchmark=benchmark, candidate_job=job, cases=cases_to_run,
            run_dir=run_dir, credential_file=credential_file,
            n_concurrent=args.n_concurrent,
        )
    elif args.benchmark_id == OSWORLD_ID:
        if credential_file is None:
            raise RuntimeError("OSWorld native evaluator requires the authorized credential file")
        native_results = run_osworld_native(
            benchmark=benchmark, candidate_job=job, cases=cases_to_run,
            run_dir=run_dir, credential_file=credential_file,
            n_concurrent=args.n_concurrent,
        )
    eval_credential_file = (
        credential_file
        if args.benchmark_id == BROWSECOMP_ID
        else None
    )
    eval_config, eval_job = stage_job(benchmark_id=args.benchmark_id, benchmark=benchmark, candidate=candidate, cases=cases_to_run, run_dir=run_dir, jobs_dir=args.jobs_dir, eval_phase=True, candidate_job=job, credential_file=eval_credential_file, native_results=native_results, n_concurrent=args.n_concurrent)
    try:
        eval_process = run_harbor(eval_config, run_dir / "eval-run")
        if eval_process["exit_code"] != 0:
            raise RuntimeError(f"Eval Harbor job failed; see {eval_process['stderr']}")
        eval_summary = collect_eval(eval_job, cases_to_run)
    finally:
        cleanup_job_networks(run_dir, eval_phase=True)
    if args.benchmark_id == BROWSECOMP_ID:
        eval_summary = retry_browsecomp_infrastructure_rows(
            benchmark=benchmark, candidate=candidate, initial_summary=eval_summary,
            run_dir=run_dir, jobs_dir=args.jobs_dir,
            credential_file=credential_file, n_concurrent=args.n_concurrent,
        )
    fresh = {row["case_id"]: row for row in eval_summary["cases"]}
    for row in fresh.values():
        row["cache_key"] = store_cached_row(
            cache_dir=cache_dir,
            candidate_digest=candidate_digest,
            case=row,
            benchmark_digest=benchmark_digest,
            protocol_source_digest=adapter_source_digest,
        )
        row["cache_hit"] = False
    combined = {**cached_cases, **fresh}
    ordered = [combined[case_id] for case_id in cases]
    summary = {"schema_version": "1.1", "benchmark_id": args.benchmark_id, "run_id": args.run_id, "candidate_run": candidate_summary, "eval_run": eval_summary, "cases": ordered, "total_score": sum(int(row["score"]) for row in ordered), "max_total_score": 100 * len(cases), "mean_score": sum(int(row["score"]) for row in ordered) / len(ordered), "row_cache_hits": len(cached_cases), "row_cache_misses": len(cases_to_run)}
    write_json(run_dir / "score_summary.json", summary)
    print(json.dumps(summary, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
