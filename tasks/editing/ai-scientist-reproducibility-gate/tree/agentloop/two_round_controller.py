#!/usr/bin/env python3
"""Accepted Candidate -> public feedback -> optional revisions -> frozen hidden lifecycle.

The controller owns Candidate snapshots and evaluator execution.  It never
creates a Candidate: a single Builder session submits one to ten deliveries via
the socket exposed by ``harbor/formal_one_stop.py``.  Static callers may use
``--dry-run`` for protocol checks, but smoke evidence is permanently labelled
and is not formal evidence.
"""

from __future__ import annotations

import argparse
import copy
import fcntl
import hashlib
import json
import os
import shutil
import stat
import subprocess
import sys
import tempfile
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

try:
    from .candidate_adapter import build_candidate
    from .protocol import sha256_file, tree_digest, validate_delivery, write_json
    from .stage_recovery import CaseStages, RecoveryError, execution_identity, immutable_json, read as read_checkpoint
except ImportError:  # direct execution from the agentloop directory
    from candidate_adapter import build_candidate
    from protocol import sha256_file, tree_digest, validate_delivery, write_json
    from stage_recovery import CaseStages, RecoveryError, execution_identity, immutable_json, read as read_checkpoint

try:
    from .stable_product import product_source_digest
    from .product_registry import ProductRegistry
    from .lower_request_identity import resolve_binding
except ImportError:
    from stable_product import product_source_digest
    from product_registry import ProductRegistry
    from lower_request_identity import resolve_binding

DEV_CASES = ("dev_001", "dev_002")
HIDDEN_CASES = ("test_001", "test_002", "test_003", "test_004", "test_005", "test_006")
INFRA_CLASSIFICATIONS = {
    "infrastructure-invalid",
    "launcher_infrastructure_error",
    "evaluator_infrastructure_error",
    "broker_infrastructure_error",
    "provider_infrastructure_error",
    "credential_infrastructure_error",
    "mount_infrastructure_error",
    "runtime_dependency_infrastructure_error",
    "provider_failure",
}


def now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _parse_timestamp(value: object) -> datetime | None:
    if not isinstance(value, str) or not value.strip():
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo is not None else parsed.replace(tzinfo=timezone.utc)


def _all_started_after_freeze(
    freeze_at: object, hidden_started_at: object, attestations: list[dict[str, Any]]
) -> bool:
    freeze_time = _parse_timestamp(freeze_at)
    hidden_start = _parse_timestamp(hidden_started_at)
    return bool(
        freeze_time is not None
        and hidden_start is not None
        and hidden_start >= freeze_time
        and attestations
        and all(
            (started := _parse_timestamp(entry.get("started_at"))) is not None
            and started >= freeze_time
            for entry in attestations
        )
    )


def _make_tree_read_only(root: Path) -> None:
    """Remove write bits without following symlinks."""
    for path in sorted(root.rglob("*"), key=lambda item: len(item.parts), reverse=True):
        if path.is_symlink():
            continue
        path.chmod(stat.S_IMODE(path.stat().st_mode) & ~0o222)
    root.chmod(stat.S_IMODE(root.stat().st_mode) & ~0o222)


def _broker_counts(result: dict[str, Any]) -> tuple[int, int, int]:
    broker = result.get("broker") if isinstance(result.get("broker"), dict) else {}
    calls = int(broker.get("calls_delta", broker.get("calls", 0)) or 0)
    failures = int(broker.get("failures_delta", broker.get("failures", 0)) or 0)
    successful = int(broker.get("successful_calls", max(0, calls - failures)) or 0)
    return calls, failures, successful


class Controller:
    """Evaluator-owned lifecycle for up to ten accepted Candidate snapshots."""

    def __init__(
        self,
        benchmark: Path,
        run_dir: Path,
        broker_endpoint: str,
        dry_run: bool = False,
        *,
        image: str | None = None,
        dependency_overlay: Path | None = None,
        public_case_ids: Iterable[str] = DEV_CASES,
        hidden_case_ids: Iterable[str] = HIDDEN_CASES,
        run_kind: str | None = None,
        max_dev_rounds: int = 10,
        n_concurrent: int = 1,
        result_judge_endpoint: str | None = None,
        defer_max_rounds_freeze: bool = False,
        lower_executor: Any | None = None,
        readiness_profile: str | None = None,
        current_binding: dict[str, Any] | None = None,
    ) -> None:
        self.benchmark = benchmark.resolve()
        self.run_dir = run_dir.resolve()
        self.broker_endpoint = broker_endpoint
        self.result_judge_endpoint = result_judge_endpoint
        self.dry_run = dry_run
        self.image = image
        self.dependency_overlay = dependency_overlay.resolve() if dependency_overlay else None
        self.public_case_ids = tuple(public_case_ids)
        self.hidden_case_ids = tuple(hidden_case_ids)
        if not self.public_case_ids or not set(self.public_case_ids).issubset(DEV_CASES):
            raise ValueError("public_case_ids must be a non-empty subset of the canonical public inventory")
        if not self.hidden_case_ids or not set(self.hidden_case_ids).issubset(HIDDEN_CASES):
            raise ValueError("hidden_case_ids must be a non-empty subset of the canonical hidden inventory")
        self.run_kind = "smoke" if dry_run else (run_kind or "formal")
        if self.run_kind not in {"formal", "pilot", "smoke"}:
            raise ValueError("run_kind must be formal, pilot, or smoke")
        if not 1 <= max_dev_rounds <= 10:
            raise ValueError("max_dev_rounds must be in 1..10")
        if n_concurrent != 1:
            raise ValueError("n_concurrent must equal 1")
        if type(defer_max_rounds_freeze) is not bool:
            raise ValueError("defer_max_rounds_freeze must be a bool")
        self.defer_max_rounds_freeze = defer_max_rounds_freeze
        if lower_executor is not None and not callable(lower_executor):
            raise ValueError("lower_executor must be evaluator callable or None")
        self._lower_executor = lower_executor
        self.readiness_profile = readiness_profile
        self.current_binding = copy.deepcopy(current_binding) if current_binding is not None else None
        if readiness_profile is not None:
            if readiness_profile != 'single-dev-two-round-hidden-smoke-v1' or self.public_case_ids != ('dev_001',) or self.hidden_case_ids != ('test_001',) or max_dev_rounds != 2:
                raise ValueError('invalid AI Scientist readiness profile')
            if not isinstance(self.current_binding, dict) or self.current_binding.get('task') != 'ai-scientist':
                raise ValueError('readiness binding required')
            from .readiness import validate_binding
            validate_binding(self.current_binding)
            if self.run_kind!='pilot': raise ValueError('readiness requires nonformal pilot')
            self.defer_max_rounds_freeze=True
        self.builder_exit_evidence=None
        self.max_dev_rounds = max_dev_rounds
        self.n_concurrent = n_concurrent
        self.records: list[dict[str, Any]] = []
        self.infrastructure_attempts: list[dict[str, Any]] = []
        self.terminal_infrastructure_latch: dict[str, Any] | None = None
        self.frozen: dict[str, Any] | None = None
        self.feedback: dict[str, Any] | None = None
        self.feedback_digest: str | None = None
        self.run_dir.mkdir(parents=True, exist_ok=True)
        self.product_registry = ProductRegistry(self.run_dir / 'product_executions')
        self._load_state()

    def _load_state(self) -> None:
        state_path = self.run_dir / "dev_lifecycle.json"
        self.terminal_infrastructure_latch = None
        if state_path.is_file():
            state = json.loads(state_path.read_text(encoding="utf-8"))
            if not isinstance(state, dict):
                raise RuntimeError("controller lifecycle must be an object")
            existing_kind = state.get("evidence_kind")
            if existing_kind and existing_kind != self.run_kind:
                raise RuntimeError(f"run directory is {existing_kind}, cannot mix with {self.run_kind}")
            records = state.get("records", [])
            if not isinstance(records, list):
                raise RuntimeError("controller lifecycle records must be a list")
            self.records = records
            attempts = state.get("infrastructure_attempts", [])
            if not isinstance(attempts, list) or any(not isinstance(item, dict) for item in attempts):
                raise RuntimeError("controller infrastructure attempts must be a list of objects")
            self.infrastructure_attempts = attempts
            latch = state.get("terminal_infrastructure_latch")
            # A lifecycle written before the latch field was introduced can
            # still contain a terminal infrastructure attempt.  Reconstruct
            # the immutable first-terminal identity before accepting any new
            # Candidate, otherwise a distinct retry could bypass the stop.
            if latch is None:
                prior_terminal = next(
                    (item for item in attempts
                     if isinstance(item, dict)
                     and item.get("state") in {"public_infrastructure_invalid", "infrastructure_error"}),
                    None,
                )
                if prior_terminal is not None:
                    latch = self._terminal_latch_value(prior_terminal)
            if latch is not None:
                if not isinstance(latch, dict):
                    raise RuntimeError("controller terminal infrastructure latch must be an object")
                if (
                    latch.get("schema_version") != "agentswe-ai-scientist-terminal-infrastructure-latch-v1"
                    or latch.get("state") not in {"public_infrastructure_invalid", "infrastructure_error"}
                    or latch.get("classification_axis") != "infrastructure"
                    or latch.get("round_consumed") is not False
                    or not isinstance(latch.get("candidate_digest"), str)
                    or len(latch["candidate_digest"]) != 64
                    or type(latch.get("submission_number")) is not int
                    or latch.get("submission_number") < 1
                    or latch.get("retry_allowed") is not False
                ):
                    raise RuntimeError("controller terminal infrastructure latch is invalid")
                self.terminal_infrastructure_latch = copy.deepcopy(latch)
            frozen = state.get("frozen")
            self.frozen = frozen if isinstance(frozen, dict) else None
        latest_number = max(
            (int(record.get("submission_number", 0)) for record in self.records if isinstance(record, dict)),
            default=0,
        )
        feedback_paths = sorted(self.run_dir.glob("feedback/candidate_*.json"))
        feedback_path = self.run_dir / "feedback" / f"candidate_{latest_number:03d}.json"
        if latest_number and feedback_path.is_file():
            value = json.loads(feedback_path.read_text(encoding="utf-8"))
            if isinstance(value, dict):
                self.feedback = value
                self.feedback_digest = sha256_file(feedback_path)
        elif feedback_paths:
            # A partially written lifecycle may have no accepted record yet;
            # do not silently bind a stale feedback file to a new submission.
            self.feedback = None
            self.feedback_digest = None

    @staticmethod
    def _axis(result: dict[str, Any]) -> str:
        explicit = result.get("classification_axis")
        if explicit in {"candidate", "infrastructure"}:
            return str(explicit)
        classification = str(result.get("classification", "unclassified"))
        if classification in INFRA_CLASSIFICATIONS or classification.endswith("_infrastructure_error"):
            return "infrastructure"
        return "candidate"

    @staticmethod
    def _terminal_latch_value(record: dict[str, Any]) -> dict[str, Any] | None:
        state = record.get("state")
        if state not in {"public_infrastructure_invalid", "infrastructure_error"}:
            return None
        if record.get("round_consumed") is not False:
            raise RuntimeError("terminal infrastructure result must have round_consumed=false")
        digest = record.get("candidate_digest")
        if not isinstance(digest, str) or len(digest) != 64:
            raise RuntimeError("terminal infrastructure result is missing its Candidate digest")
        return {
            "schema_version": "agentswe-ai-scientist-terminal-infrastructure-latch-v1",
            "latched_at": now(),
            "submission_number": record.get("submission_number"),
            "candidate_digest": digest,
            "state": state,
            "classification_axis": "infrastructure",
            "round_consumed": False,
            "error": record.get("error"),
            "retry_allowed": False,
        }

    def _latch_terminal_infrastructure(self, record: dict[str, Any]) -> bool:
        value = self._terminal_latch_value(record)
        if value is None:
            return False
        if self.terminal_infrastructure_latch is None:
            self.terminal_infrastructure_latch = value
            record["terminal_latch_created"] = True
            return True
        if self.terminal_infrastructure_latch.get("candidate_digest") != value["candidate_digest"]:
            raise RuntimeError("terminal infrastructure latch changed inside the submission lock")
        record["terminal_latch_created"] = False
        return False

    def distinct_terminal_rejection(self, candidate: Path) -> dict[str, Any] | None:
        latch = self.terminal_infrastructure_latch
        if latch is None:
            return None
        try:
            from .readiness import delivery_digest
            candidate_digest = delivery_digest(candidate) if self.readiness_profile else tree_digest(candidate)
        except (OSError, ValueError):
            candidate_digest = None
        if candidate_digest == latch.get("candidate_digest"):
            # Exact-digest stage recovery retains completed/unknown calls and
            # is governed by the existing CaseStages recovery contract.
            return None
        return {
            "accepted": False,
            "submission_number": latch.get("submission_number"),
            "candidate_digest": candidate_digest,
            "state": "submission_rejected_after_infrastructure_terminal",
            "error": "distinct_submission_rejected_after_infrastructure_terminal",
            "classification_axis": "infrastructure",
            "round_consumed": False,
            "retry_allowed": False,
            "build_started": False,
            "provider_dispatch": False,
            "terminal_failure": copy.deepcopy(latch),
        }

    def _run_case(self, candidate_repo: Path, case_id: str, out: Path, *, hidden: bool = False) -> dict[str, Any]:
        case_file = self.benchmark / "agentloop" / "cases" / case_id / "case_input.json"
        command = [
            sys.executable, "-E", "-s", "-B",
            str(self.benchmark / "agentloop" / "lower_agent_launcher.py"),
            "--candidate-repository",
            str(candidate_repo),
            "--case-file",
            str(case_file),
            "--output-dir",
            str(out),
            "--broker-endpoint",
            self.broker_endpoint,
        ]
        if self.image:
            command.extend(["--image", self.image])
        if self.dependency_overlay:
            command.extend(["--dependency-overlay", str(self.dependency_overlay)])
        if self.dry_run:
            command.append("--dry-run")
        else:
            # The public caller cannot select a signing key. The controller
            # resolves only its own run's registered private broker binding.
            binding = resolve_binding(self.run_dir.parent, self.broker_endpoint)
            command.extend(["--logical-context-binding", str(binding)])
        def execute_lower():
            started = time.monotonic()
            started_at = now()
            try:
                completed = (self._lower_executor or subprocess.run)(
                    command, cwd=self.benchmark / "agentloop", text=True,
                    capture_output=True, check=False,
                )
            except OSError as exc:
                # An arbitrary pipe/communication OSError may happen AFTER
                # a child has started. Only an identified exec/cwd failure is
                # evidence that no lower invocation began.
                not_started = (isinstance(exc, (FileNotFoundError, PermissionError))
                               and exc.filename in {command[0], str(self.benchmark / "agentloop")})
                result = {"case_id": case_id, "valid": False,
                          "classification": "launcher_infrastructure_error",
                          "classification_axis": "infrastructure", "dispatch_not_started": not_started,
                          "error": f"{type(exc).__name__}: {exc}"}
            else:
                result_path = out / "launcher_result.json"
                try:
                    value = json.loads(result_path.read_text(encoding="utf-8"))
                    if not isinstance(value, dict):
                        raise ValueError("launcher_result must be an object")
                    result = value
                except (OSError, ValueError) as exc:
                    result = {"case_id": case_id, "valid": False,
                              "classification": "launcher_infrastructure_error",
                              "classification_axis": "infrastructure",
                              "exit_code": completed.returncode,
                              "error": f"launcher evidence unavailable: {type(exc).__name__}",
                              "stderr": completed.stderr[-2000:]}
            result.update({"case_id": case_id, "classification_axis": self._axis(result),
                           "evidence_kind": self.run_kind, "real_execution": bool(result.get("real_execution", False)),
                           "output_path": str(out), "controller_started_at": started_at,
                           "controller_finished_at": now(), "elapsed_seconds": round(time.monotonic() - started, 3)})
            return result

        def evaluate():
            try:
                sys.path.insert(0, str(self.benchmark / "evaluator"))
                from case_evidence import score_case
                return score_case(case_id=case_id, record_path=out / "launcher_result.json",
                    candidate=candidate_repo, candidate_digest=tree_digest(candidate_repo),
                    output=out / "semantic_result", broker_endpoint=self.result_judge_endpoint)
            except Exception as exc:
                return {"classification": "unresolved", "score": None, "contract_valid": False,
                        "round_consumed": False, "reason": "evaluator evidence/scoring failed: " + type(exc).__name__}

        stages = None
        try:
            # Public dev recovery retains a single lower rollout per Candidate
            # and case. Hidden still runs only through its post-freeze caller.
            if case_id in self.public_case_ids:
                identity = execution_identity(self, tree_digest(candidate_repo))
                stages = CaseStages(out, {**identity, "case_id": case_id, "output": str(out)})
            result = stages.lower(execute_lower) if stages else execute_lower()
            if case_id in self.public_case_ids and not self.dry_run:
                if self._axis(result) == "infrastructure":
                    evaluation = {"classification": "infrastructure_invalid", "score": None,
                                  "contract_valid": False, "round_consumed": False,
                                  "reason": "lower stage is unresolved; automatic lower replay forbidden"}
                elif self.readiness_profile:
                    from .readiness import execution_valid
                    ok=execution_valid(result)
                    evaluation={'classification':'readiness_execution','score':None,'contract_valid':ok,
                                'round_consumed':ok,'readiness_only':True,'semantic_judge_called':False}
                else:
                    evaluation = stages.score(evaluate)
                result["result_evaluation"] = evaluation
                if not evaluation.get("contract_valid"):
                    result["lower_classification"] = result.get("classification")
                    result["classification"] = "evaluator_evidence_unresolved"
                    result["classification_axis"] = "infrastructure"
        except RecoveryError as exc:
            result = {"case_id": case_id, "valid": False, "real_execution": False,
                      "classification": "stage_recovery_infrastructure_error", "classification_axis": "infrastructure",
                      "error": str(exc), "output_path": str(out), "evidence_kind": self.run_kind,
                      "result_evaluation": {"score": None, "contract_valid": False, "round_consumed": False}}
        if stages:
            observation = stages.observe(result)
            result["controller_observation_path"] = str(observation)
            result["controller_observation_sha256"] = sha256_file(observation)
        if not (out / "controller_observation.json").exists():
            immutable_json(out / "controller_observation.json", result)
        return result

    def _dev_gate(self, record: dict[str, Any]) -> tuple[bool, list[str]]:
        if self.readiness_profile:
            from .readiness import execution_valid
            reasons=[]
            if record.get('build',{}).get('valid') is not True: reasons.append('build_not_ready')
            if set(record.get('dev',{}))!={'dev_001'} or not execution_valid(record.get('dev',{}).get('dev_001',{})): reasons.append('public_execution_incomplete')
            return not reasons,reasons
        reasons: list[str] = []
        build = record.get("build") if isinstance(record.get("build"), dict) else {}
        build_zero = False
        if build.get("valid") is not True:
            try:
                from agentloop.build_evidence import verified_build_failure
                root = Path(record['attempt_paths']['build'])
                verified_build_failure(build, root, root / 'repository')
                build_zero = True
            except (OSError, ValueError, TypeError, KeyError):
                reasons.append("candidate_build_not_ready_or_unattributed_failure")
        dev = record.get("dev") if isinstance(record.get("dev"), dict) else {}
        if set(dev) != set(self.public_case_ids):
            reasons.append("public_dev_inventory_incomplete")
        for case_id in self.public_case_ids:
            result = dev.get(case_id)
            if not isinstance(result, dict):
                reasons.append(f"{case_id}:missing_result")
                continue
            calls, failures, successful = _broker_counts(result)
            if self.dry_run:
                # The protocol self-test deliberately has no provider and no
                # lower-agent execution.  It may exercise submission,
                # feedback, digest fencing and freeze, but can never satisfy
                # the formal gate because ``run_kind`` is ``smoke``.
                if result.get("classification") != "smoke_only_no_model_call":
                    reasons.append(f"{case_id}:unexpected_smoke_classification")
                continue
            if self._axis(result) == "infrastructure":
                reasons.append(f"{case_id}:infrastructure_failure")
            from agentloop.build_evidence import candidate_zero_contract_valid
            causal_zero = build_zero and candidate_zero_contract_valid(result)
            if build_zero and not causal_zero:
                reasons.append(f'{case_id}:compilation_zero_contract_missing_or_changed')
            if not result.get("real_execution") and self.run_kind == "formal" and not causal_zero:
                reasons.append(f"{case_id}:not_real_execution")
            evaluation = result.get("result_evaluation", {})
            if evaluation.get("contract_valid") is not True or evaluation.get("round_consumed") is not True:
                reasons.append(f"{case_id}:no_valid_semantic_or_candidate_zero_contract")
        return not reasons, reasons

    def _build_failure_case(self, build, build_root, candidate, case_id, out):
        """A healthy, measured build failure needs no invented lower rollout."""
        from agentloop.build_evidence import build_failure_record
        started = now()
        try:
            expected = build_failure_record(build=build, build_dir=build_root, candidate=candidate,
                case_id=case_id, task=self.benchmark / 'agentloop/cases' / case_id / 'task.md')
            if (out / 'launcher_result.json').exists():
                result = read_checkpoint(out / 'launcher_result.json')
                if any(result.get(key) != value for key, value in expected.items()):
                    raise ValueError('Recorded compilation attribution changed')
            else:
                result = expected
                result.update(output_path=str(out), evidence_kind=self.run_kind,
                    controller_started_at=started, controller_finished_at=now())
                immutable_json(out / 'launcher_result.json', result)
            sys.path.insert(0, str(self.benchmark / 'evaluator'))
            from case_evidence import score_case
            result['result_evaluation'] = score_case(case_id=case_id,
                record_path=out / 'launcher_result.json', candidate=candidate,
                candidate_digest=tree_digest(candidate), output=out / 'semantic_result',
                broker_endpoint=self.result_judge_endpoint)
        except (OSError, ValueError, TypeError, KeyError) as exc:
            result = {'case_id': case_id, 'valid': False, 'real_execution': False,
                'classification': 'build_evidence_infrastructure_error',
                'classification_axis': 'infrastructure', 'error': str(exc),
                'result_evaluation': {'score': None, 'contract_valid': False, 'round_consumed': False}}
        immutable_json(out / 'controller_observation.json', result)
        return result

    def _write_feedback(self, record: dict[str, Any]) -> dict[str, Any]:
        from agentloop.public_feedback import (build_feedback, clean, harness_errors,
                                               score_caps)
        number = int(record.get("submission_number", len(self.records) + 1))
        feedback: dict[str, Any] = {
            "schema_version": "agentswe-ai-scientist-public-feedback-v1",
            "generated_at": now(),
            "source_submission": number,
            "candidate_digest": record.get("candidate_digest"),
            "patch_sha256": record.get("patch_sha256"),
            "build": build_feedback(record.get("build", {})),
            "public_cases": {},
        }
        for case_id in self.public_case_ids:
            result = (record.get("dev") or {}).get(case_id, {})
            if not isinstance(result, dict):
                result = {}
            semantic = result.get("result_evaluation", {}).get("feedback")
            feedback["public_cases"][case_id] = {
                "classification": result.get("classification"),
                "classification_axis": self._axis(result),
                "valid": result.get("valid"),
                "decision": result.get("decision", (result.get("answer") or {}).get("decision") if isinstance(result.get("answer"), dict) else None),
                "error": clean(result.get("error")),
                "semantic_feedback": clean(semantic),
                # 2026-09-21: the same evidence-bound ceiling contract the hidden
                # judge is handed, plus the evaluator's own harness/validator
                # findings, so the Builder can see WHICH published requirement
                # bound this case's score and why.
                "score_caps": score_caps(semantic),
                "harness_errors": harness_errors(result, semantic),
            }
        eligible, reasons = self._dev_gate(record)
        feedback["public_round_evaluable"] = eligible
        feedback["gate_reasons"] = reasons
        scores = {
            case_id: (result.get("result_evaluation", {}).get("score") if not self.dry_run else 0)
            for case_id, result in (record.get("dev") or {}).items()
        }
        feedback["dev_scores"] = scores
        feedback["dev_mean"] = sum(scores.values()) / len(scores) if scores and all(type(value) is int for value in scores.values()) else None
        feedback["dev_passed"] = bool(feedback["dev_mean"] is not None and feedback["dev_mean"] > 60)
        record["dev_scores"] = scores
        record["dev_mean"] = feedback["dev_mean"]
        record["dev_passed"] = feedback["dev_passed"]
        path = self.run_dir / "feedback" / f"candidate_{number:03d}.json"
        write_json(path, feedback)
        if self.readiness_profile:
            from .readiness import canonical_feedback
            feedback['feedback_digest']=canonical_feedback(feedback)
            path.write_text(json.dumps(feedback,sort_keys=True,separators=(',',':'),ensure_ascii=False)+'\n')
            self.feedback_digest=feedback['feedback_digest']
            record['feedback_digest']=self.feedback_digest
        self.feedback = feedback
        self.feedback_digest = sha256_file(path)
        record["feedback_path"] = str(path)
        record["feedback_digest"] = self.feedback_digest
        return feedback

    def _write_lifecycle(self) -> None:
        write_json(
            self.run_dir / "dev_lifecycle.json",
            {
                "schema_version": "agentswe-ai-scientist-dev-lifecycle-v1",
                "evidence_kind": self.run_kind,
                "smoke_excluded_from_formal": self.run_kind == "smoke",
                "pilot_excluded_from_formal": self.run_kind == "pilot",
                "broker_endpoint": self.broker_endpoint,
                "max_dev_rounds": self.max_dev_rounds,
                "n_concurrent": self.n_concurrent,
                "dev_passed_is_automatic_freeze": False,
                "records": self.records,
                "infrastructure_attempts": self.infrastructure_attempts,
                "terminal_infrastructure_latch": self.terminal_infrastructure_latch,
                "feedback_digest": self.feedback_digest,
                "revision_contract": {
                    "requires_feedback_bound_later_distinct_candidate": len(self.records) > 1,
                    "empty_feedback_attestation_is_false": True,
                },
                "frozen": self.frozen,
            },
        )

    def _freeze_latest(self, record: dict[str, Any], *, reason: str) -> dict[str, Any]:
        if self.readiness_profile:
            from .readiness import verify_rounds
            if reason!='builder_exit': raise RuntimeError('readiness freezes only after Builder exit')
            verify_rounds(self.records,self.records[0].get('builder_session_id') if self.records else None,self.current_binding,self.builder_exit_evidence)
        if reason not in {"max_dev_rounds", "builder_exit"}:
            raise RuntimeError("invalid freeze reason")
        if not self.records or record != self.records[-1]:
            raise RuntimeError("only the latest accepted Candidate may freeze")
        if reason == "max_dev_rounds" and len(self.records) != self.max_dev_rounds:
            raise RuntimeError("max_dev_rounds freeze requires the accepted submission limit")
        number = int(record["submission_number"])
        paths = record.get("attempt_paths")
        if paths is None:
            # Keep read compatibility with already accepted legacy ledgers.
            materialized = self.run_dir / "build" / f"candidate_{number:03d}" / "repository"
        else:
            if not isinstance(paths, dict):
                raise RuntimeError("latest accepted Candidate attempt paths are invalid")
            attempt_root = Path(str(paths.get("root", "")))
            if (attempt_root.is_symlink() or attempt_root.parent != self.run_dir / "submission_attempts"
                    or Path(str(paths.get("repository", ""))) != attempt_root / "build" / "repository"):
                raise RuntimeError("latest accepted Candidate attempt paths are invalid")
            materialized = attempt_root / "build" / "repository"
        if not materialized.is_dir():
            raise RuntimeError("latest accepted Candidate materialized repository is missing")
        source_digest = tree_digest(materialized)
        if source_digest != record.get("build", {}).get("candidate_repo_digest"):
            raise RuntimeError("latest accepted Candidate materialized repository changed")
        frozen = self.run_dir / "frozen_candidate"
        if frozen.exists():
            raise RuntimeError("frozen Candidate path already exists")
        shutil.copytree(materialized, frozen, symlinks=True)
        if tree_digest(frozen) != source_digest:
            raise RuntimeError("frozen latest Candidate copy digest mismatch")
        _make_tree_read_only(frozen)
        digests = [str(item.get("candidate_digest")) for item in self.records]
        # A maximum is not a minimum: the Builder may exit after its first
        # accepted submission. Bind feedback for revisions that actually exist.
        requires_revision = len(self.records) > 1
        feedback_chain_complete = bool(self.records) and all(
            isinstance(item.get("feedback_digest"), str)
            and (index == 0 or item.get("feedback_digest_ack") == self.records[index - 1].get("feedback_digest"))
            for index, item in enumerate(self.records)
        )
        distinct_later_revision = (
            len(self.records) >= 2
            and self.records[-1].get("role") == "feedback_revision"
            and self.records[-1].get("candidate_digest") != self.records[-2].get("candidate_digest")
            and isinstance(self.records[-1].get("build", {}).get("product_source_digest"), str)
            and self.records[-1].get("build", {}).get("product_source_digest") != self.records[-2].get("build", {}).get("product_source_digest")
            and self.records[-1].get("feedback_digest_ack") == self.records[-2].get("feedback_digest")
        )
        manifest = {
            "schema_version": "agentswe-ai-scientist-freeze-manifest-v2",
            "evidence_kind": self.run_kind,
            "public_case_inventory": list(self.public_case_ids),
            "hidden_case_inventory": list(self.hidden_case_ids),
            "source_submission": number,
            "source_submission_id": f"candidate-{number:03d}",
            "accepted_submission_count": len(self.records),
            "accepted_candidate_digests": digests,
            "freeze_reason": reason,
            "max_dev_rounds": self.max_dev_rounds,
            "n_concurrent": self.n_concurrent,
            "dev_passed_is_automatic_freeze": False,
            "candidate_delivery_digest": record["candidate_digest"],
            "candidate_materialized_digest": source_digest,
            "product_source_digest": record.get("build", {}).get("product_source_digest"),
            "candidate_digest": source_digest,
            "patch_sha256": record["patch_sha256"],
            "frozen_candidate_path": str(frozen),
            # The Code judge derives its evidence focus from the delivery beside
            # the accepted Candidate; without this key it packs the whole
            # repository and the judge exhausts its budget before answering.
            "accepted_candidate_path": (str(paths["candidate"])
                                       if isinstance(paths, dict) and paths.get("candidate")
                                       else None),
            "frozen_at": now(),
            "frozen_tree_read_only": True,
            "hidden_allowed": True,
            "dev_gate": {"latest_dev_passed": record.get("dev_passed"), "record_only": True},
            "feedback_digest": self.feedback_digest,
            "feedback_digest_ack": record.get("feedback_digest_ack"),
            "feedback_consumed": bool(feedback_chain_complete and (not requires_revision or distinct_later_revision)),
            "feedback_chain_complete": bool(feedback_chain_complete and (not requires_revision or distinct_later_revision)),
            "revision_contract": {
                "requires_feedback_bound_later_distinct_candidate": requires_revision,
                "later_distinct_candidate_present": distinct_later_revision,
                "feedback_consumed": bool(feedback_chain_complete and (not requires_revision or distinct_later_revision)),
            },
            "dev_evaluated": True,
            "credential_mounted_to_candidate": False,
            "frozen_tree_regular": True,
        }
        freeze_path = self.run_dir / "freeze_manifest.json"
        if self.readiness_profile:
            from .readiness import FILES
            manifest.update(readiness_profile=self.readiness_profile,current_binding=self.current_binding,
                readiness_only=True,score_threshold=None,builder_exit_evidence=self.builder_exit_evidence,
                submission_sha256={name:sha256_file(Path(record['attempt_paths']['candidate'])/name) for name in FILES})
        write_json(freeze_path, manifest)
        freeze_path.chmod(stat.S_IMODE(freeze_path.stat().st_mode) & ~0o222)
        self.frozen = manifest
        return manifest

    def freeze_latest(self, reason: str = "builder_exit", *, builder_exit_evidence=None) -> dict[str, Any]:
        if builder_exit_evidence is not None: self.builder_exit_evidence=copy.deepcopy(builder_exit_evidence)
        if self.frozen is not None:
            return self.frozen
        if not self.records:
            raise RuntimeError("no structurally valid accepted Candidate to freeze")
        manifest = self._freeze_latest(self.records[-1], reason=reason)
        self._write_lifecycle()
        return manifest

    def submit(
        self,
        candidate: Path,
        *,
        builder_session_id: str | None = None,
        feedback_digest_ack: str | None = None,
    ) -> dict[str, Any]:
        # The one-stop mutex covers one object. flock also covers another
        # process/controller trying to operate on this same persistent run.
        with (self.run_dir / ".submission.lock").open("a+") as handle:
            try:
                fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                return {"accepted": False, "error": "submission_already_active",
                        "classification_axis": "infrastructure", "round_consumed": False}
            try:
                self._load_state()
                return self._submit_locked(candidate, builder_session_id=builder_session_id,
                                           feedback_digest_ack=feedback_digest_ack)
            finally:
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)

    def _product_guard(self, build, paths, delivery_digest, *, pending):
        """Reserve materialized source before any dev action; reports are not a product."""
        digest = build.get('product_source_digest')
        if not isinstance(digest, str) or len(digest) != 64:
            # An unapplied patch has no resulting product; it cannot launch lower.
            if build.get('valid') or build.get('patch_apply', {}).get('exit_code') == 0:
                raise RecoveryError('stable pre-compile product identity missing')
            return None
        if product_source_digest(Path(paths['repository'])) != digest:
            raise RecoveryError('materialized product source changed after build')
        owner = {'product_source_digest': digest, 'attempt_root': paths['root'],
                 'candidate_digest': delivery_digest}
        saved = self.product_registry.lookup(digest)
        if saved is None:
            # Read-only old receipts can block replay; never invent a new score
            # or rewrite historical evidence to add this field retroactively.
            for prior in self.records + self.infrastructure_attempts:
                prior_paths = prior.get('attempt_paths', {})
                if prior_paths.get('root') == paths['root']:
                    continue
                prior_build = prior.get('build', {})
                previous = prior_build.get('product_source_digest')
                repository = Path(prior_paths.get('repository', '/nonexistent-evaluator-product'))
                if previous is None and repository.is_dir() and tree_digest(repository) == prior_build.get('candidate_repo_digest'):
                    previous = product_source_digest(repository)
                if previous == digest:
                    saved = {'product_source_digest': digest, 'attempt_root': prior_paths.get('root'),
                             'candidate_digest': prior.get('candidate_digest')}
                    break
        if saved is None:
            self.product_registry.claim(digest, owner)
            return None
        if saved == owner and pending is not None:
            return None  # Existing CaseStages owns all completed/unknown calls.
        prior = next((item for item in reversed(self.records + self.infrastructure_attempts)
                      if item.get('attempt_paths', {}).get('root') == saved.get('attempt_root')), None)
        return {'owner': saved, 'record': copy.deepcopy(prior) if prior else None}

    def _submit_locked(self, candidate: Path, *, builder_session_id: str | None,
                       feedback_digest_ack: str | None) -> dict[str, Any]:
        terminal_rejection = self.distinct_terminal_rejection(candidate)
        if terminal_rejection is not None:
            return terminal_rejection
        errors = validate_delivery(candidate)
        if errors:
            return {"accepted": False, "error": "invalid_submission", "details": errors}
        number = len(self.records) + 1
        if self.readiness_profile:
            from .readiness import validate_metadata
            try:
                if not builder_session_id: raise ValueError('native Builder session required')
                validate_metadata(candidate,{'builder_session_id':builder_session_id,'submission_number':number,
                    'revision_of_candidate_digest':self.records[-1]['candidate_digest'] if self.records else None,
                    'feedback_digest':self.feedback_digest if self.records else None})
            except (ValueError,OSError,KeyError,TypeError) as exc:
                return {'accepted':False,'round_consumed':False,'error':'readiness_metadata_invalid','details':str(exc)}
        from .readiness import delivery_digest
        digest = delivery_digest(candidate) if self.readiness_profile else tree_digest(candidate)
        patch_sha = sha256_file(candidate / "solution.patch")
        duplicate = next((record for record in self.records if digest == record.get("candidate_digest")), None)
        if duplicate is not None:
            if self.readiness_profile: return {'accepted':False,'round_consumed':False,'error':'readiness_candidate_replay_forbidden'}
            feedback_path = Path(str(duplicate.get("feedback_path", "")))
            if not feedback_path.is_file() or sha256_file(feedback_path) != duplicate.get("feedback_digest"):
                return {"accepted": False, "error": "cached_feedback_missing_or_changed",
                        "classification_axis": "infrastructure", "round_consumed": False}
            feedback = json.loads(feedback_path.read_text(encoding="utf-8"))
            return {"accepted": True, **duplicate, "duplicate_digest": True, "round_consumed": False,
                    "feedback": feedback, "feedback_digest": duplicate["feedback_digest"],
                    "frozen": self.frozen is not None, "freeze": self.frozen}
        if self.frozen is not None:
            return {"accepted": False, "error": "frozen"}
        if len(self.records) >= self.max_dev_rounds:
            return {"accepted": False, "error": "submission_limit_reached"}
        if number > 1:
            if not self.feedback_digest:
                return {"accepted": False, "error": "latest_feedback_missing"}
            if feedback_digest_ack != self.feedback_digest:
                return {
                    "accepted": False,
                    "error": "latest_feedback_digest_ack_mismatch",
                    "expected_feedback_digest": self.feedback_digest,
                }
            first_session = self.records[0].get("builder_session_id")
            if not builder_session_id or builder_session_id != first_session:
                return {"accepted": False, "error": "builder_session_changed_between_submissions"}
            if feedback_digest_ack is None or not isinstance(self.feedback, dict) or not self.feedback:
                return {"accepted": False, "error": "empty_feedback_cannot_bind_revision"}

        # Retry the same delivery/session in the SAME stage directories. The
        # case checkpoint and shared scoring intent own already executed work.
        pending = next((item for item in reversed(self.infrastructure_attempts)
                        if item.get("candidate_digest") == digest), None)
        if self.readiness_profile and pending is not None:
            return {'accepted':False,'round_consumed':False,'error':'readiness_attempt_replay_forbidden'}
        identity = execution_identity(self, digest)
        attempts_root = self.run_dir / "submission_attempts"
        attempts_root.mkdir(parents=True, exist_ok=True)
        if pending is not None:
            try:
                if pending.get("builder_session_id") != builder_session_id:
                    raise RecoveryError("Builder session changed during infrastructure recovery")
                if pending.get("submission_number") != number:
                    raise RecoveryError("infrastructure attempt is not the current accepted ordinal")
                if pending.get("stage_recovery_version") != 1:
                    raise RecoveryError("legacy attempt requires independent recovery audit; no resampling")
                paths = copy.deepcopy(pending["attempt_paths"])
                attempt_root = Path(paths["root"])
                if (attempt_root.is_symlink() or attempt_root.parent != attempts_root
                        or Path(paths["candidate"]) != attempt_root / "candidate"
                        or Path(paths["build"]) != attempt_root / "build"
                        or Path(paths["repository"]) != attempt_root / "build" / "repository"):
                    raise RecoveryError("infrastructure recovery paths are invalid")
                if read_checkpoint(Path(paths["record"])) != pending:
                    raise RecoveryError("infrastructure attempt record changed")
                if read_checkpoint(attempt_root / "execution_identity.json") != identity:
                    raise RecoveryError("execution identity changed during infrastructure recovery")
                snapshot, build_root = Path(paths["candidate"]), Path(paths["build"])
                if tree_digest(snapshot) != digest:
                    raise RecoveryError("pending Candidate snapshot changed")
                build = copy.deepcopy(pending["build"])
                if not build.get("valid"):
                    from agentloop.build_evidence import verified_build_failure
                    verified_build_failure(build, build_root, build_root / 'repository')
                if tree_digest(build_root / "repository") != build.get("candidate_repo_digest"):
                    raise RecoveryError("pending materialized Candidate is unavailable or changed")
                previous_record = {"path": paths["record"], "sha256": sha256_file(Path(paths["record"]))}
                recovery = Path(tempfile.mkdtemp(prefix="recovery_", dir=attempt_root))
                paths["record"] = str(recovery / "attempt_record.json")
                started_path = recovery / "attempt_started.json"
            except (RecoveryError, OSError, KeyError, TypeError, ValueError) as exc:
                return {"accepted": False, "error": "infrastructure_recovery_requires_attention",
                        "details": str(exc), "classification_axis": "infrastructure", "round_consumed": False}
        else:
            # An interrupted process may have reserved a stage without writing
            # the lifecycle ledger. Do not start an untracked second rollout.
            known_roots = {str(item.get("attempt_paths", {}).get("root"))
                           for item in self.records + self.infrastructure_attempts}
            for started in attempts_root.glob("*/attempt_started.json"):
                if str(started.parent) not in known_roots:
                    return {"accepted": False, "error": "uncommitted_attempt_requires_recovery",
                            "classification_axis": "infrastructure", "round_consumed": False,
                            "attempt_started_path": str(started)}
            attempt_root = Path(tempfile.mkdtemp(prefix=f"candidate_{number:03d}_", dir=attempts_root))
            snapshot, build_root = attempt_root / "candidate", attempt_root / "build"
            paths = {"root": str(attempt_root), "candidate": str(snapshot), "build": str(build_root),
                     "repository": str(build_root / "repository"), "dev": str(attempt_root / "dev"),
                     "record": str(attempt_root / "attempt_record.json")}
            started_path = attempt_root / "attempt_started.json"
            previous_record = None
            immutable_json(attempt_root / "execution_identity.json", identity)
        immutable_json(started_path, {
            "schema_version": "agentswe-ai-scientist-submission-attempt-v1",
            "submitted_at": now(), "candidate_digest": digest, "submission_number": number,
            "builder_session_id": builder_session_id, "attempt_paths": paths,
            "accepted": False, "round_consumed": False, "stage_recovery_version": 1,
            "previous_record": previous_record,
        })
        if pending is None:
            shutil.copytree(candidate, snapshot, symlinks=True)
            build = build_candidate(self.benchmark / "input" / "repository", snapshot, build_root)
        record: dict[str, Any] = {
            "submission_number": number,
            "candidate_digest": digest,
            "patch_sha256": patch_sha,
            "role": "initial" if number == 1 else "feedback_revision",
            "builder_session_id": builder_session_id,
            "feedback_digest_ack": feedback_digest_ack,
            "submitted_at": now(),
            "evidence_kind": self.run_kind,
            "build": build,
            "attempt_paths": paths,
            "stage_recovery_version": 1,
            "previous_record": previous_record,
            "dev": {},
        }
        if self.readiness_profile:
            record.update(readiness_profile=self.readiness_profile,
                revision_of_candidate_digest=self.records[-1]['candidate_digest'] if self.records else None)
        try:
            duplicate_product = self._product_guard(build, paths, digest, pending=pending)
        except (RecoveryError, OSError, ValueError, TypeError) as exc:
            duplicate_product = {'owner': {}, 'record': None, 'error': str(exc)}
        if duplicate_product is not None:
            previous = duplicate_product.get('record') or {}
            record.update(accepted=False, round_consumed=False,
                state='product_execution_already_attempted', error='product_replay_forbidden',
                classification_axis='infrastructure', retry_allowed=False,
                product_source_digest=build.get('product_source_digest'),
                retained_product_attempt=duplicate_product['owner'],
                dev=copy.deepcopy(previous.get('dev', {})),
                details=duplicate_product.get('error', 'Completed and unknown calls are retained; changing reports cannot start another product execution.'))
            for field in ('dev_scores','dev_mean','dev_passed','feedback_digest'):
                if field in previous: record[field] = previous[field]
            path = Path(str(previous.get('feedback_path', '')))
            if path.is_file() and sha256_file(path) == previous.get('feedback_digest'):
                record['feedback'] = json.loads(path.read_text())
            immutable_json(Path(paths['record']), record)
            self.infrastructure_attempts.append(record)
            self._write_lifecycle()
            return record
        if build.get("valid"):
            candidate_repo = build_root / "repository"
            for case_id in self.public_case_ids:
                out = attempt_root / "dev" / case_id
                record["dev"][case_id] = self._run_case(candidate_repo, case_id, out)
        else:
            for case_id in self.public_case_ids:
                record['dev'][case_id] = ({'classification_axis':'candidate','classification':'candidate_build_failure','real_execution':False} if self.readiness_profile else self._build_failure_case(build, build_root,
                    build_root / 'repository', case_id, attempt_root / 'dev' / case_id))
        record["completed_at"] = now()
        eligible, reasons = self._dev_gate(record)
        record["public_round_evaluable"] = eligible
        record["public_gate_reasons"] = reasons
        record["state"] = "public_complete" if eligible else ("candidate_build_failure" if not build.get("valid") else "public_complete_not_evaluable")
        infra = any(self._axis(value) == "infrastructure" for value in record["dev"].values())
        if infra or not eligible:
            record.update({"accepted": False, "round_consumed": False,
                           "state": "public_infrastructure_invalid" if infra else "public_evidence_unresolved"})
            self.infrastructure_attempts.append(record)
            self._latch_terminal_infrastructure(record)
            immutable_json(Path(paths["record"]), record)
            self._write_lifecycle()
            return record
        record.update({"accepted": True, "round_consumed": True})
        if not post_freeze_superseded(self, record):
            self.records.append(record)
        self._write_feedback(record)
        if len(self.records) == self.max_dev_rounds and not self.defer_max_rounds_freeze:
            self._freeze_latest(record, reason="max_dev_rounds")
            record["state"] = "frozen"
        immutable_json(Path(paths["record"]), record)
        self._write_lifecycle()
        return {"accepted": True, **record, "feedback": self.feedback, "feedback_digest": self.feedback_digest, "frozen": self.frozen is not None, "freeze": self.frozen}

    def run_hidden(self, case_ids: Iterable[str] | None = None) -> dict[str, Any]:
        requested = tuple(case_ids) if case_ids is not None else self.hidden_case_ids
        if self.readiness_profile and requested!=('test_001',):
            return {'valid':False,'classification':'protocol_error','error':'readiness_test_001_only'}
        if self.readiness_profile and (self.run_dir/'hidden-after-freeze-attestation.json').exists():
            return {'valid':False,'classification':'protocol_error','error':'readiness_hidden_replay_forbidden'}
        if self.frozen is None:
            return {"valid": False, "classification": "protocol_error", "error": "hidden_before_freeze"}
        # The real one-stop calls this method, not run_hidden.py's standalone
        # CLI. Use the same ledger/freeze gate before dispatching any case.
        try:
            from .run_hidden import _validate_freeze
        except ImportError:
            from run_hidden import _validate_freeze
        _, freeze_errors = _validate_freeze(self.run_dir, self.frozen, public_case_ids=self.public_case_ids)
        if freeze_errors:
            return {"valid": False, "classification": "protocol_error",
                    "error": "freeze_manifest_not_latest_accepted_candidate", "details": freeze_errors}
        if self.frozen.get("source_submission") != len(self.records) or self.frozen.get("hidden_allowed") is not True:
            return {"valid": False, "classification": "protocol_error", "error": "freeze_manifest_not_latest_accepted_candidate"}
        frozen_repo = Path(str(self.frozen["frozen_candidate_path"]))
        if not frozen_repo.is_dir():
            return {"valid": False, "classification": "evaluator_infrastructure_error", "error": "frozen_candidate_repository_missing"}
        expected_digest = str(self.frozen["candidate_materialized_digest"])
        freeze_path = self.run_dir / "freeze_manifest.json"
        freeze_sha = sha256_file(freeze_path)
        freeze_bytes = freeze_path.read_bytes()
        if tree_digest(frozen_repo) != expected_digest:
            return {"valid": False, "classification": "protocol_error", "error": "frozen_digest_changed_before_hidden"}

        hidden_started_at = now()
        cases: dict[str, Any] = {}
        attestations: list[dict[str, Any]] = []
        for case_id in requested:
            if case_id not in self.hidden_case_ids:
                return {"valid": False, "classification": "protocol_error", "error": f"unknown_hidden_case:{case_id}"}
            before = tree_digest(frozen_repo)
            started_at = now()
            out = self.run_dir / "hidden" / case_id
            latest = self.records[-1]
            if latest.get('build', {}).get('valid') is False:
                result = self._build_failure_case(latest['build'], Path(latest['attempt_paths']['build']),
                    frozen_repo, case_id, out)
            else:
                result = self._run_case(frozen_repo, case_id, out, hidden=True)
            finished_at = now()
            after = tree_digest(frozen_repo)
            result_path = out / "launcher_result.json"
            cases[case_id] = result
            from agentloop.build_evidence import (candidate_zero_attribution_verified,
                                                  candidate_zero_contract_valid)
            # Hidden cases are scored by the formal finalizer, so a hidden record
            # carries no result_evaluation yet.  A causal Candidate zero that the
            # shared contract already recognises must still count as a measurement
            # here, otherwise one Candidate-caused case suppresses Result judging
            # for every other case in the same formal cell.
            causal_zero = (candidate_zero_contract_valid(result)
                           or candidate_zero_attribution_verified(result))
            attestations.append(
                {
                    "case_id": case_id,
                    "started_at": started_at,
                    "finished_at": finished_at,
                    "result_path": str(result_path),
                    "result_sha256": sha256_file(result_path) if result_path.is_file() else None,
                    "classification": result.get("classification"),
                    "classification_axis": self._axis(result),
                    "broker": result.get("broker", {}),
                    "real_execution": result.get("real_execution"),
                    "causal_candidate_zero": causal_zero,
                    "frozen_digest_before": before,
                    "frozen_digest_after": after,
                    "frozen_digest_stable": before == expected_digest == after,
                }
            )
        final_digest = tree_digest(frozen_repo)
        if freeze_path.read_bytes() != freeze_bytes:
            raise RuntimeError("immutable freeze manifest changed during hidden execution")
        executed = [entry["case_id"] for entry in attestations]
        all_materialized = all(Path(entry["result_path"]).is_file() for entry in attestations)
        all_real = all(bool(entry.get("real_execution")) for entry in attestations)
        all_measured = all(entry.get('real_execution') or entry.get('causal_candidate_zero') for entry in attestations)
        all_evaluable = True
        for entry in attestations:
            calls, failures, successful = _broker_counts({"broker": entry.get("broker", {})})
            # Historical transient failures may have recovered. Only terminal
            # attribution and observed successful execution matter here; the
            # independent scoring contract performs the final semantic gate.
            if entry.get("classification_axis") == "infrastructure" or (
                    not entry.get('causal_candidate_zero') and (calls <= 0 or successful <= 0)):
                all_evaluable = False
        complete_inventory = executed == list(self.hidden_case_ids)
        if self.readiness_profile:
            from .readiness import execution_valid
            all_evaluable=all(execution_valid(row) for row in cases.values())
        all_cases_started_after_freeze = _all_started_after_freeze(
            self.frozen.get("frozen_at"), hidden_started_at, attestations
        )
        attestation = {
            "schema_version": "agentswe-ai-scientist-hidden-after-freeze-attestation-v1",
            "evidence_kind": self.run_kind,
            "freeze_manifest": str(freeze_path),
            "freeze_manifest_sha256": freeze_sha,
            "frozen_candidate_path": str(frozen_repo),
            "frozen_candidate_digest": expected_digest,
            "hidden_started_at": hidden_started_at,
            "freeze_created_at": self.frozen.get("frozen_at"),
            "all_cases_started_after_freeze": all_cases_started_after_freeze,
            "expected_cases": list(self.hidden_case_ids),
            "executed_cases": executed,
            "complete_inventory": complete_inventory,
            "all_cases_materialized": all_materialized,
            "all_cases_real": all_real,
            "all_cases_measured_or_causal_zero": all_measured,
            "all_cases_evaluable": all_evaluable,
            "frozen_digest_stable": final_digest == expected_digest and all(entry["frozen_digest_stable"] for entry in attestations),
            "cases": attestations,
            "formal_complete": bool(
                self.run_kind == "formal"
                and self.hidden_case_ids == HIDDEN_CASES
                and complete_inventory
                and all_materialized
                and all_measured
                and all_evaluable
                and final_digest == expected_digest
            ),
            "pilot_complete": bool(
                self.run_kind == "pilot"
                and self.public_case_ids == ("dev_001",)
                and self.hidden_case_ids == ("test_001",)
                and complete_inventory
                and all_materialized
                and all_measured
                and all_evaluable
                and final_digest == expected_digest
            ),
        }
        write_json(self.run_dir / "hidden-after-freeze-attestation.json", attestation)
        value = {
            "valid": True,
            "classification": "hidden_after_freeze",
            "evidence_kind": self.run_kind,
            "frozen_digest": expected_digest,
            "case_ids": executed,
            "cases": cases,
            "attestation_path": str(self.run_dir / "hidden-after-freeze-attestation.json"),
            "formal_complete": attestation["formal_complete"],
            "pilot_complete": attestation["pilot_complete"],
        }
        write_json(self.run_dir / "hidden_lifecycle.json", value)
        return value


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--benchmark", type=Path, default=Path(__file__).resolve().parents[1])
    parser.add_argument("--run-dir", type=Path, required=True)
    parser.add_argument("--candidate", type=Path)
    parser.add_argument("--broker-endpoint", default="http://127.0.0.1:18080/v1/responses")
    parser.add_argument("--builder-session-id")
    parser.add_argument("--feedback-digest-ack")
    parser.add_argument("--image")
    parser.add_argument("--dependency-overlay", type=Path)
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--action", choices=("submit", "hidden"), default="submit")
    args = parser.parse_args()
    controller = Controller(
        args.benchmark,
        args.run_dir,
        args.broker_endpoint,
        args.dry_run,
        image=args.image,
        dependency_overlay=args.dependency_overlay,
    )
    if args.action == "submit":
        if args.candidate is None:
            parser.error("--candidate is required for submit")
        result = controller.submit(
            args.candidate.resolve(),
            builder_session_id=args.builder_session_id,
            feedback_digest_ack=args.feedback_digest_ack,
        )
    else:
        result = controller.run_hidden()
    print(json.dumps(result, indent=2, sort_keys=True))
    return 0 if result.get("accepted", result.get("valid", False)) else 1


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


_fr_wrap_submit(Controller)
_FR_WRAPPED_FREEZE = [name for name in _FR_FREEZE_METHODS if _fr_wrap_freeze(Controller, name)]
assert _FR_WRAPPED_FREEZE, 'freeze/submit race guard found no freeze entry point'
# --- end 0921b freeze/submit race guard ------------------------------------


if __name__ == "__main__":
    raise SystemExit(main())
