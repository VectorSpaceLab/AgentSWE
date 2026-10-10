#!/usr/bin/env python3
"""Real same-session Builder lifecycle for the AI Scientist Edit sibling.

No provider, Docker, Harbor or hidden case is started by the default command.
The explicit --run-formal flag is required for the expensive path.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import signal
import shutil
import socket
import socketserver
import stat
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

ROOT = Path(__file__).resolve().parents[1]
SHARED_RESULT_JUDGE = Path("@@AGENTSWE_EDITING_CONTROL@@/result_judge.py")
CREATE_CODE_JUDGE = Path("@@AGENTSWE_EDITING_CONTROL@@/code_judge_runner.py")
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from agentloop.protocol import write_json
from agentloop.lower_transport import snapshot_valid
from agentloop.two_round_controller import Controller
from agentloop.public_feedback import submission_payload
from harbor.native_builder_evidence import NativeEvidenceError, observe_thread, verify_native
from harbor.native_builder_runner import run_native_builder
from harbor import direct_harbor_builder as direct_builder

MODEL = "deepseek-flash"
BUILDER_MODEL = "deepseek-flash"  # upper Builder (Codex harness) only
BUILDER_EFFORT = "max"
LOWER_EFFORT = "high"
BUILDER_IMAGE = "agentswe/create-agent-builder-codex:0.144.1-node24.6.0-0812"
LOWER_IMAGE = "agentswe/edit-candidate-python311:0826"
BUILDER_BROKER = Path("@@AGENTSWE_EDITING_CONTROL@@/builder_broker_xhigh.py")
LOWER_BROKER = ROOT / "evaluator/lower_responses_broker.py"
GENERATED_BUILDER_TASK_TIMEOUT_SECONDS = 17_880  # 5 h Builder cap (D2, 2026-09-19): outer 18000 = task + cleanup margin
BUILDER_CLEANUP_MARGIN_SECONDS = 120
DEFAULT_OUTER_BUILDER_TIMEOUT_SECONDS = GENERATED_BUILDER_TASK_TIMEOUT_SECONDS + BUILDER_CLEANUP_MARGIN_SECONDS


def now() -> str:
    return datetime.now(timezone.utc).isoformat()


def read_json(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return value if isinstance(value, dict) else {}


def free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def broker_stats(endpoint: str) -> dict[str, Any]:
    url = endpoint.split("/v1/", 1)[0].rstrip("/") + "/stats"
    request = urllib.request.Request(url, headers={"Authorization": "Bearer stats-only-placeholder"})
    try:
        with urllib.request.urlopen(request, timeout=10) as response:
            value = json.loads(response.read())
    except Exception as exc:
        return {"error": f"{type(exc).__name__}: {exc}"}
    return value if isinstance(value, dict) else {"error": "stats is not an object"}


def broker_call_count(value: dict[str, Any]) -> int:
    runtime = value.get("runtime") if isinstance(value.get("runtime"), dict) else {}
    raw = runtime.get("calls", value.get("calls", 0))
    try:
        return int(raw or 0)
    except (TypeError, ValueError):
        return -1


def broker_failure_count(value: dict[str, Any]) -> int:
    runtime = value.get("runtime") if isinstance(value.get("runtime"), dict) else {}
    raw = runtime.get("failures", value.get("failures", 0))
    try:
        return int(raw or 0)
    except (TypeError, ValueError):
        return -1


def hidden_evidence_ready_for_finalizer(attestation: dict[str, Any]) -> bool:
    """Allow independent judges once the complete hidden execution exists.

    ``formal_complete`` intentionally remains stricter: it is false when a
    case is infrastructure-invalid or Candidate-invalid.  That must prevent a
    publishable aggregate, but it must not suppress Result judging for other
    scoreable cases or the independent Code axis.
    """
    expected = [f"test_{i:03d}" for i in range(1, 7)]
    return all((
        attestation.get("evidence_kind") == "formal",
        attestation.get("expected_cases") == expected,
        attestation.get("executed_cases") == expected,
        attestation.get("complete_inventory") is True,
        attestation.get("all_cases_materialized") is True,
        (attestation.get("all_cases_real") is True or
         (attestation.get('all_cases_measured_or_causal_zero') is True
          and all(entry.get('real_execution') or entry.get('causal_candidate_zero')
                  for entry in attestation.get('cases', []))
          and len(attestation.get('cases', [])) == len(expected))),
        attestation.get("all_cases_started_after_freeze") is True,
        attestation.get("frozen_digest_stable") is True,
    ))


def builder_timeout_contract(effective_outer_timeout: int) -> dict[str, int | bool]:
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


def read_container_id(cidfile: Path) -> str | None:
    try:
        value = cidfile.read_text(encoding="ascii").strip()
    except OSError:
        return None
    return value or None


def start_broker(name: str, script: Path, credential: Path, port: int, effort: str, image: str, cidfile: Path,
                 *, history_seed: Path | None = None, history_seed_sha256: str | None = None,
                 defer_removal: bool = False) -> None:
    if script.resolve() == JUDGE_BROKER_SCRIPT:
        if effort != BUILDER_EFFORT:
            raise ValueError("Result judge requires xhigh")
        return _judge_runtime().start_judge_broker(name=name, credential=credential, image=image,
            port=port, cidfile=cidfile, defer_removal=defer_removal)
    if script.resolve() == BUILDER_BROKER.resolve():
        if effort != BUILDER_EFFORT:
            raise ValueError("Builder broker requires xhigh")
        if str(BUILDER_BROKER.parent) not in sys.path:
            sys.path.insert(0,str(BUILDER_BROKER.parent))
        from builder_broker_runtime import start_builder_broker
        return start_builder_broker(name=name,credential=credential,image=image,port=port,cidfile=cidfile)
    if not script.is_file():
        raise FileNotFoundError(f"broker script missing: {script}")
    is_lower = script.resolve() == LOWER_BROKER.resolve()
    if is_lower and effort != LOWER_EFFORT:
        raise ValueError("lower broker requires medium")
    cidfile.parent.mkdir(parents=True, exist_ok=True)
    if is_lower and (cidfile.exists() or cidfile.is_symlink()):
        raise FileExistsError("existing lower CID evidence must not be replaced")
    cidfile.unlink(missing_ok=True)
    if is_lower:
        from evaluator.lower_request_reserve import load_history
        seeded_history = load_history(history_seed, history_seed_sha256, verify_sources=True)
    lower_evidence = cidfile.parent / (cidfile.stem + "-lower-transport")
    if is_lower:
        lower_evidence.mkdir(exist_ok=False)
        from agentloop.lower_request_identity import create_binding
        create_binding(cidfile.parent, lower_evidence, f'http://127.0.0.1:{port}/v1/responses')
        if history_seed is not None:
            shutil.copyfile(history_seed, lower_evidence / 'history_seed.json')
    command = [
        "docker", "run", "-d", "--rm", "--name", name, "--network", "host",
        "-v", f"{script.resolve()}:/broker.py:ro",
        "-v", f"{credential.resolve()}:/run/secrets/agentswe.env:ro",
        "--cidfile", str(cidfile.resolve()),
        *(["--label", "agentswe.owner=ai-lower-broker", "-v", f"{lower_evidence.resolve()}:/evidence",
           "-v", f"{script.resolve().parent / 'responses_stream.py'}:/responses_stream.py:ro",
           "-v", f"{ROOT / 'agentloop/lower_request_identity.py'}:/lower_request_identity.py:ro",
           "-v", f"{ROOT / 'evaluator/lower_request_reserve.py'}:/lower_request_reserve.py:ro",
           "-e", "AGENTSWE_EVALUATOR_PROXY_URL=",
           "-v", "/etc/ssl/certs:/etc/ssl/certs:ro", "-e", "SSL_CERT_FILE=/etc/ssl/certs/ca-certificates.crt"] if is_lower else []),
        image, "python3", "/broker.py",
        "--credential-file", "/run/secrets/agentswe.env",
        "--bind", "127.0.0.1", "--port", str(port),
    ]
    if is_lower:
        command.extend(["--stats-output", "/evidence/broker_stats.json", "--context-key-file", "/evidence/logical-context.key"])
        if history_seed is not None:
            command.extend(["--history-seed", "/evidence/history_seed.json", "--history-seed-sha256", history_seed_sha256])
        write_json(lower_evidence / "source_bindings.json", {
            str(path): hashlib.sha256(path.read_bytes()).hexdigest()
            for path in (script.resolve(), script.resolve().parent / 'responses_stream.py',
                         Path(__file__).resolve(), ROOT / "agentloop/lower_transport.py",
                         ROOT / 'agentloop/lower_request_identity.py', ROOT / 'evaluator/lower_request_reserve.py')})
    completed = subprocess.run(command, check=True, text=True, capture_output=True)
    # ``docker run`` stdout is the ID returned by this exact invocation.
    # Never recover ownership by querying the deterministic name: a stale
    # same-name container could otherwise be mistaken for this run.
    if not cidfile.is_file():
        value = (completed.stdout or "").strip().splitlines()
        if value:
            cidfile.write_text(value[-1].strip() + "\n", encoding="ascii")
        else:
            raise RuntimeError("docker run succeeded without returning a current-run container ID")
    for _ in range(60):
        try:
            with urllib.request.urlopen(f"http://127.0.0.1:{port}/healthz", timeout=2) as response:
                if response.status == 200:
                    if is_lower:
                        snapshot = broker_stats(f"http://127.0.0.1:{port}/v1/responses")
                        if not snapshot_valid(snapshot, fresh=history_seed is None):
                            raise RuntimeError("lower broker does not match the bound single-upstream protocol")
                        if snapshot.get('attempts') != seeded_history['attempts'] or snapshot.get('current_invocation_calls') != 0:
                            raise RuntimeError("lower broker startup history differs from trusted seed")
                    return
        except Exception:
            time.sleep(0.5)
    raise RuntimeError(f"broker {name} did not become healthy")


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


def remove_and_verify_container(name: str, cidfile: Path | None, *, attempted: bool) -> dict[str, Any]:
    """Remove only a current-run container ID and prove absence fail-closed."""
    container_id = read_container_id(cidfile) if cidfile is not None else None
    receipt: dict[str, Any] = {
        "role": name,
        "cidfile": str(cidfile) if cidfile is not None else None,
        "container_id": container_id,
        "startup_attempted": attempted,
        "ownership_proven": container_id is not None,
        "cleanup_attempted": container_id is not None,
        "absent_after_cleanup": False,
    }
    if not attempted:
        receipt.update({"status": "not_started", "absent_after_cleanup": True})
        return receipt
    if container_id is None:
        receipt.update({
            "status": "ownership_unproven",
            "cleanup_error": "Docker startup was attempted but no current-run container ID was captured",
        })
        return receipt
    try:
        removed = subprocess.run(
            ["docker", "rm", "-f", container_id],
            text=True, capture_output=True,
            check=False,
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
            text=True, capture_output=True,
            check=False,
        )
    except OSError as exc:
        receipt.update({
            "remove_exit_code": removed.returncode,
            "status": "cleanup_error",
            "cleanup_error": f"docker inspect failed: {type(exc).__name__}: {exc}",
        })
        return receipt
    inspect_text = ((inspected.stdout or "") + (inspected.stderr or "")).strip()
    inspect_lower = inspect_text.lower()
    absent = inspected.returncode != 0 and (
        "no such object" in inspect_lower or "no such container" in inspect_lower
    )
    receipt.update({
        "remove_exit_code": removed.returncode,
        "inspect_exit_code": inspected.returncode,
        "absent_after_cleanup": absent,
        "status": "absent" if absent else "cleanup_unverified",
    })
    if not absent:
        receipt["cleanup_error"] = inspect_text[-1000:] or "docker inspect did not prove absence"
    return receipt


class BuilderLifecycle:
    def __init__(
        self,
        run_dir: Path,
        workspace: Path,
        lower_endpoint: str,
        image: str,
        *,
        public_case_ids: tuple[str, ...] = ("dev_001", "dev_002"),
        hidden_case_ids: tuple[str, ...] = tuple(f"test_{i:03d}" for i in range(1, 7)),
        run_kind: str = "formal",
        max_dev_rounds: int = 10,
        n_concurrent: int = 1,
        result_judge_endpoint: str | None = None,
        readiness_profile: str | None = None,
        current_binding: dict[str, Any] | None = None,
    ) -> None:
        token = hashlib.sha256(f"{run_dir}:{time.time_ns()}".encode()).hexdigest()
        self.run_dir = run_dir
        self.workspace = workspace
        self.token = token
        self.session_id: str | None = None
        self.invocation_id = f"harbor-invocation-{hashlib.sha256((token + ':invocation').encode()).hexdigest()[:16]}"
        self.socket_path = Path(tempfile.gettempdir()) / f"ai-scientist-builder-{token[:12]}.sock"
        self.server: socketserver.ThreadingUnixStreamServer | None = None
        self.events: list[dict[str, Any]] = []
        self.native_observations: list[dict[str, Any]] = []
        # A feedback is "received" only when a write to the Builder's socket
        # returned. The Builder's shell-tool timeout can kill the helper while
        # the evaluator is still answering, and the response write then raises
        # BrokenPipeError: the round is accepted and its feedback bytes are on
        # disk, but no feedback_delivered event exists and verify_native
        # rejects the whole session. Keep the ready response so a later
        # connection can deliver it for real, and receipt it at that later
        # moment -- never before, and never for a write that did not happen.
        self.feedback_write_failures: list[dict[str, Any]] = []
        self.undelivered_feedback: dict[int, dict[str, Any]] = {}
        # A non-200 answer is not a receipt and consumes no round, but it is the
        # evaluator's only account of why a delivery was refused. A preflight
        # whose caller was killed must stay diagnosable, so keep it and let
        # --status reprint it rather than dropping it on the floor.
        self.undelivered_responses: list[dict[str, Any]] = []
        self.event_lock = threading.Lock()
        self.submission_lock = threading.Lock()
        self.controller = Controller(
            ROOT,
            run_dir / "lifecycle",
            lower_endpoint,
            False,
            image=image,
            public_case_ids=public_case_ids,
            hidden_case_ids=hidden_case_ids,
            run_kind=run_kind,
            max_dev_rounds=max_dev_rounds,
            n_concurrent=n_concurrent,
            result_judge_endpoint=result_judge_endpoint,
            readiness_profile=readiness_profile,current_binding=current_binding,
        )

    def event(self, name: str, **fields: Any) -> None:
        value = {"event": name, "at": now(), "epoch_ns": time.time_ns(), "builder_session_id": self.session_id, "builder_invocation_id": self.invocation_id, **fields}
        with self.event_lock:
            self.run_dir.mkdir(parents=True, exist_ok=True)
            with (self.run_dir / "builder_observer_events.jsonl").open("a") as handle:
                handle.write(json.dumps(value, ensure_ascii=False) + "\n")
                handle.flush()
                os.fsync(handle.fileno())
            self.events.append(value)

    def bind_native_thread(self) -> None:
        observed = observe_thread(self.run_dir, self.native_observations)
        if self.session_id is not None and self.session_id != observed["thread_id"]:
            raise NativeEvidenceError("builder_session_changed_between_submissions")
        self.session_id = observed["thread_id"]
        self.native_observations.append(observed)
        self.event("native_thread_observed", source=observed)

    @staticmethod
    def _feedback_file_bytes(feedback: dict[str, Any]) -> bytes:
        """Re-encode a visible feedback object exactly as its file was written.

        Both writers of the authoritative feedback file use this one compact
        canonical encoding: agentloop/protocol.py:canonical_json on the formal
        path, and the literal json.dumps(sort_keys=True, separators=(',', ':'),
        ensure_ascii=False) + '\\n' that two_round_controller._write_feedback
        rewrites the file with on the readiness path. `_write_feedback` then
        sets feedback_digest to sha256_file() of exactly that file, so this
        encoding is the one whose sha256 the digest is.  submission_payload
        passes `feedback` through untouched ("Feedback is already the exact
        public projection whose file is hashed"), so the visible copy
        re-encodes to the same bytes.
        """
        return (json.dumps(feedback, sort_keys=True, separators=(",", ":"),
                           ensure_ascii=False) + "\n").encode("utf-8")

    @staticmethod
    def _delivered_records(payload: dict[str, Any]):
        """Per-record views of what one response actually carried.

        A submit response is one record at top level; a status or feedback
        response carries the same submission_payload() dicts under "records".
        Both come from submission_payload(), so the same accepted record has
        byte-identical content either way.
        """
        records = (payload or {}).get("records")
        if isinstance(records, list) and records:
            # A status or feedback response may also repeat its latest record
            # at the top level for compatibility. The records list is
            # authoritative and already contains it, so the top-level copy must
            # not mint a second receipt for the same round.
            for record in records:
                if isinstance(record, dict) and record.get("submission_number"):
                    yield record
            return
        if isinstance(payload, dict) and payload.get("submission_number"):
            yield payload

    @classmethod
    def _carries_feedback(cls, record: dict[str, Any]) -> bool:
        """True only when the authoritative feedback bytes themselves went out.

        The test is cryptographic: the record counts only when its visible
        feedback re-encodes to bytes whose sha256 is the record's own
        feedback_digest, i.e. only when the complete authoritative file
        content was delivered. Naming a digest without carrying its body
        fails this and must never count as a delivery.
        """
        digest = record.get("feedback_digest")
        feedback = record.get("feedback")
        if not digest or not isinstance(feedback, dict) or not feedback:
            return False
        try:
            return hashlib.sha256(cls._feedback_file_bytes(feedback)).hexdigest() == digest
        except (TypeError, ValueError):
            return False

    def feedback_written(self, payload: dict[str, Any]) -> None:
        """Receipt every feedback the socket write that just returned carried.

        Socket transmission is evidence of writing, not model consumption.
        """
        delivered = [record for record in self._delivered_records(payload)
                     if self._carries_feedback(record)]
        for record in delivered:
            number = record["submission_number"]
            digest = record["feedback_digest"]
            payload_sha256 = hashlib.sha256(
                json.dumps(record, sort_keys=True, ensure_ascii=False).encode()).hexdigest()
            with self.event_lock:
                self.undelivered_feedback.pop(number, None)
                seen = any(row.get("event") == "feedback_delivered"
                           and row.get("candidate_number") == number
                           and row.get("feedback_digest") == digest
                           and row.get("payload_sha256") == payload_sha256 for row in self.events)
            if seen:
                continue
            self.event("feedback_delivered", candidate_number=number,
                       feedback_digest=digest,
                       duplicate_digest=record.get("duplicate_digest") is True,
                       evidence="socket_write_and_flush_completed",
                       payload_sha256=payload_sha256)

    def feedback_write_failed(self, payload: dict[str, Any], exc: BaseException) -> None:
        """A ready response the Builder's socket never took.

        This is the opposite of a receipt: it records that delivery did not
        happen, and keeps the response deliverable by a later connection.
        """
        numbers = []
        with self.event_lock:
            receipted = {row.get("candidate_number") for row in self.events
                         if row.get("event") == "feedback_delivered"}
            for record in self._delivered_records(payload):
                if not self._carries_feedback(record):
                    continue
                number = record["submission_number"]
                numbers.append(number)
                if number not in receipted:
                    self.undelivered_feedback[number] = record
            self.feedback_write_failures.append({"at": now(), "error": f"{type(exc).__name__}: {exc}",
                "candidate_numbers": numbers, "builder_session_id": self.session_id})
            write_json(self.run_dir / "native_feedback_write_failures.json", {
                "schema_version": "ai-scientist-feedback-delivery-ledger/v1",
                "write_failures": self.feedback_write_failures,
                "undelivered_candidate_numbers": sorted(self.undelivered_feedback)})
        self.event("feedback_write_failed", error_type=type(exc).__name__,
                   candidate_numbers=numbers)

    def response_write_failed(self, action: str | None, code: int,
                              payload: dict[str, Any], exc: BaseException) -> None:
        """A non-200 answer the Builder's socket never took.

        Such an answer accepted no candidate and consumed no round, so nothing
        is receipted here. The response is kept verbatim, and named by the
        action that produced it, so a later --status connection still delivers
        it and the run records which call broke the pipe.
        """
        with self.event_lock:
            self.undelivered_responses.append({"at": now(), "action": action, "status": code,
                                               "payload": payload,
                                               "error": f"{type(exc).__name__}: {exc}"})
            write_json(self.run_dir / "native_undelivered_responses.json", {
                "schema_version": "ai-scientist-undelivered-response-ledger/v1",
                "undelivered_responses": self.undelivered_responses})
        self.event("feedback_write_failed", error_type=type(exc).__name__,
                   candidate_number=payload.get("submission_number"),
                   action=action, response_status=code)

    def submit(self, feedback_digest: str | None) -> tuple[int, dict[str, Any]]:
        # The socket server is threaded; only one evaluation may be active.
        if not self.submission_lock.acquire(blocking=False):
            return 409, {"accepted": False, "error": "dev_evaluation_already_active", "round_consumed": False}
        try:
            return self._submit(feedback_digest)
        finally:
            self.submission_lock.release()

    def _submit(self, feedback_digest: str | None) -> tuple[int, dict[str, Any]]:
        # BuilderLifecycle may have been initialized before another process
        # recorded the terminal latch.  Refresh the atomic lifecycle file
        # before native observation so a stale in-memory Controller cannot
        # bind a thread for a submission that is already hard-stopped.
        try:
            self.controller._load_state()
        except (OSError, ValueError, RuntimeError) as exc:
            self.event("lifecycle_state_unavailable", reason=str(exc), build_started=False, provider_dispatch=False)
            return 503, {"accepted": False, "round_consumed": False,
                         "classification_axis": "infrastructure", "error": "lifecycle_state_unavailable",
                         "details": str(exc), "build_started": False, "provider_dispatch": False}
        terminal_rejection = self.controller.distinct_terminal_rejection(self.workspace)
        if terminal_rejection is not None:
            self.event(
                "submission_rejected_after_infrastructure_terminal",
                candidate_number=terminal_rejection.get("submission_number"),
                candidate_digest=terminal_rejection.get("candidate_digest"),
                terminal_candidate_digest=(terminal_rejection.get("terminal_failure") or {}).get("candidate_digest"),
                state=terminal_rejection.get("state"),
                round_consumed=False,
                build_started=False,
                provider_dispatch=False,
            )
            return 503, submission_payload(terminal_rejection, self.session_id)
        try:
            self.bind_native_thread()
        except (NativeEvidenceError, OSError, ValueError) as exc:
            self.event("native_evidence_unavailable", reason=str(exc))
            return 503, {"accepted": False, "round_consumed": False,
                         "classification_axis": "infrastructure", "error": str(exc)}
        number = len(self.controller.records) + 1
        self.event("submission_started", candidate_number=number, feedback_digest_ack=feedback_digest)
        result = self.controller.submit(self.workspace, builder_session_id=self.session_id, feedback_digest_ack=feedback_digest)
        number = result.get("submission_number", number)
        duplicate = result.get("duplicate_digest") is True
        accepted = bool(result.get("accepted"))
        self.event("submission_finished", candidate_number=number, accepted=accepted, candidate_digest=result.get("candidate_digest"), state=result.get("state"), duplicate_digest=duplicate, round_consumed=result.get("round_consumed", False))
        if result.get("terminal_latch_created") is True:
            self.event(
                "infrastructure_terminal_latched",
                candidate_number=number,
                candidate_digest=result.get("candidate_digest"),
                state=result.get("state"),
                classification_axis=result.get("classification_axis", "infrastructure"),
                round_consumed=False,
                retry_allowed=False,
            )
        if not accepted:
            status = 503 if result.get("classification_axis") == "infrastructure" or "infrastructure" in str(result.get("state", "")) else 409
            return status, submission_payload({"accepted": False, **result}, self.session_id)
        payload = submission_payload({"accepted": True, **result}, self.session_id)
        payload["feedback_digest"] = result.get("feedback_digest")
        payload["feedback"] = result.get("feedback")
        return 200, payload

    def _record_feedback_path(self, record: dict[str, Any]) -> Path:
        path = Path(str(record.get("feedback_path") or ""))
        if path.is_file():
            return path
        number = int(record.get("submission_number") or 0)
        return self.controller.run_dir / "feedback" / f"candidate_{number:03d}.json"

    def _with_feedback(self, record: dict[str, Any]) -> dict[str, Any]:
        """One accepted record's public projection, carrying its own feedback.

        submission_payload already publishes each record's feedback_digest, but
        the body only ever reached the Builder for the latest record at top
        level, so a response that names an earlier candidate could not carry
        the bytes whose digest it names. Reading each record's own file makes
        every accepted round recoverable and makes a receipt minted from this
        response provable. The digest is unchanged: it is the file's sha256,
        the value the record and the next submission's feedback_digest_ack are
        bound to.
        """
        public = submission_payload(record, self.session_id)
        path = self._record_feedback_path(record)
        if "feedback" not in public and path.is_file():
            public["feedback"] = json.loads(path.read_text(encoding="utf-8"))
        return public

    def feedback(self) -> tuple[int, dict[str, Any]]:
        """Re-deliver one accepted record whose earlier response was lost.

        This re-reads the same authoritative feedback the accepted submission
        already carries; it issues nothing new and consumes no round. The
        oldest response a failed write left undelivered is preferred.
        """
        records = list(self.controller.records)
        if not records:
            return 404, {"error": "no accepted submission has feedback yet"}
        pending = [n for n in sorted(self.undelivered_feedback) if 1 <= n <= len(records)]
        chosen = records[(pending[0] if pending else len(records)) - 1]
        return 200, {"builder_session_id": self.session_id,
                     "builder_invocation_id": self.invocation_id,
                     "records": [self._with_feedback(chosen)]}

    def status(self) -> tuple[int, dict[str, Any]]:
        if self.controller.readiness_profile: self.bind_native_thread()
        return 200, {"builder_session_id": self.session_id, "builder_invocation_id": self.invocation_id, "records": [self._with_feedback(record) for record in self.controller.records], "feedback": self.controller.feedback, "feedback_digest": self.controller.feedback_digest, "frozen": self.controller.frozen is not None, "terminal_infrastructure_latched": self.controller.terminal_infrastructure_latch is not None, "undelivered_responses": self.undelivered_responses}

    def validate(self):
        if not self.controller.readiness_profile: return 400,{'error':'readiness profile required'}
        if not self.submission_lock.acquire(blocking=False): return 409,{'error':'evaluation already active'}
        try:
            from agentloop.readiness import validate_metadata
            from agentloop.candidate_adapter import build_candidate
            from agentloop.stable_product import product_source_digest
            self.bind_native_thread();self.controller._load_state()
            records=self.controller.records
            if len(records)>=2 or self.controller.terminal_infrastructure_latch: return 409,{'valid':False,'error':'readiness lifecycle terminal'}
            validate_metadata(self.workspace,{'builder_session_id':self.session_id,'submission_number':len(records)+1,
                'revision_of_candidate_digest':records[-1]['candidate_digest'] if records else None,
                'feedback_digest':self.controller.feedback_digest if records else None})
            base=self.run_dir/'presubmit';base.mkdir(exist_ok=True)
            attempt=Path(tempfile.mkdtemp(prefix='check-',dir=base));delivery=attempt/'submission'
            shutil.copytree(self.workspace,delivery,symlinks=True)
            built=build_candidate(ROOT/'input/repository',delivery,attempt/'build')
            valid=built.get('valid') is True and product_source_digest(attempt/'build/repository')==built.get('product_source_digest')
            if records and built.get('product_source_digest')==records[-1]['build'].get('product_source_digest'): valid=False
            from agentloop.public_feedback import build_feedback
            return (200 if valid else 409),{'valid':valid,'round_consumed':False,'build':build_feedback(built),'builder_session_id':self.session_id}
        except (ValueError,OSError,RuntimeError) as exc:
            return 409,{'valid':False,'round_consumed':False,'error':str(exc)}
        finally:self.submission_lock.release()

    def close(self) -> None:
        if self.server:
            self.server.shutdown()
            self.server.server_close()
        self.socket_path.unlink(missing_ok=True)


def start_submission_socket(lifecycle: BuilderLifecycle) -> None:
    class Handler(socketserver.StreamRequestHandler):
        def handle(self) -> None:
            request: dict[str, Any] = {}
            try:
                request = json.loads(self.rfile.readline(1 << 20))
                if request.get("token") != lifecycle.token:
                    code, payload = 401, {"error": "unauthorized"}
                elif request.get("action") == "submit":
                    code, payload = lifecycle.submit(request.get("feedback_digest"))
                elif request.get("action") == "status":
                    code, payload = lifecycle.status()
                elif request.get('action')=='validate':
                    code,payload=lifecycle.validate()
                elif request.get("action") == "feedback":
                    code, payload = lifecycle.feedback()
                else:
                    code, payload = 400, {"error": "unknown_action"}
            except Exception as exc:
                code, payload = 400, {"error": f"{type(exc).__name__}: {exc}"}
            try:
                self.wfile.write(json.dumps({"status": code, "payload": payload}, ensure_ascii=False).encode() + b"\n")
                self.wfile.flush()
            except OSError as exc:
                # The Builder's helper was killed, or its shell tool timed out,
                # while the evaluator was still answering. Nothing arrived, so
                # nothing is receipted: record the failed write and keep the
                # ready response deliverable by a later --status or --feedback
                # connection.
                if code == 200:
                    lifecycle.feedback_write_failed(payload, exc)
                else:
                    lifecycle.response_write_failed(
                        request.get("action") if isinstance(request, dict) else None,
                        code, payload, exc)
                return
            if code == 200:
                # feedback_written itself decides what this write actually
                # carried, per record and by digest, so a --status or
                # --feedback response can mint the receipt a lost submit owed.
                lifecycle.feedback_written(payload)

    class Server(socketserver.ThreadingUnixStreamServer):
        daemon_threads = True

    lifecycle.socket_path.unlink(missing_ok=True)
    lifecycle.server = Server(str(lifecycle.socket_path), Handler)
    lifecycle.socket_path.chmod(stat.S_IRUSR | stat.S_IWUSR)
    threading.Thread(target=lifecycle.server.serve_forever, daemon=True).start()


def write_submit_helper(path: Path) -> None:
    path.write_text(
        """#!/usr/bin/env python3
\"\"\"Builder-side controller client.

A dev evaluation runs the public cases and takes far longer than one shell tool
call, so the submit runs in a detached, fully daemonised holder. The foreground
process only polls a local response file in short bounded waits: a shell-tool
timeout can kill the poller without cutting the evaluator response off. Nothing
here needs nohup, backgrounding or kill.
\"\"\"
import argparse, json, os, socket, time
from pathlib import Path

TMP = Path(os.environ.get("TMPDIR") or "/tmp")
RESPONSE = TMP / "agentswe-submit-response.json"
HOLDER = TMP / "agentswe-submit-holder.pid"
VALIDATE_RESPONSE = TMP / "agentswe-validate-response.json"
VALIDATE_HOLDER = TMP / "agentswe-validate-holder.pid"

p = argparse.ArgumentParser(prog="submit_dev_candidate")
p.add_argument("--status", action="store_true",
               help="every accepted record, its own full feedback and its exact digest")
p.add_argument("--feedback", action="store_true",
               help="reprint one accepted record's evaluator feedback and its exact digest")
p.add_argument("--validate", action="store_true", help="run the evaluator-owned preflight")
p.add_argument("--feedback-digest", dest="feedback_digest",
               help="exact digest of the feedback this revision answers")
p.add_argument("--wait", action="store_true",
               help="accepted for compatibility; a submit always waits by polling")
p.add_argument("--await-only", dest="await_only", action="store_true",
               help="keep waiting for the submission already running")
p.add_argument("--poll-seconds", dest="poll_seconds", type=float, default=240.0,
               help="how long this call polls before reporting 202")
a, _extra = p.parse_known_args()

SOCK = os.environ["AGENTSWE_DEV_CONTROLLER_SOCKET"]
TOKEN = os.environ["AGENTSWE_DEV_CONTROLLER_TOKEN"]


def call(request):
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as s:
        s.connect(SOCK)
        s.sendall((json.dumps({"token": TOKEN, **request}) + "\\n").encode())
        return json.loads(s.makefile().readline())


def finish(result):
    print(json.dumps(result, ensure_ascii=False, indent=2))
    raise SystemExit(0 if result.get("status") in (200, 202) else 1)


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


if a.status:
    finish(call({"action": "status"}))
if a.feedback:
    finish(call({"action": "feedback"}))
if a.validate:
    # The preflight copies the delivery and runs the controlled build, which can
    # outlast one shell tool call. Route it through the same detached holder the
    # submit uses: a killed foreground poller loses nothing, and running the
    # same command again (optionally with --await-only) reprints the preflight
    # the holder already stored.
    if not a.await_only and not alive(VALIDATE_HOLDER):
        detach({"action": "validate"}, VALIDATE_RESPONSE, VALIDATE_HOLDER)
    preflight = poll_path(VALIDATE_RESPONSE, time.time() + max(1.0, a.poll_seconds))
    if preflight is None:
        print(json.dumps({"status": 202, "payload": {"state": "validate_in_progress",
            "reason": "the evaluator preflight is still building this Candidate; run "
                      "submit_dev_candidate --validate --await-only again to keep waiting. "
                      "The preflight result is kept on disk, so a shell-tool timeout never "
                      "loses it and the next call reprints it"}},
            ensure_ascii=False, indent=2))
        raise SystemExit(3)
    finish(preflight)

if not a.await_only and not holder_alive():
    for item in (RESPONSE, HOLDER):
        try:
            item.unlink()
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
        ensure_ascii=False, indent=2))
    raise SystemExit(3)
finish(result)
""",
        encoding="utf-8",
    )
    path.chmod(0o755)


def stage_public_package(run_dir: Path, public_case_ids: tuple[str, ...] = ("dev_001", "dev_002")) -> tuple[Path, dict[str, Any]]:
    public = run_dir / "builder_public_package"
    subprocess.run([sys.executable, str(ROOT / "harbor" / "stage_public_package.py"), "--output", str(public)], check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    for case_dir in (public / "dev_cases").iterdir():
        if case_dir.is_dir() and case_dir.name not in public_case_ids:
            shutil.rmtree(case_dir)
    manifest = read_json(public / "package_manifest.json")
    manifest["public_case_inventory"] = list(public_case_ids)
    write_json(public / "package_manifest.json", manifest)
    return public, manifest


def builder_config(run_dir: Path, public: Path, workspace: Path, lifecycle: BuilderLifecycle, provider: Path, image: str, *, pilot: bool = False, timeout_contract: dict[str, int | bool] | None = None) -> Path:
    timeout_contract = timeout_contract or builder_timeout_contract(DEFAULT_OUTER_BUILDER_TIMEOUT_SECONDS)
    task = run_dir / "builder_task"
    (task / "environment").mkdir(parents=True)
    (task / "tests").mkdir(parents=True)
    (task / "task.toml").write_text(
        f"""schema_version = "1.4"
[task]
name = "local/ai-scientist-reproducibility-gate-agentloop-builder"
version = "1.0.0"
description = "AI Scientist same-session {'one-case pilot' if pilot else 'formal'} feedback lifecycle"
artifacts = [{{ source = "/workspace/submission", destination = "builder_submission" }}]
[agent]
timeout_sec = {float(timeout_contract['generated_task_timeout_seconds'])}
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
workdir = "/workspace"
[environment.healthcheck]
command = "python3 /usr/local/lib/agentswe-builder-resource-check.py"
retries = 1
timeout_sec = 10.0
""",
        encoding="utf-8",
    )
    public_description = "dev_001 only" if pilot else "both dev_001 and dev_002"
    hidden_description = "test_001 only as a non-formal pipeline pilot" if pilot else "all six hidden cases"
    (task / "instruction.md").write_text(
        f"""# AI Scientist Edit Builder: same-session feedback revision

You are the only upper Builder session. Read only /builder-package/input,
/builder-package/dev_cases, and /builder-package/input/repository. Never
inspect hidden cases, evaluator source, credentials, prior runs, or Candidate
snapshots.

Work in /workspace/worktree. Put exactly solution.patch, edit_report.json,
and run_report.json in /workspace/submission. Submit each new snapshot with
submit_dev_candidate --wait. The evaluator runs {public_description} against the
Candidate's own AI Scientist product and returns feedback and a digest.
Consume that feedback in this same uninterrupted session, revise when useful,
regenerate all three files, and submit another distinct digest with
submit_dev_candidate --feedback-digest <digest> --wait. Continue for up to
{lifecycle.controller.max_dev_rounds} accepted rounds or exit when satisfied.
Duplicate digests are idempotent and dev mean above 60 never freezes by itself.
Hidden cases are unavailable and {hidden_description} executes only after the latest accepted snapshot is frozen.
This is {'pilot evidence only; no formal Result or Code is published' if pilot else 'the formal lifecycle'}.

A dev evaluation runs the public cases and can take far longer than one shell
tool call, so submit_dev_candidate hands the submit to a detached helper and
returns early instead of holding one long command. If it prints status 202 with
state submission_in_progress, run submit_dev_candidate --await-only again, as
many times as needed, until it prints the real response. Never wrap the command
in nohup, never background or disown it, and never kill it. At any time,
including while an evaluation is still running, submit_dev_candidate --status
answers within seconds with every accepted record, its own full feedback and
its exact digest, and submit_dev_candidate --feedback reprints one accepted
record's feedback and digest. If a response is ever lost, recover it with one
of those two commands; do not resubmit an unchanged Candidate.
""",
        encoding="utf-8",
    )
    submit = task / "environment" / "submit_dev_candidate"
    if lifecycle.controller.readiness_profile:
        instruction=task/'instruction.md'
        instruction.write_text(instruction.read_text().replace(
            'Continue for up to\n2 accepted rounds or exit when satisfied.\nDuplicate digests are idempotent and dev mean above 60 never freezes by itself.',
            'Complete exactly two accepted valid rounds in this native session, then exit. If a submit_dev_candidate call ends without printing a JSON response, its result was lost in transit: re-issue the identical command with the same arguments until it prints one. Repeating an already accepted submission is answered from the duplicate path and consumes no round. Each must change product source. Duplicate submissions are rejected.')+
            '\nReadiness profile single-dev-two-round-hidden-smoke-v1: run_report.json must add builder_session_id (from submit_dev_candidate --status), submission_number (1 then 2), revision_of_candidate_digest (null then Candidate 1 digest), feedback_digest (null then exact F1). Round 2 edit_report.json must include feedback_response explaining how the revision addresses F1. Keep the seven shared run_report fields. Before each submission run submit_dev_candidate --validate --wait to check delivery metadata and controlled compilation. Score has no readiness threshold. Hidden results are evaluator-only.\n'
            'A dev evaluation can take more than ten minutes, which is longer than one shell tool call, so submit_dev_candidate hands the submit to a detached helper and returns early instead of holding one long command. If it prints status 202 with state submission_in_progress, run submit_dev_candidate --await-only again, as many times as needed, until it prints the real response. Never wrap the command in nohup, never background or disown it, and never kill it. At any time, including while an evaluation is still running, submit_dev_candidate --status answers within seconds with every accepted record, its own full feedback and its exact digest, and submit_dev_candidate --feedback reprints one accepted record and its digest. If a submit response is ever lost, recover it with one of those two commands instead of resending a delivery: they consume no round, they cannot trip the replay fence, and they re-deliver the same authoritative feedback bytes.\n')
    write_submit_helper(submit)
    resource_check = task / 'environment/builder_resource_check.py'
    shutil.copyfile(ROOT / 'harbor/builder_resource_check.py', resource_check)
    test = task / "tests" / "test.sh"
    test.write_text("printf '0\\n' > /logs/verifier/reward.txt\nexit 0\n", encoding="utf-8")
    test.chmod(0o755)
    # Harbor validates CODEX_CONFIG_TOML_PATH on the host before launching the
    # task container.  Use the run-local absolute host path and bind it to the
    # exact same absolute path inside the container.
    provider_target = str(provider.resolve())
    persistent_worktree = run_dir / "builder_workspace" / "worktree"
    persistent_home = run_dir / "builder_workspace" / "codex_home"
    persistent_worktree.mkdir(parents=True,exist_ok=True)
    persistent_home.mkdir(parents=True,exist_ok=True)
    wrapper = task / "environment" / "codex-no-unified-exec"
    wrapper.write_text('#!/usr/bin/python3\n"""Run native Codex with two explicit feature overrides, preserving argv."""\nimport os\nimport sys\n\nDISABLED_FEATURES = frozenset(("unified_exec", "code_mode_host"))\nNATIVE_CODEX = "/opt/openai/codex/bin/codex.js"\n\n\ndef native_args(args):\n    out = ["--disable", "unified_exec", "--disable", "code_mode_host"]\n    index = 0\n    while index < len(args):\n        arg = args[index]\n        if arg == "--":\n            out.extend(args[index:])\n            break\n        if arg == "--enable" and index + 1 < len(args):\n            feature = args[index + 1]\n            if feature not in DISABLED_FEATURES:\n                out.extend((arg, feature))\n            index += 2\n            continue\n        if arg.startswith("--enable=") and arg.split("=", 1)[1] in DISABLED_FEATURES:\n            index += 1\n            continue\n        # Preserve option values literally, even when a value resembles a flag.\n        value_options = {\n            "-c", "--config", "-m", "--model", "-p", "--profile",\n            "-C", "--cd", "-s", "--sandbox", "-i", "--image",\n            "-o", "--output-last-message", "--output-schema", "--disable",\n            "--color", "--add-dir", "--local-provider",\n        }\n        if arg in value_options and index + 1 < len(args):\n            out.extend(args[index:index + 2])\n            index += 2\n            continue\n        out.append(arg)\n        index += 1\n    return out\n\n\nif __name__ == "__main__":\n    os.execv(NATIVE_CODEX, ["codex"] + native_args(sys.argv[1:]))\n', encoding="utf-8")
    wrapper.chmod(0o755)
    write_json(task / "environment" / "docker-compose.yaml", {"services": {"main": {"command": ["sh", "-c", "mkdir -p /tmp/codex-home && exec sleep infinity"], "cpu_quota": 800000, "cpu_period": 100000, "volumes": [
        {"type": "bind", "source": str(public), "target": "/builder-package", "read_only": True},
        {"type": "bind", "source": str(workspace), "target": "/workspace/submission"},
        {"type": "bind", "source": str(persistent_worktree), "target": "/workspace/worktree"},
        {"type": "bind", "source": str(persistent_home), "target": "/tmp/codex-home"},
        {"type": "bind", "source": str(lifecycle.socket_path), "target": "/run/agentswe-controller.sock"},
        {"type": "bind", "source": str(submit), "target": "/usr/local/bin/submit_dev_candidate", "read_only": True},
        {"type": "bind", "source": str(resource_check), "target": "/usr/local/lib/agentswe-builder-resource-check.py", "read_only": True},
        {"type": "bind", "source": str(wrapper), "target": "/workspace/codex-wrapper/codex", "read_only": True},
        {"type": "bind", "source": str(provider), "target": provider_target, "read_only": True},
    ], "environment": {"AGENTSWE_DEV_CONTROLLER_SOCKET": "/run/agentswe-controller.sock", "AGENTSWE_DEV_CONTROLLER_TOKEN": lifecycle.token}}}})
    config = run_dir / "builder_job_config.json"
    write_json(config, {"job_name": f"ai-scientist-agentloop-builder-{run_dir.name}", "jobs_dir": str(run_dir / "jobs"), "n_attempts": 1, "n_concurrent_trials": 1, "quiet": True, "retry": {"max_retries": 0}, "environment": {"type": "docker", "delete": True, "cpu_enforcement_policy": "limit"}, "agents": [{"import_path": "agentswe_codex_resume:CodexResume", "model_name": BUILDER_MODEL, "env": {"CODEX_HOME": "/tmp/agentswe-codex-home", "CODEX_CONFIG_TOML_PATH": provider_target, "PATH": "/workspace/codex-wrapper:/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin"}, "kwargs": {"reasoning_effort": BUILDER_EFFORT, "web_search": "live"}}], "tasks": [{"path": str(task)}], "timeout_contract": timeout_contract})
    return config


def freeze_after_builder_exit(lifecycle: BuilderLifecycle, exit_code: int) -> dict[str, Any] | None:
    """An interrupted under-budget session is recoverable, not builder_exit.

    A budget-complete freeze already created by the controller is preserved.
    """
    if exit_code == 0 and lifecycle.controller.records and not lifecycle.controller.frozen:
        if lifecycle.controller.readiness_profile:
            native=verify_native(lifecycle.run_dir,lifecycle.controller.records,
                [e for e in lifecycle.events if e.get('event')=='feedback_delivered'],lifecycle.native_observations)
            lifecycle.controller.freeze_latest('builder_exit',builder_exit_evidence={**native,'builder_exit_code':exit_code})
        else:
            lifecycle.controller.freeze_latest("builder_exit")
    return lifecycle.controller.frozen


def builder_attestation(lifecycle: BuilderLifecycle, exit_code: int, *, pilot: bool = False) -> dict[str, Any]:
    records = lifecycle.controller.records
    expected_public = ["dev_001"] if pilot else ["dev_001", "dev_002"]
    accepted = [event for event in lifecycle.events if event.get("event") == "submission_finished" and event.get("accepted")]
    feedback_events = [event for event in lifecycle.events if event.get("event") == "feedback_delivered"]
    native = verify_native(lifecycle.run_dir, records, feedback_events, lifecycle.native_observations,
                           allow_interrupted=(exit_code != 0 and (lifecycle.controller.frozen or {}).get("freeze_reason") == "max_dev_rounds"))
    result: dict[str, Any] = {
        "schema_version": "agentswe-ai-scientist-builder-session-attestation-v1",
        "evidence_kind": lifecycle.controller.run_kind,
        "builder_session_id": lifecycle.session_id,
        "builder_invocation_id": lifecycle.invocation_id,
        "builder_model": BUILDER_MODEL,
        "builder_reasoning_effort": BUILDER_EFFORT,
        "builder_exit_code": exit_code,
        "single_continuous_session": native["valid"],
        "same_session": bool(records) and all(record.get("builder_session_id") == lifecycle.session_id for record in records),
        "native_evidence": native,
        "events": lifecycle.events,
        "accepted_candidate_count": len(records),
        "accepted_submission_digests": [record.get("candidate_digest") for record in records],
        "all_accepted_rounds_public_cases": bool(records) and all(sorted((record.get("dev") or {}).keys()) == expected_public for record in records),
        "feedback_delivered": bool(records) and all(
            any(event.get("candidate_number") == record.get("submission_number")
                and event.get("feedback_digest") == record.get("feedback_digest")
                and event.get("builder_session_id") == lifecycle.session_id for event in feedback_events)
            for record in records),
        "feedback_digest": lifecycle.controller.feedback_digest,
        "distinct_delivery_digests": len({record.get("candidate_digest") for record in records}) == len(records),
        "distinct_product_digests": all(isinstance(record.get("build", {}).get("product_source_digest"), str) for record in records) and len({record.get("build", {}).get("product_source_digest") for record in records}) == len(records),
        "max_dev_rounds": lifecycle.controller.max_dev_rounds,
        "n_concurrent": lifecycle.controller.n_concurrent,
        "dev_passed_is_automatic_freeze": False,
        "freeze_reason": (lifecycle.controller.frozen or {}).get("freeze_reason"),
        "freeze": lifecycle.controller.frozen,
    }
    result["public_case_inventory"] = expected_public
    freeze = result["freeze"] if isinstance(result["freeze"], dict) else {}
    revision = freeze.get("revision_contract") if isinstance(freeze.get("revision_contract"), dict) else {}
    requires_revision = len(records) > 1
    later_distinct = bool(revision.get("later_distinct_candidate_present"))
    feedback_bound = bool(revision.get("feedback_consumed"))
    result["revision_contract"] = {
        "requires_feedback_bound_later_distinct_candidate": requires_revision,
        "later_distinct_candidate_present": later_distinct,
        "feedback_consumed": feedback_bound,
        "empty_feedback_attestation": False,
    }
    result["builder_terminal_valid"] = exit_code == 0 or (
        result["freeze_reason"] == "max_dev_rounds" and len(records) == lifecycle.controller.max_dev_rounds)
    result["structural_complete"] = all((result["same_session"], bool(records), result["all_accepted_rounds_public_cases"], result["feedback_delivered"], result["distinct_delivery_digests"], result["distinct_product_digests"], bool(result["freeze"]), result["freeze_reason"] in {"max_dev_rounds", "builder_exit"}, result["builder_terminal_valid"], freeze.get("feedback_chain_complete") is True, freeze.get("feedback_consumed") is True, (not requires_revision or later_distinct), (not requires_revision or feedback_bound)))
    result["complete"] = result["structural_complete"] and native["valid"]
    result["feedback_revision_observed"] = bool(native["valid"] and native["revision_observed"] and requires_revision and later_distinct and feedback_bound)
    result["formal_lifecycle_eligible"] = bool(not pilot and lifecycle.controller.run_kind == "formal" and result["complete"])
    result["pilot_lifecycle_eligible"] = bool(pilot and lifecycle.controller.run_kind == "pilot" and result["complete"])
    return result


def static_config(run_dir: Path) -> dict[str, Any]:
    run_dir.mkdir(parents=True, exist_ok=True)
    public, manifest = stage_public_package(run_dir)
    workspace = run_dir / "builder_workspace" / "submission"
    workspace.mkdir(parents=True)
    lifecycle = BuilderLifecycle(run_dir, workspace, "http://127.0.0.1:1/v1/responses", LOWER_IMAGE)
    try:
        provider = run_dir / "builder_provider.toml"
        direct_builder.write_provider(provider)
        timeout_contract = builder_timeout_contract(DEFAULT_OUTER_BUILDER_TIMEOUT_SECONDS)
        config = builder_config(run_dir, public, workspace, lifecycle, provider, BUILDER_IMAGE, timeout_contract=timeout_contract)
        lock = {"schema_version": "agentswe-ai-scientist-agentloop-protocol-v1", "builder": {"model": BUILDER_MODEL, "reasoning_effort": BUILDER_EFFORT, "single_continuous_session": True, "transport": "native_codex_direct", "builder_broker_started": False}, "lower_agent": {"product": "Candidate-modified AI Scientist", "model": MODEL, "reasoning_effort": LOWER_EFFORT, "broker": "independent_evaluator_owned", "credential": "broker-only-placeholder"}, "public_cases": ["dev_001", "dev_002"], "hidden_cases": [f"test_{i:03d}" for i in range(1, 7)], "builder_visibility": manifest, "submission_socket": str(lifecycle.socket_path), "timeout_contract": timeout_contract, "result_axis": "N/A until complete hidden evidence and independent Result judge", "code_axis": "N/A until independent Code judge"}
        write_json(run_dir / "protocol_lock.json", lock)
        return {"status": "static_config_ready", "formal_execution_started": False, "builder_job_config": str(config), "protocol_lock": str(run_dir / "protocol_lock.json"), "public_package": str(public), "package_manifest": manifest}
    finally:
        lifecycle.close()


def run_execution(args: argparse.Namespace, *, pilot: bool) -> int:
    profile=getattr(args,'readiness_profile',None)
    binding=None
    if profile:
        from agentloop.readiness import PROFILE,SHARED,sha,validate_binding
        if profile!=PROFILE or not pilot: raise ValueError('readiness requires explicit pilot profile')
        if sha(args.readiness_binding_file)!=args.readiness_binding_sha256: raise ValueError('readiness binding file changed')
        binding=validate_binding(read_json(args.readiness_binding_file))
        sys.path.insert(0,str(SHARED))
        from readiness_binding import verify_binding,configure_native_no_replay
        control_cfg=__import__('importlib.util').util.spec_from_file_location('readiness_config',SHARED/'formal_config.py')
        cfg=__import__('importlib.util').util.module_from_spec(control_cfg); control_cfg.loader.exec_module(cfg)
        verify_binding(cfg.TASKS['ai-scientist'],binding)
    run_dir = args.run_dir.resolve()
    if run_dir.exists() and any(run_dir.iterdir()):
        raise RuntimeError(f"refusing to overwrite non-empty run directory: {run_dir}")
    run_dir.mkdir(parents=True, exist_ok=True)
    credential, harbor = args.credential_file.resolve(), args.harbor.resolve()
    if not credential.is_file():
        raise FileNotFoundError(f"evaluator credential file missing: {credential}")
    if not harbor.is_file():
        raise FileNotFoundError(f"Harbor executable missing: {harbor}")
    lower_port, builder_port, judge_port = free_port(), free_port(), free_port()
    while len({lower_port, builder_port, judge_port}) != 3:
        builder_port, judge_port = free_port(), free_port()
    public_lower_endpoint = f"http://127.0.0.1:{lower_port}/v1/responses"
    builder_endpoint = f"http://{args.builder_host_address}:{builder_port}/v1/responses"
    judge_endpoint = f"http://127.0.0.1:{judge_port}/v1/responses"
    public_judge_port = free_port()
    while public_judge_port in {lower_port, builder_port, judge_port}:
        public_judge_port = free_port()
    public_judge_endpoint = f"http://127.0.0.1:{public_judge_port}/v1/responses"
    public_case_ids = ("dev_001",) if pilot else ("dev_001", "dev_002")
    hidden_case_ids = ("test_001",) if pilot else tuple(f"test_{i:03d}" for i in range(1, 7))
    evidence_kind = "pilot" if pilot else "formal"
    suffix = hashlib.sha256(str(run_dir).encode()).hexdigest()[:10]
    public_lower_name = f"ai-scientist-{evidence_kind}-public-lower-{suffix}"
    hidden_lower_name = f"ai-scientist-{evidence_kind}-hidden-lower-{suffix}"
    builder_name = f"ai-scientist-{evidence_kind}-builder-{suffix}"
    judge_name = f"ai-scientist-{evidence_kind}-result-judge-{suffix}"
    public_judge_name = f"ai-scientist-{evidence_kind}-dev-judge-{suffix}"
    cidfiles = {
        "public_lower": run_dir / "brokers/public_lower.cid",
        "hidden_lower": run_dir / "brokers/hidden_lower.cid",
        "builder": run_dir / "brokers/builder.cid",
        "judge": run_dir / "brokers/judge.cid",
        "public_judge": run_dir / "brokers/public_judge.cid",
    }
    attempted = {key: False for key in cidfiles}
    hidden_lower_endpoint: str | None = None
    lifecycle: BuilderLifecycle | None = None
    judge_handle = None
    try:
        attempted["public_lower"] = True
        start_broker(public_lower_name, LOWER_BROKER, credential, lower_port, LOWER_EFFORT, args.builder_image, cidfiles["public_lower"])
        if not profile:
            attempted["public_judge"] = True
            start_broker(public_judge_name, JUDGE_BROKER_SCRIPT, credential, public_judge_port, BUILDER_EFFORT, args.builder_image, cidfiles["public_judge"])
        provider = run_dir / "builder_provider.toml"
        direct_builder.write_provider(provider, args.builder_base_url)
        if profile:
            write_json(run_dir/'builder_provider_retry_policy.json',configure_native_no_replay(provider))
            write_json(run_dir/'readiness_current_binding.json',binding)
        public, manifest = stage_public_package(run_dir, public_case_ids)
        workspace = run_dir / "builder_workspace" / "submission"
        workspace.mkdir(parents=True)
        lifecycle = BuilderLifecycle(
            run_dir,
            workspace,
            public_lower_endpoint,
            args.lower_image,
            public_case_ids=public_case_ids,
            hidden_case_ids=hidden_case_ids,
            run_kind=evidence_kind,
            max_dev_rounds=2 if profile else args.max_dev_rounds,
            n_concurrent=args.n_concurrent,
            result_judge_endpoint=None if profile else public_judge_endpoint,
            readiness_profile=profile,current_binding=binding,
        )
        start_submission_socket(lifecycle)
        timeout_contract = builder_timeout_contract(args.builder_timeout)
        config = builder_config(run_dir, public, workspace, lifecycle, provider, args.builder_image, pilot=pilot, timeout_contract=timeout_contract)
        write_json(run_dir / "protocol_lock.json", {"schema_version": "agentswe-ai-scientist-pilot-protocol-v1" if pilot else "agentswe-ai-scientist-agentloop-protocol-v1", "evidence_kind": evidence_kind, "builder": {"model": BUILDER_MODEL, "reasoning_effort": BUILDER_EFFORT, "transport": "native_codex_direct", "builder_broker_started": False, "single_continuous_session": True}, "lower_agent": {"product": "Candidate-modified AI Scientist", "model": MODEL, "reasoning_effort": LOWER_EFFORT, "public_broker_endpoint": public_lower_endpoint, "hidden_broker_endpoint": None, "credential": "broker-only-placeholder"}, "timeout_contract": timeout_contract, "independent_brokers": True, "fresh_hidden_broker_after_freeze": True, "public_cases": list(public_case_ids), "hidden_cases": list(hidden_case_ids), "builder_visibility": manifest, "formal_finalizer_allowed": not pilot, "formal_result_publishable": False, "code_score_publishable": False})
        lifecycle.event("builder_invocation_started")
        if profile:
            lock=read_json(run_dir/'protocol_lock.json');lock.update(readiness_profile=profile,current_binding=binding,required_valid_rounds=2,score_threshold=None)
            write_json(run_dir/'protocol_lock.json',lock)
        started = time.time_ns()
        completed_builder = run_native_builder(lifecycle=lifecycle, config=config,
            credential=credential, harbor=harbor, timeout=args.builder_timeout,
            proxy=args.builder_proxy, provider_url=args.builder_base_url)
        builder_exit = completed_builder.returncode
        lifecycle.event("builder_invocation_finished", exit_code=builder_exit)
        freeze_after_builder_exit(lifecycle, builder_exit)
        attestation = builder_attestation(lifecycle, builder_exit, pilot=pilot)
        attestation.update({"builder_started_epoch_ns": started, "builder_ended_epoch_ns": time.time_ns()})
        write_json(run_dir / "builder_session_attestation.json", attestation)
        if not attestation["complete"]:
            write_json(run_dir / "summary.json", {"schema_version": "agentswe-ai-scientist-pilot-summary-v1" if pilot else "agentswe-ai-scientist-formal-summary-v1", "evidence_kind": evidence_kind, "status": "builder_integration_incomplete", "formal_result_claimed": False, "code_score_claimed": False, "result_axis": "N/A", "code_axis": "N/A", "formal_finalizer_invoked": False, "builder_attestation": attestation, "builder_transport": direct_builder.native_stats(run_dir), "public_lower_broker": broker_stats(public_lower_endpoint), "hidden_lower_broker": None})
            return 2
        hidden_port = free_port()
        while hidden_port in {lower_port, builder_port}:
            hidden_port = free_port()
        hidden_lower_endpoint = f"http://127.0.0.1:{hidden_port}/v1/responses"
        attempted["hidden_lower"] = True
        start_broker(hidden_lower_name, LOWER_BROKER, credential, hidden_port, LOWER_EFFORT, args.builder_image, cidfiles["hidden_lower"])
        hidden_broker_before = broker_stats(hidden_lower_endpoint)
        hidden_initial_calls = broker_call_count(hidden_broker_before)
        hidden_initial_failures = broker_failure_count(hidden_broker_before)
        if hidden_initial_calls != 0 or hidden_initial_failures != 0:
            raise RuntimeError(
                "fresh hidden lower broker did not start at calls=0/failures=0: "
                f"calls={hidden_initial_calls}, failures={hidden_initial_failures}"
            )
        write_json(run_dir / "hidden_broker_before.json", hidden_broker_before)
        lifecycle.controller.broker_endpoint = hidden_lower_endpoint
        protocol = read_json(run_dir / "protocol_lock.json")
        protocol["lower_agent"]["hidden_broker_endpoint"] = hidden_lower_endpoint
        protocol["lower_agent"]["hidden_broker_initial_calls"] = hidden_initial_calls
        protocol["lower_agent"]["hidden_broker_initial_failures"] = hidden_initial_failures
        protocol["lower_agent"]["controller_endpoint_switched_after_freeze"] = True
        write_json(run_dir / "protocol_lock.json", protocol)
        hidden = lifecycle.controller.run_hidden(hidden_case_ids)
        hidden_attestation = read_json(run_dir / "lifecycle" / "hidden-after-freeze-attestation.json")
        readiness_judges = None
        if profile:
            attempted["judge"] = True
            judge_handle = start_broker(judge_name, JUDGE_BROKER_SCRIPT, credential, judge_port, BUILDER_EFFORT, args.builder_image, cidfiles["judge"], defer_removal=True)
            from evaluator.readiness_smoke import run as run_readiness_smoke
            readiness_judges = run_readiness_smoke(task_root=ROOT, run_dir=run_dir, hidden=hidden_attestation,
                credential=credential, result_endpoint=judge_endpoint)
            write_json(run_dir/'readiness_judges.json',readiness_judges)
        finalizer_exit = None
        finalizer_result = None
        if not pilot:
            attempted["judge"] = True
            start_broker(judge_name, JUDGE_BROKER_SCRIPT, credential, judge_port, BUILDER_EFFORT, args.builder_image, cidfiles["judge"])
        if not pilot and hidden_evidence_ready_for_finalizer(hidden_attestation):
            command = [sys.executable, str(ROOT / "evaluator" / "formal_finalize.py"), "--run-dir", str(run_dir),
                       "--credential-file", str(credential), "--result-broker-endpoint", judge_endpoint,
                       "--result-judge", str(SHARED_RESULT_JUDGE), "--code-judge", str(CREATE_CODE_JUDGE)]
            completed = subprocess.run(command, text=True, capture_output=True, check=False)
            finalizer_exit = completed.returncode
            if (run_dir / "formal_aggregation.json").is_file():
                finalizer_result = read_json(run_dir / "formal_aggregation.json")
        execution_complete = hidden_attestation.get("pilot_complete" if pilot else "formal_complete") is True
        pilot_measurement: dict[str, Any] | str = "N/A"
        if pilot and execution_complete:
            pilot_measurement = {"score": "N/A", "reason": "single-case pilot is diagnostic only and cannot invoke formal publication", "formal_result_publishable": False, "code_score_publishable": False}
        write_json(run_dir / "summary.json", {"schema_version": "agentswe-ai-scientist-pilot-summary-v1" if pilot else "agentswe-ai-scientist-formal-summary-v1", "evidence_kind": evidence_kind, "status": "pilot_pipeline_complete" if pilot and execution_complete else ("lifecycle_complete" if execution_complete else "lifecycle_evidence_invalid_or_candidate_failed"), "pilot_evidence_complete": execution_complete if pilot else False, "pilot_measurement": pilot_measurement if pilot else None, "formal_result_claimed": bool(finalizer_result and finalizer_result.get("formal_result_publishable")), "code_score_claimed": bool(finalizer_result and finalizer_result.get("code_score_publishable")), "formal_finalizer_invoked": bool(not pilot and finalizer_exit is not None), "builder_session_attestation": "builder_session_attestation.json", "freeze_manifest": "lifecycle/freeze_manifest.json", "hidden_attestation": "lifecycle/hidden-after-freeze-attestation.json", "hidden": hidden, "fresh_hidden_broker_initial_calls": hidden_initial_calls, "fresh_hidden_broker_initial_failures": hidden_initial_failures, "builder_transport": direct_builder.native_stats(run_dir), "public_lower_broker": broker_stats(public_lower_endpoint), "hidden_lower_broker": broker_stats(hidden_lower_endpoint), "formal_finalizer_exit": finalizer_exit, "formal_aggregation": "formal_aggregation.json" if finalizer_result else None, "result_axis": finalizer_result.get("result_axis") if finalizer_result else "N/A", "code_axis": finalizer_result.get("code_axis") if finalizer_result else "N/A"})
        return 0 if execution_complete and (pilot or finalizer_exit == 0) else 2
    finally:
        if lifecycle is not None:
            lifecycle.close()
        if profile and judge_handle is not None:
            try:
                retained = judge_handle.close()
                # The coordinator reads 'containers'/'networks' as lists of full
                # Docker IDs. A nested role object carries the same ownership
                # proof but is refused by readiness_cleanup.arguments(), so the
                # run can never be finalized. Declare through the shared helper
                # and keep the role detail as its own artifact.
                import types as _types
                from harbor.readiness_resources import retained_manifest
                retained_manifest(run_dir, [] if retained.get('absent_after_cleanup') is True
                                  else [_types.SimpleNamespace(container_id=retained['container_id'])])
                write_json(run_dir/'readiness_judge_retention.json', retained)
            except Exception as exc:
                write_json(run_dir/'readiness_retained_resources.json', {'run_id':run_dir.name,'owner':'evaluator','error':str(exc),'coordinator_cleanup_required':True})
        cleanup_results = [
            remove_and_verify_container("hidden_lower", cidfiles["hidden_lower"], attempted=attempted["hidden_lower"]),
            remove_and_verify_container("public_lower", cidfiles["public_lower"], attempted=attempted["public_lower"]),
            remove_and_verify_container("builder", cidfiles["builder"], attempted=attempted["builder"]),
            remove_and_verify_container("public_judge", cidfiles["public_judge"], attempted=attempted["public_judge"]),
        ]
        try:
            judge_stats = broker_stats(judge_endpoint)
        except Exception as exc:
            judge_stats = {"error": f"{type(exc).__name__}: {exc}"}
        write_json(run_dir / "judge_broker_stats.json", judge_stats)
        cleanup_results.append(remove_and_verify_container("judge", cidfiles["judge"], attempted=attempted["judge"] and not profile))
        controller_closed = lifecycle is None or not lifecycle.socket_path.exists()
        cleanup = {
            "schema_version": "agentswe-cleanup-attestation-v2",
            "containers_requested_removed": [hidden_lower_name, public_lower_name, builder_name, judge_name],
            "cleanup_results": cleanup_results,
            "all_started_containers_absent": all(item["absent_after_cleanup"] for item in cleanup_results),
            "controller_closed": controller_closed,
            "unrelated_containers_touched": False,
            "completed": bool(
                controller_closed
                and all(item["absent_after_cleanup"] for item in cleanup_results)
            ),
        }
        write_json(run_dir / "cleanup_attestation.json", cleanup)
        summary = read_json(run_dir / "summary.json") if (run_dir / "summary.json").is_file() else {}
        aggregation = read_json(run_dir / "formal_aggregation.json") if (run_dir / "formal_aggregation.json").is_file() else {}
        freeze = read_json(run_dir / "lifecycle" / "freeze_manifest.json") if (run_dir / "lifecycle" / "freeze_manifest.json").is_file() else {}
        write_json(run_dir / "one_stop_summary.json", {"schema_version": "agentswe-edit-one-stop-summary-v1", "status": summary.get("status", "formal_execution_failed"), "dev_lifecycle": "lifecycle/dev_lifecycle.json", "freeze": {"path": "lifecycle/freeze_manifest.json", "digest": freeze.get("candidate_materialized_digest"), "reason": freeze.get("freeze_reason")}, "hidden_inventory": [f"test_{i:03d}" for i in range(1, 7)], "hidden_summary": summary.get("hidden", "N/A"), "result_judge_contracts": aggregation.get("result_judge_contracts", {}), "code_contract": aggregation.get("code_contract"), "result_axis": aggregation.get("result_axis", "N/A"), "code_axis": aggregation.get("code_axis", "N/A"), "combined_score": None, "cleanup_attestation": "cleanup_attestation.json", "judge_broker_stats": "judge_broker_stats.json"})


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--run-dir", type=Path, required=True)
    parser.add_argument("--run-formal", action="store_true")
    parser.add_argument("--pilot", action="store_true", help="run isolated dev_001 -> feedback -> dev_001 -> freeze -> test_001 pilot evidence")
    parser.add_argument("--self-test", action="store_true", help="provider-free pilot/static protocol check")
    parser.add_argument("--credential-file", type=Path, default=Path("@@AGENTSWE_CREDENTIAL_FILE@@"))
    parser.add_argument("--harbor", type=Path, default=Path("@@AGENTSWE_HARBOR_BIN@@"))
    parser.add_argument("--builder-image", default=BUILDER_IMAGE)
    parser.add_argument("--lower-image", default=LOWER_IMAGE)
    parser.add_argument("--builder-base-url", default="https://api.deepseek.com/v1")
    parser.add_argument("--builder-proxy", default="http://127.0.0.1:7890")
    parser.add_argument("--builder-host-address", default="172.17.0.1")
    parser.add_argument("--builder-timeout", type=int, default=DEFAULT_OUTER_BUILDER_TIMEOUT_SECONDS)
    parser.add_argument("--max-dev-rounds", type=int, default=10)
    parser.add_argument('--readiness-profile',choices=['single-dev-two-round-hidden-smoke-v1'])
    parser.add_argument('--readiness-binding-file',type=Path)
    parser.add_argument('--readiness-binding-sha256')
    parser.add_argument("--n-concurrent", type=int, default=1)
    args = parser.parse_args()
    if args.readiness_profile and (not args.pilot or args.run_formal or (not args.self_test and (not args.readiness_binding_file or not args.readiness_binding_sha256))):
        parser.error('readiness requires --pilot and binding file/SHA, and forbids --run-formal')
    if not 1 <= args.max_dev_rounds <= 10:
        parser.error("--max-dev-rounds must be in 1..10")
    if args.n_concurrent != 1:
        parser.error("--n-concurrent must equal 1")
    if args.run_formal and args.pilot:
        parser.error("--run-formal and --pilot are mutually exclusive")
    if args.builder_timeout <= 0:
        parser.error("--builder-timeout must be positive")
    try:
        timeout_contract = builder_timeout_contract(args.builder_timeout)
    except ValueError as exc:
        parser.error(str(exc))
    if not timeout_contract["covers_task_plus_cleanup"]:
        parser.error(
            "--builder-timeout must cover generated task timeout plus cleanup margin "
            f"({timeout_contract['required_outer_timeout_seconds']} seconds required)"
        )
    if args.self_test:
        if args.run_formal:
            parser.error("--self-test and --run-formal are mutually exclusive")
        print(json.dumps({"self_test": "PASS", "execution_mode": "pilot" if args.pilot else "static", "provider_calls": 0, "docker_started": False, "harbor_started": False, "builder": {"model": BUILDER_MODEL, "reasoning_effort": BUILDER_EFFORT, "single_continuous_session": True}, "timeout_contract": timeout_contract, "lower_broker_lifecycle": {"public": "started for Candidate public rounds", "hidden": "fresh broker started only after latest accepted Candidate freeze", "hidden_initial_calls_required": 0, "endpoint_switch_required": True}, "public_cases": ["dev_001"] if args.pilot else ["dev_001", "dev_002"], "hidden_cases": ["test_001"] if args.pilot else [f"test_{i:03d}" for i in range(1, 7)], "formal_result_claimed": False, "code_score_claimed": False}, indent=2, ensure_ascii=False))
        return 0
    if args.run_formal:
        return run_execution(args, pilot=False)
    if args.pilot:
        return run_execution(args, pilot=True)
    print(json.dumps(static_config(args.run_dir.resolve()), indent=2, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
