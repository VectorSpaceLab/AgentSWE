#!/usr/bin/env python3
"""Accepted-submission Candidate controller with dev feedback and freeze fencing."""
from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
from datetime import datetime, timezone
import time
from evaluator.harness.product_attempts import write_attempt
from evaluator.harness.public_execution_diagnostic import PublicExecutionDiagnostic, collect_public_diagnostic
from evaluator.harness.public_semantic_feedback import PublicCaseRejectionFeedback, collect_public_case_feedback
from pathlib import Path
from typing import Any, Callable

if __package__ in {None, ""}:
    from candidate_adapter import build, materialize, tree_digest
    from case_specs import HIDDEN_CASES, PUBLIC_CASES, canonical_hidden_case
else:
    from .candidate_adapter import build, materialize, tree_digest
    from .case_specs import HIDDEN_CASES, PUBLIC_CASES, canonical_hidden_case


# --- judge resample (2026-09-20) ---------------------------------------------
# A shared Result judge that completed one paid-for response and then refused to
# publish it is an evaluator-side SAMPLING fault: the apparatus is healthy, the
# sample is not.  Booking it as infrastructure loses the Candidate's public
# round -- and on the ai-scientist tree it latched a terminal that ended the
# whole formal run (0905-edit-codex-xhigh-0919-fw-001-ai-scientist, 03:04 CST:
# deepseek-flash emitted a complete 100/100 verdict missing exactly one `}`,
# result_judge.close_unclosed_json appended it at the END of the text instead of
# at the container the model left open, and the misnested parse produced 31
# validation errors).  Across the 121 result contracts on this host 8 of the 65
# real judge calls are `model_output_invalid` -- about 12% -- so this is a
# routine event, not a freak one.
#
# The hidden axis already recovers by setting the refused attempt aside and
# judging again (see <run>/formal_scoring/result_axis/<case>.attempt-001-invalid
# on this host).  This gives the public round the same remedy, automatically and
# exactly once.  Nothing measured is repeated: the Candidate product is not
# re-executed, no lower-agent call is re-issued, the immutable evidence inputs
# are reused byte for byte, and the refused attempt is renamed rather than
# deleted so both verdicts stay auditable.  The control plane's
# one-response-per-immutable-directory contract (execution_scoring.py:96-116,
# result_judge.py:943-946) is preserved because the retry gets a fresh
# directory.
RESAMPLEABLE_JUDGE_STATES = {"model_output_invalid"}
JUDGE_RESAMPLE_ATTEMPTS = 1
# Everything the shared judge (result_judge.py:885-1016), the shared scorer
# (execution_scoring.py:96-119) and the task-local once-guards author inside the
# judge directory.  Any OTHER file there was written by this caller before the
# judge ran and is restored byte for byte into the fresh attempt.
JUDGE_AUTHORED_FILES = (
    "scoring_intent.json", "input_manifest.json", "judge_prompt.txt",
    "logical_request_started.json", "provider_response.json",
    "provider_response-attempts.json", "model_response.json",
    "result_eval_result.json", "result_score_contract.json",
    "task_judge_intent.json", "task_judge_invocation.json",
    "task_cli_stdout.log", "task_cli_stderr.log",
)


def refused_judge_state(judge_dir):
    """The judge's own evaluation_state when it answered and refused to publish.

    None keeps today's behaviour untouched.  A resample is offered only when the
    contract proves the apparatus worked: exactly one completed response with
    complete usage accounting, refused over the model's output rather than over
    the infrastructure.  A missing, unreadable, transport-failed or
    ``infrastructure_error`` contract returns None and stays terminal.
    """
    import json as _json
    from pathlib import Path as _Path
    try:
        contract = _json.loads(
            (_Path(judge_dir) / "result_score_contract.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if not isinstance(contract, dict) or contract.get("contract_valid") is True:
        return None
    usage = contract.get("provider_usage")
    if not isinstance(usage, dict):
        return None
    if usage.get("completed_responses") != 1 or usage.get("usage_complete") is not True:
        return None
    state = contract.get("evaluation_state")
    return state if state in RESAMPLEABLE_JUDGE_STATES else None


def resample_refused_verdict(judge_dir, rejudge, *, recreate_dir=True):
    """Ask the evaluator's own Result judge again, in a new immutable directory.

    Returns the new judge outcome, or None when nothing was resampled and the
    caller must keep the outcome it already holds.  ``recreate_dir`` is False
    for callers whose own once-guard creates the directory with
    ``mkdir(exist_ok=False)``.
    """
    import json as _json
    import shutil as _shutil
    from pathlib import Path as _Path
    judge_dir = _Path(judge_dir)
    outcome = None
    for attempt in range(1, JUDGE_RESAMPLE_ATTEMPTS + 1):
        state = refused_judge_state(judge_dir)
        if state is None:
            return outcome
        retired = judge_dir.parent / ("%s.attempt-%03d-%s" % (judge_dir.name, attempt, state))
        if retired.exists() or not judge_dir.is_dir():
            return outcome
        judge_dir.rename(retired)
        restored = []
        if recreate_dir:
            judge_dir.mkdir(parents=True)
            for item in sorted(retired.iterdir()):
                if item.name in JUDGE_AUTHORED_FILES or item.name.startswith("provider_response-attempt-"):
                    continue
                (_shutil.copytree if item.is_dir() else _shutil.copy2)(item, judge_dir / item.name)
                restored.append(item.name)
        (judge_dir.parent / ("judge_resample_%03d.json" % attempt)).write_text(
            _json.dumps({
                "schema_version": "agentswe-edit-judge-resample-v1",
                "attempt": attempt,
                "judge_dir": str(judge_dir),
                "retired_attempt": str(retired),
                "retired_evaluation_state": state,
                "restored_caller_inputs": restored,
                "candidate_product_re_executed": False,
                "lower_agent_calls_re_issued": 0,
                "reason": "the shared Result judge completed one response and refused to publish "
                          "it; the evaluator's own judge is resampled into a new immutable "
                          "directory and the refused attempt is retained",
            }, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
        outcome = rejudge()
    return outcome


# --- dev/hidden judge parity (2026-09-21) -------------------------------------
# The HIDDEN axis hands the shared Result judge two task-owned inputs that the
# PUBLIC dev axis did not issue at all:
#
#   * a bounded judge projection of the trajectory and of the private oracle
#     (evaluator/judge_input_projection.py, wired at evaluator/formal_axes.py:205-219
#     and 230-237), and
#   * an evidence-bound score-cap contract, `agentswe-result-score-caps/v1`
#     (evaluator/result_score_caps.py, wired at evaluator/formal_axes.py:214-218
#     and 238-246), which the shared judge binds to the exact rubric/native/oracle
#     bytes it was given (result_judge.py:908-954) and then enforces arithmetically
#     (result_judge.py:775-793).
#
# Consequence, measured on 0905-edit-codex-xhigh-0921-v4-001-deepcode: round 4
# dev_002 was published to the Builder as 96/100, while the same contract built
# from that round's own recorded dev evidence caps it at 20 (c12, 0 of 2 reserved
# adversarial probes produced a product-persisted refusal receipt).  The Builder
# exits on a dev score that the hidden axis contradicts by 76 points.
#
# This gives the dev judge the same projection and the same contract.  It adds no
# rubric obligation and relaxes none: the conditions come from the same task-owned
# issuer the hidden axis runs, over the dev case's own evidence.  Both steps fail
# OPEN -- any defect degrades to exactly today's behaviour and never fails a round.
DEV_SCORE_CAP_CONTRACT_NAME = "result_score_caps.json"
DEV_HARNESS_ERROR_LINE_CHARS = 400
DEV_HARNESS_ERROR_LINES_MAX = 24


def _task_module(path: Path, name: str):
    """Load a task-owned evaluator module by path, like readiness_smoke.py:120-124."""
    import importlib.util as _ilu
    spec = _ilu.spec_from_file_location(name, str(path))
    module = _ilu.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def dev_judge_inputs(*, task_root, case_id, judge_dir, trajectory, oracle, rubric, native_evidence):
    """Projection + evaluator-issued ceilings for ONE public dev case.

    Returns ``(trajectory_for_judge, oracle_for_judge, cap_contract_or_None,
    cap_entries, binding)``.  The contract is written INSIDE this submission's own
    judge directory, so it is immutable per submission and is restored byte for byte
    into a resample attempt (it is not in JUDGE_AUTHORED_FILES above).  It is issued
    from the PROJECTED oracle because the shared judge re-binds it to the bytes it is
    actually handed (result_judge.py:925-927).
    """
    task_root = Path(task_root)
    judge_trajectory, judge_oracle = Path(trajectory), Path(oracle)
    binding: dict[str, Any] = {
        "schema_version": "deepcode-dev-judge-input-binding-v1", "case_id": case_id,
        "why": "the public dev judge is given the same bounded projection and the same "
               "evaluator-issued score-cap contract as the hidden judge",
        "projection_manifests": [], "score_cap_contract": None, "errors": []}
    try:
        projection = _task_module(task_root / "evaluator" / "judge_input_projection.py",
                                  "deepcode_dev_judge_input_projection")
        judge_trajectory, judge_oracle, manifests = projection.project_case(
            trajectory=judge_trajectory, oracle=judge_oracle)
        binding["projection_manifests"] = [str(item) for item in manifests]
    except Exception as exc:  # noqa: BLE001 - fail open to today's unprojected input
        judge_trajectory, judge_oracle = Path(trajectory), Path(oracle)
        binding["errors"].append("judge input projection unavailable: %s: %s" % (type(exc).__name__, exc))
    binding["trajectory_shown_to_judge"] = str(judge_trajectory)
    binding["oracle_shown_to_judge"] = str(judge_oracle)
    contract_path, entries = None, []
    try:
        caps = _task_module(task_root / "evaluator" / "result_score_caps.py",
                            "deepcode_dev_result_score_caps")
        destination = Path(judge_dir) / DEV_SCORE_CAP_CONTRACT_NAME
        caps.write_contract(destination, case_id=case_id, rubric=Path(rubric),
                            native_evidence=Path(native_evidence), oracle_summary=judge_oracle)
        value = json.loads(destination.read_text(encoding="utf-8"))
        entries = value.get("entries") if isinstance(value.get("entries"), list) else []
        contract_path = destination
        binding["score_cap_contract"] = str(destination)
        binding["score_cap_schema_version"] = value.get("schema_version")
    except Exception as exc:  # noqa: BLE001 - fail open to today's uncapped judging
        binding["errors"].append("evaluator score-cap contract unavailable: %s: %s" % (type(exc).__name__, exc))
    return judge_trajectory, judge_oracle, contract_path, entries, binding


def judge_contract_document(judgement):
    """The shared judge's own contract for this case, or {} when unavailable."""
    path = (judgement or {}).get("contract_path")
    if not path:
        return {}
    try:
        value = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return value if isinstance(value, dict) else {}


def dev_score_cap_facts(entries, contract, binding):
    """Which ceilings fired on this dev case, and which one binds the total.

    Only conditions established as VIOLATED are reported -- by the evaluator
    (determinate, decided from this dev case's own persisted state) or by the judge
    (`ceiling_assessments`, result_judge.py:777-793).  Unviolated and pending
    semantic_review conditions are not enumerated, so the Builder learns which
    published requirement it failed, never the whole condition catalogue.
    """
    rows = []
    maxima, reasons, refs = {}, {}, {}
    for entry in entries:
        if not isinstance(entry, dict) or not isinstance(entry.get("cap_id"), str):
            continue
        maxima[entry["cap_id"]] = entry.get("maximum_score")
        reasons[entry["cap_id"]] = entry.get("reason")
        refs[entry["cap_id"]] = entry.get("requirement_ref")
        if entry.get("status") == "violated":
            rows.append({"cap_id": entry["cap_id"], "maximum_score": entry.get("maximum_score"),
                         "requirement_ref": entry.get("requirement_ref"),
                         "reason": entry.get("reason"), "decided_by": "evaluator"})
    assessments = contract.get("ceiling_assessments")
    if isinstance(assessments, dict):
        for cap_id, item in sorted(assessments.items()):
            if isinstance(item, dict) and item.get("violated") is True:
                rows.append({"cap_id": cap_id, "maximum_score": maxima.get(cap_id),
                             "requirement_ref": refs.get(cap_id),
                             "reason": item.get("evidence") or reasons.get(cap_id),
                             "decided_by": "result_judge"})
    values = [row["maximum_score"] for row in rows
              if type(row.get("maximum_score")) is int]
    binding_value = min(values) if values else None
    return {
        "contract_issued": binding.get("score_cap_contract") is not None,
        "contract_path": binding.get("score_cap_contract"),
        "schema_version": binding.get("score_cap_schema_version"),
        "condition_count": len(entries),
        "violated": rows,
        "violated_cap_ids": [row["cap_id"] for row in rows],
        "binding_cap_value": binding_value,
        "binding_cap_ids": sorted({row["cap_id"] for row in rows
                                   if row.get("maximum_score") == binding_value}) if values else [],
        "evaluator_determinate_score_cap": contract.get("score_cap"),
        "judge_contract_errors": contract.get("errors") if isinstance(contract.get("errors"), list) else [],
        "issue_errors": list(binding.get("errors") or [])}


def _dev_error_line(value):
    text = " ".join(str(value).split())
    if not text:
        return None
    return text if len(text) <= DEV_HARNESS_ERROR_LINE_CHARS else text[:DEV_HARNESS_ERROR_LINE_CHARS - 1] + "\u2026"


def _dev_tail(path, count=6):
    try:
        text = Path(path).read_text(encoding="utf-8", errors="replace")
    except OSError:
        return []
    return [line for line in (item.strip() for item in text.splitlines()) if line][-count:]


def dev_harness_errors(result, output, judgement, contract):
    """Verbatim harness/validator error lines about this Candidate's own dev case.

    These are the evaluator's own tool output -- the task-native artifact validator,
    the shared Result validator, the case driver and the product launcher.  The
    Builder previously saw `candidate_behavior_failure, score 0` with nothing to act
    on (see evaluator/harness/public_feedback.py::case_diagnosis).  No hidden case and
    no private oracle expectation is read here.
    """
    lines = []
    validation = result.get("artifact_validation")
    if isinstance(validation, dict):
        for key in ("errors", "quality_schema_findings"):
            for item in validation.get(key) or []:
                lines.append("artifact_validation.%s: %s" % (key, item))
    if result.get("infra_valid") is False:
        lines.append("harness: infrastructure-invalid case execution (%s)" % result.get("classification"))
    code = result.get("controller_process_exit_code")
    if type(code) is int and code != 0:
        lines.append("harness: case driver exit code %d%s"
                     % (code, " (case deadline reached; this case is a zero and the round is spent)"
                        if code == 124 else ""))
    launcher_code = result.get("candidate_exit_code")
    if type(launcher_code) is int and launcher_code != 0:
        lines.append("harness: product launcher exit code %d" % launcher_code)
    if result.get("lower_agent_output_truncated") is True:
        lines.append("harness: an evaluator-owned lower-model turn ended on its output-token "
                     "ceiling, so this rollout was cut short by the harness, not by the product")
    # Log tails are diagnosis only when something actually went wrong; on a clean
    # scoreable case they are the product's own DEBUG noise.
    went_wrong = bool(lines) or (judgement or {}).get("contract_valid") is not True
    if went_wrong:
        for line in _dev_tail(Path(output) / "launcher.stderr.log"):
            lines.append("launcher.stderr: " + line)
        # DeepCode records result["stderr"] as a PATH to the case stderr log, not its text.
        stderr = result.get("stderr")
        if isinstance(stderr, str) and stderr:
            source = Path(stderr)
            tail = _dev_tail(source) if source.is_file() else [
                item.strip() for item in stderr.splitlines() if item.strip()][-6:]
            for line in tail:
                lines.append("case.stderr: " + line)
    for item in (contract.get("errors") if isinstance(contract.get("errors"), list) else []):
        lines.append("result_judge_validator: %s" % item)
    reason = (judgement or {}).get("reason")
    if isinstance(reason, str) and reason.strip():
        lines.append("result_judge: " + reason.strip())
    bounded = []
    for line in lines:
        line = _dev_error_line(line)
        # A log line that is nothing but an evaluator-owned path carries no diagnosis
        # and would reach the Builder as a bare "[evaluator-private-path]".
        if line is not None and line.split(": ", 1)[-1].startswith("/") and " " not in line.split(": ", 1)[-1]:
            continue
        if line is not None and line not in bounded:
            bounded.append(line)
        if len(bounded) >= DEV_HARNESS_ERROR_LINES_MAX:
            break
    return bounded


class ProtocolError(RuntimeError):
    pass


class PublicEvaluationRejected(ProtocolError):
    """Only controller-created, validated public facts cross the 422 boundary."""
    def __init__(self, message: str, diagnostics=(), *, candidate_digest=None, case_feedback=()):
        super().__init__(message)
        self.public_diagnostics = tuple(value for value in diagnostics
            if type(value) is PublicExecutionDiagnostic)
        self.candidate_digest = candidate_digest
        self.public_case_feedback = tuple(value for value in case_feedback
            if type(value) is PublicCaseRejectionFeedback)


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _write_json(path: Path, value: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(value, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    os.replace(temporary, path)


def _make_read_only(root: Path) -> None:
    """Fence the frozen source while keeping it readable for case copies."""
    for path in sorted(root.rglob("*"), key=lambda item: len(item.parts), reverse=True):
        if path.is_symlink():
            continue
        try:
            path.chmod(path.stat().st_mode & ~0o222)
        except OSError as exc:
            raise ProtocolError(f"unable to make frozen Candidate immutable: {path}: {exc}") from exc
    root.chmod(root.stat().st_mode & ~0o222)


class CandidateController:
    def __init__(self, *, base_repository: Path, run_dir: Path, launcher: Path, broker_endpoint: str | None, runtime_python: str | None = None, hidden_root: Path | None = None, dry_run: bool = False, public_case_ids: tuple[str, ...] = tuple(PUBLIC_CASES), hidden_case_ids: tuple[str, ...] = tuple(HIDDEN_CASES), evidence_kind: str = "formal", max_dev_rounds: int = 10, result_judge_endpoint: str | None = None, readiness_profile: str | None = None, current_binding: dict | None = None) -> None:
        self.base_repository, self.run_dir, self.launcher = base_repository, run_dir, launcher
        self.broker_endpoint, self.runtime_python, self.dry_run = broker_endpoint, runtime_python, dry_run
        self.result_judge_endpoint = result_judge_endpoint
        self.public_case_ids = tuple(public_case_ids)
        self.hidden_case_ids = tuple(hidden_case_ids)
        self.evidence_kind = "smoke" if dry_run else evidence_kind
        self.readiness_profile = readiness_profile
        self.current_binding = current_binding
        if readiness_profile:
            if not isinstance(current_binding, dict) or current_binding.get('task') != 'deepcode':
                raise ValueError('DeepCode readiness binding required')
            for key in ('source_digest', 'contract_digest', 'registry_digest'):
                value = current_binding.get(key)
                if not isinstance(value, str) or len(value) != 64 or any(c not in '0123456789abcdef' for c in value):
                    raise ValueError('readiness binding lacks ' + key)
        if readiness_profile and (readiness_profile != 'single-dev-two-round-hidden-smoke-v1'
                or evidence_kind != 'pilot' or dry_run or tuple(public_case_ids) != ('dev_001',)
                or tuple(hidden_case_ids) != ('test_001',) or max_dev_rounds != 2):
            raise ValueError('readiness requires real pilot dev001 two rounds and test001')
        if not 1 <= max_dev_rounds <= 10:
            raise ValueError("max_dev_rounds must be in 1..10")
        self.max_dev_rounds = max_dev_rounds
        if not self.public_case_ids or not set(self.public_case_ids).issubset(PUBLIC_CASES):
            raise ValueError("public_case_ids must be a non-empty subset of the canonical public inventory")
        if not self.hidden_case_ids or not set(self.hidden_case_ids).issubset(HIDDEN_CASES):
            raise ValueError("hidden_case_ids must be a non-empty subset of the canonical hidden inventory")
        if self.evidence_kind not in {"formal", "pilot", "smoke"}:
            raise ValueError("evidence_kind must be formal, pilot, or smoke")
        self.hidden_root = hidden_root.resolve() if hidden_root else None
        self._public_diagnostics: dict[str, PublicExecutionDiagnostic] = {}
        self.records: list[dict[str, Any]] = []
        self.frozen: dict[str, Any] | None = None
        self.run_dir.mkdir(parents=True, exist_ok=True)

    def submit(
        self,
        candidate: Path,
        *,
        acceptance_guard: Callable[[], None] | None = None,
    ) -> dict[str, Any]:
        round_no = len(self.records) + 1
        dest = self.run_dir / "candidates" / f"candidate_{round_no:03d}"
        attempt = self.run_dir / "candidates" / f".candidate_attempt_{time.time_ns()}"
        try:
            materialize(candidate, attempt, base_repository=self.base_repository)
            digest = tree_digest(attempt)
            prior = next(
                (
                    record
                    for record in self.records
                    if record["candidate_digest"] == digest
                ),
                None,
            )
            if prior is not None:
                if self.readiness_profile:
                    raise ProtocolError('readiness forbids duplicate materialized product')
                return prior
            intent = self.run_dir/'product_attempts'/(digest + '.json')
            if intent.exists():
                raise ProtocolError('This exact materialized product has a preserved prior evaluation; no replay is allowed.')
            if self.frozen is not None:
                raise ProtocolError("Candidate lifecycle is already frozen")
            if len(self.records) >= self.max_dev_rounds:
                raise ProtocolError("accepted submission limit reached")
            if acceptance_guard is not None:
                acceptance_guard()
            dest.parent.mkdir(parents=True, exist_ok=True)
            if dest.exists():
                raise ProtocolError(f"accepted Candidate destination already exists: {dest}")
            write_attempt(intent, {'candidate_digest': digest, 'state': 'reserved_before_build_and_dev'}, initial=True)
            os.replace(attempt, dest)
        finally:
            shutil.rmtree(attempt, ignore_errors=True)
        self.current_evaluation_root = self.run_dir / "evaluations" / f"candidate_{round_no:03d}_attempt_{time.time_ns()}"
        self._public_diagnostics = {}
        build_result = (build(dest, self.current_evaluation_root / "build", readiness_profile=self.readiness_profile)
            if self.readiness_profile else build(dest, self.current_evaluation_root / "build"))
        record: dict[str, Any] = {
            "round": round_no,
            "source_submission": round_no,
            "candidate_digest": digest,
            "build": build_result,
            "dev": [],
            "submitted_at": _utc_now(),
            "candidate_path": str(dest),
        }
        if build_result["exit_code"] == 0 and not self.dry_run:
            if not self.broker_endpoint: raise ProtocolError("broker endpoint is required for lower-agent execution")
            for case_id in self.public_case_ids:
                write_attempt(intent, {'candidate_digest': digest, 'state': 'case_in_progress', 'active_case': case_id, 'record': record})
                record["dev"].append(self._run_dev(dest, case_id, round_no))
                write_attempt(intent, {'candidate_digest': digest, 'state': 'case_finished', 'record': record})
        else:
            record["dev"] = [{"case_id": case_id, "status": "build_failure" if build_result["exit_code"] else "dry_run"} for case_id in self.public_case_ids]
        record["evidence_kind"] = self.evidence_kind
        if not self.dry_run and (build_result["exit_code"] != 0 or any(item.get("semantic_score_contract_valid") is not True for item in record["dev"])):
            record.update({"accepted": False, "round_consumed": False})
            rejected = self.run_dir / "rejected_evaluations" / f"candidate_{round_no:03d}_{time.time_ns()}"
            rejected.mkdir(parents=True)
            os.replace(dest, rejected / "candidate")
            record["rejected_candidate_path"] = str(rejected / "candidate")
            record["evaluation_evidence_root"] = str(self.current_evaluation_root)
            _write_json(rejected / "record.json", record)
            write_attempt(intent, {'candidate_digest': digest, 'state': 'not_evaluable', 'record': record})
            public_cases = []
            for case_id in self.public_case_ids:
                try:
                    feedback = collect_public_case_feedback(run_dir=self.run_dir,
                        evaluation_root=self.current_evaluation_root, repository=dest,
                        case_root=self.base_repository.parent.parent / 'dev_cases' / case_id,
                        case_id=case_id, candidate_digest=digest)
                    if type(feedback) is PublicCaseRejectionFeedback:
                        public_cases.append(feedback)
                except Exception:
                    # Optional diagnostics must not replace the terminal rejection
                    # or re-enter case/scorer execution when evidence is unavailable.
                    pass
            raise PublicEvaluationRejected(f"evaluation unresolved or infrastructure-invalid; accepted round not consumed; inspect {rejected / 'record.json'}", self._public_diagnostics.values(),
                candidate_digest=digest, case_feedback=public_cases)
        record["accepted"] = True
        if not post_freeze_superseded(self, record):
            self.records.append(record)
        write_attempt(intent, {'candidate_digest': digest, 'state': 'accepted', 'record': record})
        _write_json(self.run_dir / f"dev_feedback_candidate_{round_no:03d}.json", record)
        return record

    def _run_dev(self, repository: Path, case_id: str, round_no: int) -> dict[str, Any]:
        case = self.base_repository.parent.parent / "dev_cases" / case_id
        if not case.is_dir() or case.is_symlink():
            raise ProtocolError(f"public case unavailable: {case_id}")
        output = self.current_evaluation_root / case_id
        request_path = output.with_name(output.name + '-case-request.json')
        _write_json(request_path, {'repository': str(repository), 'case': str(case), 'output': str(output),
            'launcher': str(self.launcher), 'broker_endpoint': self.broker_endpoint or '', 'case_id': case_id,
            'candidate_digest': tree_digest(repository),
            'runtime_python': self.runtime_python or os.environ.get('DEEPCODE_PYTHON')})
        command = [sys.executable, '-I', str(Path(__file__).with_name('dev_case_runner.py')), '--request', str(request_path)]
        proc = subprocess.run(command, text=True, capture_output=True, timeout=605, check=False)
        try: result = json.loads((output / 'controller_execution_result.json').read_text())
        except (OSError, ValueError): result = {'classification': 'evaluator_infrastructure_error',
            'infra_valid': False, 'stderr': proc.stderr[-1000:], 'case_id': case_id}
        if result.get('infra_valid') is False:
            diagnostic = collect_public_diagnostic(run_dir=self.run_dir,
                evaluation_root=self.current_evaluation_root, repository=repository,
                case_root=case, case_id=case_id, candidate_digest=tree_digest(repository))
            if diagnostic is not None:
                self._public_diagnostics[case_id] = diagnostic
        runtime_repository = output / 'runtime_repository'
        home = output / 'deepcode-home'; workspace = output / 'workspace'; task = case / 'input.md'
        oracle_path = output / 'private-semantic-oracle.json'
        shared = "@@AGENTSWE_EDITING_CONTROL@@"
        if shared not in sys.path: sys.path.insert(0, shared)
        from execution_scoring import judge_execution_case
        result["candidate_digest"] = tree_digest(repository)
        if self.readiness_profile:
            from execution_contract import classify_candidate_execution
            verdict = classify_candidate_execution(result, case_id=case_id, candidate_digest=result['candidate_digest'])
            valid = (result.get('infra_valid') is True and verdict['classification'] in {'scoreable', 'candidate_zero'}
                and result.get('real_execution') is True and (result.get('broker_delta') or {}).get('successful_calls', 0) > 0)
            judgement = {'classification': verdict['classification'], 'score': None,
                'contract_valid': valid, 'round_consumed': valid, 'assessment': verdict.get('reason'),
                'judge_invoked': False, 'evaluation_mode': 'readiness_public_execution'}
        else:
            judge_dir = output / "semantic_scoring"
            task_root = self.base_repository.parent.parent
            rubric = task_root / "evaluator/agentloop_result_rubric.md"
            native_evidence = output / "result.json"
            # Same bounded projection and same evidence-bound ceilings as the hidden
            # axis (evaluator/formal_axes.py:205-247).  `execution_record=result` must
            # stay byte-identical to controller_execution_result.json at judge time --
            # public_semantic_feedback.py:186-190 re-derives and compares it -- so every
            # field this patch adds to `result` is added AFTER the judge has run.
            judge_trajectory, judge_oracle, cap_contract, cap_entries, cap_binding = dev_judge_inputs(
                task_root=task_root, case_id=case_id, judge_dir=judge_dir,
                trajectory=output / "stdout.jsonl", oracle=oracle_path,
                rubric=rubric, native_evidence=native_evidence)
            _write_json(output / "dev_judge_input_binding.json", cap_binding)

            def rejudge():
                return judge_execution_case(case_input=task, rubric=rubric,
                    artifact=workspace / "agent_result.json", raw_trajectory=judge_trajectory, native_evidence=native_evidence,
                    private_oracle=judge_oracle, execution_record=result, candidate_digest=result["candidate_digest"], case_id=case_id,
                    output=judge_dir, broker_endpoint=self.result_judge_endpoint, score_cap_contract=cap_contract)

            judgement = rejudge()
            again = resample_refused_verdict(judge_dir, rejudge)
            if again is not None:
                judgement = again
            judge_contract = judge_contract_document(judgement)
            result["dev_judge_input_binding"] = cap_binding
            result["result_score_caps"] = dev_score_cap_facts(cap_entries, judge_contract, cap_binding)
            result["harness_errors"] = dev_harness_errors(result, output, judgement, judge_contract)
        result.update({"score": judgement["score"], "semantic_score_contract_valid": judgement["contract_valid"],
            "semantic_judgement": judgement, "round_consumed": judgement["round_consumed"]})
        result['execution_record_path'] = str(output / 'controller_result.json')
        result['context_path'] = str(output / 'logical-context.json')
        (output / "controller_result.json").write_text(json.dumps(result, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
        return result

    def freeze(self, *, builder_exit_evidence: dict | None = None) -> dict[str, Any]:
        if not self.records: raise ProtocolError("freeze requires at least one accepted submission")
        if self.readiness_profile:
            if len(self.records) != 2 or not builder_exit_evidence or builder_exit_evidence.get('returncode') != 0 or builder_exit_evidence.get('native_valid') is not True:
                raise ProtocolError('readiness freeze requires exactly two valid rounds and native Builder exit')
            if any(record.get('builder_session_id') != builder_exit_evidence.get('builder_session_id') for record in self.records):
                raise ProtocolError('readiness Builder session changed')
            first, second = self.records
            if first['candidate_digest'] == second['candidate_digest'] or first['submission_sha256']['solution.patch'] == second['submission_sha256']['solution.patch']:
                raise ProtocolError('readiness requires a real product revision')
            for record in self.records:
                if tree_digest(Path(record['candidate_path'])) != record['candidate_digest']:
                    raise ProtocolError('accepted product changed before freeze')
                stable = record['build'].get('readiness_source_immutability', {})
                if stable.get('unchanged') is not True or stable.get('before') != stable.get('after'):
                    raise ProtocolError('readiness source immutability missing')
                from v2_readiness import tree_digest as delivery_digest
                delivery = Path(record['delivery_path'])
                if delivery_digest(delivery) != record['delivery_candidate_digest']:
                    raise ProtocolError('readiness delivery changed before freeze')
                if set(delivery.iterdir()) != {delivery/n for n in ('solution.patch', 'edit_report.json', 'run_report.json')}:
                    raise ProtocolError('readiness delivery requires exactly three files')
        final = self.records[-1]; frozen_path = self.run_dir / "frozen_candidate"
        final_build = final.get("build")
        if not isinstance(final_build, dict) or final_build.get("exit_code") != 0:
            raise ProtocolError("the latest accepted Candidate must pass the evaluator build gate before freeze")
        if frozen_path.exists():
            raise ProtocolError("immutable frozen Candidate copy already exists")
        source = self.run_dir / "candidates" / f"candidate_{final['source_submission']:03d}"
        if not source.is_dir():
            raise ProtocolError("latest accepted Candidate materialization is unavailable")
        shutil.copytree(source, frozen_path, symlinks=True)
        actual = tree_digest(frozen_path)
        if actual != final["candidate_digest"]: raise ProtocolError("freeze digest mismatch")
        _make_read_only(frozen_path)
        after = tree_digest(frozen_path)
        if after != actual: raise ProtocolError("freeze digest changed while fencing")
        writable = []
        for path in [frozen_path, *frozen_path.rglob("*")]:
            if path.is_symlink():
                continue
            if path.stat().st_mode & 0o222:
                writable.append(str(path))
        if writable:
            raise ProtocolError(f"frozen Candidate is not immutable: {writable[:3]}")
        frozen_at = _utc_now()
        self.frozen = {
            "schema_version": "deepcode-agentloop-freeze-v2",
            "evidence_kind": self.evidence_kind,
            "public_case_inventory": list(self.public_case_ids),
            "hidden_case_inventory": list(self.hidden_case_ids),
            "candidate_digest": actual,
            "candidate_path": str(frozen_path),
            "frozen_at": frozen_at,
            "dev_feedback": self.records,
            "hidden_allowed": True,
            "source_submission": final["source_submission"],
            "accepted_submission_count": len(self.records),
            "max_dev_rounds": self.max_dev_rounds,
            "accepted_candidate_digests": [record["candidate_digest"] for record in self.records],
            "feedback_chain_complete": True,
            "frozen_tree_read_only": True,
            "frozen_digest_before": actual,
            "frozen_digest_after": after,
            "frozen_digest_stable": actual == after,
            "readiness_profile": self.readiness_profile,
            "current_binding": self.current_binding,
            "builder_exit_evidence": builder_exit_evidence,
        }
        if self.readiness_profile:
            self.frozen.update(builder_session_id=final['builder_session_id'],
                delivery_candidate_digest=final['delivery_candidate_digest'],
                submission_sha256=final['submission_sha256'])
        _write_json(self.run_dir / "freeze_manifest.json", self.frozen)
        return self.frozen

    def run_hidden(self, case_id: str) -> dict[str, Any]:
        if self.frozen is None: raise ProtocolError("hidden execution is forbidden before freeze")
        if case_id not in self.hidden_case_ids: raise ProtocolError("unknown hidden case for this lifecycle")
        if self.hidden_root is None:
            raise ProtocolError("real hidden execution requires evaluator-owned hidden root")
        try:
            canonical_hidden_case(case_id, self.hidden_root)
        except ValueError as exc:
            raise ProtocolError(str(exc)) from exc
        if not self.broker_endpoint:
            raise ProtocolError("hidden execution requires evaluator-owned broker endpoint")
        if __package__ in {None, ""}:
            from hidden_runner import run
        else:
            from .hidden_runner import run
        output = self.run_dir / "hidden" / case_id
        result = run(
            frozen_manifest=self.run_dir / "freeze_manifest.json",
            hidden_root=self.hidden_root,
            case_id=case_id,
            output=output,
            broker_endpoint=self.broker_endpoint,
            python_executable=Path(self.runtime_python) if self.runtime_python else None,
            launcher=self.launcher,
        )
        return result

    def run_all_hidden(self) -> list[dict[str, Any]]:
        """Run exactly the lifecycle's evaluator-owned hidden inventory."""
        if self.frozen is None:
            raise ProtocolError("hidden execution is forbidden before freeze")
        if self.hidden_root is None:
            raise ProtocolError("real hidden execution requires evaluator-owned hidden root")
        results: list[dict[str, Any]] = []
        errors: list[dict[str, Any]] = []
        for case_id in self.hidden_case_ids:
            try:
                results.append(self.run_hidden(case_id))
            except Exception as exc:
                error = {
                    "case_id": case_id,
                    "classification": "evaluator_infrastructure_error",
                    "error_type": type(exc).__name__,
                    "error": str(exc),
                }
                errors.append(error)
                results.append(error)
        case_results = [
            item for item in results
            if isinstance(item, dict) and item.get("schema_version") == "deepcode-agentloop-hidden-run-v2"
        ]
        attestation = {
            "schema_version": "deepcode-agentloop-hidden-after-freeze-attestation/v2",
            "evidence_kind": self.evidence_kind,
            "formal_result_claimed": False,
            "result_or_score_claimed": False,
            "freeze_manifest": str((self.run_dir / "freeze_manifest.json").resolve()),
            "frozen_candidate_digest": self.frozen["candidate_digest"],
            "source_submission": self.frozen["source_submission"],
            "case_inventory": list(self.hidden_case_ids),
            "expected_cases": list(self.hidden_case_ids),
            "executed_cases": [item.get("case_id") for item in results],
            "hidden_started_after_freeze": len(case_results) == len(self.hidden_case_ids) and all(item.get("hidden_started_after_freeze") is True for item in case_results),
            "all_cases_started_after_freeze": len(case_results) == len(self.hidden_case_ids) and all(item.get("hidden_started_after_freeze") is True for item in case_results),
            "scheduled_records_used_as_results": False,
            "cases": results,
            "infrastructure_errors": errors,
            "all_six_dispatched": self.hidden_case_ids == tuple(HIDDEN_CASES) and len(results) == len(HIDDEN_CASES),
            "pilot_case_dispatched": self.evidence_kind == "pilot" and self.hidden_case_ids == ("test_001",) and len(results) == 1,
            "all_six_have_result_json": all(
                isinstance(item, dict) and Path(str(item.get("evidence", {}).get("result", ""))).is_file()
                for item in case_results
            ) and not errors,
            "frozen_digest_stable": all(
                item.get("frozen_digest_stable") is True
                for item in case_results
            ) and not errors,
            "canonical_case_only": all(
                item.get("candidate_visibility", {}).get("canonical_test_case_tree") is False
                for item in case_results
            ) and not errors,
            "case_result_count": len(case_results),
        }
        common_complete = bool(
            attestation["all_six_have_result_json"]
            and attestation["hidden_started_after_freeze"]
            and attestation["frozen_digest_stable"]
            and attestation["canonical_case_only"]
        )
        attestation["complete"] = bool(attestation["all_six_dispatched"] and common_complete)
        attestation["pilot_complete"] = bool(attestation["pilot_case_dispatched"] and common_complete)
        _write_json(self.run_dir / "hidden-after-freeze-attestation.json", attestation)
        return results


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(); parser.add_argument("--base-repository", type=Path, required=True); parser.add_argument("--candidate", type=Path, action="append", required=True); parser.add_argument("--max-dev-rounds", type=int, default=10); parser.add_argument("--run-dir", type=Path, required=True); parser.add_argument("--launcher", type=Path, default=Path(__file__).with_name("deepcode_lower_agent.py")); parser.add_argument("--broker-endpoint"); parser.add_argument("--python", dest="runtime_python"); parser.add_argument("--hidden-root", type=Path); parser.add_argument("--dry-run", action="store_true"); parser.add_argument("--run-hidden", action="append", default=[]); args = parser.parse_args(argv)
    if args.runtime_python:
        os.environ["DEEPCODE_PYTHON"] = args.runtime_python
    controller = CandidateController(base_repository=args.base_repository.resolve(), run_dir=args.run_dir.resolve(), launcher=args.launcher.resolve(), broker_endpoint=args.broker_endpoint, runtime_python=args.runtime_python, hidden_root=args.hidden_root, dry_run=args.dry_run, max_dev_rounds=args.max_dev_rounds)
    submissions = [controller.submit(candidate.resolve()) for candidate in args.candidate]
    frozen = controller.freeze()
    if args.run_hidden:
        # The CLI flag is a boolean trigger; the evaluator always owns and
        # dispatches the complete six-case hidden inventory after freeze.
        hidden = controller.run_all_hidden()
    else:
        hidden = []
    hidden_errors = [item for item in hidden if item.get("error_type")]
    summary = {
        "schema_version": "deepcode-agentloop-controller-v2",
        "status": "completed" if not hidden_errors else "completed_with_infrastructure_errors",
        "submissions": submissions,
        "freeze": frozen,
        "hidden": hidden,
        "hidden_infrastructure_errors": hidden_errors,
        "hidden_attestation": str((args.run_dir / "hidden-after-freeze-attestation.json").resolve()) if args.run_hidden else None,
    }
    _write_json(args.run_dir / "controller_summary.json", summary)
    print(json.dumps(summary, indent=2, ensure_ascii=False)); return 0


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


_fr_wrap_submit(CandidateController)
_FR_WRAPPED_FREEZE = [name for name in _FR_FREEZE_METHODS if _fr_wrap_freeze(CandidateController, name)]
assert _FR_WRAPPED_FREEZE, 'freeze/submit race guard found no freeze entry point'
# --- end 0921b freeze/submit race guard ------------------------------------


if __name__ == "__main__": raise SystemExit(main())
