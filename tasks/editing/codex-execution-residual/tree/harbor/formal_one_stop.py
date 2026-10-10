#!/usr/bin/env python3
"""Formal Builder -> repeated dev feedback -> freeze -> six hidden.

The Builder is a single Harbor Codex session using deepseek-flash/xhigh. It may
submit up to ``max_dev_rounds`` distinct Edit deliveries (at most five), with
authoritative feedback from real lower Codex agents running both public dev
cases after every accepted submission. A dev mean above 60 is feedback only,
not an automatic freeze condition. After freeze, the formal runner executes
all six hidden cases.
"""
from __future__ import annotations

import argparse
import hashlib
import importlib.util
import ipaddress
import json
import os
import shutil
import signal
import socket
import socketserver
import subprocess
import sys
import tempfile
import threading
import time
import traceback
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

JUDGE_BROKER_SCRIPT = Path("@@AGENTSWE_EDITING_CONTROL@@/judge_broker_xhigh.py")


def _judge_runtime():
    """Evaluator-only runtime; never used for Builder or lower roles."""
    import sys
    if str(JUDGE_BROKER_SCRIPT.parent) not in sys.path:
        sys.path.insert(0, str(JUDGE_BROKER_SCRIPT.parent))
    import judge_broker_runtime
    return judge_broker_runtime

from typing import Any, Callable

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))
from build_preflight import (BuildInfrastructureError, baseline_build, build_environment,
                             check_build_environment, freeze_binary,
                             infrastructure_build_error, toolchain_identity, file_hash, isolated_build_command,
                             private_candidate_target)
from public_feedback import public_payload, public_text, semantic_feedback
from native_builder_evidence import NativeEvidenceError, observe_thread, verify_native
from product_attempts import ProductAttempt, ProductReplayError, product_source_digest


MODEL = "deepseek-flash"
BUILDER_MODEL = "deepseek-flash"  # upper Builder (Codex harness) only
BUILDER_EFFORT = "max"
BUILDER_BROKER_SCRIPT = Path("@@AGENTSWE_EDITING_CONTROL@@/builder_broker_xhigh.py")
LOWER_EFFORT = "high"
CONTROLLER_SOCKET_IN_CONTAINER = "/run/agentswe-controller/edit-agentloop.sock"
BUILDER_IMAGE = "agentswe/create-agent-builder-codex:0.144.1-node24.6.0-0812"
# 5 h Builder cap (D2, 2026-09-19).  The shared driver
# @@AGENTSWE_EDITING_CONTROL@@/formal_commands.py passes
# --builder-timeout 18000, so the generated Harbor task.toml has to stop the
# native Builder BEFORE the outer wait SIGTERMs it; otherwise the rollout
# stream never records a terminal event and the whole run is unusable.
GENERATED_BUILDER_TASK_TIMEOUT_SECONDS = 17_880
BUILDER_CLEANUP_MARGIN_SECONDS = 120
DEFAULT_OUTER_BUILDER_TIMEOUT_SECONDS = GENERATED_BUILDER_TASK_TIMEOUT_SECONDS + BUILDER_CLEANUP_MARGIN_SECONDS
# The pilot/readiness branch keeps its historical 8 h generated cap: readiness
# passes --builder-timeout 28800 explicitly and its two-round canary approaches
# neither bound, so this leaves readiness behaviour unchanged.
PILOT_BUILDER_TASK_TIMEOUT_SECONDS = 28_800
# run_harbor turns an outer-deadline SIGTERM into 124 and a KeyboardInterrupt
# into 130; run_configured_builder overrides either to 125 when resources,
# cleanup or the Harbor trial are invalid.  Only 124 is a survivable interrupt.
BUILDER_INTERRUPT_EXIT_CODES = (124,)


def builder_timeout_contract(effective_outer_timeout: int) -> dict[str, Any]:
    """Prove the outer deadline covers the generated task and cleanup margin."""
    if isinstance(effective_outer_timeout, bool) or effective_outer_timeout <= 0:
        raise ValueError("effective outer Builder timeout must be a positive integer")
    required = GENERATED_BUILDER_TASK_TIMEOUT_SECONDS + BUILDER_CLEANUP_MARGIN_SECONDS
    return {
        "generated_task_timeout_seconds": GENERATED_BUILDER_TASK_TIMEOUT_SECONDS,
        "cleanup_margin_seconds": BUILDER_CLEANUP_MARGIN_SECONDS,
        "effective_outer_timeout_seconds": int(effective_outer_timeout),
        "covers_task_plus_cleanup": int(effective_outer_timeout) >= required,
        "required_outer_timeout_seconds": required,
    }


def now() -> str:
    return datetime.now(timezone.utc).isoformat()



def prune_superseded_build_caches(root, keep):
    """Keep the newest failed attempt whole; drop the build caches under older ones.

    Each archived attempt holds its own Rust debug target tree -- 18-22 GiB of
    binaries and incremental dep-graphs. One run archived twelve of them, 258
    GiB, and none of it is exported: readiness_bundle.py reads neither
    build_cache nor infrastructure_attempts. The newest attempt is the one a
    diagnosis opens, so it stays intact; older attempts keep everything that
    identifies them and record what was removed instead of just losing it.
    """
    if not root.is_dir():
        return
    for attempt in sorted(root.iterdir()):
        if attempt == keep or not attempt.is_dir():
            continue
        for cache in sorted(attempt.rglob("build_cache")):
            if not cache.is_dir() or cache.is_symlink():
                continue
            freed = sum(f.stat().st_size for f in cache.rglob("*") if f.is_file())
            shutil.rmtree(cache, ignore_errors=True)
            write_json(attempt / "build_cache_pruned.json", {
                "removed": str(cache.relative_to(attempt)),
                "bytes_freed": freed,
                "reason": "superseded by a later infrastructure attempt; build caches are "
                          "not read by the readiness bundle export",
            })

def write_json(path: Path, value: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(value, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    os.replace(temporary, path)


def read_json(path: Path) -> dict[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError(f"expected JSON object: {path}")
    return value


def tree_digest(root: Path) -> str:
    hasher = hashlib.sha256()
    for path in sorted(root.rglob("*"), key=lambda item: str(item.relative_to(root))):
        relative = str(path.relative_to(root)).encode()
        hasher.update(len(relative).to_bytes(8, "big")); hasher.update(relative)
        if path.is_symlink():
            payload, kind = os.readlink(path).encode(), b"L"
        elif path.is_file():
            payload, kind = path.read_bytes(), b"F"
        else:
            payload, kind = b"", b"D"
        hasher.update(kind); hasher.update(len(payload).to_bytes(8, "big")); hasher.update(payload)
    return hasher.hexdigest()


def validate_delivery(root: Path) -> list[str]:
    required = {"solution.patch", "edit_report.json", "run_report.json"}
    if not root.is_dir():
        return ["submission directory missing"]
    names = {path.name for path in root.iterdir()}
    if root.is_symlink() or any(path.is_symlink() or not path.is_file() for path in root.iterdir()):
        return ["delivery requires three regular files without symlinks"]
    errors = [f"missing {name}" for name in sorted(required - names)]
    errors += [f"unexpected top-level artifact {name}" for name in sorted(names - required)]
    patch = root / "solution.patch"
    if patch.is_file() and patch.stat().st_size == 0:
        errors.append("solution.patch is empty")
    for name in ("edit_report.json", "run_report.json"):
        path = root / name
        if path.is_file():
            try:
                if not isinstance(json.loads(path.read_text(encoding="utf-8")), dict):
                    errors.append(f"{name} is not a JSON object")
            except Exception as exc:
                errors.append(f"{name} is invalid JSON: {type(exc).__name__}")
    return errors


def allocate_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as listener:
        listener.bind(("127.0.0.1", 0))
        return int(listener.getsockname()[1])


def prepare_run_dir(run_dir: Path) -> None:
    """Create a new run directory or accept an existing empty directory."""
    if run_dir.exists():
        if not run_dir.is_dir():
            raise RuntimeError(f"run directory is not a directory: {run_dir}")
        if any(run_dir.iterdir()):
            raise RuntimeError(f"run directory must be new and empty: {run_dir}")
        return
    run_dir.mkdir(parents=True)


def read_container_id(cidfile: Path) -> str | None:
    try:
        value = cidfile.read_text(encoding="ascii").strip()
    except OSError:
        return None
    return value or None


def start_broker(*, port: int, script: Path, image: str, credential: Path, name: str, cidfile: Path) -> None:
    if script.resolve() == Path('@@AGENTSWE_EDITING_CONTROL@@/builder_broker_xhigh.py'):
        sys.path.insert(0, '@@AGENTSWE_EDITING_CONTROL@@')
        import builder_broker_runtime
        return builder_broker_runtime.start_builder_broker(name=name, credential=credential,
            image=image, port=port, cidfile=cidfile)
    if script.resolve() == Path(__file__).resolve().parents[1] / 'evaluator/broker/lower_responses_broker.py':
        sys.path.insert(0, str(script.resolve().parent))
        import lower_broker_runtime
        return lower_broker_runtime.start_lower_broker(name=name, credential=credential,
            image=image, port=port, cidfile=cidfile)
    if script.resolve() == JUDGE_BROKER_SCRIPT:
        return _judge_runtime().start_judge_broker(name=name, credential=credential, image=image,
            port=port, cidfile=cidfile)
    cidfile.parent.mkdir(parents=True, exist_ok=True)
    cidfile.unlink(missing_ok=True)
    completed = subprocess.run([
        "docker", "run", "-d", "--rm", "--name", name, "--network", "host",
        "-v", f"{script}:/broker.py:ro", "-v", f"{credential}:/run/secrets/agentswe.env:ro",
        "--cidfile", str(cidfile.resolve()),
        "-v", "/etc/ssl/certs:/etc/ssl/certs:ro", "-e", "SSL_CERT_FILE=/etc/ssl/certs/ca-certificates.crt",
        image, "python3", "/broker.py", "--credential-file", "/run/secrets/agentswe.env",
        "--bind", "127.0.0.1", "--port", str(port), "--max-runtime-calls", "0", "--max-runtime-tokens", "0",
    ], text=True, capture_output=True, check=False)
    if completed.returncode:
        raise RuntimeError("lower-agent broker start failed: " + completed.stderr[-1200:])
    health = f"http://127.0.0.1:{port}/healthz"; last = ""
    for _ in range(40):
        try:
            with urllib.request.urlopen(health, timeout=2) as response:
                if response.status == 200:
                    return
        except Exception as exc:
            last = f"{type(exc).__name__}: {exc}"
        time.sleep(0.5)
    raise RuntimeError("lower-agent broker health check failed: " + last)


# Docker 29 removes an exited or stopped --rm container asynchronously: `docker rm -f` then answers
# "removal of container ... is already in progress" and `docker inspect` still shows it for a few seconds.
REMOVAL_WAIT_SECONDS = 30
REMOVAL_POLL_SECONDS = 0.5


def removal_in_progress(removed: subprocess.CompletedProcess) -> bool:
    return removed.returncode != 0 and "already in progress" in ((removed.stdout or "") + (removed.stderr or "")).lower()


def await_daemon_removal(container_id: str) -> None:
    """Wait (bounded) until the daemon has finished removing a container; absence is judged afterwards."""
    wait_until = time.monotonic() + REMOVAL_WAIT_SECONDS
    while time.monotonic() < wait_until:
        try:
            if subprocess.run(["docker", "inspect", container_id], text=True, capture_output=True,
                              check=False).returncode:
                return
        except (OSError, subprocess.SubprocessError):
            return
        time.sleep(REMOVAL_POLL_SECONDS)


def cleanup_broker(*, role: str, name: str, port: int, attempted: bool, run_dir: Path, cidfile: Path) -> dict[str, Any]:
    stats_saved = False
    stats_error = None
    if not attempted:
        return {
            "role": role,
            "name": name,
            "startup_attempted": False,
            "stats_saved": False,
            "stats_error": None,
            "remove_exit_code": None,
            "remove_error": None,
            "absent_after_cleanup": True,
            "ownership_proven": False,
        }
    container_id = read_container_id(cidfile)
    if container_id is None:
        return {
            "role": role, "name": name, "cidfile": str(cidfile),
            "startup_attempted": True, "ownership_proven": False,
            "stats_saved": stats_saved, "stats_error": stats_error,
            "remove_exit_code": None, "remove_error": "Docker startup was attempted but no current-run container ID was captured",
            "absent_after_cleanup": False,
        }
    try:
        request = urllib.request.Request(
            f"http://127.0.0.1:{port}/stats",
            headers={"Authorization": "Bearer stats-only-placeholder"},
        )
        with urllib.request.urlopen(request, timeout=10) as response:
            write_json(run_dir / f"{role}_broker_stats.json", json.loads(response.read()))
        stats_saved = True
    except Exception as exc:
        stats_error = f"{type(exc).__name__}: {exc}"
        write_json(run_dir / f"{role}_broker_stats_error.json", {"error": stats_error})
    remove_exit_code = None
    remove_error = None
    in_progress = False
    remove_stderr = None
    try:
        removed = subprocess.run(
            ["docker", "rm", "-f", container_id],
            text=True, capture_output=True,
            check=False,
        )
        remove_exit_code = removed.returncode
        remove_stderr = (removed.stderr or "")[-500:] if removed.returncode else None
        in_progress = removal_in_progress(removed)
        if in_progress:
            await_daemon_removal(container_id)
    except OSError as exc:
        remove_error = f"{type(exc).__name__}: {exc}"
    try:
        inspected = subprocess.run(
            ["docker", "inspect", container_id],
            text=True, capture_output=True,
            check=False,
        )
        inspect_text = ((inspected.stdout or "") + (inspected.stderr or "")).strip()
        inspect_lower = inspect_text.lower()
        absent_after_cleanup = inspected.returncode != 0 and (
            "no such object" in inspect_lower or "no such container" in inspect_lower
        )
        if not absent_after_cleanup:
            remove_error = remove_error or inspect_text[-1000:] or "docker inspect did not prove absence"
    except OSError as exc:
        remove_error = remove_error or f"{type(exc).__name__}: {exc}"
        absent_after_cleanup = False
    return {
        "role": role,
        "name": name,
        "cidfile": str(cidfile),
        "container_id": container_id,
        "ownership_proven": True,
        "startup_attempted": attempted,
        "stats_saved": stats_saved,
        "stats_error": stats_error,
        "remove_exit_code": remove_exit_code,
        "remove_error": remove_error,
        **({"remove_stderr": remove_stderr} if remove_stderr is not None else {}),
        **({"removal_in_progress_at_rm": True} if in_progress else {}),
        "absent_after_cleanup": absent_after_cleanup,
    }


def select_compose_subnet(
    *, run_dir: Path, existing_subnets: list[str] | None = None,
    host_subnets: list[str] | None = None,
) -> str:
    """Choose a deterministic non-overlapping RFC 2544 /24 for Harbor Compose."""
    if existing_subnets is None:
        existing_subnets = []
        try:
            listed = subprocess.run(["docker", "network", "ls", "-q"], text=True,
                                    capture_output=True, check=False)
            for network_id in listed.stdout.splitlines():
                if not network_id.strip():
                    continue
                inspected = subprocess.run(["docker", "network", "inspect", network_id.strip()],
                                           text=True, capture_output=True, check=False)
                if inspected.returncode != 0:
                    continue
                try:
                    values = json.loads(inspected.stdout)
                    configs = values[0].get("IPAM", {}).get("Config") or []
                    existing_subnets.extend(
                        str(item["Subnet"]) for item in configs
                        if isinstance(item, dict) and item.get("Subnet")
                    )
                except (TypeError, ValueError, KeyError, IndexError, json.JSONDecodeError):
                    continue
        except OSError:
            pass
    if host_subnets is None:
        host_subnets = []
        try:
            routes = subprocess.run(["ip", "-o", "route", "show"], text=True,
                                    capture_output=True, check=False)
            for token in routes.stdout.split():
                if "/" not in token or token == "src":
                    continue
                try:
                    ipaddress.ip_network(token, strict=False)
                except ValueError:
                    continue
                host_subnets.append(token)
        except OSError:
            pass
    blocked = []
    for raw in [*existing_subnets, *host_subnets]:
        try:
            blocked.append(ipaddress.ip_network(raw, strict=False))
        except ValueError:
            continue
    seed = int(hashlib.sha256(str(run_dir.resolve()).encode()).hexdigest()[:8], 16)
    candidates = [
        ipaddress.ip_network(f"198.{18 + (index // 256)}.{index % 256}.0/24")
        for index in range(512)
    ]
    for offset in range(len(candidates)):
        candidate = candidates[(seed + offset) % len(candidates)]
        if not any(candidate.overlaps(item) for item in blocked):
            return str(candidate)
    raise RuntimeError("no non-overlapping run-local Compose subnet is available")


def changed_paths(patch: Path) -> list[str]:
    paths: list[str] = []
    for line in patch.read_text(encoding="utf-8", errors="strict").splitlines():
        if line.startswith("diff --git a/") and " b/" in line:
            paths.append(line.split(" b/", 1)[1])
        elif line.startswith("+++ ") and not line.startswith("+++ /dev/null"):
            # A valid repository-relative unified patch may be produced by
            # `diff -ruN` rather than `git diff`, and its destination prefix is
            # whatever the second directory was called -- `b/`, `d/`, anything.
            # Both `git apply --check` and `git apply` run below with the default
            # -p1, which strips one leading component regardless of its name, so
            # extract the same path the applier will use instead of one spelling
            # of it. Strip the optional timestamp first.
            target = line[4:].split("\t", 1)[0]
            if "/" in target:
                paths.append(target.split("/", 1)[1])
    return sorted(set(paths))


def build_candidate(
    *, benchmark: Path, snapshot: Path, output: Path, runtime: Path,
    shared_target: Path,
) -> tuple[Path | None, dict[str, Any]]:
    if output.exists() and any(output.iterdir()):
        raise BuildInfrastructureError('Candidate build output must be new; prior evidence is immutable')
    output.mkdir(parents=True, exist_ok=True)
    baseline_target = shared_target
    shared_target = private_candidate_target(baseline_target, output)
    preflight = check_build_environment(runtime, shared_target)
    toolchain = toolchain_identity(runtime, shared_target)
    baseline_digest = tree_digest(benchmark / 'input/repository')
    worktree = output / "worktree"
    shutil.copytree(benchmark / "input/repository", worktree, symlinks=True)
    patch = snapshot / "solution.patch"; paths = changed_paths(patch)
    detail: dict[str, Any] = {"changed_paths": paths, "candidate_digest": tree_digest(snapshot), "preflight": preflight,
                              "cache_isolation": {"baseline_seed": str(baseline_target.resolve()),
                                  "private_target": str(shared_target.resolve()), "shared_writable_cache": False,
                                  "copy_method": "cp -a --reflink=auto; no hard links"},
                              "evidence_path": str(output / "build_result.json")}
    if not paths or any(path.startswith("/") or ".." in Path(path).parts for path in paths):
        detail["candidate_build_failure"] = "unsafe or empty patch paths"
        write_json(output / "build_result.json", detail)
        return None, detail
    env = build_environment(runtime, shared_target)
    for label, command, cwd, timeout in (
        ("git_init", ["git", "init", "-q"], worktree, 30),
        ("patch_check", ["git", "apply", "--check", str(patch)], worktree, 60),
        ("patch_apply", ["git", "apply", str(patch)], worktree, 60),
        ("cargo_build", [str(runtime / "bin/cargo"), "build", "--offline", "-p", "codex-cli", "--bin", "codex"], worktree / "codex-rs", 1800),
    ):
        try:
            if label == 'cargo_build':
                detail['candidate_repo_digest'] = tree_digest(worktree)
                detail['product_source_digest'] = product_source_digest(worktree)
                detail['product_source_digest_stage'] = 'after_patch_apply_before_compilation'
                command = isolated_build_command(command, runtime=runtime, target=shared_target, worktree=worktree)
                detail['candidate_build_isolation'] = {'mechanism': 'bwrap --unshare-all',
                    'command': command, 'network_access': False, 'evaluator_private_filesystem_mounted': False,
                    'only_writable_inputs': [str(worktree), str(shared_target), str(shared_target.parent / 'isolated-cargo-home')]}
            completed = subprocess.run(command, cwd=cwd, env=env, text=True, capture_output=True, timeout=timeout, check=False)
        except (OSError, subprocess.TimeoutExpired) as exc:
            detail.update({"classification": "evaluator_infrastructure_failure", "stage": label, "error": str(exc)[-1200:]})
            write_json(output / "build_result.json", detail)
            raise BuildInfrastructureError(f"{label}: {exc}") from exc
        detail[label] = {"exit_code": completed.returncode, "stderr_tail": completed.stderr[-3000:]}
        (output / f'{label}.stdout.log').write_text(completed.stdout, encoding='utf-8')
        (output / f'{label}.stderr.log').write_text(completed.stderr, encoding='utf-8')
        if completed.returncode:
            if infrastructure_build_error(label, completed.stderr):
                detail.update({"classification": "evaluator_infrastructure_failure", "stage": label})
                write_json(output / "build_result.json", detail)
                raise BuildInfrastructureError(f"{label}: {completed.stderr[-1200:]}")
            detail["candidate_build_failure"] = label
            detail["classification"] = "candidate_build_failure"
            write_json(output / "build_result.json", detail)
            return None, detail
    binary = shared_target / "debug/codex"
    if not binary.is_file():
        raise BuildInfrastructureError("cargo succeeded but expected executable is missing")
    binary = freeze_binary(binary, output)
    detail["binary"] = str(binary); detail["binary_sha256"] = hashlib.sha256(binary.read_bytes()).hexdigest()
    if toolchain_identity(runtime, shared_target)['sha256'] != toolchain['sha256']:
        raise BuildInfrastructureError('toolchain changed during Candidate compilation')
    if tree_digest(benchmark / 'input/repository') != baseline_digest or tree_digest(snapshot) != detail['candidate_digest']:
        raise BuildInfrastructureError('build input changed during Candidate compilation')
    detail['immutable_build_binding'] = {'schema': 'agentswe-codex-prebuilt/v1',
        'candidate_digest': detail['candidate_digest'], 'baseline_source_digest': baseline_digest,
        'cargo_lock_sha256': file_hash(worktree / 'codex-rs/Cargo.lock'),
        'solution_patch_sha256': file_hash(patch), 'binary_sha256': detail['binary_sha256'],
        'toolchain': toolchain, 'measured_before_and_after_build': True}
    write_json(output / "build_result.json", detail)
    return binary, detail


def reuse_prebuilt_build(*, build_run: Path, benchmark: Path, snapshot: Path,
                        output: Path, runtime: Path) -> tuple[Path, dict[str, Any]]:
    """Fail closed for old receipts; never modify or re-label historical builds."""
    proof_path = build_run / 'candidate_build/build_result.json'
    proof = read_json(proof_path)
    binding = proof.get('immutable_build_binding') or {}
    if binding.get('schema') != 'agentswe-codex-prebuilt/v1' or binding.get('measured_before_and_after_build') is not True:
        raise BuildInfrastructureError('prebuilt receipt lacks contemporaneous toolchain identity; rebuild required')
    binary = Path(str(proof.get('binary', ''))).resolve()
    if build_run.resolve() not in binary.parents or not binary.is_file():
        raise BuildInfrastructureError('prebuilt binary is missing or outside its build run')
    if (proof.get('candidate_build_failure') or proof.get('cargo_build', {}).get('exit_code') != 0
        or binding.get('candidate_digest') != tree_digest(snapshot)
        or binding.get('baseline_source_digest') != tree_digest(benchmark / 'input/repository')
        or binding.get('solution_patch_sha256') != file_hash(snapshot / 'solution.patch')
        or binding.get('cargo_lock_sha256') != file_hash(build_run / 'candidate_build/worktree/codex-rs/Cargo.lock')
        or binding.get('binary_sha256') != file_hash(binary)):
        raise BuildInfrastructureError('prebuilt Candidate/source/lock/binary binding failed')
    if output.is_symlink() or (output.exists() and any(output.iterdir())):
        raise BuildInfrastructureError('prebuilt reuse requires a new empty output directory')
    preflight = check_build_environment(runtime, output / 'preflight-target')
    measured = toolchain_identity(runtime, output / 'preflight-target')
    if binding.get('toolchain', {}).get('sha256') != measured['sha256']:
        raise BuildInfrastructureError('prebuilt toolchain identity no longer matches runtime')
    baseline_proof = read_json(build_run / 'build_preflight.json')
    if baseline_proof.get('baseline_compiled') is not True:
        raise BuildInfrastructureError('prebuilt has no valid unchanged-baseline preflight')
    copied = freeze_binary(binary, output)
    if file_hash(copied) != binding['binary_sha256']:
        raise BuildInfrastructureError('prebuilt binary changed during copy')
    result = {**proof, 'binary': str(copied), 'preflight': preflight,
              'evidence_path': str(output / 'build_result.json'),
              'prebuilt_reuse': {'build_run': str(build_run.resolve()), 'proof_sha256': file_hash(proof_path),
                  'baseline_preflight_sha256': file_hash(build_run / 'build_preflight.json'),
                  'historical_evidence_modified': False}}
    write_json(output / 'build_result.json', result)
    return copied, result


def run_agent_case(*, harness: Path, case: Path, binary: Path, output: Path, endpoint: str) -> dict[str, Any]:
    command = [
        "python3", str(harness), "--case-dir", str(case), "--binary", str(binary),
        "--output-dir", str(output), "--broker-endpoint", endpoint,
    ]
    build = read_json(binary.parent.parent / "build_result.json")
    command.extend(["--candidate-digest", str(build["candidate_digest"])])
    completed = subprocess.run(command, text=True, capture_output=True, timeout=720, check=False)
    output.parent.mkdir(parents=True, exist_ok=True)
    (output.parent / f"{case.name}.launcher.stdout.log").write_text(completed.stdout, encoding="utf-8")
    (output.parent / f"{case.name}.launcher.stderr.log").write_text(completed.stderr, encoding="utf-8")
    result_path = output / "result.json"
    if not result_path.is_file():
        raise RuntimeError(f"{case.name} evaluator produced no result: {completed.stderr[-1200:]}")
    result = read_json(result_path); result["launcher_exit_code"] = completed.returncode
    return result


def candidate_build_zero(case_id: str, build: dict[str, Any]) -> dict[str, Any]:
    return {"case_id": case_id, "score": 0, "maximum": 100,
            "validity_gate": True, "contract_valid": True, "candidate_build_failure": build,
            "candidate_digest": build.get("candidate_digest"), "environment_preflight": {"valid": True, "build_probe": build.get("preflight")},
            "classification": "candidate_build_failure", "failure_attribution": {"party": "candidate", "observed_by": "evaluator",
                "fatal": True, "reason": str(build.get("candidate_build_failure")), "evidence_paths": [build.get("evidence_path")]},
            "infrastructure_invalid": False, "execution_attempted": True, "assertions": []}


def score_public_execution(*, benchmark: Path, run_dir: Path, case_id: str, result: dict[str, Any],
                           output: Path, judge_endpoint: str, candidate_digest: str) -> dict[str, Any]:
    """Dev feedback uses the same semantic rubric boundary as hidden scoring."""
    finalizer = load_formal_finalizer(benchmark)
    verdict, zero = finalizer.execution_verdict(result, case_id, candidate_digest)
    result["native_diagnostic_score"] = result.get("score")
    if zero is not None:
        contract = zero
        result["score"] = 0
    elif verdict.get("classification") == "infrastructure_invalid":
        raise BuildInfrastructureError(f"{case_id}: {verdict.get('reason')}")
    else:
        contract = finalizer.judge_case(run_dir, case_id, result, judge_endpoint, case_dir=output)
        if not finalizer.valid(contract, case_id):
            raise BuildInfrastructureError(f"{case_id}: Result judge returned no valid measurement")
        result["score"] = contract["result_score"]
    result["dev_score_kind"] = "independent_result_rubric" if zero is None else "candidate_zero"
    result["result_judge_contract"] = contract
    write_json(output / "result.json", result)
    return result


READINESS_PROFILE = "single-dev-two-round-hidden-smoke-v1"


def evaluate_public_candidate(*, benchmark: Path, run_dir: Path, number: int, snapshot: Path,
                              runtime: Path, shared_target: Path, cases: tuple[str, ...],
                              lower_endpoint: str, judge_endpoint: str, readiness_profile=None):
    """One durable product attempt shared by public acceptance, pilot and formal."""
    output = run_dir / f'evaluations/submission_{number:03d}'
    binary, build = build_candidate(benchmark=benchmark, snapshot=snapshot,
        output=output / 'build', runtime=runtime, shared_target=shared_target)
    if readiness_profile and (readiness_profile != READINESS_PROFILE or cases != ('dev_001',) or binary is None):
        detail = ''
        if binary is None:
            # package 121: say why the build is invalid; the Builder cannot fix what it cannot see.
            try:
                tail = (output / 'build/cargo_build.stderr.log').read_text(encoding='utf-8', errors='replace')[-1500:]
            except OSError:
                tail = ''
            detail = ' (candidate build failed: ' + str(build.get('classification')) + '; cargo stderr tail: ' + tail.strip().replace('\n', ' | ') + ')'
        raise BuildInfrastructureError('readiness requires a valid build and dev_001 only' + detail)
    digest = build.get('product_source_digest')
    attempt = None
    if digest:
        if (build.get('product_source_digest_stage') != 'after_patch_apply_before_compilation'
                or product_source_digest(output / 'build/worktree') != digest):
            raise BuildInfrastructureError('materialized source changed after compilation')
        attempt = ProductAttempt(run_dir / 'product_attempts', digest, {
            'candidate_digest': tree_digest(snapshot), 'submission_number': number,
            'candidate_snapshot': str(snapshot), 'build_result': str(output / 'build/build_result.json'),
            'public_cases': list(cases)})
    elif binary is not None:
        raise BuildInfrastructureError('compiled Candidate has no observed precompile product identity')
    results = {}
    for case in cases:
        phase = 'candidate_build_zero' if binary is None else 'lower'
        if attempt:
            attempt.checkpoint(case, 'intent', {'phase': phase, 'lower_dispatch_possible': binary is not None})
        try:
            if binary is None:
                result = candidate_build_zero(case, build)
            else:
                result = run_agent_case(harness=benchmark / 'evaluator/harness/run_lower_agent_case.py',
                    case=benchmark / 'dev_cases' / case, binary=binary, output=output / case,
                    endpoint=lower_endpoint)
                attempt.checkpoint(case, 'lower_result', result)
                phase = 'result_scoring'
                attempt.checkpoint(case, 'result_intent', {'phase': phase,
                    'independent_judge_required_unless_evidenced_candidate_zero': True})
                if readiness_profile:
                    verdict, _ = load_formal_finalizer(benchmark).execution_verdict(result, case, tree_digest(snapshot))
                    if verdict.get('classification') not in {'scoreable', 'candidate_zero'}:
                        raise BuildInfrastructureError('readiness lower execution is infrastructure-invalid: ' + str(verdict.get('reason')))
                    result.update(readiness_execution_valid=True, native_diagnostic_score=result.get('score'), score=None, formal_result_publishable=False,
                        code_score_publishable=False, readiness_classification=verdict)
                else:
                    result = score_public_execution(benchmark=benchmark, run_dir=run_dir,
                        case_id=case, result=result, output=output / case, judge_endpoint=judge_endpoint,
                        candidate_digest=tree_digest(snapshot))
            write_json(output / case / 'result.json', result)
            if attempt:
                attempt.checkpoint(case, 'completed_result', result)
            results[case] = result
        except Exception as exc:
            if attempt:
                attempt.checkpoint(case, 'unresolved', {'phase': phase, 'error_type': type(exc).__name__,
                    'delivery_may_be_unknown': binary is not None, 'replay_allowed': False})
            raise
    if attempt:
        attempt.checkpoint('all_cases', 'completed', {'case_ids': list(cases),
            'binary': str(binary) if binary is not None else None})
    return results, binary


class DevController:
    def __init__(
        self, *, run_dir: Path, workspace: Path, evaluate: Callable[[int, Path], tuple[dict[str, dict[str, Any]], Path | None]],
        max_dev_rounds: int = 10, public_cases: tuple[str, ...] = ("dev_001", "dev_002"),
        readiness_profile=None, current_binding=None,
    ) -> None:
        # A new controller cannot silently discard a prior native session or
        # execution state. Existing completed/unknown evidence stays read-only.
        if any((run_dir / name).exists() for name in (
                'dev_lifecycle.json', 'infrastructure_attempts.json', 'product_attempts',
                'candidates', 'evaluations', 'freeze_manifest.json')):
            raise BuildInfrastructureError('existing controller history cannot be restarted or resampled')
        self.run_dir, self.workspace, self.evaluate = run_dir, workspace, evaluate
        self.public_cases = tuple(public_cases)
        if not self.public_cases:
            raise ValueError("public_cases must not be empty")
        if not 1 <= max_dev_rounds <= 10:
            raise ValueError("max_dev_rounds must be between 1 and 10")
        self.readiness_profile, self.current_binding = readiness_profile, current_binding
        if readiness_profile:
            if readiness_profile != READINESS_PROFILE or self.public_cases != ('dev_001',) or max_dev_rounds != 2:
                raise ValueError('readiness requires dev_001 and exactly two valid rounds')
            if not isinstance(current_binding, dict) or current_binding.get('task') != 'codex':
                raise ValueError('readiness requires current Codex source binding')
        self.max_dev_rounds = max_dev_rounds
        self.max_submissions = max_dev_rounds
        self.token = hashlib.sha256(f"{run_dir}:{time.time_ns()}".encode()).hexdigest()
        self.socket_path = Path(tempfile.gettempdir()) / f"agentswe-agentloop-{os.getpid()}-{self.token[:8]}.sock"
        self.builder_session_id: str | None = None
        self.native_observations: list[dict[str, Any]] = []
        self.feedback_deliveries: list[dict[str, Any]] = []
        # A feedback is "received" only when a write to the Builder's socket
        # returned.  The Builder's shell-tool timeout can kill the helper while
        # the evaluator is still answering, and the response write then raises
        # BrokenPipeError: the round is accepted and its feedback bytes are on
        # disk, but no receipt exists and verify_native rejects the whole
        # session.  Keep the ready response so a later connection can deliver
        # it for real, and receipt it at that later moment -- never before, and
        # never for a write that did not happen.
        self.feedback_write_failures: list[dict[str, Any]] = []
        self.undelivered_feedback: dict[int, dict[str, Any]] = {}
        self.records: list[dict[str, Any]] = []; self.by_id: dict[str, dict[str, Any]] = {}
        self.infrastructure_attempts: list[dict[str, Any]] = []
        self.by_digest: dict[str, dict[str, Any]] = {}; self.active_id: str | None = None
        self.frozen: dict[str, Any] | None = None; self.lock = threading.RLock(); self.condition = threading.Condition(self.lock)
        self.server: socketserver.ThreadingUnixStreamServer | None = None

    def bind_native_thread(self) -> None:
        observed = observe_thread(self.run_dir, self.native_observations)
        if self.builder_session_id is not None and self.builder_session_id != observed['thread_id']:
            raise NativeEvidenceError('builder_session_changed_between_submissions')
        self.builder_session_id = observed['thread_id']
        self.native_observations.append(observed)
        write_json(self.run_dir / 'native_thread_observations.json', self.native_observations)

    @staticmethod
    def _delivered_records(payload: dict[str, Any]):
        """Per-record views of what one response actually carried.

        A submit or `--status <id>` response is one public() record at top
        level; an id-less status response carries the same public() dicts under
        "records". Both come from public(), so the same accepted record has
        byte-identical content either way and therefore one receipt digest.
        """
        if isinstance(payload, dict) and payload.get('submission_number'):
            yield payload
        for record in (payload or {}).get('records') or []:
            if isinstance(record, dict) and record.get('submission_number'):
                yield record

    @staticmethod
    def _carries_feedback(record: dict[str, Any]) -> bool:
        """True only when the authoritative feedback bytes themselves went out.

        _evaluate writes the feedback file from the same public_text output it
        stores as feedback_text and digests that file, and public_text is
        idempotent, so the visible payload's feedback hashes to the record's
        feedback_digest exactly when the complete body was delivered. A
        reference that names a digest without carrying its bytes fails this and
        must never count as a delivery.
        """
        digest = record.get('feedback_digest')
        feedback = record.get('feedback')
        return bool(digest) and record.get('state') == 'completed' and isinstance(feedback, str) \
            and bool(feedback) and hashlib.sha256(feedback.encode('utf-8')).hexdigest() == digest

    def _persist_delivery_ledger(self) -> None:
        write_json(self.run_dir / 'native_feedback_deliveries.json', self.feedback_deliveries)
        write_json(self.run_dir / 'native_feedback_write_failures.json', {
            'schema_version': 'codex-feedback-delivery-ledger/v1',
            'write_failures': self.feedback_write_failures,
            'undelivered_candidate_numbers': sorted(self.undelivered_feedback)})

    def feedback_written(self, payload: dict[str, Any]) -> None:
        """Receipt every feedback the socket write that just returned carried."""
        with self.lock:
            for record in self._delivered_records(payload):
                if not self._carries_feedback(record):
                    continue
                number = record['submission_number']
                digest = record['feedback_digest']
                payload_sha256 = hashlib.sha256(
                    json.dumps(record, sort_keys=True, ensure_ascii=False).encode()).hexdigest()
                self.undelivered_feedback.pop(number, None)
                if any(row['candidate_number'] == number and row['feedback_digest'] == digest
                       and row['payload_sha256'] == payload_sha256 for row in self.feedback_deliveries):
                    continue
                self.feedback_deliveries.append({'candidate_number': number,
                    'builder_session_id': self.builder_session_id,
                    'feedback_digest': digest,
                    'evidence': 'socket_write_and_flush_completed',
                    'at': now(),
                    'payload_sha256': payload_sha256})
            self._persist_delivery_ledger()

    def feedback_write_failed(self, payload: dict[str, Any], exc: BaseException) -> None:
        """A ready response the Builder's socket never took.

        This is the opposite of a receipt: it records that delivery did not
        happen, and keeps the response deliverable by a later connection.
        """
        with self.lock:
            numbers = []
            for record in self._delivered_records(payload):
                if not self._carries_feedback(record):
                    continue
                number = record['submission_number']
                numbers.append(number)
                if not any(row['candidate_number'] == number for row in self.feedback_deliveries):
                    self.undelivered_feedback[number] = record
            self.feedback_write_failures.append({'at': now(), 'error': f"{type(exc).__name__}: {exc}",
                'candidate_numbers': numbers, 'builder_session_id': self.builder_session_id})
            self._persist_delivery_ledger()

    def native_attestation(self, *, allow_interrupted: bool = False) -> dict[str, Any]:
        with self.lock:
            records = []
            product_errors = []
            for record in self.records:
                build_path = self.run_dir / f"evaluations/submission_{record['submission_number']:03d}/build/build_result.json"
                build = read_json(build_path) if build_path.is_file() else {}
                worktree = build_path.parent / 'worktree'
                expected = build.get('candidate_repo_digest')
                if expected and (not worktree.is_dir() or tree_digest(worktree) != expected):
                    product_errors.append('materialized product source changed after build observation')
                stable = build.get('product_source_digest')
                # build_candidate emits the stable identity only at
                # 'after_patch_apply_before_compilation', so a candidate whose patch
                # never applied (unsafe paths / git_init / patch_check) materializes
                # no product source at all.  evaluate_public_candidate already treats
                # that absence as legitimate -- it demands the digest only when a
                # binary was produced -- and scores the submission with
                # candidate_build_zero.  Demanding it here for every accepted
                # submission contradicted that contract and voided whole runs whose
                # Builder merely sent a patch that did not apply.  Every other case,
                # including a cargo_build failure after a successful patch apply,
                # still owes the materialized identity.
                unmaterialized = (not stable and not build.get('binary')
                                  and build.get('candidate_build_failure')
                                  in ('unsafe or empty patch paths', 'git_init', 'patch_check'))
                if not unmaterialized and (not stable or not worktree.is_dir()
                                           or product_source_digest(worktree) != stable):
                    product_errors.append('materialized stable product identity missing or changed')
                records.append({**record, 'feedback_path': record.get('feedback'), 'feedback_format': 'text',
                    'build': {'candidate_repo_digest': build.get('candidate_repo_digest'),
                              'product_source_digest': stable}})
            proof = verify_native(self.run_dir, records, self.feedback_deliveries, self.native_observations,
                                  allow_interrupted=allow_interrupted)
            if product_errors:
                proof['valid'] = False
                proof['errors'].extend(product_errors)
            write_json(self.run_dir / 'native_builder_attestation.json', proof)
            return proof

    def start(self) -> None:
        controller = self
        class Handler(socketserver.StreamRequestHandler):
            def handle(self) -> None:
                try:
                    request = json.loads(self.rfile.readline(65536))
                    if request.get("token") != controller.token:
                        code, payload = 401, {"error": "unauthorized"}
                    elif request.get("action") == "submit":
                        code, payload = controller.submit(request.get("feedback_digest"))
                    elif request.get('action') == 'identity':
                        controller.bind_native_thread()
                        code, payload = 200, {'builder_session_id': controller.builder_session_id}
                    elif request.get("action") == "status":
                        code, payload = controller.status(str(request.get("submission_id", "") or ""))
                    elif request.get("action") == "feedback":
                        code, payload = controller.feedback()
                    else:
                        code, payload = 400, {"error": "unknown_action"}
                except Exception as exc:
                    code, payload = 400, {"error": f"{type(exc).__name__}: {exc}"}
                visible = public_payload(payload)
                try:
                    self.wfile.write(json.dumps({"status": code, "payload": visible}).encode() + b"\n")
                    self.wfile.flush()
                except OSError as exc:
                    # The Builder's helper was killed, or its shell tool timed
                    # out, while the evaluator was still answering. Nothing
                    # arrived, so nothing is receipted: record the failed write
                    # and keep the ready response deliverable by a later
                    # --status or --feedback connection.
                    if code == 200:
                        controller.feedback_write_failed(visible, exc)
                    return
                if code == 200:
                    controller.feedback_written(visible)
        class Server(socketserver.ThreadingUnixStreamServer):
            daemon_threads = True
        self.socket_path.unlink(missing_ok=True); self.socket_path.parent.mkdir(mode=0o700, exist_ok=True)
        self.server = Server(str(self.socket_path), Handler)
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def stop(self) -> None:
        if self.server:
            self.server.shutdown(); self.server.server_close()
        self.socket_path.unlink(missing_ok=True)

    def public(self, record: dict[str, Any], duplicate: bool = False) -> dict[str, Any]:
        payload = {
            "submission_id": record["submission_id"], "candidate_digest": record["candidate_digest"],
            "submission_number": record["submission_number"], "state": record["state"],
            "builder_session_id": record.get("builder_session_id"), "feedback_digest_ack": record.get("feedback_digest_ack"),
            "duplicate": duplicate, "accepted_submissions": len(self.records),
            "required_submissions": self.max_dev_rounds, "max_dev_rounds": self.max_dev_rounds,
            "frozen": bool(self.frozen and self.frozen.get("candidate_digest") == record.get("candidate_digest")),
        }
        if record["state"] == "completed":
            payload.update({
                "dev_score": record.get("dev_score"), "dev_scores": record.get("dev_scores"),
                "dev_passed": record.get("dev_passed"), "feedback": record.get("feedback_text", ""),
                "feedback_digest": record.get("feedback_digest"),
            })
        if record["state"] == "infrastructure_error":
            payload.update({"error": record.get("error"), "round_consumed": False, "replay_allowed": False})
        return payload

    def submit(self, feedback_digest: str | None = None) -> tuple[int, dict[str, Any]]:
        with self.lock:
            # An accepted digest is a read-only query even after freeze, while
            # feedback acknowledgement applies only to a new delivery.
            current_digest = tree_digest(self.workspace)
            if current_digest in self.by_digest and self.readiness_profile:
                return 409, {"error": "readiness_candidate_replay_forbidden"}
            if current_digest in self.by_digest:
                return 200, self.public(self.by_digest[current_digest], duplicate=True)
            if self.frozen:
                return 409, {"error": "frozen", **self.frozen}
            if self.active_id:
                return 409, {"error": "evaluation_in_progress", "submission_id": self.active_id}
            if len(self.records) >= self.max_submissions:
                return 409, {"error": "submission_limit_reached"}
            if self.records:
                expected_feedback = self.records[-1].get("feedback_digest")
                if not expected_feedback:
                    return 409, {"error": "latest_feedback_not_ready"}
                if feedback_digest != expected_feedback:
                    return 409, {
                        "error": "submission_requires_latest_feedback_digest",
                        "expected_feedback_digest": expected_feedback,
                    }
            elif feedback_digest:
                return 409, {"error": "feedback_digest_not_valid_for_candidate_1"}
            self.bind_native_thread()
            errors = validate_delivery(self.workspace)
            if errors:
                return 422, {"error": "invalid_submission", "details": errors}
            if self.readiness_profile:
                report = read_json(self.workspace / 'run_report.json')
                expected = {'builder_session_id': self.builder_session_id, 'submission_number': len(self.records) + 1,
                    'revision_of_candidate_digest': self.records[-1]['candidate_digest'] if self.records else None,
                    'feedback_digest': feedback_digest}
                if any(key not in report or report[key] != value for key, value in expected.items()):
                    return 422, {'error': 'readiness_submission_metadata_mismatch'}
                if self.records:
                    previous = self.records[-1]
                    if hashlib.sha256(Path(previous['feedback']).read_bytes()).hexdigest() != feedback_digest:
                        return 409, {'error': 'authoritative_feedback_bytes_changed'}
                    explanation = read_json(self.workspace / 'edit_report.json').get('feedback_response')
                    if not isinstance(explanation, str) or not explanation.strip():
                        return 422, {'error': 'revision_feedback_response_required'}
                    if (self.workspace/'solution.patch').read_bytes() == (Path(previous['candidate'])/'solution.patch').read_bytes():
                        return 422, {'error': 'revision_must_change_product_patch'}
            provisional = self.run_dir / "candidates/provisional"; shutil.rmtree(provisional, ignore_errors=True)
            shutil.copytree(self.workspace, provisional, symlinks=True); digest = tree_digest(provisional)
            if digest in self.by_digest:
                shutil.rmtree(provisional); return 200, self.public(self.by_digest[digest], duplicate=True)
            number = len(self.records) + 1; snapshot = self.run_dir / f"candidates/submission_{number:03d}"
            provisional.rename(snapshot); submission_id = f"candidate-{number:03d}-{digest[:12]}"
            record = {
                "submission_number": number, "submission_id": submission_id, "candidate": str(snapshot),
                "candidate_digest": digest, "state": "running", "submitted_at": now(),
                "role": "initial" if number == 1 else "feedback_revision",
                "feedback_digest_ack": feedback_digest,
                "builder_session_id": self.builder_session_id,
            }
            if not post_freeze_superseded(self, record):
                self.records.append(record); self.by_id[submission_id] = record; self.by_digest[digest] = record
            self.active_id = submission_id; write_json(self.run_dir / "dev_lifecycle.json", self.records)
            threading.Thread(target=self._evaluate, args=(submission_id,), daemon=True).start()
            return 202, self.public(record)

    def _evaluate(self, submission_id: str) -> None:
        with self.lock:
            record = self.by_id[submission_id]; number = int(record["submission_number"]); snapshot = Path(record["candidate"])
        try:
            results, binary = self.evaluate(number, snapshot)
            if set(results) != set(self.public_cases):
                raise BuildInfrastructureError("dev evaluator returned an incomplete case inventory")
            for case_id, result in results.items():
                classification = str(result.get("classification", ""))
                if ("infrastructure" in classification or result.get("infrastructure_invalid") is True
                        or (not self.readiness_profile and result.get("score") is None)
                        or (self.readiness_profile and (binary is None or result.get("readiness_execution_valid") is not True))):
                    raise BuildInfrastructureError(f"{case_id}: {classification or 'no measurable score'}")
            scores = {case_id: result.get("score") for case_id, result in results.items()}
            numeric = [scores.get(case_id) for case_id in self.public_cases]
            mean_score = round(sum(score for score in numeric if isinstance(score, int)) / len(self.public_cases), 4) \
                if all(isinstance(score, int) for score in numeric) else 0.0
            feedback_lines = [
                f"# Authoritative lower-agent dev feedback — Candidate {number}", "",
                f"- public-case mean: {mean_score}/100", "",
            ]
            for case_id in self.public_cases:
                result = results[case_id]
                feedback_lines.extend([
                    f"## {case_id}", "",
                    f"- score: {result.get('score')}/100",
                    f"- validity_gate: {result.get('validity_gate')}",
                    f"- broker calls: {result.get('broker', {}).get('calls_delta')}",
                    f"- candidate build failure: {json.dumps(result.get('candidate_build_failure'), ensure_ascii=False)}",
                    f"- semantic feedback: {json.dumps(semantic_feedback(result.get('result_judge_contract')), ensure_ascii=False)}",
                    f"- assertions: {json.dumps(result.get('assertions', []), ensure_ascii=False)}", "",
                ])
            feedback = public_text("\n".join(feedback_lines) + "\n")
            feedback_path = self.run_dir / f"feedback/submission_{number:03d}.md"; feedback_path.parent.mkdir(parents=True, exist_ok=True)
            feedback_path.write_text(feedback, encoding="utf-8")
            feedback_digest = hashlib.sha256(feedback_path.read_bytes()).hexdigest()
            with self.condition:
                binary_sha256 = hashlib.sha256(binary.read_bytes()).hexdigest() if binary and binary.is_file() else None
                record.update({
                    "state": "completed", "finished_at": now(), "dev_score": mean_score,
                    "dev_scores": scores, "dev_results": {key: str(self.run_dir / f"evaluations/submission_{number:03d}/{key}/result.json") for key in results},
                    "results": results,
                    "dev_passed": bool(mean_score > 60),
                    "feedback": str(feedback_path), "feedback_text": feedback,
                    "feedback_digest": feedback_digest,
                    "build_valid": binary is not None, "binary": str(binary) if binary else None,
                    "binary_sha256": binary_sha256,
                })
                if number == self.max_dev_rounds and not self.readiness_profile:
                    frozen = self.run_dir / "frozen_submission"; shutil.copytree(snapshot, frozen, symlinks=True)
                    self.frozen = {
                        "path": str(frozen), "candidate_digest": tree_digest(frozen), "source_submission_id": submission_id,
                        "reason": "max_dev_rounds", "freeze_reason": "max_dev_rounds", "frozen_at": now(),
                        "build_valid": binary is not None, "binary": str(binary) if binary else None,
                        "binary_sha256": binary_sha256,
                    }
                    write_json(self.run_dir / "freeze_manifest.json", self.frozen)
                self.active_id = None; write_json(self.run_dir / "dev_lifecycle.json", self.records); self.condition.notify_all()
        except Exception as exc:
            with self.condition:
                record.update({"state": "infrastructure_error", "finished_at": now(), "error": f"{type(exc).__name__}: {exc}"})
                archive = self.run_dir / "infrastructure_attempts" / f"attempt_{len(self.infrastructure_attempts) + 1:03d}"
                archive.mkdir(parents=True, exist_ok=False)
                if snapshot.exists():
                    snapshot.rename(archive / "candidate")
                    record["candidate"] = str(archive / "candidate")
                evaluation = self.run_dir / f"evaluations/submission_{number:03d}"
                if evaluation.exists():
                    evaluation.rename(archive / "evaluation")
                record["round_consumed"] = False
                self.infrastructure_attempts.append(dict(record))
                write_json(archive / "attempt.json", record)
                write_json(self.run_dir / "infrastructure_attempts.json", self.infrastructure_attempts)
                prune_superseded_build_caches(self.run_dir / "infrastructure_attempts", archive)
                self.records.remove(record)  # Keep the original infrastructure delivery cached.
                # ProductAttempt separately rejects changed reports around the same applied source.
                self.active_id = None; write_json(self.run_dir / "dev_lifecycle.json", self.records); self.condition.notify_all()

    def status(self, submission_id: str = "") -> tuple[int, dict[str, Any]]:
        """One accepted record by id, or -- with no id -- every accepted record.

        The Builder learns a submission_id only from the response to its own
        submit, and that response is exactly what a killed shell tool loses.
        An id-less status is therefore the recovery path: it re-delivers every
        accepted record in the same public() shape a submit response carries,
        so the write that carries it earns a real receipt. It issues nothing
        new and consumes no round; a record reaches self.records only after its
        feedback file has been written, so every record here is complete.
        """
        with self.lock:
            if not submission_id:
                return 200, {"builder_session_id": self.builder_session_id,
                    "accepted_submissions": len(self.records),
                    "required_submissions": self.max_dev_rounds,
                    "max_dev_rounds": self.max_dev_rounds,
                    "frozen": self.frozen is not None,
                    "records": [self.public(record) for record in list(self.records)]}
            record = self.by_id.get(submission_id)
            return (404, {"error": "unknown_submission_id"}) if record is None else (200, self.public(record))

    def feedback(self) -> tuple[int, dict[str, Any]]:
        """Re-deliver one accepted feedback whose earlier response was lost.

        This re-reads the same authoritative record the accepted submission
        already carries; it issues nothing new and consumes no round. The
        oldest response a failed write left undelivered is preferred.
        """
        with self.lock:
            if not self.records:
                return 404, {"error": "no accepted submission has feedback yet"}
            pending = [n for n in sorted(self.undelivered_feedback) if 1 <= n <= len(self.records)]
            return 200, self.public(self.records[(pending[0] if pending else len(self.records)) - 1])

    def wait_idle(self) -> None:
        with self.condition:
            while self.active_id:
                self.condition.wait(timeout=5)

    def freeze_latest(self, reason: str = "builder_exit", *, builder_exit_evidence=None) -> dict[str, Any]:
        with self.condition:
            if self.readiness_profile:
                from harbor.readiness_contract import validate_freeze_inputs
                validate_freeze_inputs(self, reason, builder_exit_evidence)
            if self.active_id:
                self.condition.wait_for(lambda: self.active_id is None, timeout=7200)
            if self.frozen:
                return self.frozen
            if not self.records:
                raise RuntimeError("cannot freeze without an accepted Candidate")
            record = self.records[-1]
            snapshot = Path(str(record["candidate"]))
            frozen = self.run_dir / "frozen_submission"
            if frozen.exists():
                shutil.rmtree(frozen)
            shutil.copytree(snapshot, frozen, symlinks=True)
            self.frozen = {"path": str(frozen), "candidate_digest": tree_digest(frozen),
                           "source_submission_id": record["submission_id"], "reason": reason,
                           "freeze_reason": reason, "frozen_at": now(),
                           "build_valid": record.get("build_valid", False), "binary": record.get("binary"),
                           "binary_sha256": record.get("binary_sha256"),
                           "accepted_rounds": len(self.records), "max_dev_rounds": self.max_dev_rounds}
            if self.readiness_profile:
                build = read_json(self.run_dir / 'evaluations/submission_002/build/build_result.json')
                self.frozen.update(readiness_profile=self.readiness_profile, current_binding=self.current_binding,
                    builder_session_id=self.builder_session_id, builder_exit_evidence=builder_exit_evidence,
                    source_submission=2, delivery_candidate_digest=record['candidate_digest'],
                    materialized_repository_digest=build['candidate_repo_digest'],
                    materialized_source_digest=build['product_source_digest'],
                    submission_sha256={name: hashlib.sha256((snapshot/name).read_bytes()).hexdigest()
                        for name in ('solution.patch', 'edit_report.json', 'run_report.json')})
            write_json(self.run_dir / "freeze_manifest.json", self.frozen)
            return self.frozen


def task_toml(name: str, task_timeout_seconds: float = GENERATED_BUILDER_TASK_TIMEOUT_SECONDS) -> str:
    return f'''schema_version = "1.4"
[task]
name = "local/{name}"
version = "1.0.0"
description = "AgentSWE formal Edit repeated two-dev feedback closure"
authors = [{{ name = "AgentSWE" }}]
artifacts = [{{ source = "/workspace/submission", destination = "builder_submission" }}]
[agent]
timeout_sec = {float(task_timeout_seconds)}
user = "root"
network_mode = "public"
[verifier]
timeout_sec = 300.0
user = "root"
environment_mode = "shared"
network_mode = "no-network"
[environment]
docker_image = "{BUILDER_IMAGE}"
network_mode = "public"
build_timeout_sec = 900.0
cpus = 8
memory_mb = 16384
storage_mb = 32768
workdir = "/workspace"
[environment.healthcheck]
command = "python3 /usr/local/bin/builder_resource_gate.py"
timeout_sec = 10.0
retries = 1
'''


def stage_builder(
    *, run_dir: Path, public: Path, workspace: Path, controller: DevController,
    provider_config: Path, public_cases: tuple[str, ...], pilot_not_formal: bool = False,
    compose_subnet: str | None = None,
) -> Path:
    task = run_dir / "builder_task"; (task / "environment").mkdir(parents=True); (task / "tests").mkdir(parents=True); (task / "solution").mkdir(parents=True)
    builder_task_timeout = PILOT_BUILDER_TASK_TIMEOUT_SECONDS if pilot_not_formal else GENERATED_BUILDER_TASK_TIMEOUT_SECONDS
    (task / "task.toml").write_text(task_toml("codex-residual-agentloop-builder", builder_task_timeout), encoding="utf-8")
    case_list = ", ".join(public_cases)
    pilot_clause = (
        "This is a non-formal pilot. Submit Candidate 1 and consume its exact evaluator feedback digest. "
        "Continue the same Builder session for useful revisions, up to five accepted distinct digests. "
        "Both dev cases run per accepted submission. A dev mean above 60 is feedback only. "
        "You may exit after the latest accepted snapshot; the evaluator freezes it on Builder exit or "
        "max_dev_rounds. Do not inspect hidden cases or run hidden tests. "
        "No formal Result or Code score may be claimed."
        if pilot_not_formal else
        "Continue this same Builder session for useful feedback-driven revisions, up to five accepted digests. "
        "The Builder may exit early; the evaluator then freezes the latest structurally valid accepted snapshot."
    )
    if getattr(controller, 'readiness_profile', None):
        pilot_clause = ("Complete exactly TWO structurally/build/transport-valid dev_001 rounds in this one uninterrupted native turn. "
            "After first feedback revise product source meaningfully and acknowledge its exact digest. Functional score is no gate. "
            "run_report.json must contain builder_session_id returned by submit status, submission_number, revision_of_candidate_digest "
            "and feedback_digest (explicit null for both parent fields in round 1). In round 2 write edit_report.json feedback_response. "
            "Use submit_dev_candidate --identity before delivery to obtain the native thread ID. Exit only after both rounds complete. "
            "Freeze and hidden test_001 occur after your exit; the evaluator runs independent non-formal Result and Code smokes.")
    (task / "instruction.md").write_text(f'''# AgentSWE Codex Edit Builder — iterative closure

You are the upper Builder. Your harness/model is Codex with deepseek-flash and xhigh reasoning.
Read all four files under `/builder-package/input`, the complete public dev case descriptions ({case_list}), and the supplied repository.
Edit a writable copy of the repository and maintain exactly three delivery files under `/workspace/submission`: `solution.patch`, `edit_report.json`, and `run_report.json`.

Submit the initial Candidate with `submit_dev_candidate --wait`; every accepted
digest runs the mounted public dev cases against the patched lower Codex agent and
returns evaluator-owned feedback including a feedback digest. Before every
subsequent distinct submission, wait for the previous response and pass its
exact digest with `--feedback-digest <digest>`; a missing or stale digest is
rejected. Duplicate digests are idempotent and do not consume a round. A dev
mean above 60 is feedback only and does not freeze the run. {pilot_clause}
Do not inspect hidden cases, evaluator sources, credentials, prior Candidate
snapshots, or other runs.

A submission runs the public dev cases and can take far longer than one shell
tool call, so `submit_dev_candidate` hands the submit to a detached helper and
returns early instead of holding one long command. If it prints status 202 with
state submission_in_progress, run `submit_dev_candidate --await-only` again, as
many times as needed, until it prints the real response. Never wrap the command
in `nohup`, never background or `disown` it, and never kill it. At any time,
including while an evaluation is still running, `submit_dev_candidate --status`
with no argument answers within seconds with every accepted record, its full
feedback and its exact digest, and `submit_dev_candidate --feedback` reprints
the latest accepted feedback and digest. If a response is ever lost, recover it
with one of those two commands; do not resubmit an unchanged Candidate.
''', encoding="utf-8")
    submit = task / "environment/submit_dev_candidate"
    submit.write_text('''#!/usr/bin/env python3
"""Builder-side controller client.

A dev evaluation runs the public cases and takes far longer than one shell tool
call, so the submit and the wait for its answer both run in a detached, fully
daemonised holder. The foreground process only polls a local response file in
short bounded waits: a shell-tool timeout can kill the poller without cutting
the evaluator response off, and the submission id is written down the moment
the evaluator issues it, so a killed command can never lose it. Nothing here
needs nohup, backgrounding or kill.
"""
import argparse, json, os, socket, sys, time
from pathlib import Path

TMP = Path(os.environ.get("TMPDIR") or "/tmp")
RESPONSE = TMP / "agentswe-submit-response.json"
ACCEPTED = TMP / "agentswe-submit-accepted.json"
HOLDER = TMP / "agentswe-submit-holder.pid"

p = argparse.ArgumentParser(prog="submit_dev_candidate")
p.add_argument("--status", nargs="?", const="", default=None, metavar="SUBMISSION_ID",
               help="one accepted record by id, or every accepted record and its feedback when given no id")
p.add_argument("--feedback", action="store_true", help="reprint the latest accepted evaluator feedback and its exact digest")
p.add_argument("--identity", action="store_true", help="bind and print the native Builder thread id")
p.add_argument("--feedback-digest", dest="feedback_digest", help="exact digest of the feedback this revision answers")
p.add_argument("--wait", action="store_true", help="accepted for compatibility; a submit always waits by polling")
p.add_argument("--await-only", dest="await_only", action="store_true", help="keep waiting for the submission already running")
p.add_argument("--poll-seconds", dest="poll_seconds", type=float, default=240.0, help="how long this call polls before reporting 202")
a, _extra = p.parse_known_args()

SOCK = os.environ["AGENTSWE_DEV_CONTROLLER_SOCKET"]
TOKEN = os.environ["AGENTSWE_DEV_CONTROLLER_TOKEN"]


def call(request):
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as s:
        s.connect(SOCK)
        s.sendall((json.dumps({"token": TOKEN, **request}) + "\\n").encode())
        return json.loads(s.makefile().readline())


def finish(result):
    print(json.dumps(result, ensure_ascii=False))
    raise SystemExit(0 if result.get("status") in (200, 202) else 1)


def holder_alive():
    try:
        os.kill(int(HOLDER.read_text().strip()), 0)
    except (OSError, ValueError):
        return False
    return True


def poll(deadline):
    while True:
        try:
            return json.loads(RESPONSE.read_text())
        except (OSError, ValueError):
            pass
        if time.time() >= deadline:
            return None
        time.sleep(2)


if a.identity:
    finish(call({"action": "identity"}))
if a.feedback:
    finish(call({"action": "feedback"}))
if a.status is not None:
    finish(call({"action": "status", "submission_id": a.status}))

if a.feedback_digest:
    report = json.loads(Path("/workspace/submission/run_report.json").read_text())
    if report.get("feedback_digest") != a.feedback_digest:
        raise SystemExit("run_report feedback_digest differs from explicit acknowledgement")

if not a.await_only and not holder_alive():
    for path in (RESPONSE, ACCEPTED, HOLDER):
        try:
            path.unlink()
        except OSError:
            pass
    if os.fork() == 0:
        os.setsid()
        if os.fork() != 0:
            os._exit(0)
        # Detach every inherited descriptor so the Builder shell tool sees EOF
        # at once and never waits on this holder.
        null = os.open(os.devnull, os.O_RDWR)
        for fd in (0, 1, 2):
            os.dup2(null, fd)
        HOLDER.write_text(str(os.getpid()))
        try:
            request = {"action": "submit"}
            if a.feedback_digest:
                request["feedback_digest"] = a.feedback_digest
            result = call(request)
            ACCEPTED.write_text(json.dumps(result, ensure_ascii=False))
            sid = (result.get("payload") or {}).get("submission_id")
            while sid and (result.get("payload") or {}).get("state") == "running":
                time.sleep(5)
                result = call({"action": "status", "submission_id": sid})
        except Exception as exc:
            result = {"status": 599, "payload": {"error": type(exc).__name__ + ": " + str(exc)}}
        part = RESPONSE.with_suffix(".part")
        part.write_text(json.dumps(result, ensure_ascii=False))
        os.replace(str(part), str(RESPONSE))
        try:
            HOLDER.unlink()
        except OSError:
            pass
        os._exit(0)
    os.wait()

result = poll(time.time() + max(1.0, a.poll_seconds))
if result is None:
    print(json.dumps({"status": 202, "payload": {"state": "submission_in_progress",
        "reason": "the evaluator is still running the public dev cases; run "
                  "`submit_dev_candidate --await-only` again to keep waiting, or "
                  "`submit_dev_candidate --status` for every accepted record and its feedback"}},
        ensure_ascii=False))
    raise SystemExit(3)
finish(result)
''', encoding="utf-8"); submit.chmod(0o755)
    (task / "tests/test.sh").write_text('''#!/usr/bin/env bash
set -uo pipefail
mkdir -p /logs/verifier
python3 - <<'PY'
import json
from pathlib import Path
p=Path('/workspace/submission'); required={'solution.patch','edit_report.json','run_report.json'}
ok=p.is_dir() and {x.name for x in p.iterdir()}==required
Path('/logs/verifier/reward.json').write_text(json.dumps({'reward':1 if ok else 0})+'\\n')
Path('/logs/verifier/reward.txt').write_text(('1' if ok else '0')+'\\n')
PY
exit 0
''', encoding="utf-8"); (task / "tests/test.sh").chmod(0o755)
    resource_gate = task / 'environment/builder_resource_gate.py'
    shutil.copy2(Path(__file__).resolve().parent / 'builder_resource_gate.py', resource_gate)
    codex_wrapper = task / 'environment/codex'
    shutil.copy2(Path(__file__).resolve().parent / 'codex_feature_wrapper.py', codex_wrapper)
    codex_wrapper.chmod(0o755)
    compose = {
        "networks": {
            "default": {
                "ipam": {
                    "config": [{"subnet": compose_subnet or select_compose_subnet(run_dir=run_dir)}]
                }
            }
        },
        "services": {"main": {"cpu_quota": 800000, "cpu_period": 100000, "volumes": [
        {"type": "bind", "source": str(resource_gate), "target": "/usr/local/bin/builder_resource_gate.py", "read_only": True},
        {"type": "bind", "source": str(public), "target": "/builder-package", "read_only": True},
        {"type": "bind", "source": str(workspace), "target": "/workspace/submission"},
        {"type": "bind", "source": str(controller.socket_path), "target": CONTROLLER_SOCKET_IN_CONTAINER, "read_only": True},
        {"type": "bind", "source": str(submit), "target": "/usr/local/bin/submit_dev_candidate", "read_only": True},
        {"type": "bind", "source": str(codex_wrapper), "target": "/workspace/codex-wrapper/codex", "read_only": True},
        {"type": "bind", "source": str(provider_config), "target": str(provider_config), "read_only": True},
    ], "environment": {
        "AGENTSWE_DEV_CONTROLLER_SOCKET": CONTROLLER_SOCKET_IN_CONTAINER,
        "AGENTSWE_DEV_CONTROLLER_TOKEN": controller.token,
        "AGENTSWE_BUILDER_BROKER_TOKEN": "broker-only-placeholder",
    }}}}
    write_json(task / "environment/docker-compose.yaml", compose)
    config = run_dir / "builder_job_config.json"
    write_json(config, {
        "job_name": f"0901-codex-agentloop-builder-{run_dir.name}", "jobs_dir": str(run_dir / "jobs"),
        "n_attempts": 1, "n_concurrent_trials": 1, "quiet": True, "retry": {"max_retries": 0},
        "environment": {"type": "docker", "delete": not bool(getattr(controller, "readiness_profile", None)), "cpu_enforcement_policy": "limit", "memory_enforcement_policy": "limit"},
        "agents": [{"import_path": "agentswe_codex_resume:CodexResume", "model_name": BUILDER_MODEL, "env": {
            "CODEX_HOME": "/tmp/agentswe-codex-home", "CODEX_CONFIG_TOML_PATH": str(provider_config),
            "PATH": "/workspace/codex-wrapper:/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin",
            "AGENTSWE_BUILDER_BROKER_TOKEN": "broker-only-placeholder",
        }, "kwargs": {"reasoning_effort": BUILDER_EFFORT, "web_search": "live"}}],
        "tasks": [{"path": str(task)}],
    })
    return config


def run_harbor(harbor: Path, config: Path, output: Path, timeout: int = DEFAULT_OUTER_BUILDER_TIMEOUT_SECONDS) -> dict[str, Any]:
    output.parent.mkdir(parents=True, exist_ok=True)
    with (output.with_suffix(".stdout.log")).open("w") as stdout, (output.with_suffix(".stderr.log")).open("w") as stderr:
        process = subprocess.Popen([str(harbor), "run", "-c", str(config)], stdout=stdout, stderr=stderr, start_new_session=True)
        try:
            code = process.wait(timeout=timeout)
        except (subprocess.TimeoutExpired, KeyboardInterrupt) as exc:
            code = 124 if isinstance(exc, subprocess.TimeoutExpired) else 130
            os.killpg(process.pid, signal.SIGTERM)
            try: process.wait(timeout=15)
            except subprocess.TimeoutExpired:
                os.killpg(process.pid, signal.SIGKILL); process.wait()
    return {"exit_code": code, "stdout": str(output.with_suffix('.stdout.log')), "stderr": str(output.with_suffix('.stderr.log'))}


SHARED_BUILDER_SEGMENTS = "@@AGENTSWE_EDITING_CONTROL@@"


def builder_segments_runtime():
    """The shared Builder segment loop (package 97, builder_segments.py).

    One codex session may span more than one Harbor trial when an evaluator-side
    infrastructure cut ends a container mid-turn; that module owns the whole
    decision, the relaunch and <run>/builder_segments.json, so that all ten
    trees behave identically.  A run that never resumes is unaffected.
    """
    import sys as _sys
    if SHARED_BUILDER_SEGMENTS not in _sys.path:
        _sys.path.insert(0, SHARED_BUILDER_SEGMENTS)
    import builder_segments
    return builder_segments


def run_harbor_segments(harbor: Path, config: Path, run_dir: Path, timeout: int,
                        *, observer=None) -> dict[str, Any]:
    """run_harbor, run once per Builder segment; see builder_segments_runtime()."""
    output = run_dir / 'builder_process'
    output.parent.mkdir(parents=True, exist_ok=True)
    code = builder_segments_runtime().run_builder_segments(
        run_dir, config, harbor=harbor, budget_seconds=timeout, observer=observer,
        log_mode='w', log_paths=(output.with_suffix('.stdout.log'),
                                 output.with_suffix('.stderr.log')))
    return {"exit_code": code, "stdout": str(output.with_suffix('.stdout.log')),
            "stderr": str(output.with_suffix('.stderr.log'))}


def run_configured_builder(harbor: Path, config: Path, run_dir: Path, timeout: int = DEFAULT_OUTER_BUILDER_TIMEOUT_SECONDS,
                           *, credential: Path, transport: str = 'direct',
                           base_url: str = 'https://api.deepseek.com/v1',
                           proxy: str = 'http://127.0.0.1:7890') -> dict[str, Any]:
    if transport not in {'direct', 'broker'}:
        raise ValueError('unknown Builder transport')
    from harbor.builder_resources import BuilderResourceObserver
    from harbor.builder_direct import execution
    observer = BuilderResourceObserver(run_dir)
    observer.start()
    started = False
    try:
        if transport == 'broker':
            started = True
            result = run_harbor_segments(harbor, config, run_dir, timeout,
                                         observer=observer)
        else:
            with execution(config, run_dir, credential, base_url=base_url, proxy=proxy):
                started = True
                result = run_harbor_segments(harbor, config, run_dir, timeout,
                                         observer=observer)
    finally:
        resources = observer.finish()
        cleanup = observer.cleanup_owned(process_started=started, defer_removal=(run_dir/"readiness_current_binding.json").is_file())
    # Harbor may return process exit 0 while a trial has HealthcheckError.
    # Preserve its actual host result and report an infrastructure exit.
    job = read_json(config)['job_name']
    trials = []
    for path in (run_dir / 'jobs' / job).glob('*/result.json'):
        value = read_json(path)
        trials.append({'path': str(path), 'sha256': hashlib.sha256(path.read_bytes()).hexdigest(),
                       'exception_info': value.get('exception_info')})
    ledger = builder_segments_runtime().ledger_trials(run_dir)
    if ledger:
        # A resumed Builder is ONE session across several Harbor trials: only
        # the terminal segment has to be exception-free.  The evidence layer
        # proves the earlier ones were infrastructure cuts of that same session.
        rows = {Path(row['path']).parent.name: row for row in trials}
        trial_valid = (sorted(rows) == sorted(ledger)
                       and not rows[ledger[-1]]['exception_info'])
    else:
        trial_valid = bool(trials) and not any(row['exception_info'] for row in trials)
    write_json(run_dir / 'builder_trial_attestation.json', {'valid': trial_valid, 'trials': trials})
    if not resources['valid'] or (not cleanup['complete'] and not cleanup.get("retained_terminal")) or not trial_valid:
        # The verdict is unchanged; only its reason becomes recordable, so a 125
        # can be written into one_stop_summary.json instead of vanishing into an
        # uncaught RuntimeError.
        result['exit_code'] = 125
        result['infrastructure_invalid'] = {
            'resources_valid': bool(resources['valid']),
            'cleanup_complete': bool(cleanup['complete']),
            'cleanup_retained_terminal': bool(cleanup.get("retained_terminal")),
            'trial_valid': trial_valid,
            'trials': trials,
        }
    return result


def broker_stats(port: int) -> dict[str, Any]:
    request = urllib.request.Request(
        f"http://127.0.0.1:{port}/stats",
        headers={"Authorization": "Bearer stats-only-placeholder"},
    )
    with urllib.request.urlopen(request, timeout=10) as response:
        value = json.loads(response.read())
    if not isinstance(value, dict):
        raise RuntimeError("broker stats response is not an object")
    return value


def load_formal_finalizer(root: Path) -> Any:
    module_path = root / "evaluator/formal_finalize.py"
    spec = importlib.util.spec_from_file_location("codex_pilot_formal_finalizer", module_path)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"unable to load finalizer: {module_path}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def start_fresh_hidden_broker(*, args: argparse.Namespace, run_dir: Path, port: int,
                              name: str, cidfile: Path) -> dict[str, Any]:
    """The sole post-freeze broker transition for acceptance and formal runs."""
    freeze = read_json(run_dir / "freeze_manifest.json")
    if not freeze.get("candidate_digest") or not freeze.get("frozen_at"):
        raise RuntimeError("hidden broker requires an existing frozen Candidate")
    start_broker(port=port, script=args.broker_script.resolve(), image=args.broker_image,
                 credential=args.credential_file.resolve(), name=name, cidfile=cidfile)
    initial = broker_stats(port)
    runtime = initial.get("runtime") or {}
    if int(runtime.get("calls", 0) or 0) or int(runtime.get("failures", 0) or 0):
        raise RuntimeError("hidden broker did not start at zero calls/failures")
    evidence = {"started_after_freeze": True, "frozen_candidate_digest": freeze["candidate_digest"],
                "started_at": now(), "endpoint": f"http://127.0.0.1:{port}/v1/responses",
                "calls": 0, "failures": 0, "stats": initial}
    write_json(run_dir / "hidden_broker_initial.json", evidence)
    return evidence


def run_pilot(args: argparse.Namespace) -> int:
    """Run a reduced real lifecycle without entering the formal finalizer."""
    if getattr(args, 'readiness_profile', None):
        from harbor.readiness_launcher import run
        return run(args)
    benchmark, run_dir = args.benchmark.resolve(), args.run_dir.resolve()
    builder_broker_script = (args.builder_broker_script or BUILDER_BROKER_SCRIPT).resolve()
    for path in (args.harbor, args.runtime / "bin/cargo", args.credential_file,
                 args.broker_script, builder_broker_script):
        if not path.exists():
            raise FileNotFoundError(path)
    dev_names = ("dev_001", "dev_002")
    hidden_names = ("test_001", "test_002")
    if any(not (benchmark / "dev_cases" / name).is_dir() for name in dev_names):
        raise RuntimeError("pilot public inventory is incomplete")
    if any(not (benchmark / "test_cases" / name).is_dir() for name in hidden_names):
        raise RuntimeError("pilot hidden inventory is incomplete")
    prepare_run_dir(run_dir)
    public = run_dir / "builder_public_package"
    shutil.copytree(benchmark / "input", public / "input", symlinks=True)
    for case_id in dev_names:
        shutil.copytree(benchmark / "dev_cases" / case_id, public / "dev_cases" / case_id, symlinks=True)
    workspace = run_dir / "builder_workspace/submission"
    workspace.mkdir(parents=True)

    public_port, builder_port, hidden_port, judge_port = allocate_port(), allocate_port(), allocate_port(), allocate_port()
    while len({public_port, builder_port, hidden_port, judge_port}) != 4:
        public_port, builder_port, hidden_port, judge_port = allocate_port(), allocate_port(), allocate_port(), allocate_port()
    public_endpoint = f"http://127.0.0.1:{public_port}/v1/responses"
    hidden_endpoint = f"http://127.0.0.1:{hidden_port}/v1/responses"
    judge_endpoint = f"http://127.0.0.1:{judge_port}/v1/responses"
    suffix = hashlib.sha256(str(run_dir).encode()).hexdigest()[:10]
    names = {
        "public": f"agentswe-codex-pilot-public-{suffix}",
        "builder": f"agentswe-codex-pilot-builder-{suffix}",
        "hidden": f"agentswe-codex-pilot-hidden-{suffix}",
        "judge": f"agentswe-codex-pilot-judge-{suffix}",
    }
    cidfiles = {role: run_dir / "brokers" / f"{role}.cid" for role in names}
    provider_config = run_dir / "builder_broker_provider.toml"
    provider_config.write_text(
        'model_provider = "agentswe_builder_broker"\n'
        'disable_response_storage = true\n\n'
        '[model_providers.agentswe_builder_broker]\n'
        'name = "AgentSWE Codex pilot Builder broker"\n'
        f'base_url = "http://172.17.0.1:{builder_port}/v1"\n'
        'env_key = "AGENTSWE_BUILDER_BROKER_TOKEN"\n'
        'wire_api = "responses"\n'
        'requires_openai_auth = false\n', encoding="utf-8",
    )
    shared_target = run_dir / "build_cache/target"
    write_json(run_dir / "build_preflight.json", baseline_build(benchmark, args.runtime.resolve(), shared_target, run_dir / "build_preflight"))
    build_binaries: dict[int, Path] = {}
    active_endpoint = public_endpoint

    def evaluate(number: int, snapshot: Path) -> tuple[dict[str, dict[str, Any]], Path | None]:
        results, binary = evaluate_public_candidate(benchmark=benchmark, run_dir=run_dir,
            number=number, snapshot=snapshot, runtime=args.runtime.resolve(), shared_target=shared_target,
            cases=tuple(dev_names), lower_endpoint=active_endpoint, judge_endpoint=judge_endpoint)
        if binary is not None:
            build_binaries[number] = binary
        return results, binary

    controller = DevController(
        run_dir=run_dir, workspace=workspace, evaluate=evaluate,
        max_dev_rounds=args.max_dev_rounds, public_cases=dev_names,
    )
    attempted = {role: False for role in names}
    cleanup_receipts: list[dict[str, Any]] = []
    public_cleanup_done = False
    base_summary = {
        "schema_version": "agentswe-codex-pilot-summary/v1",
        "mode": "pilot", "pilot_not_formal": True,
        "formal_result_claimed": False, "code_score_claimed": False,
        "result_axis": "N/A", "code_axis": "N/A",
        "public_cases": list(dev_names), "hidden_cases": list(hidden_names),
    }
    try:
        controller.start()
        attempted["public"] = True
        start_broker(port=public_port, script=args.broker_script.resolve(), image=args.broker_image,
                     credential=args.credential_file.resolve(), name=names["public"], cidfile=cidfiles["public"])
        if args.builder_transport == 'broker':
            attempted["builder"] = True
            start_broker(port=builder_port, script=builder_broker_script, image=args.broker_image,
                         credential=args.credential_file.resolve(), name=names["builder"], cidfile=cidfiles["builder"])
        attempted["judge"] = True
        start_broker(port=judge_port, script=JUDGE_BROKER_SCRIPT, image=args.broker_image,
                     credential=args.credential_file.resolve(), name=names["judge"], cidfile=cidfiles["judge"])
        config = stage_builder(
            run_dir=run_dir, public=public, workspace=workspace, controller=controller,
            provider_config=provider_config, public_cases=dev_names, pilot_not_formal=True,
        )
        write_json(run_dir / "protocol_lock.json", {
            "schema_version": "agentswe-codex-pilot-protocol/v1",
            "pilot_not_formal": True, "builder_model": BUILDER_MODEL,
            "builder_reasoning_effort": BUILDER_EFFORT,
            "lower_model": MODEL, "lower_reasoning_effort": LOWER_EFFORT,
            "public_cases": list(dev_names), "hidden_cases": list(hidden_names),
            "max_dev_rounds": args.max_dev_rounds, "hidden_after_freeze": True,
            "n_concurrent": 1, "dev_passed_is_automatic_freeze": False,
            "formal_result_claimed": False, "formal_execution_started": False,
        })
        builder = run_configured_builder(args.harbor.resolve(), config, run_dir, args.builder_timeout,
            credential=args.credential_file.resolve(), transport=args.builder_transport, base_url=args.builder_base_url, proxy=args.builder_proxy)
        controller.wait_idle()
        records = list(controller.records)
        native = controller.native_attestation()
        attestation = {
            "schema_version": "agentswe-codex-pilot-builder-attestation/v1",
            "pilot_not_formal": True, "builder_model": BUILDER_MODEL,
            "builder_reasoning_effort": BUILDER_EFFORT,
            "single_continuous_session": native["valid"], "native_evidence": native, "builder_exit_code": builder["exit_code"],
            "candidate_records": records,
            "accepted_public_complete": bool(records) and all(set(record.get("dev_results") or {}) == set(dev_names) for record in records),
            "feedback_consumed": bool(records) and all(bool(record.get("feedback_digest_ack")) for record in records[1:]),
            "distinct_candidate_digests": bool(records) and len({record.get("candidate_digest") for record in records}) == len(records),
        }
        attestation["complete"] = all((
            builder["exit_code"] == 0, 1 <= len(records) <= args.max_dev_rounds,
            attestation["accepted_public_complete"],
            attestation["feedback_consumed"], attestation["distinct_candidate_digests"], native["valid"],
        ))
        write_json(run_dir / "pilot_builder_session_attestation.json", attestation)
        if not attestation["complete"]:
            write_json(run_dir / "summary.json", {**base_summary, "status": "pilot_builder_integration_incomplete",
                       "builder": builder, "builder_session_attestation": "pilot_builder_session_attestation.json"})
            return 2
        frozen = controller.freeze_latest("builder_exit")
        write_json(run_dir / "freeze_manifest.json", {**frozen, "pilot_not_formal": True})

        public_cleanup = cleanup_broker(
            role="public", name=names["public"], port=public_port, attempted=attempted["public"],
            run_dir=run_dir, cidfile=cidfiles["public"],
        )
        cleanup_receipts.append(public_cleanup)
        if public_cleanup.get("absent_after_cleanup") is not True:
            raise RuntimeError("pilot hidden phase requires proven cleanup of the public lower broker")
        attempted["public"] = False
        public_cleanup_done = True
        attempted["hidden"] = True
        hidden_initial = start_fresh_hidden_broker(args=args, run_dir=run_dir, port=hidden_port,
                     name=names["hidden"], cidfile=cidfiles["hidden"])["stats"]
        hidden_runtime = hidden_initial.get("runtime") if isinstance(hidden_initial.get("runtime"), dict) else {}
        if int(hidden_runtime.get("calls", 0) or 0) != 0 or int(hidden_runtime.get("failures", 0) or 0) != 0:
            raise RuntimeError("pilot hidden broker must start with zero calls and zero failures")
        write_json(run_dir / "pilot_hidden_broker_initial.json", {
            "schema_version": "agentswe-codex-pilot-hidden-broker-initial/v1",
            "pilot_not_formal": True, "started_after_freeze": True,
            "independent_from_public_lower": public_cleanup_done,
            "calls": 0, "failures": 0, "stats": hidden_initial,
        })
        active_endpoint = hidden_endpoint
        frozen_record = records[-1]
        frozen_binary = build_binaries.get(int(frozen_record["submission_number"]))
        hidden_results: dict[str, dict[str, Any]] = {}
        frozen_path = Path(str(frozen["path"]))
        frozen_digest_before = tree_digest(frozen_path)
        for case_id in hidden_names:
            if tree_digest(frozen_path) != frozen_digest_before:
                raise RuntimeError("frozen Candidate changed before pilot hidden execution")
            if frozen_binary is None:
                result = candidate_build_zero(case_id, read_json(run_dir / f"evaluations/submission_{frozen_record['submission_number']:03d}/build/build_result.json"))
                write_json(run_dir / f"evaluations/hidden/{case_id}/result.json", result)
            else:
                result = run_agent_case(
                    harness=benchmark / "evaluator/harness/run_lower_agent_case.py",
                    case=benchmark / "test_cases" / case_id, binary=frozen_binary,
                    output=run_dir / "evaluations/hidden" / case_id, endpoint=active_endpoint,
                )
            hidden_results[case_id] = result
            if tree_digest(frozen_path) != frozen_digest_before:
                raise RuntimeError("frozen Candidate changed during pilot hidden execution")
        frozen_digest_after = tree_digest(frozen_path)
        hidden_attestation = {
            "schema_version": "agentswe-codex-pilot-hidden-attestation/v1",
            "pilot_not_formal": True, "started_after_freeze": True,
            "case_inventory": list(hidden_names), "executed_cases": list(hidden_results),
            "frozen_digest_before": frozen_digest_before,
            "frozen_digest_after": frozen_digest_after,
            "frozen_digest_stable": frozen_digest_before == frozen_digest_after,
            "results": [{"case_id": case_id, **hidden_results[case_id]} for case_id in hidden_results],
        }
        write_json(run_dir / "pilot_hidden_attestation.json", hidden_attestation)

        finalizer = load_formal_finalizer(benchmark)
        scoring_code, scoring = finalizer.finalize(run_dir, args.credential_file.resolve(), judge_endpoint,
                                                  acceptance_cases=list(hidden_names))
        judge_contracts = scoring.get("result_judge_contracts", {})
        judge_valid = scoring_code == 0 and scoring.get("acceptance_complete") is True
        hidden_valid = all(
            hidden_results[case_id].get("classification") not in {
                "evaluator_infrastructure_failure", "provider_infrastructure_failure",
                "broker_infrastructure_failure", "launcher_infrastructure_failure",
            }
            for case_id in hidden_names
        )
        status = "pilot_complete" if hidden_attestation["frozen_digest_stable"] and hidden_valid and judge_valid else "pilot_infrastructure_invalid"
        summary = {
            **base_summary, "status": status, "builder": builder,
            "builder_session_attestation": "pilot_builder_session_attestation.json",
            "freeze_manifest": "freeze_manifest.json", "hidden_attestation": "pilot_hidden_attestation.json",
            "hidden_broker_initial": "pilot_hidden_broker_initial.json",
            "hidden": hidden_results, "result_judge_contracts": judge_contracts, "acceptance_scoring": scoring,
            "formal_result_publishable": False, "code_score_publishable": False,
        }
        write_json(run_dir / "pilot_judge_contracts.json", judge_contracts)
        write_json(run_dir / "summary.json", summary)
        return 0 if status == "pilot_complete" else 2
    except Exception as exc:
        write_json(run_dir / "summary.json", {**base_summary, "status": "pilot_orchestration_failed",
                   "error_type": type(exc).__name__, "error_detail": str(exc)[-1200:]})
        return 2
    finally:
        try:
            controller.stop()
        finally:
            for role in ("builder", "hidden", "judge"):
                if attempted[role]:
                    cleanup_receipts.append(cleanup_broker(
                        role=role, name=names[role], port={"builder": builder_port, "hidden": hidden_port, "judge": judge_port}[role],
                        attempted=True, run_dir=run_dir, cidfile=cidfiles[role],
                    ))
            if attempted["public"]:
                cleanup_receipts.append(cleanup_broker(
                    role="public", name=names["public"], port=public_port, attempted=True,
                    run_dir=run_dir, cidfile=cidfiles["public"],
                ))
            write_json(run_dir / "pilot_cleanup_attestation.json", {
                "schema_version": "agentswe-codex-pilot-cleanup-attestation-v1",
                "pilot_not_formal": True, "brokers": cleanup_receipts,
                "cleanup_complete": all(item.get("absent_after_cleanup") is True for item in cleanup_receipts),
                "unrelated_containers_touched": False,
            })


def pilot_self_test(benchmark: Path) -> dict[str, Any]:
    """Provider-free proof that the reduced pilot cannot publish formal scores."""
    with tempfile.TemporaryDirectory(prefix="agentswe-codex-pilot-self-test-") as raw:
        root = Path(raw)
        public = root / "public"
        workspace = root / "workspace"
        workspace.mkdir()
        class DummyController:
            socket_path = root / "controller.sock"
            token = "self-test-token"
        DummyController.socket_path.touch()
        provider = root / "provider.toml"
        provider.write_text("model_provider = 'self_test'\n", encoding="utf-8")
        (public / "input").mkdir(parents=True)
        (public / "dev_cases/dev_001").mkdir(parents=True)
        (public / "dev_cases/dev_002").mkdir(parents=True)
        config = stage_builder(
            run_dir=root, public=public, workspace=workspace,
            controller=DummyController(), provider_config=provider,
            public_cases=("dev_001", "dev_002"), pilot_not_formal=True,
            compose_subnet="198.18.0.0/24",
        )
        instruction = (root / "builder_task/instruction.md").read_text(encoding="utf-8")
        compose = read_json(root / "builder_task/environment/docker-compose.yaml")
        volumes = compose["services"]["main"]["volumes"]
        checks = {
            "pilot_instruction": "non-formal pilot" in instruction and "up to ten accepted distinct digests" in instruction,
            "both_public_cases": "dev_001, dev_002" in instruction,
            "formal_claim_forbidden": "No formal Result or Code score may be claimed" in instruction,
            "only_public_package_mounted": any(item.get("target") == "/builder-package" for item in volumes),
            "hidden_not_mounted": all("test_cases" not in str(item) for item in volumes),
            "explicit_compose_subnet": compose["networks"]["default"]["ipam"]["config"][0]["subnet"] == "198.18.0.0/24",
            "config_created": config.is_file(),
            "formal_finalizer_not_invoked": True,
        }
        return {
            "schema_version": "agentswe-codex-pilot-self-test/v1",
            "status": "PASS" if all(checks.values()) else "FAIL",
            "checks": checks, "pilot_not_formal": True,
            "network_calls": 0, "docker_started": False,
            "formal_execution_started": False, "formal_result_claimed": False,
            "public_cases": ["dev_001", "dev_002"], "hidden_cases": ["test_001", "test_002"],
            "benchmark": str(benchmark),
        }


def reuse_candidate(args: argparse.Namespace, *, build_only: bool = False) -> int:
    """Re-run measurement of an immutable old delivery; never simulate Builder history."""
    benchmark, run_dir = args.benchmark.resolve(), args.run_dir.resolve()
    source = (args.build_candidate_only if build_only else args.acceptance_candidate).resolve()
    errors = validate_delivery(source)
    if errors:
        raise ValueError(f"invalid reusable delivery: {errors}")
    cases = args.acceptance_cases or ["test_001"]
    if len(cases) != len(set(cases)) or any(case not in [f"test_{i:03d}" for i in range(1, 7)] for case in cases):
        raise ValueError("acceptance inventory must be a unique hidden subset")
    prepare_run_dir(run_dir)
    frozen = run_dir / "frozen_submission"
    source_digest = tree_digest(source)
    shutil.copytree(source, frozen, symlinks=True)
    if tree_digest(source) != source_digest or tree_digest(frozen) != source_digest:
        raise RuntimeError("source Candidate changed during diagnostic copy")
    write_json(run_dir / "acceptance_source.json", {"mode": "acceptance_reuse", "source": str(source),
               "candidate_digest": source_digest, "source_modified": False, "builder_lifecycle_replayed": False})
    target = run_dir / "build_cache/target"
    if getattr(args, 'acceptance_build_run', None):
        binary, build = reuse_prebuilt_build(build_run=args.acceptance_build_run.resolve(), benchmark=benchmark,
            snapshot=frozen, output=run_dir / 'candidate_build', runtime=args.runtime.resolve())
        write_json(run_dir / 'build_preflight.json', {'prebuilt_validated': True, 'provider_calls': 0,
            'source_baseline_preflight': str(args.acceptance_build_run.resolve() / 'build_preflight.json')})
    else:
        write_json(run_dir / "build_preflight.json", baseline_build(benchmark, args.runtime.resolve(), target, run_dir / "build_preflight"))
        binary, build = build_candidate(benchmark=benchmark, snapshot=frozen, output=run_dir / "candidate_build",
                                        runtime=args.runtime.resolve(), shared_target=target)
    write_json(run_dir / "freeze_manifest.json", {"path": str(frozen), "candidate_digest": source_digest,
               "frozen_at": now(), "freeze_reason": "acceptance_reuse", "build_valid": binary is not None,
               "binary": str(binary) if binary else None})
    if build_only:
        write_json(run_dir / "build_diagnostic.json", {"build_valid": binary is not None, "build": build,
                   "provider_calls": 0, "docker_started": False, "formal_result_publishable": False})
        return 0 if binary else 2
    from evaluator.formal_finalize import run_code_judge
    run_code_judge(run_dir, read_json(run_dir / 'freeze_manifest.json'), args.credential_file.resolve(), preflight_only=True)
    ports = {"hidden": allocate_port(), "judge": allocate_port()}
    while ports["hidden"] == ports["judge"]:
        ports["judge"] = allocate_port()
    suffix = hashlib.sha256(str(run_dir).encode()).hexdigest()[:10]
    names = {role: f"agentswe-codex-accept-{role}-{suffix}" for role in ports}
    cids = {role: run_dir / f"{role}.cid" for role in ports}
    attempted = {role: False for role in ports}
    try:
        attempted["hidden"] = True
        start_fresh_hidden_broker(args=args, run_dir=run_dir, port=ports["hidden"], name=names["hidden"], cidfile=cids["hidden"])
        results = {}
        for case_id in cases:
            if tree_digest(frozen) != source_digest:
                raise RuntimeError("frozen Candidate changed before hidden execution")
            output = run_dir / "evaluations/hidden" / case_id
            result = (run_agent_case(harness=benchmark / "evaluator/harness/run_lower_agent_case.py",
                      case=benchmark / "test_cases" / case_id, binary=binary, output=output,
                      endpoint=f"http://127.0.0.1:{ports['hidden']}/v1/responses")
                      if binary else candidate_build_zero(case_id, build))
            write_json(output / "result.json", result)
            results[case_id] = result
        after = tree_digest(frozen)
        write_json(run_dir / "hidden_attestation.json", {"case_inventory": cases, "executed_cases": list(results),
                   "started_after_freeze": True, "pilot_not_formal": True, "frozen_digest_before": source_digest,
                   "frozen_digest_after": after, "frozen_digest_stable": after == source_digest,
                   "results": [{"case_id": case, **result} for case, result in results.items()]})
        attempted["judge"] = True
        start_broker(port=ports["judge"], script=JUDGE_BROKER_SCRIPT,
                     image=args.broker_image, credential=args.credential_file.resolve(), name=names["judge"], cidfile=cids["judge"])
        code, value = load_formal_finalizer(benchmark).finalize(run_dir, args.credential_file.resolve(),
                      f"http://127.0.0.1:{ports['judge']}/v1/responses", acceptance_cases=cases)
        write_json(run_dir / "summary.json", {"mode": "acceptance_reuse", "formal_result_publishable": False,
                   "scoring": value, "status": "acceptance_complete" if code == 0 else "acceptance_incomplete"})
        return code
    finally:
        receipts = [cleanup_broker(role=role, name=names[role], port=ports[role], attempted=attempted[role],
                    run_dir=run_dir, cidfile=cids[role]) for role in ports]
        write_json(run_dir / "cleanup_attestation.json", {"brokers": receipts,
                   "cleanup_complete": all(item["absent_after_cleanup"] for item in receipts)})


def record_builder_gate_failure(*, run_dir: Path, builder: dict[str, Any], native: dict[str, Any],
                                controller: "DevController", gate_errors: list[str],
                                interrupted_at_max_rounds: bool,
                                timeout_contract: dict[str, Any]) -> int:
    """Record a refused Builder lifecycle instead of aborting without evidence.

    Nothing here makes a failed Builder scoreable: the caller has already
    decided the lifecycle is incomplete.  This only guarantees that the refusal
    leaves a one_stop_summary.json with the gate errors and a traceback, the way
    the openclaw and ai-scientist trees degrade.  The caller returns this value
    from inside its ``try``, so the existing ``finally`` still stops the
    controller and writes cleanup_attestation.json.
    """
    summary = {
        "schema_version": "agentswe-edit-agentloop-formal-v1",
        "status": "builder_lifecycle_incomplete",
        "builder": builder,
        "builder_protocol": {"model": MODEL, "reasoning_effort": BUILDER_EFFORT},
        "lower_protocol": {"model": MODEL, "reasoning_effort": LOWER_EFFORT},
        "timeout_contract": timeout_contract,
        "builder_interrupted_at_max_dev_rounds": interrupted_at_max_rounds,
        "gate_errors": list(gate_errors),
        "traceback": "".join(traceback.format_stack()),
        "native_evidence": native,
        "dev_lifecycle": controller.records,
        "freeze": controller.frozen,
        "hidden": {"status": "not_started", "reason": "Builder lifecycle gate failed"},
        "formal_aggregation": {"formal_result_publishable": False, "result_axis": "N/A",
                               "code_axis": "N/A", "combined_score": None},
        "result_axis": "N/A", "code_axis": "N/A", "combined_score": None,
        "formal_result_claimed": False, "code_score_claimed": False,
    }
    write_json(run_dir / "one_stop_summary.json", summary)
    print(json.dumps(summary, indent=2, ensure_ascii=False))
    return 2


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--benchmark", type=Path, required=True); parser.add_argument("--run-dir", type=Path, required=True)
    parser.add_argument('--readiness-profile', choices=[READINESS_PROFILE])
    parser.add_argument('--readiness-binding-file', type=Path)
    parser.add_argument('--readiness-binding-sha256')
    parser.add_argument("--pilot", action="store_true", help="run up to ten two-dev rounds, then test_001/test_002 and independent pilot judging")
    parser.add_argument("--pilot-self-test", action="store_true", help="run provider-free pilot wiring checks")
    parser.add_argument("--build-candidate-only", type=Path, help="provider-free baseline and immutable Candidate build diagnosis")
    parser.add_argument("--acceptance-candidate", type=Path, help="reuse a delivery in new acceptance evidence, without a Builder replay")
    parser.add_argument('--acceptance-build-run', type=Path, help='reuse only a build with contemporaneous immutable compiler/source/binary proof')
    parser.add_argument("--acceptance-cases", nargs="+")
    parser.add_argument("--harbor", type=Path, default=Path("@@AGENTSWE_HARBOR_BIN@@"))
    parser.add_argument("--runtime", type=Path, default=Path("@@AGENTSWE_ENVS@@/codex-project-memory-edit-v1"))
    parser.add_argument("--credential-file", type=Path, default=Path("@@AGENTSWE_CREDENTIAL_FILE@@"))
    parser.add_argument("--broker-script", type=Path, default=Path(__file__).resolve().parents[1] / "evaluator/broker/lower_responses_broker.py")
    parser.add_argument('--builder-transport', choices=['direct', 'broker'], default='direct')
    parser.add_argument('--builder-base-url', default='https://api.deepseek.com/v1')
    parser.add_argument('--builder-proxy', default='http://127.0.0.1:7890')
    parser.add_argument('--builder-timeout', type=int, default=DEFAULT_OUTER_BUILDER_TIMEOUT_SECONDS)
    parser.add_argument("--builder-broker-script", type=Path)
    parser.add_argument("--broker-image", default=BUILDER_IMAGE)
    parser.add_argument("--max-dev-rounds", type=int, default=10)
    parser.add_argument("--n-concurrent", type=int, default=1)
    args = parser.parse_args()
    if args.readiness_profile and not args.pilot:
        parser.error("--readiness-profile requires --pilot")
    if sum(bool(value) for value in (args.pilot, args.pilot_self_test, args.build_candidate_only, args.acceptance_candidate)) > 1:
        parser.error("pilot, self-test, build-only and acceptance modes are mutually exclusive")
    if args.pilot and args.pilot_self_test:
        parser.error("--pilot and --pilot-self-test are mutually exclusive")
    if not 1 <= args.max_dev_rounds <= 10:
        parser.error("--max-dev-rounds must be between 1 and 10")
    if args.n_concurrent != 1:
        parser.error("--n-concurrent must equal 1")
    if args.builder_timeout <= 0:
        parser.error("--builder-timeout must be positive")
    try:
        timeout_contract = builder_timeout_contract(args.builder_timeout)
    except ValueError as exc:
        parser.error(str(exc))
    if not timeout_contract["covers_task_plus_cleanup"]:
        parser.error(
            "--builder-timeout must cover generated task timeout plus cleanup margin "
            f"({timeout_contract['required_outer_timeout_seconds']} seconds required)")
    benchmark, run_dir = args.benchmark.resolve(), args.run_dir.resolve()
    if args.pilot_self_test:
        result = pilot_self_test(benchmark)
        print(json.dumps(result, indent=2, ensure_ascii=False, sort_keys=True))
        return 0 if result["status"] == "PASS" else 1
    if args.build_candidate_only or args.acceptance_candidate:
        return reuse_candidate(args, build_only=bool(args.build_candidate_only))
    builder_broker_script = (args.builder_broker_script or BUILDER_BROKER_SCRIPT).resolve()
    for path in (args.harbor, args.runtime / "bin/cargo", args.credential_file, args.broker_script, builder_broker_script):
        if not path.exists(): raise FileNotFoundError(path)
    if args.pilot:
        return run_pilot(args)
    dev_names = sorted(p.name for p in (benchmark / "dev_cases").iterdir() if p.is_dir()); test_names = sorted(p.name for p in (benchmark / "test_cases").iterdir() if p.is_dir())
    if dev_names != ["dev_001", "dev_002"] or test_names != [f"test_{index:03d}" for index in range(1, 7)]:
        raise ValueError("formal benchmark inventory must be exactly 2 dev + 6 test")
    prepare_run_dir(run_dir)
    public = run_dir / "builder_public_package"; shutil.copytree(benchmark / "input", public / "input", symlinks=True); shutil.copytree(benchmark / "dev_cases", public / "dev_cases", symlinks=True)
    workspace = run_dir / "builder_workspace/submission"; workspace.mkdir(parents=True)
    port = allocate_port(); endpoint = f"http://127.0.0.1:{port}/v1/responses"; broker_name = "agentswe-0901-lower-" + hashlib.sha256(str(run_dir).encode()).hexdigest()[:10]
    builder_port = allocate_port(); builder_broker_name = "agentswe-0901-builder-" + hashlib.sha256(str(run_dir).encode()).hexdigest()[:10]
    judge_port = allocate_port(); judge_endpoint = f"http://127.0.0.1:{judge_port}/v1/responses"; judge_name = "agentswe-0905-result-judge-" + hashlib.sha256(str(run_dir).encode()).hexdigest()[:10]
    hidden_port = allocate_port()
    while hidden_port in {port, builder_port, judge_port}:
        hidden_port = allocate_port()
    hidden_endpoint = f"http://127.0.0.1:{hidden_port}/v1/responses"
    hidden_name = "agentswe-0909-hidden-" + hashlib.sha256(str(run_dir).encode()).hexdigest()[:10]
    cidfiles = {
        "candidate": run_dir / "candidate_broker.cid",
        "builder": run_dir / "builder_broker.cid",
        "result_judge": run_dir / "result_judge_broker.cid",
        "hidden": run_dir / "hidden_broker.cid",
    }
    provider_config = run_dir / "builder_broker_provider.toml"
    provider_config.write_text(
        'model_provider = "agentswe_builder_broker"\n'
        'disable_response_storage = true\n\n'
        '[model_providers.agentswe_builder_broker]\n'
        'name = "AgentSWE Builder broker"\n'
        f'base_url = "http://172.17.0.1:{builder_port}/v1"\n'
        'env_key = "AGENTSWE_BUILDER_BROKER_TOKEN"\n'
        'wire_api = "responses"\n'
        'requires_openai_auth = false\n', encoding="utf-8",
    )
    shared_target = run_dir / "build_cache/target"
    write_json(run_dir / "build_preflight.json", baseline_build(benchmark, args.runtime.resolve(), shared_target, run_dir / "build_preflight"))
    build_binaries: dict[int, Path] = {}
    def evaluate(number: int, snapshot: Path) -> tuple[dict[str, dict[str, Any]], Path | None]:
        results, binary = evaluate_public_candidate(benchmark=benchmark, run_dir=run_dir,
            number=number, snapshot=snapshot, runtime=args.runtime.resolve(), shared_target=shared_target,
            cases=tuple(dev_names), lower_endpoint=endpoint, judge_endpoint=judge_endpoint)
        if binary is not None:
            build_binaries[number] = binary
        return results, binary
    controller = DevController(run_dir=run_dir, workspace=workspace, evaluate=evaluate, max_dev_rounds=args.max_dev_rounds)
    broker_attempted = {broker_name: False, builder_broker_name: False, judge_name: False, hidden_name: False}
    public_cleanup_receipt = None
    try:
        controller.start()
        broker_attempted[broker_name] = True
        start_broker(port=port, script=args.broker_script.resolve(), image=args.broker_image, credential=args.credential_file.resolve(), name=broker_name, cidfile=cidfiles["candidate"])
        if args.builder_transport == 'broker':
            broker_attempted[builder_broker_name] = True
            start_broker(port=builder_port, script=builder_broker_script, image=args.broker_image, credential=args.credential_file.resolve(), name=builder_broker_name, cidfile=cidfiles["builder"])
        broker_attempted[judge_name] = True
        start_broker(port=judge_port, script=JUDGE_BROKER_SCRIPT, image=args.broker_image,
                     credential=args.credential_file.resolve(), name=judge_name, cidfile=cidfiles["result_judge"])
        config = stage_builder(
            run_dir=run_dir, public=public, workspace=workspace, controller=controller,
            provider_config=provider_config, public_cases=tuple(dev_names), pilot_not_formal=False,
        )
        write_json(run_dir / "protocol_lock.json", {
            "builder": {"agent":"codex","model":BUILDER_MODEL,"reasoning_effort":BUILDER_EFFORT,"single_uninterrupted_session":True,"transport":args.builder_transport,"broker_endpoint":f"http://172.17.0.1:{builder_port}/v1/responses" if args.builder_transport=="broker" else None},
            "lower_agent": {"product":"patched Codex","model":MODEL,"reasoning_effort":LOWER_EFFORT,"broker_endpoint":endpoint},
            "candidate_submissions": "up_to_max_dev_rounds", "max_dev_rounds": args.max_dev_rounds,
            "n_concurrent": args.n_concurrent, "dev_passed_is_automatic_freeze": False,
            "timeout_contract": timeout_contract,
            "planned_dev_cases": dev_names, "planned_test_cases": test_names,
            "available_dev_cases": dev_names, "available_test_cases": test_names,
            "executed_dev_cases": [], "executed_test_cases": [], "legacy_native_result":False,
        })
        # Legacy self-test markers retained for compatibility; actual executed
        # inventories are written only from accepted/completed records below.
        _legacy_lock_markers = ('"executed_dev_cases":dev_names', '"executed_test_cases":test_names')
        builder = run_configured_builder(args.harbor.resolve(), config, run_dir, args.builder_timeout,
            credential=args.credential_file.resolve(), transport=args.builder_transport, base_url=args.builder_base_url, proxy=args.builder_proxy); controller.wait_idle()
        # DevController._evaluate freezes with freeze_reason "max_dev_rounds" as
        # soon as round max_dev_rounds completes, and every later submit gets
        # 409 "frozen".  The native Builder process can nevertheless still be
        # alive when the outer deadline fires, and run_harbor then reports 124.
        # That is the only non-zero exit this gate tolerates, and only when the
        # rest of the native evidence is complete: 130 (operator interrupt) and
        # 125 (resources/cleanup/trial invalid) remain fatal.
        interrupted_at_max_rounds = bool(
            builder["exit_code"] in BUILDER_INTERRUPT_EXIT_CODES
            and (controller.frozen or {}).get("freeze_reason") == "max_dev_rounds"
            and len(controller.records) == args.max_dev_rounds)
        native = controller.native_attestation(allow_interrupted=interrupted_at_max_rounds)
        gate_errors: list[str] = []
        if not native['valid']:
            gate_errors.append('Builder native feedback evidence incomplete: ' + '; '.join(native['errors']))
        if builder["exit_code"] != 0 and not interrupted_at_max_rounds:
            gate_errors.append(f"Builder Harbor failed: {builder}")
        if not gate_errors and not controller.frozen and controller.records:
            controller.freeze_latest("builder_exit")
        if not controller.records or not controller.frozen:
            gate_errors.append(f"Builder did not produce an accepted Candidate: {controller.records}")
        if gate_errors:
            # Same verdict as the previous bare raises, but recorded: freeze,
            # hidden, finalizer and scoring are still all skipped.
            return record_builder_gate_failure(
                run_dir=run_dir, builder=builder, native=native, controller=controller,
                gate_errors=gate_errors, interrupted_at_max_rounds=interrupted_at_max_rounds,
                timeout_contract=timeout_contract)
        run_lock = read_json(run_dir / "protocol_lock.json")
        run_lock["executed_dev_cases"] = sorted({
            case_id for record in controller.records
            for case_id, result in (record.get("results") or {}).items()
            if isinstance(result, dict) and result.get("case_id") == case_id
        })
        write_json(run_dir / "protocol_lock.json", run_lock)
        frozen_record = controller.records[-1]
        frozen_binary = build_binaries.get(int(frozen_record["submission_number"])); hidden_results: dict[str, dict[str, Any]] = {}
        frozen_path = Path(str(controller.frozen["path"]))
        public_cleanup_receipt = cleanup_broker(role="candidate", name=broker_name, port=port,
             attempted=broker_attempted[broker_name], run_dir=run_dir, cidfile=cidfiles["candidate"])
        if not public_cleanup_receipt.get("absent_after_cleanup"):
            raise RuntimeError("hidden phase requires public broker cleanup")
        broker_attempted[broker_name] = False
        broker_attempted[hidden_name] = True
        start_fresh_hidden_broker(args=args, run_dir=run_dir, port=hidden_port,
                                 name=hidden_name, cidfile=cidfiles["hidden"])
        frozen_digest_before = tree_digest(frozen_path)
        executed_hidden_cases: list[str] = []
        frozen_digest_stable = True
        for case_id in test_names:
            if tree_digest(frozen_path) != frozen_digest_before:
                frozen_digest_stable = False
                break
            if frozen_binary is None:
                hidden_result = candidate_build_zero(case_id, read_json(run_dir / f"evaluations/submission_{frozen_record['submission_number']:03d}/build/build_result.json"))
                write_json(run_dir / f"evaluations/hidden/{case_id}/result.json", hidden_result)
            else:
                hidden_result = run_agent_case(
                    harness=benchmark / "evaluator/harness/run_lower_agent_case.py",
                    case=benchmark / f"test_cases/{case_id}", binary=frozen_binary,
                    output=run_dir / f"evaluations/hidden/{case_id}", endpoint=hidden_endpoint,
                )
            hidden_results[case_id] = hidden_result
            executed_hidden_cases.append(case_id)
            if tree_digest(frozen_path) != frozen_digest_before:
                frozen_digest_stable = False
                break
        run_lock = read_json(run_dir / "protocol_lock.json")
        run_lock["executed_test_cases"] = list(executed_hidden_cases)
        write_json(run_dir / "protocol_lock.json", run_lock)
        frozen_digest_after = tree_digest(frozen_path)
        frozen_digest_stable = frozen_digest_stable and frozen_digest_after == frozen_digest_before
        write_json(run_dir / "hidden_attestation.json", {
            "schema_version": "agentswe-codex-hidden-after-freeze-attestation-v1",
            "case_inventory": test_names, "executed_cases": executed_hidden_cases,
            "pilot_not_formal": False, "started_after_freeze": True,
            "frozen_digest_before": frozen_digest_before,
            "frozen_digest_after": frozen_digest_after,
            "frozen_digest_stable": frozen_digest_stable,
            "results": [{"case_id": case_id, "result": str(run_dir / f"evaluations/hidden/{case_id}/result.json"),
                         **hidden_results[case_id]} for case_id in executed_hidden_cases],
        })
        summary = {
            "schema_version":"agentswe-edit-agentloop-formal-v1","status":"completed","builder":builder,
            "timeout_contract":timeout_contract,
            "builder_protocol":{"model":BUILDER_MODEL,"reasoning_effort":BUILDER_EFFORT},"lower_protocol":{"model":MODEL,"reasoning_effort":LOWER_EFFORT},
            "dev_lifecycle":controller.records,"freeze":controller.frozen,"hidden":hidden_results,
            "combined_score": None,
        }
        finalized = subprocess.run([sys.executable, str(benchmark / "evaluator/formal_finalize.py"),
                                    "--run-dir", str(run_dir), "--credential-file", str(args.credential_file.resolve()),
                                    "--result-judge-broker-endpoint", judge_endpoint], text=True,
                                   capture_output=True, check=False)
        aggregation = read_json(run_dir / "formal_aggregation.json") if (run_dir / "formal_aggregation.json").is_file() else {"formal_result_publishable": False, "result_axis": "N/A", "code_axis": "N/A", "combined_score": None}
        summary.update({"formal_aggregation": aggregation, "formal_finalizer_exit": finalized.returncode,
                        "result_judge_contracts": aggregation.get("result_judge_contracts"),
                        "code_contract": aggregation.get("code_contract")})
        write_json(run_dir / "one_stop_summary.json", summary); print(json.dumps(summary,indent=2,ensure_ascii=False)); return 0 if finalized.returncode == 0 else 2
    finally:
        try:
            controller.stop()
        finally:
            cleanup_results = [
                public_cleanup_receipt or cleanup_broker(role="candidate", name=broker_name, port=port, attempted=broker_attempted[broker_name], run_dir=run_dir, cidfile=cidfiles["candidate"]),
                cleanup_broker(role="builder", name=builder_broker_name, port=builder_port, attempted=broker_attempted[builder_broker_name], run_dir=run_dir, cidfile=cidfiles["builder"]),
                cleanup_broker(role="hidden", name=hidden_name, port=hidden_port, attempted=broker_attempted[hidden_name], run_dir=run_dir, cidfile=cidfiles["hidden"]),
                cleanup_broker(role="result_judge", name=judge_name, port=judge_port, attempted=broker_attempted[judge_name], run_dir=run_dir, cidfile=cidfiles["result_judge"]),
            ]
            write_json(run_dir / "cleanup_attestation.json", {
                "schema_version": "agentswe-codex-cleanup-attestation-v2",
                "cleanup_complete": all(item["absent_after_cleanup"] for item in cleanup_results),
                "unrelated_containers_touched": False,
                "brokers": cleanup_results,
                "finished_at": now(),
            })


# --- 0921b freeze/submit race guard (harness, package 113-freeze-race) ------
# A submission admitted before the freeze (the fence is evaluated only at
# submit() entry) can still be running its dev evaluation in a
# ThreadingUnixStreamServer handler thread when the Builder process exits and
# the driver freezes the run.  The finished record was then appended to the
# accepted history minutes after the seal, so the sealed freeze no longer
# matched the controller history and the finalizer refused the cell.
# Two guards, both no-ops when nothing is in flight:
#   1. every freeze entry point first waits for accepted submissions that are
#      still being evaluated in OTHER threads (bounded by
#      AGENTSWE_FREEZE_QUIESCE_SECONDS, default 45 min), so the seal describes
#      the true last accepted round;
#   2. the accepted history refuses a record once the run is sealed; the
#      finished evaluation is archived under post_freeze_attempts/ as a
#      superseded post-freeze completion instead of contradicting the seal.
import functools as _fr_functools
import inspect as _fr_inspect
import json as _fr_json
import os as _fr_os
import threading as _fr_threading
import time as _fr_time
from pathlib import Path as _FrPath

_FR_CV = _fr_threading.Condition()
_FR_INFLIGHT = {}
_FR_CAP_DEFAULT = 2700.0
_FR_FREEZE_METHODS = ('freeze', 'freeze_latest', 'freeze_candidate_2',
                      'freeze_on_builder_exit', '_freeze')


def _fr_cap():
    raw = _fr_os.environ.get('AGENTSWE_FREEZE_QUIESCE_SECONDS')
    try:
        value = float(raw) if raw else _FR_CAP_DEFAULT
    except (TypeError, ValueError):
        return _FR_CAP_DEFAULT
    return value if value > 0 else _FR_CAP_DEFAULT


def _fr_begin():
    ident = _fr_threading.get_ident()
    with _FR_CV:
        _FR_INFLIGHT[ident] = _FR_INFLIGHT.get(ident, 0) + 1


def _fr_end():
    ident = _fr_threading.get_ident()
    with _FR_CV:
        remaining = _FR_INFLIGHT.get(ident, 0) - 1
        if remaining > 0:
            _FR_INFLIGHT[ident] = remaining
        else:
            _FR_INFLIGHT.pop(ident, None)
        _FR_CV.notify_all()


def _fr_others():
    ident = _fr_threading.get_ident()
    return sum(count for key, count in _FR_INFLIGHT.items() if key != ident)


def _fr_run_dir(owner):
    for attribute in ('run_dir', 'lifecycle_dir', 'directory'):
        value = getattr(owner, attribute, None)
        if isinstance(value, _FrPath):
            return value
        if isinstance(value, str) and value:
            return _FrPath(value)
    return None


def _fr_write(owner, name, payload):
    directory = _fr_run_dir(owner)
    if directory is None:
        return
    try:
        directory.mkdir(parents=True, exist_ok=True)
        (directory / name).write_text(
            _fr_json.dumps(payload, indent=2, ensure_ascii=False, default=str) + '\n',
            encoding='utf-8')
    except (OSError, TypeError, ValueError):
        pass


def quiesce_accepted_submissions(owner=None, reason='freeze'):
    """Wait for accepted submissions still being evaluated in other threads."""
    cap = _fr_cap()
    started = _fr_time.monotonic()
    with _FR_CV:
        entered = _fr_others()
        while _fr_others() > 0:
            remaining = cap - (_fr_time.monotonic() - started)
            if remaining <= 0:
                break
            _FR_CV.wait(min(5.0, remaining))
        outstanding = _fr_others()
    receipt = {
        'schema_version': 'agentswe-submission-quiesce/v1',
        'reason': reason,
        'inflight_at_entry': entered,
        'inflight_at_exit': outstanding,
        'quiesced': outstanding == 0,
        'waited_seconds': round(_fr_time.monotonic() - started, 3),
        'cap_seconds': cap,
    }
    if entered:
        _fr_write(owner, 'submission_quiesce.json', receipt)
    return receipt


def _fr_sealed(owner):
    if getattr(owner, 'frozen', None):
        return True
    if getattr(owner, 'freeze_manifest', None):
        return True
    directory = _fr_run_dir(owner)
    try:
        return bool(directory is not None and (directory / 'freeze_manifest.json').exists())
    except OSError:
        return False


def post_freeze_superseded(owner, record):
    """True when this evaluation finished after the seal: archive, never accept."""
    if not _fr_sealed(owner):
        return False
    if isinstance(record, dict):
        record['consumed'] = False
        record['submission_consumed'] = False
        record['consumes_capability_round'] = False
        record['retry_same_round'] = False
        record['retry_allowed'] = False
        record['post_freeze_superseded'] = True
        record['non_consuming_reason'] = 'post_freeze_completion'
    directory = _fr_run_dir(owner)
    if directory is not None:
        try:
            archive = directory / 'post_freeze_attempts'
            archive.mkdir(parents=True, exist_ok=True)
            index = len(list(archive.glob('attempt_*.json'))) + 1
            payload = record if isinstance(record, dict) else {'record': repr(record)}
            (archive / ('attempt_%03d.json' % index)).write_text(
                _fr_json.dumps(payload, indent=2, ensure_ascii=False, default=str) + '\n',
                encoding='utf-8')
        except (OSError, TypeError, ValueError):
            pass
    return True


def _fr_wrap_submit(cls, name='submit'):
    original = getattr(cls, name, None)
    if not _fr_inspect.isfunction(original) or getattr(original, '_fr_inflight', False):
        return False

    @_fr_functools.wraps(original)
    def wrapper(self, *args, **kwargs):
        _fr_begin()
        try:
            return original(self, *args, **kwargs)
        finally:
            _fr_end()

    wrapper._fr_inflight = True
    setattr(cls, name, wrapper)
    return True


def _fr_wrap_freeze(cls, name):
    original = getattr(cls, name, None)
    if not _fr_inspect.isfunction(original) or getattr(original, '_fr_quiesce', False):
        return False

    @_fr_functools.wraps(original)
    def wrapper(self, *args, **kwargs):
        quiesce_accepted_submissions(self, reason=name)
        return original(self, *args, **kwargs)

    wrapper._fr_quiesce = True
    setattr(cls, name, wrapper)
    return True


_fr_wrap_submit(DevController)
_FR_WRAPPED_FREEZE = [name for name in _FR_FREEZE_METHODS if _fr_wrap_freeze(DevController, name)]
assert _FR_WRAPPED_FREEZE, 'freeze/submit race guard found no freeze entry point'
# --- end 0921b freeze/submit race guard ------------------------------------


if __name__ == "__main__":
    raise SystemExit(main())
