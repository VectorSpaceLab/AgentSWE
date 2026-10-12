#!/usr/bin/env python3
"""Evaluator-owned single-session Builder submission controller.

The Builder can see only its writable three-file delivery directory and a
Unix socket exposing ``submit``/``status``.  A submission is validated,
snapshotted, applied to the pinned OpenClaw source, evaluated on both public
cases, and reduced to Builder-visible feedback.  Candidate 2 is accepted only
after that feedback was delivered to the same socket session.
"""
from __future__ import annotations

import hashlib
import json
import os
import shutil
import socketserver
import subprocess
import tempfile
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

from controller.two_round_controller import DEV, TwoRoundController, feedback_projection, tree_digest


def contract_tree_digest(root):
    """The readiness contract's candidate digest (identical to v2_readiness.tree_digest and
    agentloop.protocol.tree_digest): name-first framing over every entry. The Create-style
    `tree_digest` above frames kind-first and is kept for repository/product digests.
    Verified 2026-09-19 on 0919-ds-002: v2 == this for all three submission dirs."""
    root = Path(root)
    digest = hashlib.sha256()
    for path in sorted(root.rglob("*"), key=lambda p: p.relative_to(root).as_posix()):
        name = path.relative_to(root).as_posix().encode()
        if path.is_symlink():
            kind, payload = b"L", os.readlink(path).encode()
        elif path.is_file():
            kind, payload = b"F", path.read_bytes()
        elif path.is_dir():
            kind, payload = b"D", b""
        else:
            raise ValueError("special file in tree")
        digest.update(len(name).to_bytes(8, "big")); digest.update(name)
        digest.update(kind); digest.update(len(payload).to_bytes(8, "big")); digest.update(payload)
    return digest.hexdigest()
from lower_agent.entry_contract import PRODUCTION_ENTRY

DELIVERY_FILES = {"solution.patch", "edit_report.json", "run_report.json"}
PROBE_MARKER = "PROBE_ONLY_CANDIDATE2_MARKER"
ALLOWED_PREFIXES = (
    "src/gateway/",
    "src/channels/",
    "src/sessions/",
    "src/agents/",
    "src/audit/",
    "src/cli/",
    "src/state/",
    "src/media/",
    "packages/gateway-protocol/",
)


def now() -> str:
    return datetime.now(timezone.utc).isoformat()


def write_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    with temporary.open("w", encoding="utf-8") as output:
        output.write(json.dumps(value, indent=2, ensure_ascii=False) + "\n")
        output.flush()
        os.fsync(output.fileno())
    os.replace(temporary, path)
    descriptor = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
    try: os.fsync(descriptor)
    finally: os.close(descriptor)


def json_object(path: Path) -> dict[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError(f"{path.name} must be a JSON object")
    return value


def changed_paths(patch: Path) -> list[str]:
    paths: list[str] = []
    for line in patch.read_text(encoding="utf-8").splitlines():
        if line.startswith("diff --git a/") and " b/" in line:
            paths.append(line.split(" b/", 1)[1])
        elif line.startswith("+++ b/"):
            paths.append(line[6:].split("\t", 1)[0])
    return sorted(set(paths))


def marker_paths(root: Path) -> list[str]:
    hits: list[str] = []
    if not root.is_dir():
        return hits
    for path in root.rglob("*"):
        if path.is_symlink() or not path.is_file():
            continue
        relative = path.relative_to(root).as_posix()
        if PROBE_MARKER in path.name:
            hits.append(relative)
            continue
        if path.stat().st_size <= 8 * 1024 * 1024:
            try:
                if PROBE_MARKER in path.read_text(encoding="utf-8"):
                    hits.append(relative)
            except (OSError, UnicodeDecodeError):
                pass
    return sorted(set(hits))


def validate_delivery(root: Path) -> list[str]:
    errors: list[str] = []
    if not root.is_dir():
        return ["delivery directory is missing"]
    names = {path.name for path in root.iterdir()}
    errors.extend(f"missing {name}" for name in sorted(DELIVERY_FILES - names))
    errors.extend(f"unexpected top-level artifact {name}" for name in sorted(names - DELIVERY_FILES))
    if errors:
        return errors
    if any((root / name).is_symlink() or not (root / name).is_file() for name in DELIVERY_FILES):
        return ["delivery artifacts must be ordinary files"]
    hits = marker_paths(root)
    if hits:
        errors.append(f"formal delivery contains prohibited probe marker: {hits}")
    patch = root / "solution.patch"
    try:
        paths = changed_paths(patch)
    except (OSError, UnicodeDecodeError) as exc:
        return errors + [f"solution.patch is not readable UTF-8: {type(exc).__name__}"]
    if not paths:
        errors.append("solution.patch contains no changed paths")
    for value in paths:
        candidate = Path(value)
        if candidate.is_absolute() or ".." in candidate.parts:
            errors.append(f"unsafe patch path: {value}")
        elif not any(value.startswith(prefix) for prefix in ALLOWED_PREFIXES):
            errors.append(f"patch path outside allowed production/test surfaces: {value}")
    try:
        edit = json_object(root / "edit_report.json")
        # feedback_response is REQUIRED from submission 2 on (see run_report below),
        # and this compared key sets for exact equality -- so a conformant round-2
        # delivery was refused here while the rule below refused the delivery
        # without it.  The four keys stay required; anything outside these five
        # is still refused.
        _required_edit = {"summary", "changed_paths", "production_seams", "known_limits"}
        _optional_edit = {"feedback_response"}
        if not _required_edit <= set(edit) or (set(edit) - _required_edit - _optional_edit):
            errors.append("edit_report.json fields do not match the delivery contract")
        if not isinstance(edit.get("summary"), str) or not edit.get("summary", "").strip():
            errors.append("edit_report.json summary must be non-empty")
        for key in ("changed_paths", "production_seams", "known_limits"):
            if not isinstance(edit.get(key), list) or not all(isinstance(item, str) for item in edit.get(key, [])):
                errors.append(f"edit_report.json {key} must be an array of strings")
        if isinstance(edit.get("changed_paths"), list) and sorted(set(edit["changed_paths"])) != paths:
            errors.append("edit_report.json changed_paths does not exactly match solution.patch")
    except (OSError, UnicodeDecodeError, json.JSONDecodeError, ValueError) as exc:
        errors.append(f"edit_report.json is invalid: {type(exc).__name__}: {exc}")
    try:
        report = json_object(root / "run_report.json")
        required_report = {"status", "commands", "tests", "errors"}
        # The readiness profile requires four further fields in run_report.json,
        # and this compared key sets for exact equality -- so a delivery
        # carrying them would be refused here while finalize refuses the
        # delivery without them. The four above stay required and anything
        # outside these eight is still refused.
        readiness_report = {"builder_session_id", "submission_number",
                            "revision_of_candidate_digest", "feedback_digest"}
        unexpected = set(report) - required_report - readiness_report
        if not required_report <= set(report) or unexpected:
            errors.append(
                "run_report.json fields do not match the delivery contract; required %s, "
                "optional %s, found %s" % (sorted(required_report), sorted(readiness_report),
                                           sorted(report)))
        if report.get("status") not in {"ok", "error"}:
            errors.append("run_report.json status must be ok or error")
        # Readiness rule (shared v2 bundle validator: "revision explanation required"):
        # from the second submission on, edit_report.json must explain in the
        # Builder's own prose how the revision answers the feedback. Refusing it
        # here gives the Builder an actionable error instead of a silent failure
        # at admission. Formal deliveries carry no submission_number and are untouched.
        if isinstance(report.get("submission_number"), int) and report["submission_number"] >= 2:
            try:
                _edit_for_feedback = json_object(root / "edit_report.json")
            except (OSError, UnicodeDecodeError, json.JSONDecodeError, ValueError):
                _edit_for_feedback = {}
            _fr = _edit_for_feedback.get("feedback_response")
            if not isinstance(_fr, str) or not _fr.strip():
                errors.append("edit_report.json feedback_response is required from submission 2 on: a non-empty string of your own prose explaining how this revision answers the evaluator's feedback")
        if not isinstance(report.get("tests"), list) or not all(isinstance(item, str) for item in report.get("tests", [])):
            errors.append("run_report.json tests must be an array of strings")
        if not isinstance(report.get("errors"), list) or not all(isinstance(item, str) for item in report.get("errors", [])):
            errors.append("run_report.json errors must be an array of strings")
        commands = report.get("commands")
        if not isinstance(commands, list):
            errors.append("run_report.json commands must be an array")
        else:
            expected = {"command", "exit_code", "duration_seconds"}
            if any(not isinstance(item, dict) or set(item) != expected for item in commands):
                errors.append("run_report.json command records have invalid fields")
    except (OSError, UnicodeDecodeError, json.JSONDecodeError, ValueError) as exc:
        errors.append(f"run_report.json is invalid: {type(exc).__name__}: {exc}")
    return errors


def materialize_delivery(*, source: Path, delivery: Path, output: Path) -> dict[str, Any]:
    """Apply one validated patch to a private copy of the pinned product."""
    errors = validate_delivery(delivery)
    if errors:
        return {"ready": False, "classification": "invalid_patch", "errors": errors}
    if output.exists():
        raise FileExistsError(f"materialized Candidate already exists: {output}")
    shutil.copytree(
        source,
        output,
        symlinks=True,
        ignore=shutil.ignore_patterns(".git", "node_modules", "dist", ".artifacts", "__pycache__"),
    )
    commands: list[dict[str, Any]] = []
    for label, command in (
        ("patch_check", ["git", "apply", "--check", str(delivery / "solution.patch")]),
        ("patch_apply", ["git", "apply", str(delivery / "solution.patch")]),
    ):
        completed = subprocess.run(command, cwd=output, text=True, capture_output=True, check=False, timeout=120)
        item = {
            "label": label,
            "command": command,
            "exit_code": completed.returncode,
            "stdout_tail": completed.stdout[-2000:],
            "stderr_tail": completed.stderr[-2000:],
        }
        commands.append(item)
        if completed.returncode:
            shutil.rmtree(output, ignore_errors=True)
            return {"ready": False, "classification": "invalid_patch", "errors": [f"{label} failed"], "commands": commands}
    hits = marker_paths(output)
    if hits:
        shutil.rmtree(output, ignore_errors=True)
        return {"ready": False, "classification": "invalid_patch", "errors": [f"materialized product contains prohibited probe marker: {hits}"], "commands": commands}
    return {
        "ready": True,
        "classification": "candidate_materialized",
        "changed_paths": changed_paths(delivery / "solution.patch"),
        "candidate_digest": tree_digest(output),
        "commands": commands,
    }


class BuilderSessionController:
    """One native invocation, durable intent reservation and public feedback."""

    def __init__(self, *, run_dir, workspace, source, evaluate, public_case_ids=DEV,
                 hidden_case_ids=tuple(f"test_{i:03d}" for i in range(1, 7)),
                 evidence_kind="formal", max_dev_rounds=10, n_concurrent=1,
                 require_native_evidence=True, readiness_profile=None, current_binding=None):
        self.run_dir = Path(run_dir).resolve()
        self.workspace = Path(workspace).resolve()
        self.source = Path(source).resolve()
        self.public_case_ids = tuple(public_case_ids)
        self.hidden_case_ids = tuple(hidden_case_ids)
        self.evidence_kind = evidence_kind
        self.lifecycle = TwoRoundController(self.run_dir / "lifecycle", evaluate,
            dev_case_ids=self.public_case_ids, hidden_case_ids=self.hidden_case_ids,
            evidence_kind=evidence_kind, max_dev_rounds=max_dev_rounds, n_concurrent=n_concurrent)
        self.readiness_profile = readiness_profile
        self.current_binding = current_binding
        if readiness_profile:
            self.lifecycle.readiness_fields = self._readiness_freeze_fields
        self.run_dir.mkdir(parents=True, exist_ok=True)
        self.intent_path = self.run_dir / "builder_request_intents.json"
        if self.intent_path.exists():
            raise RuntimeError("Existing Builder intents require reconciliation; automatic replay is forbidden")
        entropy = hashlib.sha256(f"{self.run_dir}:{time.time_ns()}".encode()).hexdigest()
        self.token = entropy
        self.require_native_evidence = require_native_evidence
        self.session_id = None if require_native_evidence else "diagnostic-session-" + entropy[:16]
        self.socket_path = Path(tempfile.gettempdir()) / f"openclaw-builder-{entropy[:12]}.sock"
        self.server = None
        self.lock = threading.RLock()
        self.active = False
        self.feedback_delivered_at = None
        self.feedback_digest = None
        self.feedback_acknowledged = False
        self.feedback_acknowledged_at = None
        self.events = []
        self.submissions = []
        self.deliveries = []
        self.observations = []
        self.intents = {}
        self.product_outcomes = {}
        self.frozen = None

    def _event(self, event, **fields):
        value = {"event": event, "at": now(), "session_id": self.session_id, **fields}
        self.events.append(value)
        with (self.run_dir / "builder_events.jsonl").open("a", encoding="utf-8") as output:
            output.write(json.dumps(value, ensure_ascii=False) + "\n")
            output.flush()
            os.fsync(output.fileno())

    def _persist(self):
        write_json(self.intent_path, self.intents)
        write_json(self.run_dir / "builder_submissions.json", self.submissions)
        write_json(self.run_dir / "builder_feedback_deliveries.json", self.deliveries)

    def _identity(self):
        if self.require_native_evidence:
            from harbor.native_builder_evidence import observe_thread
            ref = observe_thread(self.run_dir, self.observations)
            if self.session_id is not None and self.session_id != ref["thread_id"]:
                raise ValueError("native Builder identity changed")
            self.session_id = ref["thread_id"]
            if not self.observations or self.observations[-1] != ref:
                self.observations.append(ref)
                write_json(self.run_dir / "builder_native_observations.json", self.observations)

    def _session(self, session_id, *, allow_initial=False):
        try:
            self._identity()
        except Exception as exc:
            self._event("native_identity_unavailable", error_type=type(exc).__name__)
            return 503, {"error": "native_identity_unavailable"}
        if session_id != self.session_id and not (allow_initial and not session_id):
            return 401, {"error": "builder_session_mismatch"}
        return None

    @staticmethod
    def _public_freeze(value):
        return {k: value.get(k) for k in ("candidate_digest", "freeze_reason", "source_submission")} if value else None

    def _feedback_payload(self, submission):
        path = Path(submission["feedback"])
        data = path.read_bytes()
        if hashlib.sha256(data).hexdigest() != submission["feedback_digest"]:
            raise RuntimeError("authoritative feedback bytes changed")
        return {
            "builder_session_id": self.session_id,
            "submission_number": submission["number"],
            "candidate_digest": submission["candidate_digest"],
            # The readiness contract's candidate identity is the three-file delivery digest;
            # exposed at top level so the Builder copies the right one into round 2.
            "delivery_digest": submission.get("delivery_digest"),
            "feedback_digest": submission["feedback_digest"],
            "feedback_digest_ack": submission.get("feedback_digest_ack"),
            "feedback": json.loads(data),
            "submission": {k: submission.get(k) for k in ("number", "candidate_digest", "delivery_digest", "feedback_digest", "feedback_digest_ack", "dev_case_ids")},
            "freeze": self._public_freeze(self.frozen),
            "active": False,
        }

    def record_delivery(self, payload):
        """Called only after the actual response write and flush succeed."""
        number = payload.get("submission_number")
        if not isinstance(number, int) or not 1 <= number <= len(self.submissions):
            return
        with self.lock:
            row = self.submissions[number - 1]
            expected = self._feedback_payload(row)
            if any(payload.get(k) != expected.get(k) for k in ("builder_session_id", "candidate_digest", "feedback_digest", "feedback", "feedback_digest_ack")):
                raise ValueError("feedback delivery binding mismatch")
            delivered = now()
            receipt = {"candidate_number": number, "builder_session_id": self.session_id,
                "feedback_digest": row["feedback_digest"], "delivered_at": delivered,
                "payload_sha256": hashlib.sha256(json.dumps(payload, sort_keys=True, ensure_ascii=False).encode()).hexdigest()}
            self.deliveries.append(receipt)
            if row.get("feedback_delivered_at") is None:
                row["feedback_delivered_at"] = delivered
                self._event("feedback_delivered", number=number, feedback_digest=row["feedback_digest"])
            if number == len(self.submissions):
                self.feedback_delivered_at = row["feedback_delivered_at"]
            self._persist()

    def start(self):
        controller = self
        class Handler(socketserver.StreamRequestHandler):
            def handle(self):
                try:
                    request = json.loads(self.rfile.readline(1 << 20))
                    if request.get("token") != controller.token:
                        code, payload = 401, {"error": "unauthorized"}
                    elif request.get("action") == "submit":
                        code, payload = controller.submit(request.get("builder_session_id"), request.get("feedback_digest"))
                    elif request.get("action") == "ack_feedback":
                        code, payload = controller.acknowledge_feedback(request.get("builder_session_id"), request.get("feedback_digest"))
                    elif request.get("action") == "status":
                        code, payload = controller.status(request.get("builder_session_id"))
                    else:
                        code, payload = 400, {"error": "unknown_action"}
                except Exception as exc:
                    controller._event("controller_error", error_type=type(exc).__name__)
                    code, payload = 503, {"error": "controller_infrastructure_failure"}
                try:
                    self.wfile.write(json.dumps({"status": code, "payload": payload}, ensure_ascii=False).encode() + b"\n")
                    self.wfile.flush()
                except (BrokenPipeError, ConnectionError, OSError):
                    controller._event("feedback_write_failed", response_status=code)
                    return
                if code == 200:
                    controller.record_delivery(payload)
        class Server(socketserver.ThreadingUnixStreamServer):
            daemon_threads = True
        self.server = Server(str(self.socket_path), Handler)
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def close(self):
        if self.server:
            self.server.shutdown()
            self.server.server_close()
            self.server = None
        self.socket_path.unlink(missing_ok=True)

    def _outcome(self, digest, code, payload, *, product_digest=None, state="completed"):
        self.intents[digest].update(state=state, response_status=code, payload=payload, completed_at=now())
        if product_digest:
            self.intents[digest]["product_digest"] = product_digest
            self.product_outcomes[product_digest] = digest
        self._persist()
        return code, payload

    def submit(self, session_id, feedback_digest=None):
        with self.lock:
            error = self._session(session_id)
            if error:
                return error
            errors = validate_delivery(self.workspace)
            if errors:
                return 422, {"error": "invalid_submission", "details": errors}
            digest = contract_tree_digest(self.workspace)  # readiness contract candidate digest
            cached = self.intents.get(digest)
            if cached:
                return cached.get("response_status", 503), cached.get("payload", {"error": "submission_outcome_unknown", "resampling_forbidden": True})
            if self.active:
                return 409, {"error": "evaluation_in_progress"}
            if self.frozen:
                return 409, {"error": "candidate_already_frozen"}
            number = len(self.submissions) + 1
            if number > self.lifecycle.max_dev_rounds:
                return 409, {"error": "submission_limit_reached"}
            if number > 1 and (not self.feedback_delivered_at or feedback_digest != self.feedback_digest):
                return 409, {"error": "latest_feedback_not_delivered_or_ack_mismatch"}
            self.intents[digest] = {"state": "unknown", "reserved_at": now(), "number": number,
                "builder_session_id": self.session_id, "feedback_digest_ack": feedback_digest,
                "resampling_forbidden": True}
            self._persist()
            self.active = True
            product_digest = None
            try:
                name = "submission_" + f"{number:03d}" + "_" + digest[:16]
                delivery = self.run_dir / "builder_submissions" / name
                delivery.parent.mkdir(parents=True, exist_ok=True)
                shutil.copytree(self.workspace, delivery, symlinks=True)
                if contract_tree_digest(delivery) != digest:
                    raise RuntimeError("delivery changed while snapshotting")
                product = self.run_dir / "materialized_candidates" / name
                materialized = materialize_delivery(source=self.source, delivery=delivery, output=product)
                write_json(self.run_dir / "materialization" / (name + ".json"), materialized)
                if not materialized.get("ready"):
                    return self._outcome(digest, 422, {"error": "candidate_materialization_failed", "details": materialized.get("errors", [])})
                product_digest = materialized["candidate_digest"]
                if product_digest in self.product_outcomes:
                    original = self.intents[self.product_outcomes[product_digest]]
                    return self._outcome(digest, original.get("response_status", 503), original.get("payload", {"error": "submission_outcome_unknown", "resampling_forbidden": True}), product_digest=product_digest, state=original["state"])
                self.product_outcomes[product_digest] = digest
                self.intents[digest]["product_digest"] = product_digest
                self._persist()
                submitted_at = now()
                self._event("candidate_submitted", number=number, delivery_digest=digest, candidate_digest=product_digest)
                # Readable by _readiness_freeze_fields for the freeze that happens
                # inside submit(), before this round joins self.submissions.
                self._pending_delivery = {"delivery_digest": digest, "delivery_snapshot": str(delivery)}
                record = self.lifecycle.submit(product)
                if record.classification == "infrastructure-invalid":
                    self._event("public_infrastructure_invalid", number=number)
                    # This attempt consumed no round, so it must not latch.  Leaving the
                    # product digest in product_outcomes made every later delivery that
                    # materialized to the same tree replay this 503 through the
                    # product_outcomes branch above, without re-running the round --
                    # one transient evaluator fault became a permanent terminal for
                    # that code and the Builder could only exit.  The delivery-digest
                    # intent stays cached, so a byte-identical resubmission is still
                    # idempotent and no model work is ever resampled for one request.
                    self.product_outcomes.pop(product_digest, None)
                    return self._outcome(digest, 503, {"error": "public_infrastructure_invalid", "round_consumed": False,
                        "resampling_forbidden": True, "dev_results": feedback_projection(record.dev)}, state="unknown")
                feedback_path = self.run_dir / "lifecycle/feedback" / f"candidate_{number:03d}.json"
                feedback_hash = hashlib.sha256(feedback_path.read_bytes()).hexdigest()
                row = {"number": number, "role": "initial" if number == 1 else "feedback_revision",
                    "builder_session_id": self.session_id, "delivery_snapshot": str(delivery), "delivery_digest": digest,
                    "candidate_digest": record.digest, "submitted_at": submitted_at,
                    "dev_completed_at": record.dev_completed_at, "feedback": str(feedback_path),
                    "feedback_digest": feedback_hash, "feedback_digest_ack": feedback_digest if number > 1 else None,
                    "feedback_delivered_at": None, "dev_case_ids": list(record.dev), "dev_results": record.dev}
                self.submissions.append(row)
                self.feedback_digest = feedback_hash
                self.feedback_delivered_at = None
                self.feedback_acknowledged = number > 1
                self.feedback_acknowledged_at = submitted_at if number > 1 else None
                if number > 1:
                    self._event("feedback_acknowledged", number=number, feedback_digest=feedback_digest)
                if self.lifecycle.frozen:
                    self.frozen = self.lifecycle.frozen
                return self._outcome(digest, 200, self._feedback_payload(row), product_digest=product_digest)
            except Exception as exc:
                self._event("submission_outcome_unknown", number=number, error_type=type(exc).__name__)
                return self._outcome(digest, 503, {"error": "submission_outcome_unknown", "resampling_forbidden": True}, product_digest=product_digest, state="unknown")
            finally:
                self._pending_delivery = None
                self.active = False

    def acknowledge_feedback(self, session_id, feedback_digest=None):
        with self.lock:
            error = self._session(session_id)
            if error:
                return error
            if not self.feedback_delivered_at or feedback_digest != self.feedback_digest:
                return 409, {"error": "exact_delivered_feedback_digest_required"}
            self.feedback_acknowledged = True
            self.feedback_acknowledged_at = now()
            self._event("feedback_acknowledged", feedback_digest=feedback_digest)
            return 200, {"builder_session_id": self.session_id, "feedback_digest": feedback_digest, "feedback_acknowledged": True}

    def status(self, session_id=None):
        with self.lock:
            error = self._session(session_id, allow_initial=True)
            if error:
                return error
            payload = self._feedback_payload(self.submissions[-1]) if self.submissions else {"builder_session_id": self.session_id}
            payload.update(active=self.active, accepted_submissions=len(self.submissions),
                feedback_acknowledged=self.feedback_acknowledged, frozen=self._public_freeze(self.frozen))
            return 200, payload

    def _readiness_freeze_fields(self):
        """What the shared readiness contract reads off the sealed freeze.

        The accepted delivery is the last recorded submission; its three files
        are hashed from that same snapshot, so the freeze attests the bytes it
        was taken over rather than a digest recomputed elsewhere later.
        """
        # The lifecycle freezes from inside its submit(), before the round it has
        # just accepted is appended to self.submissions, so submissions[-1] is the
        # PREVIOUS round for exactly that window and the freeze used to seal the
        # previous delivery's digest and file hashes.  The delivery under
        # evaluation publishes itself as _pending_delivery for that window.
        latest = getattr(self, "_pending_delivery", None)
        if latest is None and self.submissions:
            latest = self.submissions[-1]
        if latest is None:
            raise RuntimeError("readiness freeze requires an accepted submission")
        snapshot = Path(latest["delivery_snapshot"])
        names = ("solution.patch", "edit_report.json", "run_report.json")
        if sorted(path.name for path in snapshot.iterdir()) != sorted(names):
            raise RuntimeError("accepted delivery is not exactly the three contract files")
        return {
            "readiness_profile": self.readiness_profile,
            "current_binding": self.current_binding,
            "delivery_candidate_digest": latest["delivery_digest"],
            "submission_sha256": {
                name: hashlib.sha256((snapshot / name).read_bytes()).hexdigest() for name in names
            },
        }

    def freeze_on_builder_exit(self):
        with self.lock:
            if not self.frozen:
                self.frozen = self.lifecycle.freeze_latest("builder_exit")
                self._event("candidate_frozen", number=len(self.submissions), candidate_digest=self.frozen["candidate_digest"], freeze_reason="builder_exit")
            return self.frozen

    def write_attestation(self, *, builder_exit_code, builder_started_at, builder_finished_at,
                          interrupt_exit_codes=(124,)):
        from harbor.native_builder_evidence import verify_native
        records = [{**item, "feedback_path": item["feedback"], "build": {"candidate_repo_digest": item["candidate_digest"]}} for item in self.submissions]
        # two_round_controller freezes with freeze_reason "max_dev_rounds" as
        # soon as the last round is recorded, and every later submit gets 409
        # "candidate_already_frozen".  The native Builder process can still be
        # alive when the outer deadline fires, and native_builder_runner then
        # reports 124.  That is the only interrupted terminal tolerated here,
        # and only with a max_dev_rounds freeze and a full round count; 125
        # (resource/cleanup failure) stays fatal.
        interrupted_at_max_rounds = bool(
            builder_exit_code in tuple(interrupt_exit_codes)
            and (self.frozen or {}).get("freeze_reason") == "max_dev_rounds"
            and len(self.submissions) == self.lifecycle.max_dev_rounds)
        native = verify_native(self.run_dir, records, self.deliveries, self.observations,
                               allow_interrupted=interrupted_at_max_rounds)
        accepted = 1 <= len(self.submissions) <= self.lifecycle.max_dev_rounds
        distinct = accepted and len({r["candidate_digest"] for r in self.submissions}) == len(self.submissions)
        feedback_chain = accepted and all(r.get("feedback_delivered_at") and (index == 0 or (r.get("feedback_digest_ack") == self.submissions[index - 1]["feedback_digest"] and self.submissions[index - 1]["feedback_delivered_at"] <= r["submitted_at"])) for index, r in enumerate(self.submissions))
        public_complete = accepted and all(set(r["dev_results"]) == set(self.public_case_ids) and all(isinstance(v, dict) and v.get("candidate_runtime_ready") is True and v.get("production_entry") == PRODUCTION_ENTRY and v.get("model") == "deepseek-flash" and v.get("reasoning_effort") == "high" and v.get("credential_mode") == "placeholder-only" and v.get("classification") not in {"broker_infrastructure_error", "launcher_infrastructure_error", "infrastructure-invalid", "frozen_candidate_mutation"} and isinstance(v.get("broker_stats_delta"), dict) for v in r["dev_results"].values()) for r in self.submissions)
        frozen_matches = bool(accepted and self.frozen and self.frozen["candidate_digest"] == self.submissions[-1]["candidate_digest"])
        no_marker = all(not marker_paths(Path(r["delivery_snapshot"])) for r in self.submissions)
        builder_terminal_valid = builder_exit_code == 0 or interrupted_at_max_rounds
        eligible = bool(self.require_native_evidence and native["valid"] and builder_terminal_valid and accepted and distinct and feedback_chain and public_complete and frozen_matches and no_marker)
        result = {"schema_version": "openclaw-builder-session-attestation-v2", "evidence_kind": self.evidence_kind,
            "public_case_inventory": list(self.public_case_ids), "hidden_case_inventory": list(self.hidden_case_ids),
            "builder_session_id": self.session_id, "builder_model": "deepseek-flash", "builder_reasoning_effort": "max",
            "builder_started_at": builder_started_at, "builder_finished_at": builder_finished_at, "builder_exit_code": builder_exit_code,
            "builder_terminal_valid": builder_terminal_valid,
            "builder_interrupted_at_max_dev_rounds": interrupted_at_max_rounds,
            "single_builder_process": native["valid"], "same_session": native["valid"], "native_evidence": native,
            "accepted_submission_count": len(self.submissions), "accepted_submission_digests": [r["candidate_digest"] for r in self.submissions],
            "max_dev_rounds": self.lifecycle.max_dev_rounds, "n_concurrent": self.lifecycle.n_concurrent,
            "dev_passed_is_automatic_freeze": False, "distinct_candidate_digests": distinct,
            "feedback_delivered_count": sum(bool(r.get("feedback_delivered_at")) for r in self.submissions),
            "feedback_acknowledged_count": max(0, len(self.submissions) - 1), "feedback_chain_complete": feedback_chain,
            "all_accepted_public_dev_case_ids": [r["dev_case_ids"] for r in self.submissions],
            "latest_candidate_frozen": frozen_matches, "freeze_reason": (self.frozen or {}).get("freeze_reason"),
            "probe_marker_absent": no_marker, "public_evidence_complete": public_complete,
            "events": self.events, "submissions": self.submissions,
            "freeze_manifest": str(self.run_dir / "lifecycle/freeze_manifest.json") if self.frozen else None,
            "formal_lifecycle_eligible": eligible and self.evidence_kind == "formal" and self.public_case_ids == DEV and self.hidden_case_ids == tuple(f"test_{i:03d}" for i in range(1, 7)),
            "pilot_lifecycle_eligible": eligible and self.evidence_kind == "pilot" and self.public_case_ids == ("dev_001",) and self.hidden_case_ids == ("test_001",),
            "formal_result_claimed": False}
        write_json(self.run_dir / "builder_session_attestation.json", result)
        return result
