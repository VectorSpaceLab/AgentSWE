"""Run/case-owned systemd scope; aggregate memory and complete subtree cleanup.

No global services, firewall, networks or unrelated units are changed. The
bootstrap blocks before task code until the evaluator verifies the actual unit
identity, description, cgroup and memory controller. Every control action is
bound to that verified random scope identity.
"""
from __future__ import annotations
import argparse
import json
import os
from pathlib import Path
import subprocess
import sys
import time
import uuid

MEMORY_BYTES = 4096 * 1024 * 1024
DEADLINE_SECONDS = 600
CLEAN_ENV = {"PATH": "/usr/bin:/bin", "LANG": "C.UTF-8"}


def _show(unit):
    result = subprocess.run(["systemctl", "show", unit, "--no-pager", "--property=Id,Description,LoadState,ActiveState,ControlGroup,MemoryMax,MemorySwapMax"],
        env=CLEAN_ENV, cwd="/", capture_output=True, text=True, timeout=5)
    values = dict(line.split("=", 1) for line in result.stdout.splitlines() if "=" in line)
    if values.get("LoadState") == "not-found":
        return values
    if result.returncode:
        raise RuntimeError("cannot inspect owned scope")
    return values


def verify_identity(actual, expected):
    if not expected["unit"].startswith("agentswe-edit-owner-c-") or not expected["unit"].endswith(".scope"):
        raise RuntimeError("unrecognized scope owner prefix")
    if any(actual.get(key) != expected[value] for key, value in (("Id", "unit"), ("Description", "description"), ("ControlGroup", "cgroup"))):
        raise RuntimeError("owned scope identity mismatch; refusing any process control")
    if actual.get("MemoryMax") != str(expected["memory_bytes"]) or actual.get("MemorySwapMax") != "0":
        raise RuntimeError("owned scope resource contract mismatch")
    cgroup = expected["cgroup"]
    if cgroup != "/system.slice/" + expected["unit"]:
        raise RuntimeError("unexpected scope cgroup location")


def stop_owned(expected):
    actual = _show(expected["unit"])
    if actual.get("LoadState") == "not-found":
        root = Path("/sys/fs/cgroup") / expected["cgroup"].lstrip("/")
        events = (root / "cgroup.events").read_text() if root.exists() else "populated 0\n"
        if "populated 0" not in events:
            raise RuntimeError("unit absent but expected cgroup is populated; refusing unverified cleanup")
        return {"complete": True, "unit_absent": True, "cgroup_events": events,
                "identity_verified_before_stop": False}
    verify_identity(actual, expected)
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


def run_owned(command, *, cwd, env, output: Path, timeout=DEADLINE_SECONDS, memory_bytes=MEMORY_BYTES):
    """Return (CompletedProcess, resource attestation); unique output required.

    Timeout includes startup and all command-owned setup/model/product/artifact/
    observer work. Provider-free dependency preflight may be performed before
    this entry; callers must not grant a fresh per-stage 600 seconds inside it.
    """
    if not 0 < timeout <= DEADLINE_SECONDS or not 32 * 1024 * 1024 <= memory_bytes <= MEMORY_BYTES:
        raise ValueError("resource contract may not exceed 600 seconds / 4096 MiB")
    output = Path(output).resolve()
    output.mkdir(parents=True, exist_ok=False)
    token = uuid.uuid4().hex
    unit = "agentswe-edit-owner-c-" + token + ".scope"
    expected = {"unit": unit, "description": "AgentSWE case owner " + token,
                "cgroup": "/system.slice/" + unit, "memory_bytes": memory_bytes}
    (output / "scope-ownership.json").write_text(json.dumps(expected, indent=2) + "\n")
    ready, permit = output / "scope-ready.json", output / "scope-permit"
    started = time.monotonic()
    cleanup_reserve = min(10.0, max(.25, timeout * .1))
    work_timeout = max(.001, timeout - cleanup_reserve)
    argv = ["systemd-run", "--scope", "--quiet", "--collect", "--unit=" + unit,
        "--description=" + expected["description"], "--property=MemoryMax=" + str(memory_bytes),
        "--property=MemorySwapMax=0", "--property=TasksMax=512", "--property=RuntimeMaxSec=" + str(work_timeout) + "s",
        "--property=TimeoutStopSec=3s", "--",
        sys.executable, "-I", str(Path(__file__).resolve()), "--bootstrap", "--ready", str(ready),
        "--permit", str(permit), "--", *command]
    process = subprocess.Popen(argv, cwd=cwd, env=env, text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    attestation = {"schema_version": "agentswe-owned-case-resources/v1", **expected,
        "timeout_seconds": timeout, "valid": False, "timed_out": False, "configuration_delta": [],
        "independent_scope_deadline_seconds": work_timeout,
        "cleanup_reserve_seconds": cleanup_reserve,
        "independent_scope_stop_grace_seconds": 3,
        "budget_scope": "all command-owned setup/model/product/artifact/observer work", "cleanup": None}
    try:
        deadline = started + work_timeout
        while not ready.exists():
            if process.poll() is not None:
                raise RuntimeError("owned scope bootstrap failed before resource attestation")
            if time.monotonic() >= min(deadline, started + 10):
                raise TimeoutError("owned scope bootstrap did not become ready")
            time.sleep(.02)
        observed = json.loads(ready.read_text())
        actual = _show(unit)
        verify_identity(actual, expected)
        if observed.get("cgroup") != expected["cgroup"] or observed.get("memory_max") != str(memory_bytes) or observed.get("memory_swap_max") != "0":
            raise RuntimeError("actual process cgroup does not match resource contract")
        attestation.update(valid=True, observed=observed, systemd_properties=actual)
        permit.write_text(token)
        try:
            stdout, stderr = process.communicate(timeout=max(.001, deadline - time.monotonic()))
        except subprocess.TimeoutExpired:
            attestation["timed_out"] = True
            attestation["cleanup"] = stop_owned(expected)
            stdout, stderr = process.communicate(timeout=8)
        returncode = 124 if attestation["timed_out"] else process.returncode
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
    options.ready.write_text(json.dumps({"pid": os.getpid(), "cgroup": cgroup,
        "memory_max": (root / "memory.max").read_text().strip(), "memory_swap_max": (root / "memory.swap.max").read_text().strip()}))
    end = time.monotonic() + 15
    while not options.permit.exists():
        if time.monotonic() >= end:
            return 78
        time.sleep(.02)
    command = options.command[1:] if options.command[:1] == ["--"] else options.command
    os.execvpe(command[0], command, os.environ)


if __name__ == "__main__":
    raise SystemExit(bootstrap())
