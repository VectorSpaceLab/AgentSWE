#!/usr/bin/env python3
"""Auditable accepted-submission -> feedback -> freeze controller."""
from __future__ import annotations

import hashlib
import json
import shutil
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable


MODEL_ARTIFACT_SCHEMA = "dyad-lower-agent-artifact-v3"
MODEL_ARTIFACT_OWNER = "model_via_dyad_typed_chat"
MODEL_ARTIFACT_PRODUCER = "model finish action captured from Dyad persisted typed chat"


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def tree_digest(root: Path) -> str:
    h = hashlib.sha256()
    for path in sorted(root.rglob("*"), key=lambda p: str(p.relative_to(root))):
        rel = str(path.relative_to(root)).encode()
        h.update(len(rel).to_bytes(8, "big")); h.update(rel)
        if path.is_symlink(): kind, data = b"L", path.readlink().as_posix().encode()
        elif path.is_file(): kind, data = b"F", path.read_bytes()
        else: kind, data = b"D", b""
        h.update(kind); h.update(len(data).to_bytes(8, "big")); h.update(data)
    return h.hexdigest()


def model_authored_artifact(value: Any) -> bool:
    """Return true only for the captured artifact from the model finish path."""
    artifact = value.get("artifact") if isinstance(value, dict) else None
    provenance = value.get("artifact_provenance") if isinstance(value, dict) else None
    return (
        value.get("artifact_present") is True
        and isinstance(artifact, dict)
        and artifact.get("schema_version") == MODEL_ARTIFACT_SCHEMA
        and isinstance(provenance, dict)
        and provenance.get("artifact_owner") == MODEL_ARTIFACT_OWNER
        and provenance.get("producer_entry") == MODEL_ARTIFACT_PRODUCER
        and provenance.get("evaluator_synthesized") is False
        and provenance.get("exists") is True
    )


@dataclass
class CandidateRecord:
    number: int
    digest: str
    path: str
    state: str = "accepted"
    dev_results: dict[str, Any] = field(default_factory=dict)
    feedback: str = ""
    infrastructure_invalid: bool = False
    builder_session_id: str = ""
    feedback_sha256: str = ""


class TwoRoundController:
    """Lifecycle state machine; evaluation is injected and never hidden here."""

    def __init__(self, run_dir: Path, evaluate: Callable[[int, Path], dict[str, Any]], *, builder_session_id: str | None = None, max_dev_rounds: int = 10) -> None:
        self.run_dir = run_dir
        self.evaluate = evaluate
        self.builder_session_id = builder_session_id or str(uuid.uuid4())
        if not 1 <= max_dev_rounds <= 10:
            raise ValueError("max_dev_rounds must be in 1..10")
        self.max_dev_rounds = max_dev_rounds
        self.records: list[CandidateRecord] = []
        self.frozen: dict[str, Any] | None = None

    def submit(self, candidate: Path, *, builder_session_id: str | None = None, feedback_sha256: str | None = None) -> CandidateRecord:
        if self.frozen is not None:
            raise RuntimeError("hidden/freeze already committed")
        if len(self.records) >= self.max_dev_rounds:
            raise RuntimeError("accepted submission limit reached")
        if not candidate.is_dir():
            raise ValueError("Candidate directory is missing")
        digest = tree_digest(candidate)
        if any(item.digest == digest for item in self.records):
            raise ValueError("accepted submission must have a different digest")
        number = len(self.records) + 1
        if builder_session_id is not None and builder_session_id != self.builder_session_id:
            raise RuntimeError("Candidate was submitted from a different Builder session")
        if number >= 2:
            expected_feedback = self.records[-1].feedback_sha256
            if not feedback_sha256 or feedback_sha256 != expected_feedback:
                raise RuntimeError("later accepted submission must acknowledge latest feedback digest")
        snapshot = self.run_dir / "candidates" / f"candidate_{number:03d}"
        snapshot.parent.mkdir(parents=True, exist_ok=True)
        shutil.copytree(candidate, snapshot, symlinks=True)
        record = CandidateRecord(number=number, digest=digest, path=str(snapshot), state="evaluating", builder_session_id=self.builder_session_id)
        result = self.evaluate(number, snapshot)
        if set(result) != {"dev_001", "dev_002"}:
            raise RuntimeError("every accepted Candidate must run both dev cases")
        invalid_results = [
            case_id
            for case_id, item in result.items()
            if not self._real_lower_result(item)
        ]
        if invalid_results:
            raise RuntimeError(
                "Candidate dev results lack real product evidence: "
                + ", ".join(sorted(invalid_results))
            )
        record.dev_results = result
        record.infrastructure_invalid = any(item.get("classification") == "infrastructure-invalid" for item in result.values() if isinstance(item, dict))
        if record.infrastructure_invalid:
            record.state = "infrastructure-invalid"
        else:
            record.state = "completed"
            record.feedback = self._feedback(number, result)
            record.feedback_sha256 = hashlib.sha256(Path(record.feedback).read_bytes()).hexdigest()
            self.records.append(record)
        self._write_lifecycle()
        return record

    def _feedback(self, number: int, result: dict[str, Any]) -> str:
        lines = [f"# Dyad lower-agent dev feedback — Candidate {number}", ""]
        for case_id in ("dev_001", "dev_002"):
            item = result[case_id]
            lines.extend([
                f"## {case_id}",
                f"- score: {item.get('score', 'N/A')}/100",
                f"- classification: {item.get('classification', 'unclassified')}",
                f"- broker: {json.dumps(item.get('broker', {}), sort_keys=True)}",
                f"- assertions: {json.dumps(item.get('assertions', []), ensure_ascii=False)}",
                "",
            ])
        path = self.run_dir / "feedback" / f"candidate_{number:03d}.md"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("\n".join(lines), encoding="utf-8")
        return str(path)

    def freeze_latest(self) -> dict[str, Any]:
        if not self.records:
            raise RuntimeError("freeze requires at least one accepted submission")
        if len({item.digest for item in self.records}) != len(self.records):
            raise RuntimeError("freeze digest collision")
        candidate = Path(self.records[-1].path)
        frozen = self.run_dir / "frozen_candidate"
        shutil.rmtree(frozen, ignore_errors=True)
        shutil.copytree(candidate, frozen, symlinks=True)
        self.frozen = {
            "schema_version": "dyad-agentloop-freeze-v2",
            "candidate_digest": tree_digest(frozen),
            "source_submission": self.records[-1].number,
            "accepted_submission_count": len(self.records),
            "max_dev_rounds": self.max_dev_rounds,
            "accepted_candidate_digests": [item.digest for item in self.records],
            "source_submission_id": f"candidate-{self.records[-1].number:03d}",
            "builder_session_id": self.builder_session_id,
            "feedback_digest": self.records[-1].feedback_sha256 or None,
            "feedback_consumed": all(item.number == 1 or bool(item.feedback_sha256) for item in self.records),
            "feedback_chain_complete": all(item.number == 1 or bool(item.feedback_sha256) for item in self.records),
            "frozen_at": utc_now(),
            "hidden_started_at": None,
        }
        self._write_json(self.run_dir / "freeze_manifest.json", self.frozen)
        return self.frozen

    # Compatibility alias retained for older callers; this is no longer a
    # fixed Candidate-2 operation.
    freeze_candidate_2 = freeze_latest

    def run_hidden(self, evaluate_hidden: Callable[[Path], dict[str, Any]]) -> dict[str, Any]:
        if self.frozen is None:
            raise RuntimeError("hidden evaluation is forbidden before freeze")
        frozen = self.run_dir / "frozen_candidate"
        if tree_digest(frozen) != self.frozen["candidate_digest"]:
            raise RuntimeError("frozen Candidate digest changed")
        started = utc_now()
        if started <= self.frozen["frozen_at"]:
            raise RuntimeError("hidden did not start after freeze")
        self.frozen["hidden_started_at"] = started
        result = evaluate_hidden(frozen)
        if not self._real_hidden_result(result):
            raise RuntimeError("hidden result is a stub or lacks real product evidence")
        self._write_json(self.run_dir / "freeze_manifest.json", self.frozen)
        self._write_json(self.run_dir / "hidden_result.json", result)
        return result

    @staticmethod
    def _real_lower_result(value: Any) -> bool:
        """Accept only evidence that came from a real product execution path."""
        if not isinstance(value, dict):
            return False
        if value.get("classification") in {
            "not-run",
            "evaluator_materialize_required",
            "static_only",
            "mock_or_stub_only",
        }:
            return False
        if value.get("static_stage_a") is True or value.get("stub") is True:
            return False
        classification = value.get("classification")
        if classification == "infrastructure-invalid":
            return isinstance(value.get("failure_class"), str) and bool(value.get("failure_class"))
        if classification not in {"valid", "candidate_failure", "headless_real_chat_stream", "headless_real_acceptance"}:
            return False
        if value.get("product_entry_observed") is not True and value.get("real_product") is not True:
            return False
        if int(value.get("broker_calls_delta", 0) or 0) <= 0:
            return False
        if int(value.get("broker_successful_calls_delta", 0) or 0) <= 0:
            return False
        if classification == "candidate_failure":
            # A real product run can fail before it produces a usable
            # model-authored artifact. Keep that feedback scoreable.
            return True
        if classification == "valid":
            return model_authored_artifact(value)
        artifact = value.get("artifact")
        if isinstance(artifact, dict):
            if artifact.get("real_product") is not True:
                return False
            return value.get("artifact_present") is True
        if value.get("real_product") is True and value.get("artifact_success") is True:
            return True
        if value.get("classification") in {
            "headless_real_chat_stream",
            "headless_real_acceptance",
        }:
            return value.get("artifact_present") is not False
        return False

    @staticmethod
    def _real_hidden_result(value: Any) -> bool:
        """Reject hidden placeholders even after a syntactically valid freeze."""
        if not isinstance(value, dict):
            return False
        if value.get("classification") in {
            "not-run",
            "evaluator_materialize_required",
            "static_only",
            "mock_or_stub_only",
        }:
            return False
        if value.get("static_stage_a") is True or value.get("stub") is True:
            return False
        cases = value.get("cases")
        if isinstance(cases, dict):
            if set(cases) != {f"test_{index:03d}" for index in range(1, 7)}:
                return False
            return all(
                isinstance(item, dict)
                and item.get("classification") in {"valid", "candidate_failure"}
                and item.get("product_entry_observed") is True
                and item.get("broker_calls_delta", 0) > 0
                and item.get("broker_successful_calls_delta", 0) > 0
                and (
                    item.get("classification") == "candidate_failure"
                    or model_authored_artifact(item)
                )
                for item in cases.values()
            )
        if value.get("classification") in {"valid", "candidate_failure"}:
            return TwoRoundController._real_lower_result(value)
        artifact = value.get("artifact")
        if isinstance(artifact, dict):
            return artifact.get("real_product") is True and artifact.get("success") is True
        return (
            value.get("real_product") is True
            and value.get("artifact_success") is True
        ) or value.get("classification") in {
            "headless_real_chat_stream",
            "headless_real_acceptance",
        }

    def _write_lifecycle(self) -> None:
        self._write_json(self.run_dir / "dev_lifecycle.json", [record.__dict__ for record in self.records])

    @staticmethod
    def _write_json(path: Path, value: object) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(value, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
