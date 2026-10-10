#!/usr/bin/env python3
"""Real Builder -> up to ten accepted dev-feedback rounds -> freeze -> hidden run.

This file contains only the lifecycle adapter.  The lower product remains the
patched Aider in this sibling; no generic external coding agent is substituted.
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
IMAGE = "agentswe/create-agent-builder-codex:0.144.1-node24.6.0-0812"
# 5 h Builder cap (D2, 2026-09-19).  The shared driver passes an outer
# --builder-timeout of 18000 s; the generated task.toml budget must stay below
# it by the cleanup margin, otherwise Harbor is killed mid-agent and the run
# reports 124 rather than a Builder exit.
GENERATED_BUILDER_TASK_TIMEOUT_SECONDS = 17_880
BUILDER_CLEANUP_MARGIN_SECONDS = 120
DEFAULT_OUTER_BUILDER_TIMEOUT_SECONDS = (
    GENERATED_BUILDER_TASK_TIMEOUT_SECONDS + BUILDER_CLEANUP_MARGIN_SECONDS
)


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
    """argparse gate: refuse an outer budget that cannot cover the task."""
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
ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
from harbor.public_feedback import public_payload
from harbor.native_builder_evidence import observe_thread, verify_native
CASES = json.loads((ROOT / "evaluator/agentloop_cases.json").read_text())


def now() -> str:
    return datetime.now(timezone.utc).isoformat()


def write_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")


def read_json(path: Path) -> dict[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8"))
    return value if isinstance(value, dict) else {}


def _initialize_builder_worktree(worktree: Path) -> None:
    """Create a clean run-local Git baseline from the public source snapshot.

    The pinned public Aider source may contain edits relative to its upstream
    checkout. Reusing its historical index makes those unrelated edits look
    like Builder changes, so the Builder patch becomes noisy and ordinary Git
    inspection loses its meaning. A fresh local baseline preserves the copied
    bytes while making the Builder's diff contain only its own edits.
    """
    git_dir = worktree / ".git"
    if git_dir.exists() or git_dir.is_symlink():
        shutil.rmtree(git_dir)
    commands = [
        ["git", "init", "-q", "-b", "main", str(worktree)],
        ["git", "config", "core.autocrlf", "false"],
        ["git", "config", "user.name", "AgentSWE Builder baseline"],
        ["git", "config", "user.email", "builder-baseline@example.invalid"],
        ["git", "add", "-A"],
        ["git", "commit", "-q", "-m", "public source baseline"],
    ]
    for command in commands:
        completed = subprocess.run(command, cwd=worktree, text=True, capture_output=True, check=False)
        if completed.returncode:
            raise RuntimeError(f"Builder worktree baseline failed: {completed.stderr[-600:]}")


def digest(root: Path) -> str:
    h = hashlib.sha256()
    for item in sorted(root.rglob("*"), key=lambda p: p.relative_to(root).as_posix()):
        rel = item.relative_to(root)
        if any(part in {".git", "__pycache__"} for part in rel.parts) or item.suffix == ".pyc":
            continue
        name = rel.as_posix().encode()
        h.update(len(name).to_bytes(8, "big")); h.update(name)
        if item.is_symlink():
            kind, payload = b"L", os.readlink(item).encode()
        elif item.is_file():
            kind, payload = b"F", item.read_bytes()
        else:
            kind, payload = b"D", b""
        h.update(kind); h.update(len(payload).to_bytes(8, "big")); h.update(payload)
    return h.hexdigest()


def port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])


def read_container_id(cidfile: Path) -> str | None:
    try:
        value = cidfile.read_text(encoding="ascii").strip()
    except OSError:
        return None
    return value or None


def start_broker(*, name: str, script: Path, credential: Path, value_port: int, effort: str, cidfile: Path) -> None:
    if script.resolve() == Path('@@AGENTSWE_EDITING_CONTROL@@/builder_broker_xhigh.py'):
        sys.path.insert(0, '@@AGENTSWE_EDITING_CONTROL@@')
        import builder_broker_runtime
        return builder_broker_runtime.start_builder_broker(name=name, credential=credential,
            image=IMAGE, port=value_port, cidfile=cidfile)
    if script.resolve() == ROOT / 'evaluator/broker/lower_responses_broker.py':
        if effort != LOWER_EFFORT:
            raise ValueError('task-owned lower broker is locked to medium')
        from evaluator.broker.lower_broker_runtime import start_lower_broker
        return start_lower_broker(name=name, credential=credential, image=IMAGE,
            port=value_port, cidfile=cidfile)
    if script.resolve() == JUDGE_BROKER_SCRIPT:
        if effort != BUILDER_EFFORT:
            raise ValueError("Result judge requires xhigh")
        return _judge_runtime().start_judge_broker(name=name, credential=credential, image=IMAGE,
            port=value_port, cidfile=cidfile)
    cidfile.parent.mkdir(parents=True, exist_ok=True)
    cidfile.unlink(missing_ok=True)
    cmd = [
        "docker", "run", "-d", "--rm", "--name", name, "--network", "host",
        "-v", f"{script}:/broker.py:ro",
        "-v", f"{credential}:/run/secrets/agentswe.env:ro",
        "--cidfile", str(cidfile.resolve()),
        IMAGE, "python3", "/broker.py", "--credential-file", "/run/secrets/agentswe.env",
        "--bind", "0.0.0.0", "--port", str(value_port),
    ]
    if effort == LOWER_EFFORT:
        cmd += ["--max-runtime-calls", "0", "--max-runtime-tokens", "0"]
    subprocess.run(cmd, check=True, stdout=subprocess.DEVNULL)
    for _ in range(60):
        try:
            with urllib.request.urlopen(f"http://127.0.0.1:{value_port}/healthz", timeout=2) as r:
                if r.status == 200:
                    return
        except Exception:
            time.sleep(0.5)
    raise RuntimeError(f"broker {name} did not become healthy")


def stats(endpoint: str) -> dict[str, Any]:
    base = endpoint.split("/v1/", 1)[0].rstrip("/") + "/stats"
    req = urllib.request.Request(base, headers={"Authorization": "Bearer stats-only-placeholder"})
    with urllib.request.urlopen(req, timeout=10) as r:
        return read_json_from_bytes(r.read())


def read_json_from_bytes(raw: bytes) -> dict[str, Any]:
    value = json.loads(raw)
    return value if isinstance(value, dict) else {}


def public_round_complete(record: dict[str, Any], expected: tuple[str, ...] = ("dev_001", "dev_002")) -> bool:
    dev = record.get("dev")
    if not isinstance(dev, dict) or set(dev) != set(expected):
        return False
    for entry in dev.values():
        if not isinstance(entry, dict) or not Path(str(entry.get("result", ""))).is_file():
            return False
    return True


SUBMIT_WAITING_INSTRUCTION = """

## Waiting for an evaluation, and recovering a lost response

A submission runs the public dev cases and can take far longer than one shell
tool call, so `submit_dev_candidate` hands the submit to a detached helper and
returns early instead of holding one long command. If it prints status 202 with
state submission_in_progress, run `submit_dev_candidate --await-only` again, as
many times as needed, until it prints the real response. Never wrap the command
in `nohup`, never background or `disown` it, and never kill it.

At any time, including while an evaluation is still running,
`submit_dev_candidate --status` answers within seconds with every accepted
record, its own full feedback and its exact digest, and
`submit_dev_candidate --feedback` reprints one accepted record's feedback and
digest. If a response is ever lost, recover it with one of those two commands;
do not resubmit an unchanged Candidate.
"""


# A preflight rejection used to reach the Builder as a bare
# {"classification": "candidate_build_failure", "exit_code": 2} with an empty
# stderr_tail, because public_payload() drops `build_result` and everything in
# it -- including the delivery_errors list that is the only statement of what
# is actually wrong.  Lift those fields to the top level of the response, where
# the allow list can carry them, and render one actionable sentence beside them.
PREFLIGHT_HINTS = {
    "delivery_contract": ("Each line above is one unmet delivery-contract condition, "
                          "reported verbatim by the evaluator build."),
    "unsafe_or_empty_patch": ("solution.patch carried no usable `+++ b/<path>` header, so the "
                              "preflight could read no changed path from it at all; regenerate "
                              "the patch from the worktree with `git diff`."),
    "patch_apply": ("solution.patch did not apply onto the pristine source with `git apply`; "
                    "regenerate it against the unmodified supplied repository."),
}
PREFLIGHT_DELIVERY_RULE = (
    "Fix the delivery in /workspace/submission and run validate_dev_candidate again: "
    "solution.patch must change aider/worktree_plan_adapter.py and must also add or change "
    "at least one source-adjacent Python test file whose path starts with tests/ and ends "
    "in .py, every changed path must stay inside aider/ or tests/, and edit_report.json "
    "changed_paths must list exactly the paths the patch touches.")


def preflight_failure_detail(build_result: dict[str, Any]) -> dict[str, Any]:
    """The evaluator's own build verdict, verbatim, in Builder-visible fields."""
    error = build_result.get("error")
    delivery_errors = [str(value) for value in (build_result.get("delivery_errors") or [])]
    head = "preflight rejected this delivery"
    if error:
        head += " (build_result.error=" + str(error) + ")"
    lines = [head + (":" if delivery_errors else ".")]
    lines.extend("  - " + value for value in delivery_errors)
    hint = PREFLIGHT_HINTS.get(str(error))
    if hint:
        lines.append(hint)
    lines.append(PREFLIGHT_DELIVERY_RULE)
    return {"build_error": error, "delivery_errors": delivery_errors,
            "preflight_message": "\n".join(lines)}


def start_builder_server(controller: "BuilderLifecycle") -> None:
    class Handler(socketserver.StreamRequestHandler):
        def handle(self) -> None:
            try:
                request = json.loads(self.rfile.readline(1 << 20))
                if request.get("token") != controller.token:
                    code, payload = 401, {"error": "unauthorized"}
                elif request.get("action") == "validate":
                    code, payload = controller.validate()
                elif request.get("action") == "submit":
                    code, payload = controller.submit(request.get("feedback_digest"))
                elif request.get("action") == "status":
                    code, payload = controller.status()
                elif request.get("action") == "feedback":
                    code, payload = controller.feedback()
                else:
                    code, payload = 400, {"error": "unknown_action"}
            except Exception as exc:
                code, payload = 400, {"error": f"{type(exc).__name__}: {exc}"}
            delivered = public_payload(payload)
            try:
                self.wfile.write(json.dumps({"status": code, "payload": delivered}, ensure_ascii=False).encode() + b"\n")
                self.wfile.flush()
            except OSError as exc:
                # The Builder's helper was killed, or its shell tool timed out,
                # while the evaluator was still answering. Nothing arrived, so
                # nothing is receipted: record the failed write and keep the
                # ready response deliverable by a later --status or --feedback
                # connection.
                if code == 200:
                    controller.record_feedback_write_failure(delivered, exc)
                return
            if code == 200:
                # record_feedback_delivery itself decides what this write
                # actually carried, per record and by digest.
                controller.record_feedback_delivery(delivered)

    class Server(socketserver.ThreadingUnixStreamServer):
        daemon_threads = True

    controller.socket_path.unlink(missing_ok=True)
    controller.server = Server(str(controller.socket_path), Handler)
    threading.Thread(target=controller.server.serve_forever, daemon=True).start()


class BuilderLifecycle:
    """Expose only submit/status to one Builder process and attest the session."""

    def __init__(self, *, run_dir: Path, workspace: Path, lower_broker: str, source: Path,
                 image: str, pilot_not_formal: bool = False, max_dev_rounds: int = 10, readiness_profile=None, current_binding=None):
        self.run_dir, self.workspace = run_dir, workspace
        self.controller = __import__("harbor.agentloop_controller", fromlist=["Controller"]).Controller(
            run_dir / "lifecycle", source, lower_broker, True,
            dependency_overlay=None, image=image,
            public_cases=("dev_001",) if readiness_profile else ("dev_001", "dev_002"),
            hidden_cases=("test_001",) if pilot_not_formal else tuple(f"test_{number:03d}" for number in range(1, 7)),
            pilot_not_formal=pilot_not_formal,
            max_dev_rounds=max_dev_rounds, readiness_profile=readiness_profile, current_binding=current_binding,
        )
        self.pilot_not_formal = pilot_not_formal
        self.token = hashlib.sha256(f"{run_dir}:{time.time_ns()}".encode()).hexdigest()
        self.session_id: str | None = None
        self.native_observations: list[dict[str, Any]] = []
        self.socket_path = Path(tempfile.gettempdir()) / f"aider-builder-{self.token[:12]}.sock"
        self.server: socketserver.ThreadingUnixStreamServer | None = None
        self.events: list[dict[str, Any]] = []
        self.validation_count = 0
        self.lock = threading.RLock()
        # A feedback is "received" only when a write to the Builder's socket
        # returned. The Builder's shell-tool timeout can kill the helper while
        # the evaluator is still answering, and the response write then raises
        # BrokenPipeError: the round is accepted and its feedback bytes are on
        # disk, but no feedback_delivered event exists and verify_native
        # rejects the whole session. Keep the ready response so a later
        # connection can deliver it for real, and receipt it at that later
        # moment -- never before, and never for a write that did not happen.
        # A dedicated lock: self.lock is held by submit() for the whole
        # evaluation, and a recovery connection must never queue behind it.
        self.feedback_ledger_lock = threading.Lock()
        self.feedback_write_failures: list[dict[str, Any]] = []
        self.undelivered_feedback: dict[int, dict[str, Any]] = {}

    def bind_native(self) -> None:
        observed = observe_thread(self.run_dir, self.native_observations)
        if self.session_id is not None and self.session_id != observed['thread_id']:
            raise RuntimeError('native Builder thread changed')
        self.session_id = observed['thread_id']
        self.controller.builder_session_id = self.session_id
        self.native_observations.append(observed)
        write_json(self.run_dir / 'builder_native_observations.json', self.native_observations)

    @staticmethod
    def _feedback_file_bytes(feedback: dict[str, Any]) -> bytes:
        """Re-encode a visible feedback object exactly as its file was written.

        harbor/product_attempts.py:checkpoint is the only writer of the
        authoritative feedback file and uses json.dump(ensure_ascii=False,
        indent=2) plus a trailing newline, with no sort_keys, so insertion
        order is part of the bytes. harbor/agentloop_controller.py sets
        feedback_digest to file_digest() of exactly that file, so this encoding
        is the one whose sha256 the digest is. public_payload is idempotent --
        the file was already written from public_payload(feedback), every key
        it kept is in PUBLIC_FIELDS and public_text leaves an already-scrubbed
        string alone -- and json.loads preserves the file's key order, so the
        visible copy re-encodes to the same bytes.
        """
        return (json.dumps(feedback, ensure_ascii=False, indent=2) + "\n").encode("utf-8")

    @staticmethod
    def _delivered_records(payload: dict[str, Any]):
        """Per-record views of what one response actually carried.

        A submit response is one record at top level; a status or feedback
        response carries the same per-record dicts under "records". Both are
        built from the same accepted record and the same feedback file.
        """
        records = (payload or {}).get('records')
        if isinstance(records, list) and records:
            # A status or feedback response may also repeat its latest record
            # at the top level for compatibility. The records list is
            # authoritative and already contains it, so the top-level copy must
            # not mint a second receipt for the same round.
            for record in records:
                if isinstance(record, dict) and record.get('submission_number'):
                    yield record
            return
        if isinstance(payload, dict) and payload.get('submission_number'):
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
        digest = record.get('feedback_digest')
        feedback = record.get('feedback')
        if not digest or not isinstance(feedback, dict) or not feedback:
            return False
        try:
            return hashlib.sha256(cls._feedback_file_bytes(feedback)).hexdigest() == digest
        except (TypeError, ValueError):
            return False

    def record_feedback_delivery(self, payload: dict[str, Any]) -> None:
        """Receipt every feedback the socket write that just returned carried.

        Called only after the actual socket write and flush return.
        """
        with self.feedback_ledger_lock:
            existing = [row for row in self.events if row.get('event') == 'feedback_delivered']
            for record in self._delivered_records(payload):
                if not self._carries_feedback(record):
                    continue
                number = record['submission_number']
                digest = record['feedback_digest']
                payload_sha256 = hashlib.sha256(
                    json.dumps(record, sort_keys=True, ensure_ascii=False).encode()).hexdigest()
                self.undelivered_feedback.pop(number, None)
                if any(row.get('candidate_number') == number and row.get('feedback_digest') == digest
                       and row.get('payload_sha256') == payload_sha256 for row in existing):
                    continue
                row = {'event': 'feedback_delivered', 'at': now(),
                    'candidate_number': number,
                    'builder_session_id': self.session_id,
                    'feedback_digest': digest,
                    'evidence': 'socket_write_and_flush_completed',
                    'payload_sha256': payload_sha256}
                self.events.append(row)
                existing.append(row)
            write_json(self.run_dir / 'builder_feedback_deliveries.json', self.events)

    def record_feedback_write_failure(self, payload: dict[str, Any], exc: BaseException) -> None:
        """A ready response the Builder's socket never took.

        This is the opposite of a receipt: it records that delivery did not
        happen, and keeps the response deliverable by a later connection.
        """
        with self.feedback_ledger_lock:
            numbers = []
            receipted = {row.get('candidate_number') for row in self.events
                         if row.get('event') == 'feedback_delivered'}
            for record in self._delivered_records(payload):
                if not self._carries_feedback(record):
                    continue
                number = record['submission_number']
                numbers.append(number)
                if number not in receipted:
                    self.undelivered_feedback[number] = record
            self.feedback_write_failures.append({'at': now(), 'error': f"{type(exc).__name__}: {exc}",
                'candidate_numbers': numbers, 'builder_session_id': self.session_id})
            write_json(self.run_dir / 'builder_feedback_write_failures.json', {
                'schema_version': 'aider-feedback-delivery-ledger/v1',
                'write_failures': self.feedback_write_failures,
                'undelivered_candidate_numbers': sorted(self.undelivered_feedback)})

    def native_attestation(self, *, allow_interrupted: bool = False) -> dict[str, Any]:
        records = []
        product_bound = True
        for record in self.controller.records:
            materialized = Path(record.get("materialized_path") or self.run_dir / 'lifecycle' / f"materialized_{int(record['submission_number']):03d}") / 'aider'
            product_bound &= materialized.is_dir() and digest(materialized) == record.get('native_product_source_digest')
            records.append({**record, 'build': {'candidate_repo_digest': record.get('native_product_source_digest')}})
        proof = verify_native(self.run_dir, records,
            [v for v in self.events if v.get('event') == 'feedback_delivered'], self.native_observations,
            allow_interrupted=allow_interrupted)
        if not product_bound:
            proof['valid'] = False
            proof['errors'].append('materialized Aider source changed after accepted submission')
        return proof

    def _validate_workspace(self, *, trigger: str) -> dict[str, Any]:
        """Run an evaluator-owned build preflight without consuming a round."""
        self.validation_count += 1
        number = len(self.controller.records) + 1
        workspace_digest = digest(self.workspace)
        validation_dir = self.run_dir / "validations" / f"candidate_{number:03d}_attempt_{self.validation_count:03d}"
        validation_dir.mkdir(parents=True, exist_ok=False)
        required = ["solution.patch", "edit_report.json", "run_report.json"]
        missing = [name for name in required if not (self.workspace / name).is_file()]
        if missing:
            result: dict[str, Any] = {
                "schema_version": "agentswe-builder-preflight-v1",
                "classification": "candidate_preflight_failure",
                "ready_for_submission": False,
                "submission_consumed": False,
                "candidate_number": number,
                "workspace_digest": workspace_digest,
                "missing_delivery_files": missing,
                "trigger": trigger,
            }
        else:
            output = validation_dir / "materialized"
            command = [
                "python3", str(ROOT / "evaluator/materialize_candidate.py"),
                "--source", str(self.controller.source),
                "--candidate", str(self.workspace),
                "--output", str(output),
                "--patch", str(self.workspace / "solution.patch"),
            ]
            done = subprocess.run(command, text=True, capture_output=True, check=False)
            build_result = read_json(output / "build_result.json") if (output / "build_result.json").is_file() else {}
            result = {
                "schema_version": "agentswe-builder-preflight-v1",
                "classification": build_result.get("classification", "candidate_preflight_failure"),
                "ready_for_submission": (done.returncode == 0 and build_result.get("classification") == "ready_for_lower") or
                     (build_result.get("classification") == "candidate_build_failure" and build_result.get("error") not in {"delivery_contract", "unsafe_or_empty_patch", "patch_apply"}),
                "submission_consumed": False,
                "candidate_number": number,
                "workspace_digest": workspace_digest,
                "patch_sha256": hashlib.sha256((self.workspace / "solution.patch").read_bytes()).hexdigest(),
                "trigger": trigger,
                "exit_code": done.returncode,
                "build_result": build_result,
                "stderr_tail": done.stderr[-1200:],
            }
            if not result["ready_for_submission"]:
                # materialize_candidate.py reports a Candidate-side rejection on
                # stdout and in build_result.json, so `done.stderr` is empty
                # exactly when the Candidate, not the environment, is at fault.
                # Without this the Builder saw an unexplained exit 2 and an empty
                # stderr_tail, and could only conclude the preflight was broken.
                detail = preflight_failure_detail(build_result)
                result.update(detail)
                if not result["stderr_tail"].strip():
                    result["stderr_tail"] = detail["preflight_message"]
        result_path = validation_dir / "validation_result.json"
        write_json(result_path, result)
        result["validation_result"] = str(result_path)
        self.events.append({
            "event": "candidate_preflight",
            "number": number,
            "session_id": self.session_id,
            "at": now(),
            "workspace_digest": workspace_digest,
            "ready_for_submission": result["ready_for_submission"],
            "validation_result": str(result_path),
            "trigger": trigger,
        })
        return result

    def validate(self) -> tuple[int, dict[str, Any]]:
        with self.lock:
            result = self._validate_workspace(trigger="builder_request")
            return (200 if result["ready_for_submission"] else 422), result

    def submit(self, feedback_digest: str | None = None) -> tuple[int, dict[str, Any]]:
        with self.lock:
            return self._submit_locked(feedback_digest)

    def _submit_locked(self, feedback_digest: str | None = None) -> tuple[int, dict[str, Any]]:
        current_digest = digest(self.workspace)
        duplicate = next((record for record in self.controller.records
                          if record.get('candidate_digest') == current_digest), None)
        if duplicate is not None and getattr(self.controller, "readiness_profile", None):
            return 409, {'error':'readiness does not allow replay'}
        if duplicate is not None:
            path = self.run_dir / 'lifecycle' / f"feedback_candidate_{int(duplicate['submission_number']):03d}.json"
            return 200, {**duplicate, 'duplicate': True, 'round_consumed': False,
                'builder_session_id': self.session_id,
                'feedback': read_json(path) if path.is_file() else None,
                'feedback_digest': hashlib.sha256(path.read_bytes()).hexdigest() if path.is_file() else None}
        self.bind_native()
        number = len(self.controller.records) + 1
        feedback_path = self.run_dir / "lifecycle" / f"feedback_candidate_{number-1:03d}.json"
        expected_feedback = hashlib.sha256(feedback_path.read_bytes()).hexdigest() if feedback_path.is_file() else None
        if number == 1 and feedback_digest is not None:
            return 409, {"error": "Candidate 1 cannot acknowledge feedback"}
        if number > 1 and feedback_digest != expected_feedback:
            return 409, {"error": "every revision must acknowledge exact evaluator feedback digest",
                         "expected_feedback_digest": expected_feedback}
        if getattr(self.controller, "readiness_profile", None):
            from harbor.readiness_contract import validate_delivery
            validate_delivery(self.controller, self.workspace, feedback_digest)
        preflight = self._validate_workspace(trigger="submission_gate")
        if getattr(self.controller, "readiness_profile", None) and preflight.get('exit_code') != 0:
            return 422, preflight
        if not preflight["ready_for_submission"]:
            self.events.append({
                "event": "submission_rejected_preflight",
                "number": number,
                "session_id": self.session_id,
                "at": now(),
                "workspace_digest": preflight["workspace_digest"],
                "validation_result": preflight["validation_result"],
                "submission_consumed": False,
            })
            return 422, preflight
        before = digest(self.workspace)
        self.events.append({"event": "submission_started", "number": number, "session_id": self.session_id, "at": now(), "workspace_digest": before,
                            "feedback_digest_ack": feedback_digest})
        result = self.controller.submit(self.workspace, feedback_digest_ack=feedback_digest)
        after = digest(self.workspace)
        self.events.append({
            "event": "submission_finished", "number": number, "session_id": self.session_id, "at": now(),
            "workspace_digest": after, "feedback_available": number == 1 and bool(self.controller.feedback),
            "result_state": result.get("state"),
            "feedback_digest_ack": feedback_digest,
        })
        if result.get("duplicate"):
            path = Path(result.get("feedback_path") or self.run_dir / "lifecycle" / "missing_feedback")
            return (200 if result.get("accepted") else 409), {**result,
                "builder_session_id": self.session_id,
                "feedback": read_json(path) if path.is_file() else None,
                "feedback_digest": hashlib.sha256(path.read_bytes()).hexdigest() if path.is_file() else None}
        result["builder_session_id"] = self.session_id
        if result.get('accepted') is True:
            materialized = Path(result.get('materialized_path') or self.run_dir / 'lifecycle' / f'materialized_{int(result.get("submission_number", number)):03d}') / 'aider'
            if not materialized.is_dir():
                raise RuntimeError('accepted Candidate lacks the materialized Aider product')
            result['native_product_source_digest'] = digest(materialized)
            if getattr(self.controller, "readiness_profile", None):
                result['readiness_materialized_digest'] = digest(materialized.parent)
            self.controller._write_lifecycle()
        result["preflight"] = preflight
        result["feedback"] = self.controller.feedback
        feedback_path = self.run_dir / "lifecycle" / f"feedback_candidate_{number:03d}.json"
        if feedback_path.is_file():
            result["feedback_digest"] = hashlib.sha256(feedback_path.read_bytes()).hexdigest()
        if result.get("accepted") is not True:
            return 409, result
        return 200, result

    def _record_feedback_path(self, record: dict[str, Any]) -> Path:
        path = Path(str(record.get("feedback_path") or ""))
        if path.is_file():
            return path
        number = int(record.get("submission_number") or 0)
        return self.run_dir / "lifecycle" / f"feedback_candidate_{number:03d}.json"

    def _with_feedback(self, record: dict[str, Any]) -> dict[str, Any]:
        """Attach one accepted record's own authoritative feedback and digest.

        Only the latest feedback was published, and without its digest, so a
        status response could never carry the bytes whose digest it names.
        Reading each record's own file makes every accepted round recoverable
        and makes a receipt minted from this response provable. The digest is
        the file's sha256, exactly the value the record carries and the next
        submission's feedback_digest_ack is checked against.
        """
        path = self._record_feedback_path(record)
        if not path.is_file():
            return record
        return {**record, "feedback": read_json(path),
                "feedback_digest": hashlib.sha256(path.read_bytes()).hexdigest()}

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
                     "records": [self._with_feedback(chosen)],
                     "frozen": self.controller.frozen}

    def status(self) -> tuple[int, dict[str, Any]]:
        if getattr(self.controller, "readiness_profile", None):
            self.bind_native()
        records = [self._with_feedback(record) for record in self.controller.records]
        payload = {
            "builder_session_id": self.session_id,
            "records": records,
            "feedback": self.controller.feedback,
            "frozen": self.controller.frozen,
        }
        # The top-level feedback has always been the latest record's; publish
        # its digest beside it so a response that carries the bytes also names
        # the value they hash to.
        if records and records[-1].get("feedback_digest"):
            payload["feedback"] = records[-1]["feedback"]
            payload["feedback_digest"] = records[-1]["feedback_digest"]
        return 200, payload

    def close(self) -> None:
        if self.server:
            self.server.shutdown(); self.server.server_close()
            self.server = None
        self.socket_path.unlink(missing_ok=True)


def public_package(run_dir: Path) -> Path:
    public = run_dir / "builder_public_package"
    subprocess.run([
        "python3", str(ROOT / "harbor/stage_public_package.py"),
        "--output", str(public),
    ], check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    return public


def restrict_public_package_to_pilot(public: Path) -> None:
    """Readiness smoke retains the complete two-case public interface."""
    if not all((public / "dev_cases" / case).is_dir() for case in ("dev_001", "dev_002")):
        raise ValueError("pilot public package requires both dev cases")


def builder_config(
    run_dir: Path,
    public: Path,
    workspace: Path,
    controller: BuilderLifecycle,
    provider_config: Path,
    pilot_not_formal: bool = False,
) -> Path:
    task = run_dir / "builder_task"
    worktree = run_dir / "builder_workspace/worktree"
    if worktree.exists():
        shutil.rmtree(worktree)
    shutil.copytree(public / "input/repository", worktree, symlinks=True)
    _initialize_builder_worktree(worktree)
    (task / "environment").mkdir(parents=True)
    (task / "tests").mkdir(parents=True)
    (task / "task.toml").write_text(
        f'''schema_version = "1.4"
[task]
name = "local/aider-agentloop-builder"
version = "1.0.0"
description = "Aider Edit Builder up-to-ten-round feedback lifecycle"
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
docker_image = "{IMAGE}"
network_mode = "public"
build_timeout_sec = 900.0
cpus = 8
memory_mb = 16384
storage_mb = 32768
workdir = "/workspace"
[environment.healthcheck]
command = "python3 /usr/local/bin/builder_resource_gate.py"
timeout_sec = 10
retries = 1
interval_sec = 1
''', encoding="utf-8")
    instruction = f"""# Aider Edit Builder: feedback-driven iterative delivery

You are the single upper Builder session for this benchmark. Read the four
files under /builder-package/input, the complete public dev cases under
/builder-package/dev_cases, and the supplied repository. Do not inspect hidden
cases, evaluator source, credentials, prior runs, or Candidate snapshots.

Work in /workspace/submission and maintain exactly three top-level delivery
files there: solution.patch, edit_report.json, and run_report.json. Submit a
Candidate by running submit_dev_candidate --wait. The evaluator executes both
public dev cases against the real patched Aider and returns authoritative
structured feedback after every accepted, distinct digest.

Before each submission, run `validate_dev_candidate`. This evaluator-owned
preflight checks the delivery contract, patch application and Python syntax
without consuming either Candidate round. That delivery contract requires
`solution.patch` to change `aider/worktree_plan_adapter.py` and to also add or
change at least one source-adjacent Python test file under `tests/` (a changed
path starting with `tests/` and ending in `.py`), with every changed path inside
`aider/` or `tests/` and `edit_report.json` `changed_paths` listing exactly the
paths the patch touches. If it fails, fix the delivery and validate again.
`submit_dev_candidate` enforces the same preflight and rejects an invalid
delivery without consuming a Candidate round.
Do not call either wrapper with `--help`; those wrappers are live evaluator
actions, not documentation commands. Once `validate_dev_candidate` returns
`ready_for_submission: true`, your very next action must be
`submit_dev_candidate --wait` (for Candidate 1) or the exact
`submit_dev_candidate --feedback-digest <digest> --wait` (for a revision).
Do not pause for another inspection or issue another preflight after a ready
response.

The evaluator-only Python environment described in the resources file is not
mounted into this Builder container. Do not spend the delivery budget trying
to recreate it: do not install packages, create import shims, or repeatedly
rerun a public fixture solely because an optional local dependency is absent.
Use the supplied source and a bounded, focused test for local validation; the
evaluator performs the real public execution after submission. Keep any local
fixture smoke bounded to one final check, then prepare and submit the delivery.

The top-level `run_report.json` must include these exact zero-valued provider
count fields alongside `schema_version`, `status`, `commands`,
`duration_seconds`, and `errors`: `deepseek`, `gateway`, `gateway_image`, `serper`,
and `web_retrieval`. Do not put those five counters inside a nested
`tool_counts` object; nested counts do not satisfy the delivery contract.

Continue this same uninterrupted Builder session for useful feedback-driven
revisions, up to {controller.controller.max_dev_rounds} accepted distinct Candidate digests. A duplicate digest is
idempotent and does not consume a round. A dev mean above 60 is feedback only;
it does not freeze the run. You may stop early after the latest structurally
valid accepted snapshot. Hidden cases are unavailable and run only after the
evaluator freezes that snapshot.
"""
    if pilot_not_formal:
        instruction = """# Aider Edit Builder: non-formal readiness pilot

You are the one uninterrupted upper Builder session. Work directly in the
already writable `/workspace/worktree`; do not create another worktree, spawn
or delegate to subagents, wait for another agent, or use web search. Read the
four input files and both `/builder-package/dev_cases/dev_001` and
`/builder-package/dev_cases/dev_002`. Never inspect
hidden cases, evaluator source, credentials, prior runs, or Candidate snapshots.

Maintain exactly `solution.patch`, `edit_report.json`, and `run_report.json` in
`/workspace/submission`. Implement a focused Candidate 1, generate the patch
from the worktree diff, write both JSON reports, run `validate_dev_candidate`,
and fix validation failures before `submit_dev_candidate --wait`. Do not call
either wrapper with `--help`. When validation returns
`ready_for_submission: true`, immediately run `submit_dev_candidate --wait`;
do not perform another inspection or validation first. Do not call submit while
the delivery is empty. The evaluator executes dev_001 and dev_002 for each
accepted Candidate and prints authoritative feedback plus its digest. Consume
that feedback in this same session for useful revisions, up to five distinct
accepted Candidate digests. Each revision must acknowledge the exact prior
feedback digest using `submit_dev_candidate --feedback-digest <exact-digest> --wait`.
A duplicate digest is idempotent. Dev mean above 60 is feedback only, and does
not freeze the run. You may end after the latest accepted snapshot; the evaluator
freezes it on Builder exit or max_dev_rounds. Do not run hidden tests yourself. The
evaluator alone executes exactly one test_001. This pilot cannot publish a
formal six-hidden Result or Code score. The delivery preflight requires at
least one focused source-adjacent Python test in the same patch as the adapter;
add such a test under `tests/` and exercise it locally before submitting.

The evaluator-only Python environment described in the resources file is not
mounted into this Builder container. Do not spend the delivery budget trying
to recreate it: do not install packages, create import shims, or repeatedly
rerun a public fixture solely because an optional local dependency is absent.
Use the supplied source and a bounded, focused test for local validation; the
evaluator performs the real public execution after submission. Keep any local
fixture smoke bounded to one final check, then prepare and submit the delivery.

The top-level `run_report.json` must include these exact zero-valued provider
count fields alongside `schema_version`, `status`, `commands`,
`duration_seconds`, and `errors`: `deepseek`, `gateway`, `gateway_image`, `serper`,
and `web_retrieval`. Do not put those five counters inside a nested
`tool_counts` object; nested counts do not satisfy the delivery contract.
"""
    if getattr(controller.controller, 'readiness_profile', None):
        instruction = """# Aider Edit readiness
Read /builder-package/input and dev_001. Edit /workspace/worktree and deliver exactly solution.patch, edit_report.json, run_report.json in /workspace/submission. Preserve the delivery schema and the five zero provider counters deepseek, gateway, gateway_image, serper, web_retrieval.
Complete exactly TWO valid dev_001 rounds in this one uninterrupted native Builder turn. Use submit_dev_candidate --status to obtain builder_session_id. Include builder_session_id, submission_number, revision_of_candidate_digest, feedback_digest in run_report.json; round 1 has explicit null parent digest and feedback digest. Submit with submit_dev_candidate --wait. After reading first feedback, revise product source meaningfully, set edit_report.json feedback_response, and submit round 2 with --feedback-digest equal to the exact first feedback SHA256. Set round 2 parent digest to round 1 candidate_digest. Duplicate delivery or product is rejected. Functional score is no gate. Include at least one focused source-adjacent Python test in the same patch as the adapter, and run that test locally before submitting each valid round. Exit after both rounds finish. The evaluator freezes round 2 only after your exit, executes test_001, and runs independent non-formal Result and Code smokes.
"""
    (task / "instruction.md").write_text(instruction + SUBMIT_WAITING_INSTRUCTION, encoding="utf-8")
    submit = task / "environment/submit_dev_candidate"
    submit.write_text("""#!/usr/bin/env python3
\"\"\"Builder-side controller client.

A dev evaluation runs the public cases and takes far longer than one shell tool
call, so the submit runs in a detached, fully daemonised holder. The foreground
process only polls a local response file in short bounded waits: a shell-tool
timeout can kill the poller without cutting the evaluator response off. Nothing
here needs nohup, backgrounding or kill.
\"\"\"
import argparse, json, os, socket, sys, time
from pathlib import Path

TMP = Path(os.environ.get("TMPDIR") or "/tmp")
RESPONSE = TMP / "agentswe-submit-response.json"
HOLDER = TMP / "agentswe-submit-holder.pid"
VALIDATE_RESPONSE = TMP / "agentswe-validate-response.json"
VALIDATE_HOLDER = TMP / "agentswe-validate-holder.pid"

p = argparse.ArgumentParser(prog=os.path.basename(sys.argv[0]), add_help=False)
p.add_argument("--help", action="help")
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
    print(json.dumps(result, ensure_ascii=False))
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
if a.validate or os.path.basename(sys.argv[0]) == "validate_dev_candidate":
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
                      "validate_dev_candidate --await-only again (or "
                      "submit_dev_candidate --validate --await-only) to keep waiting. "
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
        ensure_ascii=False))
    raise SystemExit(3)
finish(result)
""", encoding="utf-8")
    submit.chmod(0o755)
    resource_gate = task / 'environment/builder_resource_gate.py'
    shutil.copy2(ROOT / 'harbor/builder_resource_gate.py', resource_gate)
    (task / "tests/test.sh").write_text("printf '0\\n' > /logs/verifier/reward.txt\nexit 0\n", encoding="utf-8")
    (task / "tests/test.sh").chmod(0o755)
    write_json(task / "environment/docker-compose.yaml", {"services": {"main": {
        "cpu_quota": 800000, "cpu_period": 100000, "volumes": [
        {"type": "bind", "source": str(resource_gate), "target": "/usr/local/bin/builder_resource_gate.py", "read_only": True},
        {"type": "bind", "source": str(public), "target": "/builder-package", "read_only": True},
        {"type": "bind", "source": str(worktree), "target": "/workspace/worktree"},
        {"type": "bind", "source": str(workspace), "target": "/workspace/submission"},
        {"type": "bind", "source": str(controller.socket_path), "target": "/run/aider-builder.sock", "read_only": True},
        {"type": "bind", "source": str(submit), "target": "/usr/local/bin/submit_dev_candidate", "read_only": True},
        {"type": "bind", "source": str(submit), "target": "/usr/local/bin/validate_dev_candidate", "read_only": True},
        {"type": "bind", "source": str(provider_config), "target": str(provider_config), "read_only": True},
    ], "environment": {
        "AGENTSWE_DEV_CONTROLLER_SOCKET": "/run/aider-builder.sock",
        "AGENTSWE_DEV_CONTROLLER_TOKEN": controller.token,
        "AGENTSWE_BUILDER_BROKER_TOKEN": "broker-only-placeholder",
        # The Builder operates on an evaluator-created bind mount. Declare
        # that exact workspace safe for Git so ordinary status/diff commands
        # do not fail on container/host ownership differences.
        "GIT_CONFIG_COUNT": "1",
        "GIT_CONFIG_KEY_0": "safe.directory",
        "GIT_CONFIG_VALUE_0": "/workspace/worktree",
    }}}})
    config = run_dir / "builder_job_config.json"
    write_json(config, {
        "job_name": f"aider-agentloop-builder-{run_dir.name}",
        "jobs_dir": str(run_dir / "jobs"), "n_attempts": 1, "n_concurrent_trials": 1,
        "quiet": True, "retry": {"max_retries": 0}, "environment": {"type": "docker", "delete": not bool(getattr(controller.controller, 'readiness_profile', None)),
            "cpu_enforcement_policy": "limit", "memory_enforcement_policy": "limit"},
        "agents": [{"import_path": "agentswe_codex_resume:CodexResume", "model_name": BUILDER_MODEL, "env": {
            "CODEX_HOME": "/tmp/agentswe-codex-home",
            "CODEX_CONFIG_TOML_PATH": str(provider_config),
            "AGENTSWE_BUILDER_BROKER_TOKEN": "broker-only-placeholder",
        }, "kwargs": {"reasoning_effort": BUILDER_EFFORT, "web_search": "disabled"}}],
        "tasks": [{"path": str(task)}],
    })
    return config


def run_builder(harbor: Path, config: Path, run_dir: Path, timeout_seconds: int) -> int:
    with (run_dir / "builder.stdout.log").open("w") as out, (run_dir / "builder.stderr.log").open("w") as err:
        process = subprocess.Popen(
            [str(harbor), "run", "-c", str(config)],
            stdout=out,
            stderr=err,
            start_new_session=True,
        )
        try:
            return process.wait(timeout=timeout_seconds)
        except subprocess.TimeoutExpired:
            err.write(f"Builder wall-clock timeout after {timeout_seconds} seconds\n")
            err.flush()
            os.killpg(process.pid, signal.SIGTERM)
            try:
                process.wait(timeout=15)
            except subprocess.TimeoutExpired:
                os.killpg(process.pid, signal.SIGKILL)
                process.wait()
            return 124
        except KeyboardInterrupt:
            err.write("Builder interrupted by evaluator; terminating Harbor process group\n")
            err.flush()
            os.killpg(process.pid, signal.SIGTERM)
            try:
                process.wait(timeout=15)
            except subprocess.TimeoutExpired:
                os.killpg(process.pid, signal.SIGKILL)
                process.wait()
            return 130


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


def run_configured_builder(harbor: Path, config: Path, run_dir: Path, timeout_seconds: int,
                           *, credential: Path, transport: str = 'direct',
                           base_url: str = 'https://api.deepseek.com/v1',
                           proxy: str = 'http://127.0.0.1:7890') -> int:
    if transport not in {'direct', 'broker'}:
        raise ValueError('unknown Builder transport')
    from harbor.builder_resources import BuilderResourceObserver
    from harbor.builder_direct import execution
    observer = BuilderResourceObserver(run_dir)
    observer.start()
    code = 125
    try:
        if transport == 'broker':
            code = builder_segments_runtime().run_builder_segments(
                run_dir, config, harbor=harbor, observer=observer,
                budget_seconds=timeout_seconds, log_mode='w')
        else:
            with execution(config, run_dir, credential, base_url=base_url, proxy=proxy):
                code = builder_segments_runtime().run_builder_segments(
                    run_dir, config, harbor=harbor, observer=observer,
                    budget_seconds=timeout_seconds, log_mode='w')
    finally:
        resources = observer.finish()
        if (run_dir/'readiness_current_binding.json').is_file():
            cleanup = observer.cleanup_owned(defer_removal=True)
            if not cleanup.get('retained_terminal'):
                code = 125
    return code if resources['valid'] else 125


def save_role_stats(run_dir: Path, label: str, endpoint: str, transport: str) -> None:
    if label in {'builder', 'pilot_builder'} and transport == 'direct':
        from harbor.direct_harbor_builder import native_stats
        write_json(run_dir / 'builder_native_stats.json', native_stats(run_dir))
    else:
        write_json(run_dir / f'{label}_broker_stats.json', stats(endpoint))


def cleanup_builder_containers(run_dir: Path) -> list[str]:
    """Remove compose siblings only when one container mounts this run dir."""
    listed = subprocess.run(
        ["docker", "ps", "-aq"], text=True, capture_output=True, check=False,
    ).stdout.split()
    projects: set[str] = set()
    run_prefix = str(run_dir.resolve())
    for container_id in listed:
        inspected = subprocess.run(
            ["docker", "inspect", container_id], text=True, capture_output=True, check=False,
        )
        if inspected.returncode:
            continue
        try:
            item = json.loads(inspected.stdout)[0]
        except (json.JSONDecodeError, IndexError, TypeError):
            continue
        mounts = item.get("Mounts") if isinstance(item, dict) else []
        if any(str(mount.get("Source", "")).startswith(run_prefix) for mount in mounts or [] if isinstance(mount, dict)):
            labels = (item.get("Config") or {}).get("Labels") or {}
            project = labels.get("com.docker.compose.project")
            if isinstance(project, str) and project:
                projects.add(project)
    removed: list[str] = []
    for project in sorted(projects):
        ids = subprocess.run(
            ["docker", "ps", "-aq", "--filter", f"label=com.docker.compose.project={project}"],
            text=True, capture_output=True, check=False,
        ).stdout.split()
        if ids:
            subprocess.run(["docker", "rm", "-f", *ids], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=False)
            removed.append(project)
    return removed


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


def cleanup_owned_containers(names: dict[str, str], attempted: dict[str, bool], cidfiles: dict[str, Path]) -> dict[str, Any]:
    """Clean only IDs captured by this run and verify absence fail-closed."""
    containers: dict[str, dict[str, Any]] = {}
    for role, name in names.items():
        if not attempted.get(role, False):
            containers[role] = {"name": name, "attempted": False, "skipped": True,
                                "absent_after_cleanup": True, "ownership_proven": False}
            continue
        container_id = read_container_id(cidfiles[role])
        if container_id is None:
            containers[role] = {"name": name, "attempted": True, "ownership_proven": False,
                                "absent_after_cleanup": False,
                                "cleanup_error": "Docker startup was attempted but no current-run container ID was captured"}
            continue
        in_progress = False
        try:
            removed = subprocess.run(
                ["docker", "rm", "-f", container_id], text=True, capture_output=True, check=False,
            )
            remove_exit_code = removed.returncode
            remove_error = removed.stderr[-500:] if removed.returncode else None
            in_progress = removal_in_progress(removed)
            if in_progress:
                await_daemon_removal(container_id)
        except OSError as exc:
            remove_exit_code = None
            remove_error = f"{type(exc).__name__}: {exc}"
        try:
            inspected = subprocess.run(
                ["docker", "inspect", container_id], text=True, capture_output=True, check=False,
            )
            inspect_exit_code = inspected.returncode
            inspect_stderr = (inspected.stderr or "")[-500:]
            absent = inspected.returncode != 0 and any(
                marker in inspect_stderr.lower()
                for marker in ("no such object", "no such container")
            )
            inspect_error = inspect_stderr if not absent else None
        except OSError as exc:
            inspect_exit_code = None
            absent = False
            inspect_error = f"{type(exc).__name__}: {exc}"
        containers[role] = {
            "name": name, "attempted": True, "remove_exit_code": remove_exit_code,
            "remove_error": remove_error, "inspect_exit_code": inspect_exit_code,
            "absent_after_cleanup": absent, "inspect_error": inspect_error,
            **({"removal_in_progress_at_rm": True} if in_progress else {}),
        }
    return {
        "containers": containers,
        "all_attempted_absent": all(
            not item.get("attempted") or item.get("absent_after_cleanup") is True
            for item in containers.values()
        ),
    }


def _static_protocol_lock() -> dict[str, Any]:
    """Return the checked-in lifecycle lock without runtime endpoints.

    Static mode must not manufacture ports or broker URLs.  The checked-in
    lock is the source of truth for the immutable protocol; this copy only
    adds evaluator-owned audit metadata to the run directory.
    """
    lock = read_json(ROOT / "protocol_lock.json")
    lock["execution"] = {
        "mode": "static",
        "network_calls": 0,
        "docker_started": False,
        "formal_execution_started": False,
        "formal_result_claimed": False,
        "code_score_claimed": False,
    }
    return lock


def static_audit() -> dict[str, Any]:
    """Audit the one-stop contract using local files only.

    Keep this deliberately boring: no subprocesses, sockets, Docker probes,
    HTTP requests, credential reads, or case execution are allowed here.
    """
    errors: list[str] = []
    required_files = [
        "protocol_lock.json",
        "evaluator/agentloop_cases.json",
        "evaluator/materialize_candidate.py",
        "evaluator/formal_finalize.py",
        "evaluator/harness/run_lower_agent_case.py",
        "harbor/agentloop_controller.py",
        "harbor/stage_public_package.py",
    ]
    missing = [relative for relative in required_files if not (ROOT / relative).is_file()]
    if missing:
        errors.append(f"missing required files: {', '.join(missing)}")

    try:
        lock = read_json(ROOT / "protocol_lock.json")
    except (OSError, json.JSONDecodeError) as exc:
        lock = {}
        errors.append(f"protocol_lock.json is not valid JSON: {type(exc).__name__}")
    try:
        cases = read_json(ROOT / "evaluator/agentloop_cases.json")
    except (OSError, json.JSONDecodeError) as exc:
        cases = {}
        errors.append(f"agentloop_cases.json is not valid JSON: {type(exc).__name__}")

    expected_public = ["dev_001", "dev_002"]
    expected_hidden = [f"test_{number:03d}" for number in range(1, 7)]
    if lock.get("benchmark_id") != "12-edit-aider-worktree-transaction-agentloop-v1":
        errors.append("protocol benchmark_id is not locked to this Aider sibling")
    lower = lock.get("lower_agent") if isinstance(lock.get("lower_agent"), dict) else {}
    if lower.get("product") != "Aider":
        errors.append("lower product is not Aider")
    if lower.get("model") != MODEL or lower.get("reasoning_effort") != LOWER_EFFORT:
        errors.append("lower model/effort lock differs from deepseek-flash/medium")
    if lock.get("candidate_credential") != "broker-only-placeholder":
        errors.append("candidate credential is not broker-only-placeholder")
    builder = lock.get("builder") if isinstance(lock.get("builder"), dict) else {}
    if builder.get("single_session") is not True or builder.get("feedback_revisions") not in {1, "up_to_max_dev_rounds"}:
        errors.append("Builder single-session/feedback-revision lock is incomplete")
    inventory = lock.get("inventory") if isinstance(lock.get("inventory"), dict) else {}
    if inventory.get("public_dev") != expected_public or inventory.get("hidden") != expected_hidden:
        errors.append("protocol inventory is not exactly 2 public plus 6 hidden cases")
    lifecycle = lock.get("lifecycle") if isinstance(lock.get("lifecycle"), dict) else {}
    if lifecycle.get("accepted_submissions") != "up_to_max_dev_rounds" or lifecycle.get("accepted_range") != [1, 5] or lifecycle.get("fresh_feedback_after_each") is not True or lifecycle.get("hidden_only_after_freeze") is not True or lifecycle.get("freeze_latest_accepted") is not True or lifecycle.get("dev_passed_is_automatic_freeze") is not False:
        errors.append("accepted-submission/latest-freeze lifecycle gates are incomplete")

    public = cases.get("public_dev") if isinstance(cases.get("public_dev"), dict) else {}
    hidden = cases.get("hidden") if isinstance(cases.get("hidden"), dict) else {}
    if list(public) != expected_public:
        errors.append("public case inventory is not exactly dev_001/dev_002")
    if list(hidden) != expected_hidden:
        errors.append("hidden case inventory is not exactly test_001..test_006")

    source = (ROOT / "harbor/formal_one_stop.py").read_text(encoding="utf-8")
    required_markers = {
        "explicit formal gate": "--run-formal",
        "offline self-test gate": "--self-test",
        "Builder execution": "run_builder(",
        "broker execution": "start_broker(",
        "hidden execution": "run_hidden()",
        "formal finalizer": "formal_finalize.py",
        "same-session lifecycle": "single_continuous_session",
    }
    for label, marker in required_markers.items():
        if marker not in source:
            errors.append(f"formal source is missing {label} marker: {marker}")

    return {
        "schema_version": "agentswe-aider-static-audit-v1",
        "status": "static_audit_pass" if not errors else "static_audit_failed",
        "checked_at": now(),
        "benchmark_id": lock.get("benchmark_id"),
        "required_files": required_files,
        "public_cases": expected_public,
        "hidden_cases": expected_hidden,
        "errors": errors,
        "network_calls": 0,
        "docker_started": False,
        "formal_execution_started": False,
        "formal_result_claimed": False,
        "code_score_claimed": False,
    }


def run_static(run_dir: Path) -> int:
    """Write a provider-free audit receipt and return without execution."""
    run_dir.mkdir(parents=True, exist_ok=True)
    expected_hidden = [f"test_{number:03d}" for number in range(1, 7)]
    audit = static_audit()
    write_json(run_dir / "protocol_lock.json", _static_protocol_lock())
    summary = {
        "schema_version": "agentswe-aider-formal-summary-v1",
        "status": "static_audit_only" if audit["status"] == "static_audit_pass" else "static_audit_failed",
        "execution_mode": "static",
        "network_calls": 0,
        "docker_started": False,
        "formal_execution_started": False,
        "formal_result_claimed": False,
        "code_score_claimed": False,
        "formal_result": "N/A",
        "code_score": "N/A",
        "audit": audit,
    }
    write_json(run_dir / "summary.json", summary)
    write_json(run_dir / "one_stop_summary.json", {
        "status": summary["status"], "dev_lifecycle": [], "freeze": None,
        "hidden": {"inventory": expected_hidden, "summary": "not executed in static mode"},
        "result_judge_contracts": None, "code_contract": None, "combined_score": None,
        "cleanup_attestation": None, "network_calls": 0,
    })
    print(json.dumps(summary, indent=2, ensure_ascii=False))
    return 0 if audit["status"] == "static_audit_pass" else 2


def self_test() -> int:
    """Exercise the static path and assert that it cannot enter formal mode."""
    with tempfile.TemporaryDirectory(prefix="aider-formal-one-stop-self-test-") as temporary:
        run_dir = Path(temporary) / "static"
        code = run_static(run_dir)
        summary = read_json(run_dir / "summary.json")
        lock = read_json(run_dir / "protocol_lock.json")
        checks = [
            code == 0,
            summary.get("execution_mode") == "static",
            summary.get("network_calls") == 0,
            summary.get("docker_started") is False,
            summary.get("formal_execution_started") is False,
            summary.get("formal_result_claimed") is False,
            lock.get("execution", {}).get("formal_execution_started") is False,
        ]
    passed = all(checks)
    print(f"SELF_TEST={'PASS' if passed else 'FAIL'}")
    print("network_calls=0")
    print("docker_started=false")
    print("formal_execution_started=false")
    return 0 if passed else 1


def _run_formal(args: argparse.Namespace, run_dir: Path) -> int:
    """Execute the pre-existing real Builder/public/hidden lifecycle.

    This function is reachable only from ``main`` when ``--run-formal`` is
    explicit.  Keep all expensive side effects in this function so importing
    the module, asking for help, self-testing, and the default audit remain
    provider-free.
    """
    run_dir = run_dir.resolve()
    if run_dir.exists():
        if run_dir.is_symlink() or not run_dir.is_dir():
            raise RuntimeError(f"refusing to use non-directory formal run path: {run_dir}")
        if any(run_dir.iterdir()):
            raise RuntimeError(f"refusing to overwrite non-empty formal run directory: {run_dir}")
    else:
        run_dir.mkdir(parents=True)
    lower_port, builder_port = port(), port()
    judge_port = port()
    lower_endpoint = f"http://127.0.0.1:{lower_port}/v1/responses"
    builder_endpoint = f"http://172.17.0.1:{builder_port}/v1/responses"
    builder_stats_endpoint = f"http://127.0.0.1:{builder_port}/v1/responses"
    judge_endpoint = f"http://127.0.0.1:{judge_port}/v1/responses"
    lower_name = "aider-formal-lower-" + hashlib.sha256(str(run_dir).encode()).hexdigest()[:10]
    builder_name = "aider-formal-builder-" + hashlib.sha256(str(run_dir).encode()).hexdigest()[:10]
    judge_name = "aider-formal-result-judge-" + hashlib.sha256(str(run_dir).encode()).hexdigest()[:10]
    lower_script = ROOT / "evaluator/broker/lower_responses_broker.py"
    builder_script = Path("@@AGENTSWE_EDITING_CONTROL@@/builder_broker_xhigh.py")
    names = {"lower": lower_name, "builder": builder_name, "result_judge": judge_name}
    cidfiles = {role: run_dir / "brokers" / f"{role}.cid" for role in names}
    attempted = {role: False for role in names}
    cleanup_done = False
    controller: BuilderLifecycle | None = None
    try:
        attempted["lower"] = True
        start_broker(name=lower_name, script=lower_script, credential=args.credential_file.resolve(), value_port=lower_port, effort=LOWER_EFFORT, cidfile=cidfiles["lower"])
        if args.builder_transport == 'broker':
            attempted["builder"] = True
            start_broker(name=builder_name, script=builder_script, credential=args.credential_file.resolve(), value_port=builder_port, effort=BUILDER_EFFORT, cidfile=cidfiles["builder"])
        attempted["result_judge"] = True
        start_broker(name=judge_name, script=JUDGE_BROKER_SCRIPT,
                     credential=args.credential_file.resolve(), value_port=judge_port, effort=BUILDER_EFFORT, cidfile=cidfiles["result_judge"])
        (run_dir / "builder_broker_provider.toml").write_text(
            'model_provider = "aider_builder_broker"\ndisable_response_storage = true\n\n'
            '[model_providers.aider_builder_broker]\nname = "Aider evaluator Builder broker"\n'
            f'base_url = "{builder_endpoint.rsplit("/v1", 1)[0]}/v1"\nenv_key = "AGENTSWE_BUILDER_BROKER_TOKEN"\n'
            'wire_api = "responses"\nrequires_openai_auth = false\n', encoding="utf-8")
        workspace = run_dir / "builder_workspace/submission"; workspace.mkdir(parents=True)
        controller = BuilderLifecycle(
            run_dir=run_dir,
            workspace=workspace,
            lower_broker=lower_endpoint,
            source=ROOT / "input/repository",
            image=args.lower_image,
            max_dev_rounds=args.max_dev_rounds,
        )
        controller.controller.dependency_overlay = args.dependency_overlay.resolve() if args.dependency_overlay else None
        controller.controller.judge_endpoint = judge_endpoint
        public = public_package(run_dir)
        start_builder_server(controller)
        config = builder_config(
            run_dir, public, workspace, controller,
            run_dir / "builder_broker_provider.toml",
        )
        write_json(run_dir / "protocol_lock.json", {
            "builder": {"model": BUILDER_MODEL, "reasoning_effort": BUILDER_EFFORT, "single_continuous_session_required": True,
                "transport": args.builder_transport, "broker_endpoint": builder_endpoint if args.builder_transport == 'broker' else None},
            "lower_agent": {"product": "patched Aider", "model": MODEL, "reasoning_effort": LOWER_EFFORT, "broker_endpoint": lower_endpoint},
            "lifecycle": {"accepted_submissions": "up_to_max_dev_rounds", "accepted_range": [1, 5], "fresh_feedback_after_each": True, "hidden_only_after_freeze": True, "freeze_latest_accepted": True, "dev_passed_is_automatic_freeze": False},
            "candidate_submissions": "up_to_max_dev_rounds", "max_dev_rounds": args.max_dev_rounds,
            "n_concurrent": args.n_concurrent, "dev_passed_is_automatic_freeze": False,
            "public_cases": ["dev_001", "dev_002"], "hidden_cases": [f"test_{i:03d}" for i in range(1, 7)],
            "builder_visibility": {"input": True, "dev_cases": True, "hidden": False, "evaluator": False, "credentials": False},
        })
        code = run_configured_builder(args.harbor.resolve(), config, run_dir, args.builder_timeout,
            credential=args.credential_file.resolve(), transport=args.builder_transport,
            base_url=args.builder_base_url, proxy=args.builder_proxy)
        controller.close()
        if not controller.controller.frozen and controller.controller.records:
            controller.controller.freeze_latest("builder_exit")
        public_dev_complete = bool(controller.controller.records) and all(
            public_round_complete(record) for record in controller.controller.records
        )
        # Survivable max_dev_rounds exit (D2, 2026-09-19).  Once the Builder has
        # spent its whole accepted-round budget the controller has already frozen
        # the latest Candidate with freeze_reason "max_dev_rounds"; nothing then
        # stops the Builder process, so a non-zero exit afterwards is the outer
        # deadline, not missing evidence.  125 stays fatal: it is this evaluator's
        # own resource/cleanup proof failing, never the Builder running long.
        max_rounds_interrupt = bool(
            code not in (0, 125)
            and (controller.controller.frozen or {}).get("freeze_reason") == "max_dev_rounds"
            and len(controller.controller.records) >= args.max_dev_rounds
        )
        native = controller.native_attestation(allow_interrupted=max_rounds_interrupt)
        write_json(run_dir / "builder_session_attestation.json", {
            "schema_version": "aider-builder-session-attestation-v1",
            "builder_session_id": controller.session_id,
            "builder_model": BUILDER_MODEL, "builder_reasoning_effort": BUILDER_EFFORT,
            "same_session": native['valid'], "native_evidence": native, "events": controller.events,
            "candidate_records": controller.controller.records,
            "feedback_consumed_required": True,
            "preflight_required": True,
            "preflight_attempts": controller.validation_count,
            "builder_exit_code": code,
            "builder_timeout_seconds": args.builder_timeout,
            "builder_timed_out": code == 124,
            "max_dev_rounds_interrupt_accepted": max_rounds_interrupt,
            "accepted_rounds": len(controller.controller.records),
            "max_dev_rounds": args.max_dev_rounds,
            "distinct_digests": len({record.get("candidate_digest") for record in controller.controller.records}) == len(controller.controller.records),
            "public_dev_complete": public_dev_complete,
            "dev_passed_is_automatic_freeze": False,
            "public_dev_complete": public_dev_complete,
            "freeze": controller.controller.frozen,
        })
        if (code != 0 and not max_rounds_interrupt) or not native['valid'] or not controller.controller.records or not controller.controller.frozen or not public_dev_complete:
            write_json(run_dir / "summary.json", {"status": "builder_integration_incomplete", "builder_exit_code": code, "builder_timed_out": code == 124, "public_dev_complete": public_dev_complete, "records": controller.controller.records})
            return 2
        public_cleanup = cleanup_owned_containers({"lower": names["lower"]}, {"lower": attempted["lower"]}, {"lower": cidfiles["lower"]})
        write_json(run_dir / "public_broker_cleanup_before_hidden.json", public_cleanup)
        if not public_cleanup.get("all_attempted_absent"):
            raise RuntimeError("public lower broker cleanup unverified")
        attempted["lower"] = False
        hidden_port = port()
        while hidden_port in {lower_port, builder_port, judge_port}:
            hidden_port = port()
        names["hidden"] = "aider-formal-hidden-" + hashlib.sha256(str(run_dir).encode()).hexdigest()[:10]
        cidfiles["hidden"] = run_dir / "brokers/hidden.cid"
        attempted["hidden"] = True
        start_broker(name=names["hidden"], script=lower_script, credential=args.credential_file.resolve(),
                     value_port=hidden_port, effort=LOWER_EFFORT, cidfile=cidfiles["hidden"])
        hidden_endpoint = f"http://127.0.0.1:{hidden_port}/v1/responses"
        initial = stats(hidden_endpoint)
        if int((initial.get("runtime") or {}).get("calls", 0)) != 0:
            raise RuntimeError("formal hidden broker must start at zero calls")
        write_json(run_dir / "hidden_broker_initial.json", {"started_after_freeze": True,
                   "independent_from_public": hidden_endpoint != lower_endpoint, "stats": initial})
        controller.controller.broker = hidden_endpoint
        hidden = controller.controller.run_hidden()
        finalizer_output = run_dir / "formal_aggregation.json"
        finalized = subprocess.run([
            "python3", str(ROOT / "evaluator/formal_finalize.py"),
            "--run-dir", str(run_dir),
            "--credential-file", str(args.credential_file.resolve()),
            "--result-judge-broker-endpoint", judge_endpoint,
        ], text=True, capture_output=True, check=False)
        aggregation = read_json(finalizer_output) if finalizer_output.is_file() else {
            "formal_result_publishable": False,
            "result_axis": "N/A",
            "code_axis": "N/A",
            "reasons": [f"formal finalizer failed with exit {finalized.returncode}"],
        }
        write_json(run_dir / "summary.json", {
            "status": "formal_result_ready" if aggregation.get("formal_result_publishable") else "lifecycle_complete_result_not_publishable",
            "formal_result_claimed": aggregation.get("formal_result_publishable") is True,
            "code_score_claimed": aggregation.get("code_score_publishable") is True,
            "builder_session_id": controller.session_id,
            "records": controller.controller.records,
            "freeze": controller.controller.frozen,
            "hidden": hidden,
            "formal_aggregation": aggregation,
            "finalizer_exit_code": finalized.returncode,
        })
        write_json(run_dir / "one_stop_summary.json", {
            "status": "completed" if aggregation.get("formal_result_publishable") else "formal_result_not_publishable",
            "dev_lifecycle": controller.controller.records,
            "freeze": controller.controller.frozen,
            "hidden": {"inventory": [f"test_{i:03d}" for i in range(1, 7)], "summary": hidden},
            "result_judge_contracts": aggregation.get("result_judge_contracts"),
            "code_contract": aggregation.get("code_contract"),
            "combined_score": None,
            "cleanup_attestation": str(run_dir / "builder_container_cleanup.json"),
        })
        return 0 if aggregation.get("formal_result_publishable") else 2
    except Exception as exc:
        # Without this the formal branch left no summary.json at all on an
        # orchestration failure, so a dead run was indistinguishable from a run
        # that never started.  Mirrors run_pilot's own except handling; gates
        # are untouched, the run still returns 2.
        failure = {
            "status": "orchestration_failed",
            "classification": "evaluator_orchestration_failure",
            "error_type": type(exc).__name__,
            "error_detail": str(exc)[-1200:],
            "traceback_tail": traceback.format_exc()[-4000:],
            "formal_result_claimed": False, "code_score_claimed": False,
            "result_axis": "N/A", "code_axis": "N/A",
        }
        write_json(run_dir / "summary.json", failure)
        write_json(run_dir / "one_stop_summary.json",
                   {**failure, "status": "formal_result_not_publishable"})
        return 2
    finally:
        if not cleanup_done:
            if controller is not None:
                controller.close()
            for label, endpoint in (("lower", lower_endpoint), ("builder", builder_stats_endpoint), ("result_judge", judge_endpoint)):
                try:
                    save_role_stats(run_dir, label, endpoint, args.builder_transport)
                except Exception as exc:
                    write_json(run_dir / f"{label}_broker_stats_error.json", {"error_type": type(exc).__name__, "error": str(exc)[-500:]})
            cleanup = cleanup_owned_containers(names, attempted, cidfiles)
            cleanup["schema_version"] = "agentswe-aider-owned-container-cleanup-v1"
            cleanup["controller_socket_closed"] = controller is None or not controller.socket_path.exists()
            try:
                cleanup["removed_compose_projects"] = cleanup_builder_containers(run_dir)
            except OSError as exc:
                cleanup["removed_compose_projects"] = []
                cleanup["compose_cleanup_error"] = f"{type(exc).__name__}: {exc}"
                cleanup["all_attempted_absent"] = False
            cleanup["completed"] = bool(cleanup["controller_socket_closed"] and cleanup["all_attempted_absent"])
            cleanup["completed_at"] = now()
            write_json(run_dir / "builder_container_cleanup.json", cleanup)
            cleanup_done = True


def pilot_self_test() -> int:
    """Generate the reduced Builder job locally without Docker/provider execution."""
    with tempfile.TemporaryDirectory(prefix="aider-pilot-self-test-") as temporary:
        run_dir = Path(temporary) / "pilot"
        run_dir.mkdir(parents=True)
        workspace = run_dir / "workspace"; workspace.mkdir()
        public = public_package(run_dir)
        restrict_public_package_to_pilot(public)
        lifecycle = BuilderLifecycle(
            run_dir=run_dir, workspace=workspace,
            lower_broker="http://127.0.0.1:1/v1/responses",
            source=ROOT / "input/repository", image="pilot-dry-image",
            pilot_not_formal=True,
        )
        try:
            provider = run_dir / "provider.toml"
            provider.write_text(
                'model_provider = "aider_builder_broker"\ndisable_response_storage = true\n\n'
                '[model_providers.aider_builder_broker]\nname = "pilot dry broker"\n'
                'base_url = "http://builder-broker.invalid/v1"\n'
                'env_key = "AGENTSWE_BUILDER_BROKER_TOKEN"\nwire_api = "responses"\nrequires_openai_auth = false\n',
                encoding="utf-8",
            )
            config = builder_config(run_dir, public, workspace, lifecycle, provider, pilot_not_formal=True)
            instruction = (run_dir / "builder_task/instruction.md").read_text(encoding="utf-8")
            config_text = config.read_text(encoding="utf-8")
            checks = {
                "pilot_inventory": lifecycle.controller.public_cases == ("dev_001", "dev_002")
                and lifecycle.controller.hidden_cases == ("test_001",),
                "builder_lock": BUILDER_MODEL in config_text and BUILDER_EFFORT in config_text,
                "placeholder_only": "broker-only-placeholder" in config_text
                and "OPENAI_API_KEY=" not in config_text and "DEEPSEEK_API_KEY=" not in config_text,
                "both_dev_cases_visible": (public / "dev_cases/dev_001").is_dir()
                and (public / "dev_cases/dev_002").is_dir(),
                "pilot_instruction": "exactly one test_001" in instruction and "formal six-hidden" in instruction,
                "writable_worktree_seeded": (run_dir / "builder_workspace/worktree").is_dir(),
                "no_builder_delegation": "do not create another worktree, spawn" in instruction,
                "submission_failure_is_nonzero": "raise SystemExit(0 if result.get" in
                (run_dir / "builder_task/environment/submit_dev_candidate").read_text(encoding="utf-8"),
            }
        finally:
            lifecycle.close()
    result = {
        "self_test": "PASS" if all(checks.values()) else "FAIL",
        "checks": checks, "pilot_not_formal": True,
        "network_calls": 0, "docker_started": False,
        "formal_execution_started": False, "formal_result_claimed": False,
    }
    print(json.dumps(result, indent=2, ensure_ascii=False))
    return 0 if result["self_test"] == "PASS" else 1


def _pilot_score_aider(run_dir: Path) -> dict[str, Any] | str:
    case_dir = run_dir / "lifecycle/evaluations/hidden/test_001"
    result_path = case_dir / "result.json"
    required = [case_dir / "case_state.json", case_dir / "broker_before.json", case_dir / "broker_after.json", result_path]
    if not all(path.is_file() for path in required):
        return "N/A"
    result = read_json(result_path)
    score_dir = run_dir / "pilot_scoring/test_001"
    answer_path = score_dir / "answer.json"
    write_json(answer_path, result.get("answer") if isinstance(result.get("answer"), dict) else {})
    score_path = score_dir / "score.json"
    completed = subprocess.run([
        sys.executable, str(ROOT / "evaluator/harness/score_agent_case.py"),
        "--answer", str(answer_path), "--state", str(case_dir / "case_state.json"),
        "--broker-before", str(case_dir / "broker_before.json"),
        "--broker-after", str(case_dir / "broker_after.json"), "--output", str(score_path),
    ], text=True, capture_output=True, check=False)
    return read_json(score_path) if completed.returncode == 0 and score_path.is_file() else "N/A"


def run_pilot(args: argparse.Namespace, run_dir: Path) -> int:
    """Run the real 2-dev/feedback/up-to-ten-round/freeze/1-test pilot lifecycle."""
    if getattr(args, 'readiness_profile', None):
        from harbor.readiness_launcher import run
        return run(args, run_dir)
    run_dir = run_dir.resolve()
    if run_dir.exists() and any(run_dir.iterdir()):
        raise RuntimeError(f"refusing to overwrite non-empty pilot run directory: {run_dir}")
    run_dir.mkdir(parents=True, exist_ok=True)
    base_summary = {
        "schema_version": "agentswe-aider-pilot-summary/v1", "mode": "pilot",
        "pilot_not_formal": True, "formal_result_claimed": False,
        "code_score_claimed": False, "result_axis": "N/A", "code_axis": "N/A",
    }
    lower_port, builder_port, hidden_port = port(), port(), port()
    while len({lower_port, builder_port, hidden_port}) != 3:
        lower_port, builder_port, hidden_port = port(), port(), port()
    public_endpoint = f"http://127.0.0.1:{lower_port}/v1/responses"
    hidden_endpoint = f"http://127.0.0.1:{hidden_port}/v1/responses"
    builder_container_endpoint = f"http://172.17.0.1:{builder_port}/v1/responses"
    builder_host_endpoint = f"http://127.0.0.1:{builder_port}/v1/responses"
    suffix = hashlib.sha256(str(run_dir).encode()).hexdigest()[:10]
    names = {"public": f"aider-pilot-public-{suffix}", "builder": f"aider-pilot-builder-{suffix}",
             "hidden": f"aider-pilot-hidden-{suffix}"}
    judge_port = port()
    while judge_port in {lower_port, builder_port, hidden_port}:
        judge_port = port()
    judge_endpoint = f"http://127.0.0.1:{judge_port}/v1/responses"
    names["result_judge"] = f"aider-pilot-judge-{suffix}"
    attempted = {key: False for key in names}
    controller: BuilderLifecycle | None = None
    lower_script = ROOT / "evaluator/broker/lower_responses_broker.py"
    builder_script = Path("@@AGENTSWE_EDITING_CONTROL@@/builder_broker_xhigh.py")
    cidfiles = {key: run_dir / "brokers" / f"{key}.cid" for key in names}
    try:
        attempted["public"] = True
        start_broker(name=names["public"], script=lower_script, credential=args.credential_file.resolve(),
                     value_port=lower_port, effort=LOWER_EFFORT, cidfile=cidfiles["public"])
        if args.builder_transport == 'broker':
            attempted["builder"] = True
            start_broker(name=names["builder"], script=builder_script, credential=args.credential_file.resolve(),
                         value_port=builder_port, effort=BUILDER_EFFORT, cidfile=cidfiles["builder"])
        attempted["result_judge"] = True
        start_broker(name=names["result_judge"], script=JUDGE_BROKER_SCRIPT,
                     credential=args.credential_file.resolve(), value_port=judge_port, effort=BUILDER_EFFORT, cidfile=cidfiles["result_judge"])
        provider = run_dir / "builder_broker_provider.toml"
        provider.write_text(
            'model_provider = "aider_builder_broker"\ndisable_response_storage = true\n\n'
            '[model_providers.aider_builder_broker]\nname = "Aider pilot Builder broker"\n'
            f'base_url = "{builder_container_endpoint.rsplit("/v1", 1)[0]}/v1"\n'
            'env_key = "AGENTSWE_BUILDER_BROKER_TOKEN"\nwire_api = "responses"\nrequires_openai_auth = false\n', encoding="utf-8")
        workspace = run_dir / "builder_workspace/submission"; workspace.mkdir(parents=True)
        controller = BuilderLifecycle(run_dir=run_dir, workspace=workspace, lower_broker=public_endpoint,
                                      source=ROOT / "input/repository", image=args.lower_image,
                                      pilot_not_formal=True, max_dev_rounds=args.max_dev_rounds)
        controller.controller.dependency_overlay = args.dependency_overlay.resolve() if args.dependency_overlay else None
        controller.controller.judge_endpoint = judge_endpoint
        public = public_package(run_dir); restrict_public_package_to_pilot(public)
        start_builder_server(controller)
        config = builder_config(run_dir, public, workspace, controller, provider, pilot_not_formal=True)
        write_json(run_dir / "protocol_lock.json", {
            "schema_version": "agentswe-aider-pilot-protocol/v1", "pilot_not_formal": True,
            "builder": {"model": BUILDER_MODEL, "reasoning_effort": BUILDER_EFFORT, "single_continuous_session": True,
                        "transport": args.builder_transport,
                        "credential": "native_tmpfs_auth" if args.builder_transport == 'direct' else "broker-only-placeholder"},
            "lower_agent": {"product": "patched Aider", "model": MODEL, "reasoning_effort": LOWER_EFFORT,
                            "credential": "broker-only-placeholder"},
            "public_cases": ["dev_001", "dev_002"], "hidden_cases": ["test_001"], "formal_finalizer_allowed": False,
            "acceptance_finalizer_allowed": True, "max_dev_rounds": args.max_dev_rounds,
            "n_concurrent": 1, "dev_passed_is_automatic_freeze": False,
            "public_hidden_brokers_independent": public_endpoint != hidden_endpoint and names["public"] != names["hidden"],
            "hidden_broker_started_strictly_after_freeze": True,
            "hidden_broker_requires_zero_call_start": True,
        })
        code = run_configured_builder(args.harbor.resolve(), config, run_dir, args.builder_timeout,
            credential=args.credential_file.resolve(), transport=args.builder_transport,
            base_url=args.builder_base_url, proxy=args.builder_proxy)
        if not controller.controller.frozen and controller.controller.records:
            controller.controller.freeze_latest("builder_exit")
        records = controller.controller.records
        feedback_path = run_dir / "lifecycle/feedback_candidate_001.json"
        expected_feedback = hashlib.sha256(feedback_path.read_bytes()).hexdigest() if feedback_path.is_file() else None
        second_events = [event for event in controller.events if event.get("event") == "submission_started" and event.get("number") == 2]
        native = controller.native_attestation()
        attestation = {
            "schema_version": "agentswe-aider-pilot-builder-session-attestation/v1",
            "pilot_not_formal": True, "builder_session_id": controller.session_id,
            "builder_model": BUILDER_MODEL, "builder_reasoning_effort": BUILDER_EFFORT,
            "builder_exit_code": code, "same_session": native['valid'], "native_evidence": native, "events": controller.events,
            "accepted_submission_count": len(records),
            "accepted_submissions_public_complete": bool(records) and all(public_round_complete(record) for record in records),
            "feedback_digest": expected_feedback,
            "feedback_consumed": native['valid'],
            "distinct_digests": bool(records) and len({item.get("candidate_digest") for item in records}) == len(records),
            "freeze": controller.controller.frozen,
        }
        attestation["complete"] = all((code == 0, native['valid'], 1 <= len(records) <= args.max_dev_rounds,
                                        attestation["accepted_submissions_public_complete"],
                                        attestation["feedback_consumed"], attestation["distinct_digests"], bool(attestation["freeze"])))
        write_json(run_dir / "pilot_builder_session_attestation.json", attestation)
        if not attestation["complete"]:
            write_json(run_dir / "summary.json", {**base_summary, "status": "pilot_builder_integration_incomplete",
                       "builder_session_attestation": "pilot_builder_session_attestation.json"})
            return 2
        public_cleanup = cleanup_owned_containers({"public": names["public"]}, {"public": attempted["public"]}, {"public": cidfiles["public"]})
        write_json(run_dir / "public_broker_cleanup_before_hidden.json", public_cleanup)
        if not public_cleanup.get("all_attempted_absent"):
            raise RuntimeError("public lower broker cleanup unverified")
        attempted["public"] = False
        attempted["hidden"] = True
        start_broker(name=names["hidden"], script=lower_script, credential=args.credential_file.resolve(),
                     value_port=hidden_port, effort=LOWER_EFFORT, cidfile=cidfiles["hidden"])
        hidden_initial = stats(hidden_endpoint)
        hidden_runtime = hidden_initial.get("runtime") if isinstance(hidden_initial.get("runtime"), dict) else {}
        if public_endpoint == hidden_endpoint or names["public"] == names["hidden"]:
            raise RuntimeError("pilot hidden broker must be independent from public lower broker")
        if int(hidden_runtime.get("calls", 0) or 0) != 0 or int(hidden_runtime.get("failures", 0) or 0) != 0:
            raise RuntimeError("pilot hidden broker must start with zero calls and zero failures")
        write_json(run_dir / "pilot_hidden_broker_initial.json", {
            "schema_version": "agentswe-aider-pilot-hidden-broker-initial/v1",
            "pilot_not_formal": True,
            "started_after_freeze": True,
            "independent_from_public_lower": True,
            "calls": 0,
            "failures": 0,
            "stats": hidden_initial,
        })
        controller.controller.broker = hidden_endpoint
        hidden = controller.controller.run_hidden()
        scored = subprocess.run(["python3", str(ROOT / "evaluator/formal_finalize.py"), "--run-dir", str(run_dir),
                 "--credential-file", str(args.credential_file.resolve()), "--result-judge-broker-endpoint", judge_endpoint,
                 "--acceptance-cases", "test_001"], text=True, capture_output=True, check=False)
        scoring = read_json(run_dir / "acceptance_aggregation.json") if (run_dir / "acceptance_aggregation.json").is_file() else {}
        case_result = read_json(Path(str(hidden[0].get("result")))) if hidden and Path(str(hidden[0].get("result"))).is_file() else {}
        infrastructure_invalid = case_result.get("classification") in {
            "infrastructure-invalid", "broker_infrastructure_error", "launcher_infrastructure_error", "provider_failure"
        }
        evaluation = {"schema_version": "agentswe-aider-pilot-evaluation/v1", "pilot_not_formal": True,
                      "case_id": "test_001", "acceptance_scoring": scoring,
                      "native_result_publishable": False, "formal_result_publishable": False,
                      "code_score_publishable": False, "infrastructure_invalid": infrastructure_invalid}
        write_json(run_dir / "pilot_evaluation.json", evaluation)
        write_json(run_dir / "summary.json", {**base_summary,
            "status": "pilot_complete" if scored.returncode == 0 and scoring.get("acceptance_complete") else "pilot_incomplete",
            "classification": "real_same_session_two_dev_feedback_freeze_one_test",
            "builder_session_attestation": "pilot_builder_session_attestation.json",
            "freeze_manifest": "lifecycle/freeze_manifest.json",
            "hidden_attestation": "lifecycle/hidden-after-freeze-attestation.json",
            "hidden_broker_initial": "pilot_hidden_broker_initial.json",
            "pilot_evaluation": evaluation, "hidden": hidden})
        return 0 if scored.returncode == 0 and scoring.get("acceptance_complete") else 2
    except Exception as exc:
        write_json(run_dir / "summary.json", {**base_summary, "status": "pilot_orchestration_failed",
                   "error_type": type(exc).__name__, "error_detail": str(exc)[-1200:]})
        return 2
    finally:
        if controller is not None:
            controller.close()
        for label, endpoint in (("pilot_public_lower", public_endpoint), ("pilot_builder", builder_host_endpoint),
                                ("pilot_hidden_lower", hidden_endpoint)):
            try:
                save_role_stats(run_dir, label, endpoint, args.builder_transport)
            except Exception:
                write_json(run_dir / f"{label}_broker_stats.json", {"status": "not_started_or_unavailable"})
        cleanup = cleanup_owned_containers(names, attempted, cidfiles)
        cleanup["schema_version"] = "agentswe-aider-owned-container-cleanup-v1"
        cleanup["pilot_not_formal"] = True
        cleanup["controller_socket_closed"] = controller is None or not controller.socket_path.exists()
        try:
            cleanup["removed_compose_projects"] = cleanup_builder_containers(run_dir)
        except OSError as exc:
            cleanup["removed_compose_projects"] = []
            cleanup["compose_cleanup_error"] = f"{type(exc).__name__}: {exc}"
            cleanup["all_attempted_absent"] = False
        cleanup["completed"] = bool(cleanup["controller_socket_closed"] and cleanup["all_attempted_absent"])
        cleanup["completed_at"] = now()
        write_json(run_dir / "pilot_cleanup_attestation.json", cleanup)


def main() -> int:
    parser = argparse.ArgumentParser(
        description=(
            "Aider Agent-loop one-stop runner. Default mode performs a local,"
            " provider-free static audit; real execution requires --run-formal."
        )
    )
    parser.add_argument('--readiness-profile', choices=['single-dev-two-round-hidden-smoke-v1'])
    parser.add_argument('--readiness-binding-file', type=Path)
    parser.add_argument('--readiness-binding-sha256')
    parser.add_argument(
        "--run-dir",
        type=Path,
        help="Output directory for static receipts or the explicit formal run",
    )
    parser.add_argument(
        "--run-formal",
        action="store_true",
        help="Explicitly enable Docker/Harbor/provider/public/hidden execution",
    )
    parser.add_argument(
        "--pilot",
        action="store_true",
        help="Run real two-dev feedback lifecycle then test_001 as pilot_not_formal",
    )
    parser.add_argument(
        "--pilot-self-test",
        action="store_true",
        help="Provider-free dry test of the reduced pilot configuration",
    )
    parser.add_argument(
        "--self-test",
        action="store_true",
        help="Run the offline static-path self-test; never starts Docker or network calls",
    )
    parser.add_argument("--credential-file", type=Path, default=Path("@@AGENTSWE_CREDENTIAL_FILE@@"))
    parser.add_argument("--harbor", type=Path, default=Path("@@AGENTSWE_HARBOR_BIN@@"))
    parser.add_argument('--builder-transport', choices=['direct', 'broker'], default='direct')
    parser.add_argument('--builder-base-url', default='https://api.deepseek.com/v1')
    parser.add_argument('--builder-proxy', default='http://127.0.0.1:7890')
    parser.add_argument("--image", default=IMAGE)
    parser.add_argument("--lower-image", default="agentswe/edit-candidate-python311:0826")
    parser.add_argument("--builder-timeout", type=int, default=28800)
    parser.add_argument("--max-dev-rounds", type=int, default=10,
                        help="maximum distinct accepted Builder submissions (1..10)")
    parser.add_argument("--n-concurrent", type=int, default=1,
                        help="dev evaluations per branch; formal protocol requires 1")
    parser.add_argument(
        "--dependency-overlay",
        type=Path,
        default=Path("@@AGENTSWE_ENVS@@/aider-worktree-transaction-ledger-edit-v1/lib/python3.11/site-packages"),
    )
    args = parser.parse_args()
    if args.readiness_profile and not args.pilot:
        parser.error('--readiness-profile requires --pilot')
    if not 1 <= args.max_dev_rounds <= 10:
        parser.error("--max-dev-rounds must be between 1 and 10")
    if args.n_concurrent != 1:
        parser.error("--n-concurrent must equal 1")
    if args.builder_timeout <= 0:
        parser.error("--builder-timeout must be positive")
    enforce_builder_timeout_contract(parser, args.builder_timeout)
    selected_modes = sum(bool(value) for value in (args.self_test, args.pilot_self_test, args.pilot, args.run_formal))
    if selected_modes > 1:
        parser.error("--self-test, --pilot-self-test, --pilot, and --run-formal are mutually exclusive")
    if args.self_test:
        return self_test()
    if args.pilot_self_test:
        return pilot_self_test()
    if args.run_dir is None:
        parser.error("--run-dir is required for static audit, --pilot, or --run-formal")
    run_dir = args.run_dir.resolve()
    if args.pilot:
        return run_pilot(args, run_dir)
    if not args.run_formal:
        return run_static(run_dir)
    return _run_formal(args, run_dir)


if __name__ == "__main__":
    raise SystemExit(main())
