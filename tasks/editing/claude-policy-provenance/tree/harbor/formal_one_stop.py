#!/usr/bin/env python3
"""One-stop Claude Edit Builder -> repeated public feedback -> freeze -> six hidden.

Formal execution is opt-in via ``--run-formal``.  The default self-test is
offline and never starts Harbor, Docker, a provider call, or hidden execution.

The live path owns one upper Builder Harbor invocation locked to
deepseek-flash/xhigh, one public lower broker locked to deepseek-flash/medium, and a
separate hidden lower broker started only after the final Candidate is frozen.
The Builder may submit up to ten distinct accepted Candidates, receives
evaluator feedback from both public cases after each accepted round, and
revises in the same session. The evaluator freezes on max rounds or Builder
exit. The Candidate sees only ``broker-only-placeholder``; provider
credentials for lower/judge remain evaluator-owned. The upper Builder defaults
to the user-authorized native GATEWAY auth and existing proxy transport.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import socket
import socketserver
import stat
import subprocess
import sys
import tempfile
import threading
import time
import urllib.error
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
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from agentloop.evaluator.builder_protocol import (  # noqa: E402
    BUILDER_EFFORT,
    BUILDER_MODEL,
    feedback_digest,
    load_and_verify_witness,
)
from agentloop.evaluator.controller import Controller, DEV_CASES  # noqa: E402
from agentloop.evaluator.case_contract import load_case_bundle, require_decision_authority  # noqa: E402
from agentloop.evaluator.hidden_executor import CASE_IDS  # noqa: E402
from agentloop.evaluator.materialize import materialize, patch_paths  # noqa: E402
from agentloop.evaluator.public_package import stage as stage_public  # noqa: E402
from agentloop.protocol import MODEL, REASONING_EFFORT, candidate_tree_digest, write_json  # noqa: E402
from harbor.public_feedback import public_payload, semantic_feedback
from harbor.native_builder_evidence import NativeEvidenceError, observe_thread, verify_native


BUILDER_IMAGE = "agentswe/create-agent-builder-codex:0.144.1-node24.6.0-0812"
LOWER_IMAGE = "agentswe/edit-candidate-python311:0826"
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
BUILDER_BROKER_SCRIPT = Path(
    "@@AGENTSWE_EDITING_CONTROL@@/builder_broker_xhigh.py"
)
PLACEHOLDER = "broker-only-placeholder"
PROVIDER_KEYS = {"OPENAI_API_KEY", "DEEPSEEK_API_KEY", "AGENTSWE_PROVIDER_KEY"}
DELIVERY_FILES = {"solution.patch", "edit_report.json", "run_report.json"}
PROVIDER_COUNTERS = {"deepseek", "gateway", "gateway_image", "serper", "web_retrieval"}
READINESS_PROFILE = "single-dev-two-round-hidden-smoke-v1"


def now() -> str:
    return datetime.now(timezone.utc).isoformat()


def read_json(path: Path) -> dict[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError(f"expected JSON object: {path}")
    return value


def delivery_digest(root: Path) -> str:
    digest = hashlib.sha256()
    for path in sorted(root.rglob("*"), key=lambda item: item.relative_to(root).as_posix()):
        relative = path.relative_to(root).as_posix().encode("utf-8")
        digest.update(len(relative).to_bytes(8, "big")); digest.update(relative)
        if path.is_symlink():
            kind, payload = b"L", os.readlink(path).encode("utf-8")
        elif path.is_file():
            kind, payload = b"F", path.read_bytes()
        else:
            kind, payload = b"D", b""
        digest.update(kind); digest.update(len(payload).to_bytes(8, "big")); digest.update(payload)
    return digest.hexdigest()


def free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as listener:
        listener.bind(("127.0.0.1", 0))
        return int(listener.getsockname()[1])


def _broker_base(endpoint: str) -> str:
    return endpoint.removesuffix("/v1/responses").rstrip("/")


def broker_health(endpoint: str) -> dict[str, Any]:
    with urllib.request.urlopen(_broker_base(endpoint) + "/healthz", timeout=5) as response:
        value = json.loads(response.read())
    if not isinstance(value, dict):
        raise RuntimeError("broker health is not a JSON object")
    if value.get("ok") is not True and value.get("status") != "ok":
        raise RuntimeError(f"broker is not healthy: {value}")
    return value


def broker_stats(endpoint: str) -> dict[str, Any]:
    request = urllib.request.Request(
        _broker_base(endpoint) + "/stats",
        headers={"Authorization": "Bearer stats-only-placeholder"},
    )
    with urllib.request.urlopen(request, timeout=10) as response:
        value = json.loads(response.read())
    if not isinstance(value, dict):
        raise RuntimeError("broker stats are not a JSON object")
    return value


def credential_has_provider_key(path: Path) -> bool:
    try:
        for raw in path.read_text(encoding="utf-8").splitlines():
            line = raw.strip()
            if line and not line.startswith("#") and "=" in line:
                key, value = line.split("=", 1)
                if key.strip() in PROVIDER_KEYS and value.strip().strip("'\""):
                    return True
    except (OSError, UnicodeError):
        return False
    return False


def readiness_delivery_errors(delivery: Path, number: int, session_id: str | None,
                              rounds: list[dict[str, Any]], feedback_digest: str | None) -> list[str]:
    """Report wrong readiness metadata at preflight, naming the expected value.

    Without this the Builder learns it wrote the wrong digest only when the
    bundle is exported, long after freeze, and the whole run is spent. Preflight
    consumes no round, so the Builder can correct the file and try again.

    The digests are not secret: every value named here was already handed to the
    Builder in the 200 response to its own accepted submission.
    """
    try:
        report = read_json(delivery / "run_report.json")
    except Exception:
        return []  # validate_delivery already reports an unreadable run_report
    if not isinstance(report, dict):
        return []
    previous = None
    if number > 1 and len(rounds) >= number - 1:
        previous = rounds[number - 2].get("delivery_digest")
    expected = {
        "builder_session_id": session_id,
        "submission_number": number,
        "revision_of_candidate_digest": previous,
        "feedback_digest": feedback_digest if number > 1 else None,
    }
    errors: list[str] = []
    for key, value in expected.items():
        if key not in report:
            errors.append(f"run_report.json is missing readiness field {key}; it must be {value!r}")
        elif report[key] != value:
            errors.append(f"run_report.json {key} is {report[key]!r}; it must be exactly {value!r}"
                          + (" -- this is round.delivery_digest from the 200 response to your accepted"
                             " previous submission, not patch_sha256 and not candidate_digest"
                             if key == "revision_of_candidate_digest" and value is not None else ""))
    if number > 1:
        try:
            edit = read_json(delivery / "edit_report.json")
        except Exception:
            edit = {}
        if not isinstance(edit, dict) or not isinstance(edit.get("feedback_response"), str) \
                or not edit["feedback_response"].strip():
            errors.append("edit_report.json must carry feedback_response: your own prose explaining"
                          " how this revision answers the feedback")
    return errors


def validate_delivery(root: Path) -> list[str]:
    errors: list[str] = []
    if not root.is_dir():
        return ["submission directory is missing"]
    entries = list(root.iterdir())
    if any(path.is_symlink() or not path.is_file() for path in entries):
        errors.append("submission top level must contain regular files only")
    names = {path.name for path in entries}
    errors.extend(f"missing {name}" for name in sorted(DELIVERY_FILES - names))
    errors.extend(f"unexpected top-level artifact {name}" for name in sorted(names - DELIVERY_FILES))
    if errors:
        return errors
    patch = root / "solution.patch"
    try:
        text = patch.read_text(encoding="utf-8")
    except (OSError, UnicodeError) as exc:
        return [f"solution.patch is unreadable UTF-8: {type(exc).__name__}"]
    paths = patch_paths(text)
    if not text.strip() or not paths:
        errors.append("solution.patch is empty or has no changed paths")
    try:
        edit = read_json(root / "edit_report.json")
    except Exception as exc:
        edit = {}
        errors.append(f"edit_report.json is invalid: {type(exc).__name__}")
    required_edit = {
        "schema_version": str, "feature_summary": str, "changed_paths": list,
        "commands_run": list, "compatibility_notes": list, "limitations": list,
    }
    for field, expected in required_edit.items():
        if not isinstance(edit.get(field), expected):
            errors.append(f"edit_report.json field {field} has the wrong type")
    if edit.get("schema_version") != "1.0":
        errors.append("edit_report.json schema_version must be 1.0")
    changed = edit.get("changed_paths")
    if isinstance(changed, list) and (any(not isinstance(item, str) for item in changed) or sorted(changed) != paths):
        errors.append("edit_report.json changed_paths must exactly match solution.patch")
    for field in ("commands_run", "compatibility_notes", "limitations"):
        value = edit.get(field)
        if isinstance(value, list) and any(not isinstance(item, str) for item in value):
            errors.append(f"edit_report.json {field} must contain strings only")
    try:
        report = read_json(root / "run_report.json")
    except Exception as exc:
        report = {}
        errors.append(f"run_report.json is invalid: {type(exc).__name__}")
    if report.get("status") not in {"success", "failure"}:
        errors.append("run_report.json status must be success or failure")
    artifacts = report.get("artifact_paths")
    if not isinstance(artifacts, list) or any(not isinstance(item, str) for item in artifacts):
        errors.append("run_report.json artifact_paths must be a string array")
    elif sorted(artifacts) != paths:
        errors.append("run_report.json artifact_paths must exactly match solution.patch")
    report_errors = report.get("errors")
    if not isinstance(report_errors, list) or any(not isinstance(item, str) for item in report_errors):
        errors.append("run_report.json errors must be a string array")
    for field in ("runtime_seconds", "peak_memory_mb"):
        value = report.get(field)
        if not isinstance(value, (int, float)) or isinstance(value, bool) or value < 0:
            errors.append(f"run_report.json {field} must be non-negative")
    providers = report.get("providers")
    if not isinstance(providers, dict) or set(providers) != PROVIDER_COUNTERS:
        errors.append("run_report.json providers must contain the exact declared counters")
    elif any(not isinstance(value, int) or isinstance(value, bool) or value < 0 for value in providers.values()):
        errors.append("run_report.json provider counters must be non-negative integers")
    return errors


def start_broker(*, name: str, script: Path, credential: Path, port: int,
                 image: str, cidfile: Path, provider_url: str | None = None,
                 evaluator_proxy_url: str = '', defer_removal: bool = False) -> None:  # direct egress by default
    if script.resolve() == BUILDER_BROKER_SCRIPT:
        sys.path.insert(0, '@@AGENTSWE_EDITING_CONTROL@@')
        import builder_broker_runtime
        return builder_broker_runtime.start_builder_broker(name=name, credential=credential,
            image=image, port=port, cidfile=cidfile)
    if script.resolve() == JUDGE_BROKER_SCRIPT:
        return _judge_runtime().start_judge_broker(name=name, credential=credential, image=image,
            port=port, cidfile=cidfile, upstream=provider_url or _judge_runtime().DEFAULT_UPSTREAM,
            defer_removal=defer_removal)
    cidfile.parent.mkdir(parents=True, exist_ok=True)
    cidfile.unlink(missing_ok=True)
    command = [
        "docker", "run", "-d", "--rm", "--name", name, "--network", "host",
        "-v", f"{script.resolve()}:/broker.py:ro",
        "-v", f"{ROOT / 'agentloop/evaluator/responses_stream.py'}:/responses_stream.py:ro",
        "-e", "AGENTSWE_EVALUATOR_PROXY_URL=" + evaluator_proxy_url,
        "-v", f"{credential.resolve()}:/run/secrets/agentswe.env:ro",
        "-v", "/etc/ssl/certs:/etc/ssl/certs:ro",
        "-e", "SSL_CERT_FILE=/etc/ssl/certs/ca-certificates.crt",
        "--cidfile", str(cidfile.resolve()),
        image, "python3", "/broker.py", "--credential-file", "/run/secrets/agentswe.env",
    ]
    if provider_url is not None:
        command.extend(["--provider-url", provider_url])
    command.extend(["--bind", "127.0.0.1", "--port", str(port)])
    completed = subprocess.run(command, text=True, capture_output=True, check=False)
    if completed.returncode:
        raise RuntimeError("broker_container_start_failure: " + completed.stderr[-1200:])
    endpoint = f"http://127.0.0.1:{port}/v1/responses"
    last_error = "no response"
    for _ in range(60):
        try:
            health = broker_health(endpoint)
            if health.get("ok") is True or health.get("status") == "ok":
                return
        except Exception as exc:
            last_error = f"{type(exc).__name__}: {exc}"
        time.sleep(0.5)
    raise RuntimeError("broker_health_failure: " + last_error)


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


def cleanup_owned_container(role: str, cidfile: Path, *, attempted: bool) -> dict[str, Any]:
    container_id = read_container_id(cidfile)
    result: dict[str, Any] = {
        "role": role, "cidfile": str(cidfile), "container_id": container_id,
        "startup_attempted": attempted, "ownership_proven": container_id is not None,
        "cleanup_attempted": container_id is not None, "absent_after_cleanup": False,
    }
    if not attempted:
        result.update({"status": "not_started", "absent_after_cleanup": True})
        return result
    if container_id is None:
        result.update({"status": "ownership_unproven", "cleanup_error": "Docker startup was attempted but no current-run container ID was captured"})
        return result
    removed = subprocess.run(["docker", "rm", "-f", container_id], text=True,
                             capture_output=True, check=False)
    if removed.returncode:
        result["remove_stderr"] = (removed.stderr or "")[-500:]
    if removal_in_progress(removed):
        result["removal_in_progress_at_rm"] = True
        await_daemon_removal(container_id)
    inspected = subprocess.run(["docker", "inspect", container_id], text=True,
                               capture_output=True, check=False)
    inspect_text = ((inspected.stdout or "") + (inspected.stderr or "")).strip()
    inspect_lower = inspect_text.lower()
    absent = inspected.returncode != 0 and ("no such object" in inspect_lower or "no such container" in inspect_lower)
    result.update({"remove_exit_code": removed.returncode, "inspect_exit_code": inspected.returncode,
                   "absent_after_cleanup": absent, "status": "absent" if absent else "cleanup_unverified"})
    if not absent:
        result["cleanup_error"] = inspect_text[-1000:] or "docker inspect did not prove absence"
    return result


def cleanup_run_mounted_containers(run_dir: Path, excluded_names: set[str]) -> list[dict[str, Any]]:
    """Remove only containers whose mount sources are inside this new run."""
    listed = subprocess.run(["docker", "ps", "-aq"], text=True, stdout=subprocess.PIPE,
                            stderr=subprocess.PIPE, check=False)
    results: list[dict[str, Any]] = []
    if listed.returncode:
        return [{"classification": "docker_inventory_failure", "error_detail": listed.stderr[-500:]}]
    root = run_dir.resolve()
    for container_id in listed.stdout.split():
        inspected = subprocess.run(["docker", "inspect", container_id], text=True,
                                   stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=False)
        if inspected.returncode:
            continue
        try:
            values = json.loads(inspected.stdout)
            value = values[0] if isinstance(values, list) and values else {}
            name = str(value.get("Name", "")).lstrip("/")
            mounts = value.get("Mounts", []) if isinstance(value, dict) else []
            sources = [Path(str(item.get("Source"))).resolve() for item in mounts
                       if isinstance(item, dict) and isinstance(item.get("Source"), str)]
        except (ValueError, json.JSONDecodeError, OSError):
            continue
        if name in excluded_names:
            continue
        if not any(source == root or root in source.parents for source in sources):
            continue
        removed = subprocess.run(["docker", "rm", "-f", container_id], text=True,
                                 stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=False)
        in_progress = removal_in_progress(removed)
        if in_progress:  # an exited --rm product container the daemon is still removing
            await_daemon_removal(container_id)
        verify = subprocess.run(["docker", "inspect", container_id], text=True,
                                stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=False)
        results.append({"container_id": container_id, "container_name": name,
                        "matched_run_mount": True, "remove_exit_code": removed.returncode,
                        **({"removal_in_progress_at_rm": True} if in_progress else {}),
                        "absent_after_cleanup": verify.returncode != 0})
    return results


class BuilderLifecycle:
    """Evaluator-owned submit channel for one uninterrupted Harbor Builder."""

    def __init__(self, *, run_dir: Path, workspace: Path, public_endpoint: str,
                 pilot_not_formal: bool = False, max_dev_rounds: int = 10,
                 readiness_profile: str | None = None, current_binding: dict[str, Any] | None = None) -> None:
        token = hashlib.sha256(f"{run_dir}:{time.time_ns()}".encode()).hexdigest()
        self.run_dir = run_dir
        self.workspace = workspace
        self.token = token
        self.session_id: str | None = None
        self.native_observations: list[dict[str, Any]] = []
        self.connection_id = f"claude-builder-channel-{hashlib.sha256((token + ':channel').encode()).hexdigest()[:16]}"
        self.socket_path = Path(tempfile.gettempdir()) / f"claude-builder-{token[:12]}.sock"
        self.server: socketserver.ThreadingUnixStreamServer | None = None
        self.events: list[dict[str, Any]] = []
        self.validation_count = 0
        self.feedback_path: Path | None = None
        self.feedback_digest: str | None = None
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
        self.pilot_not_formal = pilot_not_formal
        self.readiness_profile = readiness_profile
        self.current_binding = current_binding
        if readiness_profile not in (None, READINESS_PROFILE):
            raise ValueError("unsupported readiness profile")
        if readiness_profile and not pilot_not_formal:
            raise ValueError("readiness profile requires pilot_not_formal")
        if not 1 <= max_dev_rounds <= 10:
            raise ValueError("max_dev_rounds must be between 1 and 10")
        self.max_dev_rounds = max_dev_rounds
        self.max_submissions = max_dev_rounds
        public_case_ids = ("dev_001",) if readiness_profile else DEV_CASES
        hidden_case_ids = ("test_001",) if pilot_not_formal else CASE_IDS
        self.controller = Controller(
            repository=ROOT / "input/repository",
            cases=ROOT / "agentloop/cases",
            run_dir=run_dir / "lifecycle",
            broker_endpoint=public_endpoint,
            public_case_ids=public_case_ids,
            hidden_case_ids=hidden_case_ids,
            pilot_not_formal=pilot_not_formal,
            max_dev_rounds=max_dev_rounds,
            current_binding=current_binding,
            readiness_profile=readiness_profile,
        )

    def event(self, event: str, **fields: Any) -> None:
        value = {"event": event, "at": now(), "epoch_ns": time.time_ns(),
                 "builder_session_id": self.session_id,
                 "builder_connection_id": self.connection_id, **fields}
        with self.lock:
            with (self.run_dir / 'builder_observer_events.jsonl').open('a') as handle:
                handle.write(json.dumps(value, ensure_ascii=False) + "\n")
                handle.flush()
                os.fsync(handle.fileno())
            self.events.append(value)

    def bind_native_thread(self) -> None:
        observed = observe_thread(self.run_dir, self.native_observations)
        if self.session_id is not None and self.session_id != observed['thread_id']:
            raise NativeEvidenceError('builder_session_changed_between_submissions')
        self.session_id = observed['thread_id']
        self.native_observations.append(observed)
        self.event('native_thread_observed', source=observed)

    @staticmethod
    def _feedback_file_bytes(feedback: dict[str, Any]) -> bytes:
        """Re-encode a visible feedback object exactly as its file was written.

        agentloop/protocol.py:write_json is the only writer of the
        authoritative feedback file, and builder_protocol.feedback_digest is
        sha256 of that file, so this encoding is the one whose hash the digest
        is. public_payload is idempotent -- the file was already written from
        public_payload(value), every key it kept is in PUBLIC_FIELDS and
        public_text leaves an already-scrubbed string alone -- so the visible
        copy re-encodes to the same bytes.
        """
        return (json.dumps(feedback, indent=2, ensure_ascii=False, sort_keys=True) + "\n").encode("utf-8")

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

    def feedback_written(self, payload: dict[str, Any]) -> None:
        """Receipt every feedback the socket write that just returned carried."""
        for record in self._delivered_records(payload):
            if not self._carries_feedback(record):
                continue
            number = record['submission_number']
            digest = record['feedback_digest']
            payload_sha256 = hashlib.sha256(
                json.dumps(record, sort_keys=True, ensure_ascii=False).encode()).hexdigest()
            with self.feedback_ledger_lock:
                self.undelivered_feedback.pop(number, None)
                seen = any(row.get('event') == 'feedback_delivered'
                           and row.get('candidate_number') == number
                           and row.get('feedback_digest') == digest
                           and row.get('payload_sha256') == payload_sha256 for row in self.events)
            if seen:
                continue
            self.event('feedback_delivered', candidate_number=number,
                feedback_digest=digest, duplicate_digest=record.get('duplicate') is True,
                evidence='socket_write_and_flush_completed',
                payload_sha256=payload_sha256)

    def feedback_write_failed(self, payload: dict[str, Any], exc: BaseException) -> None:
        """A ready response the Builder's socket never took.

        This is the opposite of a receipt: it records that delivery did not
        happen, and keeps the response deliverable by a later connection.
        """
        with self.feedback_ledger_lock:
            numbers = []
            receipted = {event.get('candidate_number') for event in self.events
                         if event.get('event') == 'feedback_delivered'}
            for record in self._delivered_records(payload):
                if not self._carries_feedback(record):
                    continue
                number = record['submission_number']
                numbers.append(number)
                if number not in receipted:
                    self.undelivered_feedback[number] = record
            self.feedback_write_failures.append({'at': now(), 'error': f"{type(exc).__name__}: {exc}",
                'candidate_numbers': numbers, 'builder_session_id': self.session_id})
            write_json(self.run_dir / 'native_feedback_write_failures.json', {
                'schema_version': 'claude-feedback-delivery-ledger/v1',
                'write_failures': self.feedback_write_failures,
                'undelivered_candidate_numbers': sorted(self.undelivered_feedback)})

    def _snapshot(self, destination: Path) -> Path:
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copytree(self.workspace, destination, symlinks=True)
        return destination

    def _preflight(self, *, trigger: str) -> dict[str, Any]:
        self.validation_count += 1
        number = len(self.controller.rounds) + 1
        attempt_dir = self.run_dir / "validations" / f"candidate_{number:03d}_attempt_{self.validation_count:03d}"
        delivery = self._snapshot(attempt_dir / "delivery")
        errors = validate_delivery(delivery)
        if self.readiness_profile:
            # Preflight is the last point where a wrong digest is still cheap:
            # it consumes no round, and delivery_errors is what the Builder acts
            # on. Discovered at bundle export instead, it costs the whole run.
            errors.extend(readiness_delivery_errors(
                delivery, number, self.session_id, self.controller.rounds, self.feedback_digest))
        result: dict[str, Any] = {
            "schema_version": "agentswe-claude-builder-preflight/v1",
            "candidate_number": number,
            "trigger": trigger,
            "submission_consumed": False,
            "delivery_digest": delivery_digest(delivery),
            "delivery_errors": errors,
        }
        if not errors:
            try:
                build = materialize(ROOT / "input/repository", delivery / "solution.patch", attempt_dir / "candidate")
            except (OSError, ValueError, RuntimeError, subprocess.SubprocessError) as exc:
                infrastructure = isinstance(exc, (OSError, subprocess.SubprocessError)) or type(exc).__name__ == "BuildInfrastructureError"
                result.update(classification="evaluator_infrastructure_failure" if infrastructure else "candidate_build_failure", ready_for_submission=False,
                              error_type=type(exc).__name__, error_detail=str(exc)[-1000:])
            else:
                result.update(classification="ready_for_lower", ready_for_submission=True,
                              build=build, candidate_digest=build["candidate_digest"],
                              patch_sha256=build["patch_sha256"])
        else:
            result.update(classification="candidate_preflight_failure", ready_for_submission=False)
        path = attempt_dir / "preflight_result.json"
        write_json(path, result)
        result["preflight_result"] = str(path)
        result["delivery_snapshot"] = str(delivery)
        self.event("candidate_preflight", candidate_number=number, trigger=trigger,
                   classification=result["classification"],
                   ready_for_submission=result["ready_for_submission"],
                   submission_consumed=False, preflight_result=str(path))
        return result

    def validate(self) -> tuple[int, dict[str, Any]]:
        with self.lock:
            return self._validate_locked()

    def _validate_locked(self) -> tuple[int, dict[str, Any]]:
        if self.controller.frozen:
            return 409, {"classification": "lifecycle_frozen", "error": "the final Candidate is already frozen"}
        result = self._preflight(trigger="builder_request")
        return (200 if result["ready_for_submission"] else 422), result

    def _write_witness(self, submissions: list[dict[str, Any]], *, consumed: bool) -> Path:
        witness: dict[str, Any] = {
            "schema_version": "agentswe-builder-session-witness/v1",
            "model": BUILDER_MODEL,
            "reasoning_effort": BUILDER_EFFORT,
            "single_connection": True,
            "transport": "evaluator-owned-single-session",
            "session_id": self.session_id,
            "connection_id": self.connection_id,
            "builder_exit_code": None,
            "feedback_consumed": consumed,
            "feedback_digest": self.feedback_digest if consumed else None,
            "latest_feedback_digest": self.feedback_digest if consumed else None,
            "submissions": submissions,
        }
        path = self.run_dir / "builder_session_witness.json"
        write_json(path, witness)
        load_and_verify_witness(path, expected_submissions=len(submissions))
        return path

    def _create_feedback(self, round_record: dict[str, Any]) -> tuple[Path, str]:
        public_cases: list[dict[str, Any]] = []
        for item in round_record.get("dev", []):
            if not isinstance(item, dict):
                continue
            events = item.get("trajectory") if isinstance(item.get("trajectory"), list) else []
            public_cases.append({
                "case_id": item.get("case_id"),
                "classification": item.get("classification"),
                "candidate_observation": item.get("answer", {}).get("decision") if isinstance(item.get("answer"), dict) else None,
                "trajectory_event_kinds": [event.get("kind") for event in events if isinstance(event, dict)],
                "artifact_present": isinstance(item.get("artifact"), dict),
                "score": item.get("score"), "score_kind": item.get("score_kind"),
                "result_judge_feedback": semantic_feedback(item.get("result_judge_contract")),
            })
        value = {
            "schema_version": "agentswe-edit-feedback/v1",
            "builder_session_id": self.session_id,
            "connection_id": self.connection_id,
            "candidate_number": round_record["submission_number"],
            "candidate_digest": round_record["candidate_digest"],
            # The value the next round must echo as revision_of_candidate_digest.
            # Handed over here because the Builder reliably copies fields out of
            # this object and reliably fails to extract the same value from the
            # earlier submit response.
            "revision_of_candidate_digest": round_record.get("delivery_digest"),
            "public_inventory": list(self.controller.public_case_ids),
            "public_cases": public_cases,
            "summary": (
                "Revise the Claude policy-provenance plugin using the observed public trajectory and product artifact."
                if self.pilot_not_formal else
                "Revise the Claude policy-provenance plugin using the two observed public trajectories and product artifacts."
            ),
            "oracle_included": False,
            "native_suite_used_as_result": False,
            "dev_score": round_record.get("dev_score"), "dev_passed": round_record.get("dev_passed"),
        }
        number = int(round_record["submission_number"])
        path = self.run_dir / "feedback" / f"candidate_{number:03d}_feedback.json"
        write_json(path, public_payload(value))
        return path, feedback_digest(path)

    def _blocked_product_payload(self, digest):
        attempt = self.controller.lookup_product_attempt(digest)
        if attempt is None:
            return None
        return {"classification": "product_execution_already_attempted", "candidate_digest": digest,
            "state": attempt['state'], "accepted": False, "duplicate": True,
            "submission_consumed": False, "round_consumed": False, "retry_allowed": False,
            "error": "This product already has an execution intent. Its completed and unknown results are retained. Do not resubmit the same product; no model requests will be repeated.",
            "public_cases": [{"case_id": row.get('case_id'), "classification": row.get('classification'),
                "score": row.get('score'), "score_kind": row.get('score_kind'),
                "result_judge_feedback": semantic_feedback(row.get('result_judge_contract'))}
                for row in attempt['case_results'] if isinstance(row, dict)]}

    def submit(self, acknowledged_feedback_digest: str | None) -> tuple[int, dict[str, Any]]:
        with self.lock:
            return self._submit_locked(acknowledged_feedback_digest)

    def _submit_locked(self, acknowledged_feedback_digest: str | None) -> tuple[int, dict[str, Any]]:
        delivery_digest = candidate_tree_digest(self.workspace)
        for record in self.accepted_records():
            saved_digest = record.get('delivery_digest')
            if saved_digest is None and Path(str(record.get('delivery_path', ''))).is_dir():
                saved_digest = candidate_tree_digest(Path(record['delivery_path']))
            if saved_digest == delivery_digest:
                return 200, self._duplicate_payload(record)
        if self.controller.frozen:
            return 409, {"classification": "lifecycle_frozen", "error": "the final Candidate is already frozen"}
        number = len(self.controller.rounds) + 1
        if number > self.max_submissions:
            return 409, {"classification": "submission_limit_reached",
                         "error": "max_dev_rounds accepted submissions reached"}
        if number == 1 and acknowledged_feedback_digest is not None:
            return 422, {"classification": "feedback_protocol_failure", "error": "Candidate 1 cannot acknowledge feedback"}
        if number > 1 and acknowledged_feedback_digest != self.feedback_digest:
            return 422, {"classification": "feedback_protocol_failure",
                         "error": "a revised Candidate must acknowledge the exact latest evaluator feedback digest",
                         "expected_feedback_digest": self.feedback_digest}
        self.bind_native_thread()
        preflight = self._preflight(trigger="submission_gate")
        if not preflight["ready_for_submission"]:
            self.event("submission_rejected_preflight", candidate_number=number,
                       classification=preflight["classification"], submission_consumed=False)
            return 422, preflight
        digest = str(preflight["candidate_digest"])
        duplicate = next((record for record in self.accepted_records()
                          if digest == record.get('candidate_digest')), None)
        if duplicate is not None:
            return 200, self._duplicate_payload(duplicate)
        blocked = self._blocked_product_payload(digest)
        if blocked is not None:
            self.event('submission_replay_blocked', candidate_number=number, candidate_digest=digest,
                       classification=blocked['classification'], submission_consumed=False)
            return 409, blocked
        source_delivery = Path(str(preflight["delivery_snapshot"]))
        accepted_delivery = self.run_dir / "candidates" / f"submission_{number:03d}"
        self._snapshot_from(source_delivery, accepted_delivery)
        patch = accepted_delivery / "solution.patch"
        submissions = [
            {
                "submission_number": int(record["submission_number"]),
                "session_id": self.session_id,
                "connection_id": self.connection_id,
                "patch": str(Path(str(record["delivery_path"])) / "solution.patch"),
                "candidate_digest": record["candidate_digest"],
                **({"feedback_digest": record["feedback"]["feedback_digest"]}
                   if int(record["submission_number"]) > 1 and isinstance(record.get("feedback"), dict) else {}),
            }
            for record in self.accepted_records()
        ]
        submissions.append({
            "submission_number": number, "session_id": self.session_id,
            "connection_id": self.connection_id, "patch": str(patch),
            "candidate_digest": digest,
            **({"feedback_digest": self.feedback_digest} if number > 1 else {}),
        })
        witness_path = self._write_witness(submissions, consumed=number > 1)
        self.controller.feedback_record_path = self.feedback_path
        self.controller.bind_builder_witness(witness_path, expected_submissions=number)
        self.event("submission_started", candidate_number=number, candidate_digest=digest,
                   feedback_digest_ack=acknowledged_feedback_digest)
        try:
            round_record = self.controller.submit(patch, number)
        except Exception as exc:
            shutil.rmtree(accepted_delivery, ignore_errors=True)
            self.event("submission_not_consumed", candidate_number=number,
                       classification="public_or_launcher_infrastructure_failure",
                       error_type=type(exc).__name__, error_detail=str(exc)[-1000:])
            blocked = self._blocked_product_payload(digest)
            if blocked is not None:
                return 503, blocked
            return 503, {"classification": "public_or_launcher_infrastructure_failure",
                         "error_type": type(exc).__name__, "error_detail": str(exc)[-1000:],
                         "submission_consumed": False, "preflight": preflight}
        round_record["feedback_digest_ack"] = acknowledged_feedback_digest
        round_record["delivery_path"] = str(accepted_delivery)
        round_record['delivery_digest'] = candidate_tree_digest(accepted_delivery)
        write_json(self.run_dir / "lifecycle" / f"round_{number:03d}.json", round_record)
        if not self.controller.frozen:
            self.feedback_path, self.feedback_digest = self._create_feedback(round_record)
            self.controller.feedback_record_path = self.feedback_path
            round_record["evaluator_feedback"] = str(self.feedback_path)
            round_record["evaluator_feedback_digest"] = self.feedback_digest
            write_json(self.run_dir / "lifecycle" / f"round_{number:03d}.json", round_record)
            self.event("feedback_created", candidate_number=number, feedback_digest=self.feedback_digest,
                       feedback_path=str(self.feedback_path))
            if number == self.max_dev_rounds:
                self.controller.freeze_latest("max_dev_rounds")
        self.event("submission_finished", candidate_number=number, candidate_digest=digest,
                   accepted=True, submission_consumed=True,
                   feedback_consumed=number > 1, frozen=bool(self.controller.frozen))
        payload = {"accepted": True, "submission_number": number,
                   "candidate_digest": digest, "round": round_record,
                   "builder_session_id": self.session_id,
                   "builder_connection_id": self.connection_id,
                   "feedback_digest_ack": acknowledged_feedback_digest, "preflight": preflight}
        if self.feedback_path and self.feedback_digest:
            payload.update(feedback=read_json(self.feedback_path), feedback_digest=self.feedback_digest)
        return 200, payload

    def _duplicate_payload(self, record: dict[str, Any]) -> dict[str, Any]:
        number = int(record['submission_number'])
        path = self.run_dir / 'feedback' / f'candidate_{number:03d}_feedback.json'
        return {'accepted': True, 'duplicate': True, 'submission_consumed': False,
                'round_consumed': False, 'submission_number': number,
                'candidate_digest': record.get('candidate_digest'), 'round': record,
                'builder_session_id': self.session_id, 'builder_connection_id': self.connection_id,
                'feedback': read_json(path) if path.is_file() else None,
                'feedback_digest': feedback_digest(path) if path.is_file() else None}

    def finalize_witness(self, builder_exit_code: int) -> None:
        """Record the real Harbor exit after the live session has ended."""
        path = self.run_dir / "builder_session_witness.json"
        if not path.is_file():
            return
        witness = read_json(path)
        witness["builder_exit_code"] = builder_exit_code
        write_json(path, witness)
        count = len(witness.get("submissions", []))
        if 1 <= count <= self.max_dev_rounds and builder_exit_code == 0:
            load_and_verify_witness(path, expected_submissions=count)

    @staticmethod
    def _snapshot_from(source: Path, destination: Path) -> None:
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copytree(source, destination, symlinks=False)

    def accepted_records(self) -> list[dict[str, Any]]:
        records: list[dict[str, Any]] = []
        for record in self.controller.rounds:
            enriched = dict(record)
            if "delivery_path" not in enriched:
                path = self.run_dir / "candidates" / f"submission_{int(record['submission_number']):03d}"
                enriched["delivery_path"] = str(path)
            records.append(enriched)
        return records

    def _record_feedback_path(self, record: dict[str, Any]) -> Path:
        path = Path(str(record.get("evaluator_feedback") or ""))
        if path.is_file():
            return path
        number = int(record.get("submission_number") or 0)
        return self.run_dir / "feedback" / f"candidate_{number:03d}_feedback.json"

    def _with_feedback(self, record: dict[str, Any]) -> dict[str, Any]:
        """Attach one accepted record's own authoritative feedback and digest.

        Only the latest feedback is published at top level, so a response that
        names an earlier candidate could never carry the bytes whose digest it
        names. Reading each record's own file makes every accepted round
        recoverable, and makes a receipt minted from this response provable.
        The digest is the file's sha256, exactly the value the record and the
        next submission's feedback_digest_ack are bound to.
        """
        path = self._record_feedback_path(record)
        if not path.is_file():
            return record
        return {**record, "feedback": read_json(path), "feedback_digest": feedback_digest(path)}

    def feedback(self) -> tuple[int, dict[str, Any]]:
        """Re-deliver one accepted record whose earlier response was lost.

        This re-reads the same authoritative feedback the accepted submission
        already carries; it issues nothing new and consumes no round. The
        oldest response a failed write left undelivered is preferred.
        """
        records = self.accepted_records()
        if not records:
            return 404, {"error": "no accepted submission has feedback yet"}
        pending = [n for n in sorted(self.undelivered_feedback) if 1 <= n <= len(records)]
        chosen = records[(pending[0] if pending else len(records)) - 1]
        return 200, {"builder_session_id": self.session_id,
            "builder_connection_id": self.connection_id,
            "records": [self._with_feedback(chosen)],
            "frozen": self.controller.frozen}

    def status(self) -> tuple[int, dict[str, Any]]:
        with self.lock:
            return 200, {
                "builder_session_id": self.session_id,
                "builder_connection_id": self.connection_id,
                "records": [self._with_feedback(record) for record in self.accepted_records()],
                "blocked_products": [self._blocked_product_payload(digest)
                    for digest in sorted(self.controller.known_product_digests())
                    if digest not in {row['candidate_digest'] for row in self.controller.rounds}],
                "feedback": read_json(self.feedback_path) if self.feedback_path and self.feedback_path.is_file() else None,
                "feedback_digest": self.feedback_digest,
                "frozen": self.controller.frozen,
            }

    def close(self) -> None:
        if self.server:
            self.server.shutdown()
            self.server.server_close()
        self.socket_path.unlink(missing_ok=True)


def start_submission_server(lifecycle: BuilderLifecycle) -> None:
    class Handler(socketserver.StreamRequestHandler):
        def handle(self) -> None:
            try:
                request = json.loads(self.rfile.readline(1 << 20))
                if request.get("token") != lifecycle.token:
                    code, payload = 401, {"classification": "controller_auth_failure", "error": "unauthorized"}
                elif request.get("action") == "validate":
                    code, payload = lifecycle.validate()
                elif request.get("action") == "submit":
                    code, payload = lifecycle.submit(request.get("feedback_digest"))
                elif request.get("action") == "status":
                    code, payload = lifecycle.status()
                elif request.get("action") == "feedback":
                    code, payload = lifecycle.feedback()
                else:
                    code, payload = 400, {"classification": "controller_protocol_failure", "error": "unknown action"}
            except Exception as exc:
                code, payload = 500, {"classification": "evaluator_controller_failure",
                                      "error_type": type(exc).__name__, "error_detail": str(exc)[-1000:]}
            visible = public_payload(payload)
            try:
                self.wfile.write(json.dumps({"status": code, "payload": visible}, ensure_ascii=False).encode() + b"\n")
                self.wfile.flush()
            except OSError as exc:
                # The Builder's helper was killed, or its shell tool timed out,
                # while the evaluator was still answering. Nothing arrived, so
                # nothing is receipted: record the failed write and keep the
                # ready response deliverable by a later --status or --feedback
                # connection.
                if code == 200:
                    lifecycle.feedback_write_failed(visible, exc)
                return
            if code == 200:
                lifecycle.feedback_written(visible)

    class Server(socketserver.ThreadingUnixStreamServer):
        daemon_threads = True

    lifecycle.socket_path.unlink(missing_ok=True)
    lifecycle.server = Server(str(lifecycle.socket_path), Handler)
    lifecycle.socket_path.chmod(stat.S_IRUSR | stat.S_IWUSR)
    threading.Thread(target=lifecycle.server.serve_forever, daemon=True).start()


SUBMIT_WAITING_INSTRUCTION = """

## Waiting for an evaluation, and recovering a lost response

A submission runs the public dev cases and can take far longer than one shell
tool call, so `submit_dev_candidate` hands the submit to a detached helper and
returns early instead of holding one long command. If it prints status 202 with
state submission_in_progress, run `submit_dev_candidate --await-only` again, as
many times as needed, until it prints the real response. Never wrap the command
in `nohup`, never background or `disown` it, and never kill it.

`submit_dev_candidate --feedback` answers at any time, including while an
evaluation is still running, and reprints one accepted record's full feedback
and its exact digest. Once the evaluation in progress has answered,
`submit_dev_candidate --status` prints every accepted record with its own full
feedback and digest. If a response is ever lost, recover it with one of those
two commands; do not resubmit an unchanged Candidate.
"""


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

p = argparse.ArgumentParser(prog=os.path.basename(__file__))
p.add_argument("--status", nargs="?", const="", default=None, metavar="SUBMISSION_NUMBER",
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


if a.status is not None:
    finish(call({"action": "status"}))
if a.feedback:
    finish(call({"action": "feedback"}))
if a.validate or os.path.basename(__file__) == "validate_dev_candidate":
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
            if a.feedback_digest is not None:
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


def stage_public_package(run_dir: Path) -> tuple[Path, dict[str, Any]]:
    public = run_dir / "builder_public_package"
    manifest = stage_public(ROOT, public)
    return public, manifest


def restrict_public_package_to_pilot(public: Path, manifest: dict[str, Any]) -> dict[str, Any]:
    """Retain both public dev cases for the readiness pilot."""
    if not all((public / "dev_cases" / case).is_dir() for case in DEV_CASES):
        raise ValueError("pilot requires both public dev cases")
    value = dict(manifest)
    value.update({
        "pilot_not_formal": True,
        "public_case_inventory": list(DEV_CASES),
        "hidden_oracle_mounted": False,
        "evaluator_source_mounted": False,
        "credential_mounted": False,
    })
    write_json(public / "PUBLIC_PACKAGE_MANIFEST.json", value)
    return value


def restrict_public_package_to_readiness(public: Path, manifest: dict[str, Any]) -> dict[str, Any]:
    """Expose only dev_001 for v2 readiness while preserving no hidden material."""
    extra = public / "dev_cases" / "dev_002"
    if extra.exists():
        shutil.rmtree(extra)
    if not (public / "dev_cases" / "dev_001").is_dir():
        raise ValueError("readiness requires public dev_001")
    value = dict(manifest)
    value.update({
        "pilot_not_formal": True,
        "readiness_profile": READINESS_PROFILE,
        "public_case_inventory": ["dev_001"],
        "hidden_case_inventory": ["test_001"],
        "hidden_oracle_mounted": False,
        "evaluator_source_mounted": False,
        "credential_mounted": False,
    })
    write_json(public / "PUBLIC_PACKAGE_MANIFEST.json", value)
    return value


def builder_config(*, run_dir: Path, public: Path, workspace: Path,
                   lifecycle: BuilderLifecycle, provider_config: Path,
                   image: str, pilot_not_formal: bool = False,
                   readiness_profile: str | None = None) -> Path:
    task = run_dir / "builder_task"
    (task / "environment").mkdir(parents=True)
    (task / "tests").mkdir(parents=True)
    (task / "task.toml").write_text(
        f'''schema_version = "1.4"
[task]
name = "local/claude-policy-provenance-agentloop-builder"
version = "1.0.0"
description = "Claude policy provenance same-session feedback lifecycle"
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
workdir = "/workspace"
[environment.healthcheck]
command = "python3 /usr/local/bin/builder_resource_gate.py"
timeout_sec = 10
retries = 1
interval_sec = 1
''', encoding="utf-8")
    instruction = f"""# Claude policy-provenance Edit Builder — repeated feedback closure

You are the one upper Builder session. Read the four files under
`/builder-package/input`, the public repository, and both complete public dev
cases. Never inspect hidden cases, evaluator source, credentials, prior runs,
or Candidate snapshots.

Work only in `/workspace/submission`. Maintain exactly `solution.patch`,
`edit_report.json`, and `run_report.json` using the delivery contract. Run
`validate_dev_candidate` until the evaluator-owned patch/build preflight is
ready. Then run `submit_dev_candidate` to submit Candidate 1. The evaluator
runs both public cases against the Candidate's actual Claude policy hook and
inspector and returns structured feedback plus its digest.

Read and use that feedback in this same uninterrupted Builder session. You may
make further genuine revisions, up to {lifecycle.max_dev_rounds} distinct
accepted submissions total. Before every revised submission, regenerate all
three delivery files, validate, ensure the product digest differs from every
accepted Candidate, and run `submit_dev_candidate --feedback-digest
<exact-latest-digest>`. Both dev cases run after every accepted submission.
A dev mean above 60 is feedback only and never freezes automatically. The
evaluator freezes the latest accepted Candidate when you stop or when the
round limit is reached. Hidden cases are unavailable until after freeze.
"""
    if readiness_profile:
        instruction = f"""# Claude policy-provenance Edit Builder — v2 readiness

You are one uninterrupted deepseek-flash/xhigh Builder session. Read the four input files, the public repository, and only dev_001. Never inspect hidden cases, evaluator source, credentials, prior runs, or Candidate snapshots.

Maintain exactly `solution.patch`, `edit_report.json`, and `run_report.json` in `/workspace/submission`. Run `/usr/local/bin/validate_dev_candidate`, then submit Candidate 1 with `/usr/local/bin/submit_dev_candidate`. After receiving the exact evaluator feedback digest, make one genuine revision with a different product digest and submit Candidate 2 with `--feedback-digest <exact-latest-digest>`.

`run_report.json` must additionally carry four readiness fields in both rounds. For Candidate 1: `builder_session_id` (your session id), `submission_number` 1, `revision_of_candidate_digest` null, `feedback_digest` null. For Candidate 2: the same `builder_session_id`, `submission_number` 2, `revision_of_candidate_digest` copied verbatim from the feedback object's own `revision_of_candidate_digest` field (the same object you take `feedback_digest` from). It is a fixed value that does not change between your attempts, and it is not the candidate digest or the patch sha256 of what you are about to submit, and `feedback_digest` set to the exact feedback digest you acknowledged. For Candidate 2 only, `edit_report.json` must also carry `feedback_response`: a string of prose that you write, explaining how this revision answers the feedback. It is your explanation, not a copy of the evaluator's feedback object. Complete exactly two accepted dev_001 rounds and stop. The evaluator freezes Candidate 2 and runs test_001 only after freeze; this run is readiness evidence and publishes no formal score.
"""
    elif pilot_not_formal:
        instruction = f"""# Claude policy-provenance Edit Builder — non-formal pilot

You are one uninterrupted upper Builder session. Read the four input files
under `/builder-package/input`, the supplied repository, and both public cases
under `/builder-package/dev_cases`. Never inspect hidden cases, evaluator
source, credentials, prior runs, or Candidate snapshots.

Maintain exactly `solution.patch`, `edit_report.json`, and `run_report.json` in
`/workspace/submission` using the delivery contract. Run
`/usr/local/bin/validate_dev_candidate` until the evaluator-owned patch/build
preflight is ready. Then run `/usr/local/bin/submit_dev_candidate` to submit
Candidate 1. Writing the delivery files or announcing submission does not
submit a Candidate; the command must return the evaluator's structured result.
The evaluator runs the real Claude product on dev_001 and dev_002 and returns
authoritative feedback and its digest for an accepted submission.

Consume each fresh feedback record in this same session. Before each genuine
revision submission, regenerate all three delivery files, validate, ensure the
product digest differs from every accepted Candidate, and run
`/usr/local/bin/submit_dev_candidate --feedback-digest <exact-latest-digest>`.
Continue for up to {lifecycle.max_dev_rounds} accepted submissions. A dev mean
above 60 is feedback only and never freezes automatically. The evaluator
freezes the latest accepted Candidate when the Builder exits or the
accepted-round limit is reached. The evaluator alone runs test_001 once after
freeze. This pilot never creates a formal six-hidden Result or Code score.
"""
    (task / "instruction.md").write_text(instruction + SUBMIT_WAITING_INSTRUCTION, encoding="utf-8")
    submit = task / "environment" / "submit_dev_candidate"
    validate = task / "environment" / "validate_dev_candidate"
    write_submit_helper(submit)
    shutil.copy2(submit, validate)
    validate.chmod(0o755)
    resource_gate = task / "environment" / "builder_resource_gate.py"
    shutil.copy2(ROOT / "harbor" / "builder_resource_gate.py", resource_gate)
    test = task / "tests" / "test.sh"
    test.write_text("printf '0\\n' > /logs/verifier/reward.txt\nexit 0\n", encoding="utf-8"); test.chmod(0o755)
    # Harbor validates CODEX_CONFIG_TOML_PATH on the host before it starts the
    # task container.  Mount the evaluator-owned file at the same absolute
    # path so that the host preflight and the in-container Codex process both
    # resolve one read-only provider configuration.
    provider_target = str(provider_config)
    socket_target = "/run/agentswe/claude-builder-controller.sock"
    write_json(task / "environment" / "docker-compose.yaml", {"services": {"main": {
        # Docker Compose 2.5.0 drops the cpus scalar on this host. Explicit
        # CFS values are observed in the real cgroup before native agent setup.
        "cpu_quota": 800000, "cpu_period": 100000,
        "volumes": [
            {"type": "bind", "source": str(resource_gate), "target": "/usr/local/bin/builder_resource_gate.py", "read_only": True},
            {"type": "bind", "source": str(public), "target": "/builder-package", "read_only": True},
            {"type": "bind", "source": str(workspace), "target": "/workspace/submission"},
            {"type": "bind", "source": str(lifecycle.socket_path), "target": socket_target},
            {"type": "bind", "source": str(submit), "target": "/usr/local/bin/submit_dev_candidate", "read_only": True},
            {"type": "bind", "source": str(validate), "target": "/usr/local/bin/validate_dev_candidate", "read_only": True},
            {"type": "bind", "source": str(provider_config), "target": provider_target, "read_only": True},
        ],
        "environment": {
            "AGENTSWE_DEV_CONTROLLER_SOCKET": socket_target,
            "AGENTSWE_DEV_CONTROLLER_TOKEN": lifecycle.token,
            "AGENTSWE_BUILDER_BROKER_TOKEN": PLACEHOLDER,
        },
    }}})
    config = run_dir / "builder_job_config.json"
    write_json(config, {
        "job_name": f"claude-policy-agentloop-builder-{run_dir.name}",
        "jobs_dir": str(run_dir / "jobs"), "n_attempts": 1,
        "n_concurrent_trials": 1, "quiet": True, "retry": {"max_retries": 0},
        "environment": {"type": "docker", "delete": True,
                        "cpu_enforcement_policy": "limit", "memory_enforcement_policy": "limit"},
        "agents": [{"import_path": "agentswe_codex_resume:CodexResume", "model_name": BUILDER_MODEL,
                    "env": {"CODEX_HOME": "/tmp/agentswe-codex-home",
                            "CODEX_CONFIG_TOML_PATH": provider_target,
                            "AGENTSWE_BUILDER_BROKER_TOKEN": PLACEHOLDER},
                    "kwargs": {"reasoning_effort": BUILDER_EFFORT, "web_search": "live"}}],
        "tasks": [{"path": str(task)}],
    })
    return config


def run_builder(harbor: Path, config: Path, run_dir: Path, timeout: int) -> int:
    with (run_dir / "builder.stdout.log").open("w") as stdout, \
         (run_dir / "builder.stderr.log").open("w") as stderr:
        try:
            result = subprocess.run([str(harbor), "run", "-c", str(config)],
                                    stdout=stdout, stderr=stderr, check=False, timeout=timeout)
            return result.returncode
        except subprocess.TimeoutExpired:
            stderr.write(f"Builder wall-clock timeout after {timeout} seconds\n")
            return 124


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


def run_configured_builder(harbor: Path, config: Path, run_dir: Path, timeout: int,
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
                budget_seconds=timeout, log_mode='w')
        else:
            with execution(config, run_dir, credential, base_url=base_url, proxy=proxy):
                code = builder_segments_runtime().run_builder_segments(
                    run_dir, config, harbor=harbor, observer=observer,
                    budget_seconds=timeout, log_mode='w')
    finally:
        resource_proof = observer.finish()
        # claude observed its Builder container but never cleaned it up. Under
        # readiness the evaluator must be able to attest what it owned, so the
        # resources are retained and attributed here rather than left to Harbor.
        readiness = (run_dir / 'readiness_current_binding.json').is_file()
        # F5 (2026-09-19): readiness_current_binding.json is written only by the
        # pilot path, so formal runs skipped cleanup_owned entirely and left the
        # Builder container for Harbor -- which does not remove it after a kill.
        # Call it always; only readiness defers removal, and only readiness
        # treats an unproven result as fatal, so formal gains a receipt and an
        # actual removal without gaining a new way to fail.
        try:
            cleanup = observer.cleanup_owned(defer_removal=readiness)
        except Exception as exc:  # ownership mismatch must not mask the real error
            cleanup = {'complete': False, 'cleanup_error_type': type(exc).__name__,
                       'cleanup_error': str(exc)[-500:]}
            write_json(run_dir / 'builder_container_cleanup_error.json', cleanup)
        if readiness and not (cleanup.get('retained_terminal') or cleanup.get('nothing_started')):
            code = 125
    return code if resource_proof['valid'] else 125


def save_builder_stats(run_dir: Path, label: str, endpoint: str | None, transport: str) -> None:
    if transport == 'direct':
        from harbor.direct_harbor_builder import native_stats
        write_json(run_dir / f'{label}_native_stats.json', native_stats(run_dir))
    else:
        save_stats(run_dir, label, endpoint)


def public_round_complete(record: dict[str, Any], expected: tuple[str, ...] = DEV_CASES) -> bool:
    dev = record.get("dev")
    if not isinstance(dev, list) or [item.get("case_id") for item in dev if isinstance(item, dict)] != list(expected):
        return False
    return all(isinstance(item, dict) and item.get("accepted", True) is not False and
               isinstance(item.get("broker"), dict) and isinstance(item.get("trajectory"), list)
               and item.get("score_kind") in {"independent_result_rubric", "candidate_zero"}
               and isinstance(item.get("score"), (int, float))
               and not isinstance(item.get("score"), bool)
               for item in dev)


def builder_attestation(lifecycle: BuilderLifecycle, exit_code: int,
                        started_ns: int, finished_ns: int, *,
                        allow_max_rounds_interrupt: bool = False) -> dict[str, Any]:
    records = lifecycle.accepted_records()
    first, second = (records + [{}, {}])[:2]
    feedback_delivered = [event for event in lifecycle.events if event.get("event") == "feedback_delivered"]
    native_records = [{**record,
        'feedback_path': record.get('evaluator_feedback'),
        'feedback_digest': record.get('evaluator_feedback_digest'),
        'feedback_digest_ack': record.get('feedback_digest_ack'),
        'build': {**record.get('build', {}), 'candidate_repo_digest': record.get('candidate_digest')},
    } for record in records]
    # Survivable max_dev_rounds exit (D2, 2026-09-19).  Once the Builder has
    # spent its whole accepted-round budget the evaluator has already frozen the
    # latest Candidate with freeze_reason "max_dev_rounds"; nothing then stops
    # the Builder process, so a non-zero exit afterwards is the outer deadline,
    # not missing evidence.  125 stays fatal: it is this evaluator's own
    # resource/cleanup proof failing, never the Builder running long.
    max_rounds_interrupt = bool(
        allow_max_rounds_interrupt
        and exit_code not in (0, 125)
        and (lifecycle.controller.frozen or {}).get("freeze_reason") == "max_dev_rounds"
        and len(records) >= lifecycle.max_dev_rounds
    )
    native = verify_native(lifecycle.run_dir, native_records, feedback_delivered,
                           lifecycle.native_observations,
                           allow_interrupted=max_rounds_interrupt)

    starts = {int(event["candidate_number"]): event for event in lifecycle.events
              if event.get("event") == "submission_started" and isinstance(event.get("candidate_number"), int)}
    feedback_bound = all(
        isinstance(record.get("feedback"), dict)
        and starts.get(number, {}).get("feedback_digest_ack") == record["feedback"].get("feedback_digest")
        for number, record in enumerate(records[1:], 2)
    )
    all_public_complete = bool(records) and all(
        public_round_complete(record, lifecycle.controller.public_case_ids) for record in records
    )
    digests = [record.get("candidate_digest") for record in records]
    distinct = bool(records) and all(isinstance(digest, str) and digest for digest in digests) and len(set(digests)) == len(digests)
    freeze = lifecycle.controller.frozen
    freeze_latest = bool(records) and isinstance(freeze, dict) and freeze.get("source_submission") == len(records) and freeze.get("candidate_digest") == digests[-1]
    value: dict[str, Any] = {
        "schema_version": "agentswe-claude-builder-session-attestation/v1",
        "builder_session_id": lifecycle.session_id,
        "builder_connection_id": lifecycle.connection_id,
        "builder_model": BUILDER_MODEL,
        "builder_reasoning_effort": BUILDER_EFFORT,
        "builder_exit_code": exit_code,
        "builder_timed_out": exit_code == 124,
        "single_harbor_invocation": native["valid"],
        "single_continuous_session": native["valid"],
        "native_evidence": native,
        "started_epoch_ns": started_ns,
        "finished_epoch_ns": finished_ns,
        "events": lifecycle.events,
        "accepted_rounds": len(records),
        "max_dev_rounds": lifecycle.max_dev_rounds,
        "candidate_records": records,
        "accepted_candidate_count": len(records),
        "candidate_1_and_2": len(records) == 2,
        "candidate_1_public_dev_complete": public_round_complete(first, lifecycle.controller.public_case_ids),
        "candidate_2_public_dev_complete": public_round_complete(second, lifecycle.controller.public_case_ids),
        "all_accepted_public_dev_complete": all_public_complete,
        "feedback_delivered": bool(feedback_delivered),
        "feedback_delivered_count": len(feedback_delivered),
        "feedback_digest": lifecycle.feedback_digest,
        "feedback_consumed": feedback_bound,
        "distinct_candidate_digests": distinct,
        "freeze": freeze,
        "freeze_latest_accepted_candidate": freeze_latest,
        "preflight_attempts": lifecycle.validation_count,
        "public_case_inventory": list(lifecycle.controller.public_case_ids),
        "hidden_case_inventory": list(lifecycle.controller.hidden_case_ids),
        "pilot_not_formal": lifecycle.pilot_not_formal,
    }
    pilot_revision_complete = not lifecycle.pilot_not_formal or len(records) >= 1
    readiness_two_rounds = not lifecycle.readiness_profile or len(records) == 2
    value["max_dev_rounds_interrupt_accepted"] = max_rounds_interrupt
    value["pilot_revision_complete"] = pilot_revision_complete
    value["readiness_two_rounds"] = readiness_two_rounds
    value["readiness_profile"] = lifecycle.readiness_profile
    value["current_binding"] = lifecycle.current_binding
    value["complete"] = all((
        exit_code == 0 or max_rounds_interrupt,
        1 <= len(records) <= lifecycle.max_dev_rounds,
        all_public_complete, feedback_bound, distinct, freeze_latest,
        pilot_revision_complete, readiness_two_rounds, native["valid"],
    ))
    return value


def close_lower_broker_stats(path: Path, cleanup: dict[str, Any] | None) -> None:
    """Record server close in a lower broker's final stats file.

    Only when the live snapshot showed no handler in flight AND the owned container
    is proven absent after cleanup; otherwise the block stays "open" and the
    readiness normalizer refuses the run, which is the honest outcome.
    """
    try:
        value = read_json(path)
    except Exception:
        return
    lifecycle = value.get("lifecycle")
    if (not isinstance(lifecycle, dict) or lifecycle.get("in_flight") != 0
            or not (isinstance(cleanup, dict) and cleanup.get("absent_after_cleanup") is True)):
        return
    lifecycle.update(state="closed", server_close_completed=True,
                     close_evidence={"container_id": cleanup.get("container_id"), "absent_after_cleanup": True})
    write_json(path, value)


def save_stats(run_dir: Path, label: str, endpoint: str | None) -> None:
    if endpoint is None:
        write_json(run_dir / f"{label}_broker_stats.json", {"status": "not_started"})
        return
    try:
        write_json(run_dir / f"{label}_broker_stats.json", broker_stats(endpoint))
    except Exception as exc:
        write_json(run_dir / f"{label}_broker_stats_error.json",
                   {"classification": "broker_stats_collection_failure",
                    "error_type": type(exc).__name__, "error_detail": str(exc)[-500:]})


def classify_incomplete(exit_code: int, lifecycle: BuilderLifecycle,
                        builder_stats_value: dict[str, Any] | None = None) -> str:
    if exit_code == 124:
        return "builder_timeout_or_no_submission"
    runtime = builder_stats_value.get("runtime", {}) if isinstance(builder_stats_value, dict) else {}
    if int(runtime.get("provider_failures", 0) or 0) > 0:
        return "builder_provider_infrastructure_failure"
    if int(runtime.get("protocol_failures", 0) or 0) > 0:
        return "builder_broker_protocol_failure"
    if int(runtime.get("delivery_failures", 0) or 0) > 0:
        return "builder_broker_delivery_failure"
    if exit_code != 0:
        stderr_path = lifecycle.run_dir / "builder.stderr.log"
        stderr = stderr_path.read_text(encoding="utf-8", errors="replace").lower() if stderr_path.is_file() else ""
        if any(term in stderr for term in ("mount", "bind source", "permission denied", "docker-compose")):
            return "builder_mount_or_container_infrastructure_failure"
        return "builder_launcher_failure"
    if not lifecycle.controller.rounds:
        return "builder_no_submission"
    if not lifecycle.controller.frozen:
        if last_submission_infrastructure_failure(lifecycle):
            return "public_or_launcher_infrastructure_failure"
        return "latest_candidate_freeze_gate_failure"
    return "builder_lifecycle_incomplete"


def last_submission_infrastructure_failure(lifecycle: BuilderLifecycle) -> bool:
    """True when the session's last submission outcome is a recorded evaluator-side failure.

    A submission the public evaluation or its launcher could not complete is recorded as
    submission_not_consumed / public_or_launcher_infrastructure_failure. When no submission was
    accepted after it, the run ended unfrozen because of that failure, not because of the Builder.
    """
    outcomes = [event for event in getattr(lifecycle, "events", [])
                if isinstance(event, dict) and event.get("event") in {"submission_finished", "submission_not_consumed"}]
    return bool(outcomes) and outcomes[-1].get("event") == "submission_not_consumed" \
        and outcomes[-1].get("classification") == "public_or_launcher_infrastructure_failure"


def offline_self_test() -> dict[str, Any]:
    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        workspace = root / "workspace"; workspace.mkdir()
        public, manifest = stage_public_package(root)
        lifecycle = BuilderLifecycle(run_dir=root, workspace=workspace,
                                     public_endpoint="http://127.0.0.1:1/v1/responses")
        try:
            provider = root / "provider.toml"
            provider.write_text(
                'model_provider = "claude_builder_broker"\ndisable_response_storage = true\n\n'
                '[model_providers.claude_builder_broker]\nname = "Claude evaluator Builder broker"\n'
                'base_url = "http://builder-broker.invalid/v1"\n'
                'env_key = "AGENTSWE_BUILDER_BROKER_TOKEN"\nwire_api = "responses"\nrequires_openai_auth = false\n',
                encoding="utf-8")
            config = builder_config(run_dir=root, public=public, workspace=workspace,
                                    lifecycle=lifecycle, provider_config=provider,
                                    image=BUILDER_IMAGE)
            config_text = config.read_text(encoding="utf-8")
            compose_text = (root / "builder_task/environment/docker-compose.yaml").read_text(encoding="utf-8")
            assert PLACEHOLDER in config_text and PLACEHOLDER in compose_text
            assert not any(key in config_text or key in compose_text for key in ("DEEPSEEK_API_KEY=", "OPENAI_API_KEY="))
            assert manifest["hidden_oracle_mounted"] is False and manifest["evaluator_source_mounted"] is False
            assert tuple(DEV_CASES) == ("dev_001", "dev_002") and len(CASE_IDS) == 6
            assert validate_delivery(workspace)
        finally:
            lifecycle.close()
    return {
        "self_test": "PASS", "network_calls": 0, "docker_started": False,
        "formal_execution_started": False, "builder": {"model": BUILDER_MODEL, "reasoning_effort": BUILDER_EFFORT},
        "lower": {"model": MODEL, "reasoning_effort": REASONING_EFFORT},
        "public_cases": list(DEV_CASES), "hidden_cases": list(CASE_IDS),
        "candidate_credential": PLACEHOLDER,
    }


def run_formal(args: argparse.Namespace) -> int:
    run_dir = args.run_dir.resolve()
    if run_dir.exists():
        raise RuntimeError(f"refusing to overwrite existing run directory: {run_dir}")
    run_dir.mkdir(parents=True)
    credential = args.credential_file.resolve()
    harbor = args.harbor.resolve()
    hidden_cases = args.hidden_cases_dir.resolve()
    if not credential.is_file() or not credential_has_provider_key(credential):
        write_json(run_dir / "summary.json", {"status": "preflight_failed",
                   "classification": "credential_preflight_failure",
                   "formal_result_claimed": False, "code_score_claimed": False})
        return 2
    if not harbor.is_file() or not os.access(harbor, os.X_OK):
        write_json(run_dir / "summary.json", {"status": "preflight_failed",
                   "classification": "builder_launcher_preflight_failure",
                   "formal_result_claimed": False, "code_score_claimed": False})
        return 2
    if hidden_cases == (ROOT / "test_cases").resolve() or ROOT in hidden_cases.parents:
        write_json(run_dir / "summary.json", {"status": "preflight_failed",
                   "classification": "hidden_isolation_preflight_failure",
                   "error": "formal hidden specs must be evaluator-issued outside the benchmark sibling",
                   "formal_result_claimed": False, "code_score_claimed": False})
        return 2
    missing_hidden = [case_id for case_id in CASE_IDS if not (hidden_cases / f"{case_id}.json").is_file() or not (hidden_cases / f"{case_id}.oracle.json").is_file()]
    if missing_hidden:
        write_json(run_dir / "summary.json", {"status": "preflight_failed",
                   "classification": "hidden_inventory_preflight_failure",
                   "missing": missing_hidden, "formal_result_claimed": False,
                   "code_score_claimed": False})
        return 2
    try:
        for case_id in CASE_IDS:
            visible, oracle, _, _ = load_case_bundle(hidden_cases, case_id)
            require_decision_authority(visible, oracle)
    except Exception as exc:
        write_json(run_dir / "summary.json", {"status": "preflight_failed",
                   "classification": "hidden_case_contract_preflight_failure",
                   "error_type": type(exc).__name__, "error_detail": str(exc)[:500],
                   "formal_result_claimed": False, "code_score_claimed": False})
        return 2

    ports: set[int] = set()
    while len(ports) < 4:
        ports.add(free_port())
    public_port, builder_port, hidden_port, judge_port = sorted(ports)
    public_endpoint = f"http://127.0.0.1:{public_port}/v1/responses"
    hidden_endpoint = f"http://127.0.0.1:{hidden_port}/v1/responses"
    judge_endpoint = f"http://127.0.0.1:{judge_port}/v1/responses"
    builder_host_endpoint = f"http://127.0.0.1:{builder_port}/v1/responses"
    builder_container_endpoint = f"http://{args.builder_host_address}:{builder_port}/v1/responses"
    suffix = hashlib.sha256(str(run_dir).encode()).hexdigest()[:10]
    names = {
        "public_lower": f"claude-public-lower-{suffix}",
        "builder": f"claude-builder-{suffix}",
        "hidden_lower": f"claude-hidden-lower-{suffix}",
        "result_judge": f"claude-result-judge-{suffix}",
    }
    cidfiles = {role: run_dir / "brokers" / f"{role}.cid" for role in names}
    started: dict[str, bool] = {name: False for name in names}
    attempted: dict[str, bool] = {name: False for name in names}
    lifecycle: BuilderLifecycle | None = None
    builder_exit = -1
    try:
        attempted["public_lower"] = True
        start_broker(name=names["public_lower"], script=ROOT / "agentloop/evaluator/broker.py",
                     credential=credential, port=public_port, image=args.lower_image,
                     provider_url=args.provider_url, cidfile=cidfiles["public_lower"])
        started["public_lower"] = True
        if args.builder_transport == 'broker':
            attempted["builder"] = True
            start_broker(name=names["builder"], script=BUILDER_BROKER_SCRIPT,
                         credential=credential, port=builder_port, image=args.builder_image, cidfile=cidfiles["builder"])
            started["builder"] = True
        provider = run_dir / "builder_broker_provider.toml"
        provider.write_text(
            'model_provider = "claude_builder_broker"\ndisable_response_storage = true\n\n'
            '[model_providers.claude_builder_broker]\nname = "Claude evaluator Builder broker"\n'
            f'base_url = "{builder_container_endpoint.rsplit("/v1", 1)[0]}/v1"\n'
            'env_key = "AGENTSWE_BUILDER_BROKER_TOKEN"\nwire_api = "responses"\nrequires_openai_auth = false\n',
            encoding="utf-8")
        public, public_manifest = stage_public_package(run_dir)
        workspace = run_dir / "builder_workspace" / "submission"; workspace.mkdir(parents=True)
        lifecycle = BuilderLifecycle(run_dir=run_dir, workspace=workspace,
                                     public_endpoint=public_endpoint,
                                     max_dev_rounds=args.max_dev_rounds)
        attempted["result_judge"] = True
        start_broker(name=names["result_judge"], script=JUDGE_BROKER_SCRIPT,
                     credential=credential, port=judge_port, image=args.builder_image, cidfile=cidfiles["result_judge"])
        started["result_judge"] = True
        lifecycle.controller.judge_endpoint = judge_endpoint
        start_submission_server(lifecycle)
        config = builder_config(run_dir=run_dir, public=public, workspace=workspace,
                                lifecycle=lifecycle, provider_config=provider,
                                image=args.builder_image)
        write_json(run_dir / "protocol_lock.json", {
            "schema_version": "agentswe-claude-formal-protocol/v1",
            "builder": {"model": BUILDER_MODEL, "reasoning_effort": BUILDER_EFFORT,
                        "single_harbor_invocation": True, "single_continuous_session": True,
                        "transport": args.builder_transport,
                        "credential_method": "native tmpfs auth.json" if args.builder_transport == "direct" else PLACEHOLDER,
                        "broker_endpoint_for_builder": builder_container_endpoint if args.builder_transport == "broker" else None},
            "public_lower": {"product": "Candidate Claude policy hook + inspector",
                             "model": MODEL, "reasoning_effort": REASONING_EFFORT,
                             "credential": PLACEHOLDER, "broker_endpoint": public_endpoint},
            "hidden_lower": {"product": "frozen Candidate Claude policy hook + inspector",
                             "model": MODEL, "reasoning_effort": REASONING_EFFORT,
                             "credential": PLACEHOLDER, "fresh_broker_started_after_freeze": True},
            "public_cases": list(DEV_CASES), "hidden_cases": list(CASE_IDS),
            "max_dev_rounds": args.max_dev_rounds,
            "n_concurrent": args.n_concurrent,
            "dev_passed_is_automatic_freeze": False,
            "builder_visibility": public_manifest,
            "result_axis": "N/A until independent Result evaluation",
            "code_axis": "N/A until independent Code evaluation",
        })
        lifecycle.event("builder_invocation_started")
        started_ns = time.time_ns()
        builder_exit = run_configured_builder(harbor, config, run_dir, args.builder_timeout,
            credential=credential, transport=args.builder_transport,
            base_url=args.builder_base_url, proxy=args.builder_proxy)
        finished_ns = time.time_ns()
        lifecycle.event("builder_invocation_finished", exit_code=builder_exit)
        lifecycle.finalize_witness(builder_exit)
        if builder_exit == 0 and lifecycle.controller.rounds and not lifecycle.controller.frozen:
            lifecycle.controller.freeze_latest("builder_exit")
        attestation = builder_attestation(lifecycle, builder_exit, started_ns, finished_ns,
                                          allow_max_rounds_interrupt=True)
        write_json(run_dir / "builder_session_attestation.json", attestation)
        if not attestation["complete"]:
            try:
                builder_stats_value = broker_stats(builder_host_endpoint) if started['builder'] else None
            except Exception:
                builder_stats_value = None
            write_json(run_dir / "summary.json", {
                "status": "builder_integration_incomplete",
                "classification": classify_incomplete(builder_exit, lifecycle, builder_stats_value),
                "builder_session_attestation": "builder_session_attestation.json",
                "formal_result_claimed": False, "code_score_claimed": False,
                "result_axis": "N/A", "code_axis": "N/A",
            })
            return 2

        # The hidden broker does not exist before this point.  Its initial
        # stats are therefore necessarily independent and zero-call.
        attempted["hidden_lower"] = True
        start_broker(name=names["hidden_lower"], script=ROOT / "agentloop/evaluator/broker.py",
                     credential=credential, port=hidden_port, image=args.lower_image,
                     provider_url=args.provider_url, cidfile=cidfiles["hidden_lower"])
        started["hidden_lower"] = True
        lifecycle.controller.hidden_cases = hidden_cases
        lifecycle.controller.hidden_broker_endpoint = hidden_endpoint
        lifecycle.controller.hidden_output = run_dir / "hidden_after_freeze"
        hidden_records = lifecycle.controller.hidden()
        finalizer_output = run_dir / "formal_aggregation.json"
        finalizer = subprocess.run([
            sys.executable, str(ROOT / "evaluator/formal_finalize.py"),
            "--run-dir", str(run_dir), "--output", str(finalizer_output),
            "--result-judge-broker-endpoint", judge_endpoint,
            "--credential-file", str(credential),
        ], text=True, capture_output=True, check=False)
        aggregation = read_json(finalizer_output) if finalizer_output.is_file() else {}
        write_json(run_dir / "summary.json", {
            "status": "completed" if finalizer.returncode == 0 else "formal_finalization_refused",
            "classification": "real_same_session_repeated_public_feedback_freeze_six_hidden",
            "builder_session_attestation": "builder_session_attestation.json",
            "freeze_manifest": "lifecycle/freeze_manifest.json",
            "hidden_attestation": "lifecycle/hidden_after_freeze_attestation.json",
            "hidden_case_count": len(hidden_records),
            "formal_result_claimed": aggregation.get("formal_result_publishable") is True,
            "code_score_claimed": aggregation.get("code_score_publishable") is True,
            "formal_aggregation": aggregation,
            "formal_finalizer_exit": finalizer.returncode,
            "formal_finalizer_stderr_tail": finalizer.stderr[-1500:],
        })
        write_json(run_dir / "one_stop_summary.json", {
            "status": "completed" if finalizer.returncode == 0 else "formal_result_not_publishable",
            "dev_lifecycle": lifecycle.controller.rounds, "freeze": lifecycle.controller.frozen,
            "hidden": {"inventory": list(CASE_IDS), "summary": hidden_records},
            "result_judge_contracts": aggregation.get("result_judge_contracts"),
            "code_contract": aggregation.get("code_contract"), "combined_score": None,
            "cleanup_attestation": str(run_dir / "cleanup_attestation.json"),
        })
        return 0 if finalizer.returncode == 0 else 2
    except Exception as exc:
        classification = "evaluator_orchestration_failure"
        detail = str(exc)
        if "broker_container_start_failure" in detail or "broker_health_failure" in detail:
            classification = "broker_infrastructure_failure"
        elif isinstance(exc, (urllib.error.URLError, TimeoutError, ConnectionError)):
            classification = "provider_or_broker_transport_failure"
        elif "hidden" in detail.lower():
            classification = "hidden_execution_infrastructure_failure"
        write_json(run_dir / "summary.json", {
            "status": "orchestration_failed", "classification": classification,
            "error_type": type(exc).__name__, "error_detail": detail[-1200:],
            "builder_exit_code": builder_exit,
            "formal_result_claimed": False, "code_score_claimed": False,
            "result_axis": "N/A", "code_axis": "N/A",
        })
        return 2
    finally:
        if lifecycle is not None:
            lifecycle.close()
        save_stats(run_dir, "public_lower", public_endpoint if started["public_lower"] else None)
        save_builder_stats(run_dir, "builder", builder_host_endpoint if started["builder"] else None, args.builder_transport)
        save_stats(run_dir, "hidden_lower", hidden_endpoint if started["hidden_lower"] else None)
        save_stats(run_dir, "result_judge", judge_endpoint if started.get("result_judge") else None)
        cleanup_results: list[dict[str, Any]] = []
        harbor_cleanup = cleanup_run_mounted_containers(run_dir, set(names.values()))
        for role in names:
            cleanup_results.append(cleanup_owned_container(role, cidfiles[role], attempted=attempted[role]))
        write_json(run_dir / "cleanup_attestation.json", {
            "schema_version": "agentswe-claude-cleanup-attestation/v1",
            "container_names": names, "owned_container_cleanup": cleanup_results,
            "stats_saved_before_cleanup": True, "cleanup_finished_at": now(),
            "cleanup_results": cleanup_results,
            "run_mounted_harbor_cleanup": harbor_cleanup,
            "all_started_containers_absent": all(item["absent_after_cleanup"] for item in cleanup_results),
            "cleanup_complete": all(item["absent_after_cleanup"] for item in cleanup_results),
            "unrelated_containers_touched": False,
        })


def pilot_self_test() -> dict[str, Any]:
    """Build the reduced pilot configuration without Docker, Harbor, or provider I/O."""
    with tempfile.TemporaryDirectory(prefix="claude-pilot-self-test-") as directory:
        root = Path(directory)
        workspace = root / "workspace"; workspace.mkdir()
        public, manifest = stage_public_package(root)
        manifest = restrict_public_package_to_pilot(public, manifest)
        lifecycle = BuilderLifecycle(
            run_dir=root, workspace=workspace,
            public_endpoint="http://127.0.0.1:1/v1/responses",
            pilot_not_formal=True,
            max_dev_rounds=10,
        )
        try:
            provider = root / "provider.toml"
            provider.write_text(
                'model_provider = "claude_builder_broker"\ndisable_response_storage = true\n\n'
                '[model_providers.claude_builder_broker]\nname = "pilot dry broker"\n'
                'base_url = "http://builder-broker.invalid/v1"\n'
                'env_key = "AGENTSWE_BUILDER_BROKER_TOKEN"\nwire_api = "responses"\nrequires_openai_auth = false\n',
                encoding="utf-8",
            )
            config = builder_config(
                run_dir=root, public=public, workspace=workspace, lifecycle=lifecycle,
                provider_config=provider, image=BUILDER_IMAGE, pilot_not_formal=True,
            )
            instruction = (root / "builder_task/instruction.md").read_text(encoding="utf-8")
            config_text = config.read_text(encoding="utf-8")
            checks = {
                "pilot_inventory": lifecycle.controller.public_case_ids == DEV_CASES
                and lifecycle.controller.hidden_case_ids == ("test_001",),
                "builder_lock": BUILDER_MODEL in config_text and BUILDER_EFFORT in config_text,
                "placeholder_only": PLACEHOLDER in config_text and not any(key in config_text for key in PROVIDER_KEYS),
                "both_dev_cases_visible": (public / "dev_cases/dev_001").is_dir()
                and (public / "dev_cases/dev_002").is_dir(),
                "pilot_instruction": "test_001 once" in instruction and "formal six-hidden" in instruction,
                "hidden_isolated": manifest.get("hidden_oracle_mounted") is False,
            }
        finally:
            lifecycle.close()
    return {
        "self_test": "PASS" if all(checks.values()) else "FAIL",
        "checks": checks,
        "pilot_not_formal": True,
        "network_calls": 0,
        "docker_started": False,
        "formal_execution_started": False,
        "formal_result_claimed": False,
        "public_cases": list(DEV_CASES),
        "hidden_cases": ["test_001"],
    }


def verify_readiness_binding(args: argparse.Namespace) -> dict[str, Any] | None:
    if not args.readiness_profile:
        return None
    if args.readiness_profile != READINESS_PROFILE:
        raise ValueError("unsupported readiness profile")
    if not args.readiness_binding_file or not args.readiness_binding_sha256:
        raise ValueError("readiness requires binding file and SHA256")
    path = args.readiness_binding_file.resolve()
    if path.is_symlink() or not path.is_file() or hashlib.sha256(path.read_bytes()).hexdigest() != args.readiness_binding_sha256:
        raise ValueError("readiness binding bytes changed")
    value = read_json(path)
    control_root = Path(os.environ.get("READINESS_CONTROL_ROOT", "@@AGENTSWE_EDITING_CONTROL@@"))
    verifier = control_root / "readiness_binding.py"
    if not verifier.is_file():
        raise ValueError("readiness binding verifier missing")
    import importlib.util
    # readiness_binding imports its siblings by bare name.
    if str(control_root) not in sys.path:
        sys.path.insert(0, str(control_root))
    spec = importlib.util.spec_from_file_location("readiness_binding", verifier)
    if spec is None or spec.loader is None:
        raise ValueError("readiness binding verifier unavailable")
    module = importlib.util.module_from_spec(spec); spec.loader.exec_module(module)
    binding_source = Path(os.environ.get("READINESS_SOURCE_OVERRIDE", str(ROOT))).resolve()
    measured = module.verify_binding(binding_source, value, control_root=control_root)
    if measured != value:
        raise ValueError("readiness binding changed during verification")
    return value


def run_pilot(args: argparse.Namespace) -> int:
    """Run up to ten real two-dev rounds and one hidden case without formal publication."""
    readiness_binding = verify_readiness_binding(args)
    readiness = readiness_binding is not None
    run_dir = args.run_dir.resolve()
    if run_dir.exists():
        raise RuntimeError(f"refusing to overwrite existing pilot run directory: {run_dir}")
    run_dir.mkdir(parents=True)
    if readiness:
        # The Builder runner sits several layers below the parsed arguments, so
        # the verified binding doubles as the runtime marker for a readiness run.
        # It is written here rather than in verify_readiness_binding because that
        # runs before the directory exists, and creating it early would trip the
        # guard immediately above.
        write_json(run_dir / "readiness_current_binding.json", readiness_binding)
    credential = args.credential_file.resolve()
    harbor = args.harbor.resolve()
    hidden_cases = args.hidden_cases_dir.resolve()
    base_summary = {
        "schema_version": "agentswe-claude-pilot-summary/v1",
        "mode": "pilot",
        "pilot_not_formal": True,
        "formal_result_claimed": False,
        "code_score_claimed": False,
        "result_axis": "N/A",
        "code_axis": "N/A",
    }
    # The evaluator credential may intentionally be root-only.  The Candidate
    # and host-side Builder must never read it; the Docker-owned broker validates
    # the mounted secret when it starts.
    if not credential.is_file():
        write_json(run_dir / "summary.json", {**base_summary, "status": "preflight_failed", "classification": "credential_preflight_failure"})
        return 2
    if not harbor.is_file() or not os.access(harbor, os.X_OK):
        write_json(run_dir / "summary.json", {**base_summary, "status": "preflight_failed", "classification": "builder_launcher_preflight_failure"})
        return 2
    if hidden_cases == (ROOT / "test_cases").resolve() or ROOT in hidden_cases.parents:
        write_json(run_dir / "summary.json", {**base_summary, "status": "preflight_failed", "classification": "hidden_isolation_preflight_failure"})
        return 2
    if not (hidden_cases / "test_001.json").is_file() or not (hidden_cases / "test_001.oracle.json").is_file():
        write_json(run_dir / "summary.json", {**base_summary, "status": "preflight_failed", "classification": "pilot_test_001_missing"})
        return 2
    try:
        load_case_bundle(hidden_cases, "test_001")
    except Exception as exc:
        write_json(run_dir / "summary.json", {**base_summary, "status": "preflight_failed",
                   "classification": "pilot_case_contract_preflight_failure",
                   "error_type": type(exc).__name__, "error_detail": str(exc)[:500]})
        return 2

    ports: set[int] = set()
    while len(ports) < 4:
        ports.add(free_port())
    public_port, builder_port, hidden_port, judge_port = sorted(ports)
    judge_endpoint = f"http://127.0.0.1:{judge_port}/v1/responses"
    public_endpoint = f"http://127.0.0.1:{public_port}/v1/responses"
    hidden_endpoint = f"http://127.0.0.1:{hidden_port}/v1/responses"
    builder_host_endpoint = f"http://127.0.0.1:{builder_port}/v1/responses"
    builder_container_endpoint = f"http://{args.builder_host_address}:{builder_port}/v1/responses"
    suffix = hashlib.sha256(str(run_dir).encode()).hexdigest()[:10]
    names = {
        "public_lower": f"claude-pilot-public-{suffix}",
        "builder": f"claude-pilot-builder-{suffix}",
        "hidden_lower": f"claude-pilot-hidden-{suffix}",
        "result_judge": f"claude-pilot-judge-{suffix}",
    }
    cidfiles = {role: run_dir / "brokers" / f"{role}.cid" for role in names}
    started = {role: False for role in names}
    attempted = {role: False for role in names}
    lifecycle: BuilderLifecycle | None = None
    builder_exit = -1
    try:
        attempted["public_lower"] = True
        start_broker(name=names["public_lower"], script=ROOT / "agentloop/evaluator/broker.py",
                     credential=credential, port=public_port, image=args.lower_image, provider_url=args.provider_url, cidfile=cidfiles["public_lower"])
        started["public_lower"] = True
        if args.builder_transport == 'broker':
            attempted["builder"] = True
            start_broker(name=names["builder"], script=BUILDER_BROKER_SCRIPT,
                         credential=credential, port=builder_port, image=args.builder_image, cidfile=cidfiles["builder"])
            started["builder"] = True
        provider = run_dir / "builder_broker_provider.toml"
        provider.write_text(
            'model_provider = "claude_builder_broker"\ndisable_response_storage = true\n\n'
            '[model_providers.claude_builder_broker]\nname = "Claude pilot Builder broker"\n'
            f'base_url = "{builder_container_endpoint.rsplit("/v1", 1)[0]}/v1"\n'
            'env_key = "AGENTSWE_BUILDER_BROKER_TOKEN"\nwire_api = "responses"\nrequires_openai_auth = false\n',
            encoding="utf-8",
        )
        public, public_manifest = stage_public_package(run_dir)
        public_manifest = (restrict_public_package_to_readiness(public, public_manifest)
                           if readiness else restrict_public_package_to_pilot(public, public_manifest))
        workspace = run_dir / "builder_workspace/submission"; workspace.mkdir(parents=True)
        lifecycle = BuilderLifecycle(run_dir=run_dir, workspace=workspace,
                                     public_endpoint=public_endpoint, pilot_not_formal=True,
                                     max_dev_rounds=(2 if readiness else args.max_dev_rounds),
                                     readiness_profile=args.readiness_profile,
                                     current_binding=readiness_binding)
        attempted["result_judge"] = True
        start_broker(name=names["result_judge"], script=JUDGE_BROKER_SCRIPT,
                     credential=credential, port=judge_port, image=args.builder_image, cidfile=cidfiles["result_judge"],
                     # Handed to the coordinator under readiness, so it has to
                     # outlive its own stop; --rm would delete it there.
                     defer_removal=readiness)
        started["result_judge"] = True
        lifecycle.controller.judge_endpoint = judge_endpoint
        start_submission_server(lifecycle)
        config = builder_config(run_dir=run_dir, public=public, workspace=workspace,
                                lifecycle=lifecycle, provider_config=provider,
                                image=args.builder_image, pilot_not_formal=True,
                                readiness_profile=args.readiness_profile)
        write_json(run_dir / "protocol_lock.json", {
            "schema_version": "agentswe-claude-pilot-protocol/v1",
            "pilot_not_formal": True,
            "builder": {"model": BUILDER_MODEL, "reasoning_effort": BUILDER_EFFORT,
                        "single_harbor_invocation": True, "single_continuous_session": True,
                        "transport": args.builder_transport,
                        "credential_method": "native tmpfs auth.json" if args.builder_transport == "direct" else PLACEHOLDER},
            "lower": {"product": "Candidate Claude policy hook + inspector", "model": MODEL,
                      "reasoning_effort": REASONING_EFFORT, "credential": PLACEHOLDER},
            "public_cases": list(lifecycle.controller.public_case_ids),
            "hidden_cases": list(lifecycle.controller.hidden_case_ids),
            "readiness_profile": args.readiness_profile,
            "current_binding": readiness_binding,
            "builder_visibility": public_manifest,
            "formal_finalizer_allowed": False,
            "public_hidden_brokers_independent": public_endpoint != hidden_endpoint and names["public_lower"] != names["hidden_lower"],
            "hidden_broker_started_strictly_after_freeze": True,
            "hidden_broker_requires_zero_call_start": True,
        })
        lifecycle.event("builder_invocation_started", pilot_not_formal=True)
        started_ns = time.time_ns()
        builder_exit = run_configured_builder(harbor, config, run_dir, args.builder_timeout,
            credential=credential, transport=args.builder_transport,
            base_url=args.builder_base_url, proxy=args.builder_proxy)
        finished_ns = time.time_ns()
        lifecycle.event("builder_invocation_finished", exit_code=builder_exit, pilot_not_formal=True)
        lifecycle.finalize_witness(builder_exit)
        attestation = builder_attestation(lifecycle, builder_exit, started_ns, finished_ns)
        write_json(run_dir / "pilot_builder_session_attestation.json", attestation)
        if not attestation["complete"]:
            write_json(run_dir / "summary.json", {**base_summary,
                "status": "pilot_builder_integration_incomplete",
                "classification": classify_incomplete(builder_exit, lifecycle),
                "builder_session_attestation": "pilot_builder_session_attestation.json"})
            return 2

        attempted["hidden_lower"] = True
        start_broker(name=names["hidden_lower"], script=ROOT / "agentloop/evaluator/broker.py",
                     credential=credential, port=hidden_port, image=args.lower_image, provider_url=args.provider_url, cidfile=cidfiles["hidden_lower"])
        started["hidden_lower"] = True
        lifecycle.controller.hidden_cases = hidden_cases
        lifecycle.controller.hidden_broker_endpoint = hidden_endpoint
        lifecycle.controller.hidden_output = run_dir / "pilot_hidden_after_freeze"
        hidden_records = lifecycle.controller.hidden()
        hidden_run = read_json(run_dir / "pilot_hidden_after_freeze/hidden_run.json")
        hidden_before = hidden_run.get("broker", {}).get("before_runtime", {}) if isinstance(hidden_run.get("broker"), dict) else {}
        if int(hidden_before.get("calls", -1)) != 0 or int(hidden_before.get("failures", -1)) != 0:
            raise RuntimeError("pilot hidden broker zero-call start attestation missing")
        write_json(run_dir / "pilot_hidden_broker_initial.json", {
            "schema_version": "agentswe-claude-pilot-hidden-broker-initial/v1",
            "pilot_not_formal": True,
            "started_after_freeze": True,
            "independent_from_public_lower": public_endpoint != hidden_endpoint and names["public_lower"] != names["hidden_lower"],
            "calls": 0,
            "failures": 0,
            "stats": hidden_run.get("broker", {}).get("before"),
        })
        if args.readiness_profile:
            # Readiness runs the same two judges through the same finalizer, but
            # into its own roots, and wraps each answer with an independent judge
            # identity. Nothing here is publishable and the coordinator admits
            # the exported bundle.
            sys.path.insert(0, str(ROOT))
            from evaluator.readiness_smoke import run as run_readiness_judges
            readiness = run_readiness_judges(
                task_root=ROOT, run_dir=run_dir, credential=credential,
                result_endpoint=judge_endpoint)
            complete = readiness["readiness_judges_complete"]
            write_json(run_dir / "summary.json", {**base_summary,
                "readiness_profile": args.readiness_profile,
                "current_binding": readiness_binding,
                "status": "readiness_evidence_complete" if complete else "readiness_evidence_incomplete",
                "classification": "real_same_session_two_dev_feedback_freeze_one_test",
                "builder_session_attestation": "pilot_builder_session_attestation.json",
                "freeze_manifest": "lifecycle/freeze_manifest.json",
                "readiness_judge_smoke": "readiness_judge_smoke.json",
                "formal_result_claimed": False, "code_score_claimed": False,
                "score_threshold": None, "pipeline_ready": False,
                "admission_required": True})
            return 0 if complete else 2
        finalizer_output = run_dir / "acceptance_aggregation.json"
        finalizer = subprocess.run([
            sys.executable, str(ROOT / "evaluator/formal_finalize.py"),
            "--run-dir", str(run_dir), "--output", str(finalizer_output),
            "--result-judge-broker-endpoint", judge_endpoint,
            "--acceptance-cases", "test_001",
            "--credential-file", str(credential),
        ], text=True, capture_output=True, timeout=3600)
        evaluation = read_json(finalizer_output) if finalizer_output.is_file() else {}
        write_json(run_dir / "summary.json", {**base_summary,
            "readiness_profile": args.readiness_profile,
            "current_binding": readiness_binding,
            "status": "acceptance_complete" if finalizer.returncode == 0 else "acceptance_finalization_refused",
            "classification": "real_same_session_two_dev_feedback_freeze_one_test",
            "builder_session_attestation": "pilot_builder_session_attestation.json",
            "freeze_manifest": "lifecycle/freeze_manifest.json",
            "hidden_attestation": "lifecycle/hidden_after_freeze_attestation.json",
            "hidden_broker_initial": "pilot_hidden_broker_initial.json",
            "pilot_evaluation": evaluation,
            "finalizer_exit": finalizer.returncode,
            "finalizer_stderr_tail": finalizer.stderr[-1500:],
            "hidden_case_count": len(hidden_records)})
        return 0 if finalizer.returncode == 0 else 2
    except Exception as exc:
        write_json(run_dir / "summary.json", {**base_summary, "status": "pilot_orchestration_failed",
                   "classification": "pilot_infrastructure_orchestration_failure",
                   "error_type": type(exc).__name__, "error_detail": str(exc)[-1200:],
                   "traceback": __import__("traceback").format_exc()[-4000:]})
        return 2
    finally:
        if lifecycle is not None:
            lifecycle.close()
        save_stats(run_dir, "pilot_public_lower", public_endpoint if started["public_lower"] else None)
        save_builder_stats(run_dir, "pilot_builder", builder_host_endpoint if started["builder"] else None, args.builder_transport)
        save_stats(run_dir, "pilot_hidden_lower", hidden_endpoint if started["hidden_lower"] else None)
        save_stats(run_dir, "result_judge", judge_endpoint if started.get("result_judge") else None)
        cleanup_results = []
        cleanup_run_mounted_containers(run_dir, set(names.values()))
        retained_judge = None
        retention_error = None
        if readiness and attempted.get("result_judge"):
            try:
                from harbor.readiness_resources import retain_container
                judge_cid = Path(cidfiles["result_judge"])
                transport = (judge_cid.parent / (judge_cid.stem + "-judge-transport")).resolve()
                retained_judge = retain_container(
                    read_container_id(judge_cid), run_dir,
                    expected_name=names["result_judge"],
                    expected_mount=(str(transport), "/evidence"))
            except Exception as exc:
                # Unprovable ownership must not become a silently skipped
                # cleanup; fall through and remove it as usual.
                retention_error = f"{type(exc).__name__}: {exc}"
        for role in names:
            if retained_judge is not None and role == "result_judge":
                cleanup_results.append({"role": role, "container_id": retained_judge["container_id"],
                                        "ownership_proven": True, "cleanup_attempted": False,
                                        "absent_after_cleanup": False, "removal_deferred": True,
                                        "retained_terminal": True})
                continue
            cleanup_results.append(cleanup_owned_container(role, cidfiles[role], attempted=attempted[role]))
        for role, label in (("public_lower", "pilot_public_lower"), ("hidden_lower", "pilot_hidden_lower")):
            close_lower_broker_stats(run_dir / f"{label}_broker_stats.json",
                                     next((r for r in cleanup_results if r.get("role") == role), None))
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
            "pilot_not_formal": True, "cleanup_results": cleanup_results,
            "removal_deferred": retained_judge is not None,
            "coordinator_cleanup_required": retained_judge is not None,
            "retained_resources": retained_resources,
            "retention_error": retention_error,
            "all_started_containers_absent": all(item["absent_after_cleanup"] for item in cleanup_results),
            "cleanup_complete": all(item["absent_after_cleanup"] for item in cleanup_results),
            "unrelated_containers_touched": False,
        })


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--self-test", action="store_true",
                        help="offline configuration/gate test; starts no formal run")
    parser.add_argument("--run-formal", action="store_true",
                        help="explicitly start the real Builder/public/freeze/hidden lifecycle")
    parser.add_argument("--pilot", action="store_true",
                        help="run both dev cases per accepted round then test_001 as pilot_not_formal")
    parser.add_argument("--pilot-self-test", action="store_true",
                        help="provider-free dry test of the reduced pilot configuration")
    parser.add_argument("--run-dir", type=Path,
                        help="new evaluator-owned output directory; existing paths are refused")
    parser.add_argument("--hidden-cases-dir", type=Path,
                        help="external evaluator-issued test_001.json..test_006.json directory")
    parser.add_argument("--readiness-profile", choices=[READINESS_PROFILE])
    parser.add_argument("--readiness-binding-file", type=Path)
    parser.add_argument("--readiness-binding-sha256")
    parser.add_argument("--credential-file", type=Path,
                        default=Path("@@AGENTSWE_CREDENTIAL_FILE@@"))
    parser.add_argument("--harbor", type=Path, default=Path("@@AGENTSWE_HARBOR_BIN@@"))
    parser.add_argument("--builder-image", default=BUILDER_IMAGE)
    parser.add_argument("--builder-transport", choices=('direct','broker'), default='direct')
    parser.add_argument("--builder-base-url", default='https://api.deepseek.com/v1')
    parser.add_argument("--builder-proxy", default='http://127.0.0.1:7890')
    parser.add_argument("--lower-image", default=LOWER_IMAGE)
    parser.add_argument("--builder-host-address", default="172.17.0.1")
    parser.add_argument("--provider-url", default=os.environ.get("AGENTSWE_PROVIDER_URL", "https://api.deepseek.com/v1/responses"))
    parser.add_argument("--builder-timeout", type=int, default=28800)
    parser.add_argument("--max-dev-rounds", type=int, default=10)
    parser.add_argument("--n-concurrent", type=int, default=1)
    args = parser.parse_args(argv)
    selected_modes = sum(bool(value) for value in (args.self_test, args.run_formal, args.pilot, args.pilot_self_test))
    if selected_modes > 1:
        parser.error("--self-test, --pilot-self-test, --pilot, and --run-formal are mutually exclusive")
    if args.self_test:
        print(json.dumps(offline_self_test(), indent=2, ensure_ascii=False))
        return 0
    if args.pilot_self_test:
        result = pilot_self_test()
        print(json.dumps(result, indent=2, ensure_ascii=False))
        return 0 if result["self_test"] == "PASS" else 1
    if not args.run_formal and not args.pilot:
        parser.error("choose --self-test, --pilot-self-test, --pilot, or --run-formal")
    if args.run_dir is None or args.hidden_cases_dir is None:
        parser.error("--pilot/--run-formal require --run-dir and --hidden-cases-dir")
    if args.builder_timeout <= 0:
        parser.error("--builder-timeout must be positive")
    enforce_builder_timeout_contract(parser, args.builder_timeout)
    if not 1 <= args.max_dev_rounds <= 10:
        parser.error("--max-dev-rounds must be between 1 and 10")
    if args.n_concurrent != 1:
        parser.error("--n-concurrent must equal 1")
    if args.readiness_profile and not args.pilot:
        parser.error("--readiness-profile requires --pilot")
    if args.readiness_profile and args.max_dev_rounds != 2:
        parser.error("readiness requires --max-dev-rounds 2")
    if args.pilot:
        return run_pilot(args)
    return run_formal(args)


if __name__ == "__main__":
    raise SystemExit(main())
