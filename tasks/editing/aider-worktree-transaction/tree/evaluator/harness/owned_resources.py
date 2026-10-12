"""Aider-owned systemd scope; public 4GiB aggregate memory and cleanup.

No global services, firewall, networks or unrelated units are changed. The
bootstrap blocks before task code until the evaluator verifies the actual unit
identity, description, cgroup and memory controller. Every control action is
bound to that verified random scope identity.
"""
# Adapted from Claude SHA256 fc934ad2caeb395c1b44802a38a246429d0795fc2130a1a38aef84664d1e05fd; originally OpenHands owned_resources.py SHA256 22898bebce67ef8cd7b59b1e80aa28bb245eb3c28be93a580df8962dfd8c5d79
from __future__ import annotations
import argparse
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import time
import uuid

MEMORY_BYTES = 4 * 1024 * 1024 * 1024
DEADLINE_SECONDS = 600
BUILD_DEADLINE_SECONDS = 1800
SUITE_DEADLINE_SECONDS = 4800
CLEAN_ENV = {"PATH": "/usr/bin:/bin", "LANG": "C.UTF-8"}


def _show(unit):
    result = subprocess.run(["systemctl", "show", unit, "--no-pager", "--property=Id,Description,LoadState,ActiveState,ControlGroup,MemoryMax,MemorySwapMax,RuntimeMaxUSec,TimeoutStopUSec,TasksMax"],
        env=CLEAN_ENV, cwd="/", capture_output=True, text=True, timeout=5)
    values = dict(line.split("=", 1) for line in result.stdout.splitlines() if "=" in line)
    if values.get("LoadState") == "not-found":
        return values
    if result.returncode:
        raise RuntimeError("cannot inspect owned scope")
    return values


def _duration_seconds(value):
    units = {'us': 1e-6, 'ms': .001, 's': 1, 'min': 60, 'h': 3600, 'd': 86400}
    compact = re.sub(r'\s+', '', str(value))
    parts = re.findall(r'(\d+(?:\.\d+)?)(us|ms|min|s|h|d)', compact)
    if not parts or ''.join(number + unit for number, unit in parts) != compact:
        raise RuntimeError('unverifiable systemd duration: ' + str(value))
    return sum(float(number) * units[unit] for number, unit in parts)


def verify_identity(actual, expected, *, require_resources=True):
    if not expected["unit"].startswith("agentswe-edit-aider-") or not expected["unit"].endswith(".scope"):
        raise RuntimeError("unrecognized scope owner prefix")
    if any(actual.get(key) != expected[value] for key, value in (("Id", "unit"), ("Description", "description"), ("ControlGroup", "cgroup"))):
        raise RuntimeError("owned scope identity mismatch; refusing any process control")
    cgroup = expected["cgroup"]
    parent = expected.get("aggregate_parent", {}).get("cgroup", "/system.slice")
    if cgroup != parent + "/" + expected["unit"]:
        raise RuntimeError("unexpected scope cgroup location")
    if require_resources:
        if actual.get("MemoryMax") != str(expected["memory_bytes"]) or actual.get("MemorySwapMax") != "0":
            raise RuntimeError("owned scope resource contract mismatch")
        if abs(_duration_seconds(actual.get('RuntimeMaxUSec')) - expected['timeout_seconds']) > .001:
            raise RuntimeError('independent scope deadline differs from case budget')
        if _duration_seconds(actual.get('TimeoutStopUSec')) != 3 or actual.get('TasksMax') != 'infinity':
            raise RuntimeError('scope cleanup grace or undocumented task-count cap differs')


def stop_owned(expected):
    actual = _show(expected["unit"])
    if actual.get("LoadState") == "not-found":
        root = Path("/sys/fs/cgroup") / expected["cgroup"].lstrip("/")
        events = (root / "cgroup.events").read_text() if root.exists() else "populated 0\n"
        if "populated 0" not in events:
            raise RuntimeError("unit absent but expected cgroup is populated; refusing unverified cleanup")
        return {"complete": True, "unit_absent": True, "cgroup_events": events,
                "identity_verified_before_stop": False}
    # A misconfigured but exactly owned unit must still be cleaned up. Resource
    # mismatch invalidates the run; it must not strand the owned subtree.
    verify_identity(actual, expected, require_resources=False)
    for command in (["systemctl", "kill", "--kill-who=all", "--signal=SIGKILL", expected["unit"]],
                    ["systemctl", "stop", expected["unit"]]):
        subprocess.run(command, cwd="/", env=CLEAN_ENV, capture_output=True, text=True, timeout=8)
    after = _show(expected["unit"])
    cgroup_root = Path("/sys/fs/cgroup") / expected["cgroup"].lstrip("/")
    events = (cgroup_root / "cgroup.events").read_text() if cgroup_root.exists() else "populated 0\n"
    complete = after.get("ActiveState") in (None, "inactive", "failed") and "populated 0" in events
    if not complete:
        raise RuntimeError("owned scope cleanup did not prove empty subtree")
    return {"complete": True, "unit_state": after.get("ActiveState"), "cgroup_events": events,
            "identity_verified_before_stop": True}



def _ambient_aggregate():
    """Discover the evaluator's real inherited cgroup; no environment hints."""
    current = next((line.split(":", 2)[2] for line in Path("/proc/self/cgroup").read_text().splitlines() if line.startswith("0::")), "")
    match = re.fullmatch(r"/(agentswe_aider_[0-9a-f]{32}\.slice)/agentswe-edit-aider-[0-9a-f]{32}\.scope", current)
    return match.group(1) if match else None


def verify_aggregate(parent):
    unit = parent["unit"]
    if not re.fullmatch(r"agentswe_aider_[0-9a-f]{32}\.slice", unit) or parent["cgroup"] != "/" + unit:
        raise RuntimeError("invalid aggregate owner identity")
    actual = _show(unit)
    if actual.get("Id") != unit or actual.get("ControlGroup") != parent["cgroup"]:
        raise RuntimeError("aggregate parent identity mismatch")
    if actual.get("MemoryMax") != str(parent["memory_bytes"]) or actual.get("MemorySwapMax") != "0":
        raise RuntimeError("aggregate parent memory contract mismatch")
    root = Path("/sys/fs/cgroup") / unit
    files = {name: (root / name).read_text().strip() for name in ("memory.max", "memory.swap.max", "memory.current", "memory.events", "cgroup.events")}
    if files["memory.max"] != str(parent["memory_bytes"]) or files["memory.swap.max"] != "0":
        raise RuntimeError("actual aggregate cgroup memory contract mismatch")
    if (root / "memory.peak").is_file(): files["memory.peak"] = (root / "memory.peak").read_text().strip()
    return {"systemd_properties": actual, "controller_files": files}


def stop_aggregate(parent):
    actual = _show(parent["unit"])
    root = Path("/sys/fs/cgroup") / parent["cgroup"].lstrip("/")
    if actual.get("LoadState") == "not-found":
        if root.exists() and "populated 0" not in (root / "cgroup.events").read_text():
            raise RuntimeError("aggregate unit absent but subtree populated")
        return {"complete": True, "unit_absent": True}
    if actual.get("Id") != parent["unit"] or actual.get("ControlGroup") != parent["cgroup"]:
        raise RuntimeError("aggregate cleanup identity mismatch")
    for command in (["systemctl", "kill", "--kill-who=all", "--signal=SIGKILL", parent["unit"]], ["systemctl", "stop", parent["unit"]]):
        subprocess.run(command, cwd="/", env=CLEAN_ENV, capture_output=True, text=True, timeout=8)
    events = (root / "cgroup.events").read_text() if root.exists() else "populated 0\n"
    if "populated 0" not in events: raise RuntimeError("aggregate owned subtree remains populated")
    return {"complete": True, "cgroup_events": events, "identity_verified_before_stop": True}

def run_owned(command, *, cwd, env, output: Path, timeout=DEADLINE_SECONDS, memory_bytes=MEMORY_BYTES, purpose='case'):
    """Return (CompletedProcess, resource attestation); unique output required.

    Timeout includes startup and all command-owned setup/model/product/artifact/
    observer work. Provider-free dependency preflight may be performed before
    this entry; callers must not grant a fresh per-stage 600 seconds inside it.
    """
    limit = {'case': DEADLINE_SECONDS, 'build': BUILD_DEADLINE_SECONDS,
             'suite': SUITE_DEADLINE_SECONDS}.get(purpose)
    if limit is None or not 0 < timeout <= limit or not 32 * 1024 * 1024 <= memory_bytes <= MEMORY_BYTES:
        raise ValueError("invalid Aider purpose/resource budget; case600s, build1800s, suite4800s, memory4GiB maxima")
    output = Path(output).resolve()
    output.mkdir(parents=True, exist_ok=False)
    token = uuid.uuid4().hex
    unit = "agentswe-edit-aider-" + token + ".scope"
    inherited = _ambient_aggregate()
    aggregate_unit = inherited or "agentswe_aider_" + token + ".slice"
    if inherited:
        actual_parent = _show(inherited)
        parent_memory = int(actual_parent.get("MemoryMax", "0"))
        if not 32 * 1024 * 1024 <= parent_memory <= MEMORY_BYTES:
            raise RuntimeError("invalid inherited aggregate memory cap")
    else:
        parent_memory = memory_bytes
    aggregate_parent = {"unit": aggregate_unit, "cgroup": "/" + aggregate_unit, "memory_bytes": parent_memory, "created_here": inherited is None}
    if inherited: verify_aggregate(aggregate_parent)
    expected = {"unit": unit, "description": "AgentSWE Aider " + purpose + " owner " + token,
                "cgroup": aggregate_parent["cgroup"] + "/" + unit, "memory_bytes": memory_bytes, "aggregate_parent": aggregate_parent,
                "timeout_seconds": timeout}
    (output / "scope-ownership.json").write_text(json.dumps(expected, indent=2) + "\n")
    ready, permit = output / "scope-ready.json", output / "scope-permit"
    started = time.monotonic()
    argv = ["systemd-run", "--scope", "--quiet", "--collect", "--unit=" + unit,
        "--slice=" + aggregate_unit, "--description=" + expected["description"], "--property=MemoryMax=" + str(memory_bytes),
        "--property=MemorySwapMax=0", "--property=TasksMax=infinity", "--property=RuntimeMaxSec=" + str(timeout) + "s",
        "--property=TimeoutStopSec=3s", "--",
        sys.executable, "-I", str(Path(__file__).resolve()), "--bootstrap", "--ready", str(ready),
        "--permit", str(permit), "--", *command]
    process = subprocess.Popen(argv, cwd=cwd, env=env, text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    attestation = {"schema_version": "agentswe-aider-owned-case-resources/v1", **expected,
        "timeout_seconds": timeout, "purpose": purpose, "valid": False, "timed_out": False,
        "configuration_delta": [{"id": "aider-public-memory-budget", "create_reference_bytes": 4294967296,
            "effective_bytes": memory_bytes, "public_task_max_bytes": MEMORY_BYTES,
            "accounting": "cgroup-v2 aggregate memory.current, not RSS or an asserted PSS sample",
            "reason": "Use the 4GiB Aider agentloop aggregate limit; cgroup memory is distinct from historical native PSS"}],
        "independent_scope_deadline_seconds": timeout,
        "independent_scope_stop_grace_seconds": 3,
        "budget_scope": "all command-owned setup/model/product/artifact/observer work", "cleanup": None}
    try:
        deadline = started + timeout
        while not ready.exists():
            if process.poll() is not None:
                raise RuntimeError("owned scope bootstrap failed before resource attestation")
            if time.monotonic() >= min(deadline, started + 10):
                raise TimeoutError("owned scope bootstrap did not become ready")
            time.sleep(.02)
        # bootstrap() now publishes by atomic rename, but a torn or not-yet-flushed
        # ready file must never be the difference between a scored round and
        # launcher_infrastructure_error: keep polling inside the same bootstrap
        # deadline until the JSON parses, and fail exactly as before if it never does.
        while True:
            try:
                observed = json.loads(ready.read_text())
                break
            except (OSError, UnicodeDecodeError, ValueError):
                if process.poll() is not None:
                    raise RuntimeError("owned scope bootstrap failed before resource attestation")
                if time.monotonic() >= min(deadline, started + 10):
                    raise TimeoutError("owned scope bootstrap did not become ready")
                time.sleep(.02)
        if not inherited:
            configured = subprocess.run(["systemctl", "set-property", "--runtime", aggregate_unit, "MemoryMax=" + str(parent_memory), "MemorySwapMax=0"],
                cwd="/", env=CLEAN_ENV, capture_output=True, text=True, timeout=5)
            if configured.returncode: raise RuntimeError("aggregate parent memory controller setup failed: " + configured.stderr[-500:])
        aggregate_observed = verify_aggregate(aggregate_parent)
        actual = _show(unit)
        verify_identity(actual, expected)
        if observed.get("cgroup") != expected["cgroup"] or observed.get("memory_max") != str(memory_bytes) or observed.get("memory_swap_max") != "0":
            raise RuntimeError("actual process cgroup does not match resource contract")
        attestation.update(valid=True, observed=observed, systemd_properties=actual, aggregate_observed=aggregate_observed,
            budget_scope="all nested suite/build/case processes share one verified aggregate parent memory cap")
        permit.write_text(token)
        try:
            stdout, stderr = process.communicate(timeout=max(.001, deadline - time.monotonic()))
        except subprocess.TimeoutExpired:
            attestation["timed_out"] = True
            attestation["cleanup"] = stop_owned(expected)
            stdout, stderr = process.communicate(timeout=8)
        returncode = 124 if attestation["timed_out"] else process.returncode
        attestation["aggregate_final_observed"] = verify_aggregate(aggregate_parent)
        attestation.update(process_exit_code=process.returncode, memory_failure_attribution="not_inferred_from_exit_code")
        return subprocess.CompletedProcess(command, returncode, stdout, stderr), attestation
    except Exception as exc:
        attestation["error"] = type(exc).__name__ + ": " + str(exc)
        raise
    finally:
        try:
            if attestation["cleanup"] is None:
                attestation["cleanup"] = stop_owned(expected)
        finally:
            if aggregate_parent["created_here"]:
                attestation["aggregate_cleanup"] = stop_aggregate(aggregate_parent)
            if process.poll() is None:
                process.terminate()
                try:
                    process.wait(timeout=3)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait(timeout=3)
            attestation["elapsed_seconds"] = round(time.monotonic() - started, 3)
            (output / "resource-attestation.json").write_text(json.dumps(attestation, indent=2) + "\n")


def bootstrap():
    parser = argparse.ArgumentParser()
    parser.add_argument("--bootstrap", action="store_true")
    parser.add_argument("--ready", type=Path, required=True)
    parser.add_argument("--permit", type=Path, required=True)
    parser.add_argument("command", nargs=argparse.REMAINDER)
    options = parser.parse_args()
    cgroup = next(line.split(":", 2)[2] for line in Path("/proc/self/cgroup").read_text().splitlines() if line.startswith("0::"))
    root = Path("/sys/fs/cgroup") / cgroup.lstrip("/")
    # Publish atomically.  The evaluator polls for this path's existence and
    # parses it immediately, so a create-then-write lets it read an empty file
    # and abort an otherwise healthy case with
    # "JSONDecodeError: Expecting value: line 1 column 1 (char 0)".
    _ready_partial = options.ready.with_name(options.ready.name + ".partial")
    _ready_partial.write_text(json.dumps({"pid": os.getpid(), "cgroup": cgroup,
        "memory_max": (root / "memory.max").read_text().strip(), "memory_swap_max": (root / "memory.swap.max").read_text().strip()}))
    os.replace(_ready_partial, options.ready)
    end = time.monotonic() + 15
    while not options.permit.exists():
        if time.monotonic() >= end:
            return 78
        time.sleep(.02)
    command = options.command[1:] if options.command[:1] == ["--"] else options.command
    os.execvpe(command[0], command, os.environ)


if __name__ == "__main__":
    raise SystemExit(bootstrap())
