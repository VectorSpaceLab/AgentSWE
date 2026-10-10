#!/usr/bin/env python3
"""Real same-session Builder lifecycle for the OpenHands Edit benchmark.

This is an explicit-cost entry point.  The default invocation performs only a
provider-free static audit.  Nothing external is contacted unless
``--run-formal`` is supplied. One Harbor/Codex Builder process may submit up to
ten distinct accepted deliveries over a token-protected Unix socket. Every
accepted digest runs both public cases and returns feedback to that process.
Duplicate digests are idempotent, infrastructure-invalid attempts do not
consume rounds, and max rounds or Builder exit freezes the latest valid snapshot.
"""
from __future__ import annotations

import argparse
import hashlib
import ipaddress
import json
import os
import shutil
import signal
import socket
import socketserver
import stat
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

from typing import Any


MODEL = "deepseek-flash"
BUILDER_MODEL = "deepseek-flash"  # upper Builder (Codex harness) only
BUILDER_EFFORT = "max"
LOWER_EFFORT = "high"
BUILDER_IMAGE = "agentswe/create-agent-builder-codex:0.144.1-node24.6.0-0812"
# 5 h Builder cap (D2, 2026-09-19).  The shared driver passes an outer
# --builder-timeout of 18000 s; the generated task.toml budget must stay below
# it by the cleanup margin, otherwise Harbor is killed mid-agent and the run
# reports 124 rather than a Builder exit.
GENERATED_BUILDER_TASK_TIMEOUT_SECONDS = 17_880
BUILDER_CLEANUP_MARGIN_SECONDS = 120
# A submission holds the lifecycle lock while it materializes the Candidate and
# runs both public dev cases, which is far longer than the Builder's shell-tool
# window.  A read-only action must answer inside that window instead of queueing
# behind the submission, so it waits this long for the lock and then falls back
# to a records snapshot.
READ_ONLY_ACTION_LOCK_TIMEOUT_SECONDS = 20
DEFAULT_OUTER_BUILDER_TIMEOUT_SECONDS = (
    GENERATED_BUILDER_TASK_TIMEOUT_SECONDS + BUILDER_CLEANUP_MARGIN_SECONDS
)
BUILDER_BROKER_SCRIPT = Path(
    "@@AGENTSWE_EDITING_CONTROL@@/builder_broker_xhigh.py"
)
ROOT = Path(__file__).resolve().parents[1]
SHARED_RESULT_JUDGE = Path("@@AGENTSWE_EDITING_CONTROL@@/result_judge.py")
CREATE_CODE_JUDGE = Path("@@AGENTSWE_EDITING_CONTROL@@/code_judge_runner.py")
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from evaluator.controller.two_round_controller import TwoRoundController, read_json, tree_digest, write_json  # noqa: E402
from harbor.stage_public_package import stage as stage_public_package  # noqa: E402
from harbor.native_builder_evidence import observe_thread, verify_native
from harbor.public_feedback import public_payload
from evaluator.controller.product_intents import ProductIntents
from adapters.materialize_candidate import patch_paths as submission_patch_paths
from adapters.materialize_candidate import validate as validate_submission
from harbor.builder_dependencies import prepare as prepare_builder_dependencies
from harbor.readiness_preflight import PROFILE as READINESS_PROFILE, load_and_verify_binding


DIRECT_BUILDER_SCRIPT = Path("@@AGENTSWE_EDITING_CONTROL@@/direct_harbor_builder.py")


def builder_timeout_contract(effective_outer_timeout: int) -> dict[str, Any]:
    """Prove the outer deadline covers the generated task and cleanup margin."""
    if isinstance(effective_outer_timeout, bool) or effective_outer_timeout <= 0:
        raise ValueError("effective outer Builder timeout must be a positive integer")
    required = GENERATED_BUILDER_TASK_TIMEOUT_SECONDS + BUILDER_CLEANUP_MARGIN_SECONDS
    return {
        "generated_task_timeout_seconds": GENERATED_BUILDER_TASK_TIMEOUT_SECONDS,
        "cleanup_margin_seconds": BUILDER_CLEANUP_MARGIN_SECONDS,
        "effective_outer_timeout_seconds": effective_outer_timeout,
        "covers_task_plus_cleanup": effective_outer_timeout >= required,
        "required_outer_timeout_seconds": required,
    }


def enforce_builder_timeout_contract(parser, effective_outer_timeout: int) -> dict[str, Any]:
    """argparse gate: refuse an outer budget that cannot cover the task.

    Attached to the execution branches only; --self-test, --pilot-self-test and
    the static audit keep working on this file's 1800 s default.
    """
    try:
        contract = builder_timeout_contract(effective_outer_timeout)
    except ValueError as exc:
        parser.error(str(exc))
    if not contract["covers_task_plus_cleanup"]:
        parser.error(
            "--builder-timeout must cover generated task timeout plus cleanup margin "
            f"({contract['required_outer_timeout_seconds']} seconds required)"
        )
    return contract


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


def direct_builder_runtime():
    if str(DIRECT_BUILDER_SCRIPT.parent) not in sys.path:
        sys.path.insert(0, str(DIRECT_BUILDER_SCRIPT.parent))
    import direct_harbor_builder
    return direct_harbor_builder


def run_native_builder(*, lifecycle, config: Path, credential: Path,
                       harbor: Path, timeout: int = 28800,
                       proxy: str = "http://127.0.0.1:7890"):
    """Actual consumer for user-authorized native direct Builder authentication.

    Lower and Judge remain evaluator-owned. Never reconstruct or retry a
    Builder request here; the unmodified native CLI owns its stream recovery.
    """
    direct = direct_builder_runtime()
    run = lifecycle.run_dir
    composition = run / "builder_task/environment/docker-compose.yaml"
    compose = read_json(composition)
    compose["services"]["main"].get("environment", {}).pop("AGENTSWE_BUILDER_BROKER_TOKEN", None)
    write_json(composition, compose)
    child = None
    from harbor.builder_resources import BuilderResourceObserver
    resource_observer = BuilderResourceObserver(run)
    resource_observer.start()
    try:
        with direct.existing_proxy_for_builder(config, composition, proxy) as proxy_receipt:
            write_json(run / "builder_proxy.json", proxy_receipt)
            with direct.direct_auth(config, credential) as auth_receipt:
                write_json(run / "builder_transport.json", {**auth_receipt,
                    "shared_runtime_sha256": hashlib.sha256(DIRECT_BUILDER_SCRIPT.read_bytes()).hexdigest(),
                    "native_model": BUILDER_MODEL, "native_effort": BUILDER_EFFORT,
                    "outer_timeout_seconds": timeout})
                # 97: one codex session, up to RESUME_CAP + 1 Harbor jobs.
                # See builder_segments_runtime(); the launch loop itself is
                # unchanged and now lives in that shared module.
                def adopt_builder_child(process):
                    nonlocal child
                    child = process
                with builder_segments_runtime().builder_session(
                        run, config, harbor=harbor, observer=resource_observer,
                        write_json=write_json, on_start=adopt_builder_child,
                        env={**os.environ, "PYTHONPATH": str(DIRECT_BUILDER_SCRIPT.parent)}) as session:
                    code = session.run(budget_seconds=timeout)
                    resources = resource_observer.finish()
                    if not resources["valid"]: code = 125
                    gate_proof = {'valid': False, 'errors': []}
                    try:
                        job = json.loads(config.read_text())['job_name']
                        results = list((run / 'jobs' / job).glob('*/result.json'))
                        ledger = builder_segments_runtime().ledger_trials(run)
                        if ledger:
                            # A resumed Builder is ONE session across several
                            # Harbor trials.  Count the evaluator's segments,
                            # not the trials, and gate on the terminal one; the
                            # evidence layer separately proves that every
                            # earlier segment was an infrastructure cut of that
                            # same session.  With no ledger this is the old rule.
                            if sorted(p.parent.name for p in results) != sorted(ledger):
                                raise ValueError('native Harbor trial results disagree with the segment ledger')
                            result = run / 'jobs' / job / ledger[-1] / 'result.json'
                        else:
                            if len(results) != 1:
                                raise ValueError('expected exactly one native Harbor trial result')
                            result = results[0]
                        if any(p.is_symlink() for p in (result, *result.parents)):
                            raise ValueError('Harbor trial result contains a symlink')
                        data = result.read_bytes(); trial = json.loads(data)
                        gate_proof['trial'] = {'path': str(result), 'sha256': hashlib.sha256(data).hexdigest(),
                            **{key: trial.get(key) for key in ('exception_info', 'agent_setup', 'agent_execution')}}
                        if trial.get('exception_info') or not trial.get('agent_setup') or not trial.get('agent_execution'):
                            raise ValueError('Harbor trial failed or never started the native Agent')
                        gate = result.parent / 'agent/builder_resource_gate.json'
                        if gate.is_symlink(): raise ValueError('preagent gate is a symlink')
                        data = gate.read_bytes(); value = json.loads(data)
                        gate_proof['gate'] = {'path': str(gate), 'sha256': hashlib.sha256(data).hexdigest(), 'content': value}
                        if value.get('valid') is not True:
                            raise ValueError('preagent resource gate rejected environment')
                        dependency = result.parent / 'agent/builder_dependency_gate.json'
                        if dependency.is_symlink(): raise ValueError('preagent dependency gate is a symlink')
                        data = dependency.read_bytes(); value = json.loads(data)
                        gate_proof['dependencies'] = {'path': str(dependency), 'sha256': hashlib.sha256(data).hexdigest(), 'content': value}
                        if value.get('valid') is not True:
                            raise ValueError('preagent dependency gate rejected environment')
                        gate_proof['valid'] = True
                    except (OSError, ValueError, TypeError, KeyError) as exc:
                        gate_proof['errors'].append(str(exc))
                    write_json(run / 'builder_preagent_gate_attestation.json', gate_proof)
                    if not gate_proof['valid']: code = 125
                    return subprocess.CompletedProcess(child.args, code)
    finally:
        if child is not None and child.poll() is None:
            os.killpg(child.pid, signal.SIGTERM)
            try: child.wait(timeout=15)
            except subprocess.TimeoutExpired:
                os.killpg(child.pid, signal.SIGKILL); child.wait()
        resource_observer.finish()
        readiness = (run / "readiness_current_binding.json").is_file()
        cleanup = resource_observer.cleanup_owned(process_started=child is not None,
                                                  defer_removal=readiness)
        write_json(run / "builder_native_stats.json", direct.native_stats(run))
        if readiness:
            # Deferred removal reports complete=False by design; what must hold is
            # that every owned container reached a terminal state we can still name.
            if not cleanup.get("retained_terminal") and not cleanup.get("nothing_started"):
                raise RuntimeError("native Builder terminal retention is unproven")
        elif not cleanup["complete"]:
            raise RuntimeError("native Builder container cleanup is unproven")


def now() -> str:
    return datetime.now(timezone.utc).isoformat()


def directory_digest(root: Path) -> str:
    value = hashlib.sha256()
    for path in sorted(root.rglob("*"), key=lambda item: item.relative_to(root).as_posix()):
        relative = path.relative_to(root)
        if "__pycache__" in relative.parts or path.suffix == ".pyc":
            continue
        name = relative.as_posix().encode()
        value.update(len(name).to_bytes(8, "big")); value.update(name)
        if path.is_symlink():
            kind, payload = b"L", os.readlink(path).encode()
        elif path.is_file():
            kind, payload = b"F", path.read_bytes()
        else:
            kind, payload = b"D", b""
        value.update(kind); value.update(len(payload).to_bytes(8, "big")); value.update(payload)
    return value.hexdigest()


def free_port() -> int:
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        return int(listener.getsockname()[1])


def _select_compose_subnet(
    *,
    run_dir: Path,
    existing_subnets: list[str] | None = None,
    host_subnets: list[str] | None = None,
) -> str:
    """Choose a run-stable private /24 outside Docker and host route ranges.

    Harbor's generated Compose project normally asks Docker for another
    default bridge network.  A long sequence of interrupted trials can leave
    those default pools exhausted before the Builder container starts.  Give
    this task its own deterministic subnet instead, while checking current
    Docker networks and host routes when running for real.  The explicit
    network remains the Compose ``default`` network, so Harbor's egress
    sidecar and service wiring are unchanged.
    """
    if existing_subnets is None:
        existing_subnets = []
        try:
            listed = subprocess.run(
                ["docker", "network", "ls", "-q"],
                text=True,
                capture_output=True,
                check=False,
            )
            for network_id in listed.stdout.splitlines():
                if not network_id.strip():
                    continue
                inspected = subprocess.run(
                    ["docker", "network", "inspect", network_id.strip()],
                    text=True,
                    capture_output=True,
                    check=False,
                )
                if inspected.returncode != 0:
                    continue
                try:
                    values = json.loads(inspected.stdout)
                    configs = values[0].get("IPAM", {}).get("Config") or []
                    existing_subnets.extend(
                        str(item["Subnet"])
                        for item in configs
                        if isinstance(item, dict) and item.get("Subnet")
                    )
                except (TypeError, ValueError, KeyError, IndexError, json.JSONDecodeError):
                    continue
        except OSError:
            # If Docker is unavailable, the explicit candidate range is still
            # checked against host routes below; the real Compose invocation
            # remains responsible for reporting any daemon-level failure.
            pass
    if host_subnets is None:
        host_subnets = []
        try:
            routes = subprocess.run(
                ["ip", "-o", "route", "show"],
                text=True,
                capture_output=True,
                check=False,
            )
            for token in routes.stdout.split():
                if "/" in token and token not in {"src"}:
                    try:
                        ipaddress.ip_network(token, strict=False)
                    except ValueError:
                        continue
                    host_subnets.append(token)
        except OSError:
            # The Docker overlap check remains authoritative when ``ip`` is
            # unavailable in a host environment.
            pass

    blocked: list[ipaddress._BaseNetwork] = []
    for raw in [*existing_subnets, *host_subnets]:
        try:
            blocked.append(ipaddress.ip_network(raw, strict=False))
        except ValueError:
            continue
    seed = int(hashlib.sha256(str(run_dir.resolve()).encode()).hexdigest()[:8], 16)
    # RFC 2544 benchmarking space is not part of Docker's default bridge
    # pools and is normally unrouted.  It gives interrupted multi-run suites
    # room even when the host already routes all of 10/8 or 172.16/12.
    candidates = [
        ipaddress.ip_network(f"198.{18 + (index // 256)}.{index % 256}.0/24")
        for index in range(512)
    ]
    for offset in range(len(candidates)):
        candidate = candidates[(seed + offset) % len(candidates)]
        if any(candidate.overlaps(item) for item in blocked):
            continue
        return str(candidate)
    raise RuntimeError("no non-overlapping run-local Compose subnet is available")


def read_stats(path: Path) -> dict[str, Any]:
    path = Path(path)
    if path.name == "builder.json":
        current = path.parent / "builder.json-builder-transport" / "broker_stats.json"
        if current.is_file(): path = current
    value = read_json(path)
    return value or {
        "schema_version": "agentswe-broker-stats/v1",
        "runtime": {"calls": 0, "failures": 0, "successful_calls": 0, "input_tokens": 0, "output_tokens": 0, "total_tokens": 0},
    }


def read_container_id(cidfile: Path) -> str | None:
    try:
        value = cidfile.read_text(encoding="ascii").strip()
    except OSError:
        return None
    return value or None


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


def retain_owned_container(role: str, cidfile: Path, *, attempted: bool) -> dict[str, Any]:
    """Stop this run's own container and keep it for the coordinator.

    The readiness profile needs a cleanup attestation over resources that still
    exist to be inspected, so removal is deferred. Ownership rests on the same
    evidence the removing variant uses: the container id captured from this run's
    cidfile, never a deterministic name.
    """
    container_id = read_container_id(cidfile)
    receipt: dict[str, Any] = {
        "role": role, "cidfile": str(cidfile), "container_id": container_id,
        "startup_attempted": attempted, "ownership_proven": container_id is not None,
        "removal_deferred": True, "absent_after_cleanup": False,
        "retained_terminal": False, "cleanup_error": None,
    }
    if not attempted:
        receipt.update(status="not_started", absent_after_cleanup=True, retained_terminal=True)
        return receipt
    if container_id is None:
        receipt.update(status="ownership_unproven",
                       cleanup_error="Docker startup was attempted but no current-run container ID was captured")
        return receipt
    try:
        subprocess.run(["docker", "stop", "--time", "20", container_id],
                       text=True, capture_output=True, check=False)
        inspected = subprocess.run(["docker", "inspect", container_id],
                                   text=True, capture_output=True, check=False)
    except OSError as exc:
        receipt.update(status="cleanup_error",
                       cleanup_error=f"docker stop/inspect failed: {type(exc).__name__}: {exc}")
        return receipt
    if not inspected.returncode:
        value = json.loads(inspected.stdout)[0]
        if ((value.get("State") or {}).get("Status") == "removing"
                or ((value.get("HostConfig") or {}).get("AutoRemove") and not (value.get("State") or {}).get("Running"))):
            # The lower and Builder brokers run with --rm, so `docker stop` hands them to the daemon's own
            # removal: Docker 24 has finished it here, Docker 29 finishes it asynchronously. Wait for it
            # rather than handing the coordinator a container that is about to disappear.
            receipt["removal_in_progress_after_stop"] = True
            await_daemon_removal(container_id)
            try:
                inspected = subprocess.run(["docker", "inspect", container_id],
                                           text=True, capture_output=True, check=False)
            except OSError as exc:
                receipt.update(status="cleanup_error",
                               cleanup_error=f"docker inspect failed: {type(exc).__name__}: {exc}")
                return receipt
    if inspected.returncode:
        text = ((inspected.stdout or "") + (inspected.stderr or "")).strip().lower()
        if "no such" in text:
            # Harbor may already have torn the container down; absence is the
            # terminal state the coordinator would itself have produced.
            receipt.update(status="absent", absent_after_cleanup=True, retained_terminal=True,
                           removed_by_environment_teardown=True)
            return receipt
        receipt.update(status="cleanup_unverified", cleanup_error=text[-1000:])
        return receipt
    state = (json.loads(inspected.stdout)[0].get("State") or {})
    if state.get("Running") or state.get("Pid") not in (0, None):
        receipt.update(status="retain_unverified",
                       cleanup_error="owned container did not reach a terminal state")
        return receipt
    receipt.update(status="retained", retained_terminal=True, exit_code=state.get("ExitCode"))
    return receipt


def cleanup_owned_container(role: str, cidfile: Path, *, attempted: bool) -> dict[str, Any]:
    """Remove and verify only a container ID captured from this run.

    A deterministic container name is not ownership evidence: deleting it
    before a run or during cleanup could touch an unrelated prior run.  If a
    run attempted Docker startup but never obtained a container ID, cleanup is
    explicitly unproven rather than silently treating an inspect error as
    absence.
    """
    container_id = read_container_id(cidfile)
    receipt: dict[str, Any] = {
        "role": role,
        "cidfile": str(cidfile),
        "container_id": container_id,
        "startup_attempted": attempted,
        "ownership_proven": container_id is not None,
        "cleanup_attempted": container_id is not None,
        "absent_after_cleanup": False,
        "cleanup_error": None,
    }
    if not attempted:
        receipt["absent_after_cleanup"] = True
        receipt["status"] = "not_started"
        return receipt
    if container_id is None:
        receipt["status"] = "ownership_unproven"
        receipt["cleanup_error"] = "Docker startup was attempted but no current-run container ID was captured"
        return receipt
    try:
        removed = subprocess.run(
            ["docker", "rm", "-f", container_id],
            text=True, capture_output=True, check=False,
        )
    except OSError as exc:
        receipt.update({
            "status": "cleanup_error",
            "cleanup_error": f"docker rm failed: {type(exc).__name__}: {exc}",
        })
        return receipt
    if removed.returncode:
        receipt["remove_stderr"] = (removed.stderr or "")[-500:]
    if removal_in_progress(removed):
        receipt["removal_in_progress_at_rm"] = True
        await_daemon_removal(container_id)
    try:
        inspected = subprocess.run(
            ["docker", "inspect", container_id],
            text=True, capture_output=True, check=False,
        )
    except OSError as exc:
        receipt.update({
            "remove_exit_code": removed.returncode,
            "status": "cleanup_error",
            "cleanup_error": f"docker inspect failed: {type(exc).__name__}: {exc}",
        })
        return receipt
    inspect_text = ((inspected.stdout or "") + (inspected.stderr or "")).strip()
    no_such_text = inspect_text.lower()
    no_such = inspected.returncode != 0 and (
        "no such object" in no_such_text or "no such container" in no_such_text
    )
    receipt["remove_exit_code"] = removed.returncode
    receipt["inspect_exit_code"] = inspected.returncode
    receipt["absent_after_cleanup"] = no_such
    if no_such:
        receipt["status"] = "absent"
    else:
        receipt["status"] = "cleanup_unverified"
        receipt["cleanup_error"] = inspect_text[-1000:] or "docker inspect did not prove absence"
    return receipt


def start_broker(
    *, name: str, credential: Path, evidence_dir: Path, stats_name: str,
    port: int, role: str, effort: str, image: str, cidfile: Path | None = None,
    defer_removal: bool = False,
) -> None:
    evidence_dir.mkdir(parents=True, exist_ok=True)
    cidfile = cidfile or (evidence_dir / f"{stats_name}.cid")
    if role == "result_judge":
        if effort != BUILDER_EFFORT:
            raise ValueError("Result judge requires xhigh")
        return _judge_runtime().start_judge_broker(name=name, credential=credential, image=image,
            port=port, cidfile=cidfile, defer_removal=defer_removal)
    if role == "builder":
        if effort != BUILDER_EFFORT: raise ValueError("Builder requires xhigh")
        return start_builder_broker(name=name, credential=credential, evidence_dir=evidence_dir, port=port, image=image, cidfile=cidfile)
    if cidfile.exists(): raise FileExistsError("preserve existing lower broker ownership receipt: " + str(cidfile))
    script = ROOT / "evaluator/broker/candidate_broker.py"
    command = [
        "docker", "run", "-d", "--rm", "--name", name, "--network", "host",
        "-e", "AGENTSWE_EVALUATOR_PROXY_URL=",
        "-v", f"{script}:/broker.py:ro",
        "-v", "@@AGENTSWE_EDITING_CONTROL@@/responses_stream.py:/responses_stream.py:ro",
        "-v", f"{credential}:/run/secrets/agentswe.env:ro",
        "-v", f"{evidence_dir}:/evidence",
        "--cidfile", str(cidfile),
        image, "python3", "/broker.py",
        "--credential-file", "/run/secrets/agentswe.env",
        "--bind", "127.0.0.1", "--port", str(port),
        "--stats-file", f"/evidence/{stats_name}",
        "--role", role, "--reasoning-effort", effort,
    ]
    subprocess.run(command, check=True, stdout=subprocess.DEVNULL)
    for _ in range(60):
        try:
            with urllib.request.urlopen(f"http://127.0.0.1:{port}/healthz", timeout=2) as response:
                if response.status == 200:
                    return
        except Exception:
            time.sleep(0.5)
    raise RuntimeError(f"{role} broker did not become healthy")


def start_builder_broker(*, name: str, credential: Path, evidence_dir: Path, port: int, image: str, cidfile: Path | None = None) -> None:
    """Use shared upper transport; consumer retains exact CID cleanup ownership."""
    sys.path.insert(0, "@@AGENTSWE_EDITING_CONTROL@@")
    import builder_broker_runtime
    evidence_dir.mkdir(parents=True, exist_ok=True)
    cidfile = cidfile or evidence_dir / f"{name}.cid"
    return builder_broker_runtime.start_builder_broker(name=name, credential=credential, image=image, port=port, cidfile=cidfile)


def endpoint_stats(port: int) -> dict[str, Any]:
    request = urllib.request.Request(
        f"http://127.0.0.1:{port}/stats",
        headers={"Authorization": "Bearer stats-only-placeholder"},
    )
    with urllib.request.urlopen(request, timeout=10) as response:
        value = json.loads(response.read())
    return value if isinstance(value, dict) else {}


def missing_integration_entries(submission: Path) -> list[str]:
    """Name the frozen integration entry paths a delivery leaves out.

    The evaluator copies its own product_observation.ts into the Candidate
    repository and that file imports these modules, so a patch without them
    cannot start the product: the run that prompted this got
    "Failed to resolve import ./src/stores/recovery-store" and was reported to
    the Builder as evaluator infrastructure failure with resampling forbidden.
    The requirement was already declared in evaluator/harness/common.py but only
    enforced on a harness the dev rounds do not use.

    The missing paths are listed explicitly: the Builder acts on this text.
    """
    from evaluator.harness.common import REQUIRED_INTEGRATION_PATHS
    patch = Path(submission) / "solution.patch"
    if not patch.is_file():
        return []  # the delivery validation above already reports this
    try:
        present = set(submission_patch_paths(patch))
    except Exception:
        return []  # unparsable patches are the delivery validation's business
    missing = sorted(REQUIRED_INTEGRATION_PATHS - present)
    if not missing:
        return []
    return ["solution.patch does not change the required integration entry path(s): "
            + ", ".join(missing)
            + " -- the evaluator's product observation harness imports these modules and the"
              " product cannot start without them"]


class BuilderLifecycle:
    """Controller socket facade bound to one Builder invocation/session."""

    def __init__(
        self, *, run_dir: Path, workspace: Path, lower_endpoint: str,
        node_modules: Path, timeout: int, pilot_not_formal: bool = False,
        max_dev_rounds: int = 10, n_concurrent: int = 1,
        result_judge_endpoint: str | None = None,
        readiness_profile: str | None = None,
        current_binding: dict[str, Any] | None = None,
    ) -> None:
        self.run_dir = run_dir
        self.workspace = workspace
        self.delivery_intents = ProductIntents(run_dir, name="builder_delivery_intents.json")
        self.active_intent_id = None
        self.token = hashlib.sha256(f"{run_dir}:{time.time_ns()}".encode()).hexdigest()
        self.session_id: str | None = None
        self.native_observations: list[dict[str, Any]] = []
        self.lock = threading.RLock()
        self.invocation_id = f"harbor-invocation-{hashlib.sha256((self.token + ':invocation').encode()).hexdigest()[:16]}"
        self.socket_path = Path(tempfile.gettempdir()) / f"openhands-builder-{self.token[:12]}.sock"
        self.server: socketserver.ThreadingUnixStreamServer | None = None
        self.events: list[dict[str, Any]] = []
        self.attempts: list[dict[str, Any]] = []
        self.pilot_not_formal = pilot_not_formal
        self.readiness_profile = readiness_profile
        # A feedback is "received" only when a write to the Builder's socket
        # returned.  The helper can be killed by the Builder's shell-tool
        # timeout while a submission is still running, and the response write
        # then raises BrokenPipeError: the round is accepted and its feedback
        # bytes are on disk, but no receipt exists, verify_native rejects the
        # whole session, and _submit then refuses the next round as
        # previous_feedback_not_delivered.  Keep the ready response so a later
        # connection can deliver it for real, and receipt it at that later
        # moment -- never before, and never for a write that did not happen.
        self.feedback_write_failures: list[dict[str, Any]] = []
        self.undelivered_feedback: dict[int, dict[str, Any]] = {}
        self.current_binding = current_binding
        self.controller = TwoRoundController(
            workspace, run_dir / "lifecycle",
            ["python3", str(ROOT / "lower_agent/openhands_lower_agent.py")],
            case_root=ROOT, broker_endpoint=lower_endpoint,
            node_modules=node_modules, stats_file=run_dir / "brokers/lower.json", timeout=timeout,
            public_cases=("dev_001",) if pilot_not_formal else DEV_CASES,
            hidden_cases=("test_001", "test_002") if pilot_not_formal else HIDDEN_CASES,
            public_max_retries=0,
            pilot_not_formal=pilot_not_formal,
            max_dev_rounds=max_dev_rounds, n_concurrent=n_concurrent,
            result_judge_endpoint=result_judge_endpoint,
            readiness_profile=readiness_profile, current_binding=current_binding,
        )

    def _event(self, event: str, **values: Any) -> None:
        with self.lock:
            row = {"event": event, "at": now(), "epoch_ns": time.time_ns(),
                   "builder_session_id": self.session_id,
                   "builder_invocation_id": self.invocation_id, **values}
            path = self.run_dir / "builder_events.jsonl"
            with path.open("a", encoding="utf-8") as handle:
                handle.write(json.dumps(row, ensure_ascii=False) + "\n")
                handle.flush(); os.fsync(handle.fileno())
            self.events.append(row)

    def _observe_native(self) -> None:
        observation = observe_thread(self.run_dir, self.native_observations)
        if self.session_id is not None and observation["thread_id"] != self.session_id:
            raise RuntimeError("native Builder thread changed")
        self.session_id = observation["thread_id"]
        self.native_observations.append(observation)
        write_json(self.run_dir / "native_stream_observations.json", self.native_observations)

    def _payload(self, record: dict[str, Any]) -> dict[str, Any]:
        feedback_path = Path(record["feedback_path"])
        raw = feedback_path.read_bytes()
        if hashlib.sha256(raw).hexdigest() != record["feedback_digest"]:
            raise RuntimeError("authoritative feedback bytes changed")
        return public_payload({
            "accepted": True, "submission_number": record["submission"],
            "candidate": record["submission"], "builder_session_id": record["builder_session_id"],
            "candidate_digest": record["candidate_digest"],
            # The delivered triple is what the readiness contract calls the
            # candidate and what the next round's run_report.json is checked
            # against; the product identity above is a different digest.
            "delivery_candidate_digest": (record.get("delivery_digests") or [None])[0],
            "feedback_digest": record["feedback_digest"],
            "feedback_digest_ack": record.get("feedback_digest_ack"),
            "feedback": json.loads(raw),
            "frozen": self.controller.frozen,
            "freeze_reason": (self.controller.freeze_manifest or {}).get("freeze_reason"),
        })

    @staticmethod
    def _delivered_records(payload: dict[str, Any]):
        """Per-record views of what one response actually carried.

        A submit or feedback response is one _payload record at top level; a
        status response carries the same _payload dicts under "records". Both
        come from _payload, so the same accepted record has byte-identical
        content either way and therefore one receipt digest.
        """
        if isinstance(payload, dict) and payload.get("submission_number"):
            yield payload
        for record in (payload or {}).get("records") or []:
            if isinstance(record, dict) and record.get("submission_number"):
                yield record

    @staticmethod
    def _carries_feedback(record: dict[str, Any]) -> bool:
        """True only when the substantive feedback body itself went out.

        _payload re-reads the authoritative feedback file and refuses to build a
        record unless those bytes hash to the record's feedback_digest, so a
        record that reaches a 200 response and still carries a non-empty
        feedback object carries verified bytes. A reference that names a digest
        without carrying its body must never count as a delivery.
        """
        feedback = record.get("feedback")
        return bool(record.get("accepted")) and bool(record.get("feedback_digest")) \
            and isinstance(feedback, dict) and bool(feedback)

    def _persist_delivery_ledger(self) -> None:
        write_json(self.run_dir / "native_feedback_deliveries.json", {
            "schema_version": "openhands-feedback-delivery-ledger/v1",
            "delivered": [v for v in self.events if v.get("event") == "feedback_delivered"],
            "write_failures": self.feedback_write_failures,
            "undelivered_candidate_numbers": sorted(self.undelivered_feedback)})

    def feedback_written(self, payload: dict[str, Any]) -> None:
        """Receipt every feedback the socket write that just returned carried.

        A flush receipt alone is insufficient for the echo binding: the final
        attestation still looks for these full bytes in the native tool output.
        """
        with self.lock:
            for record in self._delivered_records(payload):
                if not self._carries_feedback(record):
                    continue
                number = record["submission_number"]
                digest = record["feedback_digest"]
                payload_sha256 = hashlib.sha256(
                    json.dumps(record, sort_keys=True, ensure_ascii=False).encode()).hexdigest()
                self.undelivered_feedback.pop(number, None)
                if any(v.get("event") == "feedback_delivered" and v.get("candidate_number") == number
                       and v.get("feedback_digest") == digest
                       and v.get("payload_sha256") == payload_sha256 for v in self.events):
                    continue
                self._event("feedback_delivered", candidate_number=number,
                            feedback_digest=digest, payload_sha256=payload_sha256)
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
                number = record["submission_number"]
                numbers.append(number)
                if not any(v.get("event") == "feedback_delivered" and v.get("candidate_number") == number
                           for v in self.events):
                    self.undelivered_feedback[number] = record
            detail = f"{type(exc).__name__}: {exc}"
            self.feedback_write_failures.append({"at": now(), "error": detail,
                "candidate_numbers": numbers, "builder_session_id": self.session_id})
            self._event("feedback_write_failed", candidate_numbers=numbers, error=detail)
            self._persist_delivery_ledger()

    def _materialize(self, number: int, attempt: int) -> tuple[int, Path, dict[str, Any]]:
        delivery = self.run_dir / "deliveries" / f"attempt_{attempt:03d}"
        delivery.parent.mkdir(parents=True, exist_ok=True)
        shutil.copytree(self.workspace, delivery, symlinks=True)
        product = self.run_dir / "materialized" / f"candidate_{number:03d}_attempt_{attempt:03d}"
        product.parent.mkdir(parents=True, exist_ok=True)
        command = [
            "python3", str(ROOT / "adapters/materialize_candidate.py"),
            "--repository", str(ROOT / "input/repository"),
            "--submission", str(delivery), "--output", str(product),
            "--node-modules", str(self.controller.node_modules),
            "--run-typecheck",
        ]
        from lower_agent.owned_resources import run_owned
        build_started = time.monotonic()
        process, resources = run_owned(command, cwd=ROOT,
            env={key: value for key, value in os.environ.items() if key in ("PATH", "LANG", "LC_ALL", "TZ")},
            output=self.run_dir / "materialization_resources" / f"attempt_{attempt:03d}",
            timeout=570, memory_bytes=4 * 1024 ** 3, purpose="build")
        resource_valid = (resources.get("valid") is True and resources.get("aggregate_cleanup", {}).get("complete") is True
                          and time.monotonic() - build_started <= 600)
        materialize_result = read_json(product.parent / "materialize_result.json") or {}
        receipt = {
            "attempt": attempt, "requested_candidate_number": number,
            "delivery_digest": directory_digest(delivery),
            "exit_code": process.returncode, "resource_valid": resource_valid,
            "resource_attestation": resources,
            "stdout_tail": process.stdout[-2000:], "stderr_tail": process.stderr[-2000:],
            "materialize_result": materialize_result,
        }
        write_json(self.run_dir / "materialization" / f"attempt_{attempt:03d}.json", receipt)
        return process.returncode, product, receipt

    def submit(self, feedback_digest: str | None) -> tuple[int, dict[str, Any]]:
        with self.lock:
            return self._submit(feedback_digest)

    def _submit(self, feedback_digest: str | None) -> tuple[int, dict[str, Any]]:
        self._observe_native()
        errors = validate_submission(self.workspace)
        errors.extend(missing_integration_entries(self.workspace))
        if errors:
            return 422, public_payload({"accepted": False, "classification": "invalid_submission", "errors": errors})
        delivery = directory_digest(self.workspace)
        patch_digest = hashlib.sha256((self.workspace / "solution.patch").read_bytes()).hexdigest()
        delivered_feedback = bool(feedback_digest and any(event.get('event') == 'feedback_delivered'
            and event.get('feedback_digest') == feedback_digest for event in self.events))
        request_id = hashlib.sha256(json.dumps([delivery, feedback_digest, delivered_feedback]).encode()).hexdigest()
        prior = self.delivery_intents.entries.get(request_id)
        if prior is None:
            prior = next((row for row in self.delivery_intents.entries.values()
                if row['metadata'].get('patch_digest') == patch_digest
                and row.get('result', {}).get('status') in (200, 422, 503)), None)
        if prior is not None:
            response = prior.get('result', {'status':503, 'payload':{'accepted':False,
                'round_consumed':False,'error':'Submission outcome unknown','resampling_forbidden':True}})
            return response['status'], response['payload']
        self.delivery_intents.reserve(request_id, delivery_digest=delivery,
            patch_digest=patch_digest, feedback_digest_ack=feedback_digest)
        self.active_intent_id = request_id
        try:
            code, payload = self._submit_uncached(feedback_digest)
        except Exception as exc:
            self._event('submission_outcome_unknown', error_type=type(exc).__name__)
            code, payload = 503, {'accepted':False, 'round_consumed':False,
                'error':'Evaluator submission outcome unknown', 'resampling_forbidden':True}
        payload = public_payload(payload)
        if code == 503:
            payload['resampling_forbidden'] = True
        result = {'status':code,'payload':payload}
        self.delivery_intents.finish(request_id, 'unknown' if code == 503 else 'completed', result)
        return code, payload

    @staticmethod
    def _partial_feedback(dev):
        result = {}
        for case in DEV_CASES:
            row = dev.get(case) or {}
            evaluation = row.get('result_evaluation', {})
            complete = evaluation.get('contract_valid') is True and evaluation.get('round_consumed') is True
            result[case] = {'state':'completed' if complete else 'unknown',
                'score':evaluation.get('score') if complete else None,
                'classification':row.get('classification', 'lower_agent_infrastructure_failure'),
                'semantic_feedback':evaluation.get('feedback') if complete else None}
        return result

    def _submit_uncached(self, feedback_digest: str | None) -> tuple[int, dict[str, Any]]:
        delivery_digest = directory_digest(self.workspace)
        # Retrying an already accepted delivery returns the same authoritative
        # feedback, including after max-round freeze; it never rebuilds/runs dev.
        duplicate = next((r for r in self.controller.records
                          if delivery_digest in r.get("delivery_digests", [])), None)
        if duplicate is not None:
            return 200, self._payload(duplicate)
        number = len(self.controller.records) + 1
        attempt = len(self.attempts) + 1
        if self.controller.frozen or number > self.controller.max_dev_rounds:
            return 409, {"error": "max_dev_rounds_reached", "accepted_submissions": len(self.controller.records)}
        expected_feedback = self.controller.records[-1].get("feedback_digest") if self.controller.records else None
        if number == 1 and feedback_digest:
            return 409, {"error": "feedback_digest_not_valid_for_candidate_1"}
        if number > 1 and feedback_digest != expected_feedback:
            return 409, {"error": "submission_requires_latest_feedback_digest", "expected_feedback_digest": expected_feedback}
        if number > 1 and not any(event.get('event') == 'feedback_delivered' and event.get('feedback_digest') == expected_feedback for event in self.events):
            return 409, {'error':'previous_feedback_not_delivered', 'expected_feedback_digest':expected_feedback}
        self._event("submission_attempt_started", attempt=attempt, requested_candidate=number, feedback_digest_ack=feedback_digest)
        build_code, product, receipt = self._materialize(number, attempt)
        self.attempts.append(receipt)
        if receipt.get("resource_valid") is False:
            self._event("submission_attempt_infrastructure_invalid", attempt=attempt, reason="materialization_resource_failure")
            return 503, {"accepted": False, "round_consumed": False,
                         "error": "Evaluator materialization resource/cleanup failure; no Candidate score"}
        if build_code != 0:
            self._event("submission_attempt_rejected", attempt=attempt, reason="candidate_build_failure")
            materialization = receipt.get("materialize_result", {})
            return 422, public_payload({"accepted": False, "classification": "candidate_build_failure",
                         "error": "Public candidate materialization/typecheck failed", "round_consumed": False,
                         "errors": materialization.get("errors", []),
                         "details": [{"exit_code": item.get("exit_code"),
                                      "error_detail": item.get("stderr_tail") or item.get("stdout_tail")}
                                     for item in materialization.get("commands", []) if item.get("exit_code") != 0]})
        identity = receipt.get('materialize_result', {}).get('product_source_identity')
        if not isinstance(identity, str) or len(identity) != 64:
            raise RuntimeError('Stable materialized source identity is missing')
        self.delivery_intents.entries[self.active_intent_id]['metadata'].update(
            product_source_identity=identity, product_evaluation_reserved=True)
        self.delivery_intents.persist()
        try:
            record = self.controller.submit(product, feedback_digest_ack=feedback_digest,
                                            product_source_identity=identity,
                                            builder_session_id=self.session_id)
        except Exception as exc:
            self._event("submission_attempt_rejected", attempt=attempt, reason=type(exc).__name__)
            prior = self.controller.product_intents.entries.get(identity, {})
            partial = {case: row.get('result') for case, row in prior.get('cases', {}).items()}
            return 503, public_payload({"accepted": False, "round_consumed": False,
                "error": "Public evaluator outcome unknown", "resampling_forbidden": True,
                "dev": self._partial_feedback(partial)})
        if not record.get("accepted", True):
            self._event("submission_attempt_infrastructure_invalid", attempt=attempt)
            return 503, public_payload({"accepted": False, "round_consumed": False,
                "error": "Public evaluator infrastructure invalid; submission not consumed",
                "resampling_forbidden": True, "dev": self._partial_feedback(record.get('dev', {}))})
        if record.get("duplicate_digest"):
            original = next(r for r in self.controller.records if r["candidate_digest"] == record["candidate_digest"])
            original.setdefault("delivery_digests", []).append(delivery_digest)
            self.controller._persist()
            return 200, self._payload(original)
        record.update(builder_session_id=self.session_id,
                      delivery_digests=[receipt["delivery_digest"]],
                      feedback_path=str(self.controller.run_dir / record["feedback"]),
                      build={"candidate_repo_digest": record["candidate_digest"], "materialization": receipt})
        self.controller._persist()
        self._event("candidate_accepted", candidate=number, attempt=attempt,
                    candidate_digest=record["candidate_digest"], feedback_digest=record["feedback_digest"],
                    feedback_digest_ack=feedback_digest)
        payload = self._payload(record)
        self._event("feedback_created", candidate_number=number, feedback_digest=record["feedback_digest"])
        return 200, payload

    def status(self) -> tuple[int, dict[str, Any]]:
        with self.lock:
            return 200, public_payload({
                "builder_session_id": self.session_id,
                "accepted_submissions": len(self.controller.records),
                "records": [self._payload(record) for record in self.controller.records],
                "feedback_digest": self.controller.records[-1].get("feedback_digest") if self.controller.records else None,
                "frozen": self.controller.frozen,
            })

    def status_snapshot(self) -> tuple[int, dict[str, Any]]:
        """Read-only view taken while a submission holds the lock.

        controller.records only gains a record after that record's feedback
        file has been written, so every record visible here is complete, and
        _payload still refuses any record whose bytes no longer hash to its
        digest. No native observation is taken and no state is mutated.
        """
        records = list(self.controller.records)
        return 200, public_payload({
            "builder_session_id": self.session_id,
            "accepted_submissions": len(records),
            "records": [self._payload(record) for record in records],
            "feedback_digest": records[-1].get("feedback_digest") if records else None,
            "frozen": self.controller.frozen,
            "state": "submission_in_progress",
        })

    def feedback(self) -> tuple[int, dict[str, Any]]:
        """Re-deliver an accepted feedback whose earlier response was lost.

        This re-reads the same authoritative feedback file the accepted record
        already names; it issues nothing new and consumes no round. The oldest
        response that a failed write left undelivered is preferred.
        """
        with self.lock:
            records = self.controller.records
            if not records:
                return 404, {"error": "no accepted submission has feedback yet"}
            pending = [n for n in sorted(self.undelivered_feedback) if 1 <= n <= len(records)]
            return 200, self._payload(records[(pending[0] if pending else len(records)) - 1])

    def close(self) -> None:
        if self.server:
            self.server.shutdown(); self.server.server_close()
        self.socket_path.unlink(missing_ok=True)


def start_builder_server(lifecycle: BuilderLifecycle) -> None:
    class Handler(socketserver.StreamRequestHandler):
        def handle(self) -> None:
            try:
                request = json.loads(self.rfile.readline(1 << 20))
                action = request.get("action")
                if request.get("token") != lifecycle.token:
                    code, payload = 401, {"error": "unauthorized"}
                elif action in {"submit", "status", "feedback"}:
                    # A read-only action must answer inside the Builder's
                    # shell-tool window even while a submission holds the lock
                    # for the length of the materialization and both dev cases.
                    read_only = action in {"status", "feedback"}
                    held = lifecycle.lock.acquire(
                        timeout=(READ_ONLY_ACTION_LOCK_TIMEOUT_SECONDS if read_only else -1))
                    if held:
                        try:
                            code, payload = (lifecycle.submit(request.get("feedback_digest"))
                                             if action == "submit" else getattr(lifecycle, action)())
                        finally:
                            lifecycle.lock.release()
                    else:
                        code, payload = lifecycle.status_snapshot()
                else:
                    code, payload = 400, {"error": "unknown_action"}
            except Exception as exc:
                code, payload = 400, {"error": f"{type(exc).__name__}: {exc}"}
            payload = public_payload(payload)
            try:
                self.wfile.write(json.dumps({"status": code, "payload": payload}, ensure_ascii=False).encode() + b"\n")
                self.wfile.flush()
            except (BrokenPipeError, ConnectionError, OSError) as exc:
                # The Builder's helper was killed, or its shell tool timed out,
                # while the evaluator was still running the dev cases. Nothing
                # arrived, so nothing is receipted: record the failed write and
                # keep the ready response deliverable by a later --status,
                # --feedback, or identical re-submit.
                if code == 200:
                    lifecycle.feedback_write_failed(payload, exc)
                return
            if code == 200:
                lifecycle.feedback_written(payload)

    class Server(socketserver.ThreadingUnixStreamServer):
        daemon_threads = True

    lifecycle.socket_path.unlink(missing_ok=True)
    lifecycle.server = Server(str(lifecycle.socket_path), Handler)
    lifecycle.socket_path.chmod(stat.S_IRUSR | stat.S_IWUSR)
    threading.Thread(target=lifecycle.server.serve_forever, daemon=True).start()


def write_submit_helper(path: Path) -> None:
    path.write_text('''#!/usr/bin/env python3
"""Builder-side controller client.

A submission materializes the Candidate and runs the public dev cases, which
takes far longer than a shell tool call. The submit therefore runs in a
detached, fully daemonised child that holds the socket until the evaluator
answers, and the foreground process only polls a local response file in short
bounded waits. A shell-tool timeout can kill the poller without ever cutting
the evaluator response off, and nothing here needs nohup or kill.
"""
import argparse, json, os, socket, sys, time
from pathlib import Path

TMP = Path(os.environ.get("TMPDIR") or "/tmp")
RESPONSE = TMP / "agentswe-submit-response.json"
HOLDER = TMP / "agentswe-submit-holder.pid"

parser = argparse.ArgumentParser(prog="submit_dev_candidate")
parser.add_argument("--status", action="store_true", help="accepted records and their feedback; always answers quickly")
parser.add_argument("--feedback", action="store_true", help="reprint the evaluator feedback and digest already produced")
parser.add_argument("--feedback-digest", dest="feedback_digest", help="digest of the feedback this revision answers")
parser.add_argument("--wait", action="store_true", help="accepted for compatibility; a submit always waits by polling")
parser.add_argument("--await-only", dest="await_only", action="store_true", help="keep polling the submission already running")
parser.add_argument("--poll-seconds", dest="poll_seconds", type=float, default=240.0, help="how long this call polls before reporting 202")
args, _extra = parser.parse_known_args()
action = "status" if args.status else ("feedback" if args.feedback else "submit")


def call(request):
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as s:
        s.connect(os.environ["AGENTSWE_DEV_CONTROLLER_SOCKET"])
        s.sendall((json.dumps({"token": os.environ["AGENTSWE_DEV_CONTROLLER_TOKEN"],
                               **request}) + "\\n").encode())
        return json.loads(s.makefile().readline())


def finish(result):
    print(json.dumps(result, ensure_ascii=False, indent=2))
    raise SystemExit(0 if result.get("status") == 200 else 1)


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


if action != "submit":
    finish(call({"action": action}))

if not args.await_only and not holder_alive():
    for path in (RESPONSE, HOLDER):
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
            if args.feedback_digest:
                request["feedback_digest"] = args.feedback_digest
            result = call(request)
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

result = poll(time.time() + max(1.0, args.poll_seconds))
if result is None:
    print(json.dumps({"status": 202, "payload": {"state": "submission_in_progress",
        "reason": "the evaluator is still running the public dev cases; run "
                  "`submit_dev_candidate --await-only` again to keep waiting, or "
                  "`submit_dev_candidate --status` for the accepted records so far"}},
        ensure_ascii=False, indent=2))
    raise SystemExit(3)
finish(result)
''', encoding="utf-8")
    path.chmod(0o755)


def builder_config(
    *, run_dir: Path, public: Path, workspace: Path,
    lifecycle: BuilderLifecycle, provider_config: Path, image: str,
    pilot_not_formal: bool = False,
) -> Path:
    task = run_dir / "builder_task"
    worktree = run_dir / "builder_workspace/worktree"
    if worktree.exists():
        shutil.rmtree(worktree)
    shutil.copytree(public / "input/repository", worktree, symlinks=True)
    # The Builder container starts with /workspace as its process directory,
    # while the writable checkout is mounted at /workspace/worktree.  Seed a
    # local baseline repository so ordinary git status/diff commands work even
    # when the model forgets to cd before its first inspection.
    subprocess.run(["git", "init", "-q"], cwd=worktree, check=True)
    subprocess.run(["git", "config", "user.email", "builder@invalid"], cwd=worktree, check=True)
    subprocess.run(["git", "config", "user.name", "Builder"], cwd=worktree, check=True)
    subprocess.run(["git", "add", "-A"], cwd=worktree, check=True)
    subprocess.run(["git", "commit", "-qm", "pristine baseline"], cwd=worktree, check=True)
    (task / "environment").mkdir(parents=True)
    (task / "tests").mkdir(parents=True)
    (task / "task.toml").write_text(
        f'''schema_version = "1.4"
[task]
name = "local/openhands-effect-recovery-agentloop-builder"
version = "1.0.0"
description = "OpenHands Edit up-to-ten-round feedback lifecycle"
artifacts = [{{ source = "/workspace/submission", destination = "builder_submission" }}]
[agent]
timeout_sec = {float(GENERATED_BUILDER_TASK_TIMEOUT_SECONDS)}
user = "root"
network_mode = "public"
[verifier]
timeout_sec = 300.0
user = "root"
network_mode = "no-network"
[environment]
docker_image = "{image}"
network_mode = "public"
build_timeout_sec = 900.0
cpus = 8
memory_mb = 16384
storage_mb = 32768
workdir = "/workspace/worktree"
[environment.healthcheck]
command = "python3 /usr/local/lib/agentswe-builder-resource-check.py && python3 /opt/agentswe-openhands/dependency_check.py"
retries = 1
timeout_sec = 25.0
''', encoding="utf-8",
    )
    instruction = f"""# OpenHands Edit Builder: same-session feedback revision

You are the only upper Builder session. Read only `/builder-package/input` and
`/builder-package/dev_cases`. The supplied pristine product is
`/builder-package/input/repository`, an OpenHands Agent Canvas checkout. Never
inspect hidden cases, evaluator source, credentials, prior runs, or Candidate
snapshots.

Work in an ephemeral `/workspace/worktree`. Keep exactly three files in the
mounted `/workspace/submission`: `solution.patch`, `edit_report.json`, and
`run_report.json`. Submit each new snapshot with:

    submit_dev_candidate --wait

The command synchronously runs both public dev cases through the real Candidate
OpenHands product and prints authoritative feedback plus a feedback digest.
Read that feedback, revise the implementation when useful in this same
uninterrupted session, regenerate all three delivery files, and acknowledge it:

    submit_dev_candidate --feedback-digest <printed-digest> --wait

Each accepted revision must materialize to a different product digest. Continue
for up to {lifecycle.controller.max_dev_rounds} accepted rounds or exit when satisfied; dev mean above 60 is only
recorded and never freezes automatically. Do not place source trees, caches,
credentials, or extra files in `/workspace/submission`. Hidden cases execute
only after the latest accepted snapshot is frozen.

For the first submission, implement the smallest coherent product slice that
builds and satisfies the visible public contract, then submit immediately once
the public validation gate passes. Do not spend the entire first Builder turn
implementing every optional recovery surface or hidden failure mode before
submitting; use the evaluator's public feedback to choose the next bounded
revision. A submitted, reviewable Candidate is more useful than an unsubmitted
large worktree when the upper model or broker turn is interrupted.

A submission materializes the Candidate and runs the public dev cases, which can
take more than fifteen minutes, so `submit_dev_candidate` does that work in a
detached helper and returns early rather than holding one long command. If it
prints status 202 with state submission_in_progress, run `submit_dev_candidate
--await-only` again, as many times as needed, until it prints the real response.
Never wrap the command in `nohup`, never background or `disown` it, and never
kill it. Issue it as the only command in its tool call, not alongside a heredoc
or other work. At any time, including while a submission is running,
`submit_dev_candidate --status` answers within seconds with every accepted
record and its full feedback, and `submit_dev_candidate --feedback` reprints the
evaluator feedback and its exact digest for the latest accepted submission. If a
response is ever lost anyway, use one of those two commands, or resend the
identical delivery unchanged -- an already accepted delivery is answered from
the duplicate path and consumes no round.
"""
    if pilot_not_formal:
        instruction = """# OpenHands Edit Builder: non-formal two-case pilot

You are the one uninterrupted upper Builder session. Work directly in the
already writable `/workspace/worktree`; do not create another worktree, spawn
or delegate to subagents, wait for another agent, or use web search. Read the
four input files and only `/builder-package/dev_cases/dev_001`. Never inspect
hidden cases, evaluator source, credentials, prior runs, or Candidate snapshots.

Keep exactly `solution.patch`, `edit_report.json`, and `run_report.json` in
`/workspace/submission`. Implement a focused Candidate 1, generate the patch
from the worktree diff, write both JSON reports, run `validate_dev_candidate`,
and fix validation failures before `submit_dev_candidate --wait`. Do not call
submit while the delivery is empty. The evaluator then executes exactly one
dev_001 and prints authoritative feedback plus its digest. Consume that
feedback in this same session, make one genuine revision, regenerate all three
delivery files, validate again, then run `submit_dev_candidate
--feedback-digest <exact-digest> --wait`. Candidate 2 must have a distinct
product digest. Stop immediately after freeze; do not run hidden tests yourself.

A submission materializes the Candidate and runs the public dev cases, which can
take more than fifteen minutes, so `submit_dev_candidate` does that work in a
detached helper and returns early rather than holding one long command. If it
prints status 202 with state submission_in_progress, run `submit_dev_candidate
--await-only` again, as many times as needed, until it prints the real response.
Never wrap the command in `nohup`, never background or `disown` it, and never
kill it. Issue it as the only command in its tool call, not alongside a heredoc
or other work. At any time, including while a submission is running,
`submit_dev_candidate --status` answers within seconds with every accepted
record and its full feedback, and `submit_dev_candidate --feedback` reprints the
evaluator feedback and its exact digest for the latest accepted submission. If a
response is ever lost anyway, use one of those two commands, or resend the
identical delivery unchanged -- an already accepted delivery is answered from
the duplicate path and consumes no round.
The evaluator alone executes exactly test_001 and test_002. This pilot cannot publish a
formal six-hidden Result or Code score.

For Candidate 1, implement the smallest coherent product slice that builds and
meets the visible `dev_001` contract, then submit as soon as validation passes.
Do not exhaustively implement hidden recovery surfaces before the first
submission; use the evaluator feedback for the bounded revision.
"""
    instruction += """

The verified offline toolchain is provided. Begin each shell command with
export PATH=/opt/agentswe-openhands/bin:$PATH, or invoke
/opt/agentswe-openhands/bin/npm explicitly (exactly 10.5.0). The worktree has a
writable copy of dependencies matched to the public package lock. Public helper
scripts read OPENHANDS_BENCH_ENV=/opt/agentswe-openhands/env and copy the same
verified read-only prewarm into their own worktrees; they never install or
contact a package registry. Do not run npm ci or download tooling. Unknown or
infrastructure-invalid product submissions are reserved permanently; changing
reports does not authorize a new evaluation. Read any completed per-case
feedback retained in the partial response. One accepted submission followed by
Builder exit is allowed; a second source revision is not a formal requirement.
"""
    if lifecycle.controller.readiness_profile:
        # The text above ends by allowing a single accepted submission, which is
        # true of the pilot and wrong here, and it names none of the delivery
        # metadata the readiness profile makes mandatory. Appended rather than
        # spliced so the pilot wording stays exactly as it is.
        instruction += """

Under this readiness profile, complete exactly two accepted dev rounds and stop;
the sentence above about a single accepted submission does not apply.

run_report.json must carry four further fields in both rounds. Round 1:
builder_session_id (your own Builder session id for this run), submission_number
1, revision_of_candidate_digest null, feedback_digest null. builder_session_id is
not a name you invent and not the invocation id: it is the identifier of the
Builder session this run observes. `submit_dev_candidate --status` echoes it back
as `builder_session_id`, but only once a submission attempt has been made -- the
field is null there before the first attempt, so use --status to check a value
you already reported, never as the source for the round 1 report. Round 2: the same
builder_session_id, submission_number 2, revision_of_candidate_digest set to the
delivery_candidate_digest the evaluator returned for the accepted round 1 -- the
field of that exact name in the submit response, not candidate_digest, which is a
different digest of a different tree -- and feedback_digest set to the exact
feedback digest you acknowledged.

Round 2's edit_report.json must also carry feedback_response: a string of prose
that you write, explaining how this revision answers the feedback. It is your
explanation, not a copy of the evaluator's feedback object.
"""
    (task / "instruction.md").write_text(instruction, encoding="utf-8")
    resource_check = task / "environment/builder_resource_check.py"
    shutil.copyfile(ROOT / "harbor/builder_resource_check.py", resource_check)
    submit = task / "environment/submit_dev_candidate"
    write_submit_helper(submit)
    (task / "tests/test.sh").write_text("printf '0\\n' > /logs/verifier/reward.txt\nexit 0\n", encoding="utf-8")
    (task / "tests/test.sh").chmod(0o755)
    # Harbor resolves this path on the host before container creation.  Use
    # the run-local absolute path as both source and read-only container target
    # so the same path is valid in both namespaces.
    provider_target = str(provider_config)
    dependency_mounts, dependency_proof = prepare_builder_dependencies(run_dir, public, worktree)
    write_json(run_dir / "builder_dependency_preflight.json", dependency_proof)
    compose = {
        "networks": {
            "default": {
                "ipam": {
                    "config": [{"subnet": _select_compose_subnet(run_dir=run_dir)}]
                }
            }
        },
        "services": {"main": {
            # Compose 2.5.0 mis-translates cpus; quota/period is measured below.
            "cpu_quota": 800000, "cpu_period": 100000,
            "volumes": dependency_mounts + [
        {"type": "bind", "source": str(resource_check), "target": "/usr/local/lib/agentswe-builder-resource-check.py", "read_only": True},
        {"type": "bind", "source": str(public), "target": "/builder-package", "read_only": True},
        {"type": "bind", "source": str(worktree), "target": "/workspace/worktree"},
        {"type": "bind", "source": str(workspace), "target": "/workspace/submission"},
                {"type": "bind", "source": str(lifecycle.socket_path), "target": "/run/agentswe-controller.sock"},
                {"type": "bind", "source": str(submit), "target": "/usr/local/bin/submit_dev_candidate", "read_only": True},
                {"type": "bind", "source": str(provider_config), "target": provider_target, "read_only": True},
            ],
            "environment": {
                "OPENHANDS_BENCH_ENV": "/opt/agentswe-openhands/env",
                "AGENTSWE_DEV_CONTROLLER_SOCKET": "/run/agentswe-controller.sock",
                "AGENTSWE_DEV_CONTROLLER_TOKEN": lifecycle.token,
                "AGENTSWE_BUILDER_BROKER_TOKEN": "broker-only-placeholder",
            },
        }},
    }
    write_json(task / "environment/docker-compose.yaml", compose)
    config = run_dir / "builder_job_config.json"
    write_json(config, {
        "job_name": f"openhands-agentloop-builder-{run_dir.name}",
        "jobs_dir": str(run_dir / "jobs"), "n_attempts": 1, "n_concurrent_trials": 1,
        "quiet": True, "retry": {"max_retries": 0},
        "environment": {"type": "docker", "delete": True},
        "agents": [{
            "import_path": "agentswe_codex_resume:CodexResume", "model_name": BUILDER_MODEL,
            "env": {
                "CODEX_HOME": "/tmp/agentswe-codex-home", "CODEX_CONFIG_TOML_PATH": provider_target,
                "AGENTSWE_BUILDER_BROKER_TOKEN": "broker-only-placeholder",
            },
            "kwargs": {"reasoning_effort": BUILDER_EFFORT},
        }],
        "tasks": [{"path": str(task)}],
    })
    return config


def builder_attestation(lifecycle: BuilderLifecycle, *, builder_exit_code: int,
                        started_ns: int, ended_ns: int,
                        allow_max_rounds_interrupt: bool = False) -> dict[str, Any]:
    accepted = [event for event in lifecycle.events if event["event"] == "candidate_accepted"]
    feedback_events = [event for event in lifecycle.events if event["event"] == "feedback_delivered"]
    records = lifecycle.controller.records
    # Survivable max_dev_rounds exit (D2, 2026-09-19).  Once the Builder has
    # spent its whole accepted-round budget it is answered with 409
    # "max_dev_rounds_reached" but never stopped, so a non-zero exit afterwards
    # is the outer deadline, not missing evidence.  This tree only ever freezes
    # with reason "builder_exit", so the budget itself is the signal.  125 stays
    # fatal: it is this evaluator's own resource/preagent-gate proof failing.
    max_rounds_interrupt = bool(
        allow_max_rounds_interrupt
        and builder_exit_code not in (0, 125)
        and lifecycle.controller.frozen
        and len(records) >= lifecycle.controller.max_dev_rounds
    )
    native = verify_native(lifecycle.run_dir, records, feedback_events,
                           lifecycle.native_observations,
                           allow_interrupted=max_rounds_interrupt)
    same_session = native["valid"]
    invocation_starts = [event for event in lifecycle.events if event["event"] == "builder_invocation_started"]
    invocation_ends = [event for event in lifecycle.events if event["event"] == "builder_invocation_ended"]
    single_invocation = len(invocation_starts) == len(invocation_ends) == 1
    public_cases = tuple(getattr(lifecycle.controller, "public_cases", DEV_CASES))
    hidden_cases = tuple(getattr(lifecycle.controller, "hidden_cases", HIDDEN_CASES))
    result = {
        "schema_version": "agentswe-openhands-builder-session-attestation/v1",
        "builder_session_id": lifecycle.session_id,
        "builder_invocation_id": lifecycle.invocation_id,
        "builder_model": BUILDER_MODEL,
        "builder_reasoning_effort": BUILDER_EFFORT,
        "builder_exit_code": builder_exit_code,
        "builder_started_epoch_ns": started_ns,
        "builder_ended_epoch_ns": ended_ns,
        "single_harbor_invocation": single_invocation,
        "same_session": same_session,
        "events": lifecycle.events,
        "native_evidence": native,
        "candidate_records": records,
        "submission_attempts": lifecycle.attempts,
        "accepted_candidate_count": len(records),
        "accepted_submission_digests": [record.get("candidate_digest") for record in records],
        "all_accepted_rounds_two_public_dev": bool(records) and all(set(record.get("dev", {})) == set(public_cases) for record in records),
        "feedback_delivered_each_round": bool(records) and all(any(event.get("candidate_number") == record["submission"] and event.get("feedback_digest") == record["feedback_digest"] for event in feedback_events) for record in records),
        "distinct_candidate_digests": len({record.get("candidate_digest") for record in records}) == len(records),
        "max_dev_rounds": lifecycle.controller.max_dev_rounds,
        "n_concurrent": lifecycle.controller.n_concurrent,
        "dev_passed_is_automatic_freeze": False,
        "infrastructure_attempts": lifecycle.controller.infrastructure_attempts,
        "freeze_created": lifecycle.controller.frozen,
        "freeze_reason": (lifecycle.controller.freeze_manifest or {}).get("freeze_reason"),
        "public_case_inventory": list(public_cases),
        "hidden_case_inventory": list(hidden_cases),
        "pilot_not_formal": bool(getattr(lifecycle, "pilot_not_formal", False)),
        "max_dev_rounds_interrupt_accepted": max_rounds_interrupt,
    }
    result["complete"] = all((
        builder_exit_code == 0 or max_rounds_interrupt,
        native["valid"], result["single_harbor_invocation"], result["same_session"], bool(records),
        result["all_accepted_rounds_two_public_dev"], result["feedback_delivered_each_round"],
        result["distinct_candidate_digests"], result["freeze_created"],
        result["freeze_reason"] in {"max_dev_rounds", "builder_exit"},
    ))
    return result


def prepare_run_dir(path: Path) -> None:
    if path.exists() and any(path.iterdir()):
        raise RuntimeError(f"refusing to overwrite non-empty run directory: {path}")
    path.mkdir(parents=True, exist_ok=True)


def restrict_public_package_to_pilot(public: Path, manifest: dict[str, Any]) -> dict[str, Any]:
    shutil.rmtree(public / "dev_cases/dev_002", ignore_errors=True)
    value = dict(manifest)
    value.update({
        "pilot_not_formal": True,
        "public_case_inventory": ["dev_001"],
        "hidden_mounted": False,
        "evaluator_mounted": False,
        "credential_mounted": False,
    })
    write_json(public / "PUBLIC_PACKAGE_MANIFEST.json", value)
    return value


DEV_CASES = ("dev_001", "dev_002")
HIDDEN_CASES = tuple(f"test_{number:03d}" for number in range(1, 7))
PLACEHOLDER_CREDENTIAL = "broker-only-placeholder"
STATIC_RUN_DIR = ROOT / ".formal-static-audit"


def protocol_lock(*, formal_started: bool = False, public_manifest: dict[str, Any] | None = None) -> dict[str, Any]:
    """Return the evaluator-owned protocol lock for either execution mode.

    The formal path may add its Builder-visible package manifest and endpoints
    later, but the model, session, inventory, and credential invariants are
    identical in static and formal mode.
    """
    return {
        "schema_version": "agentswe-openhands-formal-protocol/v1",
        "mode": "formal" if formal_started else "provider-free-static-audit",
        "builder": {
            "model": BUILDER_MODEL,
            "reasoning_effort": BUILDER_EFFORT,
            "transport": "native_codex_direct",
            "credential_method": "Harbor CODEX_AUTH_JSON_PATH in temporary tmpfs",
            "existing_proxy": "http://127.0.0.1:7890",
            "single_continuous_session": True,
        },
        "lower_agent": {
            "product": "@openhands/agent-canvas",
            "model": MODEL,
            "reasoning_effort": LOWER_EFFORT,
            "broker_role": "lower",
            "candidate_credential": PLACEHOLDER_CREDENTIAL,
        },
        "brokers_independent": True,
        "public_cases": list(DEV_CASES),
        "hidden_cases": list(HIDDEN_CASES),
        "hidden_strictly_after_freeze": True,
        "builder_visibility": public_manifest or {
            "visible_roots": ["input", "dev_cases"],
            "hidden_mounted": False,
            "evaluator_mounted": False,
            "prior_runs_mounted": False,
            "credential_mounted": False,
        },
        "result_axis": "N/A until hidden evidence is complete and independently judged",
        "code_axis": "N/A until independently judged",
        "formal_result_claimed": False,
        "code_score_claimed": False,
    }


def _static_required_files() -> tuple[str, ...]:
    return (
        "protocol_lock.json",
        "freeze_manifest.schema.json",
        "broker_stats.schema.json",
        "adapters/materialize_candidate.py",
        "evaluator/broker/candidate_broker.py",
        "evaluator/controller/two_round_controller.py",
        "evaluator/controller/minimal_smoke_controller.py",
        "harbor/stage_public_package.py",
        "lower_agent/openhands_lower_agent.py",
        "input/01_task_goal.md",
        "input/02_interface_and_delivery.md",
        "dev_cases/dev_001/input.md",
        "dev_cases/dev_002/input.md",
    ) + tuple(f"test_cases/{case}/input.md" for case in HIDDEN_CASES)


def static_audit(run_dir: Path) -> dict[str, Any]:
    """Audit the lifecycle contract without Docker, Harbor, or network I/O."""
    missing = [relative for relative in _static_required_files() if not (ROOT / relative).is_file()]
    errors: list[str] = []
    checked_in_lock = read_json(ROOT / "protocol_lock.json") or {}
    if checked_in_lock.get("builder", {}).get("model") != BUILDER_MODEL:
        errors.append("checked-in builder model lock mismatch")
    if checked_in_lock.get("builder", {}).get("reasoning_effort") != BUILDER_EFFORT:
        errors.append("checked-in builder reasoning lock mismatch")
    if checked_in_lock.get("lower_agent", {}).get("model") != MODEL:
        errors.append("checked-in lower model lock mismatch")
    if checked_in_lock.get("lower_agent", {}).get("reasoning_effort") != LOWER_EFFORT:
        errors.append("checked-in lower reasoning lock mismatch")
    if checked_in_lock.get("visibility", {}).get("candidate_credential") not in {"placeholder-only", PLACEHOLDER_CREDENTIAL}:
        errors.append("candidate credential is not placeholder-only")
    if checked_in_lock.get("inventory", {}).get("dev") != list(DEV_CASES):
        errors.append("public inventory mismatch")
    if checked_in_lock.get("inventory", {}).get("hidden") != list(HIDDEN_CASES):
        errors.append("hidden inventory mismatch")
    if checked_in_lock.get("lifecycle", {}).get("hidden_strictly_after_freeze") is not True:
        errors.append("hidden freeze gate missing")
    lock = protocol_lock(formal_started=False)
    run_dir.mkdir(parents=True, exist_ok=True)
    write_json(run_dir / "protocol_lock.json", lock)
    summary: dict[str, Any] = {
        "schema_version": "agentswe-openhands-one-stop-static-audit/v1",
        "status": "static_audit_pass" if not missing and not errors else "static_audit_fail",
        "mode": "provider-free-static-audit",
        "required_files_missing": missing,
        "audit_errors": errors,
        "builder_model": BUILDER_MODEL,
        "builder_reasoning_effort": BUILDER_EFFORT,
        "lower_model": MODEL,
        "lower_reasoning_effort": LOWER_EFFORT,
        "candidate_credential": PLACEHOLDER_CREDENTIAL,
        "public_case_count": len(DEV_CASES),
        "hidden_case_count": len(HIDDEN_CASES),
        "network_calls": 0,
        "docker_started": False,
        "broker_started": False,
        "harbor_started": False,
        "formal_execution_started": False,
        "formal_result_claimed": False,
        "code_score_claimed": False,
        "audited_at": now(),
    }
    write_json(run_dir / "summary.json", summary)
    return summary


def self_test() -> dict[str, Any]:
    """Run the one-stop contract audit without external execution."""
    with tempfile.TemporaryDirectory(prefix="openhands-formal-one-stop-self-test-") as raw:
        summary = static_audit(Path(raw) / "audit")
    passed = summary["status"] == "static_audit_pass"
    return {
        "status": "PASS" if passed else "FAIL",
        "network_calls": 0,
        "docker_started": False,
        "formal_execution_started": False,
        "default_mode": "provider-free-static-audit",
        "formal_requires_explicit_flag": True,
        "static_audit_status": summary["status"],
    }


def run_formal(args: argparse.Namespace) -> int:
    """Execute the pre-existing real lifecycle after the explicit gate."""
    if args.run_dir is None:
        raise ValueError("--run-formal requires --run-dir")
    run_dir = args.run_dir.resolve()
    credential, harbor, node_modules = args.credential_file.resolve(), args.harbor.resolve(), args.node_modules.resolve()
    for required, label in ((credential, "credential file"), (harbor, "Harbor executable"), (node_modules, "OpenHands node_modules")):
        if not required.exists():
            raise FileNotFoundError(f"{label} missing: {required}")
    prepare_run_dir(run_dir)
    public_lower_port, builder_port, hidden_lower_port, judge_port = (
        free_port(), free_port(), free_port(), free_port()
    )
    while len({public_lower_port, builder_port, hidden_lower_port, judge_port}) != 4:
        builder_port, hidden_lower_port, judge_port = free_port(), free_port(), free_port()
    public_lower_endpoint = f"http://127.0.0.1:{public_lower_port}/v1/responses"
    hidden_lower_endpoint = f"http://127.0.0.1:{hidden_lower_port}/v1/responses"
    builder_endpoint = f"http://{args.builder_host_address}:{builder_port}/v1/responses"
    judge_endpoint = f"http://127.0.0.1:{judge_port}/v1/responses"
    suffix = hashlib.sha256(str(run_dir).encode()).hexdigest()[:10]
    public_lower_name = f"openhands-formal-public-lower-{suffix}"
    hidden_lower_name = f"openhands-formal-hidden-lower-{suffix}"
    builder_name = f"openhands-formal-builder-{suffix}"
    judge_name = f"openhands-formal-judge-{suffix}"
    attempted = {"public_lower": False, "hidden_lower": False, "builder": False, "judge": False}
    precleaned: dict[str, dict[str, Any]] = {}
    lifecycle: BuilderLifecycle | None = None
    try:
        attempted["public_lower"] = True
        start_broker(
            name=public_lower_name, credential=credential, evidence_dir=run_dir / "brokers",
            stats_name="lower.json", port=public_lower_port, role="lower", effort=LOWER_EFFORT,
            image=args.builder_image, cidfile=run_dir / "brokers/lower.json.cid",
        )
        provider_config = run_dir / "builder_broker_provider.toml"
        attempted["judge"] = True
        start_broker(name=judge_name, credential=credential, evidence_dir=run_dir / "brokers",
                     stats_name="judge.json", port=judge_port, role="result_judge", effort=BUILDER_EFFORT,
                     image=args.builder_image, cidfile=run_dir / "brokers/judge.json.cid")
        direct_builder_runtime().write_provider(provider_config)
        public = run_dir / "builder_public_package"
        public_manifest = stage_public_package(ROOT, public)
        workspace = run_dir / "builder_workspace/submission"
        workspace.mkdir(parents=True)
        lifecycle = BuilderLifecycle(
            run_dir=run_dir, workspace=workspace, lower_endpoint=public_lower_endpoint,
            node_modules=node_modules, timeout=args.case_timeout,
            max_dev_rounds=args.max_dev_rounds, n_concurrent=args.n_concurrent,
            result_judge_endpoint=judge_endpoint,
        )
        start_builder_server(lifecycle)
        config = builder_config(
            run_dir=run_dir, public=public, workspace=workspace, lifecycle=lifecycle,
            provider_config=provider_config, image=args.builder_image,
        )
        write_json(run_dir / "protocol_lock.json", {
            **protocol_lock(formal_started=True, public_manifest=public_manifest),
            "builder_transport": "native_codex_direct",
            "builder_broker_started": False,
            "public_lower_endpoint": public_lower_endpoint,
            "hidden_lower_endpoint": hidden_lower_endpoint,
            "hidden_lower_broker_started_after_freeze": True,
            "hidden_lower_broker_requires_zero_call_start": True,
        })
        started_ns = time.time_ns()
        lifecycle._event("builder_invocation_started")
        builder = run_native_builder(lifecycle=lifecycle, config=config, credential=credential,
                                     harbor=harbor, timeout=args.builder_timeout)
        lifecycle._event("builder_invocation_ended", exit_code=builder.returncode)
        ended_ns = time.time_ns()
        if lifecycle.controller.records and not lifecycle.controller.frozen:
            lifecycle.controller.freeze_latest("builder_exit")
        attestation = builder_attestation(lifecycle, builder_exit_code=builder.returncode,
                                          started_ns=started_ns, ended_ns=ended_ns,
                                          allow_max_rounds_interrupt=True)
        write_json(run_dir / "builder_session_attestation.json", attestation)
        if not attestation["complete"]:
            write_json(run_dir / "summary.json", {
                "status": "builder_integration_incomplete", "mode": "formal",
                "formal_result_claimed": False, "code_score_claimed": False, "builder_attestation": attestation,
                "builder": direct_builder_runtime().native_stats(run_dir),
                "lower_broker": read_stats(run_dir / "brokers/lower.json"),
            })
            return 2
        # The public lower broker belongs to the Builder/dev phase.  Close it
        # before hidden execution so hidden calls start in a fresh, independent
        # evaluator-owned lower-model process with zero inherited calls.
        precleaned["public_lower"] = cleanup_owned_container(
            "public_lower", run_dir / "brokers/lower.json.cid", attempted=True,
        )
        if precleaned["public_lower"].get("absent_after_cleanup") is not True:
            raise RuntimeError("formal hidden phase requires proven cleanup of the public lower broker")
        attempted["hidden_lower"] = True
        start_broker(
            name=hidden_lower_name, credential=credential, evidence_dir=run_dir / "brokers",
            stats_name="hidden.json", port=hidden_lower_port, role="lower", effort=LOWER_EFFORT,
            image=args.builder_image, cidfile=run_dir / "brokers/hidden.json.cid",
        )
        hidden_initial = read_stats(run_dir / "brokers/hidden.json")
        hidden_runtime = hidden_initial.get("runtime") if isinstance(hidden_initial.get("runtime"), dict) else {}
        if int(hidden_runtime.get("calls", 0) or 0) != 0 or int(hidden_runtime.get("failures", 0) or 0) != 0:
            raise RuntimeError("formal hidden lower broker must start with zero calls and zero failures")
        write_json(run_dir / "hidden_lower_broker_initial.json", {
            "schema_version": "agentswe-openhands-formal-hidden-broker-initial/v1",
            "started_after_freeze": True,
            "independent_from_public_lower": True,
            "calls": 0,
            "failures": 0,
            "stats": hidden_initial,
        })
        lifecycle.controller.broker_endpoint = hidden_lower_endpoint
        lifecycle.controller.stats_file = run_dir / "brokers/hidden.json"
        hidden = lifecycle.controller.run_all_hidden()
        hidden_attestation = read_json(run_dir / "lifecycle/hidden-after-freeze-attestation.json") or {}
        public_lower_stats = read_stats(run_dir / "brokers/lower.json")
        lower_stats, builder_stats = read_stats(run_dir / "brokers/hidden.json"), direct_builder_runtime().native_stats(run_dir)
        infrastructure_failures = hidden_attestation.get("infrastructure_failures", {})
        finalizer_output = run_dir / "formal_aggregation.json"
        finalizer = subprocess.run([sys.executable, str(ROOT / "evaluator/formal_finalize.py"),
                                    "--run-dir", str(run_dir), "--output", str(finalizer_output),
                                    "--credential-file", str(credential),
                                    "--result-broker-endpoint", judge_endpoint,
                                    "--result-judge", str(SHARED_RESULT_JUDGE),
                                    "--code-judge", str(CREATE_CODE_JUDGE)],
                                   text=True, capture_output=True, check=False)
        aggregation = read_json(finalizer_output) or {}
        write_json(run_dir / "summary.json", {
            "status": "completed" if finalizer.returncode == 0 else "formal_finalization_refused",
            "mode": "formal", "formal_result_claimed": aggregation.get("formal_result_publishable") is True,
            "code_score_claimed": aggregation.get("code_score_publishable") is True,
            "builder_session_attestation": "builder_session_attestation.json",
            "freeze_manifest": "lifecycle/freeze_manifest.json",
            "hidden_attestation": "lifecycle/hidden-after-freeze-attestation.json",
            "hidden": hidden, "builder": builder_stats,
            "public_lower_broker": public_lower_stats, "lower_broker": lower_stats,
            "hidden_lower_broker_initial": "hidden_lower_broker_initial.json",
            "network_calls": None, "builder_actual_upstream_requests": None,
            "known_hidden_lower_calls": lower_stats.get("runtime", {}).get("calls"),
            "docker_started": True, "formal_execution_started": True,
            "formal_aggregation": aggregation, "formal_finalizer_exit": finalizer.returncode,
            "formal_finalizer_stderr_tail": finalizer.stderr[-1500:],
        })
        return 0 if finalizer.returncode == 0 else 2
    except Exception as exc:
        # Without this the formal branch left no summary.json at all on an
        # orchestration failure, so a dead run was indistinguishable from a run
        # that never started.  Mirrors run_pilot's own except handling; gates
        # are untouched, the run still returns 2.
        failure = {
            "status": "orchestration_failed", "mode": "formal",
            "classification": "evaluator_orchestration_failure",
            "error_type": type(exc).__name__,
            "error_detail": str(exc)[-1200:],
            "traceback_tail": traceback.format_exc()[-4000:],
            "formal_result_claimed": False, "code_score_claimed": False,
            "result_axis": "N/A", "code_axis": "N/A",
        }
        write_json(run_dir / "summary.json", failure)
        write_json(run_dir / "one_stop_summary.json",
                   {**failure, "schema_version": "agentswe-edit-one-stop-summary-v1",
                    "status": "formal_result_not_publishable"})
        return 2
    finally:
        if lifecycle is not None:
            lifecycle.close()
        retaining = (run_dir / "readiness_current_binding.json").is_file()
        owned = retain_owned_container if retaining else cleanup_owned_container
        cleanup_results = [
            precleaned.get("public_lower") or owned(
                "public_lower", run_dir / "brokers/lower.json.cid", attempted=attempted["public_lower"],
            ),
            owned("hidden_lower", run_dir / "brokers/hidden.json.cid", attempted=attempted["hidden_lower"]),
            owned("builder", run_dir / "brokers/builder.json.cid", attempted=attempted["builder"]),
            owned("judge", run_dir / "brokers/judge.json.cid", attempted=attempted["judge"]),
        ]
        if retaining:
            import types as _types
            from harbor.readiness_resources import retained_manifest
            retained_manifest(run_dir, [
                _types.SimpleNamespace(container_id=item["container_id"])
                for item in cleanup_results
                if item.get("container_id") and not item.get("absent_after_cleanup")])
        cleanup = {
            "schema_version": "agentswe-cleanup-attestation-v1",
            "owned_container_cleanup": cleanup_results,
            "controller_socket_closed": lifecycle is None or not lifecycle.socket_path.exists(),
            "cleanup_complete": all(item.get("absent_after_cleanup") is True for item in cleanup_results),
            "unrelated_containers_touched": False,
        }
        write_json(run_dir / "cleanup_attestation.json", cleanup)
        summary = read_json(run_dir / "summary.json") or {}
        aggregation = read_json(run_dir / "formal_aggregation.json") or {}
        freeze = read_json(run_dir / "lifecycle" / "freeze_manifest.json") or {}
        write_json(run_dir / "one_stop_summary.json", {
            "schema_version": "agentswe-edit-one-stop-summary-v1",
            "status": summary.get("status", "formal_execution_failed"),
            "dev_lifecycle": "lifecycle/dev_lifecycle.json",
            "freeze": {"path": "lifecycle/freeze_manifest.json", "digest": freeze.get("candidate_digest"), "reason": freeze.get("freeze_reason")},
            "hidden_inventory": list(HIDDEN_CASES),
            "hidden_summary": summary.get("hidden", "N/A"),
            "result_judge_contracts": aggregation.get("result_judge_contracts", {}),
            "code_contract": aggregation.get("code_contract"),
            "result_axis": aggregation.get("result_axis", "N/A"),
            "code_axis": aggregation.get("code_axis", "N/A"),
            "combined_score": None,
            "cleanup_attestation": "cleanup_attestation.json",
            "judge_broker_stats": "brokers/judge.json",
        })


def pilot_self_test(*, readiness_profile: str | None = None,
                    current_binding: dict[str, Any] | None = None) -> dict[str, Any]:
    """Generate and inspect the reduced pilot job without external execution."""
    with tempfile.TemporaryDirectory(prefix="openhands-pilot-self-test-") as raw:
        run_dir = Path(raw) / "pilot"; run_dir.mkdir(parents=True)
        public = run_dir / "builder_public_package"
        manifest = stage_public_package(ROOT, public)
        manifest = restrict_public_package_to_pilot(public, manifest)
        workspace = run_dir / "workspace"; workspace.mkdir()
        lifecycle = BuilderLifecycle(
            run_dir=run_dir, workspace=workspace,
            lower_endpoint="http://127.0.0.1:1/v1/responses",
            node_modules=run_dir / "node_modules", timeout=1,
            pilot_not_formal=True,
            readiness_profile=readiness_profile,
            current_binding=current_binding,
        )
        try:
            provider = run_dir / "provider.toml"
            provider.write_text(
                'model_provider = "openhands_builder_broker"\ndisable_response_storage = true\n\n'
                '[model_providers.openhands_builder_broker]\nname = "pilot dry broker"\n'
                'base_url = "http://builder-broker.invalid/v1"\n'
                'env_key = "AGENTSWE_BUILDER_BROKER_TOKEN"\nwire_api = "responses"\nrequires_openai_auth = false\n',
                encoding="utf-8",
            )
            config = builder_config(run_dir=run_dir, public=public, workspace=workspace,
                                    lifecycle=lifecycle, provider_config=provider,
                                    image=BUILDER_IMAGE, pilot_not_formal=True)
            instruction = (run_dir / "builder_task/instruction.md").read_text(encoding="utf-8")
            config_text = config.read_text(encoding="utf-8")
            checks = {
                "pilot_inventory": lifecycle.controller.public_cases == ("dev_001",)
                and lifecycle.controller.hidden_cases == (
                    ("test_001",) if readiness_profile is not None else ("test_001", "test_002")
                ),
                "no_public_retry": lifecycle.controller.public_max_retries == 0,
                "builder_lock": BUILDER_MODEL in config_text and BUILDER_EFFORT in config_text,
                "placeholder_only": PLACEHOLDER_CREDENTIAL in config_text
                and "OPENAI_API_KEY=" not in config_text and "DEEPSEEK_API_KEY=" not in config_text,
                "only_dev_001_visible": (public / "dev_cases/dev_001").is_dir()
                and not (public / "dev_cases/dev_002").exists(),
                "pilot_instruction": "exactly test_001 and test_002" in instruction and "formal six-hidden" in instruction,
                "hidden_isolated": manifest.get("hidden_mounted") is False,
                "writable_worktree_seeded": (run_dir / "builder_workspace/worktree").is_dir(),
                "no_builder_delegation": "do not create another worktree, spawn" in instruction,
                "readiness_binding": readiness_profile is None or (
                    lifecycle.controller.readiness_profile == READINESS_PROFILE
                    and lifecycle.controller.current_binding == current_binding
                    and lifecycle.controller.max_dev_rounds == 2
                ),
            }
        finally:
            lifecycle.close()
    return {
        "status": "PASS" if all(checks.values()) else "FAIL", "checks": checks,
        "pilot_not_formal": True, "network_calls": 0, "docker_started": False,
        "formal_execution_started": False, "formal_result_claimed": False,
        "public_cases": ["dev_001"],
        "hidden_cases": list(lifecycle.controller.hidden_cases),
        "readiness_profile": readiness_profile,
        "current_binding": current_binding,
    }


def run_pilot(args: argparse.Namespace) -> int:
    """Run the real reduced lifecycle and emit only pilot-local evidence."""
    if args.run_dir is None:
        raise ValueError("--pilot requires --run-dir")
    run_dir = args.run_dir.resolve()
    credential, harbor, node_modules = args.credential_file.resolve(), args.harbor.resolve(), args.node_modules.resolve()
    for required, label in ((credential, "credential file"), (harbor, "Harbor executable"), (node_modules, "OpenHands node_modules")):
        if not required.exists():
            raise FileNotFoundError(f"{label} missing: {required}")
    prepare_run_dir(run_dir)
    if args.readiness_profile is not None:
        # Both readiness gates in this file test for readiness_current_binding
        # .json, so write it under the name its own readers use; the preflight
        # record below keeps its separate, more detailed schema.
        write_json(run_dir / "readiness_current_binding.json", args.current_binding)
        write_json(run_dir / "readiness_binding_preflight.json", {
            "schema_version": "agentswe-openhands-v2-run-binding-preflight/v1",
            "task": "openhands", "profile": args.readiness_profile,
            "verification": "PASS", "binding": args.current_binding,
            **args.readiness_binding_verification,
            "provider_calls_before_verification": 0,
            "shared_registry_written": False, "shared_gate_written": False,
        })
    base_summary = {
        "schema_version": "agentswe-openhands-pilot-summary/v1", "mode": "pilot",
        "pilot_not_formal": True, "formal_result_claimed": False,
        "code_score_claimed": False, "result_axis": "N/A", "code_axis": "N/A",
        "readiness_profile": args.readiness_profile,
        "current_binding": args.current_binding,
    }
    ports: set[int] = set()
    while len(ports) < 5:
        ports.add(free_port())
    public_port, builder_port, hidden_port, public_judge_port, readiness_judge_port = sorted(ports)
    public_endpoint = f"http://127.0.0.1:{public_port}/v1/responses"
    hidden_endpoint = f"http://127.0.0.1:{hidden_port}/v1/responses"
    builder_endpoint = f"http://{args.builder_host_address}:{builder_port}/v1/responses"
    suffix = hashlib.sha256(str(run_dir).encode()).hexdigest()[:10]
    names = {"public": f"openhands-pilot-public-{suffix}", "builder": f"openhands-pilot-builder-{suffix}",
             "hidden": f"openhands-pilot-hidden-{suffix}",
             "public_judge": f"openhands-pilot-public-judge-{suffix}",
             "readiness_judge": f"openhands-pilot-readiness-judge-{suffix}"}
    started = {key: False for key in names}
    lifecycle: BuilderLifecycle | None = None
    try:
        started["public"] = True
        start_broker(name=names["public"], credential=credential, evidence_dir=run_dir / "brokers",
                     stats_name="lower.json", port=public_port, role="lower", effort=LOWER_EFFORT,
                     image=args.builder_image, cidfile=run_dir / "brokers/lower.json.cid")
        started["public_judge"] = True
        start_broker(name=names["public_judge"], credential=credential, evidence_dir=run_dir / "brokers",
                     stats_name="public-judge.json", port=public_judge_port, role="result_judge", effort=BUILDER_EFFORT,
                     image=args.builder_image, cidfile=run_dir / "brokers/public-judge.json.cid")
        provider = run_dir / "builder_broker_provider.toml"
        direct_builder_runtime().write_provider(provider)
        public = run_dir / "builder_public_package"
        public_manifest = stage_public_package(ROOT, public)
        public_manifest = restrict_public_package_to_pilot(public, public_manifest)
        workspace = run_dir / "builder_workspace/submission"; workspace.mkdir(parents=True)
        lifecycle = BuilderLifecycle(run_dir=run_dir, workspace=workspace, lower_endpoint=public_endpoint,
                                     node_modules=node_modules, timeout=args.case_timeout, pilot_not_formal=True,
                                     readiness_profile=getattr(args, "readiness_profile", None),
                                     current_binding=getattr(args, "current_binding", None),
                                     result_judge_endpoint=f"http://127.0.0.1:{public_judge_port}/v1/responses")
        start_builder_server(lifecycle)
        config = builder_config(run_dir=run_dir, public=public, workspace=workspace, lifecycle=lifecycle,
                                provider_config=provider, image=args.builder_image, pilot_not_formal=True)
        write_json(run_dir / "protocol_lock.json", {
            "schema_version": "agentswe-openhands-pilot-protocol/v1", "pilot_not_formal": True,
            "profile": lifecycle.readiness_profile,
            "current_binding": lifecycle.current_binding,
            "builder": {"model": BUILDER_MODEL, "reasoning_effort": BUILDER_EFFORT,
                        "single_continuous_session": True, "transport": "native_codex_direct",
                        "credential_method": "Harbor CODEX_AUTH_JSON_PATH"},
            "lower_agent": {"product": "@openhands/agent-canvas", "model": MODEL,
                            "reasoning_effort": LOWER_EFFORT, "credential": PLACEHOLDER_CREDENTIAL},
            "public_cases": list(lifecycle.controller.public_cases),
            "hidden_cases": list(lifecycle.controller.hidden_cases),
            "required_valid_rounds": lifecycle.controller.max_dev_rounds,
            "score_threshold": None,
            "formal_result_publishable": False,
            "code_score_publishable": False,
            "builder_visibility": public_manifest, "formal_finalizer_allowed": False,
            "public_hidden_brokers_independent": public_endpoint != hidden_endpoint and names["public"] != names["hidden"],
            "hidden_broker_started_strictly_after_freeze": True,
            "hidden_broker_requires_zero_call_start": True,
        })
        started_ns = time.time_ns(); lifecycle._event("builder_invocation_started", pilot_not_formal=True)
        builder = run_native_builder(lifecycle=lifecycle, config=config, credential=credential,
                                     harbor=harbor, timeout=args.builder_timeout)
        builder_exit = builder.returncode
        lifecycle._event("builder_invocation_ended", exit_code=builder_exit, pilot_not_formal=True)
        if lifecycle.controller.records and not lifecycle.controller.frozen:
            lifecycle.controller.freeze_latest("builder_exit")
        ended_ns = time.time_ns()
        attestation = builder_attestation(lifecycle, builder_exit_code=builder_exit, started_ns=started_ns, ended_ns=ended_ns)
        write_json(run_dir / "pilot_builder_session_attestation.json", attestation)
        if not attestation["complete"]:
            write_json(run_dir / "summary.json", {**base_summary, "status": "pilot_builder_integration_incomplete",
                       "builder_session_attestation": "pilot_builder_session_attestation.json"})
            return 2
        started["hidden"] = True
        start_broker(name=names["hidden"], credential=credential, evidence_dir=run_dir / "brokers",
                     stats_name="hidden.json", port=hidden_port, role="lower", effort=LOWER_EFFORT,
                     image=args.builder_image, cidfile=run_dir / "brokers/hidden.json.cid")
        hidden_initial = read_stats(run_dir / "brokers/hidden.json")
        hidden_runtime = hidden_initial.get("runtime") if isinstance(hidden_initial.get("runtime"), dict) else {}
        if public_endpoint == hidden_endpoint or names["public"] == names["hidden"]:
            raise RuntimeError("pilot hidden broker must be independent from public lower broker")
        if int(hidden_runtime.get("calls", 0) or 0) != 0 or int(hidden_runtime.get("failures", 0) or 0) != 0:
            raise RuntimeError("pilot hidden broker must start with zero calls and zero failures")
        write_json(run_dir / "pilot_hidden_broker_initial.json", {
            "schema_version": "agentswe-openhands-pilot-hidden-broker-initial/v1",
            "pilot_not_formal": True,
            "started_after_freeze": True,
            "independent_from_public_lower": True,
            "calls": 0,
            "failures": 0,
            "stats": hidden_initial,
        })
        lifecycle.controller.broker_endpoint = hidden_endpoint
        lifecycle.controller.stats_file = run_dir / "brokers/hidden.json"
        hidden = lifecycle.controller.run_all_hidden()
        hidden_attestation = read_json(run_dir / "lifecycle/pilot-hidden-after-freeze-attestation.json") or {}
        from evaluator.result_score import score_case
        scores = {item.get("case_id"): score_case(item) for item in hidden if isinstance(item, dict)}
        infrastructure_failures = hidden_attestation.get("infrastructure_failures", {})
        readiness_judges = None
        if args.readiness_profile is not None and hidden_attestation.get("complete") and not infrastructure_failures:
            started["readiness_judge"] = True
            start_broker(
                name=names["readiness_judge"], credential=credential,
                evidence_dir=run_dir / "brokers", stats_name="readiness-judge.json",
                port=readiness_judge_port, role="result_judge", effort=BUILDER_EFFORT,
                image=args.builder_image, cidfile=run_dir / "brokers/readiness-judge.json.cid",
                # Handed to the coordinator, so it has to outlive its own stop;
                # --rm would delete it there and then.
                defer_removal=True,
            )
            from evaluator.readiness_smoke import run as run_readiness_smoke
            readiness_judges = run_readiness_smoke(
                task_root=ROOT, run_dir=run_dir, hidden=hidden, credential=credential,
                result_endpoint=f"http://127.0.0.1:{readiness_judge_port}/v1/responses",
                provider_dispatch_authorized=True,
            )
        evaluation = {"schema_version": "agentswe-openhands-pilot-evaluation/v1", "pilot_not_formal": True,
                      "hidden_case_ids": [item.get("case_id") for item in hidden if isinstance(item, dict)],
                      "scores": scores, "formal_result_publishable": False,
                      "code_score_publishable": False, "infrastructure_failures": infrastructure_failures}
        write_json(run_dir / "pilot_evaluation.json", evaluation)
        write_json(run_dir / "summary.json", {**base_summary,
            "status": "pilot_complete" if hidden_attestation.get("complete") and not infrastructure_failures
                and (readiness_judges is None or readiness_judges.get("readiness_judges_complete"))
                else "pilot_infrastructure_invalid",
            "classification": "real_same_session_one_dev_feedback_freeze_one_test",
            "builder_session_attestation": "pilot_builder_session_attestation.json",
            "freeze_manifest": "lifecycle/freeze_manifest.json",
            "hidden_attestation": "lifecycle/pilot-hidden-after-freeze-attestation.json",
            "hidden_broker_initial": "pilot_hidden_broker_initial.json",
            "pilot_evaluation": evaluation, "readiness_judges": readiness_judges, "hidden": hidden})
        return 0 if hidden_attestation.get("complete") and not infrastructure_failures \
            and (readiness_judges is None or readiness_judges.get("readiness_judges_complete")) else 2
    except Exception as exc:
        write_json(run_dir / "summary.json", {**base_summary, "status": "pilot_orchestration_failed",
                   "error_type": type(exc).__name__, "error_detail": str(exc)[-1200:]})
        return 2
    finally:
        if lifecycle is not None:
            lifecycle.close()
        readiness_judge_cid = run_dir / "brokers/readiness-judge.json.cid"
        retained_judge = None
        retention_error = None
        if args.readiness_profile is not None and started["readiness_judge"]:
            # The coordinator refuses an empty declaration and then requires the
            # declared container to still be present and exited, so this run has
            # to hand one over rather than remove everything itself.
            try:
                from harbor.readiness_resources import retain_container
                transport = (readiness_judge_cid.parent
                             / (readiness_judge_cid.stem + "-judge-transport")).resolve()
                retained_judge = retain_container(
                    read_container_id(readiness_judge_cid), run_dir,
                    expected_name=names["readiness_judge"],
                    expected_mount=(str(transport), "/evidence"))
            except Exception as exc:
                # Unprovable ownership must not become a silently skipped
                # cleanup; fall through and remove it as usual.
                retention_error = f"{type(exc).__name__}: {exc}"
        cleanup_results = [
            cleanup_owned_container("public", run_dir / "brokers/lower.json.cid", attempted=started["public"]),
            cleanup_owned_container("builder", run_dir / "brokers" / f"{names['builder']}.cid", attempted=started["builder"]),
            cleanup_owned_container("hidden", run_dir / "brokers/hidden.json.cid", attempted=started["hidden"]),
            cleanup_owned_container("public_judge", run_dir / "brokers/public-judge.json.cid", attempted=started["public_judge"]),
            {"role": "readiness_judge", "container_id": retained_judge["container_id"],
             "ownership_proven": True, "cleanup_attempted": False,
             "absent_after_cleanup": False, "removal_deferred": True,
             "retained_terminal": True} if retained_judge is not None else
            cleanup_owned_container("readiness_judge", readiness_judge_cid, attempted=started["readiness_judge"]),
        ]
        retained_resources = None
        if retained_judge is not None:
            try:
                import types as _types
                from harbor.readiness_resources import retained_manifest
                retained_resources = retained_manifest(
                    run_dir, [_types.SimpleNamespace(container_id=retained_judge["container_id"])])
            except Exception as exc:
                retention_error = ((retention_error + "; ") if retention_error else "") + \
                    f"retained_manifest: {type(exc).__name__}: {exc}"
        write_json(run_dir / "pilot_cleanup_attestation.json", {
            "pilot_not_formal": True,
            "owned_container_cleanup": cleanup_results,
            "removal_deferred": retained_judge is not None,
            "coordinator_cleanup_required": retained_judge is not None,
            "retained_resources": retained_resources,
            "retention_error": retention_error,
            "cleanup_complete": all(item.get("absent_after_cleanup") is True for item in cleanup_results),
            "unrelated_containers_touched": False,
        })


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run-dir", type=Path, help="new output directory; default static audit uses a sibling-local directory")
    parser.add_argument("--run-formal", action="store_true", help="explicitly authorize Builder/provider/lower/hidden execution")
    parser.add_argument("--pilot", action="store_true", help="run real dev_001 feedback revision then test_001+test_002 as pilot_not_formal")
    parser.add_argument("--pilot-self-test", action="store_true", help="provider-free dry test of the reduced pilot configuration")
    parser.add_argument("--self-test", action="store_true", help="run provider-free one-stop contract checks")
    parser.add_argument("--credential-file", type=Path, default=Path("@@AGENTSWE_CREDENTIAL_FILE@@"))
    parser.add_argument("--harbor", type=Path, default=Path("@@AGENTSWE_HARBOR_BIN@@"))
    parser.add_argument("--node-modules", type=Path, default=Path("@@AGENTSWE_ENVS@@/openhands-effect-recovery-ledger-edit-v1/baseline-install/node_modules"))
    parser.add_argument("--builder-image", default=BUILDER_IMAGE)
    parser.add_argument("--builder-host-address", default="172.17.0.1")
    # A lower case may require one model response per bounded product action
    # plus a final trajectory-bound artifact response.  Keep the controller
    # budget consistent with the Vitest timeout in lower_agent, otherwise a
    # slow but healthy Responses path can be killed before artifact authoring.
    parser.add_argument("--case-timeout", type=int, default=600)
    parser.add_argument("--builder-timeout", type=int, default=1800)
    parser.add_argument("--max-dev-rounds", type=int, default=10)
    parser.add_argument("--n-concurrent", type=int, default=1)
    parser.add_argument("--readiness-profile")
    parser.add_argument("--readiness-binding-file", type=Path)
    parser.add_argument("--readiness-binding-sha256")
    parser.add_argument("--readiness-source-root", type=Path, default=ROOT)
    args = parser.parse_args(argv)
    if not 1 <= args.case_timeout <= 600:
        parser.error("--case-timeout must be in 1..600; product setup is inside this case budget")
    if not 1 <= args.max_dev_rounds <= 10:
        parser.error("--max-dev-rounds must be in 1..10")
    if args.n_concurrent != 1:
        parser.error("--n-concurrent must equal 1")
    binding_options = (args.readiness_binding_file, args.readiness_binding_sha256)
    if args.readiness_profile is None:
        if any(value is not None for value in binding_options):
            parser.error("readiness binding options require --readiness-profile")
        args.current_binding = None
    else:
        if args.readiness_profile != READINESS_PROFILE:
            parser.error("unsupported --readiness-profile")
        if not (args.pilot or args.pilot_self_test):
            parser.error("--readiness-profile requires --pilot or --pilot-self-test")
        if any(value is None for value in binding_options):
            parser.error("readiness requires --readiness-binding-file and --readiness-binding-sha256")
        try:
            args.current_binding, args.readiness_binding_verification = load_and_verify_binding(
                args.readiness_binding_file,
                args.readiness_binding_sha256,
                source_root=args.readiness_source_root,
            )
        except (OSError, ValueError, TypeError, KeyError) as exc:
            parser.error("readiness binding preflight failed: " + str(exc))
    selected_modes = sum(bool(value) for value in (args.self_test, args.pilot_self_test, args.pilot, args.run_formal))
    if selected_modes > 1:
        parser.error("--self-test, --pilot-self-test, --pilot, and --run-formal are mutually exclusive")
    if args.self_test:
        result = self_test()
        print(
            "SELF_TEST=" + result["status"]
            + " network_calls=0 docker_started=false formal_execution_started=false"
        )
        return 0 if result["status"] == "PASS" else 1
    if args.pilot_self_test:
        result = pilot_self_test(
            readiness_profile=args.readiness_profile,
            current_binding=args.current_binding,
        )
        print(json.dumps(result, indent=2, ensure_ascii=False, sort_keys=True))
        return 0 if result["status"] == "PASS" else 1
    if args.pilot:
        if args.run_dir is None:
            parser.error("--pilot requires --run-dir")
        if args.builder_timeout <= 0:
            parser.error("--builder-timeout must be positive")
        enforce_builder_timeout_contract(parser, args.builder_timeout)
        return run_pilot(args)
    if args.run_formal:
        if args.run_dir is None:
            parser.error("--run-formal requires --run-dir")
        if args.builder_timeout <= 0:
            parser.error("--builder-timeout must be positive")
        enforce_builder_timeout_contract(parser, args.builder_timeout)
        return run_formal(args)
    summary = static_audit((args.run_dir or STATIC_RUN_DIR).resolve())
    static_run = (args.run_dir or STATIC_RUN_DIR).resolve()
    write_json(static_run / "one_stop_summary.json", {
        "schema_version": "agentswe-edit-one-stop-summary-v1", "status": summary["status"],
        "mode": "provider-free-static", "provider_calls": 0, "docker_started": False,
        "dev_lifecycle": None, "freeze": None, "hidden_inventory": list(HIDDEN_CASES),
        "hidden_summary": "not_run", "result_judge_contracts": {}, "code_contract": None,
        "result_axis": "N/A", "code_axis": "N/A", "combined_score": None,
        "cleanup_attestation": {"completed": True, "nothing_started": True},
    })
    print(json.dumps(summary, indent=2, ensure_ascii=False, sort_keys=True))
    return 0 if summary["status"] == "static_audit_pass" else 1
if __name__ == "__main__":
    raise SystemExit(main())
