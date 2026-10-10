#!/usr/bin/env python3
"""Accepted-submission ledger controller for Aider Agent-loop evaluation.

The controller is intentionally independent of Harbor's formal runner. It is a
small auditable adapter: every accepted Candidate runs both public dev cases,
fresh feedback is persisted after each acceptance, and the latest accepted
Candidate is frozen when the Builder exits or the accepted-round limit is
reached. The controller permits one through ten distinct accepted Candidates;
passing development cases records feedback status but does not automatically
freeze the session. Only a verified freeze may unlock hidden cases. Real
execution is opt-in so static stage-A checks cannot incur API or Docker cost.
"""
from __future__ import annotations

import os

import argparse
import hashlib
import importlib.util
import json
import shutil
import subprocess
import stat
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
from harbor.public_feedback import (public_payload, semantic_feedback,
                                    score_cap_feedback, harness_error_lines)
from harbor.product_attempts import ProductAttempts, create, checkpoint, read as read_attempt
CASES = json.loads((ROOT / "evaluator/agentloop_cases.json").read_text(encoding="utf-8"))


class PublicScoringBoundaryError(RuntimeError):
    """Typed public-scoring failure that must not be guessed from its text."""

    def __init__(self, classification: str, message: str):
        super().__init__(message)
        self.classification = classification


def now() -> str:
    return datetime.now(timezone.utc).isoformat()


def digest(root: Path) -> str:
    h=hashlib.sha256()
    for p in sorted(root.rglob("*"),key=lambda x:x.relative_to(root).as_posix()):
        if ".git" in p.relative_to(root).parts or "__pycache__" in p.parts or p.suffix==".pyc": continue
        rel=p.relative_to(root).as_posix().encode(); h.update(len(rel).to_bytes(8,"big")); h.update(rel)
        kind,payload=(b"L",p.readlink().as_posix().encode()) if p.is_symlink() else ((b"F",p.read_bytes()) if p.is_file() else (b"D",b""))
        h.update(kind); h.update(len(payload).to_bytes(8,"big")); h.update(payload)
    return h.hexdigest()


def file_digest(path: Path) -> str | None:
    if not path.is_file():
        return None
    return hashlib.sha256(path.read_bytes()).hexdigest()


def case_spec(case_id: str, base: dict[str, Any]) -> dict[str, Any]:
    """Build the evaluator-side case request with the natural task attached.

    Hidden task text and scenario assets are read by the evaluator and passed
    only as the lower task projection; they are never copied into the Builder
    public package or the Candidate source mount.
    """
    directory = ROOT / ("test_cases" if case_id.startswith("test_") else "cases") / case_id
    task_path = directory / "input.md" if case_id.startswith("test_") else directory.with_suffix(".md")
    task = task_path.read_text(encoding="utf-8") if task_path.is_file() else str(base.get("scenario", ""))
    scenario_path = directory / "assets" / "scenario.json"
    scenario: dict[str, Any] = {}
    if scenario_path.is_file():
        value = json.loads(scenario_path.read_text(encoding="utf-8"))
        if isinstance(value, dict):
            scenario = value
    return {
        "case_id": case_id,
        **base,
        "task_input": task,
        "task_input_sha256": hashlib.sha256(task.encode("utf-8")).hexdigest(),
        "scenario_asset": scenario,
        "scenario_asset_sha256": hashlib.sha256(json.dumps(scenario, sort_keys=True, separators=(",", ":")).encode("utf-8")).hexdigest(),
    }


def _make_tree_read_only(root: Path) -> None:
    """Fence the canonical frozen Candidate without following symlinks."""
    for path in sorted(root.rglob("*"), key=lambda item: len(item.parts), reverse=True):
        if path.is_symlink():
            continue
        path.chmod(stat.S_IMODE(path.stat().st_mode) & ~0o222)
    root.chmod(stat.S_IMODE(root.stat().st_mode) & ~0o222)


class Controller:
    def __init__(self, run_dir: Path, source: Path, broker: str | None, real: bool,
                 dependency_overlay: Path | None = None, image: str | None = None,
                 public_cases: tuple[str, ...] = ("dev_001", "dev_002"),
                 hidden_cases: tuple[str, ...] = tuple(f"test_{number:03d}" for number in range(1, 7)),
                 pilot_not_formal: bool = False, max_dev_rounds: int = 10, readiness_profile=None, current_binding=None) -> None:
        self.run_dir,self.source,self.broker,self.real=run_dir,source,broker,real
        self.dependency_overlay = dependency_overlay
        self.image = image
        self.judge_endpoint: str | None = None
        canonical_public = tuple(CASES["public_dev"])
        canonical_hidden = tuple(CASES["hidden"])
        if not public_cases or any(case_id not in canonical_public for case_id in public_cases):
            raise ValueError("public_cases must be a non-empty canonical subset")
        if not hidden_cases or any(case_id not in canonical_hidden for case_id in hidden_cases):
            raise ValueError("hidden_cases must be a non-empty canonical subset")
        if (tuple(public_cases) != canonical_public or tuple(hidden_cases) != canonical_hidden) and not pilot_not_formal:
            raise ValueError("reduced inventories require pilot_not_formal")
        self.public_cases = tuple(public_cases)
        self.hidden_cases = tuple(hidden_cases)
        self.pilot_not_formal = pilot_not_formal
        if not 1 <= max_dev_rounds <= 10:
            raise ValueError("max_dev_rounds must be between 1 and 10")
        self.max_dev_rounds = max_dev_rounds
        self.readiness_profile, self.current_binding = readiness_profile, current_binding
        if readiness_profile and (readiness_profile != 'single-dev-two-round-hidden-smoke-v1' or public_cases != ('dev_001',) or hidden_cases != ('test_001',) or max_dev_rounds != 2 or not current_binding or current_binding.get('task') != 'aider'):
            raise ValueError('invalid Aider readiness profile')
        self.records: list[dict[str,Any]]=[]; self.frozen: dict[str,Any]|None=None
        self.feedback: dict[str,Any]|None=None
        self.feedback_digest: str|None=None
        self.run_dir.mkdir(parents=True,exist_ok=True)
        product_context = {
            "schema_version": "agentswe-aider-product-attempts-v1",
            "source_digest": digest(self.source), "public_cases": list(self.public_cases),
            "public_case_contracts": {case: case_spec(case, CASES["public_dev"][case]) for case in self.public_cases},
            "runtime_image": self.image,
            "dependency_overlay": str(self.dependency_overlay) if self.dependency_overlay else None,
            "controller_sources": {name: file_digest(ROOT / name) for name in (
                "harbor/agentloop_controller.py", "harbor/product_attempts.py",
                "evaluator/materialize_candidate.py", "evaluator/harness/run_lower_agent_case.py")},
            "hidden_cases": list(self.hidden_cases), "real": self.real,
            "max_dev_rounds": self.max_dev_rounds,
        }
        guard_path = os.environ.get("AGENTSWE_AIDER_PRIOR_PRODUCT_GUARD")
        if guard_path:
            guard = Path(guard_path).resolve()
            product_context["cross_run_product_guard"] = {
                "path": str(guard), "sha256": file_digest(guard),
                "schema_version": "agentswe-aider-cross-run-product-guard/v1",
            }
        self.attempts = ProductAttempts(self.run_dir, product_context)
        self._restore()

    def _restore(self) -> None:
        path = self.run_dir / "dev_lifecycle.json"
        if not path.is_file():
            return
        state = read_attempt(path)
        self.records = state["records"]
        self.frozen = state.get("frozen")
        self.feedback_digest = state.get("feedback_digest")
        if self.records:
            feedback = Path(self.records[-1]["feedback_path"])
            if file_digest(feedback) != self.feedback_digest:
                raise RuntimeError("preserved authoritative feedback changed")
            self.feedback = read_attempt(feedback)

    def _case_result(self, path: str) -> dict[str, Any]:
        result_path = Path(path)
        try:
            value = json.loads(result_path.read_text(encoding="utf-8"))
            return value if isinstance(value, dict) else {}
        except (OSError, json.JSONDecodeError):
            return {}

    def _dev_is_freeze_eligible(self, record: dict[str, Any]) -> tuple[bool, list[str]]:
        failed, reasons = self._dev_has_infrastructure_failure(record)
        return not failed, reasons

    def _legacy_dev_is_freeze_eligible(self, record: dict[str, Any]) -> tuple[bool, list[str]]:
        reasons: list[str] = []
        if record.get("build_exit_code") != 0:
            reasons.append("candidate_build_failed")
        dev = record.get("dev")
        if not isinstance(dev, dict) or set(dev) != set(self.public_cases):
            reasons.append("dev_inventory_incomplete")
            return False, reasons
        for case_id in self.public_cases:
            entry = dev.get(case_id)
            if not isinstance(entry, dict):
                reasons.append(f"{case_id}:missing_record")
                continue
            result = self._case_result(str(entry.get("result", "")))
            classification = result.get("classification")
            broker = result.get("broker") or {}
            answer = result.get("answer")
            if entry.get("exit_code") not in (0, None):
                reasons.append(f"{case_id}:launcher_exit_{entry.get('exit_code')}")
            if classification == "infrastructure-invalid" or not classification:
                reasons.append(f"{case_id}:infrastructure_or_missing_result")
            if int(broker.get("calls_delta", 0) or 0) <= 0:
                reasons.append(f"{case_id}:no_broker_call")
            if int(broker.get("failures_delta", 0) or 0) != 0:
                reasons.append(f"{case_id}:broker_failure")
            # A real lower-agent behavior failure (for example, Aider made a
            # successful model call but did not produce agent_result.json) is
            # still an evaluable Candidate outcome and must not prevent the
            # feedback revision or hidden run.  Only infrastructure failures
            # are freeze-blocking.  A valid result is required for scoring,
            # not for reaching the scoring gate.
            if classification == "candidate_valid" and (not isinstance(answer, dict) or not answer):
                reasons.append(f"{case_id}:candidate_valid_but_missing_terminal_product_artifact")
        return not reasons, reasons

    def _dev_has_infrastructure_failure(self, record: dict[str, Any]) -> tuple[bool, list[str]]:
        """Return only failures that make a round non-evaluable.

        A Candidate build/product failure is valid feedback for the next
        Builder revision.  Broker, launcher, mount, or missing runtime
        failures must remain fail-closed and block the freeze.
        """
        if self.readiness_profile:
            reasons = []
            if record.get('build_exit_code') != 0:
                reasons.append('readiness requires a successful actual build')
            if set(record.get('dev', {})) != {'dev_001'}:
                reasons.append('readiness requires exactly dev_001')
            for entry in record.get('dev', {}).values():
                if entry.get('exit_code') != 0 or self._case_result(entry['result']).get('readiness_execution_valid') is not True:
                    reasons.append('invalid readiness execution')
            return bool(reasons), reasons
        reasons: list[str] = []
        build_failed = record.get("build_exit_code") not in (0, None)
        if build_failed:
            path = Path(str(record.get("build_result_path") or self.run_dir / "missing_build_result"))
            build = self._case_result(str(path))
            candidate_zero = (record.get("build_result_sha256") is not None
                and file_digest(path) == record.get("build_result_sha256")
                and build.get("classification") == "candidate_build_failure"
                and (build.get("environment_preflight") or {}).get("valid") is True)
            if not candidate_zero:
                reasons.append("candidate_build_or_build_infrastructure_failure")
        dev = record.get("dev")
        if not isinstance(dev, dict) or set(dev) != set(self.public_cases):
            reasons.append("dev_inventory_incomplete")
            return True, reasons
        for case_id in self.public_cases:
            entry = dev.get(case_id)
            if not isinstance(entry, dict):
                reasons.append(f"{case_id}:missing_record")
                continue
            result = self._case_result(str(entry.get("result", "")))
            classification = str(result.get("classification") or "")
            if "infrastructure" in classification or classification == "provider_failure" or not classification:
                reasons.append(f"{case_id}:{classification}")
            elif classification == "unresolved_evaluator_boundary":
                reasons.append(f"{case_id}:{classification}")
            broker = result.get("broker") or {}
            if int(broker.get("failures_delta", 0) or 0) != 0:
                reasons.append(f"{case_id}:broker_failure")
            if result.get("score") is None:
                reasons.append(f"{case_id}:missing_authoritative_score")
        return bool(reasons), reasons

    def _score_public(self, case_id: str, result_path: Path, candidate_digest: str) -> None:
        result = self._case_result(str(result_path))
        spec = importlib.util.spec_from_file_location("aider_task_finalizer", ROOT / "evaluator/formal_finalize.py")
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        if self.readiness_profile:
            from evaluator import formal_finalize as module
        verdict, zero = module.execution_verdict(result, case_id, str(result.get("candidate_digest", candidate_digest)))
        if self.readiness_profile:
            if verdict.get('classification') not in {'scoreable', 'candidate_zero'}:
                raise PublicScoringBoundaryError('evaluator_infrastructure_failure', 'invalid readiness execution: '+str(verdict))
            result.update(readiness_execution_valid=True, readiness_classification=verdict,
                native_diagnostic_score=result.get('score'), score=None, formal_result_publishable=False, code_score_publishable=False)
            result_path.write_text(json.dumps(result, indent=2)+'\n')
            return
        if zero is not None:
            contract, score = zero, 0
        elif verdict.get("classification") == "infrastructure_invalid":
            raise PublicScoringBoundaryError(
                "evaluator_infrastructure_failure",
                "dev infrastructure invalid: " + str(verdict.get("reason")),
            )
        elif verdict.get("classification") != "scoreable":
            raise PublicScoringBoundaryError(
                "unresolved_evaluator_boundary",
                "dev scoring attribution unresolved: " + str(verdict.get("reason")),
            )
        else:
            if not self.judge_endpoint:
                raise PublicScoringBoundaryError(
                    "evaluator_infrastructure_failure", "dev Result judge endpoint missing"
                )
            contract = module.run_result_judge(self.run_dir, case_id, {**result, "result": str(result_path)}, self.judge_endpoint)
            if not module.valid_result_contract(contract, case_id):
                raise PublicScoringBoundaryError(
                    "evaluator_infrastructure_failure", "dev Result judge contract invalid"
                )
            score = contract["result_score"]
        result.update({"score": score, "maximum": 100, "dev_score_kind": "candidate_zero" if zero else "independent_result_rubric",
                       "result_judge_contract": contract})
        result_path.write_text(json.dumps(result, indent=2, ensure_ascii=False) + "\n")

    def _write_feedback(self, record: dict[str, Any]) -> None:
        feedback = {
            "schema_version": "agentswe-aider-dev-feedback-v1",
            "generated_at": now(),
            "source_submission": record.get("submission_number"),
            "candidate_digest": record.get("candidate_digest"),
            "dev": {},
            "eligible_for_revision": False,
        }
        for case_id, entry in (record.get("dev") or {}).items():
            result = self._case_result(str(entry.get("result", "")))
            feedback["dev"][case_id] = {
                "classification": result.get("classification"),
                "decision": (result.get("answer") or {}).get("decision"),
                "score": result.get("score"),
                "score_kind": result.get("dev_score_kind"),
                # The ceilings this case was judged under -- the same contract
                # the hidden axis issues -- and the evaluator harness/validator
                # lines recorded against it.  Without these the Builder reads a
                # public number it cannot reproduce on the hidden axis.
                "score_caps": score_cap_feedback(result.get("result_judge_contract")),
                "harness_errors": harness_error_lines(result),
                "result_judge_feedback": semantic_feedback(result.get("result_judge_contract")),
            }
        eligible, reasons = self._dev_is_freeze_eligible(record)
        feedback["eligible_for_revision"] = eligible
        feedback["eligibility_reasons"] = reasons
        scores = [feedback["dev"].get(case, {}).get("score") for case in self.public_cases]
        mean = sum(scores) / len(scores) if all(isinstance(score, (int, float)) and not isinstance(score, bool) for score in scores) else None
        feedback["dev_score"] = mean
        feedback["dev_passed"] = bool(mean is not None and mean > 60)
        record["dev_score"] = mean
        record["dev_passed"] = feedback["dev_passed"]
        feedback = public_payload(feedback)
        self.feedback = feedback
        feedback_path = self.run_dir / f"feedback_candidate_{int(record.get('submission_number', 1)):03d}.json"
        checkpoint(feedback_path, feedback)
        self.feedback_digest = file_digest(feedback_path)
        record["feedback_path"] = str(feedback_path)
        record["feedback_digest"] = self.feedback_digest

    def submit(self, candidate: Path, feedback_digest_ack: str | None = None) -> dict[str,Any]:
        with self.attempts.locked():
            self._restore()
            return self._submit(candidate, feedback_digest_ack)

    def _submit(self, candidate: Path, feedback_digest_ack: str | None = None) -> dict[str,Any]:
        if not (candidate/"solution.patch").is_file(): return {"classification":"candidate_build_failure","error":"missing solution.patch"}
        if self.readiness_profile:
            from harbor.readiness_contract import validate_delivery
            validate_delivery(self, candidate, feedback_digest_ack)
        candidate_digest=digest(candidate)
        duplicate = next((record for record in self.records if record.get("candidate_digest") == candidate_digest), None)
        previous = self.attempts.previous(candidate_digest)
        if duplicate is not None:
            if previous is not None and previous.get("state") == "unresolved_previous_attempt":
                return previous
            return {**duplicate, "duplicate": True, "round_consumed": False}
        if previous is not None:
            return previous
        if self.attempts.legacy:
            return {"accepted": False, "round_consumed": False,
                    "classification": "evaluator_infrastructure_failure",
                    "error": "legacy run has no durable product intents; automatic replay blocked"}
        if self.frozen: raise RuntimeError("hidden/freeze already started")
        if len(self.records) >= self.max_dev_rounds:
            raise RuntimeError("max_dev_rounds reached; freeze the latest accepted snapshot")
        number=len(self.records)+1
        patch_sha = file_digest(candidate / "solution.patch")
        if number == 1 and feedback_digest_ack not in {None, ""}:
            return {"accepted": False, "error": "first accepted submission cannot acknowledge feedback"}
        if number > 1:
            if not self.feedback_digest:
                return {"accepted": False, "error": "latest evaluator feedback is missing"}
            if feedback_digest_ack != self.feedback_digest:
                return {"accepted": False, "error": "feedback digest acknowledgement mismatch",
                        "expected_feedback_digest": self.feedback_digest}
        attempt = self.attempts.reserve_delivery(candidate_digest, {
            "candidate_digest": candidate_digest, "submitted_at": now(),
            "submission_number": number, "feedback_digest_ack": feedback_digest_ack})
        out = attempt / "candidate"
        shutil.copytree(candidate, out, symlinks=True)
        materialized = attempt / "materialized"
        build=subprocess.run(["python3",str(ROOT/"evaluator/materialize_candidate.py"),"--source",str(self.source),"--candidate",str(out),"--output",str(materialized),"--patch",str(out/"solution.patch")],text=True,capture_output=True,check=False)
        record={"submission_number":number,"candidate_digest":candidate_digest,"patch_sha256":patch_sha,"submitted_at":now(),"role":"initial" if number==1 else "feedback_revision","feedback_digest_ack":feedback_digest_ack,"build_exit_code":build.returncode,"build_stdout_tail":build.stdout[-2000:],"state":"candidate_build_failure" if build.returncode else "running","dev":{},"round_consumed":True}
        record.update({"attempt_path": str(attempt), "candidate_path": str(out),
                       "materialized_path": str(materialized)})
        build_evidence = self._case_result(str(materialized / "build_result.json"))
        record.update({"build_result_path": str(materialized / "build_result.json"),
                       "build_result_sha256": file_digest(materialized / "build_result.json")})
        if (materialized / "aider").is_dir():
            stable = digest(materialized / "aider")
            record["stable_product_digest"] = stable
            previous = self.attempts.reserve_product(stable, candidate_digest)
            if previous is not None:
                return previous
        if build.returncode and build_evidence.get("classification") == "candidate_build_failure" and (build_evidence.get("environment_preflight") or {}).get("valid"):
            for case_id in self.public_cases:
                case_out = attempt / "evaluations" / case_id
                case_out.mkdir(parents=True, exist_ok=False)
                result = {"case_id": case_id, "candidate_digest": digest(materialized), "score": 0, "maximum": 100,
                          "classification": "candidate_build_failure", "execution_attempted": True, "infrastructure_invalid": False,
                          "environment_preflight": build_evidence["environment_preflight"],
                          "failure_attribution": {"party": "candidate", "observed_by": "evaluator", "fatal": True,
                              "reason": "Candidate source does not compile", "evidence_paths": [str(materialized / "build_result.json")]}}
                (case_out / "result.json").write_text(json.dumps(result, indent=2) + "\n")
                self._score_public(case_id, case_out / "result.json", digest(materialized))
                record["dev"][case_id] = {"exit_code": 0, "result": str(case_out / "result.json")}
        if build.returncode==0 and self.real:
            for case_id in self.public_cases:
                case_out=attempt / "evaluations" / case_id
                spec=attempt/f"spec_{case_id}.json"; spec.write_text(json.dumps(case_spec(case_id, CASES["public_dev"][case_id]),indent=2)+"\n")
                command=["python3",str(ROOT/"evaluator/harness/run_lower_agent_case.py"),"--candidate-source",str(materialized),"--case-id",case_id,"--case-spec",str(spec),"--broker-endpoint",str(self.broker),"--output-dir",str(case_out)]
                command.extend(["--candidate-digest", digest(materialized)])
                if self.image:
                    command.extend(["--image", self.image])
                if self.dependency_overlay:
                    command.extend(["--dependency-overlay",str(self.dependency_overlay)])
                create(attempt / "intents" / f"{case_id}-lower.json", {
                    "case_id": case_id, "candidate_digest": digest(materialized),
                    "stable_product_digest": record.get("stable_product_digest"), "started_at": now()})
                receipt_root = self.run_dir.parent/'brokers/public-lower-transport/lower_requests'
                before_requests = {p.name for p in receipt_root.iterdir() if p.is_dir()} if self.readiness_profile else set()
                done=subprocess.run(command,text=True,capture_output=True,check=False)
                if self.readiness_profile:
                    after_requests = {p.name for p in receipt_root.iterdir() if p.is_dir()}
                    create(attempt/'readiness_request_binding.json', {'candidate_digest':candidate_digest,
                        'before':sorted(before_requests),'after':sorted(after_requests),'request_ids':sorted(after_requests-before_requests)})
                result_path = case_out / "result.json"
                raw = self._case_result(str(result_path))
                create(attempt / "checkpoints" / f"{case_id}-lower.json", {
                    "case_id": case_id, "exit_code": done.returncode,
                    "result_path": str(result_path), "result_sha256": file_digest(result_path),
                    "result": raw})
                create(attempt / "intents" / f"{case_id}-result.json", {
                    "case_id": case_id, "result_sha256": file_digest(result_path),
                    "candidate_digest": digest(materialized), "started_at": now()})
                try:
                    self._score_public(case_id, case_out / "result.json", digest(materialized))
                except PublicScoringBoundaryError as exc:
                    failed = self._case_result(str(case_out / "result.json"))
                    failed.update({"case_id": case_id, "classification": exc.classification, "score": None,
                                   "infrastructure_invalid": exc.classification == "evaluator_infrastructure_failure",
                                   "scoring_error": f"{type(exc).__name__}: {exc}"})
                    case_out.mkdir(parents=True, exist_ok=True)
                    result_path = case_out / "scoring_failure.json"
                    create(result_path, failed)
                except Exception as exc:
                    failed = self._case_result(str(case_out / "result.json"))
                    failed.update({"case_id": case_id, "classification": "unresolved_evaluator_boundary", "score": None,
                                   "infrastructure_invalid": False,
                                   "scoring_error": f"{type(exc).__name__}: {exc}"})
                    case_out.mkdir(parents=True, exist_ok=True)
                    result_path = case_out / "scoring_failure.json"
                    create(result_path, failed)
                create(attempt / "checkpoints" / f"{case_id}-result.json", {
                    "result_path": str(result_path), "result_sha256": file_digest(result_path),
                    "result": self._case_result(str(result_path))})
                record["dev"][case_id]={"exit_code":done.returncode,"stdout_tail":done.stdout[-1600:],"result":str(result_path)}
            eligible, reasons = self._dev_is_freeze_eligible(record)
            record["state"] = "completed" if eligible else "completed_with_invalid_dev"
            record["freeze_eligible"] = eligible
            record["eligibility_reasons"] = reasons
        elif build.returncode==0:
            record["state"]="static_build_only"
        if self.real:
            invalid, reasons = self._dev_has_infrastructure_failure(record)
            if invalid:
                unresolved = any("unresolved_evaluator_boundary" in reason for reason in reasons)
                record.update({"accepted": False, "round_consumed": False,
                               "state": "unresolved" if unresolved else "infrastructure_invalid",
                               "classification": "unresolved_evaluator_boundary" if unresolved
                                   else "evaluator_infrastructure_failure",
                               "reasons": reasons})
                self.attempts.finish(candidate_digest, record)
                return record
        record["accepted"] = True
        if not post_freeze_superseded(self, record):
            self.records.append(record)
        self._write_feedback(record)
        self._write_lifecycle()
        if number >= self.max_dev_rounds and not self.readiness_profile:
            # `self.records[0]` was a two-round assumption: with --max-dev-rounds 5
            # an infrastructure failure in rounds 2..4 could not block the freeze.
            # `record` was appended just above, so self.records[:-1] is every
            # earlier accepted round.  The dead `eligible, reasons` binding that
            # stood here is gone; both names were rebound or unused before any read.
            earlier_infra = [self._dev_has_infrastructure_failure(item)
                             for item in self.records[:-1]]
            current_infra, current_infra_reasons = self._dev_has_infrastructure_failure(record)
            previous_infra = any(flag for flag, _ in earlier_infra)
            previous_infra_reasons = [reason for flag, reasons in earlier_infra
                                      if flag for reason in reasons]
            if current_infra or previous_infra or not self.feedback:
                blocked = {
                    "schema_version": "agentswe-aider-freeze-blocked-v1",
                    "candidate_digest": candidate_digest,
                    "blocked_at": now(),
                    "reasons": previous_infra_reasons + current_infra_reasons + (["missing_feedback"] if not self.feedback else []),
                }
                (self.run_dir/"freeze_blocked.json").write_text(json.dumps(blocked,indent=2,ensure_ascii=False)+"\n")
                record["state"] = "completed_but_freeze_blocked"
                record["freeze_eligible"] = False
                record["eligibility_reasons"] = blocked["reasons"]
                self._write_lifecycle()
                self.attempts.finish(candidate_digest, record)
                return record
            frozen=self.run_dir/"frozen_candidate"
            frozen_source_digest=digest(materialized)
            shutil.copytree(materialized,frozen,symlinks=True)
            _make_tree_read_only(frozen)
            if digest(frozen) != frozen_source_digest:
                raise RuntimeError("frozen latest Candidate materialized digest mismatch")
            self.frozen=self._freeze_manifest(record, frozen_source_digest, "max_dev_rounds")
            (self.run_dir/"freeze_manifest.json").write_text(json.dumps(self.frozen,indent=2)+"\n")
            self._write_lifecycle()
        self.attempts.finish(candidate_digest, record)
        return record

    def _freeze_manifest(self, record: dict[str, Any], frozen_digest: str, reason: str) -> dict[str, Any]:
        if reason not in {"builder_exit", "max_dev_rounds"}:
            raise ValueError("invalid freeze reason")
        digests = [item.get("candidate_digest") for item in self.records]
        chain_complete = bool(self.records) and all(
            isinstance(item.get("feedback_digest"), str)
            and (index == 0 or item.get("feedback_digest_ack") == self.records[index - 1].get("feedback_digest"))
            for index, item in enumerate(self.records)
        )
        return {
            "schema_version": "agentswe-aider-freeze-manifest-v2",
            "candidate_digest": frozen_digest,
            "candidate_materialized_digest": frozen_digest,
            "candidate_delivery_digest": record.get("candidate_digest"),
            "accepted_submission_count": len(self.records),
            "accepted_candidate_digests": digests,
            "source_submission": int(record["submission_number"]),
            "source_submission_id": f"candidate-{int(record['submission_number']):03d}",
            "frozen_candidate_path": str(self.run_dir / "frozen_candidate"),
            "frozen_at": now(),
            "freeze_reason": reason,
            "hidden_started_after_freeze": False,
            "frozen_tree_read_only": True,
            "frozen_tree_regular": True,
            "build_digest": frozen_digest,
            "dev_gate": {"accepted_rounds": len(self.records), "latest": True, "feedback": True},
            "hidden_case_inventory": list(self.hidden_cases),
            "pilot_not_formal": self.pilot_not_formal,
            "feedback_digest": self.feedback_digest,
            "feedback_consumed": True,
            "feedback_chain_complete": chain_complete,
            "dev_evaluated": True,
        }

    def freeze_latest(self, reason: str = "builder_exit", *, builder_exit_evidence=None) -> dict[str, Any]:
        """Freeze the latest accepted snapshot when the Builder exits early."""
        if self.readiness_profile:
            from harbor.readiness_contract import validate_freeze
            validate_freeze(self, reason, builder_exit_evidence)
        if self.frozen:
            return self.frozen
        if not self.records:
            raise RuntimeError("cannot freeze without an accepted Candidate")
        record = self.records[-1]
        materialized = Path(record.get("materialized_path") or self.run_dir / f"materialized_{int(record['submission_number']):03d}")
        if not materialized.is_dir():
            raise RuntimeError("latest accepted Candidate has no materialized snapshot")
        frozen = self.run_dir / "frozen_candidate"
        shutil.copytree(materialized, frozen, symlinks=True)
        _make_tree_read_only(frozen)
        frozen_digest = digest(frozen)
        if frozen_digest != digest(materialized):
            raise RuntimeError("frozen Candidate digest mismatch")
        self.frozen=self._freeze_manifest(record, frozen_digest, reason)
        if self.readiness_profile:
            self.frozen.update(readiness_profile=self.readiness_profile, current_binding=self.current_binding,
                builder_session_id=record['builder_session_id'], submission_sha256={name:file_digest(Path(record['candidate_path'])/name) for name in ('solution.patch','edit_report.json','run_report.json')})
        self.run_dir.joinpath("freeze_manifest.json").write_text(json.dumps(self.frozen,indent=2,ensure_ascii=False)+"\n",encoding="utf-8")
        self._write_lifecycle()
        return self.frozen

    def run_hidden(self) -> list[dict[str,Any]]:
        if not self.frozen: raise RuntimeError("hidden requires latest accepted Candidate freeze")
        if not self.real: return [{"case_id":x,"classification":"static_smoke_only"} for x in self.hidden_cases]
        frozen_path=Path(str(self.frozen.get("frozen_candidate_path", self.run_dir/"frozen_candidate")))
        if not frozen_path.is_dir(): raise RuntimeError("frozen latest Candidate snapshot is missing")
        expected_digest=str(self.frozen.get("candidate_digest")); before_suite=digest(frozen_path)
        if before_suite != expected_digest: raise RuntimeError("frozen latest Candidate digest changed before hidden")
        results=[]
        for case_id in self.hidden_cases:
            base = CASES["hidden"][case_id]
            if digest(frozen_path) != expected_digest:
                raise RuntimeError(f"frozen latest Candidate digest changed before {case_id}")
            spec=self.run_dir/f"spec_{case_id}.json"; spec.write_text(json.dumps(case_spec(case_id, base),indent=2)+"\n")
            out=self.run_dir/f"evaluations/hidden/{case_id}"; command=["python3",str(ROOT/"evaluator/harness/run_lower_agent_case.py"),"--candidate-source",str(frozen_path),"--case-id",case_id,"--case-spec",str(spec),"--broker-endpoint",str(self.broker),"--output-dir",str(out)]
            command.extend(["--candidate-digest", expected_digest])
            if self.image:
                command.extend(["--image", self.image])
            if self.dependency_overlay:
                command.extend(["--dependency-overlay",str(self.dependency_overlay)])
            done=subprocess.run(command,text=True,capture_output=True,check=False); result_path=out/"result.json"; result=self._case_result(str(result_path)); artifact_path=out/"agent_artifact.json"; results.append({**result,"case_id":case_id,"exit_code":done.returncode,"result":str(result_path),"result_sha256":file_digest(result_path),"artifact":str(artifact_path) if artifact_path.is_file() else None,"artifact_sha256":file_digest(artifact_path),"artifact_contract":result.get("artifact_contract"),"classification":result.get("classification"),"broker":result.get("broker",{}),"terminal_product_artifact":bool(result.get("answer"))})
            if digest(frozen_path) != expected_digest:
                raise RuntimeError(f"frozen latest Candidate digest changed during {case_id}")
        digest_before_hidden = digest(frozen_path)
        attestation={
            "schema_version": "agentswe-aider-hidden-after-freeze-attestation-v1",
            "freeze_manifest": str(self.run_dir/"freeze_manifest.json"),
            "candidate_digest": self.frozen.get("candidate_digest"),
            "frozen_candidate_path": str(frozen_path),
            "frozen_digest_before": digest_before_hidden,
            "frozen_digest_stable": digest_before_hidden == expected_digest,
            "started_after_freeze": True,
            "expected_cases": list(self.hidden_cases),
            "executed_cases": [x["case_id"] for x in results],
            "results": results,
            "all_cases_materialized": len(results) == len(self.hidden_cases) and all(Path(x["result"]).is_file() and Path(str(x.get("artifact") or "")).is_file() for x in results),
            "all_cases_real": all(x.get("classification") != "static_smoke_only" for x in results),
            "pilot_not_formal": self.pilot_not_formal,
            "formal_result_publishable": False if self.pilot_not_formal else None,
        }
        digest_after_hidden = digest(frozen_path)
        attestation["frozen_digest_after"] = digest_after_hidden
        attestation["frozen_digest_stable"] = bool(
            attestation["frozen_digest_stable"] and digest_after_hidden == expected_digest
        )
        attestation["complete"] = bool(
            attestation["all_cases_materialized"]
            and attestation["all_cases_real"]
            and attestation["frozen_digest_stable"]
        )
        (self.run_dir/"hidden-after-freeze-attestation.json").write_text(json.dumps(attestation,indent=2,ensure_ascii=False)+"\n")
        return results

    def _write_lifecycle(self) -> None:
        checkpoint(self.run_dir/"dev_lifecycle.json", {
            "schema_version": "agentswe-aider-dev-lifecycle-v2",
            "evidence_kind": "pilot" if self.pilot_not_formal else "formal",
            "max_dev_rounds": self.max_dev_rounds,
            "n_concurrent": 1,
            "dev_passed_is_automatic_freeze": False,
            "records": self.records,
            "feedback_digest": self.feedback_digest,
            "frozen": self.frozen,
        })


def main() -> int:
    p=argparse.ArgumentParser(); p.add_argument("--source",type=Path,default=ROOT/"input/repository"); p.add_argument("--candidate-1",type=Path); p.add_argument("--candidate-2",type=Path); p.add_argument("--run-dir",type=Path,required=True); p.add_argument("--broker-endpoint"); p.add_argument("--dependency-overlay",type=Path); p.add_argument("--image"); p.add_argument("--run-real",action="store_true"); p.add_argument("--hidden",action="store_true"); a=p.parse_args()
    if a.run_real and not a.broker_endpoint: p.error("--run-real requires --broker-endpoint")
    c=Controller(a.run_dir.resolve(),a.source.resolve(),a.broker_endpoint,a.run_real,a.dependency_overlay.resolve() if a.dependency_overlay else None,a.image)
    for candidate in (a.candidate_1,a.candidate_2):
        if candidate: c.submit(candidate.resolve())
    if a.hidden: print(json.dumps(c.run_hidden(),indent=2))
    else: print(json.dumps({"status":"staged" if not a.run_real else "ready_for_pilot","records":c.records,"freeze":c.frozen},indent=2))
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


_fr_wrap_submit(Controller)
_FR_WRAPPED_FREEZE = [name for name in _FR_FREEZE_METHODS if _fr_wrap_freeze(Controller, name)]
assert _FR_WRAPPED_FREEZE, 'freeze/submit race guard found no freeze entry point'
# --- end 0921b freeze/submit race guard ------------------------------------


if __name__=="__main__": raise SystemExit(main())
