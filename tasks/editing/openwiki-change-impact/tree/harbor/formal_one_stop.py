#!/usr/bin/env python3
"""OpenWiki same-session Builder -> public feedback -> freeze -> hidden.

The command is provider-free by default.  Real Docker, Harbor, provider, and
hidden execution requires explicit ``--run-formal``. The native Builder uses
the user-authorized auth.json and existing proxy; lower credentials remain in
evaluator-owned brokers.
"""
from __future__ import annotations

import argparse
import hashlib
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

try:
    from .one_stop_contract import install_summary_writer
except ImportError:  # direct script execution
    from one_stop_contract import install_summary_writer


ROOT = Path(__file__).resolve().parents[1]
RESULT_JUDGE_ENTRY = Path("@@AGENTSWE_EDITING_CONTROL@@/result_judge.py")
CODE_JUDGE_ENTRY = Path("@@AGENTSWE_EDITING_CONTROL@@/code_eval.py")
RESULT_JUDGE_MODEL = "deepseek-flash"
RESULT_JUDGE_EFFORT = "max"
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from agentloop.evaluator.broker import EvaluatorBrokerLifecycle  # noqa: E402
from agentloop.evaluator.controller import Controller  # noqa: E402
from agentloop.evaluator.public_package import stage as stage_public  # noqa: E402
from agentloop.evaluator.dynamic_case_service import issue  # noqa: E402
from agentloop.evaluator.fixture_service import runtime_case_paths  # noqa: E402
from agentloop.evaluator.lower_agent_launcher import read_broker_stats, run_case  # noqa: E402
from agentloop.evaluator.execution_evidence import prepare_evidence  # noqa: E402
from agentloop.candidate_adapter import build_candidate  # noqa: E402
from harbor.direct_consumer import run_native_builder, direct_builder_runtime, DIRECT_BUILDER_SCRIPT
from harbor.native_builder_evidence import observe_thread, verify_native
from agentloop.evaluator.public_feedback import public_payload
from agentloop.protocol import (  # noqa: E402
    BUILDER_EFFORT, BUILDER_MODEL, DEV_CASES, HIDDEN_CASES,
    LOWER_EFFORT, LOWER_MODEL, PLACEHOLDER_KEY, tree_digest,
    validate_delivery, write_json,
)


BUILDER_IMAGE = "agentswe/create-agent-builder-codex:0.144.1-node24.6.0-0812"
READINESS_PROFILE = "single-dev-two-round-hidden-smoke-v1"
# 5 h Builder cap (D2, 2026-09-19).  The shared driver
# @@AGENTSWE_EDITING_CONTROL@@/formal_commands.py passes
# --builder-timeout 18000, so the generated Harbor task.toml must stop the
# native Builder BEFORE the outer wait SIGTERMs it; otherwise the returncode is
# 124 and both builder_code == 0 gates below void the run.
GENERATED_BUILDER_TASK_TIMEOUT_SECONDS = 17_880
BUILDER_CLEANUP_MARGIN_SECONDS = 120
DEFAULT_OUTER_BUILDER_TIMEOUT_SECONDS = GENERATED_BUILDER_TASK_TIMEOUT_SECONDS + BUILDER_CLEANUP_MARGIN_SECONDS
# The pilot/readiness branch keeps its historical 8 h generated cap: readiness
# passes --builder-timeout 28800 explicitly and its two-round canary approaches
# neither bound, so readiness behaviour is unchanged.
PILOT_BUILDER_TASK_TIMEOUT_SECONDS = 28_800
# run_native_builder turns an outer-deadline SIGTERM into 124 and a
# KeyboardInterrupt into 130.  Only 124 is a survivable interrupt; 125 and 130
# stay fatal.
BUILDER_INTERRUPT_EXIT_CODES = (124,)
# A submission holds the lifecycle lock while both public dev cases run, which
# is far longer than the Builder's shell-tool window.  A read-only action must
# answer inside that window instead of queueing behind the submission, so it
# waits this long for the lock and then falls back to a records snapshot.
READ_ONLY_ACTION_LOCK_TIMEOUT_SECONDS = 20


def builder_timeout_contract(effective_outer_timeout: int) -> dict[str, Any]:
    """Prove the outer deadline covers the generated task and cleanup margin."""
    if isinstance(effective_outer_timeout, bool) or effective_outer_timeout <= 0:
        raise ValueError("effective outer Builder timeout must be a positive integer")
    required = DEFAULT_OUTER_BUILDER_TIMEOUT_SECONDS
    return {
        "generated_task_timeout_seconds": GENERATED_BUILDER_TASK_TIMEOUT_SECONDS,
        "cleanup_margin_seconds": BUILDER_CLEANUP_MARGIN_SECONDS,
        "effective_outer_timeout_seconds": int(effective_outer_timeout),
        "covers_task_plus_cleanup": int(effective_outer_timeout) >= required,
        "required_outer_timeout_seconds": required,
    }


def enforce_builder_timeout_contract(parser, effective_outer_timeout: int) -> dict[str, Any]:
    """argparse gate for the two branches that actually start a Builder."""
    if effective_outer_timeout <= 0:
        parser.error("--builder-timeout must be positive")
    try:
        contract = builder_timeout_contract(effective_outer_timeout)
    except ValueError as exc:
        parser.error(str(exc))
    if not contract["covers_task_plus_cleanup"]:
        parser.error(
            "--builder-timeout must cover generated task timeout plus cleanup margin "
            f"({contract['required_outer_timeout_seconds']} seconds required)")
    return contract


def formal_max_rounds_interrupt(lifecycle: "BuilderLifecycle", exit_code: Any) -> bool:
    """True only for a formal Builder killed after spending every round.

    openwiki never freezes mid-run, so there is no freeze_reason to read: the
    Controller refuses a round outside 1..max_dev_rounds and the freeze happens
    after Builder exit.  A Builder that has consumed the whole accepted-round
    budget and is then killed by the outer deadline has produced complete
    evidence; anything else has not.  Only run_formal calls this; run_pilot,
    which readiness uses, keeps its own builder_code == 0 gate.
    """
    return bool(
        exit_code in BUILDER_INTERRUPT_EXIT_CODES
        and not lifecycle.pilot_not_formal
        and getattr(lifecycle, "readiness_profile", None) is None
        and lifecycle.max_dev_rounds
        and len(lifecycle.controller.records) == lifecycle.max_dev_rounds
    )
BUILDER_BROKER = Path(
        "@@AGENTSWE_EDITING_CONTROL@@/builder_broker_xhigh.py"
)


def now() -> str:
    return datetime.now(timezone.utc).isoformat()


def read_json(path: Path) -> dict[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8"))
    return value if isinstance(value, dict) else {}


def free_port() -> int:
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        return int(listener.getsockname()[1])


def builder_stats(port: int) -> dict[str, Any]:
    request = urllib.request.Request(
        f"http://127.0.0.1:{port}/stats",
        headers={"Authorization": "Bearer stats-only-placeholder"},
    )
    with urllib.request.urlopen(request, timeout=10) as response:
        value = json.loads(response.read())
    return value if isinstance(value, dict) else {}


def result_judge_stats(endpoint: str) -> dict[str, Any]:
    base = endpoint.split("/v1/", 1)[0].rstrip("/")
    request = urllib.request.Request(base + "/stats", headers={"Authorization": "Bearer stats-only-placeholder"})
    with urllib.request.urlopen(request, timeout=10) as response:
        value = json.loads(response.read())
    if not isinstance(value, dict):
        raise ValueError("Result judge broker stats must be an object")
    return value


def fresh_result_judge(stats: dict[str, Any], instance_id: str) -> bool:
    return _judge_runtime().fresh_judge_stats(stats, instance_id)


def start_result_judge(run_dir, credential, upstream, image, *, defer_removal=False):
    broker = _judge_runtime().start_judge_broker(
        name='openwiki-result-judge-' + hashlib.sha256(str(run_dir).encode()).hexdigest()[:16],
        credential=credential, image=image, port=free_port(),
        cidfile=run_dir / 'result_judge_broker' / 'container.cid', upstream=upstream,
        defer_removal=defer_removal)
    try:
        initial = result_judge_stats(broker.endpoint)
        write_json(run_dir / 'result_judge_broker_initial.json', initial)
        if not fresh_result_judge(initial, broker.instance_id):
            raise RuntimeError('Result judge broker failed fresh xhigh/zero-call gate')
        return broker
    except Exception:
        broker.close()
        raise


def start_builder_broker(name: str, port: int, credential: Path, image: str, run_dir: Path) -> str:
    cidfile = run_dir / "builder_container.cid"
    shared='@@AGENTSWE_EDITING_CONTROL@@'
    if shared not in sys.path:sys.path.insert(0,shared)
    from builder_broker_runtime import start_builder_broker as start_shared_builder
    start_shared_builder(name=name,credential=credential,image=image,port=port,cidfile=cidfile)
    return cidfile.read_text().strip()


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


def remove_owned_container(name: str, owned_id: str | None) -> dict[str, Any]:
    record: dict[str, Any] = {
        "name": name, "owned_id": owned_id, "ownership_proven": bool(owned_id),
        "cleanup_attempted": bool(owned_id), "absent_after_cleanup": False,
    }
    if not owned_id:
        record["cleanup_error"] = "no current-run container id was captured"
        return record
    try:
        removed = subprocess.run(["docker", "rm", "-f", owned_id], text=True,
                                 capture_output=True, check=False)
        record["remove_exit_code"] = removed.returncode
        if removed.returncode != 0:
            record["remove_error"] = removed.stderr[-800:]
        if removal_in_progress(removed):
            record["removal_in_progress_at_rm"] = True
            await_daemon_removal(owned_id)
    except Exception as exc:
        record["remove_error"] = f"{type(exc).__name__}: {exc}"
    try:
        inspected = subprocess.run(["docker", "inspect", owned_id], text=True,
                                   capture_output=True, check=False)
        record["inspect_exit_code"] = inspected.returncode
        detail = (inspected.stdout + "\n" + inspected.stderr).lower()
        if inspected.returncode == 0:
            record["absent_after_cleanup"] = False
        elif "no such object" in detail or "no such container" in detail:
            record["absent_after_cleanup"] = True
        else:
            record["inspect_error"] = f"docker inspect exit {inspected.returncode}: {detail[-800:]}"
    except Exception as exc:
        record["inspect_error"] = f"{type(exc).__name__}: {exc}"
    return record


def write_cleanup_attestation(
    run_dir: Path,
    *,
    builder_name: str,
    builder_id: str | None = None,
    lifecycle: "BuilderLifecycle | None",
    brokers: tuple[tuple[str, EvaluatorBrokerLifecycle | None], ...],
    pilot_not_formal: bool,
) -> dict[str, Any]:
    """Record cleanup only after evaluator-owned processes have been stopped."""
    try:
        native_cleanup = run_dir / "builder_container_cleanup.json"
        if native_cleanup.is_file():
            detail = read_json(native_cleanup)
            builder_cleanup = {"name": builder_name, "transport": "native_codex_direct",
                "absent_after_cleanup": detail.get("complete") is True, "cleanup": detail}
        elif builder_id is None:
            builder_cleanup = {"name": builder_name, "transport": "native_codex_direct",
                "absent_after_cleanup": not (run_dir / "owned_builder_process.json").exists(),
                "not_started": not (run_dir / "owned_builder_process.json").exists()}
        else:
            builder_cleanup = remove_owned_container(builder_name, builder_id)
    except Exception as exc:
        builder_cleanup = {
            "name": builder_name, "owned_id": builder_id,
            "ownership_proven": bool(builder_id),
            "cleanup_attempted": bool(builder_id),
            "absent_after_cleanup": False,
            "cleanup_error": f"{type(exc).__name__}: {exc}",
        }
    broker_records: list[dict[str, Any]] = []
    for role, broker in brokers:
        if broker is None:
            broker_records.append({
                "role": role,
                "started": False,
                "status": "not_started",
                "process_stopped": True,
                "complete": True,
            })
            continue
        if isinstance(broker, _judge_runtime().JudgeBroker):
            cleanup = broker.close()
            absent = cleanup.get("absent_after_cleanup") is True
            stats_present = broker.stats_path.is_file()
            broker_records.append({
                "role": role, "started": True, "status": "stopped" if absent else "cleanup_unverified",
                "process_stopped": absent, "container_name": broker.name,
                "container_id": broker.container_id, "container_absent": absent,
                "stats_path": str(broker.stats_path), "stats_present": stats_present,
                "lifecycle_path": str(broker.lifecycle_path),
                "cleanup": cleanup, "complete": absent and stats_present,
            })
            continue
        try:
            lifecycle_record = (
                read_json(broker.lifecycle_path)
                if broker.lifecycle_path.is_file()
                else {}
            )
            process_stopped = broker.process is None or broker.process.poll() is not None
            status = lifecycle_record.get("status")
            container_name = getattr(broker, "container_name", None)
            container_id = getattr(broker, "container_id", None)
            container_absent = lifecycle_record.get("container_absent")
            stats_path = getattr(broker, "stats_path", None)
            stats_present = isinstance(stats_path, Path) and stats_path.is_file()
            complete = (
                process_stopped
                and status in {"stopped", "failed", "startup_failed"}
                and (container_name is None or container_absent is True)
                and stats_present
                and not lifecycle_record.get("cleanup_errors")
            )
            broker_records.append({
                "role": role,
                "started": True,
                "status": status,
                "process_stopped": process_stopped,
                "container_name": container_name,
                "container_id": container_id,
                "container_absent": container_absent,
                "lifecycle_path": str(broker.lifecycle_path),
                "stats_path": str(stats_path) if isinstance(stats_path, Path) else None,
                "stats_present": stats_present,
                "final_stats": lifecycle_record.get("final_stats"),
                "cleanup_errors": lifecycle_record.get("cleanup_errors", []),
                "credential_value_recorded": lifecycle_record.get("credential_value_recorded") is True,
                "complete": complete,
            })
        except Exception as exc:
            broker_records.append({
                "role": role, "started": True, "status": "attestation_error",
                "process_stopped": False, "container_absent": False,
                "complete": False,
                "attestation_error": f"{type(exc).__name__}: {exc}",
            })
    controller_socket_closed = lifecycle is None or not lifecycle.socket_path.exists()
    value = {
        "schema_version": "openwiki-evaluator-cleanup-attestation/v1",
        "pilot_not_formal": pilot_not_formal,
        "recorded_at": now(),
        "builder_container_name": builder_name,
        "builder_container_id": builder_id,
        "builder_container_absent": builder_cleanup["absent_after_cleanup"],
        "builder_cleanup": builder_cleanup,
        "controller_socket_closed": controller_socket_closed,
        "brokers": broker_records,
        "unrelated_containers_touched": False,
    }
    value["completed"] = bool(
        value["builder_container_absent"]
        and controller_socket_closed
        and all(record["complete"] for record in broker_records)
    )
    write_json(run_dir / "cleanup_attestation.json", value)
    return value


class BuilderLifecycle:
    """Authenticated evaluator bridge for one continuous Builder process."""

    def __init__(self, run_dir: Path, workspace: Path, public_endpoint: str, credential: Path,
                 upstream: str, dev_cases: tuple[str, ...] = DEV_CASES,
                 hidden_cases: tuple[str, ...] = HIDDEN_CASES,
                 pilot_not_formal: bool = False, max_dev_rounds: int = 10,
                 result_judge_endpoint: str | None = None,
                 readiness_profile: str | None = None,
                 current_binding: dict[str, Any] | None = None):
        self.run_dir = run_dir
        self.workspace = workspace
        self.token = hashlib.sha256(f"{run_dir}:{time.time_ns()}".encode()).hexdigest()
        self.session_id = None
        self.native_observations = []
        self.lock = threading.RLock()
        self.connection_id = f"openwiki-builder-connection-{self.token[16:32]}"
        self.socket_path = Path(tempfile.gettempdir()) / f"openwiki-builder-{self.token[:20]}.sock"
        self.server: socketserver.ThreadingUnixStreamServer | None = None
        self.events: list[dict[str, Any]] = []
        self.validation_count = 0
        self.max_dev_rounds = max_dev_rounds
        self.controller = Controller(
            ROOT / "input/repository", ROOT, run_dir / "lifecycle", public_endpoint,
            builder_session_id="pending-native-thread-evidence",
            hidden_credential_file=credential,
            hidden_upstream=upstream,
            dev_cases=dev_cases,
            hidden_cases=hidden_cases,
            pilot_not_formal=pilot_not_formal,
            max_dev_rounds=max_dev_rounds,
            result_judge_endpoint=result_judge_endpoint,
            readiness_profile=readiness_profile,
            current_binding=current_binding,
        )
        self.pilot_not_formal = bool(pilot_not_formal)
        self.readiness_profile = readiness_profile
        # A feedback is "received" only when a write to the Builder's socket
        # returned.  The helper can be killed by the Builder's shell-tool
        # timeout while a submission is still running, and the response write
        # then raises BrokenPipeError: the round is accepted and its feedback
        # bytes are on disk, but no receipt exists and verify_native rejects
        # the whole session.  Keep the ready response so a later connection can
        # deliver it for real, and receipt it at that later moment -- never
        # before, and never for a write that did not happen.
        self.feedback_deliveries: list[dict[str, Any]] = []
        self.feedback_write_failures: list[dict[str, Any]] = []
        self.undelivered_feedback: dict[int, dict[str, Any]] = {}

    def event(self, name: str, **fields: Any) -> None:
        event = {
            "event": name, "at": now(), "builder_session_id": self.session_id,
            "builder_connection_id": self.connection_id, **fields,
        }
        with (self.run_dir / "builder_events.jsonl").open("a") as handle:
            handle.write(json.dumps(event, ensure_ascii=False) + "\n")
            handle.flush(); os.fsync(handle.fileno())
        self.events.append(event)

    def observe_native(self):
        observation = observe_thread(self.run_dir, self.native_observations)
        if self.session_id is not None and self.session_id != observation["thread_id"]:
            raise RuntimeError("native Builder thread changed")
        if self.controller.builder_session_id not in ('pending-native-thread-evidence',observation['thread_id']):
            raise RuntimeError('native Builder cannot replace a retained controller session')
        self.session_id = observation["thread_id"]
        self.controller.builder_session_id = self.session_id
        self.native_observations.append(observation)
        write_json(self.run_dir / "native_stream_observations.json", self.native_observations)

    def public_record(self, record):
        reference = record.get("feedback", {})
        feedback = {}
        if reference.get("available"):
            path = Path(reference["json"])
            if path.is_symlink() or not path.resolve().is_relative_to(self.run_dir.resolve()):
                raise ValueError("feedback escaped run")
            feedback = read_json(path)
            canonical = (json.dumps({k: v for k, v in feedback.items() if k != "feedback_digest"},
                sort_keys=True, separators=(",", ":"), ensure_ascii=False) + "\n").encode()
            if hashlib.sha256(canonical).hexdigest() != reference.get("feedback_digest"):
                raise ValueError("feedback bytes changed")
        return {"accepted": record.get("submission_consumed") is True or record.get("idempotent") is True,
            "builder_session_id": self.session_id, "submission_number": record.get("round"),
            "candidate_digest": record.get("candidate_digest"),
            "feedback_digest": reference.get("feedback_digest"), "feedback": feedback,
            "feedback_digest_ack": record.get("revision", {}).get("feedback_digest"),
            "submission_consumed": record.get("submission_consumed") is True,
            "consumes_capability_round": record.get("consumes_capability_round") is True,
            "idempotent": record.get("idempotent") is True,
            "replay_blocked": record.get('replay_blocked') is True,
            "retry_allowed": record.get('retry_allowed') is True,
            "classification": record.get('classification'),
            "product_source_digest": record.get('product_source_digest'),
            "retry_same_round": record.get("retry_same_round") is True}

    @staticmethod
    def _delivered_records(payload):
        """Per-record views of what a response actually carried.

        A submit response is one public_record at top level; a status or
        feedback response carries the same public_record dicts under
        "records".  Both come from public_record, so the same accepted record
        has byte-identical content either way and therefore one receipt digest.
        """
        if isinstance(payload, dict) and payload.get("submission_number"):
            yield payload
        for record in (payload or {}).get("records") or []:
            if isinstance(record, dict) and record.get("submission_number"):
                yield record

    @staticmethod
    def _carries_feedback(record) -> bool:
        """True only when the substantive feedback body itself went out.

        A reference stub such as validate's duplicate_candidate
        {"feedback_digest": ..., "infrastructure_invalid": ...} carries the
        digest without the bytes, and must never count as delivery. The
        authoritative body always names its own digest, the candidate it
        judged, and the per-case results; none of those keys is dropped or
        rewritten by public_payload.
        """
        digest = record.get("feedback_digest")
        feedback = record.get("feedback")
        return bool(record.get("accepted")) and bool(digest) and isinstance(feedback, dict) \
            and feedback.get("feedback_digest") == digest \
            and feedback.get("candidate_digest") == record.get("candidate_digest") \
            and isinstance(feedback.get("cases"), dict) and bool(feedback["cases"])

    def _persist_delivery_ledger(self) -> None:
        write_json(self.run_dir / "native_feedback_deliveries.json", {
            "schema_version": "openwiki-feedback-delivery-ledger/v1",
            "delivered": self.feedback_deliveries,
            "write_failures": self.feedback_write_failures,
            "undelivered_candidate_numbers": sorted(self.undelivered_feedback),
        })

    def feedback_written(self, payload):
        """Receipt every feedback the socket write that just returned carried."""
        for record in self._delivered_records(payload):
            if not self._carries_feedback(record):
                continue
            number = record["submission_number"]
            digest = record["feedback_digest"]
            payload_sha256 = hashlib.sha256(
                json.dumps(record, sort_keys=True, ensure_ascii=False).encode()).hexdigest()
            self.undelivered_feedback.pop(number, None)
            if any(v["candidate_number"] == number and v["feedback_digest"] == digest
                   and v["payload_sha256"] == payload_sha256 for v in self.feedback_deliveries):
                continue
            self.feedback_deliveries.append({
                "candidate_number": number, "feedback_digest": digest,
                "builder_session_id": self.session_id,
                "payload_sha256": payload_sha256, "at": now()})
            self.event("feedback_delivered", candidate_number=number,
                feedback_digest=digest, payload_sha256=payload_sha256)
        self._persist_delivery_ledger()

    def feedback_write_failed(self, payload, exc) -> None:
        """A ready response the Builder's socket never took.

        This is the opposite of a receipt: it records that delivery did not
        happen, and keeps the response deliverable by a later connection.
        """
        numbers = []
        for record in self._delivered_records(payload):
            if not self._carries_feedback(record):
                continue
            number = record["submission_number"]
            numbers.append(number)
            if not any(v["candidate_number"] == number for v in self.feedback_deliveries):
                self.undelivered_feedback[number] = record
        detail = f"{type(exc).__name__}: {exc}"
        self.feedback_write_failures.append({"at": now(), "error": detail,
            "candidate_numbers": numbers, "builder_session_id": self.session_id})
        self.event("feedback_write_failed", candidate_numbers=numbers, error=detail)
        self._persist_delivery_ledger()

    def validate(self) -> tuple[int, dict[str, Any]]:
        self.observe_native()
        self.validation_count += 1
        round_no = len(self.controller.records) + 1
        errors = validate_delivery(self.workspace)
        duplicate = self.controller.duplicate_submission(self.workspace) if not errors else None
        payload = {
            "schema_version": "openwiki-builder-preflight/v1",
            "candidate_number": round_no,
            "submission_consumed": False,
            "delivery_digest": tree_digest(self.workspace) if self.workspace.is_dir() else None,
            "errors": errors,
            "ready_for_submission": False,
        }
        build: dict[str, Any] | None = None
        metadata: dict[str, Any] | None = None
        if duplicate is not None:
            if self.readiness_profile:
                return 409, {"classification": "duplicate_candidate", "ready_for_submission": False,
                    "submission_consumed": False, "replay_blocked": True}
            payload.update({
                "classification": "duplicate_candidate",
                "ready_for_submission": True,
                "idempotent": True,
                "accepted_round": duplicate["accepted_round"],
                "candidate_digest": duplicate["candidate_digest"],
                "feedback": duplicate.get("feedback"),
                "consumes_capability_round": False,
            })
        elif not errors and 1 <= round_no <= self.max_dev_rounds:
            validation_build = self.run_dir / "preflight_builds" / f"candidate_{round_no:03d}_{self.validation_count:03d}"
            shutil.rmtree(validation_build, ignore_errors=True)
            build = build_candidate(
                self.controller.source,
                self.workspace,
                validation_build,
                run_build=True,
                readiness_profile=self.readiness_profile,
            )
            if build.get('valid'):
                product=self.controller.checked_product_identity(build,validation_build)
                prior=self.controller.product_attempts.lookup(product)
                if prior is not None:
                    retained=self.controller.product_replay_record(prior,{
                        'round':round_no,'builder_session_id':self.session_id,
                        'candidate_digest':payload['delivery_digest'],'product_source_digest':product,
                        'dev_cases':{},'feedback':{'available':False}})
                    payload=self.public_record(retained)
                    payload['ready_for_submission']=False
                    self.event('product_execution_replay_blocked',product_source_digest=product)
                    return 409,payload
            parent_digest = self.controller.records[-1]["candidate_digest"] if round_no >= 2 else None
            expected_feedback = (
                self.controller.records[-1].get("feedback", {}).get("feedback_digest")
                if round_no >= 2 else None
            )
            metadata = self.controller._builder_metadata(
                self.workspace, round_no, parent_digest, expected_feedback
            )
            payload["build"] = build
            payload["builder_metadata"] = metadata
            payload["ready_for_submission"] = bool(
                build.get("valid") is True and metadata.get("valid") is True
            )
            if not payload["ready_for_submission"]:
                payload["classification"] = (
                    build.get("classification") if build.get("valid") is not True
                    else "candidate_session_contract_failure"
                )
        elif round_no > self.max_dev_rounds:
            payload["classification"] = "candidate_rounds_exhausted"
        path = self.run_dir / "preflight" / f"attempt_{self.validation_count:03d}.json"
        write_json(path, payload)
        payload["evidence"] = str(path)
        self.event(
            "candidate_preflight",
            ready=payload["ready_for_submission"],
            evidence=str(path),
        )
        if payload["ready_for_submission"]:
            return 200, payload
        classification = str(payload.get("classification") or "candidate_delivery_failure")
        infra = bool((build or {}).get("infrastructure_invalid")) or classification in {
            "native_binding_failure", "infrastructure_failure", "broker_failure",
            "provider_failure", "credential_mount_failure", "evaluator_failure",
            "mount_isolation_failure", "docker_failure", "launcher_failure",
        }
        return (503 if infra else 422), payload

    def submit(self) -> tuple[int, dict[str, Any]]:
        code, preflight = self.validate()
        if code != 200:
            return code, preflight
        round_no = len(self.controller.records) + 1
        if not preflight.get("idempotent") and not 1 <= round_no <= self.max_dev_rounds:
            return 409, {"error": f"maximum of {self.max_dev_rounds} accepted submissions reached"}
        self.event("submission_started", round=round_no, delivery_digest=tree_digest(self.workspace))
        record = self.controller.submit(self.workspace, round_no, evaluate_dev=True)
        self.event(
            "submission_finished", round=round_no,
            candidate_digest=record.get("candidate_digest"),
            feedback=record.get("feedback"),
        )
        payload = self.public_record(record)
        successful = record.get("submission_consumed") is True or record.get("idempotent") is True
        infrastructure=record.get('feedback',{}).get('infrastructure_invalid') is True
        return (409 if record.get('replay_blocked') else (200 if successful else (503 if infrastructure else 422))), payload

    def status(self) -> tuple[int, dict[str, Any]]:
        self.observe_native()
        return 200, {
            "builder_session_id": self.session_id,
            "builder_connection_id": self.connection_id,
            "records": [self.public_record(record) for record in self.controller.records],
            "frozen": self.controller.frozen is not None,
        }

    def status_snapshot(self) -> tuple[int, dict[str, Any]]:
        """Read-only view taken while a submission holds the lock.

        controller.records only gains a record after that record's feedback
        file has been written, so every record visible here is complete.  No
        native observation is taken and no state is mutated; the session id was
        already pinned by the observation that admitted these records, and a
        snapshot is refused before that pin exists.
        """
        if self.session_id is None:
            raise RuntimeError("native Builder session is not observed yet")
        return 200, {
            "builder_session_id": self.session_id,
            "builder_connection_id": self.connection_id,
            "records": [self.public_record(record) for record in list(self.controller.records)],
            "frozen": self.controller.frozen is not None,
            "state": "submission_in_progress",
        }

    def feedback(self) -> tuple[int, dict[str, Any]]:
        """Re-deliver an accepted feedback whose earlier response was lost.

        This re-reads the same authoritative feedback file the accepted record
        already names; it issues nothing new and consumes no round.  The
        oldest response that a failed write left undelivered is preferred.
        """
        self.observe_native()
        records = self.controller.records
        if not records:
            return 404, {"error": "no accepted submission has feedback yet"}
        pending = [n for n in sorted(self.undelivered_feedback) if 1 <= n <= len(records)]
        return 200, self.public_record(records[(pending[0] if pending else len(records)) - 1])

    def start(self) -> None:
        lifecycle = self

        class Handler(socketserver.StreamRequestHandler):
            def handle(self) -> None:
                try:
                    request = json.loads(self.rfile.readline(1 << 20))
                    action = request.get("action")
                    if request.get("token") != lifecycle.token:
                        code, payload = 401, {"error": "unauthorized"}
                    elif action in {"validate", "submit", "status", "feedback"}:
                        # A read-only action must answer inside the Builder's
                        # shell-tool window even while a submission holds the
                        # lock for the length of both public dev cases.
                        read_only = action in {"status", "feedback"}
                        held = lifecycle.lock.acquire(
                            timeout=(READ_ONLY_ACTION_LOCK_TIMEOUT_SECONDS if read_only else -1))
                        if held:
                            try:
                                code, payload = getattr(lifecycle, action)()
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
                except OSError as exc:
                    # The Builder's helper was killed, or its shell tool timed
                    # out, while the evaluator was still running the dev cases.
                    # Nothing arrived, so nothing is receipted: record the
                    # failed write and keep the ready response deliverable by a
                    # later --status, --feedback, or identical re-submit.
                    if code == 200:
                        lifecycle.feedback_write_failed(payload, exc)
                    return
                if code == 200:
                    lifecycle.feedback_written(payload)

        class Server(socketserver.ThreadingUnixStreamServer):
            daemon_threads = True

        self.socket_path.unlink(missing_ok=True)
        self.server = Server(str(self.socket_path), Handler)
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def close(self) -> None:
        if self.server is not None:
            self.server.shutdown()
            self.server.server_close()
            self.server = None
        self.socket_path.unlink(missing_ok=True)


def stage_builder_task(
    run_dir: Path,
    public: Path,
    workspace: Path,
    lifecycle: BuilderLifecycle,
    provider: Path,
    image: str,
    builder_node_modules: Path | None = None,
) -> Path:
    # Formal must stop inside the outer --builder-timeout; the pilot/readiness
    # branch keeps the historical cap it has always been launched with.
    builder_task_timeout_seconds = (
        PILOT_BUILDER_TASK_TIMEOUT_SECONDS if lifecycle.pilot_not_formal
        else GENERATED_BUILDER_TASK_TIMEOUT_SECONDS
    )
    if builder_node_modules is None:
        builder_node_modules = ROOT / ".runtime/candidate-smoke/repository/node_modules"
    builder_node_modules = builder_node_modules.resolve()
    from agentloop.protocol import validate_internal_symlinks
    if not builder_node_modules.is_dir():
        raise RuntimeError("evaluator-owned complete offline dependencies are unavailable")
    validate_internal_symlinks(builder_node_modules)
    package = read_json(public / "input/repository/package.json")
    names = set(package.get("dependencies", {})) | set(package.get("devDependencies", {}))
    missing = sorted(name for name in names if not (builder_node_modules / name).exists())
    if missing:
        raise RuntimeError("evaluator-owned dependency snapshot is incomplete: " + ", ".join(missing))
    lifecycle.builder_node_modules = builder_node_modules
    builder_node = (ROOT / ".runtime/node-v22.12.0-linux-x64").resolve()
    if not (builder_node / "bin/node").is_file():
        raise RuntimeError("evaluator-owned Node 22 runtime is unavailable")
    write_json(run_dir / "builder_dependencies.json", {
        "path": str(builder_node_modules), "declared_packages": sorted(names), "missing": missing,
        "package_json_sha256": hashlib.sha256((public / "input/repository/package.json").read_bytes()).hexdigest(),
        "scope": "complete preinstalled dependency tree only; no prior Candidate implementation mount",
        "builder_mount": "/builder-dependencies/node_modules", "read_only": True,
        "node_runtime": str(builder_node), "node_version": "22.12.0",
        "node_binary_sha256": hashlib.sha256((builder_node / "bin/node").read_bytes()).hexdigest(),
        "build_setting": "OPENWIKI_NODE_MODULES scoped to this Builder invocation"})
    worktree = run_dir / "builder_workspace/worktree"
    shutil.copytree(public / "input/repository", worktree, symlinks=True,
                    ignore=shutil.ignore_patterns(".git", "__pycache__"))
    git_env = {"PATH": "/usr/bin:/bin", "LANG": "C.UTF-8", "GIT_CONFIG_NOSYSTEM": "1",
               "GIT_CONFIG_GLOBAL": "/dev/null", "HOME": str(worktree)}
    for command in (["git", "init", "-q"], ["git", "add", "-A"],
        ["git", "-c", "user.name=Builder", "-c", "user.email=builder@example.invalid",
         "-c", "core.hooksPath=/dev/null", "-c", "commit.gpgSign=false", "commit", "-qm", "public baseline"]):
        subprocess.run(command, cwd=worktree, env=git_env, check=True, capture_output=True, text=True, timeout=60)
    task = run_dir / "builder_task"
    (task / "environment").mkdir(parents=True)
    (task / "tests").mkdir(parents=True)
    dependency_mount: dict[str, object] | None = None
    dependency_note = ""
    if builder_node_modules is not None:
        dependency_source = builder_node_modules.resolve()
        if not dependency_source.is_dir() or not (dependency_source / "typescript/bin/tsc").is_file():
            raise RuntimeError("--builder-node-modules must be a complete evaluator-owned dependency tree")
        dependency_mount = {
            "type": "bind",
            "source": str(dependency_source),
            "target": "/builder-dependencies/node_modules",
            "read_only": True,
        }
        dependency_note = """
An evaluator-owned, read-only dependency tree is available at
`/builder-dependencies/node_modules`. It contains package dependencies only,
not task answers. After creating a writable repository copy, symlink that path
as the copy's `node_modules` instead of reinstalling dependencies.
The configured `node` is Node 22.12.0 from `/builder-runtime/node/bin`, matching
the supplied native SQLite dependency and evaluator runtime.
"""
    (task / "task.toml").write_text(f'''schema_version = "1.4"
[task]
name = "local/openwiki-agentloop-builder"
version = "1.0.0"
description = "OpenWiki Edit same-session feedback Builder"
artifacts = [{{ source = "/workspace/submission", destination = "builder_submission" }}]
[agent]
timeout_sec = {float(builder_task_timeout_seconds)}
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
command = "python3 /usr/local/lib/agentswe-builder-resource-check.py"
retries = 1
timeout_sec = 10.0
''', encoding="utf-8")
    revision_instruction = "You may finish normally after the first accepted version. If you choose another submission, make a genuine revision."
    if lifecycle.readiness_profile:
        revision_instruction = ("Complete exactly TWO accepted rounds on dev_001 in this uninterrupted native session. "
            "For round 1, include revision_of_candidate_digest:null and feedback_digest:null in run_report.json. "
            "After round 1, consume its full feedback bytes and exact digest; revise the product meaningfully and "
            "explain the feedback response in edit_report.json feedback_response. A changed report alone is insufficient. "
            "Run validate_dev_candidate before each submit; it builds using the evaluator toolchain without lower calls. "
            "Use repository-relative patch paths and stable lockfiles. Infrastructure-invalid attempts do not count. "
            "Exit only after round 2 is accepted, regardless of functional score.")
    pilot_note = "" if not lifecycle.pilot_not_formal else """
This is a non-formal pipeline pilot: both public dev cases are visible and the evaluator
runs only test_001 after freezing the latest accepted submission.
No formal Result or Code score will be published.
"""
    if lifecycle.readiness_profile:
        pilot_note = "This readiness-only pilot exposes dev_001 only and runs evaluator-owned test_001 after Builder exit."
    (task / "instruction.md").write_text(f"""# OpenWiki Edit Builder

You are one continuous Builder session using {BUILDER_MODEL}/{BUILDER_EFFORT}.
Read /builder-package/input and /builder-package/dev_cases, and work in the
complete writable baseline at /workspace/worktree. Never inspect hidden cases,
evaluator source, unrelated credentials, prior runs, or Candidate snapshots.
Run `submit_dev_candidate --status` to obtain this actual native thread ID.

Maintain exactly solution.patch, edit_report.json, and run_report.json under
/workspace/submission. Use submission_number 1 for the first accepted
submission. Read the evaluator feedback after an accepted submission.
{revision_instruction} For each later submission, run_report.json must use
the same builder_session_id returned by `--status`, the next sequential
submission_number, revision_of_candidate_digest equal to the latest accepted
candidate_digest, and feedback_digest equal to the exact evaluator feedback
value. Regenerate all three delivery files, validate, and submit. You may make
up to {lifecycle.max_dev_rounds} accepted submissions in this one continuous
session. Infrastructure-invalid attempts do not consume a submission number.
When you finish improving the solution, exit; the evaluator freezes the latest
accepted submission after Builder exit. The submit command prints the full
public feedback and its digest; consume those bytes before revising and include
the preceding digest literally in your revision submission command using
`submit_dev_candidate --feedback-digest <exact-digest>`. Do not assume a dev score above
60 ends the session.

A submission runs both public dev cases and can take more than fifteen minutes,
so `submit_dev_candidate` does that work in a detached helper and returns early
rather than holding one long command. If it prints status 202 with state
submission_in_progress, run `submit_dev_candidate --await-only` again, as many
times as needed, until it prints the real response. Never wrap the command in
`nohup`, never background or `disown` it, and never kill it. At any time,
including while a submission is running, `submit_dev_candidate --status`
answers within seconds with every accepted record and its full feedback, and
`submit_dev_candidate --feedback` reprints the evaluator feedback and its exact
digest for the latest accepted submission. If a response is ever lost anyway,
use one of those two commands, or resend the identical delivery unchanged --
the evaluator answers an identical candidate idempotently without consuming a
submission number.
{dependency_note}
{pilot_note}
""", encoding="utf-8")
    client = task / "environment/submit_dev_candidate"
    client.write_text('''#!/usr/bin/env python3
"""Builder-side controller client.

A submission runs both public dev cases, which takes far longer than a shell
tool call. The submit therefore runs in a detached, fully daemonised child that
holds the socket until the evaluator answers, and the foreground process only
polls a local response file in short bounded waits. A shell-tool timeout can
kill the poller without ever cutting the evaluator's response off.
"""
import argparse, json, os, socket, sys, time
from pathlib import Path

TMP = Path(os.environ.get("TMPDIR") or "/tmp")
RESPONSE = TMP / "agentswe-submit-response.json"
HOLDER = TMP / "agentswe-submit-holder.pid"
VALIDATE_RESPONSE = TMP / "agentswe-validate-response.json"
VALIDATE_HOLDER = TMP / "agentswe-validate-holder.pid"

parser = argparse.ArgumentParser(prog="submit_dev_candidate")
parser.add_argument("--status", action="store_true", help="accepted records and their feedback; always answers quickly")
parser.add_argument("--validate", action="store_true", help="delivery + build preflight without consuming a round")
parser.add_argument("--feedback", action="store_true", help="reprint the evaluator feedback and digest already produced")
parser.add_argument("--feedback-digest", dest="feedback_digest", help="digest of the feedback this revision answers")
parser.add_argument("--wait", action="store_true", help="accepted for compatibility; a submit always waits by polling")
parser.add_argument("--await-only", dest="await_only", action="store_true", help="keep polling the submission already running")
parser.add_argument("--poll-seconds", dest="poll_seconds", type=float, default=240.0, help="how long this call polls before reporting 202")
args, _extra = parser.parse_known_args()
name = os.path.basename(sys.argv[0])
if args.status:
    action = "status"
elif args.feedback:
    action = "feedback"
elif args.validate or name == "validate_dev_candidate":
    action = "validate"
else:
    action = "submit"


def call(name):
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as sock:
        sock.connect(os.environ["AGENTSWE_DEV_CONTROLLER_SOCKET"])
        sock.sendall((json.dumps({"token": os.environ["AGENTSWE_DEV_CONTROLLER_TOKEN"],
                                  "action": name}) + "\\n").encode())
        return json.loads(sock.makefile().readline())


def finish(result):
    print(json.dumps(result, ensure_ascii=False))
    raise SystemExit(0 if result.get("status") == 200 else 2)


def holder_alive():
    try:
        os.kill(int(HOLDER.read_text().strip()), 0)
    except (OSError, ValueError):
        return False
    return True


def alive(pid_path):
    try:
        os.kill(int(pid_path.read_text().strip()), 0)
    except (OSError, ValueError):
        return False
    return True


def poll_path(path, deadline):
    while True:
        try:
            return json.loads(path.read_text())
        except (OSError, ValueError):
            pass
        if time.time() >= deadline:
            return None
        time.sleep(2)


def poll(deadline):
    return poll_path(RESPONSE, deadline)


def detach(request, response_path, holder_path):
    # Run one request in a fully daemonised holder. The foreground caller only
    # polls response_path, so a shell-tool timeout kills the poller and never
    # the evaluator connection: the answer still lands on disk and the next
    # call reprints it instead of the evaluator writing to a closed pipe.
    for item in (response_path, holder_path):
        try:
            item.unlink()
        except OSError:
            pass
    if os.fork() == 0:
        os.setsid()
        if os.fork() != 0:
            os._exit(0)
        null = os.open(os.devnull, os.O_RDWR)
        for fd in (0, 1, 2):
            os.dup2(null, fd)
        holder_path.write_text(str(os.getpid()))
        try:
            result = call(request)
        except Exception as exc:
            result = {"status": 599, "payload": {"error": type(exc).__name__ + ": " + str(exc)}}
        part = response_path.with_suffix(".part")
        part.write_text(json.dumps(result, ensure_ascii=False))
        os.replace(str(part), str(response_path))
        try:
            holder_path.unlink()
        except OSError:
            pass
        os._exit(0)
    os.wait()


if action == "validate":
    # The preflight copies the delivery and runs the controlled build, which can
    # outlast one shell tool call. Route it through the same detached holder the
    # submit uses: a killed foreground poller loses nothing, and running the
    # same command again (optionally with --await-only) reprints the preflight
    # the holder already stored.
    if not args.await_only and not alive(VALIDATE_HOLDER):
        detach("validate", VALIDATE_RESPONSE, VALIDATE_HOLDER)
    preflight = poll_path(VALIDATE_RESPONSE, time.time() + max(1.0, args.poll_seconds))
    if preflight is None:
        print(json.dumps({"status": 202, "payload": {"state": "validate_in_progress",
            "reason": "the evaluator preflight is still building this Candidate; run "
                      "submit_dev_candidate --validate --await-only again to keep waiting. "
                      "The preflight result is kept on disk, so a shell-tool timeout never "
                      "loses it and the next call reprints it"}},
            ensure_ascii=False, indent=2))
        raise SystemExit(3)
    finish(preflight)

if action != "submit":
    finish(call(action))

if args.feedback_digest:
    report = json.loads(Path("/workspace/submission/run_report.json").read_text())
    if report.get("feedback_digest") != args.feedback_digest:
        raise SystemExit("run_report feedback_digest differs from explicit acknowledgement")

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
        # Detach every inherited descriptor so the Builder's shell tool sees
        # EOF immediately and never waits on this holder.
        null = os.open(os.devnull, os.O_RDWR)
        for fd in (0, 1, 2):
            os.dup2(null, fd)
        HOLDER.write_text(str(os.getpid()))
        try:
            result = call("submit")
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
        ensure_ascii=False))
    raise SystemExit(3)
finish(result)
''', encoding="utf-8")
    client.chmod(0o755)
    (task / "tests/test.sh").write_text("printf '0\\n' > /logs/verifier/reward.txt\nexit 0\n", encoding="utf-8")
    (task / "tests/test.sh").chmod(0o755)
    provider_host_path = provider.resolve()
    if not provider_host_path.is_absolute() or not provider_host_path.is_file():
        raise RuntimeError("Builder provider config must be an existing run-local host file")
    resource_check = task / "environment/builder_resource_check.py"
    shutil.copyfile(ROOT / "harbor/builder_resource_check.py", resource_check)
    codex_wrapper = task / "environment/codex"
    shutil.copy2(Path(__file__).resolve().parent / "codex_feature_wrapper.py", codex_wrapper)
    codex_wrapper.chmod(0o755)
    volumes: list[dict[str, object]] = [
            {"type": "bind", "source": str(resource_check), "target": "/usr/local/lib/agentswe-builder-resource-check.py", "read_only": True},
            {"type": "bind", "source": str(builder_node), "target": "/builder-runtime/node", "read_only": True},
            {"type": "bind", "source": str(public), "target": "/builder-package", "read_only": True},
            {"type": "bind", "source": str(worktree), "target": "/workspace/worktree"},
            {"type": "bind", "source": str(workspace), "target": "/workspace/submission"},
            {"type": "bind", "source": str(lifecycle.socket_path), "target": "/run/openwiki-builder.sock", "read_only": True},
            {"type": "bind", "source": str(client), "target": "/usr/local/bin/submit_dev_candidate", "read_only": True},
            {"type": "bind", "source": str(client), "target": "/usr/local/bin/validate_dev_candidate", "read_only": True},
            {"type": "bind", "source": str(codex_wrapper), "target": "/workspace/codex-wrapper/codex", "read_only": True},
            {"type": "bind", "source": str(provider_host_path), "target": str(provider_host_path), "read_only": True},
    ]
    if dependency_mount is not None:
        volumes.append(dependency_mount)
    write_json(task / "environment/docker-compose.yaml", {"services": {"main": {
        "cpu_quota": 800000, "cpu_period": 100000,
        "volumes": volumes,
        "environment": {
            "PATH": "/workspace/codex-wrapper:/builder-runtime/node/bin:/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin",
            "AGENTSWE_DEV_CONTROLLER_SOCKET": "/run/openwiki-builder.sock",
            "AGENTSWE_DEV_CONTROLLER_TOKEN": lifecycle.token,
        },
    }}})
    config = run_dir / "builder_job_config.json"
    write_json(config, {
        "job_name": f"openwiki-agentloop-{run_dir.name}",
        "jobs_dir": str(run_dir / "jobs"), "n_attempts": 1,
        "n_concurrent_trials": 1, "quiet": True, "retry": {"max_retries": 0},
        "environment": {"type": "docker", "delete": not bool(lifecycle.readiness_profile)},
        "agents": [{"import_path": "agentswe_codex_resume:CodexResume", "model_name": BUILDER_MODEL, "env": {
            "CODEX_HOME": "/tmp/agentswe-codex-home", "CODEX_CONFIG_TOML_PATH": str(provider_host_path),
            "PATH": "/workspace/codex-wrapper:/builder-runtime/node/bin:/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin",
        }, "kwargs": {"reasoning_effort": BUILDER_EFFORT, "web_search": "live"}}],
        "tasks": [{"path": str(task)}],
    })
    return config


def static_summary() -> dict[str, Any]:
    required = [
        ROOT / "agentloop/evaluator/controller.py",
        ROOT / "agentloop/evaluator/broker.py",
        ROOT / "agentloop/evaluator/lower_agent_launcher.py",
        ROOT / "agentloop/evaluator/hidden_controller.py",
        ROOT / "agentloop/evaluator/prepare_node22_runtime.sh",
    ]
    missing = [str(path.relative_to(ROOT)) for path in required if not path.is_file()]
    return {
        "schema_version": "openwiki-agentloop-one-stop-static/v1",
        "status": "static_config_ready" if not missing else "static_config_invalid",
        "missing": missing,
        "formal_execution_started": False, "network_calls": 0,
        "docker_started": False, "harbor_started": False,
        "builder": {"model": BUILDER_MODEL, "reasoning_effort": BUILDER_EFFORT, "single_continuous_session": True},
        "lower": {"product": "OpenWiki", "model": LOWER_MODEL, "reasoning_effort": LOWER_EFFORT, "candidate_credential": PLACEHOLDER_KEY},
        "public_cases": list(DEV_CASES), "hidden_cases": list(HIDDEN_CASES),
        "hidden_after_freeze": True, "formal_result_claimed": False,
        "code_score_claimed": False,
    }


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


def run_harbor(harbor: Path, config: Path, run_dir: Path, timeout: int) -> int:
    with (run_dir / "builder.stdout.log").open("w") as stdout, (run_dir / "builder.stderr.log").open("w") as stderr:
        process = subprocess.Popen([str(harbor), "run", "-c", str(config)], stdout=stdout, stderr=stderr, start_new_session=True)
        try:
            return process.wait(timeout=timeout)
        except (subprocess.TimeoutExpired, KeyboardInterrupt):
            os.killpg(process.pid, signal.SIGTERM)
            try:
                process.wait(timeout=15)
            except subprocess.TimeoutExpired:
                os.killpg(process.pid, signal.SIGKILL)
                process.wait()
            return 124 if isinstance(sys.exc_info()[1], subprocess.TimeoutExpired) else 130


def run_pilot_hidden(lifecycle: BuilderLifecycle, broker: EvaluatorBrokerLifecycle) -> dict[str, Any]:
    """Execute only test_001 with a fresh post-freeze evaluator broker."""
    from agentloop.evaluator.hidden_controller import (
        _claim_hidden_once, hidden_lifecycle_fields, validate_freeze,
    )
    from agentloop.protocol import file_sha256

    controller = lifecycle.controller
    freeze = controller.frozen
    if not isinstance(freeze, dict) or freeze.get("pilot_not_formal") is not True:
        raise RuntimeError("pilot hidden requires the reduced latest-accepted-Candidate freeze")
    if not broker.endpoint:
        raise RuntimeError("pilot hidden broker has no endpoint")
    before = read_broker_stats(broker.endpoint)
    if any(int(before.get(key, 0) or 0) != 0 for key in ("calls", "successful_calls", "failures", "provider_failures")):
        raise RuntimeError("pilot hidden broker must be fresh with zero calls")
    freeze_path = controller.run_dir / "freeze_manifest.json"
    frozen = validate_freeze(freeze, controller.run_dir, freeze_path, allow_pilot_test_001=True)
    digest_before = tree_digest(frozen)
    if digest_before != freeze.get("repository_digest"):
        raise RuntimeError("frozen Candidate changed before pilot hidden")
    gate = _claim_hidden_once(controller.run_dir, freeze_path, freeze, expected_cases=("test_001",))
    case_id = "test_001"
    case_setup_started = time.monotonic()
    started = now()
    case_dir = lifecycle.run_dir / "pilot_hidden_after_freeze" / case_id
    request_root = lifecycle.run_dir / "pilot_hidden_after_freeze" / "requests"
    case_root = controller._case_root(case_id)
    task_file = case_root / case_id / "input.md"
    task = task_file.read_text(encoding="utf-8")
    issued = issue(
        case_id, controller._case_template(case_id),
        request_root / "evaluator", request_root / "candidate", task,
    )
    runtime = runtime_case_paths(case_id, case_root, case_dir / "fixture")
    run = run_case(
        frozen, Path(issued["candidate_request"]), case_dir, broker.endpoint,
        working_directory=Path(runtime["repository"]),
        case_setup_started=case_setup_started,
        fixture_case_id=case_id, fixture_cases_root=case_root,
        fixture_output=case_dir / "fixture",
    )
    after = read_broker_stats(broker.endpoint)
    digest_after = tree_digest(frozen)
    delta = run.get("broker_delta", {}) if isinstance(run, dict) else {}
    execution = prepare_evidence(run, case_id=case_id, candidate_digest=freeze['candidate_digest'],
        repository=frozen, output=case_dir, request=issued['candidate_request'], cases_root=case_root, initial=runtime['repository'])
    execution.update(case_started_at=started, frozen_digest_before=digest_before,
        frozen_digest_after=digest_after)
    cases = {case_id: execution}
    lifecycle_fields = hidden_lifecycle_fields(freeze, cases, gate, (case_id,))
    behavior_evaluable = (
        not bool(execution.get("infrastructure_invalid"))
        and int(delta.get("successful_calls", 0) or 0) > 0
        and lifecycle_fields["frozen_digest_stable"]
        and lifecycle_fields["all_cases_started_after_freeze"]
    )
    attestation = {
        "schema_version": "openwiki-agentloop-pilot-hidden-attestation/v1",
        "pilot_not_formal": True, "hidden_after_freeze": True,
        "case_ids": [case_id], "executed_case_ids": [case_id],
        "hidden_started_at": gate["hidden_started_at"], "fresh_broker_initial_calls_zero": True,
        "freeze_manifest": str(freeze_path), "freeze_manifest_sha256": file_sha256(freeze_path),
        "hidden_once_gate": gate["path"],
        "broker_instance_id": before.get("broker_instance_id"),
        "broker_before": before, "broker_after": after, "broker_delta": delta,
        "frozen_digest_before": digest_before, "frozen_digest_after": digest_after,
        "frozen_digest_stable": digest_after == digest_before,
        "candidate_credential": PLACEHOLDER_KEY,
        "credential_mounted_to_candidate": False,
        "product_lower_executed": bool(run.get("product_started")),
        "behavior_evaluable": behavior_evaluable,
        "run": run, "cases": cases,
        "formal_result_publishable": False, "formal_code_publishable": False,
        "result_axis": "N/A", "code_axis": "N/A",
    }
    attestation.update(lifecycle_fields)
    write_json(lifecycle.run_dir / "pilot_hidden_after_freeze_attestation.json", attestation)
    write_json(lifecycle.run_dir / 'hidden-after-freeze-attestation.json', attestation)
    return attestation


def run_pilot(args: argparse.Namespace) -> int:
    readiness_profile = getattr(args, 'readiness_profile', None)
    public_cases = ('dev_001',) if readiness_profile else DEV_CASES
    current_binding = None
    if readiness_profile:
        path = args.readiness_binding_file
        if path is None or path.is_symlink() or not path.is_file():
            raise ValueError('readiness requires evaluator-owned binding file')
        data = path.read_bytes()
        if hashlib.sha256(data).hexdigest() != args.readiness_binding_sha256:
            raise ValueError('coordinator binding file SHA mismatch')
        current_binding = json.loads(data)
        shared = Path('@@AGENTSWE_EDITING_CONTROL@@')
        if str(shared) not in sys.path:
            sys.path.insert(0, str(shared))
        from readiness_binding import verify_binding
        current_binding = verify_binding(ROOT, current_binding)
    run_dir = args.run_dir.resolve()
    if run_dir.exists():
        raise RuntimeError(f"pilot run directory must not exist: {run_dir}")
    run_dir.mkdir(parents=True)
    if current_binding:
        write_json(run_dir / 'readiness_current_binding.json', current_binding)
    credential = args.credential_file.resolve()
    builder_port = free_port()
    builder_name = "openwiki-pilot-builder-" + hashlib.sha256(str(run_dir).encode()).hexdigest()[:12]
    lifecycle: BuilderLifecycle | None = None
    public_broker: EvaluatorBrokerLifecycle | None = None
    hidden_broker: EvaluatorBrokerLifecycle | None = None
    result_judge_broker: EvaluatorBrokerLifecycle | None = None
    builder_id: str | None = None
    try:
        for path in (args.harbor.resolve(), credential, DIRECT_BUILDER_SCRIPT):
            if not path.exists():
                raise FileNotFoundError(path)
        public_broker = EvaluatorBrokerLifecycle(run_dir / "public_broker", credential, args.upstream,
            docker_image=args.builder_image, defer_removal=bool(readiness_profile))
        public_broker.__enter__()
        if not public_broker.endpoint:
            raise RuntimeError("public lower broker has no endpoint")
        result_judge_broker = start_result_judge(run_dir, credential, args.upstream, args.builder_image,
            defer_removal=bool(readiness_profile))
        public = run_dir / "builder_public_package"
        public_manifest = stage_public(ROOT, public, public_cases)
        workspace = run_dir / "builder_workspace/submission"
        workspace.mkdir(parents=True)
        lifecycle = BuilderLifecycle(
            run_dir, workspace, public_broker.endpoint, credential, args.upstream,
            dev_cases=public_cases, hidden_cases=("test_001",), pilot_not_formal=True,
            max_dev_rounds=args.max_dev_rounds,
            result_judge_endpoint=result_judge_broker.endpoint,
            readiness_profile=readiness_profile,
            current_binding=current_binding,
        )
        lifecycle.start()
        provider = run_dir / "builder_direct_provider.toml"
        direct_builder_runtime().write_provider(provider)
        if readiness_profile:
            from readiness_binding import configure_native_no_replay
            write_json(run_dir / 'readiness_native_retry_policy.json', configure_native_no_replay(provider))
        config = stage_builder_task(
            run_dir, public, workspace, lifecycle, provider, args.builder_image,
            args.builder_node_modules,
        )
        write_json(run_dir / "protocol_lock.json", {
            "schema_version": "openwiki-agentloop-pilot-protocol/v1",
            "pilot_not_formal": True,
            "builder": {"model": BUILDER_MODEL, "reasoning_effort": BUILDER_EFFORT, "single_continuous_session": True},
            "public_lower": {"model": LOWER_MODEL, "reasoning_effort": LOWER_EFFORT, "candidate_credential": PLACEHOLDER_KEY},
            "hidden_lower": {"model": LOWER_MODEL, "reasoning_effort": LOWER_EFFORT, "candidate_credential": PLACEHOLDER_KEY, "fresh_after_freeze": True},
            "public_cases": list(public_cases), "hidden_cases": ["test_001"],
            "readiness_profile": readiness_profile, "score_threshold": None,
            "builder_public_package": public_manifest,
            "formal_result_claimed": False, "code_score_claimed": False,
            "result_axis": "N/A", "code_axis": "N/A",
        })
        started = now()
        builder_code = run_native_builder(lifecycle=lifecycle, config=config, credential=credential,
            harbor=args.harbor.resolve(), timeout=args.builder_timeout).returncode
        ended = now()
        native = verify_native(run_dir, lifecycle.controller.records,
            [e for e in lifecycle.events if e["event"] == "feedback_delivered"], lifecycle.native_observations)
        write_json(run_dir / "native_builder_evidence.json", native)
        if builder_code == 0 and native["valid"] and lifecycle.controller.records and lifecycle.controller.frozen is None:
            lifecycle.controller.freeze(builder_exit_evidence={
                'returncode': builder_code, 'native_valid': native['valid'],
                'builder_session_id': lifecycle.session_id, 'finished_at': ended})
        records = lifecycle.controller.records
        attestation = {
            "schema_version": "openwiki-pilot-builder-session-attestation/v1",
            "pilot_not_formal": True, "builder_session_id": lifecycle.session_id,
            "builder_connection_id": lifecycle.connection_id,
            "builder_model": BUILDER_MODEL, "builder_reasoning_effort": BUILDER_EFFORT,
            "single_continuous_session": native["valid"], "single_harbor_invocation": native["valid"],
            "native_evidence": native,
            "builder_exit_code": builder_code, "started_at": started, "finished_at": ended,
            "candidate_records": records,
            "accepted_submission_count": len(records),
            "accepted_submissions_with_public_dev": bool(records) and all(set(record.get("dev_cases", {})) == set(public_cases) for record in records),
            "feedback_chain_consumed": bool(records) and all(index == 0 or bool(record.get("revision", {}).get("feedback_bound_submission")) for index, record in enumerate(records)),
            "candidate_digests_distinct": len({record.get("candidate_digest") for record in records}) == len(records) if records else False,
            "freeze": lifecycle.controller.frozen,
        }
        attestation["complete"] = all((
            builder_code == 0, native["valid"], len(records) == 2 if readiness_profile else 1 <= len(records) <= args.max_dev_rounds,
            attestation["accepted_submissions_with_public_dev"],
            attestation["feedback_chain_consumed"], attestation["candidate_digests_distinct"], bool(attestation["freeze"]),
        ))
        write_json(run_dir / "builder_session_attestation.json", attestation)
        if not attestation["complete"]:
            write_json(run_dir / "summary.json", {
                "schema_version": "openwiki-agentloop-pilot-summary/v1", "status": "pilot_pipeline_incomplete",
                "pilot_not_formal": True, "formal_result_claimed": False, "code_score_claimed": False,
                "result_axis": "N/A", "code_axis": "N/A",
            })
            return 2
        hidden_broker = EvaluatorBrokerLifecycle(run_dir / "pilot_hidden_broker", credential, args.upstream,
            docker_image=args.builder_image, defer_removal=bool(readiness_profile))
        hidden_broker.__enter__()
        hidden = run_pilot_hidden(lifecycle, hidden_broker)
        if readiness_profile:
            from evaluator.readiness_smoke import run as run_readiness_judges
            aggregation = run_readiness_judges(task_root=ROOT, run_dir=run_dir, hidden=hidden,
                credential=credential, result_endpoint=result_judge_broker.endpoint)
            complete = aggregation['readiness_judges_complete']
            write_json(run_dir / 'summary.json', {
                'schema_version': 'openwiki-readiness-summary/v1', 'readiness_profile': readiness_profile,
                'status': 'readiness_evidence_complete' if complete else 'readiness_evidence_incomplete',
                'pilot_not_formal': True, 'public_cases': ['dev_001'], 'hidden_cases': ['test_001'],
                'formal_result_claimed': False, 'code_score_claimed': False, 'score_threshold': None,
                'readiness_judge_smoke': 'readiness_judge_smoke.json',
                'pipeline_ready': False, 'admission_required': True})
            return 0 if complete else 2
        finalizer = subprocess.run([sys.executable, str(ROOT / 'evaluator/formal_finalize.py'),
            '--run-dir', str(run_dir), '--credential-file', str(credential),
            '--result-judge-broker-endpoint', result_judge_broker.endpoint,
            '--acceptance-cases', 'test_001'], capture_output=True, text=True, check=False)
        (run_dir / 'formal_finalize.stdout.log').write_text(finalizer.stdout)
        (run_dir / 'formal_finalize.stderr.log').write_text(finalizer.stderr)
        aggregation_path = run_dir / 'acceptance_aggregation.json'
        aggregation = read_json(aggregation_path) if aggregation_path.is_file() else {}
        complete = finalizer.returncode == 0 and aggregation.get('acceptance_complete') is True
        write_json(run_dir / "summary.json", {
            "schema_version": "openwiki-agentloop-pilot-summary/v1",
            "status": "pilot_pipeline_complete" if complete else "pilot_pipeline_incomplete",
            "pilot_not_formal": True, "public_cases": list(public_cases), "hidden_cases": ["test_001"],
            "readiness_profile": readiness_profile, "score_threshold": None,
            "builder_session_attestation": "builder_session_attestation.json",
            "freeze_manifest": "lifecycle/freeze_manifest.json",
            "hidden_attestation": "pilot_hidden_after_freeze_attestation.json",
            "acceptance_aggregation": str(aggregation_path), 'acceptance_complete': complete,
            "formal_result_claimed": False,
        })
        return 0 if complete else 2
    except Exception as exc:
        write_json(run_dir / "summary.json", {
            "schema_version": "openwiki-agentloop-pilot-summary/v1",
            "status": "pilot_pipeline_infrastructure_failure", "pilot_not_formal": True,
            "classification": "evaluator_infrastructure_error", "error": f"{type(exc).__name__}: {exc}",
            "formal_result_claimed": False, "code_score_claimed": False,
            "result_axis": "N/A", "code_axis": "N/A",
        })
        return 2
    finally:
        cleanup_errors: list[str] = []
        for role, resource in (("builder_lifecycle", lifecycle),
                               ("pilot_hidden_lower", hidden_broker),
                               ("result_judge", result_judge_broker),
                               ("public_lower", public_broker)):
            if resource is None:
                continue
            try:
                resource.close()
            except Exception as exc:
                cleanup_errors.append(f"{role}: {type(exc).__name__}: {exc}")
        write_json(run_dir / "builder_native_stats.json", direct_builder_runtime().native_stats(run_dir))
        cleanup = write_cleanup_attestation(
            run_dir,
            builder_name=builder_name,
            builder_id=builder_id,
            lifecycle=lifecycle,
            brokers=(("public_lower", public_broker), ("pilot_hidden_lower", hidden_broker), ('result_judge', result_judge_broker)),
            pilot_not_formal=True,
        )
        if cleanup_errors:
            cleanup["cleanup_errors"] = cleanup_errors
            write_json(run_dir / "cleanup_attestation.json", cleanup)
        if readiness_profile:
            cleanup.update(removal_deferred=True, coordinator_cleanup_required=True,
                complete=False)
            try:
                from harbor.readiness_resources import retained_manifest
                cleanup['retained_resources'] = retained_manifest(run_dir,
                    (public_broker, hidden_broker, result_judge_broker))
            except Exception as exc:
                cleanup.setdefault('cleanup_errors', []).append(
                    'retained_manifest: ' + type(exc).__name__ + ': ' + str(exc))
            write_json(run_dir / "cleanup_attestation.json", cleanup)


def pilot_dry_run(run_dir: Path, readiness_profile: str | None = None) -> int:
    if run_dir.exists() and any(run_dir.iterdir()):
        raise RuntimeError(f"pilot dry-run directory is not empty: {run_dir}")
    run_dir.mkdir(parents=True, exist_ok=True)
    provider = (run_dir / "builder_direct_provider.toml").resolve()
    summary = {
        "schema_version": "openwiki-agentloop-pilot-dry-run/v1",
        "status": "pilot_dry_run_ready", "pilot_not_formal": True,
        "builder": {"model": BUILDER_MODEL, "reasoning_effort": BUILDER_EFFORT, "single_continuous_session": True},
        "public_cases": list(DEV_CASES), "hidden_cases": ["test_001"],
        "public_lower": {"model": LOWER_MODEL, "reasoning_effort": LOWER_EFFORT, "candidate_credential": PLACEHOLDER_KEY},
        "hidden_lower": {"model": LOWER_MODEL, "reasoning_effort": LOWER_EFFORT, "candidate_credential": PLACEHOLDER_KEY, "fresh_after_freeze": True, "initial_calls_required": 0},
        "provider_config_host_path": str(provider),
        "provider_config_container_path": str(provider),
        "host_path_equals_container_path": True,
        "network_calls": 0, "docker_started": False, "harbor_started": False,
        "provider_calls": 0, "pilot_execution_started": False,
        "formal_result_claimed": False, "code_score_claimed": False,
        "result_axis": "N/A", "code_axis": "N/A",
    }
    write_json(run_dir / "pilot_protocol_lock.json", summary)
    if readiness_profile:
        summary.update(readiness_profile=readiness_profile, public_cases=['dev_001'],
            hidden_cases=['test_001'], required_valid_rounds=2, max_dev_rounds=2,
            require_exact_feedback_digest=True, score_threshold=None)
        write_json(run_dir / 'pilot_protocol_lock.json', summary)
    write_json(run_dir / "summary.json", summary)
    print(json.dumps(summary, indent=2, ensure_ascii=False))
    return 0


def run_formal(args: argparse.Namespace) -> int:
    run_dir = args.run_dir.resolve()
    if run_dir.exists():
        raise RuntimeError(f"formal run directory must not exist: {run_dir}")
    run_dir.mkdir(parents=True)
    credential = args.credential_file.resolve()
    builder_port = free_port()
    builder_name = "openwiki-builder-" + hashlib.sha256(str(run_dir).encode()).hexdigest()[:12]
    lifecycle: BuilderLifecycle | None = None
    public_broker: EvaluatorBrokerLifecycle | None = None
    result_judge_broker: EvaluatorBrokerLifecycle | None = None
    builder_id: str | None = None
    try:
        for path in (args.harbor.resolve(), credential, DIRECT_BUILDER_SCRIPT):
            if not path.exists():
                raise FileNotFoundError(path)
        public_broker = EvaluatorBrokerLifecycle(
            run_dir / "public_broker", credential, args.upstream,
            docker_image=args.builder_image,
        )
        public_broker.__enter__()
        if not public_broker.endpoint:
            raise RuntimeError("public lower broker has no endpoint")
        result_judge_broker = start_result_judge(run_dir, credential, args.upstream, args.builder_image)
        public = run_dir / "builder_public_package"
        public_manifest = stage_public(ROOT, public)
        workspace = run_dir / "builder_workspace/submission"
        workspace.mkdir(parents=True)
        lifecycle = BuilderLifecycle(
            run_dir, workspace, public_broker.endpoint, credential, args.upstream,
            max_dev_rounds=args.max_dev_rounds,
            result_judge_endpoint=result_judge_broker.endpoint,
        )
        lifecycle.start()
        provider = run_dir / "builder_direct_provider.toml"
        direct_builder_runtime().write_provider(provider)
        config = stage_builder_task(
            run_dir, public, workspace, lifecycle, provider, args.builder_image,
            args.builder_node_modules,
        )
        write_json(run_dir / "protocol_lock.json", {
            **static_summary(), "formal_execution_started": True,
            "timeout_contract": builder_timeout_contract(args.builder_timeout),
            "builder_public_package": public_manifest,
            "brokers": {"builder": "native-direct-xhigh", "public": "independent-medium", "hidden": "fresh-after-freeze-medium"},
        })
        started = now()
        builder_code = run_native_builder(lifecycle=lifecycle, config=config, credential=credential,
            harbor=args.harbor.resolve(), timeout=args.builder_timeout).returncode
        ended = now()
        interrupted_at_max_rounds = formal_max_rounds_interrupt(lifecycle, builder_code)
        native = verify_native(run_dir, lifecycle.controller.records,
            [e for e in lifecycle.events if e["event"] == "feedback_delivered"], lifecycle.native_observations,
            allow_interrupted=interrupted_at_max_rounds)
        native["max_dev_rounds_interrupt_accepted"] = interrupted_at_max_rounds
        write_json(run_dir / "native_builder_evidence.json", native)
        if (builder_code == 0 or interrupted_at_max_rounds) and native["valid"] and lifecycle.controller.records and lifecycle.controller.frozen is None:
            lifecycle.controller.freeze()
        records = lifecycle.controller.records
        attestation = {
            "schema_version": "openwiki-builder-session-attestation/v1",
            "builder_session_id": lifecycle.session_id,
            "builder_connection_id": lifecycle.connection_id,
            "builder_model": BUILDER_MODEL, "builder_reasoning_effort": BUILDER_EFFORT,
            "single_continuous_session": native["valid"], "single_harbor_invocation": native["valid"],
            "native_evidence": native,
            "builder_exit_code": builder_code, "started_at": started, "finished_at": ended,
            "events": lifecycle.events, "candidate_records": records,
            "accepted_submission_count": len(records),
            "accepted_submissions_with_public_dev": bool(records) and all(list(record.get("dev_cases", {})) == list(DEV_CASES) for record in records),
            "candidate_digests_distinct": len({record.get("candidate_digest") for record in records}) == len(records) if records else False,
            "feedback_chain_consumed": bool(records) and all(index == 0 or bool(record.get("revision", {}).get("feedback_bound_submission")) for index, record in enumerate(records)),
            "freeze": lifecycle.controller.frozen,
        }
        attestation["builder_interrupted_at_max_dev_rounds"] = interrupted_at_max_rounds
        attestation["complete"] = all((
            builder_code == 0 or interrupted_at_max_rounds,
            native["valid"], 1 <= len(records) <= args.max_dev_rounds,
            attestation["candidate_digests_distinct"],
            attestation["accepted_submissions_with_public_dev"],
            attestation["feedback_chain_consumed"], bool(attestation["freeze"]),
        ))
        write_json(run_dir / "builder_session_attestation.json", attestation)
        if not attestation["complete"]:
            write_json(run_dir / "summary.json", {
                "status": "builder_lifecycle_incomplete", "formal_result_claimed": False,
                "code_score_claimed": False, "builder_session_attestation": attestation,
            })
            return 2
        hidden = lifecycle.controller.hidden()
        finalizer_output = run_dir / "formal_aggregation.json"
        finalizer = subprocess.run([
            sys.executable, str(ROOT / "evaluator/formal_finalize.py"),
            "--run-dir", str(run_dir), "--output", str(finalizer_output),
            "--credential-file", str(args.credential_file.resolve()),
            "--result-judge-broker-endpoint", result_judge_broker.endpoint,
        ], text=True, capture_output=True, check=False)
        aggregation = read_json(finalizer_output) if finalizer_output.is_file() else {}
        write_json(run_dir / "summary.json", {
            "status": "completed" if finalizer.returncode == 0 else "formal_finalization_refused",
            "formal_result_claimed": aggregation.get("formal_result_publishable") is True,
            "code_score_claimed": aggregation.get("code_score_publishable") is True,
            "builder_session_attestation": attestation,
            "freeze_manifest": str(run_dir / "lifecycle/freeze_manifest.json"),
            "hidden": hidden, "formal_aggregation": aggregation,
            "finalizer_exit_code": finalizer.returncode,
            "finalizer_stderr_tail": finalizer.stderr[-1500:],
            "result_judge_broker": {
                "model": RESULT_JUDGE_MODEL,
                "reasoning_effort": RESULT_JUDGE_EFFORT,
                "endpoint": result_judge_broker.endpoint,
                "stats": str(result_judge_broker.stats_path),
                "lifecycle": str(result_judge_broker.lifecycle_path),
            },
            "combined_score": None,
            "max_dev_rounds": args.max_dev_rounds,
            "n_concurrent": args.n_concurrent,
        })
        write_json(run_dir / "one_stop_summary.json", read_json(run_dir / "summary.json"))
        return 0 if finalizer.returncode == 0 else 2
    except Exception as exc:
        write_json(run_dir / "summary.json", {
            "schema_version": "openwiki-formal-summary-v1",
            "status": "orchestration_failed",
            "classification": "evaluator_infrastructure_error",
            "error_type": type(exc).__name__,
            "error": str(exc)[-1500:],
            "formal_result_claimed": False,
            "code_score_claimed": False,
            "result_axis": "N/A",
            "code_axis": "N/A",
        })
        return 2
    finally:
        cleanup_errors: list[str] = []
        for role, resource in (("builder_lifecycle", lifecycle),
                               ("public_lower", public_broker),
                               ("result_judge", result_judge_broker)):
            if resource is None:
                continue
            try:
                resource.close()
            except Exception as exc:
                cleanup_errors.append(f"{role}: {type(exc).__name__}: {exc}")
        write_json(run_dir / "builder_native_stats.json", direct_builder_runtime().native_stats(run_dir))
        cleanup = write_cleanup_attestation(
            run_dir,
            builder_name=builder_name,
            builder_id=builder_id,
            lifecycle=lifecycle,
            brokers=(("public_lower", public_broker), ("result_judge", result_judge_broker)),
            pilot_not_formal=False,
        )
        if cleanup_errors:
            cleanup["cleanup_errors"] = cleanup_errors
            write_json(run_dir / "cleanup_attestation.json", cleanup)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run-dir", type=Path)
    parser.add_argument("--max-dev-rounds", type=int, default=10)
    parser.add_argument("--n-concurrent", type=int, default=1)
    parser.add_argument("--run-formal", action="store_true")
    parser.add_argument("--pilot", action="store_true", help="both public dev cases, feedback and scored test_001 smoke; never formal scoring")
    parser.add_argument("--readiness-profile", choices=[READINESS_PROFILE])
    parser.add_argument('--readiness-binding-file', type=Path)
    parser.add_argument('--readiness-binding-sha256')
    parser.add_argument("--dry-run", action="store_true", help="with --pilot, write a provider-free wiring receipt and exit")
    parser.add_argument("--self-test", action="store_true")
    parser.add_argument("--credential-file", type=Path, default=Path("@@AGENTSWE_CREDENTIAL_FILE@@"))
    parser.add_argument("--harbor", type=Path, default=Path("@@AGENTSWE_HARBOR_BIN@@"))
    parser.add_argument("--builder-image", default=BUILDER_IMAGE)
    parser.add_argument(
        "--builder-node-modules",
        type=Path,
        help="optional evaluator-owned read-only dependency tree exposed only to the Builder",
    )
    # 1800 s (30 min) was below both the pilot's 28800 generated cap and the
    # formal task cap, so a launch relying on the default could never cover the
    # generated task. No launcher uses the default; this matches the other trees.
    parser.add_argument("--builder-timeout", type=int, default=28920)
    parser.add_argument("--upstream", default=os.environ.get("AGENTSWE_UPSTREAM_BASE_URL", "https://api.deepseek.com"))
    args = parser.parse_args(argv)
    if args.readiness_profile:
        if not args.pilot or args.run_formal:
            parser.error('--readiness-profile requires --pilot and is forbidden for formal scoring')
        if not args.dry_run and (not args.readiness_binding_file or not args.readiness_binding_sha256):
            parser.error('readiness execution requires coordinator binding file and SHA')
        args.max_dev_rounds = 2
    if not 1 <= args.max_dev_rounds <= 10:
        parser.error("--max-dev-rounds must be in 1..10")
    if args.n_concurrent != 1:
        parser.error("--n-concurrent is fixed at 1")
    if args.run_formal:
        try:
            import importlib.util
            admission_path = Path("@@AGENTSWE_EDITING_CONTROL@@/readiness_admission.py")
            if str(admission_path.parent) not in sys.path:
                sys.path.insert(0, str(admission_path.parent))
            admission_spec = importlib.util.spec_from_file_location("agentswe_formal_readiness", admission_path)
            if admission_spec is None or admission_spec.loader is None:
                raise RuntimeError("evaluator readiness admission module is unavailable")
            admission_module = importlib.util.module_from_spec(admission_spec)
            admission_spec.loader.exec_module(admission_module)
            admission_module.require_formal_readiness(ROOT)
        except Exception as exc:
            parser.error(f"formal readiness refused: {exc}")
    install_summary_writer(
        args.run_dir,
        max_dev_rounds=args.max_dev_rounds,
        n_concurrent=args.n_concurrent,
        mode="formal" if args.run_formal else "pilot" if args.pilot else "static",
    )
    if args.dry_run and not args.pilot:
        parser.error("--dry-run requires --pilot")
    if sum(bool(value) for value in (args.self_test, args.run_formal, args.pilot)) > 1:
        parser.error("--self-test, --run-formal, and --pilot are mutually exclusive")
    summary = static_summary()
    if args.self_test:
        print(json.dumps({**summary, "self_test": "PASS" if summary["status"] == "static_config_ready" else "FAIL"}, indent=2, ensure_ascii=False))
        return 0 if summary["status"] == "static_config_ready" else 1
    if args.run_dir is None:
        parser.error("--run-dir is required")
    if args.pilot:
        if args.dry_run:
            return pilot_dry_run(args.run_dir.resolve(), args.readiness_profile)
        # Enforced only where a Builder is actually started: --self-test, the
        # static audit and --pilot --dry-run must stay launchable whatever the
        # outer budget says.
        enforce_builder_timeout_contract(parser, args.builder_timeout)
        return run_pilot(args)
    if args.run_formal:
        enforce_builder_timeout_contract(parser, args.builder_timeout)
        return run_formal(args)
    run_dir = args.run_dir.resolve()
    if run_dir.exists() and any(run_dir.iterdir()):
        raise RuntimeError(f"static run directory is not empty: {run_dir}")
    run_dir.mkdir(parents=True, exist_ok=True)
    write_json(run_dir / "protocol_lock.json", summary)
    write_json(run_dir / "summary.json", summary)
    print(json.dumps(summary, indent=2, ensure_ascii=False))
    return 0 if summary["status"] == "static_config_ready" else 1


if __name__ == "__main__":
    raise SystemExit(main())
