#!/usr/bin/env python3
"""Small same-session Builder lifecycle adapter for DeepCode.

The adapter is intentionally model-agnostic.  A real Builder integration can
call ``submit`` and ``consume_feedback`` through a socket or CLI bridge, while
the evaluator retains ownership of Candidate materialization, public feedback,
freeze, and the attestation.  This module itself never starts a model.
"""
from __future__ import annotations

import hashlib
import json
import os
import secrets
import shutil
from evaluator.harness.public_feedback import public_record
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

try:
    from .controller import CandidateController, ProtocolError
    from .candidate_adapter import tree_digest
except ImportError:  # direct script/module import compatibility
    from controller import CandidateController, ProtocolError
    from candidate_adapter import tree_digest


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def write_json(path: Path, value: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(value, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    os.replace(temporary, path)


class BuilderSession:
    """Bind up to ten accepted submissions to one explicit Builder session."""

    def __init__(
        self,
        controller: CandidateController,
        *,
        session_id: str | None = None,
        require_feedback_ack: bool = False,
    ) -> None:
        self.controller = controller
        self.session_id = session_id or f"builder-session-{secrets.token_hex(8)}"
        self.require_feedback_ack = require_feedback_ack
        self.feedback_consumed = False
        self.feedback_digest: str | None = None
        self.feedback_digest_ack: str | None = None
        self.last_submit_idempotent = False
        self.last_submit_feedback: dict[str, Any] | None = None
        self.events: list[dict[str, Any]] = []
        self._write_attestation()

    @property
    def feedback_path(self) -> Path:
        return self.controller.run_dir / "builder-feedback.json"

    @property
    def attestation_path(self) -> Path:
        return self.controller.run_dir / "builder-session-attestation.json"

    def feedback_history_path(self, source_submission: int) -> Path:
        return self.controller.run_dir / f"builder-feedback-candidate_{source_submission:03d}.json"

    def submit(self, candidate: Path, *, feedback_digest_ack: str | None = None) -> dict[str, Any]:
        record_count_before = len(self.controller.records)
        requested_round = record_count_before + 1
        if self.controller.readiness_profile:
            from presubmit_validator import paths, report_ok
            prior = self.controller.records[-1] if self.controller.records else None
            report_ok(candidate, paths(candidate / 'solution.patch'), {
                'builder_session_id': self.session_id, 'submission_number': requested_round,
                'revision_of_candidate_digest': prior['delivery_candidate_digest'] if prior else None,
                'feedback_digest': self.feedback_digest if prior else None})
        # The delivery snapshot is recorded for every accepted submission (readiness and
        # formal): accepted_payload()/native_attestation() read delivery_candidate_digest
        # for each record, and the Builder is told which delivery its next revision revises.
        snapshot = self.controller.run_dir / 'delivery_snapshots' / f'{requested_round:03d}-{secrets.token_hex(8)}'
        shutil.copytree(candidate, snapshot, symlinks=True)
        candidate = snapshot

        def guard_new_acceptance() -> None:
            if requested_round >= 2 and not self.feedback_consumed:
                raise ProtocolError("later submission requires feedback consumption in the same Builder session")
            if requested_round >= 2 and self.require_feedback_ack:
                if not feedback_digest_ack or feedback_digest_ack != self.feedback_digest:
                    raise ProtocolError("later accepted submission feedback digest acknowledgement is missing or does not match")
                self.feedback_digest_ack = feedback_digest_ack

        from v2_readiness import tree_digest as delivery_digest
        before = delivery_digest(candidate)
        self.events.append({
            "event": "candidate_submission_started",
            "session_id": self.session_id,
            "source_submission": requested_round,
            "at": utc_now(),
            "candidate_input_digest": before,
        })
        self.last_submit_idempotent = False
        self.last_submit_feedback = None
        record = self.controller.submit(candidate, acceptance_guard=guard_new_acceptance)
        if len(self.controller.records) == requested_round and 'delivery_candidate_digest' not in record:
            record.update(builder_session_id=self.session_id, delivery_path=str(candidate),
                delivery_candidate_digest=before, submission_sha256={name: hashlib.sha256((candidate / name).read_bytes()).hexdigest()
                    for name in ('solution.patch', 'edit_report.json', 'run_report.json')},
                revision_of_candidate_digest=self.controller.records[-2]['delivery_candidate_digest'] if requested_round >= 2 else None,
                feedback_digest_consumed=feedback_digest_ack if requested_round >= 2 else None)
        self.last_submit_idempotent = len(self.controller.records) == record_count_before
        if self.last_submit_idempotent:
            source_submission = record.get("source_submission")
            if not isinstance(source_submission, int):
                raise ProtocolError("idempotent Candidate record has no source submission identity")
            history_path = self.feedback_history_path(source_submission)
            if not history_path.is_file():
                raise ProtocolError("prior accepted-submission feedback identity is unavailable")
            feedback = json.loads(history_path.read_text(encoding="utf-8"))
            if not isinstance(feedback, dict) or feedback.get("session_id") != self.session_id:
                raise ProtocolError("prior accepted-submission feedback does not belong to this Builder session")
            self.last_submit_feedback = feedback
            self.events.append({
                "event": "candidate_submission_idempotent",
                "session_id": self.session_id,
                "source_submission": source_submission,
                "at": utc_now(),
                "candidate_digest": record.get("candidate_digest"),
                "feedback_digest": feedback.get("feedback_digest"),
            })
            self._write_attestation()
            return record
        self.events.append({
            "event": "candidate_submission_finished",
            "session_id": self.session_id,
            "source_submission": requested_round,
            "at": utc_now(),
            "candidate_digest": record.get("candidate_digest"),
            "build_exit_code": (record.get("build") or {}).get("exit_code"),
            "feedback_digest_ack": feedback_digest_ack if requested_round >= 2 else None,
        })
        feedback = {
            "schema_version": "deepcode-builder-feedback-v1",
            "session_id": self.session_id,
            "source_submission": requested_round,
            "generated_at": utc_now(),
            "record": public_record(record),
            "consumed": False,
        }
        canonical = json.dumps(feedback, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()
        self.feedback_digest = hashlib.sha256(canonical).hexdigest()
        feedback["feedback_digest"] = self.feedback_digest
        write_json(self.feedback_path, feedback)
        write_json(self.feedback_history_path(requested_round), feedback)
        self.last_submit_feedback = feedback
        self.feedback_consumed = False
        self._write_attestation()
        if self.controller.readiness_profile:
            write_json(self.controller.run_dir / "controller_state.json", {"records": self.controller.records, "frozen": self.controller.frozen})
        return record

    def consume_feedback(self, feedback_digest: str | None = None) -> dict[str, Any]:
        if not self.controller.records:
            raise ProtocolError("feedback is available only after an accepted submission")
        if self.feedback_consumed:
            feedback = json.loads(self.feedback_path.read_text(encoding="utf-8"))
            if feedback_digest and feedback_digest != feedback.get("feedback_digest"):
                raise ProtocolError("feedback digest does not match this Builder session")
            return feedback
        if not self.feedback_path.is_file():
            raise ProtocolError("accepted-submission feedback is unavailable")
        feedback = json.loads(self.feedback_path.read_text(encoding="utf-8"))
        if not isinstance(feedback, dict) or feedback.get("session_id") != self.session_id:
            raise ProtocolError("feedback does not belong to this Builder session")
        stored_digest = feedback.get("feedback_digest")
        if not isinstance(stored_digest, str) or stored_digest != self.feedback_digest:
            raise ProtocolError("feedback digest is unavailable or changed")
        if feedback_digest is not None and feedback_digest != stored_digest:
            raise ProtocolError("feedback digest does not match this Builder session")
        consumed_at = utc_now()
        self.feedback_consumed = True
        self.events.append({
            "event": "feedback_consumed",
            "session_id": self.session_id,
            "source_submission": feedback.get("source_submission"),
            "at": consumed_at,
            "feedback_digest": stored_digest,
        })
        self._write_attestation()
        return feedback

    def freeze(self, *, builder_exit_evidence: dict | None = None) -> dict[str, Any]:
        if not self.controller.records:
            raise ProtocolError("freeze requires at least one accepted submission")
        if self.controller.readiness_profile:
            if len(self.controller.records) != 2:
                raise ProtocolError('readiness requires two rounds')
            first_feedback = json.loads(self.feedback_history_path(1).read_text())
            canonical = {key: value for key, value in first_feedback.items() if key != 'feedback_digest'}
            digest = hashlib.sha256(json.dumps(canonical, sort_keys=True, separators=(',', ':'), ensure_ascii=False).encode()).hexdigest()
            if digest != first_feedback.get('feedback_digest') or self.controller.records[1].get('feedback_digest_consumed') != digest:
                raise ProtocolError('readiness exact feedback digest changed')
        frozen = self.controller.freeze(builder_exit_evidence=builder_exit_evidence)
        self.events.append({
            "event": "latest_candidate_frozen",
            "session_id": self.session_id,
            "source_submission": frozen.get("source_submission"),
            "at": utc_now(),
            "candidate_digest": frozen.get("candidate_digest"),
        })
        self._write_attestation()
        if self.controller.readiness_profile:
            write_json(self.controller.run_dir / "controller_state.json", {"records": self.controller.records, "frozen": frozen})
        return frozen

    def status(self) -> dict[str, Any]:
        return {
            "schema_version": "deepcode-builder-session-status-v1",
            "session_id": self.session_id,
            "records": self.controller.records,
            "feedback_consumed": self.feedback_consumed or any(
                event.get("event") == "feedback_consumed" for event in self.events
            ),
            "feedback_digest": self.feedback_digest,
            "feedback_digest_ack": self.feedback_digest_ack,
            "last_submit_idempotent": self.last_submit_idempotent,
            "frozen": self.controller.frozen,
        }

    def _write_attestation(self) -> None:
        records = self.controller.records
        distinct = bool(records) and len({record.get("candidate_digest") for record in records}) == len(records)
        consumed_by_submission: dict[int, str] = {}
        acknowledgement_chain: list[dict[str, Any]] = []
        for event in self.events:
            if event.get("event") == "feedback_consumed":
                source_submission = event.get("source_submission")
                feedback_digest = event.get("feedback_digest")
                if isinstance(source_submission, int) and isinstance(feedback_digest, str):
                    consumed_by_submission[source_submission] = feedback_digest
            elif event.get("event") == "candidate_submission_finished":
                source_submission = event.get("source_submission")
                if isinstance(source_submission, int) and source_submission >= 2:
                    expected = consumed_by_submission.get(source_submission - 1)
                    acknowledged = event.get("feedback_digest_ack")
                    acknowledgement_chain.append({
                        "source_submission": source_submission,
                        "acknowledges_submission": source_submission - 1,
                        "expected_feedback_digest": expected,
                        "feedback_digest_ack": acknowledged,
                        "matches": isinstance(expected, str) and acknowledged == expected,
                    })
        expected_acknowledgements = max(0, len(records) - 1)
        feedback_chain_complete = (
            len(acknowledgement_chain) == expected_acknowledgements
            and all(item["matches"] for item in acknowledgement_chain)
        )
        write_json(self.attestation_path, {
            "schema_version": "deepcode-builder-session-attestation-v1",
            "builder_session_id": self.session_id,
            "same_session": all(event.get("session_id") == self.session_id for event in self.events),
            "accepted_submission_count": len(records),
            # Legacy diagnostic alias retained for consumers that display the
            # historical two-round milestone; it is not a lifecycle gate.
            "candidate_1_and_2": len(records) >= 2,
            "distinct_digests": distinct,
            "feedback_consumed": self.feedback_consumed or any(
                event.get("event") == "feedback_consumed" for event in self.events
            ),
            "feedback_digest": self.feedback_digest,
            "latest_feedback_digest_ack": self.feedback_digest_ack,
            "feedback_acknowledgements": acknowledgement_chain,
            "feedback_chain_complete": feedback_chain_complete,
            # Legacy diagnostic alias retained for old report renderers.
            "candidate_2_feedback_digest_ack": self.feedback_digest_ack,
            "feedback_ack_matches": (
                feedback_chain_complete
                if self.require_feedback_ack else self.feedback_consumed
            ),
            "freeze": self.controller.frozen,
            "events": self.events,
            "updated_at": utc_now(),
        })
