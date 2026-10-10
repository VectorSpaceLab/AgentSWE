#!/usr/bin/env python3
"""Create-aligned accepted-submission ledger for formal Edit development."""
from __future__ import annotations

import hashlib
import json
import os
import shutil
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

DEV_CASES = ("dev_001", "dev_002")


def now() -> str:
    return datetime.now(timezone.utc).isoformat()


def write_json(path: Path, value: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(value, indent=2, ensure_ascii=False, sort_keys=True) + "\n", encoding="utf-8")
    os.replace(temporary, path)


def tree_digest(root: Path) -> str:
    digest = hashlib.sha256()
    for path in sorted(root.rglob("*"), key=lambda item: item.relative_to(root).as_posix()):
        relative = path.relative_to(root).as_posix().encode()
        if path.is_symlink():
            kind, payload = b"L", os.readlink(path).encode()
        elif path.is_file():
            kind, payload = b"F", path.read_bytes()
        elif path.is_dir():
            continue
        else:
            kind, payload = b"O", b""
        digest.update(kind + len(relative).to_bytes(8, "big") + relative)
        digest.update(len(payload).to_bytes(8, "big") + payload)
    return digest.hexdigest()


class DevLifecycle:
    """Track up to ten distinct accepted snapshots from one Builder session.

    Duplicate digests are idempotent. Structural and infrastructure-invalid
    attempts are archived without consuming an accepted capability round.
    ``dev_passed`` is recorded when both numeric dev scores average above 60;
    it never freezes the lifecycle.
    """

    def __init__(self, run_dir: Path, session_id: str, *, max_dev_rounds: int = 10, n_concurrent: int = 1, pilot_not_formal: bool = False) -> None:
        if not 1 <= max_dev_rounds <= 10:
            raise ValueError("max_dev_rounds must be in 1..10")
        if n_concurrent != 1:
            raise ValueError("n_concurrent is fixed at 1")
        if not session_id:
            raise ValueError("one non-empty Builder session id is required")
        self.run_dir = run_dir.resolve()
        self.run_dir.mkdir(parents=True, exist_ok=True)
        self.session_id = session_id
        self.max_dev_rounds = max_dev_rounds
        self.n_concurrent = n_concurrent
        self.pilot_not_formal = pilot_not_formal
        self.accepted: list[dict[str, Any]] = []
        self.non_consuming_attempts: list[dict[str, Any]] = []
        self.duplicates: list[dict[str, Any]] = []
        self.freeze_record: dict[str, Any] | None = None
        self._persist()

    @staticmethod
    def _dev_score(results: dict[str, Any]) -> tuple[float | None, bool]:
        scores: list[float] = []
        for case_id in DEV_CASES:
            item = results.get(case_id)
            score = item.get("score") if isinstance(item, dict) else None
            if isinstance(score, bool) or not isinstance(score, (int, float)):
                return None, False
            scores.append(float(score))
        mean = sum(scores) / 2
        return mean, mean > 60

    def submit(self, snapshot: Path, dev_results: dict[str, Any], *, structurally_valid: bool = True, infrastructure_invalid: bool = False) -> dict[str, Any]:
        if self.freeze_record is not None:
            raise RuntimeError("formal lifecycle is already frozen")
        snapshot = snapshot.resolve()
        digest = tree_digest(snapshot) if snapshot.is_dir() else ""
        if digest and any(record["candidate_digest"] == digest for record in self.accepted):
            original = next(record for record in self.accepted if record["candidate_digest"] == digest)
            duplicate = {
                "schema_version": "agentswe-edit-duplicate-submission-v1",
                "candidate_digest": digest,
                "accepted_round": original["accepted_round"],
                "feedback": original["feedback"],
                "consumed": False,
                "idempotent": True,
                "at": now(),
            }
            self.duplicates.append(duplicate)
            self._persist()
            return duplicate
        if not structurally_valid or not snapshot.is_dir():
            return self._non_consuming(digest, snapshot, dev_results, "structural_invalid")
        if set(dev_results) != set(DEV_CASES):
            return self._non_consuming(digest, snapshot, dev_results, "dev_inventory_invalid")
        if infrastructure_invalid or any(
            isinstance(item, dict) and (item.get("infrastructure_invalid") is True or item.get("infra_valid") is False)
            for item in dev_results.values()
        ):
            return self._non_consuming(digest, snapshot, dev_results, "infrastructure_invalid")
        if len(self.accepted) >= self.max_dev_rounds:
            raise RuntimeError("max_dev_rounds already reached")
        round_no = len(self.accepted) + 1
        mean, passed = self._dev_score(dev_results)
        feedback_base = {
            "schema_version": "agentswe-edit-dev-feedback-v1",
            "builder_session_id": self.session_id,
            "accepted_round": round_no,
            "candidate_digest": digest,
            "dev_results": dev_results,
            "dev_mean": mean,
            "dev_passed": passed,
            "dev_passed_is_automatic_freeze": False,
        }
        feedback_digest = hashlib.sha256(json.dumps(feedback_base, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
        feedback = {**feedback_base, "feedback_digest": feedback_digest}
        record = {
            "schema_version": "agentswe-edit-accepted-submission-v1",
            "accepted_round": round_no,
            "candidate_digest": digest,
            "snapshot_path": str(snapshot),
            "dev_case_inventory": list(DEV_CASES),
            "dev_results": dev_results,
            "dev_mean": mean,
            "dev_passed": passed,
            "accepted": True,
            "feedback": feedback,
            "accepted_at": now(),
        }
        self.accepted.append(record)
        write_json(self.run_dir / f"accepted_round_{round_no:03d}.json", record)
        write_json(self.run_dir / f"feedback_round_{round_no:03d}.json", feedback)
        self._persist()
        return record

    def _non_consuming(self, digest: str, snapshot: Path, dev_results: dict[str, Any], reason: str) -> dict[str, Any]:
        record = {
            "schema_version": "agentswe-edit-non-consuming-attempt-v1",
            "attempt": len(self.non_consuming_attempts) + 1,
            "requested_accepted_round": len(self.accepted) + 1,
            "candidate_digest": digest or None,
            "snapshot_path": str(snapshot),
            "dev_results": dev_results,
            "reason": reason,
            "accepted": False,
            "consumes_capability_round": False,
            "at": now(),
        }
        self.non_consuming_attempts.append(record)
        write_json(self.run_dir / f"non_consuming_attempt_{record['attempt']:03d}.json", record)
        self._persist()
        return record

    def freeze(self, reason: str) -> dict[str, Any]:
        if self.freeze_record is not None:
            return self.freeze_record
        if not self.accepted:
            raise RuntimeError("no structurally valid accepted snapshot is available")
        if reason not in {"max_dev_rounds", "builder_exit"}:
            raise ValueError("freeze reason must be max_dev_rounds or builder_exit")
        if reason == "max_dev_rounds" and len(self.accepted) != self.max_dev_rounds:
            raise RuntimeError("max_dev_rounds freeze requested before the configured limit")
        latest = self.accepted[-1]
        source = Path(latest["snapshot_path"])
        frozen = self.run_dir / "frozen_candidate"
        if frozen.exists():
            raise RuntimeError("frozen Candidate path already exists")
        shutil.copytree(source, frozen, symlinks=True)
        digest = tree_digest(frozen)
        if digest != latest["candidate_digest"]:
            raise RuntimeError("frozen Candidate digest mismatch")
        for path in sorted([*frozen.rglob("*"), frozen], key=lambda item: len(item.parts), reverse=True):
            if not path.is_symlink():
                path.chmod(path.stat().st_mode & ~0o222)
        self.freeze_record = {
            "schema_version": "agentswe-edit-freeze-v2",
            "candidate_path": str(frozen),
            "candidate_digest": digest,
            "source_submission": latest["accepted_round"],
            "accepted_rounds": len(self.accepted),
            "max_dev_rounds": self.max_dev_rounds,
            "freeze_reason": reason,
            "dev_passed_is_automatic_freeze": False,
            "frozen_at": now(),
        }
        write_json(self.run_dir / "freeze_manifest.json", self.freeze_record)
        self._persist()
        return self.freeze_record

    def _persist(self) -> None:
        write_json(self.run_dir / "dev_lifecycle.json", {
            "schema_version": "agentswe-edit-dev-lifecycle-v2",
            "builder_session_id": self.session_id,
            "max_dev_rounds": self.max_dev_rounds,
            "n_concurrent": self.n_concurrent,
            "public_cases": list(DEV_CASES),
            "accepted_round_count": len(self.accepted),
            "accepted_submissions": self.accepted,
            "duplicate_submissions": self.duplicates,
            "infrastructure_or_invalid_attempts": self.non_consuming_attempts,
            "dev_passed_is_automatic_freeze": False,
            "pilot_not_formal": self.pilot_not_formal,
            "freeze": self.freeze_record,
        })
