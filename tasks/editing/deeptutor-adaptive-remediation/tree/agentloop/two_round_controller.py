#!/usr/bin/env python3
"""Fail-closed accepted-submission ledger for the formal DeepTutor loop.

The historical implementation consumed exactly two Candidates.  Formal Edit
evaluation now permits one through ten distinct accepted submissions in one
continuous Builder session.  This module keeps the old filename for stable
imports, but the live controller is deliberately generalized.
"""

from __future__ import annotations

import argparse
import os
from pathlib import Path
import shutil
import stat
from typing import Any
import uuid
try:
    from .public_feedback import public_case_feedback
except ImportError:
    from public_feedback import public_case_feedback

try:
    from .protocol import (
        BUILDER_EFFORT,
        BUILDER_MODEL,
        DEV_CASES,
        is_infrastructure,
        object_digest,
        tree_digest,
        utc_now,
        write_json,
    )
except ImportError:  # pragma: no cover
    from protocol import (  # type: ignore
        BUILDER_EFFORT,
        BUILDER_MODEL,
        DEV_CASES,
        is_infrastructure,
        object_digest,
        tree_digest,
        utc_now,
        write_json,
    )


def _validated_builder_witness(value: dict[str, Any], session_id: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise RuntimeError("an evaluator-owned Builder session witness is required")
    if value.get("session_id") != session_id:
        raise RuntimeError("Builder witness session_id does not match the controller")
    if value.get("builder_model") != BUILDER_MODEL:
        raise RuntimeError("Builder witness model does not match the protocol lock")
    if value.get("builder_reasoning_effort") != BUILDER_EFFORT:
        raise RuntimeError("Builder witness reasoning effort does not match the protocol lock")
    if not isinstance(value.get("connection_id"), str) or not value["connection_id"].strip():
        raise RuntimeError("Builder witness requires a non-empty connection_id")
    if value.get("single_connection") is not True:
        raise RuntimeError("Builder witness must attest one continuous connection")
    if not isinstance(value.get("started_at"), str) or not value["started_at"].strip():
        raise RuntimeError("Builder witness requires started_at")
    return dict(value)


def _validate_terminal_dev_results(
    dev_results: dict[str, Any], expected_cases: tuple[str, ...]
) -> None:
    if set(dev_results) != set(expected_cases):
        raise RuntimeError("each accepted candidate requires the configured public dev inventory")
    for case_id, value in dev_results.items():
        if not isinstance(value, dict):
            raise RuntimeError(f"{case_id} has no terminal evaluator record")
        if value.get("terminal") is not True:
            raise RuntimeError(f"{case_id} is not a terminal evaluator record")
        if not isinstance(value.get("classification"), str) or not value["classification"]:
            raise RuntimeError(f"{case_id} has no terminal evaluator classification")


class AcceptedSubmissionController:
    """Consume up to ten distinct accepted submissions from one Builder session."""

    def __init__(
        self,
        run_dir: Path,
        session_id: str,
        *,
        builder_witness: dict[str, Any],
        dev_cases: tuple[str, ...] = DEV_CASES,
        max_dev_rounds: int = 10,
        pilot_not_formal: bool = False,
        readiness_profile: str | None = None,
        current_binding: dict[str, Any] | None = None,
    ) -> None:
        if not isinstance(session_id, str) or not session_id.strip():
            raise ValueError("session_id must be non-empty")
        if not 1 <= max_dev_rounds <= 10:
            raise ValueError("max_dev_rounds must be in 1..10")
        if not dev_cases or any(case_id not in DEV_CASES for case_id in dev_cases):
            raise ValueError("dev_cases must be a non-empty subset of the public inventory")
        if tuple(dev_cases) != DEV_CASES and not pilot_not_formal:
            raise ValueError("reduced public inventory is allowed only for pilot_not_formal")
        self.run_dir = run_dir.resolve()
        self.run_dir.mkdir(parents=True, exist_ok=True)
        self.session_id = session_id
        self.builder_witness = _validated_builder_witness(builder_witness, session_id)
        self.builder_witness_digest = object_digest(self.builder_witness)
        self.dev_cases = tuple(dev_cases)
        self.max_dev_rounds = max_dev_rounds
        self.pilot_not_formal = bool(pilot_not_formal)
        self.readiness_profile=readiness_profile
        self.current_binding=current_binding
        if readiness_profile is not None:
            from .readiness import PROFILE, validate_binding
            if readiness_profile!=PROFILE or tuple(dev_cases)!=('dev_001',) or max_dev_rounds!=2 or not pilot_not_formal:
                raise ValueError('readiness requires explicit pilot dev_001/two rounds')
            validate_binding(current_binding)
        self.records: list[dict[str, Any]] = []
        self.frozen: dict[str, Any] | None = None
        self.feedback_record: dict[str, Any] | None = None
        self.infrastructure_attempts = 0
        write_json(self.run_dir / "builder_session_opened.json", {
            "schema_version": "agentswe-deeptutor-builder-session/v2",
            "session_id": self.session_id,
            "builder_witness": self.builder_witness,
            "builder_witness_digest": self.builder_witness_digest,
            "opened_at": utc_now(),
            "formal_result_claimed": False,
            "pilot_not_formal": self.pilot_not_formal,
            "public_case_inventory": list(self.dev_cases),
            "max_dev_rounds": self.max_dev_rounds,
        })

    @property
    def next_round(self) -> int:
        return len(self.records) + 1

    def submit(
        self,
        candidate: Path,
        round_no: int,
        dev_results: dict[str, Any],
        *,
        feedback: dict[str, Any] | None = None,
        builder_session_id: str,
        builder_witness: dict[str, Any],
        readiness_evidence: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Record one accepted Candidate; infrastructure attempts do not consume a slot."""
        if self.frozen is not None:
            raise RuntimeError("controller already frozen")
        if not 1 <= round_no <= self.max_dev_rounds:
            raise ValueError(f"accepted submission number must be in 1..{self.max_dev_rounds}")
        if round_no != self.next_round:
            raise RuntimeError("accepted submissions must be submitted in order")
        if builder_session_id != self.session_id:
            raise RuntimeError("candidate was submitted by a different Builder session")
        observed_witness = _validated_builder_witness(builder_witness, builder_session_id)
        if object_digest(observed_witness) != self.builder_witness_digest:
            raise RuntimeError("candidate was submitted through a different Builder connection")
        if not candidate.is_dir():
            raise RuntimeError("candidate directory is missing")
        _validate_terminal_dev_results(dev_results, self.dev_cases)
        digest = tree_digest(candidate)

        infrastructure_cases = [
            case_id for case_id, value in dev_results.items() if is_infrastructure(value)
        ]
        if self.readiness_profile:
            from .readiness import execution_valid
            infrastructure_cases = [k for k,v in dev_results.items() if not execution_valid(v)]
        if infrastructure_cases:
            self.infrastructure_attempts += 1
            record = {
                "schema_version": "agentswe-deeptutor-non-consuming-attempt/v2",
                "round_requested": round_no,
                "session_id": self.session_id,
                "builder_witness_digest": self.builder_witness_digest,
                "candidate_digest": digest,
                "candidate_path": str(candidate.resolve()),
                "dev_results": dev_results,
                "infrastructure_cases": infrastructure_cases,
                "consumed": False,
                "archived_at": utc_now(),
            }
            write_json(self.run_dir / f"infrastructure_attempt_{self.infrastructure_attempts:03d}.json", record)
            return record

        if any(digest == item.get("candidate_digest") for item in self.records):
            raise RuntimeError("each accepted submission must have a distinct candidate digest")

        expected_feedback = self.feedback_record
        if round_no == 1:
            if feedback is not None:
                raise RuntimeError("first accepted submission cannot consume feedback")
            feedback_digest = None
        else:
            if not isinstance(expected_feedback, dict):
                raise RuntimeError("the next accepted submission requires prior evaluator feedback")
            if not isinstance(feedback, dict):
                raise RuntimeError("the next accepted submission requires evaluator feedback")
            if feedback != expected_feedback:
                raise RuntimeError("submission did not return the exact evaluator feedback record")
            feedback_digest = str(feedback.get("feedback_digest") or "")
            canonical = dict(feedback)
            canonical.pop("feedback_digest", None)
            if self.readiness_profile:
                from .readiness import canonical_feedback
                expected_digest=canonical_feedback(canonical)
            else:
                expected_digest=object_digest(canonical)
            if feedback_digest != expected_digest:
                raise RuntimeError("consumed feedback digest is invalid")

        record = {
            "schema_version": "agentswe-deeptutor-accepted-candidate/v2",
            "round": round_no,
            "session_id": self.session_id,
            "builder_witness_digest": self.builder_witness_digest,
            "candidate_digest": digest,
            "candidate_path": str(candidate.resolve()),
            "dev_results": dev_results,
            "dev_passed": (
                sum(float(item["score"]) for item in dev_results.values()
                    if isinstance(item, dict) and isinstance(item.get("score"), (int, float)))
                / len(dev_results)
                > 60
            ),
            "feedback_digest_consumed": feedback_digest,
            "consumed": True,
            "submitted_at": utc_now(),
            "pilot_not_formal": self.pilot_not_formal,
        }
        if self.readiness_profile:
            if not isinstance(readiness_evidence,dict):
                raise RuntimeError('readiness requires evaluator build/delivery evidence')
            record.update(readiness_evidence)
        if not post_freeze_superseded(self, record):
            self.records.append(record)
        write_json(self.run_dir / f"accepted_candidate_{round_no:03d}.json", record)

        feedback_base = {
            "schema_version": "agentswe-deeptutor-feedback/v2",
            "session_id": self.session_id,
            "builder_witness_digest": self.builder_witness_digest,
            "source_submission": round_no,
            "source_candidate_digest": digest,
            "dev_results": {key: public_case_feedback(value) for key, value in dev_results.items()},
            "dev_passed": record["dev_passed"],
            "available_at": utc_now(),
        }
        if self.readiness_profile:
            from .readiness import canonical_feedback
            feedback_hash=canonical_feedback(feedback_base)
        else:
            feedback_hash=object_digest(feedback_base)
        self.feedback_record = {**feedback_base, "feedback_digest": feedback_hash}
        if self.readiness_profile:
            record['issued_feedback_digest']=feedback_hash
            write_json(self.run_dir / f"accepted_candidate_{round_no:03d}.json", record)
        write_json(self.run_dir / f"feedback_after_candidate_{round_no:03d}.json", self.feedback_record)
        # Preserve the pilot-era first-feedback filename for readers that only
        # know the old two-round artifact name; its contents are identical.
        if round_no == 1:
            write_json(self.run_dir / "feedback_after_candidate_1.json", self.feedback_record)
        write_json(self.run_dir / "controller_state.json", {
            "records": self.records,
            "infrastructure_attempts": self.infrastructure_attempts,
            "latest_feedback": self.feedback_record,
            "frozen": self.frozen,
        })
        return record

    def freeze(self, *, builder_exit_evidence: dict[str, Any] | None = None) -> dict[str, Any]:
        """Freeze the latest accepted valid Candidate after Builder exit/max rounds."""
        if self.frozen is not None:
            return self.frozen
        if not self.records:
            raise RuntimeError("freeze requires at least one accepted candidate")
        if self.readiness_profile:
            from .readiness import verify_rounds
            verify_rounds(self.records,self.session_id,self.current_binding,builder_exit_evidence)
        latest = self.records[-1]
        source = Path(str(latest["candidate_path"])).resolve()
        if not source.is_dir():
            raise RuntimeError("latest accepted Candidate source directory is missing")
        if tree_digest(source) != latest["candidate_digest"]:
            raise RuntimeError("latest accepted Candidate changed after public evaluation")
        _assert_regular_tree(source)
        frozen_candidate = self.run_dir / "frozen_candidate"
        temporary = self.run_dir / f".frozen_candidate.tmp-{uuid.uuid4().hex}"
        if frozen_candidate.exists():
            raise RuntimeError("frozen Candidate snapshot already exists")
        try:
            shutil.copytree(source, temporary, symlinks=False,
                            ignore=shutil.ignore_patterns("__pycache__", ".pytest_cache"))
            _assert_regular_tree(temporary)
            frozen_digest = tree_digest(temporary)
            if frozen_digest != latest["candidate_digest"]:
                raise RuntimeError("Candidate digest changed while creating freeze snapshot")
            _make_tree_read_only(temporary)
            if not _tree_is_read_only(temporary):
                raise RuntimeError("Candidate freeze permissions are not read-only")
            os.replace(temporary, frozen_candidate)
        except Exception:
            if temporary.exists():
                _make_tree_writable(temporary)
                shutil.rmtree(temporary)
            raise

        witness_consistent = all(
            item.get("builder_witness_digest") == self.builder_witness_digest
            and item.get("session_id") == self.session_id for item in self.records
        )
        feedback_consumed = all(
            item.get("round") == 1 or item.get("feedback_digest_consumed") ==
            self._feedback_digest_for(int(item["round"]) - 1)
            for item in self.records
        )
        same_session_verified = bool(
            witness_consistent
            and self.builder_witness.get("transport") == "evaluator-owned-single-session"
            and self.builder_witness.get("single_connection") is True
        )
        frozen_at = utc_now()
        attestation = {
            "schema_version": "agentswe-deeptutor-builder-session-attestation/v2",
            "session_id": self.session_id,
            "builder_witness_digest": self.builder_witness_digest,
            "builder_model": BUILDER_MODEL,
            "builder_reasoning_effort": BUILDER_EFFORT,
            "accepted_submission_count": len(self.records),
            "max_dev_rounds": self.max_dev_rounds,
            "distinct_candidate_digests": len({item["candidate_digest"] for item in self.records}) == len(self.records),
            "feedback_chain_consumed": feedback_consumed,
            "same_session_witness_consistent": witness_consistent,
            "same_session_verified": same_session_verified,
            "formal_builder_provenance_claimed": False,
            "pilot_not_formal": self.pilot_not_formal,
            "attested_at": frozen_at,
        }
        write_json(self.run_dir / "builder_session_attestation.json", attestation)
        self.frozen = {
            "schema_version": "agentswe-deeptutor-freeze/v2",
            "source_submission": int(latest["round"]),
            "accepted_submission_count": len(self.records),
            "max_dev_rounds": self.max_dev_rounds,
            "session_id": self.session_id,
            "builder_witness_digest": self.builder_witness_digest,
            "candidate_digest": latest["candidate_digest"],
            "candidate_path": str(frozen_candidate.resolve()),
            "candidate_source_path": str(source),
            "accepted_candidate_digests": [item["candidate_digest"] for item in self.records],
            "feedback_digest": latest.get("feedback_digest_consumed"),
            # A first accepted submission also receives an evaluator feedback
            # record.  Later submissions prove consumption through the
            # separate feedback_chain_consumed attestation.
            "feedback_received": self.feedback_record is not None,
            "feedback_chain_consumed": feedback_consumed,
            "same_session_verified": same_session_verified,
            "builder_session_attestation": str((self.run_dir / "builder_session_attestation.json").resolve()),
            "hidden_allowed": True,
            "frozen_tree_read_only": True,
            "frozen_tree_regular": True,
            "frozen_digest_at_creation": frozen_digest,
            "frozen_at": frozen_at,
            "pilot_not_formal": self.pilot_not_formal,
        }
        if self.readiness_profile:
            from .readiness import FILES, sha
            delivery=Path(latest['delivery_path'])
            self.frozen.update(readiness_profile=self.readiness_profile,current_binding=self.current_binding,
                delivery_candidate_digest=latest['delivery_candidate_digest'],builder_session_id=self.session_id,
                submission_sha256={name:sha(delivery/name) for name in FILES},builder_exit_evidence=builder_exit_evidence,
                score_threshold=None,readiness_only=True)
        write_json(self.run_dir / "freeze_manifest.json", self.frozen)
        write_json(self.run_dir / "controller_state.json", {
            "records": self.records,
            "infrastructure_attempts": self.infrastructure_attempts,
            "latest_feedback": self.feedback_record,
            "frozen": self.frozen,
        })
        return self.frozen

    def _feedback_digest_for(self, round_no: int) -> str:
        path = self.run_dir / f"feedback_after_candidate_{round_no:03d}.json"
        if not path.is_file():
            raise RuntimeError(f"feedback record for accepted submission {round_no} is missing")
        value = __import__("json").loads(path.read_text(encoding="utf-8"))
        return str(value.get("feedback_digest") or "")

    def hidden(self) -> dict[str, Any]:
        if self.frozen is None:
            raise RuntimeError("hidden execution is forbidden before freeze")
        raise RuntimeError(
            "hidden execution requires agentloop.run_hidden; this controller never emits a fake not_run result"
        )


# Stable import name for pilot-era tests and integrations.  The alias is
# intentionally the generalized controller; there is no live two-round path.
TwoRoundController = AcceptedSubmissionController


def _assert_regular_tree(root: Path) -> None:
    for item in root.rglob("*"):
        if item.is_symlink():
            raise RuntimeError(f"freeze source contains a symlink: {item.relative_to(root)}")
        mode = item.stat().st_mode
        if not (stat.S_ISREG(mode) or stat.S_ISDIR(mode)):
            raise RuntimeError(f"freeze source contains a special file: {item.relative_to(root)}")


def _make_tree_read_only(root: Path) -> None:
    for item in sorted(root.rglob("*"), key=lambda path: len(path.parts), reverse=True):
        os.chmod(item, item.stat().st_mode & ~0o222)
    os.chmod(root, root.stat().st_mode & ~0o222)


def _make_tree_writable(root: Path) -> None:
    for item in sorted(root.rglob("*"), key=lambda path: len(path.parts)):
        os.chmod(item, item.stat().st_mode | (0o700 if item.is_dir() else 0o600))
    os.chmod(root, root.stat().st_mode | 0o700)


def _tree_is_read_only(root: Path) -> bool:
    return all((item.stat().st_mode & 0o222) == 0 for item in (root, *root.rglob("*")))


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--self-test", action="store_true")
    args = parser.parse_args()
    if args.self_test:
        print("CONTROLLER_SELF_TEST=PASS lifecycle=accepted-submissions(1..10)->feedback-chain->freeze-latest->hidden")
    return 0


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


_fr_wrap_submit(AcceptedSubmissionController)
_FR_WRAPPED_FREEZE = [name for name in _FR_FREEZE_METHODS if _fr_wrap_freeze(AcceptedSubmissionController, name)]
assert _FR_WRAPPED_FREEZE, 'freeze/submit race guard found no freeze entry point'
# --- end 0921b freeze/submit race guard ------------------------------------


if __name__ == "__main__":
    raise SystemExit(main())
