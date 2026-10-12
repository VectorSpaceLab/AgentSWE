#!/usr/bin/env python3
"""Fail-closed OpenHands Agent-loop lifecycle controller.

The controller accepts already materialized OpenHands Agent Canvas products.
It runs both public cases for each accepted Candidate, returns fresh feedback,
freezes the latest accepted Candidate, and permits the six hidden cases only
after that immutable freeze.  Builder process/session ownership lives in
``harbor/formal_one_stop.py``; this module owns only product execution and
evidence.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import stat
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


if str(Path(__file__).resolve().parents[2]) not in sys.path:
    sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from harbor.public_feedback import public_payload

BUDGET_TRUNCATION = "evaluator_budget_truncation"
# D52 (policy D14, 2026-09-19): budget exhaustion is a Candidate outcome that is
# scored, never voided.  The string is deliberately "candidate_timeout" and not a
# new one: harbor/0905-edit-case-repair/execution_contract.py:13-17
# FATAL_CANDIDATE_CLASSES already contains it, so the shared contract scores such a
# case as a bound candidate_zero without any control-plane change.  BUDGET_TRUNCATION
# stays for a case the evaluator ended BEFORE the product could spend its budget
# (no successful lower call at all).
CANDIDATE_BUDGET_EXHAUSTED = "candidate_timeout"
DEV_CASES = ("dev_001", "dev_002")
HIDDEN_CASES = tuple(f"test_{number:03d}" for number in range(1, 7))
LIFECYCLE_SCHEMA = "agentswe-openhands-two-round-lifecycle/v2"
FEEDBACK_SCHEMA = "agentswe-openhands-feedback/v2"
FREEZE_SCHEMA = "agentswe-freeze-manifest/v1"
HIDDEN_ATTESTATION_SCHEMA = "agentswe-openhands-hidden-after-freeze-attestation/v1"
INFRASTRUCTURE_CLASSIFICATIONS = {
    "provider_failure",
    "broker_failure",
    "lower_agent_infrastructure_failure",
    # 0921: the evaluator's own case-budget guard cut this case short before the
    # Candidate could deliver an artifact.  The case was not measured, so it is
    # neither a Candidate zero nor a scoreable result; it is named here so a dev
    # round is not consumed by it and the hidden attestation reports it.
    BUDGET_TRUNCATION,
}
READINESS_PROFILE = "single-dev-two-round-hidden-smoke-v1"


def now() -> str:
    return datetime.now(timezone.utc).isoformat()


def tree_digest(root: Path) -> str:
    """Match the authoritative Create Code judge tree digest."""
    h = hashlib.sha256()
    for path in sorted(root.rglob("*"), key=lambda item: item.relative_to(root).as_posix()):
        relative = path.relative_to(root)
        name = relative.as_posix().encode()
        if path.is_symlink():
            kind, payload = b"L", os.readlink(path).encode()
        elif path.is_file():
            h.update(b"F"); h.update(len(name).to_bytes(8, "big")); h.update(name)
            h.update(path.stat().st_size.to_bytes(8, "big"))
            with path.open("rb") as handle:
                while chunk := handle.read(1024 * 1024): h.update(chunk)
            continue
        elif path.is_dir():
            continue
        else:
            kind, payload = b"O", b""
        h.update(kind); h.update(len(name).to_bytes(8, "big")); h.update(name)
        h.update(len(payload).to_bytes(8, "big")); h.update(payload)
    return h.hexdigest()


def write_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.tmp")
    temporary.write_text(json.dumps(value, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    os.replace(temporary, path)


def read_json(path: Path) -> dict[str, Any] | None:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    return value if isinstance(value, dict) else None


def runtime(stats: dict[str, Any] | None) -> dict[str, int]:
    source = (stats or {}).get("runtime", {})
    result={key:max(0,int(source.get(key,0) or 0)) for key in
        ('calls','failures','successful_calls','unknown_usage_calls','in_flight_calls','case_deadline_calls')}
    for key in ('input_tokens','output_tokens','total_tokens'):
        result[key]=None if result['unknown_usage_calls'] or source.get(key)==None else max(0,int(source.get(key,0)))
    return result


# --- D13 (2026-09-19) recovered upstream transport failures ---------------------------
# An upstream transport failure the lower agent recovered from -- it issued a new logical
# request and a later one in the same ledger succeeded -- is infrastructure noise, not a
# provider failure for this case. Same allowlist and exclusions as the shared admission
# normalizers (harbor/0905-edit-case-repair/v2_usage_normalizers.py, transport_error);
# duplicated here because the control plane is not importable at run time.
_D13_TRANSPORT_TOKENS = ("brokenpipe", "connectionreset", "connectionaborted", "connectionclosed",
                         "remotedisconnected", "serverdisconnected", "incompleteread",
                         "chunkedencoding", "ssleof", "prematureclose")
_D13_PROVIDER_SIDE_TOKENS = ("readtimeout", "readtimedout", "sockettimeout", "timeouterror",
                             "timedout", "connecttimeout", "connectionerror")
_D13_NON_TRANSPORT_TOKENS = ("credential", "apikey", "unauthor", "forbidden", "invalidrequest",
                             "protocolfailure", "schema", "casedeadline", "deadlineexceeded",
                             "maxoutputtokens", "brokerrestart", "notdispatched", "notsent", "cancel")
_D13_NON_TRANSPORT_TEXT = ("client:", "client_", "client failure", "client error", "clientfailure")


def _d13_has_5xx(text):
    groups = "".join(character if character.isdigit() else " " for character in text).split()
    return any(len(group) == 3 and group[0] == "5" for group in groups)


def _d13_transport_error(error):
    if not isinstance(error, str) or not error.strip():
        return False
    text = error.lower()
    squeezed = "".join(character for character in text if character.isalnum())
    if any(token in squeezed for token in _D13_NON_TRANSPORT_TOKENS):
        return False
    if any(token in text for token in _D13_NON_TRANSPORT_TEXT):
        return False
    if any(token in squeezed for token in _D13_TRANSPORT_TOKENS):
        return True
    provider_side = any(marker in squeezed for marker in ("provider", "upstream", "http"))
    if provider_side and any(token in squeezed for token in _D13_PROVIDER_SIDE_TOKENS):
        return True
    return bool(provider_side and _d13_has_5xx(text))


def _d13_rows(stats):
    """Ledger rows of either family: intent rows (`requests`) or attempts (`attempts`)."""
    if not isinstance(stats, dict):
        return []
    for key in ("requests", "attempts"):
        rows = stats.get(key)
        if isinstance(rows, list):
            return [row for row in rows if isinstance(row, dict)]
    return []


def _d13_row_ok(row):
    if "model_response_available" in row or "usage_unknown" in row:
        return (row.get("state") == "terminal" and row.get("model_response_available") is True
                and row.get("usage_unknown") is False)
    return (row.get("ok") is True and row.get("usage_state") == "known"
            and row.get("upstream_completion") == "completed")


def _d13_row_error(row):
    if row.get("failure_kind") == "client" or row.get("provider_outcome") == "not_dispatched":
        return None
    for key in ("error", "transport_abort_reason", "error_type", "failure_reason"):
        value = row.get(key)
        if isinstance(value, str) and value:
            return value
    return None


def _d13_row_attempts(row):
    for key in ("transport_attempts", "upstream_attempts"):
        if key in row:
            return row.get(key)
    return None


def _d13_identity(row):
    return row.get("request_sha256") or row.get("request_id")


def _d13_recovered_transport_calls(before, after):
    """Tolerated rows added between the snapshots; before=None counts the whole ledger."""
    rows = _d13_rows(after)
    seen = {_d13_identity(row) for row in _d13_rows(before)} if before is not None else set()
    count = 0
    for index, row in enumerate(rows):
        if _d13_identity(row) in seen or _d13_row_ok(row):
            continue
        if not _d13_transport_error(_d13_row_error(row)) or _d13_row_attempts(row) != 1:
            continue
        if any(_d13_row_ok(later) for later in rows[index + 1:]):
            count += 1
    return count
# --- end D13 --------------------------------------------------------------------------


def runtime_delta(before: dict[str, Any] | None, after: dict[str, Any] | None) -> dict[str, int] | None:
    if before is None or after is None:
        return None
    left, right = runtime(before), runtime(after)
    result={key:None if right[key] is None or left[key] is None else max(0,right[key]-left[key]) for key in left}
    # D13: tolerated transport failures are counted apart. unknown_usage_calls still reports
    # them, and the token sums stay complete only because a tolerated row contributes none.
    result['recovered_transport_calls']=_d13_recovered_transport_calls(before, after)
    if isinstance(before.get('logical_requests'),dict) and isinstance(after.get('logical_requests'),dict):
        rows=[row for key,row in after['logical_requests'].items() if key not in before['logical_requests']]
        unknown=sum(row.get('result',{}).get('usage_state')!='known' for row in rows)
        for key in ('input_tokens','output_tokens','total_tokens'):
            result[key]=None if unknown>result['recovered_transport_calls'] else sum(row.get('result',{}).get(key) or 0 for row in rows)
    return result


def classify_launch(
    *,
    exit_code: int,
    launcher: dict[str, Any] | None,
    artifact: dict[str, Any] | None,
    broker_delta: dict[str, int] | None = None,
) -> str:
    """Attribute provider/launcher failures separately from Candidate failures."""
    if launcher is None:
        return "lower_agent_infrastructure_failure"
    deadline_calls = int((broker_delta or {}).get("case_deadline_calls") or 0)
    # D13: a recovered upstream transport failure is infrastructure noise, not this round's
    # provider failure. The count is recorded in the delta this classification is read from.
    tolerated = deadline_calls + int((broker_delta or {}).get("recovered_transport_calls") or 0)
    if broker_delta is not None and int(broker_delta.get("unknown_usage_calls") or 0) > tolerated:
        return "provider_failure"
    # 0921: a request cut by the evaluator's own case deadline used to return
    # "candidate_product_failure" here, BEFORE the artifact was even looked at, so a
    # case that delivered a valid artifact and lost one call to the deadline was
    # booked as "Candidate product produced no valid model-authored agent artifact"
    # at 0/100 (0920-fh-003 round 4 dev_001).  The rule is redundant when there is no
    # usable artifact -- the artifact checks below return the same class, and the
    # controller then re-attributes it to the evaluator's budget guard -- and wrong
    # when there is one.  Deadline calls stay tolerated above so they are still never
    # read as a provider failure.
    text = f"{launcher.get('stderr_tail', '')} {launcher.get('stdout_tail', '')}".lower()
    launcher_class = str(launcher.get("classification", ""))
    if any(
        marker in text
        for marker in ("broker status", "broker_upstream_failure", "agent_server_provider_failure", "provider failure")
    ):
        return "provider_failure"
    if launcher_class in {"provider_failure", "broker_failure"}:
        return launcher_class
    if launcher_class in {"lower_agent_infrastructure_failure", "infrastructure-invalid"}:
        return "lower_agent_infrastructure_failure"
    if artifact is None:
        return "candidate_product_failure"
    required = {
        "schema_version", "case_id", "model", "reasoning_effort",
        "actions", "observations", "decision", "rationale",
    }
    if not required.issubset(artifact) or artifact.get("schema_version") != "agentswe-openhands-agent-result/v1":
        return "candidate_product_failure"
    if artifact.get("model") != "deepseek-flash" or artifact.get("reasoning_effort") != "high":
        return "candidate_product_failure"
    if artifact.get("decision") not in {"complete", "partial", "blocked", "conflict"}:
        return "candidate_product_failure"
    if broker_delta is not None and broker_delta.get("calls", 0) <= 0:
        return "candidate_product_failure"
    if exit_code != 0 or artifact.get("decision") != "complete":
        return "candidate_behavior_failure"
    return "valid_behavior"


ARTIFACT_SCHEMA = "agentswe-openhands-agent-result/v1"
ARTIFACT_REQUIRED = {"case_id", "model", "reasoning_effort", "actions", "observations", "decision", "rationale"}


def artifact_defect(artifact: dict[str, Any] | None) -> str | None:
    """Why this case has no usable final artifact, in words the Builder can act on.

    The Builder used to be told only "Candidate product produced no valid
    model-authored agent artifact", which is the conclusion, not the reason, and it
    spent two of its five rounds guessing at it (0920-fh-003 rounds 3 and 4).
    """
    if artifact is None:
        return "no final artifact was written for this case"
    if not isinstance(artifact, dict):
        return "the final artifact is not a JSON object"
    if artifact.get("schema_version") != ARTIFACT_SCHEMA:
        kind = artifact.get("kind")
        if isinstance(kind, str):
            return ("the final-artifact request was answered with an action-loop choice "
                    f"(kind={kind!r}), not with the agent artifact")
        return f"the final artifact carries no {ARTIFACT_SCHEMA} schema_version"
    missing = sorted(ARTIFACT_REQUIRED - set(artifact))
    if missing:
        return "the final artifact is missing required fields: " + ", ".join(missing)
    if artifact.get("decision") not in {"complete", "partial", "blocked", "conflict"}:
        return f"the final artifact decision {artifact.get('decision')!r} is not complete/partial/blocked/conflict"
    return None


def budget_truncation(output: Path, launcher: dict[str, Any] | None,
                      broker_delta: dict[str, int] | None) -> dict[str, Any] | None:
    """Evidence that the evaluator's own budget, not the product, ended this case.

    Every one of these records is written by the evaluator's lower runtime about its
    own guards.  A case that carries one and still has no usable artifact was not
    measured: it must not be reported to the Builder, or scored, as a Candidate zero.
    """
    stop = read_json(output / "budget_reserve_stop.json") or {}
    guard = read_json(output / "dispatch_guard.json") or {}
    reask = read_json(output / "artifact_reask.json") or {}
    deadline_calls = int((broker_delta or {}).get("case_deadline_calls") or 0)
    text = f"{(launcher or {}).get('stderr_tail', '')} {(launcher or {}).get('stdout_tail', '')}"
    reasons: list[str] = []
    if stop.get("reason") == "case_budget_reserve_reached":
        reasons.append(
            f"the evaluator stopped the action loop at step {stop.get('step')} of 12 with "
            f"{stop.get('remaining_seconds')}s of the case budget left, to keep the final-artifact reserve")
    if guard.get("refused") is True:
        reasons.append(f"the evaluator did not dispatch the final-artifact request ({guard.get('remaining_seconds')}s left)")
    if deadline_calls > 0:
        reasons.append(f"{deadline_calls} model request(s) were cut by the evaluator's case deadline")
    if "case_budget_reserve_reached" in text:
        reasons.append("the lower worker exited on the evaluator's case-budget guard")
    if reask.get("attempted") is True and reask.get("repaired") is not True:
        reasons.append("the final-artifact request was re-asked once in explicit artifact mode and was not answered with an artifact")
    if not reasons:
        return None
    return {"truncated": True, "reasons": reasons, "turns_completed": guard.get("turns_completed"),
            "action_loop_stop": stop or None, "dispatch_guard": guard or None,
            "artifact_reask": reask or None, "case_deadline_calls": deadline_calls}


MAX_REJECTION_TEXT = 300
MAX_REJECTIONS = 24


def observed_action_rejections(raw_trajectory: dict[str, Any]) -> list[str]:
    """Render the world's own `action_rejected` records as Builder-visible text.

    lower_agent/openhands_lower_agent.py:587 writes
    `{status: "action_rejected", ...safeError(error)}` whenever a model-selected
    product action throws, and openhands_lower_agent.py:694 refuses to bind a
    trajectory whose undispatched step has no such observation -- so this list is
    the evaluator's own record of the product throwing, not a model claim.

    Until 0921 nothing carried it into the Builder's feedback: in formal run
    0905-edit-codex-xhigh-0921-v4-001-openhands the product raised
    "Cannot read properties of undefined (reading 'backendId')" on every
    reconcile_production_workspace, and the Builder was told only that the agent
    never attempted reconcile.  Action name and exception text only; arguments
    and results are not exported, so no oracle expectation travels this way.
    """
    lines: list[str] = []
    for step in (raw_trajectory or {}).get("steps", []) or []:
        if not isinstance(step, dict):
            continue
        observation = step.get("observation")
        if not isinstance(observation, dict) or observation.get("status") != "action_rejected":
            continue
        code = observation.get("error_code")
        message = str(observation.get("message") or "no error text recorded")[:MAX_REJECTION_TEXT]
        lines.append("step %s: product action %s was rejected: %s%s" % (
            step.get("step"), step.get("action"),
            (str(code) + ": ") if code else "", message))
        if len(lines) >= MAX_REJECTIONS:
            break
    return lines


def harness_error_lines(result: dict[str, Any]) -> list[str]:
    """Create-style `harness_error` lines: harness, validator and scorer faults.

    Create's dev feedback repeats each harness_error verbatim
    (create75 evaluator/adapters/web-research-report/one_stop.py feedback_text).
    These are evaluator statements about the evaluator's own guards and about
    the Candidate product's observed faults; they name no hidden case and quote
    no oracle value.
    """
    lines: list[str] = []
    defect = result.get("artifact_defect")
    if defect:
        lines.append("final-artifact defect: " + str(defect)[:MAX_REJECTION_TEXT])
    attribution = result.get("failure_attribution")
    if isinstance(attribution, dict) and attribution.get("reason"):
        lines.append("evaluator attribution (%s): %s" % (
            attribution.get("party"), str(attribution["reason"])[:MAX_REJECTION_TEXT]))
    evaluation = result.get("result_evaluation")
    if isinstance(evaluation, dict) and evaluation.get("contract_valid") is not True and evaluation.get("reason"):
        lines.append("evaluator scoring: " + str(evaluation["reason"])[:MAX_REJECTION_TEXT])
    launcher = result.get("launcher")
    if isinstance(launcher, dict):
        tail = str(launcher.get("stderr_tail") or "").strip()
        if tail:
            lines.append("lower launcher stderr tail: " + tail[-MAX_REJECTION_TEXT:])
    return lines


def case_reason(result: dict[str, Any]) -> str:
    """One sentence naming why a case ended as it did, for the Builder's feedback."""
    truncation = result.get("budget_truncation")
    defect = result.get("artifact_defect")
    if result.get("classification") in (BUDGET_TRUNCATION, CANDIDATE_BUDGET_EXHAUSTED) and truncation:
        return ("Evaluator-side, not the product: " + "; ".join(truncation["reasons"])
                + (f". {defect}." if defect else "."))
    assessment = ((result.get("result_evaluation") or {}).get("feedback") or {}).get("assessment")
    if defect:
        return defect + (f". {assessment}" if assessment else "")
    if assessment:
        return assessment
    return str(result.get("classification"))


def failure_attribution(classification: str) -> dict[str, Any]:
    if classification == "provider_failure":
        return {"party": "provider_or_evaluator_broker", "infrastructure_invalid": True, "candidate_result_valid": False}
    if classification in {"broker_failure", "lower_agent_infrastructure_failure"}:
        return {"party": "evaluator_infrastructure", "infrastructure_invalid": True, "candidate_result_valid": False}
    if classification == CANDIDATE_BUDGET_EXHAUSTED:
        # D52/D14: the case spent its entire budget with the product observably
        # running (at least one successful lower call).  party "candidate" is what
        # execution_contract.py:86 requires for the fatal-Candidate gate, so this
        # case is scored as a bound candidate_zero instead of being discarded; the
        # evaluator's guards only decided WHICH of its own deadlines fired first.
        return {"party": "candidate", "product_party": "candidate_behavior",
                "infrastructure_invalid": False, "candidate_result_valid": True}
    if classification == BUDGET_TRUNCATION:
        # party "evaluator" is what the shared execution contract reads as an
        # evaluator-side invalid case (execution_contract.infrastructure_reason); it
        # can therefore never become a candidate_zero 0/100.
        return {"party": "evaluator", "infrastructure_invalid": True, "candidate_result_valid": False}
    if classification == "candidate_product_failure":
        return {"party": "candidate_product", "infrastructure_invalid": False, "candidate_result_valid": True}
    if classification == "candidate_behavior_failure":
        return {"party": "candidate_behavior", "infrastructure_invalid": False, "candidate_result_valid": True}
    return {"party": "candidate_behavior", "infrastructure_invalid": False, "candidate_result_valid": True}


def make_tree_read_only(root: Path) -> None:
    for path in sorted(root.rglob("*"), key=lambda item: len(item.parts), reverse=True):
        if path.is_symlink():
            continue
        path.chmod(stat.S_IMODE(path.stat().st_mode) & ~0o222)
    root.chmod(stat.S_IMODE(root.stat().st_mode) & ~0o222)


from adapters.source_identity import source_identity
from evaluator.controller.product_intents import ProductIntents


class TwoRoundController:
    """Run up to ten accepted public rounds and six hidden cases after freeze."""

    def __init__(
        self,
        workspace: Path,
        run_dir: Path,
        runner: list[str],
        *,
        case_root: Path,
        broker_endpoint: str,
        node_modules: Path,
        stats_file: Path | None = None,
        timeout: int = 600,
        public_cases: tuple[str, ...] = DEV_CASES,
        hidden_cases: tuple[str, ...] = HIDDEN_CASES,
        public_max_retries: int = 2,
        pilot_not_formal: bool = False,
        max_dev_rounds: int = 10,
        n_concurrent: int = 1,
        result_judge_endpoint: str | None = None,
        readiness_profile: str | None = None,
        current_binding: dict[str, Any] | None = None,
    ) -> None:
        self.workspace = workspace.resolve()
        self.run_dir = run_dir.resolve()
        self.runner = list(runner)
        self.case_root = case_root.resolve()
        self.broker_endpoint = broker_endpoint
        self.result_judge_endpoint = result_judge_endpoint
        self.node_modules = node_modules.resolve()
        self.stats_file = stats_file.resolve() if stats_file else None
        self.timeout = timeout
        if readiness_profile is not None and readiness_profile != READINESS_PROFILE:
            raise ValueError("unsupported readiness profile")
        if readiness_profile is not None:
            public_cases = ("dev_001",)
            hidden_cases = ("test_001",)
            max_dev_rounds = 2
            pilot_not_formal = True
            if not isinstance(current_binding, dict) or current_binding.get("task") != "openhands":
                raise ValueError("readiness profile requires current OpenHands binding")
        if not public_cases or any(case not in DEV_CASES for case in public_cases):
            raise ValueError("public_cases must be a non-empty canonical subset")
        if not hidden_cases or any(case not in HIDDEN_CASES for case in hidden_cases):
            raise ValueError("hidden_cases must be a non-empty canonical subset")
        if (tuple(public_cases) != DEV_CASES or tuple(hidden_cases) != HIDDEN_CASES) and not pilot_not_formal:
            raise ValueError("reduced inventories require pilot_not_formal")
        if public_max_retries < 0:
            raise ValueError("public_max_retries must be non-negative")
        if not 1 <= max_dev_rounds <= 10:
            raise ValueError("max_dev_rounds must be in 1..10")
        if n_concurrent != 1:
            raise ValueError("n_concurrent must equal 1")
        self.public_cases = tuple(public_cases)
        self.hidden_cases = tuple(hidden_cases)
        self.public_max_retries = public_max_retries
        self.pilot_not_formal = pilot_not_formal
        self.max_dev_rounds = max_dev_rounds
        self.n_concurrent = n_concurrent
        self.readiness_profile = readiness_profile
        self.current_binding = current_binding
        self.infrastructure_attempts: list[dict[str, Any]] = []
        self.records: list[dict[str, Any]] = []
        self.hidden: list[dict[str, Any]] = []
        self.frozen = False
        self.phase = "created"
        self.freeze_manifest: dict[str, Any] | None = None
        self.run_dir.mkdir(parents=True, exist_ok=True)
        self.product_intents = ProductIntents(self.run_dir, context={"public_cases": list(self.public_cases), "max_dev_rounds": self.max_dev_rounds})

    @property
    def state_file(self) -> Path:
        return self.run_dir / "lifecycle_state.json"

    def _persist(self) -> None:
        value = {
            "schema_version": LIFECYCLE_SCHEMA,
            "phase": self.phase,
            "candidate_submissions": len(self.records),
            "accepted_submissions": len(self.records),
            "max_dev_rounds": self.max_dev_rounds,
            "n_concurrent": self.n_concurrent,
            "dev_passed_is_automatic_freeze": False,
            "dev_cases": list(self.public_cases),
            "hidden_cases": list(self.hidden_cases),
            "pilot_not_formal": self.pilot_not_formal,
            "readiness_profile": self.readiness_profile,
            "current_binding": self.current_binding,
            "frozen": self.frozen,
            "records": self.records,
            "infrastructure_attempts": self.infrastructure_attempts,
            "hidden": self.hidden,
        }
        write_json(self.state_file, value)
        write_json(self.run_dir / "dev_lifecycle.json", value)

    def _run_case(self, candidate: Path, case_name: str, output: Path) -> dict[str, Any]:
        if case_name not in self.public_cases and case_name not in self.hidden_cases:
            raise ValueError(f"unknown case: {case_name}")
        case_group = "dev_cases" if case_name in self.public_cases else "test_cases"
        output.mkdir(parents=True, exist_ok=True)
        command = self.runner + [
            "--repository", str(candidate),
            "--case", str(self.case_root / case_group / case_name),
            "--broker-endpoint", self.broker_endpoint,
            "--node-modules", str(self.node_modules),
            "--output", str(output),
            "--timeout", str(self.timeout),
        ]
        before_stats = read_json(self.stats_file) if self.stats_file else None
        started_at, started_ns = now(), time.time_ns()
        try:
            process = subprocess.run(command, text=True, capture_output=True, timeout=self.timeout + 30, check=False)
            exit_code = process.returncode
            stdout, stderr = process.stdout, process.stderr
        except subprocess.TimeoutExpired as exc:
            exit_code = 124
            stdout = str(exc.stdout or "")
            stderr = str(exc.stderr or "") + "\ncontroller timeout"
        ended_at, ended_ns = now(), time.time_ns()
        write_json(output / "controller_execution.json", {
            "case_id": case_name,
            "command": [Path(item).name if item.startswith("/") else item for item in command],
            "started_at": started_at,
            "ended_at": ended_at,
            "duration_seconds": round((ended_ns - started_ns) / 1_000_000_000, 3),
            "exit_code": exit_code,
            "stdout_tail": stdout[-2000:].replace(str(candidate), "<candidate>").replace(str(output), "<case-output>"),
            "stderr_tail": stderr[-2000:].replace(str(candidate), "<candidate>").replace(str(output), "<case-output>"),
        })
        launcher = read_json(output / "launcher_result.json")
        artifact = read_json(output / "agent_result.json")
        after_stats = read_json(self.stats_file) if self.stats_file else None
        delta = runtime_delta(before_stats, after_stats)
        classification = classify_launch(
            exit_code=exit_code, launcher=launcher, artifact=artifact, broker_delta=delta,
        )
        if self.stats_file is not None and (before_stats is None or after_stats is None):
            classification = "lower_agent_infrastructure_failure"
        # 0921.  No usable artifact AND the evaluator's own budget guard fired: the
        # case ended on our clock, not on the product's behaviour, so it is attributed
        # to the evaluator instead of booking the Candidate a fatal zero.
        defect = artifact_defect(artifact)
        truncation = budget_truncation(output, launcher, delta) if defect else None
        if classification == "candidate_product_failure" and truncation:
            # D52: separate "our clock ended a case that never ran" (evaluator,
            # voided) from "our clock ended a case that used all of its budget"
            # (Candidate, scored).  candidate_broker.py:102 already names the
            # deadline-cut calls; a case with successful calls behind them spent
            # the budget it was given.
            classification = (CANDIDATE_BUDGET_EXHAUSTED
                              if int((delta or {}).get("successful_calls") or 0) > 0
                              else BUDGET_TRUNCATION)
        safe_launcher = None
        if launcher is not None:
            safe_launcher = {
                key: launcher.get(key)
                for key in (
                    "schema_version", "case_id", "candidate_product", "candidate_source_digest",
                    "exit_code", "stdout_tail", "stderr_tail", "broker_endpoint_is_evaluator_owned",
                    "credential_seen_by_candidate", "classification", "model_protocol", "product_attestation",
                )
                if key in launcher
            }
        result: dict[str, Any] = {
            "case_id": case_name,
            # The shared formal finalizer binds every execution record to the frozen
            # Candidate by this key (readiness used to add it later in prepare_case_evidence).
            "candidate_digest": tree_digest(candidate),
            "started_at": started_at,
            "started_epoch_ns": started_ns,
            "ended_at": ended_at,
            "ended_epoch_ns": ended_ns,
            "exit_code": exit_code,
            "classification": classification,
            "failure_attribution": failure_attribution(classification),
            "artifact_present": artifact is not None,
            "artifact_defect": defect,
            "budget_truncation": truncation,
            "launcher": safe_launcher,
        }
        raw_trajectory = read_json(output / "trajectory.json") or {}
        result["real_execution"] = any(step.get("attempted") is True for step in raw_trajectory.get("steps", []))
        # 0921: the world already recorded every rejected product action; keep it
        # on the record so the Builder's feedback can repeat it (Create parity).
        result["action_rejections"] = observed_action_rejections(raw_trajectory)
        result["action_rejection_count"] = len(result["action_rejections"])
        # Shared execution contract prerequisites (execution_contract.classify_candidate_execution):
        # an attempted execution, an evaluator-validated preflight and, for a Candidate
        # product failure, an explicit evidence-backed fatal gate. Without this shape the
        # shared contract left such rounds "unresolved" and the controller recorded them
        # as infrastructure-invalid, ending the run (0919-ds-005 round 2).
        preflight = read_json(output / "environment_preflight.json") or {}
        result["execution_attempted"] = launcher is not None
        result["environment_preflight"] = {
            "valid": preflight.get("valid") is True and preflight.get("actual_product_server_ready") is True,
            "validated_by": "evaluator", "source": str(output / "environment_preflight.json")}
        if classification == CANDIDATE_BUDGET_EXHAUSTED:
            # D52: the shared fatal-Candidate gate (execution_contract.py:85-97) needs
            # execution_attempted, a healthy preflight, fatal=True, a reason and a
            # NON-EMPTY evidence path list whose files it can hash.  The budget-guard
            # records are preferred; the launcher result and the trajectory are the
            # fallback so the list is never empty (an empty list is "unresolved").
            evidence_paths = [str(path) for path in (output / "budget_reserve_stop.json",
                                                     output / "dispatch_guard.json",
                                                     output / "artifact_reask.json",
                                                     output / "launcher_result.json",
                                                     output / "trajectory.json") if path.is_file()]
            result["failure_attribution"] = {
                **result["failure_attribution"], "observed_by": "evaluator", "fatal": True,
                "reason": "the Candidate product ran and the case spent its entire budget "
                          "without a final artifact: " + "; ".join(truncation["reasons"])
                          + (f" ({defect})" if defect else ""),
                "evidence_paths": evidence_paths}
            result["case_budget_exhausted"] = True
        if classification == BUDGET_TRUNCATION:
            result["failure_attribution"] = {
                **result["failure_attribution"], "observed_by": "evaluator", "fatal": False,
                "reason": "the evaluator's case-budget guard ended this case before a final artifact: "
                          + "; ".join(truncation["reasons"]) + (f" ({defect})" if defect else ""),
                "evidence_paths": [str(path) for path in (output / "budget_reserve_stop.json",
                                                          output / "dispatch_guard.json",
                                                          output / "artifact_reask.json") if path.is_file()]}
        if classification == "candidate_product_failure":
            evidence_paths = [str(path) for path in (output / "launcher_result.json", output / "trajectory.json",
                                                     output / "product_runtime.log") if path.is_file()]
            result["failure_attribution"] = {
                "party": "candidate", "product_party": "candidate_product", "observed_by": "evaluator", "fatal": True,
                "reason": "Candidate product produced no valid model-authored agent artifact for this case"
                          + (f": {defect}" if defect else ""),
                "evidence_paths": evidence_paths, "infrastructure_invalid": False, "candidate_result_valid": True}
        if artifact is not None:
            result["agent_result"] = artifact
            artifact_path = output / "agent_result.json"
            result["artifact_path"] = str(artifact_path)
            result["artifact_sha256"] = hashlib.sha256(artifact_path.read_bytes()).hexdigest()
            result["agent_authored_artifact"] = True
            result["artifact_provenance"] = {
                "artifact_path": str(artifact_path),
                "artifact_owner": "lower_agent_product",
                "producer_entry": "OpenHands lower product action loop",
                "evaluator_synthesized": False,
                "preexisting_before_launch": False,
                "trajectory_artifact_reference": True,
                "sha256": result["artifact_sha256"],
            }
        if after_stats is not None:
            result["broker"] = runtime(after_stats)
        if delta is not None:
            result["broker_delta"] = delta
        write_json(output / "case_result.json", result)
        if case_name in self.public_cases:
            try:
                sys.path.insert(0, str(self.case_root / "evaluator"))
                from case_evidence import score_case
                evaluation = score_case(case_id=case_name, record_path=output / "case_result.json",
                                        candidate=candidate, candidate_digest=tree_digest(candidate),
                                        output=output / "semantic_result", broker_endpoint=self.result_judge_endpoint)
            except Exception as exc:
                evaluation = {"classification": "unresolved", "score": None, "contract_valid": False,
                              "round_consumed": False, "reason": f"evaluator evidence/scoring: {type(exc).__name__}"}
            result["result_evaluation"] = evaluation
            write_json(output / "controller_scored_observation.json", result)
        return result

    def _run_public_case(self, candidate: Path, case_name: str, output: Path, *, max_retries: int = 2) -> dict[str, Any]:
        """One public execution only; preserve completed and unknown requests.

        Retrying an infrastructure classification rebuilt a dynamic world under
        a different directory and could resample already completed/unknown
        actions. An operator must inspect that immutable attempt instead.
        """
        return self._run_case(candidate, case_name, output)


    def submit(self, candidate: Path | None = None, *, feedback_digest_ack: str | None = None, product_source_identity: str | None = None, builder_session_id: str | None = None) -> dict[str, Any]:
        source = (candidate or self.workspace).resolve()
        if not source.is_dir():
            raise RuntimeError(f"candidate product missing: {source}")
        identity = product_source_identity or source_identity(source)
        if not isinstance(identity, str) or len(identity) != 64 or any(c not in '0123456789abcdef' for c in identity):
            raise ValueError('Invalid stable product source identity')
        previous = self.product_intents.entries.get(identity)
        if previous is not None:
            original = next((r for r in self.records if r.get('product_source_identity') == identity), None)
            if previous['state'] == 'completed' and original is not None:
                return {**original, 'duplicate_digest': True, 'round_consumed': False}
            if previous.get('result'):
                return previous['result']
            return {'accepted': False, 'round_consumed': False, 'infrastructure_blocked': True,
                'product_source_identity': identity, 'resampling_forbidden': True,
                'dev': {case: row.get('result') for case, row in previous['cases'].items()},
                'classification': 'lower_agent_infrastructure_failure', 'error': 'Product evaluation outcome unknown'}
        if self.frozen:
            raise RuntimeError("fail-closed: submission after freeze")
        if len(self.records) >= self.max_dev_rounds:
            raise RuntimeError("fail-closed: max_dev_rounds reached")
        number = len(self.records) + 1
        source_digest = tree_digest(source)
        expected_feedback = self.records[-1].get("feedback_digest") if self.records else None
        if number == 1 and feedback_digest_ack not in {None, ""}:
            raise RuntimeError("fail-closed: first submission cannot acknowledge feedback")
        if number > 1 and feedback_digest_ack != expected_feedback:
            raise RuntimeError("fail-closed: revision must acknowledge the immediately prior feedback digest")
        duplicate = next((record for record in self.records if record.get("candidate_digest") == source_digest), None)
        if duplicate is not None:
            return {**duplicate, "accepted": True, "duplicate_digest": True, "round_consumed": False}
        serial = len(self.product_intents.entries) + 1
        snapshot_name = f"candidate_{number:03d}"
        round_name = f"round_{number:03d}"
        if (self.run_dir / snapshot_name).exists():
            snapshot_name += f"_attempt_{serial:03d}"
            round_name += f"_attempt_{serial:03d}"
        snapshot = self.run_dir / snapshot_name
        if snapshot.exists():
            raise RuntimeError(f"fail-closed: Candidate snapshot already exists: {snapshot}")
        self.product_intents.reserve(identity, execution_digest=source_digest, candidate_root=snapshot_name,
            round_root=round_name, submission=number, feedback_digest_ack=feedback_digest_ack)
        shutil.copytree(source, snapshot, symlinks=True)
        digest = tree_digest(snapshot)
        if digest != source_digest:
            raise RuntimeError("fail-closed: Candidate snapshot digest mismatch")
        self.phase = f"candidate_{number:03d}_dev"
        self._persist()
        dev_results = {}
        for case in self.public_cases:
            self.product_intents.case_started(identity, case)
            try:
                result = self._run_public_case(snapshot, case, self.run_dir / round_name / case,
                                              max_retries=0)
            except Exception as exc:
                self.product_intents.entries[identity]['cases'][case]['error_type'] = type(exc).__name__
                self.product_intents.persist()
                raise
            dev_results[case] = result
            self.product_intents.case_finished(identity, case, result)
        infrastructure_failures = {
            case: result["classification"]
            for case, result in dev_results.items()
            if result["classification"] in INFRASTRUCTURE_CLASSIFICATIONS
            or not result.get("result_evaluation", {}).get("contract_valid")
            or not result.get("result_evaluation", {}).get("round_consumed")
        }
        # 0921: a blocked round returns before any feedback file is written, so the
        # reason has to travel on the record itself or the Builder sees only the word
        # "blocked" and revises a product that was never the problem.
        infrastructure_case_reasons = {
            case: case_reason(dev_results[case]) for case in infrastructure_failures
        }
        record: dict[str, Any] = {
            "submission": number,
            # Stamped here because freeze_latest, which runs before this call
            # returns, compares it across records; the caller's own update
            # happens afterwards and left the newest record at None.
            "builder_session_id": builder_session_id,
            "candidate_digest": digest,
            "candidate_root": snapshot_name,
            "product_source_identity": identity,
            "accepted_at": now(),
            "feedback_digest_ack": feedback_digest_ack,
            "dev": dev_results,
            "dev_inventory_complete": set(dev_results) == set(self.public_cases),
            "feedback": None,
            "infrastructure_blocked": bool(infrastructure_failures),
            "infrastructure_failures": infrastructure_failures,
        }
        record["infrastructure_case_reasons"] = infrastructure_case_reasons
        if record["infrastructure_blocked"]:
            record.update({"accepted": False, "round_consumed": False, "attempted_at": now()})
            self.infrastructure_attempts.append(record)
            self.phase = "public_infrastructure_invalid"
            self._persist()
            self.product_intents.finish(identity, 'unknown', record)
            return record
        dev_scores = {
            case: value["result_evaluation"]["score"]
            for case, value in dev_results.items()
        }
        record.update({
            "dev_scores": dev_scores,
            "dev_mean": sum(dev_scores.values()) / len(dev_scores),
            "accepted": True,
            "round_consumed": True,
        })
        record["dev_passed"] = bool(record["dev_inventory_complete"] and record["dev_mean"] > 60)
        self.phase = "feedback_ready"
        feedback = {
            "schema_version": FEEDBACK_SCHEMA,
            "candidate": number,
            "candidate_digest": digest,
            "generated_at": now(),
            "authoritative": True,
            "infrastructure_blocked": False,
            "dev_scores": dev_scores,
            "dev_mean": record["dev_mean"],
            "dev_passed": record["dev_passed"],
            "dev": {
                case: {
                    "classification": value["classification"],
                    "failure_attribution": value["failure_attribution"],
                    "artifact_present": value["artifact_present"],
                    # 0921: why this case scored what it scored, per case, always.
                    "reason": case_reason(value),
                    "artifact_defect": value.get("artifact_defect"),
                    "budget_truncation": value.get("budget_truncation"),
                    "decision": (value.get("agent_result") or {}).get("decision"),
                    "broker_delta": value.get("broker_delta", {}),
                    # 0921: what the product actually threw, and what the harness
                    # and validator said, per dev case -- Create's harness_error lines.
                    "action_rejections": value.get("action_rejections") or [],
                    "action_rejection_count": value.get("action_rejection_count") or 0,
                    "harness_errors": harness_error_lines(value),
                    "semantic_feedback": value.get("result_evaluation", {}).get("feedback"),
                }
                for case, value in dev_results.items()
            },
        }
        feedback_path = self.run_dir / f"feedback_candidate_{number:03d}.json"
        write_json(feedback_path, public_payload(feedback))
        record["feedback"] = feedback_path.name
        record["feedback_digest"] = hashlib.sha256(feedback_path.read_bytes()).hexdigest()
        if not post_freeze_superseded(self, record):
            self.records.append(record)
        self._persist()
        if number == self.max_dev_rounds:
            self.freeze(record, reason="max_dev_rounds")
        self.product_intents.finish(identity, 'completed', record)
        return record

    def freeze(self, record: dict[str, Any], *, reason: str) -> dict[str, Any]:
        if reason not in {"max_dev_rounds", "builder_exit"}:
            raise RuntimeError("fail-closed: invalid freeze reason")
        if not self.records or record is not self.records[-1]:
            raise RuntimeError("fail-closed: freeze requires latest accepted Candidate")
        if self.readiness_profile is not None:
            if len(self.records) != 2:
                raise RuntimeError("readiness freeze requires exactly two accepted rounds")
            if len({item.get("candidate_digest") for item in self.records}) != 2:
                raise RuntimeError("readiness Candidates must have distinct digests")
            if any(item.get("feedback_digest") is None for item in self.records):
                raise RuntimeError("readiness freeze requires authoritative feedback")
            if self.records[1].get("feedback_digest_ack") != self.records[0].get("feedback_digest"):
                raise RuntimeError("readiness freeze requires exact first-round feedback digest")
            sessions = {item.get("builder_session_id") for item in self.records}
            if len(sessions) > 1:
                raise RuntimeError("readiness freeze requires one Builder session")
        number = int(record["submission"])
        source = self.run_dir / f"candidate_{number:03d}"
        frozen = self.run_dir / "frozen_candidate"
        if frozen.exists():
            raise RuntimeError("fail-closed: frozen Candidate already exists")
        shutil.copytree(source, frozen, symlinks=True)
        expected_digest = tree_digest(source)
        make_tree_read_only(frozen)
        frozen_digest = tree_digest(frozen)
        if frozen_digest != expected_digest:
            raise RuntimeError("fail-closed: frozen Candidate digest mismatch")
        freeze_ns = time.time_ns()
        # Keep the public receipt compatible with the existing schema. Extended
        # provenance is written to a separate attestation, so older evaluators
        # cannot silently reinterpret a richer receipt.
        manifest = {
            "schema_version": "agentswe-freeze-manifest/v1",
            "candidate_digest": frozen_digest,
            "source_submission": number,
            "source_submission_id": f"candidate-{number:03d}",
            "accepted_submission_count": len(self.records),
            "accepted_candidate_digests": [item["candidate_digest"] for item in self.records],
            "candidate_delivery_digest": record.get("candidate_digest"),
            "candidate_materialized_digest": frozen_digest,
            "frozen_candidate_path": str(frozen),
            "candidate_path": str(frozen),
            "frozen_at": now(),
            "freeze_reason": reason,
            "max_dev_rounds": self.max_dev_rounds,
            "n_concurrent": self.n_concurrent,
            "dev_passed_is_automatic_freeze": False,
            "feedback_digest": record.get("feedback_digest"),
            "feedback_consumed": bool(self.records) and all(
                isinstance(item.get("feedback_digest"), str)
                and (index == 0 or item.get("feedback_digest_ack") == self.records[index - 1].get("feedback_digest"))
                for index, item in enumerate(self.records)
            ),
            "feedback_chain_complete": bool(self.records) and all(
                isinstance(item.get("feedback_digest"), str)
                and (index == 0 or item.get("feedback_digest_ack") == self.records[index - 1].get("feedback_digest"))
                for index, item in enumerate(self.records)
            ),
            "dev_evaluated": True,
            "credential_mounted_to_candidate": False,
            "frozen_tree_read_only": True,
            "frozen_tree_regular": True,
            "frozen_at_unix": time.time(),
            "hidden_allowed": True,
            "hidden_case_inventory": list(self.hidden_cases),
            "pilot_not_formal": self.pilot_not_formal,
            "readiness_profile": self.readiness_profile,
            "current_binding": self.current_binding,
        }
        freeze_attestation = {
            "schema_version": "agentswe-openhands-freeze-attestation/v1",
            "freeze_manifest": "freeze_manifest.json",
            "accepted_candidate_digests": [item["candidate_digest"] for item in self.records],
            "digests_distinct": len({item["candidate_digest"] for item in self.records}) == len(self.records),
            "candidate_digest": frozen_digest,
            "frozen_epoch_ns": freeze_ns,
            "frozen_tree_read_only": True,
        }
        manifest_path = self.run_dir / "freeze_manifest.json"
        write_json(manifest_path, manifest)
        manifest_path.chmod(stat.S_IMODE(manifest_path.stat().st_mode) & ~0o222)
        write_json(self.run_dir / "freeze_attestation.json", freeze_attestation)
        self.freeze_manifest = {**freeze_attestation, **manifest}
        self.frozen = True
        self.phase = "frozen"
        self._persist()
        return manifest

    def freeze_latest(self, reason: str = "builder_exit") -> dict[str, Any]:
        if self.frozen and self.freeze_manifest is not None:
            return self.freeze_manifest
        if not self.records:
            raise RuntimeError("fail-closed: no structurally valid accepted Candidate to freeze")
        return self.freeze(self.records[-1], reason=reason)

    def run_hidden(self, case: str) -> dict[str, Any]:
        if case not in self.hidden_cases:
            raise ValueError(f"hidden case must be one of {self.hidden_cases}")
        if any(item.get("case_id") == case for item in self.hidden):
            raise RuntimeError(f"fail-closed: hidden case already executed: {case}")
        if not self.frozen or self.freeze_manifest is None or self.phase not in {"frozen", "hidden_in_progress"}:
            raise RuntimeError("fail-closed: hidden execution before accepted Candidate freeze")
        frozen = self.run_dir / "frozen_candidate"
        expected = str(self.freeze_manifest["candidate_digest"])
        before = tree_digest(frozen)
        if before != expected:
            raise RuntimeError(f"fail-closed: frozen digest changed before {case}")
        result = self._run_case(frozen, case, self.run_dir / "hidden" / case)
        after = tree_digest(frozen)
        if after != expected:
            raise RuntimeError(f"fail-closed: frozen digest changed during {case}")
        result["freeze_proof"] = {
            "freeze_epoch_ns": self.freeze_manifest["frozen_epoch_ns"],
            "started_strictly_after_freeze": result["started_epoch_ns"] > self.freeze_manifest["frozen_epoch_ns"],
            "digest_before": before,
            "digest_after": after,
            "digest_stable": before == after == expected,
        }
        write_json(self.run_dir / "hidden" / case / "case_result.json", result)
        self.hidden.append(result)
        self.phase = "hidden_complete" if len(self.hidden) == len(self.hidden_cases) else "hidden_in_progress"
        self._persist()
        return result

    def run_all_hidden(self) -> list[dict[str, Any]]:
        results = [self.run_hidden(case) for case in self.hidden_cases]
        manifest = self.freeze_manifest or {}
        expected = str(manifest.get("candidate_digest", ""))
        attestation = {
            "schema_version": HIDDEN_ATTESTATION_SCHEMA,
            "created_at": now(),
            "freeze_manifest": "freeze_manifest.json",
            "freeze_manifest_sha256": hashlib.sha256((self.run_dir / "freeze_manifest.json").read_bytes()).hexdigest(),
            "candidate_digest": expected,
            "expected_cases": list(self.hidden_cases),
            "executed_cases": [item["case_id"] for item in results],
            "strictly_after_freeze": all(item["freeze_proof"]["started_strictly_after_freeze"] for item in results),
            "frozen_digest_stable": tree_digest(self.run_dir / "frozen_candidate") == expected and all(
                item["freeze_proof"]["digest_stable"] for item in results
            ),
            "all_cases_materialized": all(
                (self.run_dir / "hidden" / case / "case_result.json").is_file() for case in self.hidden_cases
            ),
            "complete_inventory": [item["case_id"] for item in results] == list(self.hidden_cases),
            "all_cases_real": all(item.get("real_execution") is True for item in results),
            "all_cases_started_after_freeze": all(item["freeze_proof"]["started_strictly_after_freeze"] for item in results),
            "cases": [{"case_id": item["case_id"],
                       "result_path": str(self.run_dir / "hidden" / item["case_id"] / "case_result.json"),
                       "result_sha256": hashlib.sha256((self.run_dir / "hidden" / item["case_id"] / "case_result.json").read_bytes()).hexdigest(),
                       "freeze_proof": item["freeze_proof"]} for item in results],
            "classifications": {item["case_id"]: item["classification"] for item in results},
            "infrastructure_failures": {
                item["case_id"]: item["classification"]
                for item in results if item["classification"] in INFRASTRUCTURE_CLASSIFICATIONS
            },
        }
        attestation["complete"] = bool(
            attestation["executed_cases"] == list(self.hidden_cases)
            and attestation["strictly_after_freeze"]
            and attestation["frozen_digest_stable"]
            and attestation["all_cases_materialized"]
        )
        attestation["pilot_not_formal"] = self.pilot_not_formal
        attestation["formal_result_publishable"] = False if self.pilot_not_formal else None
        name = "pilot-hidden-after-freeze-attestation.json" if self.pilot_not_formal else "hidden-after-freeze-attestation.json"
        write_json(self.run_dir / name, attestation)
        return results


def main() -> int:
    parser = argparse.ArgumentParser(description="Run pre-materialized OpenHands Candidates through the lifecycle")
    parser.add_argument("--workspace", type=Path, required=True)
    parser.add_argument("--candidate-2-workspace", type=Path)
    parser.add_argument("--run-dir", type=Path, required=True)
    parser.add_argument("--runner", nargs="+", required=True)
    parser.add_argument("--case-root", type=Path, required=True)
    parser.add_argument("--broker-endpoint", required=True)
    parser.add_argument("--node-modules", type=Path, required=True)
    parser.add_argument("--stats-file", type=Path)
    parser.add_argument("--timeout", type=int, default=600)
    parser.add_argument("--hidden", action="store_true")
    parser.add_argument("--max-dev-rounds", type=int, default=10)
    parser.add_argument("--n-concurrent", type=int, default=1)
    args = parser.parse_args()
    controller = TwoRoundController(
        args.workspace, args.run_dir, args.runner,
        case_root=args.case_root, broker_endpoint=args.broker_endpoint,
        node_modules=args.node_modules, stats_file=args.stats_file, timeout=args.timeout,
        max_dev_rounds=args.max_dev_rounds, n_concurrent=args.n_concurrent,
    )
    first = controller.submit()
    if first.get("infrastructure_blocked"):
        print(json.dumps({"status": "BLOCKED", "reason": "candidate_001_infrastructure", "records": controller.records}, indent=2))
        return 2
    submissions = [first]
    if args.candidate_2_workspace:
        second = controller.submit(args.candidate_2_workspace)
        submissions.append(second)
        if second.get("infrastructure_blocked"):
            print(json.dumps({"status": "BLOCKED", "reason": "candidate_infrastructure", "records": controller.records}, indent=2))
            return 2
    if args.hidden and not controller.frozen:
        controller.freeze_latest("builder_exit")
    hidden = controller.run_all_hidden() if args.hidden else []
    print(json.dumps({"status": controller.phase, "submissions": submissions, "records": controller.records, "hidden": hidden}, indent=2, ensure_ascii=False))
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


_fr_wrap_submit(TwoRoundController)
_FR_WRAPPED_FREEZE = [name for name in _FR_FREEZE_METHODS if _fr_wrap_freeze(TwoRoundController, name)]
assert _FR_WRAPPED_FREEZE, 'freeze/submit race guard found no freeze entry point'
# --- end 0921b freeze/submit race guard ------------------------------------


if __name__ == "__main__":
    raise SystemExit(main())
