#!/usr/bin/env python3
"""Trusted host controller for official, isolated OSWorld attempts."""

from __future__ import annotations

import argparse
import errno
import fcntl
import hashlib
import json
import os
import re
import secrets
import socket
import stat
import subprocess
import sys
import time
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from osworld_agent import AgentSpecError, load_prediction
from osworld_provider import (
    AUDIT_LABEL,
    PROVIDER_DIGEST,
    PROVIDER_IMAGE,
    PROVIDER_OVERLAY_ROOT,
    PROVIDER_PULL_IMAGE,
)
from osworld_runtime import infrastructure_result, ordinary_result, write_json
from osworld_fixtures import prepare_fixture_cache
from osworld_network import preflight_network


ROOT = Path(__file__).resolve().parent
SOURCE_COMMIT = "091f5ef1d5544bc74953c77875d5feb5bed30108"
TASK_METADATA_DIGEST = "e7c577823fbcd1e503a1375219084dee8a48baf9f0537e9871588812f04f18ce"
MAX_ATTEMPTS = 5
# Release: host locations come from the runner (AGENTSWE_*); the paper controller hard-coded its host.
_AGENTSWE_HOME = Path(os.environ.get("AGENTSWE_HOME", ".agentswe"))
# Evaluator-owned assets fetched by setup: VM image, boot assets, fixtures, the OSWorld source tree.
OSWORLD_ASSETS = Path(os.environ.get("AGENTSWE_OSWORLD_ASSETS", str(_AGENTSWE_HOME / "assets" / "osworld")))
PYTHON_ROOT = Path(os.environ.get(
    "AGENTSWE_STANDALONE_PYTHON312", str(_AGENTSWE_HOME / "tools" / "cpython-3.12")))
# Same image as the paper broker (it pulled this digest through a registry mirror).
BROKER_IMAGE = os.environ.get(
    "AGENTSWE_OSWORLD_BROKER_IMAGE",
    "docker.io/library/ubuntu@"
    "sha256:561618e2c15bf2397621dd04f96926663a3b5616c189cf7e38db7e82f5c538ea",
)
OSWORLD_SOURCE = Path(os.environ.get("AGENTSWE_OSWORLD_SOURCE", str(OSWORLD_ASSETS / "osworld-source")))
# The shared effort spellings the vision broker imports (agentswe_broker.config.normalize_effort),
# mounted read-only into the broker container.
BROKER_PACKAGE = ROOT.parents[2] / "broker" / "agentswe_broker"
# Host-wide VM admission gate shared by every run on this host.
CAPACITY_ROOT = Path(os.environ.get(
    "AGENTSWE_OSWORLD_CAPACITY_ROOT", str(_AGENTSWE_HOME / "coordination" / "osworld-capacity")))
VM_DIGEST_CACHE = CAPACITY_ROOT / "vm-digest-cache.json"
VM_DIGEST_CACHE_LOCK = CAPACITY_ROOT / "vm-digest-cache.lock"
DEFAULT_VM_CAPACITY = 1
DEFAULT_CAPACITY_POLL_SECONDS = 5.0
DEFAULT_CAPACITY_WAIT_SECONDS = 7200.0
CAPACITY_STABLE_SAMPLES = 3
MIN_NUMA_FREE_KIB = 8 * 1024 * 1024
# Release: a node's capacity counts reclaimable memory, not MemFree alone. The paper gate (MemFree >= 8 GiB on some
# node) waited out its 7200 s budget on hosts whose free memory sits in page cache (MemAvailable 621 GB, every node's
# MemFree under 7 GiB) and failed the case as infrastructure. Same 8 GiB threshold; AGENTSWE_OSWORLD_MIN_NUMA_FREE_GIB
# overrides it (the runner exports it through config_env, so the launch manifest records it).
NUMA_RECLAIMABLE_FIELDS = ("MemFree", "Inactive(file)", "KReclaimable")
NUMA_THRESHOLD_ENV = "AGENTSWE_OSWORLD_MIN_NUMA_FREE_GIB"
NODE_SYSFS = Path("/sys/devices/system/node")


class CapacityGateError(RuntimeError):
    """OSWorld VM admission failed before an episode could start."""


def online_numa_nodes() -> list[str]:
    path = Path("/sys/devices/system/node/online")
    if not path.is_file():
        return []
    nodes: list[str] = []
    for part in path.read_text(encoding="utf-8").strip().split(","):
        if "-" in part:
            start, end = (int(value) for value in part.split("-", 1))
            nodes.extend(str(value) for value in range(start, end + 1))
        elif re.fullmatch(r"\d+", part):
            nodes.append(part)
    return nodes


def host_gate_evidence() -> dict[str, Any]:
    kvm = Path("/dev/kvm")
    if not kvm.exists():
        raise CapacityGateError("kvm_device_missing")
    if not stat.S_ISCHR(kvm.stat().st_mode):
        raise CapacityGateError("kvm_device_not_character_device")
    numa_nodes = online_numa_nodes()
    if not numa_nodes:
        raise CapacityGateError("numa_topology_missing")
    affinity = sorted(os.sched_getaffinity(0))
    if not affinity:
        raise CapacityGateError("cpu_affinity_empty")
    return {
        "kvm_device": str(kvm),
        "kvm_character_device": True,
        "controller_process_kvm_rw": os.access(kvm, os.R_OK | os.W_OK),
        "kvm_access_path": "docker_provider_device_mapping_fail_closed",
        "numa_nodes_online": numa_nodes,
        "cpu_affinity_count": len(affinity),
        "cpu_affinity_min": affinity[0],
        "cpu_affinity_max": affinity[-1],
    }


def min_numa_free_kib() -> tuple[int, str]:
    """The per-node reclaimable-memory threshold in KiB and where it came from."""
    raw = os.environ.get(NUMA_THRESHOLD_ENV, "").strip()
    if not raw:
        return MIN_NUMA_FREE_KIB, "default"
    try:
        gib = float(raw)
    except ValueError as exc:
        raise CapacityGateError("invalid_min_numa_free_gib") from exc
    if not 0 < gib <= 4096:
        raise CapacityGateError("invalid_min_numa_free_gib")
    return int(gib * 1024 * 1024), NUMA_THRESHOLD_ENV


def node_meminfo_kib(text: str) -> dict[str, int]:
    """Fields of a /sys/devices/system/node/nodeN/meminfo file ("Node N <Field>: <value> kB")."""
    values: dict[str, int] = {}
    for line in text.splitlines():
        match = re.match(r"\s*Node\s+\d+\s+(\S+):\s+(\d+)\s+kB", line)
        if match:
            values[match.group(1)] = int(match.group(2))
    return values


def numa_capacity_evidence(host: dict[str, Any], sysfs: Path | None = None) -> dict[str, Any]:
    threshold, source = min_numa_free_kib()
    memfree_kib: dict[str, int] = {}
    reclaimable_kib: dict[str, int] = {}
    for node in host["numa_nodes_online"]:
        path = (sysfs or NODE_SYSFS) / f"node{node}" / "meminfo"
        if not path.is_file():
            continue
        fields = node_meminfo_kib(path.read_text(encoding="utf-8"))
        if "MemFree" not in fields:
            continue
        memfree_kib[str(node)] = fields["MemFree"]
        reclaimable_kib[str(node)] = sum(fields.get(name, 0) for name in NUMA_RECLAIMABLE_FIELDS)
    eligible = sorted(
        node for node, kib in reclaimable_kib.items()
        if kib >= threshold
    )
    return {
        "numa_memfree_kib": memfree_kib,
        "numa_reclaimable_kib": reclaimable_kib,
        "reclaimable_fields": list(NUMA_RECLAIMABLE_FIELDS),
        "min_numa_free_kib": threshold,
        "min_numa_free_source": source,
        "eligible_numa_nodes": eligible,
        "ready": bool(eligible),
    }


def append_capacity_event(path: Path | None, event: dict[str, Any]) -> None:
    if path is None:
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as stream:
        stream.write(json.dumps(event, sort_keys=True) + "\n")
        stream.flush()
        os.fsync(stream.fileno())


def pid_alive(pid: int) -> bool:
    if pid == os.getpid():
        return True
    try:
        os.kill(pid, 0)
    except OSError as exc:
        return exc.errno == errno.EPERM
    return True


@dataclass
class VmLease:
    slot_id: int
    capacity: int
    run_token: str
    slot_handle: Any
    evidence_path: Path | None
    acquired_at: float
    host: dict[str, Any]

    def release(self) -> None:
        try:
            append_capacity_event(self.evidence_path, {
                "event": "capacity_release", "run_token": self.run_token,
                "slot_id": self.slot_id,
                "held_seconds": round(time.monotonic() - self.acquired_at, 3),
            })
        except OSError:
            pass
        try:
            fcntl.flock(self.slot_handle.fileno(), fcntl.LOCK_UN)
        finally:
            self.slot_handle.close()


class VmCapacityGate:
    """FIFO cross-process queue with bounded KVM VM admission."""

    def __init__(
        self, *, root: Path = CAPACITY_ROOT, capacity: int | None = None,
        poll_seconds: float | None = None, wait_seconds: float | None = None,
        enforce_numa_memory: bool = True,
    ) -> None:
        try:
            self.capacity = int(os.environ.get("OSWORLD_VM_CAPACITY", DEFAULT_VM_CAPACITY)) if capacity is None else capacity
            self.poll_seconds = float(os.environ.get("OSWORLD_CAPACITY_POLL_SECONDS", DEFAULT_CAPACITY_POLL_SECONDS)) if poll_seconds is None else poll_seconds
            self.wait_seconds = float(os.environ.get("OSWORLD_CAPACITY_WAIT_SECONDS", DEFAULT_CAPACITY_WAIT_SECONDS)) if wait_seconds is None else wait_seconds
        except ValueError as exc:
            raise CapacityGateError("invalid_capacity_environment") from exc
        if not 1 <= self.capacity <= 32 or self.poll_seconds <= 0 or self.wait_seconds <= 0:
            raise CapacityGateError("invalid_capacity_configuration")
        self.root = root
        self.queue = root / "queue"
        self.slots = root / "slots"
        self.queue_lock_path = root / "queue.lock"
        self.enforce_numa_memory = enforce_numa_memory

    def prepare(self) -> None:
        self.queue.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.slots.mkdir(parents=True, exist_ok=True, mode=0o700)
        for slot_id in range(self.capacity):
            (self.slots / f"slot-{slot_id}.lock").touch(mode=0o600, exist_ok=True)

    def prune_dead_tickets(self) -> None:
        for ticket in sorted(self.queue.glob("*.json")):
            try:
                payload = json.loads(ticket.read_text(encoding="utf-8"))
                pid = int(payload.get("pid", -1))
            except (OSError, ValueError, TypeError, json.JSONDecodeError):
                ticket.unlink(missing_ok=True)
                continue
            if not pid_alive(pid):
                ticket.unlink(missing_ok=True)

    def acquire(self, run_token: str, evidence_path: Path | None = None) -> VmLease:
        if not run_token:
            raise CapacityGateError("capacity_run_token_missing")
        host = host_gate_evidence()
        self.prepare()
        created_ns = time.time_ns()
        ticket = self.queue / f"{created_ns:020d}-{os.getpid():08d}-{run_token}.json"
        ticket.write_text(json.dumps({
            "pid": os.getpid(), "run_token": run_token, "created_ns": created_ns,
        }), encoding="utf-8")
        started = time.monotonic()
        stable_samples = 0
        append_capacity_event(evidence_path, {
            "event": "capacity_queued", "run_token": run_token,
            "queue_ticket": ticket.name, "vm_capacity": self.capacity,
            "queue_root": str(self.root), "host": host,
        })
        deadline = started + self.wait_seconds
        try:
            while True:
                wait_event: dict[str, Any] = {
                    "event": "capacity_wait", "run_token": run_token,
                    "queue_ticket": ticket.name, "vm_capacity": self.capacity,
                }
                with self.queue_lock_path.open("a+") as queue_lock:
                    fcntl.flock(queue_lock.fileno(), fcntl.LOCK_EX)
                    self.prune_dead_tickets()
                    tickets = sorted(self.queue.glob("*.json"))
                    wait_event["queue_depth"] = len(tickets)
                    if tickets and tickets[0] == ticket:
                        numa_capacity = numa_capacity_evidence(host)
                        if self.enforce_numa_memory and numa_capacity["ready"] is not True:
                            stable_samples = 0
                            wait_event.update({
                                "event": "numa_capacity_wait",
                                "numa_capacity": numa_capacity,
                            })
                        else:
                            stable_samples += 1
                            if self.enforce_numa_memory and stable_samples < CAPACITY_STABLE_SAMPLES:
                                wait_event.update({
                                    "event": "numa_capacity_stabilizing",
                                    "numa_capacity": numa_capacity,
                                    "stable_samples": stable_samples,
                                    "required_stable_samples": CAPACITY_STABLE_SAMPLES,
                                })
                            else:
                                for slot_id in range(self.capacity):
                                    handle = (self.slots / f"slot-{slot_id}.lock").open("a+")
                                    try:
                                        fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                                    except BlockingIOError:
                                        handle.close()
                                        continue
                                    admitted = {
                                        "event": "capacity_admitted", "run_token": run_token,
                                        "queue_ticket": ticket.name, "slot_id": slot_id,
                                        "waited_seconds": round(time.monotonic() - started, 3),
                                        "vm_capacity": self.capacity, "host": host,
                                        "numa_capacity": numa_capacity,
                                    }
                                    try:
                                        append_capacity_event(evidence_path, admitted)
                                    except OSError as exc:
                                        fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
                                        handle.close()
                                        raise CapacityGateError("capacity_evidence_write_failed") from exc
                                    ticket.unlink(missing_ok=True)
                                    return VmLease(
                                        slot_id, self.capacity, run_token, handle,
                                        evidence_path, time.monotonic(), host,
                                    )
                    fcntl.flock(queue_lock.fileno(), fcntl.LOCK_UN)
                if time.monotonic() >= deadline:
                    raise CapacityGateError("capacity_wait_timeout")
                append_capacity_event(evidence_path, wait_event)
                time.sleep(self.poll_seconds)
        except Exception:
            ticket.unlink(missing_ok=True)
            raise


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(4 * 1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def vm_file_identity(path: Path) -> dict[str, Any]:
    metadata = path.stat()
    return {
        "path": str(path.resolve()),
        "device": metadata.st_dev,
        "inode": metadata.st_ino,
        "size": metadata.st_size,
        "mtime_ns": metadata.st_mtime_ns,
        "ctime_ns": metadata.st_ctime_ns,
    }


def verify_vm_image_digest(
    path: Path,
    expected_digest: str,
    *,
    cache_path: Path | None = None,
    lock_path: Path | None = None,
) -> str:
    """Verify the large pinned VM once without concurrent disk saturation."""
    cache_path = VM_DIGEST_CACHE if cache_path is None else cache_path
    lock_path = VM_DIGEST_CACHE_LOCK if lock_path is None else lock_path
    cache_path.parent.mkdir(parents=True, exist_ok=True)
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    with lock_path.open("a+", encoding="utf-8") as lock:
        fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
        try:
            before = vm_file_identity(path)
            cache: dict[str, Any] = {}
            if cache_path.is_file():
                try:
                    loaded = json.loads(cache_path.read_text(encoding="utf-8"))
                    if isinstance(loaded, dict):
                        cache = loaded
                except (OSError, ValueError):
                    cache = {}
            if (
                cache.get("schema_version") == "1.0"
                and cache.get("sha256") == expected_digest
                and cache.get("identity") == before
            ):
                return expected_digest
            actual_digest = sha256(path)
            after = vm_file_identity(path)
            if before != after:
                raise RuntimeError("vm_image_changed_during_digest")
            if actual_digest != expected_digest:
                raise RuntimeError("vm_image_digest_mismatch")
            payload = {
                "schema_version": "1.0",
                "sha256": actual_digest,
                "identity": after,
                "verified_at": time.time(),
                "verifier_pid": os.getpid(),
            }
            temporary = cache_path.with_name(
                f".{cache_path.name}.{os.getpid()}.{secrets.token_hex(6)}.tmp"
            )
            try:
                temporary.write_text(
                    json.dumps(payload, sort_keys=True, indent=2) + "\n",
                    encoding="utf-8",
                )
                os.replace(temporary, cache_path)
            finally:
                temporary.unlink(missing_ok=True)
            return actual_digest
        finally:
            fcntl.flock(lock.fileno(), fcntl.LOCK_UN)


def run(command: list[str], timeout: int = 60, env: dict[str, str] | None = None) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        command, text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        check=False, timeout=timeout, env=env,
    )


def free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def broker_stats(port: int, token: str) -> dict[str, Any]:
    request = urllib.request.Request(
        f"http://127.0.0.1:{port}/stats",
        headers={"Authorization": f"Bearer {token}"},
    )
    with urllib.request.urlopen(request, timeout=10) as response:
        value = json.loads(response.read())
    if not isinstance(value, dict):
        raise RuntimeError("broker_stats_invalid")
    return value


def wait_broker(port: int, container: str) -> None:
    deadline = time.monotonic() + 20
    while time.monotonic() < deadline:
        state = run(["docker", "inspect", "--format", "{{.State.Running}}", container])
        if state.returncode != 0 or state.stdout.strip() != "true":
            raise RuntimeError("broker_exited_before_ready")
        try:
            with urllib.request.urlopen(f"http://127.0.0.1:{port}/healthz", timeout=1) as response:
                if response.status == 200:
                    return
        except Exception:
            time.sleep(0.2)
    raise RuntimeError("broker_health_timeout")


def labelled_containers(run_token: str) -> list[str]:
    result = run([
        "docker", "ps", "-aq", "--filter", f"label={AUDIT_LABEL}={run_token}",
    ])
    if result.returncode != 0:
        raise RuntimeError("docker_cleanup_audit_failed")
    return [line for line in result.stdout.splitlines() if line.strip()]


def force_cleanup(run_token: str) -> tuple[bool, list[str]]:
    try:
        before = labelled_containers(run_token)
    except Exception:
        return False, []
    for container in before:
        try:
            run(["docker", "rm", "-f", "-v", container], timeout=60)
        except subprocess.TimeoutExpired:
            pass
    try:
        after = labelled_containers(run_token)
    except Exception:
        after = before
    overlay = PROVIDER_OVERLAY_ROOT / f"{run_token}.qcow2"
    try:
        overlay.unlink(missing_ok=True)
    except OSError:
        return False, before
    return not after and not overlay.exists(), before


def broker_settings_args() -> list[str]:
    """docker run arguments that carry the runner's OSWorld broker settings into the container.

    AGENTSWE_OSWORLD_SCHEMA_STRICT is always passed (unset means strict, as in the paper);
    AGENTSWE_OSWORLD_EFFORT only when it is set, so an unset effort keeps the broker's "high".
    """
    args = ["-e", f"AGENTSWE_OSWORLD_SCHEMA_STRICT={os.environ.get('AGENTSWE_OSWORLD_SCHEMA_STRICT', '1')}"]
    effort = os.environ.get("AGENTSWE_OSWORLD_EFFORT")
    if effort:
        args += ["-e", f"AGENTSWE_OSWORLD_EFFORT={effort}"]
    return args


def ensure_provider_image() -> None:
    inspect = run(["docker", "image", "inspect", PROVIDER_PULL_IMAGE])
    if inspect.returncode != 0:
        pull = run(["docker", "pull", PROVIDER_PULL_IMAGE], timeout=1800)
        if pull.returncode != 0:
            raise RuntimeError("provider_image_pull_failed")
        inspect = run(["docker", "image", "inspect", PROVIDER_PULL_IMAGE])
    if inspect.returncode != 0:
        raise RuntimeError("provider_image_missing")
    try:
        attrs = json.loads(inspect.stdout)[0]
    except (ValueError, IndexError, TypeError) as exc:
        raise RuntimeError("provider_image_inspect_invalid") from exc
    digests = attrs.get("RepoDigests") or []
    if not any(isinstance(value, str) and value.endswith(f"@{PROVIDER_DIGEST}") for value in digests):
        raise RuntimeError("provider_image_digest_unverified")


def manifest_asset_path(value: str, manifest: Path, key: str) -> Path:
    """Release: relative VM-manifest paths name files under the asset root; the provider entrypoint
    scripts are shipped next to the manifest."""
    path = Path(value)
    if path.is_absolute():
        return path
    if key == "entrypoint":
        return manifest.resolve().parent / path
    return OSWORLD_ASSETS / path


def validate_inputs(args: argparse.Namespace) -> tuple[dict[str, Any], str, dict[str, Path]]:
    if not (OSWORLD_SOURCE / "desktop_env/desktop_env.py").is_file():
        raise RuntimeError("osworld_source_missing")
    if not (BROKER_PACKAGE / "config.py").is_file():
        raise RuntimeError("broker_effort_module_missing")
    if sha256(args.task_metadata) != TASK_METADATA_DIGEST:
        raise RuntimeError("task_metadata_digest_mismatch")
    metadata = json.loads(args.task_metadata.read_text(encoding="utf-8"))
    record = metadata.get(args.task_id)
    if not isinstance(record, dict) or record.get("id") != args.task_id:
        raise RuntimeError("task_id_not_in_frozen_metadata")
    row = load_prediction(str(args.predictions), args.case_id)
    if not args.credential_file.is_file():
        raise RuntimeError("credential_file_missing")
    if args.proxy_config:
        if not args.proxy_config.is_file():
            raise RuntimeError("proxy_config_missing")
        mode = stat.S_IMODE(args.proxy_config.stat().st_mode)
        if mode & 0o077:
            raise RuntimeError("proxy_config_permissions_too_broad")
        proxy = json.loads(args.proxy_config.read_text(encoding="utf-8"))
        required = {"host", "port", "username", "password", "protocol"}
        if not isinstance(proxy, list) or not proxy or any(not isinstance(row, dict) or not required <= set(row) for row in proxy):
            raise RuntimeError("proxy_config_invalid")
    vm_manifest = json.loads(args.vm_manifest.read_text(encoding="utf-8"))
    vm_path = manifest_asset_path(vm_manifest.get("qcow2", {}).get("path", ""), args.vm_manifest, "qcow2")
    expected_digest = vm_manifest.get("qcow2", {}).get("sha256")
    if not vm_path.is_file() or not isinstance(expected_digest, str):
        raise RuntimeError("vm_manifest_invalid")
    if vm_path.stat().st_size != vm_manifest["qcow2"].get("size"):
        raise RuntimeError("vm_image_size_mismatch")
    verify_vm_image_digest(vm_path, expected_digest)
    boot_paths: dict[str, Path] = {}
    for key in ("kernel", "initrd", "entrypoint"):
        asset = vm_manifest.get("boot_assets", {}).get(key, {})
        path = manifest_asset_path(asset.get("path", ""), args.vm_manifest, key)
        if not path.is_file() or path.stat().st_size != asset.get("size"):
            raise RuntimeError(f"{key}_asset_invalid")
        if sha256(path) != asset.get("sha256"):
            raise RuntimeError(f"{key}_asset_digest_mismatch")
        boot_paths[key] = path
    return row, expected_digest, boot_paths


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--case-id", required=True)
    parser.add_argument("--task-id", required=True)
    parser.add_argument("--predictions", type=Path, required=True)
    parser.add_argument("--credential-file", type=Path, required=True)
    parser.add_argument("--task-metadata", type=Path, default=ROOT / "osworld_images/task_metadata.json")
    parser.add_argument("--vm-manifest", type=Path, required=True)
    parser.add_argument("--fixture-manifest", type=Path, default=ROOT / "osworld_images/fixture_manifest.json")
    parser.add_argument("--network-manifest", type=Path, default=ROOT / "osworld_images/network_manifest.json")
    parser.add_argument("--proxy-config", type=Path)
    parser.add_argument("--runtime-python", type=Path, default=Path(sys.executable))
    parser.add_argument("--output-dir", type=Path, required=True)
    args = parser.parse_args()
    args.output_dir.mkdir(parents=True, exist_ok=True)
    try:
        row, vm_digest, boot_paths = validate_inputs(args)
    except AgentSpecError as exc:
        write_json(args.output_dir / "native_result.json", ordinary_result(args.case_id, args.task_id, f"invalid_prediction:{exc}"))
        return 0
    except Exception as exc:
        write_json(args.output_dir / "native_result.json", infrastructure_result(args.case_id, args.task_id, str(exc)))
        return 0

    frozen = args.output_dir / "frozen_prediction.json"
    write_json(frozen, row)
    try:
        ensure_provider_image()
    except Exception as exc:
        write_json(args.output_dir / "native_result.json", infrastructure_result(args.case_id, args.task_id, str(exc)))
        return 0
    vm_manifest = json.loads(args.vm_manifest.read_text(encoding="utf-8"))
    vm_path = manifest_asset_path(vm_manifest["qcow2"]["path"], args.vm_manifest, "qcow2")
    attempts: list[dict[str, Any]] = []
    for attempt in range(1, MAX_ATTEMPTS + 1):
        run_token = hashlib.sha256(
            f"{args.case_id}:{args.task_id}:{attempt}:{secrets.token_hex(16)}".encode()
        ).hexdigest()[:24]
        runtime_token, stats_token = secrets.token_urlsafe(32), secrets.token_urlsafe(32)
        port = free_port()
        attempt_dir = args.output_dir / f"attempt_{attempt:03d}"
        attempt_dir.mkdir(parents=True, exist_ok=True)
        fixture_cache = attempt_dir / "fixture_cache"
        broker_stdout = (attempt_dir / "broker.stdout.log").open("w", encoding="utf-8")
        broker_stderr = (attempt_dir / "broker.stderr.log").open("w", encoding="utf-8")
        broker_name = f"agentswe-osworld-broker-{run_token}"
        broker_started = False
        runtime_result: subprocess.CompletedProcess[str] | None = None
        stats: dict[str, Any] | None = None
        cleanup_verified = False
        lease = None
        try:
            try:
                lease = VmCapacityGate().acquire(
                    run_token,
                    attempt_dir / "capacity_events.jsonl",
                )
                write_json(
                    attempt_dir / "capacity_lease.json",
                    {
                        "run_token": run_token,
                        "slot_id": lease.slot_id,
                        "vm_capacity": lease.capacity,
                        "host": lease.host,
                    },
                )
            except CapacityGateError as exc:
                attempts.append({"attempt": attempt, "error": str(exc), "run_token": run_token})
                break
            fixture_evidence = prepare_fixture_cache(args.fixture_manifest, args.task_id, fixture_cache)
            write_json(attempt_dir / "fixture_evidence.json", fixture_evidence)
            network_evidence = preflight_network(args.network_manifest, args.task_id)
            network_evidence["manifest_sha256"] = sha256(args.network_manifest)
            write_json(attempt_dir / "network_evidence.json", network_evidence)
            broker_run = run([
                "docker", "run", "-d", "--name", broker_name,
                "--label", f"{AUDIT_LABEL}={run_token}",
                "--label", "agentswe.osworld.role=vision-broker",
                "-p", f"127.0.0.1:{port}:8080",
                "-v", f"{PYTHON_ROOT}:/python:ro",
                "-v", "/etc/ssl/certs:/etc/ssl/certs:ro",
                "-v", f"{ROOT / 'osworld_broker.py'}:/broker.py:ro",
                "-v", f"{ROOT / 'osworld_agent.py'}:/osworld_agent.py:ro",
                "-v", f"{BROKER_PACKAGE}:/agentswe_broker:ro",
                "-v", f"{args.credential_file.resolve()}:/run/secrets/agentswe.env:ro",
                "-e", "PYTHONPATH=/",
                *broker_settings_args(),
                "-e", "SSL_CERT_FILE=/etc/ssl/certs/ca-certificates.crt",
                BROKER_IMAGE, "/python/bin/python3", "/broker.py",
                "--credential-file", "/run/secrets/agentswe.env",
                "--bind", "0.0.0.0", "--port", "8080",
                "--max-calls", "30", "--max-tokens", "100000",
                f"--runtime-token={runtime_token}", f"--stats-token={stats_token}",
            ])
            broker_stdout.write(broker_run.stdout)
            broker_stderr.write(broker_run.stderr)
            broker_stdout.flush()
            broker_stderr.flush()
            if broker_run.returncode != 0:
                raise RuntimeError("broker_start_failed")
            broker_started = True
            wait_broker(port, broker_name)
            command = [
                str(args.runtime_python), str(ROOT / "osworld_runtime.py"),
                "--case-id", args.case_id, "--task-id", args.task_id,
                "--task-metadata", str(args.task_metadata), "--prediction", str(frozen),
                "--vm-image", str(vm_path), "--vm-image-digest", vm_digest,
                "--kernel", str(boot_paths["kernel"]),
                "--initrd", str(boot_paths["initrd"]),
                "--provider-entrypoint", str(boot_paths["entrypoint"]),
                "--fixture-cache", str(fixture_cache),
                "--broker-url", f"http://127.0.0.1:{port}", f"--broker-token={runtime_token}",
                "--run-token", run_token, "--output-dir", str(attempt_dir),
            ]
            if args.proxy_config:
                command.extend(["--proxy-config", str(args.proxy_config)])
            runtime_result = run(
                command,
                timeout=2700,
                env={**os.environ, "PYTHONPATH": f"{ROOT}:{OSWORLD_SOURCE}"},
            )
            (attempt_dir / "runtime.stdout.log").write_text(runtime_result.stdout[-50_000:], encoding="utf-8")
            (attempt_dir / "runtime.stderr.log").write_text(runtime_result.stderr[-50_000:], encoding="utf-8")
            stats = broker_stats(port, stats_token)
        except Exception as exc:
            attempts.append({"attempt": attempt, "error": str(exc), "run_token": run_token})
        finally:
            if broker_started:
                exists = run(["docker", "inspect", broker_name])
                if exists.returncode == 0:
                    logs = run(["docker", "logs", "--tail", "500", broker_name], timeout=30)
                    broker_stdout.write(logs.stdout[-50_000:])
                    broker_stderr.write(logs.stderr[-50_000:])
            cleanup_verified, removed = force_cleanup(run_token)
            if lease is not None:
                lease.release()
            broker_stdout.close()
            broker_stderr.close()
            write_json(attempt_dir / "cleanup_evidence.json", {
                "run_token": run_token, "removed_containers": removed,
                "cleanup_verified": cleanup_verified,
            })
        result_path = attempt_dir / "native_result.json"
        if not cleanup_verified:
            attempts.append({"attempt": attempt, "error": "cleanup_proof_missing", "run_token": run_token})
            continue
        if runtime_result is None or runtime_result.returncode != 0 or not result_path.is_file():
            attempts.append({"attempt": attempt, "error": "runtime_process_failed", "run_token": run_token})
            continue
        result = json.loads(result_path.read_text(encoding="utf-8"))
        if not isinstance(stats, dict):
            attempts.append({"attempt": attempt, "error": "broker_stats_missing", "run_token": run_token})
            continue
        write_json(attempt_dir / "broker_stats.json", stats)
        if stats.get("failures", 0):
            attempts.append({"attempt": attempt, "error": "model_provider_failure", "broker_stats": stats})
            continue
        if result.get("official_evaluation") is not True or result.get("infrastructure_failure") is True:
            attempts.append({
                "attempt": attempt,
                "error": "runtime_infrastructure_failure",
                "runtime_errors": result.get("errors", []),
                "run_token": run_token,
            })
            continue
        result.update({
            "broker_stats": stats, "model_calls": stats.get("calls", 0),
            "model_tokens": stats.get("tokens", 0), "credential_brokered": True,
            "provider_attempts": attempts, "cleanup_verified": True,
            "source_commit": SOURCE_COMMIT,
            "fixture_manifest_sha256": sha256(args.fixture_manifest),
            "network_manifest_sha256": sha256(args.network_manifest),
        })
        if stats.get("budget_exceeded"):
            result = ordinary_result(
                args.case_id, args.task_id, "model_budget_exceeded",
                broker_stats=stats, provider_attempts=attempts, cleanup_verified=True,
            )
        write_json(args.output_dir / "native_result.json", result)
        return 0
    write_json(args.output_dir / "native_result.json", infrastructure_result(
        args.case_id, args.task_id, "osworld_provider_attempts_exhausted",
        provider_attempts=attempts, cleanup_verified=all(
            attempt.get("error") != "cleanup_proof_missing" for attempt in attempts
        ),
    ))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
