#!/usr/bin/env python3
"""Fail-closed bounded lifecycle controller for the OpenClaw Edit case.

The controller owns up to ten accepted Candidate snapshots and the canonical
latest-accepted frozen tree. Lower-agent executions always receive a disposable
copy made from that tree; neither development nor hidden evaluation is allowed
to use the canonical tree as a scratch directory. The historical class and
``freeze_candidate_2`` alias remain for compatibility with old probe tests; the
live lifecycle is not limited to two rounds.
"""
from __future__ import annotations

import hashlib
import json
import math
import os
import shutil
import stat
import time
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

from controller.dev_feedback import semantic_feedback

DEV = ("dev_001", "dev_002")
HIDDEN = tuple(f"test_{index:03d}" for index in range(1, 7))
IGNORED_TREE_PARTS = {".git", "node_modules", "dist", ".artifacts", "__pycache__"}


def now() -> str:
    return datetime.now(timezone.utc).isoformat()


def parse_time(value: str) -> datetime:
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        raise ValueError("lifecycle timestamps must include a timezone")
    return parsed


def tree_digest(root: Path, *, deadline: float | None = None) -> str:
    """Match the Create digest exactly; optionally bound enumeration/reading."""
    if deadline is not None and not math.isfinite(deadline):
        raise ValueError('tree digest deadline must be finite')
    def check():
        if deadline is not None and time.monotonic()>=deadline:
            raise TimeoutError('tree digest exceeded its enclosing deadline')
    check()
    if not root.is_dir():
        raise FileNotFoundError(f"candidate tree missing: {root}")
    digest = hashlib.sha256()
    paths=[]
    for path in root.rglob('*'):
        check();paths.append(path)
    paths.sort(key=lambda item:item.relative_to(root).as_posix())
    check()
    for path in paths:
        check()
        relative = path.relative_to(root).as_posix().encode()
        if path.is_symlink():
            kind, data = b"L", os.readlink(path).encode()
        elif path.is_file():
            digest.update(b"F"); digest.update(len(relative).to_bytes(8, "big")); digest.update(relative)
            digest.update(path.stat().st_size.to_bytes(8, "big"))
            with path.open("rb") as handle:
                while True:
                    check();chunk=handle.read(1024*1024);check()
                    if not chunk:break
                    digest.update(chunk)
            continue
        elif path.is_dir():
            continue
        else:
            kind, data = b"O", b""
        digest.update(kind)
        digest.update(len(relative).to_bytes(8, "big"))
        digest.update(relative)
        digest.update(len(data).to_bytes(8, "big"))
        digest.update(data)
    check()
    return digest.hexdigest()


def make_tree_read_only(root: Path) -> None:
    """Remove write bits without following links outside the frozen tree."""
    for path in sorted(root.rglob("*"), key=lambda item: len(item.parts), reverse=True):
        if path.is_symlink():
            continue
        mode = stat.S_IMODE(path.stat().st_mode)
        path.chmod(mode & ~0o222)
    root.chmod(stat.S_IMODE(root.stat().st_mode) & ~0o222)


def tree_is_read_only(root: Path) -> bool:
    paths = [root, *(path for path in root.rglob("*") if not path.is_symlink())]
    return all(stat.S_IMODE(path.stat().st_mode) & 0o222 == 0 for path in paths)


def feedback_projection(result: dict[str, Any]) -> dict[str, Any]:
    """Return Builder-visible feedback without evaluator filesystem facts."""
    projection: dict[str, Any] = {}
    for case_id, value in result.items():
        if not isinstance(value, dict):
            projection[case_id] = {"classification": "invalid-evaluator-record"}
            continue
        item = {
            "classification": value.get("classification", "unknown"),
            "classification_reason": value.get("classification_reason", ""),
            "artifact_valid": (value.get("artifact_contract") or {}).get("valid") if isinstance(value.get("artifact_contract"), dict) else None,
            "broker_calls": (value.get("broker_stats_delta") or {}).get("calls") if isinstance(value.get("broker_stats_delta"), dict) else None,
            "broker_successful_calls": (value.get("broker_stats_delta") or {}).get("successful_calls") if isinstance(value.get("broker_stats_delta"), dict) else None,
            "result": value.get("dev_feedback", {"score": None, "valid": False}),
        }
        projection[case_id] = item
    return projection


def feedback_digest_for_record(run_dir: Path, record: "Candidate") -> str | None:
    """Return the evaluator-owned digest for one accepted round's feedback."""
    path = run_dir / "feedback" / f"candidate_{record.number:03d}.json"
    if not path.is_file():
        return None
    return hashlib.sha256(path.read_bytes()).hexdigest()


def validate_freeze_manifest(manifest: dict[str, Any], frozen_tree: Path) -> None:
    required = {
        "accepted_candidate_digests",
        "accepted_submission_count",
        "candidate_digest",
        "source_submission",
        "source_submission_id",
        "feedback_digest",
        "feedback_chain_complete",
        "freeze_reason",
        "frozen_at",
    }
    missing = sorted(required - manifest.keys())
    if missing:
        raise ValueError(f"freeze manifest missing fields: {missing}")
    if manifest.get("schema_version") != "openclaw-agentloop-freeze-v3":
        raise ValueError("unsupported freeze manifest schema")
    digests = manifest.get("accepted_candidate_digests")
    if not isinstance(digests, list) or not digests or len(set(digests)) != len(digests):
        raise ValueError("accepted Candidate digests must be non-empty and distinct")
    if manifest["candidate_digest"] != digests[-1]:
        raise ValueError("canonical frozen digest must equal the latest accepted Candidate")
    if manifest.get("accepted_submission_count") != len(digests):
        raise ValueError("accepted_submission_count must equal the accepted digest ledger length")
    if manifest.get("source_submission") != len(digests):
        raise ValueError("source_submission must be the latest accepted round")
    if manifest.get("source_submission_id") != f"candidate-{len(digests):03d}":
        raise ValueError("source_submission_id must identify the latest accepted round")
    if manifest.get("feedback_chain_complete") is not True:
        raise ValueError("freeze manifest does not attest a complete feedback chain")
    if manifest.get("freeze_reason") not in {"max_dev_rounds", "builder_exit"}:
        raise ValueError("invalid freeze reason")
    if tree_digest(frozen_tree) != manifest["candidate_digest"]:
        raise ValueError("canonical frozen tree digest does not match manifest")
    if not manifest.get("frozen_tree_read_only") or not tree_is_read_only(frozen_tree):
        raise ValueError("canonical frozen tree is writable")
    latest_feedback = parse_time(str(manifest["latest_feedback_at"]))
    frozen_at = parse_time(str(manifest["frozen_at"]))
    if latest_feedback > frozen_at:
        raise ValueError("latest Candidate feedback occurs after freeze")


@dataclass
class Candidate:
    number: int
    digest: str
    path: str
    submitted_at: str
    dev_started_at: str
    dev_completed_at: str
    feedback_at: str
    dev: dict[str, Any] = field(default_factory=dict)
    classification: str = "pending"


class TwoRoundController:
    def __init__(
        self,
        run_dir: Path,
        evaluate: Callable[[int, Path], dict[str, Any]],
        *,
        dev_case_ids: tuple[str, ...] = DEV,
        hidden_case_ids: tuple[str, ...] = HIDDEN,
        evidence_kind: str = "formal",
        max_dev_rounds: int = 10,
        n_concurrent: int = 1,
    ) -> None:
        self.run_dir = run_dir.resolve()
        self.evaluate = evaluate
        self.dev_case_ids = tuple(dev_case_ids)
        self.hidden_case_ids = tuple(hidden_case_ids)
        self.evidence_kind = evidence_kind
        if not self.dev_case_ids or not set(self.dev_case_ids).issubset(DEV):
            raise ValueError("dev_case_ids must be a non-empty subset of the canonical public inventory")
        if not self.hidden_case_ids or not set(self.hidden_case_ids).issubset(HIDDEN):
            raise ValueError("hidden_case_ids must be a non-empty subset of the canonical hidden inventory")
        if evidence_kind not in {"formal", "pilot", "probe"}:
            raise ValueError("evidence_kind must be formal, pilot, or probe")
        if not 1 <= max_dev_rounds <= 10:
            raise ValueError("max_dev_rounds must be in 1..10")
        if n_concurrent != 1:
            raise ValueError("n_concurrent must equal 1")
        self.max_dev_rounds = max_dev_rounds
        self.n_concurrent = n_concurrent
        # Set by the session layer under the readiness profile. It owns the
        # delivery snapshots this controller never sees, and the freeze has to
        # attest them itself rather than have an exporter assert them later.
        self.readiness_fields: Any = None
        self.records: list[Candidate] = []
        self.infrastructure_attempts: list[dict[str, Any]] = []
        self.frozen: dict[str, Any] | None = None
        self.hidden_has_run = False
        self.hidden_state: dict[str, Any] | None = None

    def submit(self, candidate: Path) -> Candidate:
        if self.frozen:
            raise RuntimeError("candidate is already frozen")
        if len(self.records) >= self.max_dev_rounds:
            raise RuntimeError("max_dev_rounds reached")
        source = candidate.resolve()
        source_digest = tree_digest(source)
        if any(record.digest == source_digest for record in self.records):
            return next(record for record in self.records if record.digest == source_digest)

        number = len(self.records) + 1
        submitted_at = now()
        candidate_root = self.run_dir / "candidates"
        snapshot = candidate_root / f"candidate_{number:03d}"
        if snapshot.exists():
            # Preserve an infrastructure-invalid attempt without consuming an
            # accepted round.  This also makes a later retry auditable instead
            # of deleting the first attempt's evidence.
            retry = 1
            while snapshot.exists():
                snapshot = candidate_root / f"candidate_{number:03d}-retry-{retry:03d}"
                retry += 1
        snapshot.parent.mkdir(parents=True, exist_ok=True)
        shutil.copytree(
            source,
            snapshot,
            symlinks=True,
            ignore=shutil.ignore_patterns(*IGNORED_TREE_PARTS),
        )
        snapshot_digest = tree_digest(snapshot)
        if snapshot_digest != source_digest:
            raise RuntimeError("Candidate snapshot digest differs from submission")

        dev_started_at = now()
        result = self.evaluate(number, snapshot)
        dev_completed_at = now()
        if set(result) != set(self.dev_case_ids):
            raise RuntimeError("every round must evaluate exactly the configured public dev inventory")
        if tree_digest(snapshot) != snapshot_digest:
            raise RuntimeError("development evaluation mutated the Candidate snapshot")

        judged_feedback = {case: semantic_feedback(value, case, snapshot_digest)
                           for case, value in result.items()}
        for case, feedback_value in judged_feedback.items():
            result[case]["dev_feedback"] = feedback_value
        classification = (
            "infrastructure-invalid"
            if not all(item["valid"] for item in judged_feedback.values()) or any(
                isinstance(value, dict)
                and value.get("classification") in {
                    "infrastructure-invalid",
                    "broker_infrastructure_error",
                    "launcher_infrastructure_error",
                }
                for value in result.values()
            )
            else "completed"
        )
        feedback_at = now()
        record = Candidate(
            number=number,
            digest=snapshot_digest,
            path=str(snapshot),
            submitted_at=submitted_at,
            dev_started_at=dev_started_at,
            dev_completed_at=dev_completed_at,
            feedback_at=feedback_at,
            dev=result,
            classification=classification,
        )
        feedback = self.run_dir / "feedback" / f"candidate_{number:03d}.json"
        if classification == "infrastructure-invalid":
            # Preserve failed attempts instead of overwriting evidence when
            # the same accepted round number is retried.
            feedback = self.run_dir / "feedback" / f"infrastructure_attempt_{len(self.infrastructure_attempts)+1:03d}.json"
        feedback.parent.mkdir(parents=True, exist_ok=True)
        feedback.write_text(
            json.dumps(
                {
                    "candidate_number": number,
                    "candidate_digest": snapshot_digest,
                    "dev_results": feedback_projection(result),
                    "feedback_at": feedback_at,
                    "authoritative": True,
                    "dev_scores": {case: item["score"] for case, item in judged_feedback.items()},
                    "dev_passed": classification == "completed" and sum(item["score"] for item in judged_feedback.values()) / len(result) > 60,
                },
                indent=2,
                ensure_ascii=False,
            )
            + "\n",
            encoding="utf-8",
        )
        if classification != "infrastructure-invalid":
            if not post_freeze_superseded(self, record):
                self.records.append(record)
        else:
            self.infrastructure_attempts.append(asdict(record))
        self._write("dev_lifecycle.json", {
            "schema_version": "openclaw-dev-lifecycle-0905-v1", "evidence_kind": self.evidence_kind,
            "max_dev_rounds": self.max_dev_rounds, "n_concurrent": self.n_concurrent,
            "dev_passed_is_automatic_freeze": False, "records": [asdict(item) for item in self.records],
            "infrastructure_attempts": self.infrastructure_attempts,
        })
        if classification != "infrastructure-invalid" and len(self.records) == self.max_dev_rounds:
            self.freeze_latest("max_dev_rounds")
        return record

    def freeze_latest(self, reason: str = "builder_exit") -> dict[str, Any]:
        if self.frozen:
            return dict(self.frozen)
        if reason not in {"max_dev_rounds", "builder_exit"}:
            raise RuntimeError("invalid freeze reason")
        if not self.records:
            raise RuntimeError("freeze requires at least one accepted Candidate")
        latest = self.records[-1]
        source = Path(latest.path)
        if tree_digest(source) != latest.digest:
            raise RuntimeError("latest Candidate snapshot changed before freeze")
        frozen_tree = self.run_dir / "frozen_candidate"
        if frozen_tree.exists():
            raise FileExistsError("canonical frozen Candidate already exists")
        shutil.copytree(
            source,
            frozen_tree,
            symlinks=True,
            ignore=shutil.ignore_patterns(*IGNORED_TREE_PARTS),
        )
        copied_digest = tree_digest(frozen_tree)
        if copied_digest != latest.digest:
            raise RuntimeError("frozen copy digest differs from latest Candidate")
        make_tree_read_only(frozen_tree)
        if not tree_is_read_only(frozen_tree):
            raise RuntimeError("failed to make frozen Candidate immutable")
        feedback_path = self.run_dir / "feedback" / f"candidate_{latest.number:03d}.json"
        feedback_digest = hashlib.sha256(feedback_path.read_bytes()).hexdigest() if feedback_path.is_file() else None
        feedback_chain_complete = bool(self.records) and all(
            feedback_digest_for_record(self.run_dir, record) is not None
            for record in self.records
        )
        self.frozen = {
            "schema_version": "openclaw-agentloop-freeze-v3",
            "evidence_kind": self.evidence_kind,
            "public_case_inventory": list(self.dev_case_ids),
            "hidden_case_inventory": list(self.hidden_case_ids),
            "accepted_candidate_digests": [record.digest for record in self.records],
            "accepted_submission_count": len(self.records),
            "candidate_digest": copied_digest,
            "source_submission": latest.number,
            "source_submission_id": f"candidate-{latest.number:03d}",
            "latest_submitted_at": latest.submitted_at,
            "latest_dev_completed_at": latest.dev_completed_at,
            "latest_feedback_at": latest.feedback_at,
            "feedback_digest": feedback_digest,
            "feedback_consumed": True,
            "feedback_chain_complete": feedback_chain_complete,
            "freeze_reason": reason,
            "max_dev_rounds": self.max_dev_rounds,
            "n_concurrent": self.n_concurrent,
            "dev_passed_is_automatic_freeze": False,
            "frozen_at": now(),
            "frozen_tree_read_only": True,
            "hidden_allowed": True,
            "dev_evaluated": True,
            "credential_mounted_to_candidate": False,
            "hidden_started_at": None,
            "hidden_completed_at": None,
            "hidden_attestation": None,
        }
        if self.readiness_fields is not None:
            self.frozen.update(self.readiness_fields())
        self._write("freeze_manifest.json", self.frozen)
        return dict(self.frozen)

    def freeze_candidate_2(self) -> dict[str, Any]:
        """Compatibility alias; formal semantics freeze the latest accepted snapshot."""
        return self.freeze_latest("builder_exit")

    def run_hidden(
        self,
        evaluate_hidden: Callable[[Path], dict[str, Any]],
    ) -> dict[str, Any]:
        """Compatibility hook for an evaluator-owned six-case executor.

        The production CLI lives in evaluator/hidden_executor.py.  This hook is
        intentionally strict so a callback cannot replace the six hidden runs
        with a single scheduled or aggregate record.
        """
        if not self.frozen:
            raise RuntimeError("hidden evaluation before freeze")
        state_path = self.run_dir / "hidden_execution_state.json"
        if (self.hidden_has_run or self.frozen.get("hidden_started_at") is not None
                or state_path.exists() or state_path.is_symlink()):
            raise RuntimeError("hidden evaluation replay is forbidden")
        freeze_path = self.run_dir / "freeze_manifest.json"
        freeze_bytes = freeze_path.read_bytes()
        if json.loads(freeze_bytes) != self.frozen:
            raise RuntimeError("in-memory freeze differs from immutable manifest")
        freeze_sha256 = hashlib.sha256(freeze_bytes).hexdigest()
        frozen_tree = self.run_dir / "frozen_candidate"
        if tree_digest(frozen_tree) != self.frozen.get("candidate_digest") or not tree_is_read_only(frozen_tree):
            raise RuntimeError("frozen Candidate failed digest/read-only validation")
        digest_before = tree_digest(frozen_tree)
        self.hidden_state = {
            "schema_version": "openclaw-hidden-execution-state-v1",
            "freeze_manifest_sha256": freeze_sha256,
            "candidate_digest": digest_before,
            "hidden_started_at": now(), "hidden_completed_at": None,
            "hidden_attestation": None, "state": "started",
        }
        # Persist a one-shot reservation before any model/product dispatch.
        # A failed/unknown callback remains reserved across a new controller.
        with state_path.open("x") as handle:
            json.dump(self.hidden_state, handle, indent=2)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        self.hidden_has_run = True
        result = evaluate_hidden(frozen_tree)
        if set(result) != set(self.hidden_case_ids):
            raise RuntimeError("hidden executor must return exactly the configured hidden case records")
        digest_after = tree_digest(frozen_tree)
        if digest_after != digest_before:
            raise RuntimeError("hidden evaluation mutated the frozen Candidate")
        if freeze_path.read_bytes() != freeze_bytes:
            raise RuntimeError("hidden evaluation mutated the immutable freeze manifest")
        self.hidden_state.update(hidden_completed_at=now(),
            hidden_attestation="hidden-after-freeze-attestation.json", state="completed")
        self._write("hidden_execution_state.json", self.hidden_state)
        self._write("hidden_result.json", result)
        self._write(
            "hidden-after-freeze-attestation.json",
            {
                "schema_version": "openclaw-hidden-attestation-v1",
                "case_ids": list(self.hidden_case_ids),
                "evidence_kind": self.evidence_kind,
                "freeze_manifest_sha256": freeze_sha256,
                "hidden_started_at": self.hidden_state["hidden_started_at"],
                "hidden_completed_at": self.hidden_state["hidden_completed_at"],
                "freeze_before_hidden": parse_time(self.frozen["frozen_at"])
                <= parse_time(self.hidden_state["hidden_started_at"]),
                "candidate_digest": digest_before,
                "candidate_digest_stable": digest_before == digest_after,
                "scheduled_records_used": False,
                "formal_result_claimed": False,
            },
        )
        return result

    def _write(self, name: str, value: Any) -> None:
        path = self.run_dir / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            json.dumps(value, indent=2, ensure_ascii=False) + "\n",
            encoding="utf-8",
        )


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


_fr_wrap_submit(TwoRoundController)
_FR_WRAPPED_FREEZE = [name for name in _FR_FREEZE_METHODS if _fr_wrap_freeze(TwoRoundController, name)]
assert _FR_WRAPPED_FREEZE, 'freeze/submit race guard found no freeze entry point'
# --- end 0921b freeze/submit race guard ------------------------------------
