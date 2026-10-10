#!/usr/bin/env python3
"""Fail-closed repeated public-feedback Candidate lifecycle controller.

Two arbitrary patch files are not an Agent-loop.  A non-self-test invocation
therefore requires an evaluator-attested Builder witness and a content-bound
feedback record.  This controller owns product execution and evidence; it
does not manufacture Builder provenance.
"""
from __future__ import annotations

import argparse
import json
import shutil
import stat
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

try:
    from agentloop.evaluator.builder_protocol import (
        load_and_verify_feedback,
        load_and_verify_witness,
        feedback_digest,
        verify_submission_binding,
    )
    from agentloop.evaluator.hidden_attestation import attest
    from agentloop.evaluator.hidden_executor import CASE_IDS, candidate_digest, run_hidden
    from agentloop.evaluator.materialize import materialize
    from agentloop.protocol import read_json, write_json
except ModuleNotFoundError:  # direct ``python agentloop/evaluator/controller.py`` entrypoint
    sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
    from agentloop.evaluator.builder_protocol import (  # type: ignore
        load_and_verify_feedback,
        load_and_verify_witness,
        feedback_digest,
        verify_submission_binding,
    )
    from agentloop.evaluator.hidden_attestation import attest  # type: ignore
    from agentloop.evaluator.hidden_executor import CASE_IDS, candidate_digest, run_hidden  # type: ignore
    from agentloop.evaluator.materialize import materialize  # type: ignore
    from agentloop.protocol import read_json, write_json  # type: ignore


from agentloop.evaluator.product_attempts import ProductAttempts, ProductReplayBlocked

DEV_CASES = ("dev_001", "dev_002")
INFRA_FAILURE_KINDS = {
    "broker_infrastructure_failure",
    "provider_infrastructure_failure",
    "credential_infrastructure_failure",
    "launcher_infrastructure_failure",
    "evaluator_infrastructure_failure",
    "mount_infrastructure_failure",
}


def _runtime(stats: dict[str, Any] | None) -> dict[str, int | None]:
    value = stats.get("runtime") if isinstance(stats, dict) else None
    if not isinstance(value, dict):
        return {"calls": 0, "failures": 0, "successful_calls": 0, "total_tokens": 0,
                "broker_failures": 0, "provider_failures": 0, "credential_failures": 0,
                "protocol_failures": 0}
    calls = int(value.get("calls", 0) or 0)
    failures = int(value.get("failures", 0) or 0)
    return {
        "calls": calls,
        "failures": failures,
        "successful_calls": int(value.get("successful_calls", calls - failures) or 0),
        "total_tokens": (None if value.get("usage_complete") is False or value.get("total_tokens", value.get("tokens")) is None
                         else int(value.get("total_tokens", value.get("tokens")))),
        "broker_failures": int(value.get("broker_failures", 0) or 0),
        "provider_failures": int(value.get("provider_failures", 0) or 0),
        "credential_failures": int(value.get("credential_failures", 0) or 0),
        "protocol_failures": int(value.get("protocol_failures", 0) or 0),
    }


def _delta(before: dict[str, Any] | None, after: dict[str, Any] | None) -> dict[str, int | None] | None:
    if not isinstance(before, dict) or not isinstance(after, dict):
        return None
    left, right = _runtime(before), _runtime(after)
    return {key: right[key] - left[key] if right[key] is not None and left[key] is not None else None for key in left}


def _read_only_tree(root: Path) -> None:
    for path in sorted(root.rglob("*"), key=lambda item: len(item.parts), reverse=True):
        if path.is_symlink():
            continue
        if not path.is_file() and not path.is_dir():
            raise RuntimeError(f"frozen Candidate contains a special file: {path}")
        path.chmod(stat.S_IMODE(path.stat().st_mode) & ~0o222)
    root.chmod(stat.S_IMODE(root.stat().st_mode) & ~0o222)


def _assert_regular_tree(root: Path) -> None:
    for path in root.rglob("*"):
        if path.is_symlink() or (not path.is_file() and not path.is_dir()):
            raise RuntimeError(f"Candidate tree is not regular-file-only: {path}")


# --- D52 (2026-09-21) the evaluator's own 600 s case scope ---------------------------
D52_DEADLINE_ENV = "AGENTSWE_CLAUDE_CASE_DEADLINE_MONOTONIC"


def _d52_successful_calls(endpoint):
    """This broker's successful-call count, or None when it cannot be read.

    Only the evaluator's own broker is consulted, and any problem returns None, so
    a case can only ever keep the verdict it had before D52.
    """
    try:
        from agentloop.evaluator.lower_agent_launcher import broker_stats
        value = broker_stats(endpoint)
    except Exception:
        return None
    if not isinstance(value, dict) or value.get("error"):
        return None
    runtime = value.get("runtime")
    source = runtime if isinstance(runtime, dict) else value
    count = source.get("successful_calls")
    return int(count) if isinstance(count, int) and not isinstance(count, bool) else None


def _d52_case_budget_record(*, case_id, digest, output, endpoint, before, exit_code):
    """A SCORED Candidate booking for a case the evaluator's own scope killed.

    policy D14 (2026-09-19): an exhausted case budget is a Candidate outcome that is
    scored, never voided.  claude was the only tree with no Candidate budget class at
    all -- a scope kill SIGKILLs the launcher before trajectory.json exists, so the
    case landed on "unresolved_execution_failure" and carried no score.

    "candidate_timeout" is used deliberately: it is already a member of
    harbor/0905-edit-case-repair/execution_contract.py FATAL_CANDIDATE_CLASSES, so the
    shared contract scores this record as a bound candidate_zero with no control-plane
    change.  Returns None on ANY unproven step; the caller then keeps the old record.
    """
    after = _d52_successful_calls(endpoint)
    if before is None or after is None or after <= before:
        return None
    lifecycle = output / "product_container_lifecycle.json"
    if not lifecycle.is_file():
        return None
    record = {
        "case_id": case_id, "candidate_digest": digest,
        "classification": "candidate_timeout", "classification_axis": "candidate",
        "real_execution": True, "execution_attempted": True,
        "infrastructure_invalid": False,
        "environment_preflight": {"valid": True, "validated_by": "evaluator",
                                  "source": str(lifecycle)},
        "failure_attribution": {
            "party": "candidate", "observed_by": "evaluator", "fatal": True,
            "reason": "the Candidate product ran, made " + str(after - before)
                      + " successful lower call(s) and exhausted the whole 600 second"
                        " case budget without a terminal result",
            "evidence_paths": [str(lifecycle)]},
        "case_budget_exhausted": True,
        "successful_lower_calls_delta": after - before,
        "outer_timeout": True, "exit_code": exit_code,
        "lifecycle_evidence": str(lifecycle),
    }
    try:
        from agentloop.evaluator.semantic_score import execution_verdict
        verdict, zero = execution_verdict(record, case_id, digest)
    except Exception:
        return None
    if zero is None or verdict.get("classification") != "candidate_zero":
        return None
    record.update({"score": 0, "score_kind": "candidate_zero", "round_consumed": True,
                   "result_judge_contract": zero, "execution_verdict": verdict,
                   "reason": record["failure_attribution"]["reason"]})
    return record
# --- end D52 -------------------------------------------------------------------------


class Controller:
    """Evaluator-owned lifecycle state machine for one real Builder session."""

    def __init__(
        self,
        *,
        repository: Path,
        cases: Path,
        run_dir: Path,
        broker_endpoint: str,
        hidden_cases: Path | None = None,
        hidden_output: Path | None = None,
        hidden_broker_endpoint: str | None = None,
        builder_witness: Path | None = None,
        feedback_record: Path | None = None,
        public_case_ids: tuple[str, ...] = DEV_CASES,
        hidden_case_ids: tuple[str, ...] = CASE_IDS,
        pilot_not_formal: bool = False,
        max_dev_rounds: int = 10,
        current_binding: dict[str, Any] | None = None,
        readiness_profile: str | None = None,
    ) -> None:
        self.repository = repository.resolve()
        self.cases = cases.resolve()
        self.run_dir = run_dir.resolve()
        self.broker_endpoint = broker_endpoint
        self.judge_endpoint: str | None = None
        self.hidden_broker_endpoint = hidden_broker_endpoint
        self.hidden_cases = hidden_cases.resolve() if hidden_cases else None
        self.hidden_output = hidden_output.resolve() if hidden_output else self.run_dir / "hidden_after_freeze"
        self.builder_witness_path = builder_witness.resolve() if builder_witness else None
        self.feedback_record_path = feedback_record.resolve() if feedback_record else None
        if not public_case_ids or any(case_id not in DEV_CASES for case_id in public_case_ids):
            raise ValueError("public_case_ids must be a non-empty subset of the canonical dev inventory")
        if not hidden_case_ids or any(case_id not in CASE_IDS for case_id in hidden_case_ids):
            raise ValueError("hidden_case_ids must be a non-empty subset of the canonical hidden inventory")
        if (tuple(public_case_ids) != DEV_CASES or tuple(hidden_case_ids) != CASE_IDS) and not pilot_not_formal:
            raise ValueError("reduced inventories are permitted only for pilot_not_formal execution")
        self.public_case_ids = tuple(public_case_ids)
        self.hidden_case_ids = tuple(hidden_case_ids)
        self.pilot_not_formal = pilot_not_formal
        self.current_binding = current_binding
        # Stamped into the freeze so the sealed document itself says which
        # profile it was taken under, instead of leaving that to be inferred
        # from the run's surroundings afterwards.
        self.readiness_profile = readiness_profile
        if not 1 <= max_dev_rounds <= 10:
            raise ValueError("max_dev_rounds must be between 1 and 10")
        self.max_dev_rounds = max_dev_rounds
        self.witness = load_and_verify_witness(self.builder_witness_path) if self.builder_witness_path else None
        self.rounds: list[dict[str, Any]] = []
        self.frozen: dict[str, Any] | None = None
        self.run_dir.mkdir(parents=True, exist_ok=True)
        self.product_attempts = ProductAttempts(self.run_dir / "product_attempts")

    def bind_builder_witness(self, path: Path, *, expected_submissions: int) -> dict[str, Any]:
        """Bind evaluator-owned stage evidence before the corresponding submit.

        The one-stop orchestrator updates the same witness path at every
        accepted lifecycle boundary. Candidate 1 is provisional; every later
        submission reloads a feedback-bound witness from the same connection.
        """
        resolved = path.resolve()
        witness = load_and_verify_witness(resolved, expected_submissions=expected_submissions)
        self.builder_witness_path = resolved
        self.witness = witness
        return witness

    @staticmethod
    def _dev_is_freeze_eligible(result: dict[str, Any]) -> bool:
        """Allow Candidate behavior failures, but never infrastructure failures."""
        if ("infrastructure" in str(result.get("classification", ""))
                or result.get("infrastructure_invalid") is True or result.get("score") is None):
            return False
        if result.get("score_kind") in {"candidate_zero", "independent_result_rubric"}:
            return True
        return False

    def _require_builder(self) -> dict[str, Any]:
        if self.witness is None:
            raise RuntimeError("formal lifecycle requires evaluator-attested same-session Builder witness")
        return self.witness

    def known_product_digests(self) -> set[str]:
        import re
        values = {path.name for path in self.product_attempts.root.iterdir()
                  if re.fullmatch('[0-9a-f]{64}', path.name)}
        for path in self.run_dir.glob('round_*.json'):
            record = read_json(path)
            digest = record.get('candidate_digest')
            if isinstance(digest, str) and re.fullmatch('[0-9a-f]{64}', digest):
                values.add(digest)
        return values

    def lookup_product_attempt(self, digest):
        result = self.product_attempts.lookup(digest)
        if result is not None:
            return result
        # Preserve old completed/unknown rounds created before this ledger
        # existed. Reading them must never authorize an implicit new sample.
        for path in sorted(self.run_dir.glob('round_*.json')):
            record = read_json(path)
            if record.get('candidate_digest') == digest:
                return {'state': 'completed' if record.get('accepted') else 'infrastructure_invalid',
                        'candidate_digest': digest, 'case_results': record.get('dev', [])}
        return None

    def _run_public(self, candidate: Path, number: int) -> list[dict[str, Any]]:
        results: list[dict[str, Any]] = []
        launcher = Path(__file__).with_name("lower_agent_launcher.py").resolve()
        for case_id in self.public_case_ids:
            case = self.cases / f"{case_id}.json"
            output = self.run_dir / f"round_{number:03d}" / case_id
            workspace = output / "workspace"
            try:
                from agentloop.evaluator.dynamic_case_service import issue
                visible_template = read_json(case)
                asset = visible_template.pop("policy_asset", None)
                if asset:
                    visible_template["policy"] = read_json((case.parent / asset).resolve())
                # The public dev world's expectations live next to the case in an
                # evaluator-only sibling file, exactly as the hidden bundle does.
                # They are never staged into the Builder package.
                oracle_path = case.parent / f"{case_id}.oracle.json"
                if oracle_path.is_file():
                    oracle_template = read_json(oracle_path)
                else:
                    oracle_template = {"required_action_ids": [action["id"] for action in visible_template["allowed_actions"]],
                                       "expected_permissions": ({"read_source": "allow", "read_secret": "deny"} if case_id == "dev_001" else
                                                                {"safe_shell": "allow", "blocked_network": "deny"})}
                oracle_template = {key: value for key, value in oracle_template.items()
                                   if key not in {"schema_version", "case_id", "runtime_nonce",
                                                  "case_contract_digest", "oracle_digest",
                                                  "visible_case_digest"}}
                world = output / "issued_world"
                issued = issue(case_id, {"candidate": visible_template, "private_oracle": oracle_template}, world, world)
                case = Path(issued["candidate_payload"])
                oracle = read_json(Path(issued["oracle_path"]))
            except Exception as exc:
                value = {"case_id": case_id, "classification": "evaluator_infrastructure_failure",
                         "infrastructure_invalid": True, "round_consumed": False, "score": None,
                         "reason": "authority world issuance failed: " + type(exc).__name__ + ": " + str(exc)}
                write_json(output / "measurement.json", value)
                self.product_attempts.record_case(candidate_digest(candidate), case_id, value, output)
                results.append(value)
                continue
            command = [sys.executable, str(launcher), "--plugin-root", str(candidate / "plugins/policy-provenance-ledger"),
                       "--case", str(case), "--workspace", str(workspace), "--output", str(output),
                       "--broker-endpoint", self.broker_endpoint]
            command.extend(["--candidate-digest", candidate_digest(candidate)])
            outer_timeout = False
            # D52: the 600 s scope below is the evaluator's OWN case deadline.  Snapshot
            # the broker first so a case it kills can still be attributed.
            d52_before = _d52_successful_calls(self.broker_endpoint)
            try:
                from agentloop.evaluator.product_lifecycle import run_scoped_launcher
                done = run_scoped_launcher(command, output=output, timeout=600)
            except subprocess.TimeoutExpired as exc:
                # A wall timeout is a process observation, not attribution.
                # Retain any already-written evaluator trajectory: a proved
                # Candidate fatal error can be zero, a valid artifact can be
                # judged, and a broker failure remains infrastructure N/A.
                outer_timeout = True
                done = subprocess.CompletedProcess(command, 124, str(exc.stdout or ''), str(exc.stderr or ''))
            trajectory = output / "trajectory.json"
            if not trajectory.is_file():
                # D52 (policy D14, 2026-09-19): the 600 s scope SIGKILLs the launcher, so
                # no trajectory is ever written and the case landed here unscored.  If the
                # evaluator's broker saw this case make successful lower calls, the product
                # observably ran and spent the whole budget it was given: a Candidate
                # outcome that is SCORED.  Anything unproven keeps the old record verbatim.
                value = _d52_case_budget_record(
                    case_id=case_id, digest=candidate_digest(candidate), output=output,
                    endpoint=self.broker_endpoint, before=d52_before,
                    exit_code=done.returncode) if outer_timeout else None
                if value is None:
                    value = {"case_id": case_id, "candidate_digest": candidate_digest(candidate),
                             "classification": "unresolved_execution_failure", "score": None,
                             "infrastructure_invalid": False, "round_consumed": False,
                             "reason": "launcher ended without attributable evaluator trajectory",
                             "outer_timeout": outer_timeout, "exit_code": done.returncode,
                             "lifecycle_evidence": str(output / 'product_container_lifecycle.json')}
                write_json(output / 'measurement.json', value)
                self.product_attempts.record_case(candidate_digest(candidate), case_id, value, output)
                results.append(value)
                continue
            try:
                value = json.loads(trajectory.read_text(encoding="utf-8"))
            except (OSError, UnicodeError, json.JSONDecodeError) as exc:
                value = {"case_id": case_id, "infra": "invalid_trajectory", "classification": "launcher_infrastructure_failure",
                         "error": f"{type(exc).__name__}: {exc}"}
            if isinstance(value, dict):
                value.setdefault("case_id", case_id)
                value['outer_timeout'] = outer_timeout
                try:
                    from agentloop.evaluator.semantic_score import execution_verdict, judge_evidence
                    from agentloop.evaluator.case_contract import task_local_rubric
                    from agentloop.evaluator.hidden_executor import _oracle_comparison
                    verdict, zero = execution_verdict(value, case_id, candidate_digest(candidate))
                    if zero is not None:
                        value.update({"score": 0, "score_kind": "candidate_zero", "result_judge_contract": zero})
                    elif verdict.get("classification") == "infrastructure_invalid":
                        raise RuntimeError(str(verdict.get("reason")))
                    elif verdict.get('classification') != 'scoreable':
                        value.update(score=None, score_kind=None, round_consumed=False,
                            classification='unresolved_execution_failure',
                            execution_verdict=verdict)
                    else:
                        visible = read_json(case)
                        (output / "task_input.md").write_text(visible["task"] + "\n")
                        (output / "task_local_rubric.md").write_text(task_local_rubric(case_id))
                        write_json(output / "native_evidence.json", {"case_id": case_id, "product_events": value.get("product_events"),
                                   "binding": value.get("binding"), "observations": value.get("observations")})
                        write_json(output / "oracle_comparison.json", _oracle_comparison(case_id, oracle, value, value.get("answer")))
                        contract = judge_evidence(case_id=case_id, artifact=output / "agent_result.json", trajectory=trajectory,
                                   task_input=output / "task_input.md", rubric=output / "task_local_rubric.md",
                                   native=output / "native_evidence.json", oracle=output / "oracle_comparison.json",
                                   endpoint=self.judge_endpoint, output=output / "result_judge")
                        value.update({"score": contract["result_score"], "score_kind": "independent_result_rubric", "result_judge_contract": contract})
                    write_json(output / "measurement.json", value)
                except Exception as exc:
                    value.update({"classification": "evaluator_infrastructure_failure", "infrastructure_invalid": True,
                                  "score": None, "scoring_error": f"{type(exc).__name__}: {exc}"})
                    write_json(output / "measurement.json", value)
                self.product_attempts.record_case(candidate_digest(candidate), case_id, value, output)
                results.append(value)
            else:
                value = {"case_id": case_id, "infra": "invalid_trajectory", "classification": "launcher_infrastructure_failure"}
                self.product_attempts.record_case(candidate_digest(candidate), case_id, value, output)
                results.append(value)
        return results

    def _write_feedback_request(self, round_record: dict[str, Any]) -> Path:
        """Persist public evidence for a separate evaluator feedback producer.

        This is deliberately *not* an ``agentswe-edit-feedback/v1`` record.
        The controller cannot attest that the same Builder consumed feedback
        which it has just invented, so the next Candidate must receive an
        evaluator-owned feedback artifact and a witness that binds to its bytes.
        """
        request = {
            "schema_version": "agentswe-edit-feedback-request/v1",
            "candidate_number": round_record["submission_number"],
            "candidate_digest": round_record["candidate_digest"],
            "candidate_patch_sha256": round_record["patch_sha256"],
            "public_inventory": list(self.public_case_ids),
            "public_cases": [
                {
                    "case_id": item.get("case_id"),
                    "classification": item.get("classification"),
                    "broker_delta": _delta(
                        item.get("broker", {}).get("before") if isinstance(item.get("broker"), dict) else None,
                        item.get("broker", {}).get("after") if isinstance(item.get("broker"), dict) else None,
                    ),
                    "candidate_observation": item.get("answer", {}).get("decision") if isinstance(item.get("answer"), dict) else None,
                    "score": item.get("score"), "score_kind": item.get("score_kind"),
                }
                for item in round_record["dev"]
            ],
            "oracle_included": False,
            "native_suite_used_as_result": False,
            "feedback_artifact_required": True,
            "builder_witness_required": True,
        }
        number = int(round_record["submission_number"])
        path = self.run_dir / "feedback" / f"candidate_{number:03d}_request.json"
        write_json(path, request)
        return path

    def _refresh_witness(self, expected_submissions: int) -> dict[str, Any]:
        """Reload the attestation so the current revision sees final witness bytes."""
        if self.builder_witness_path is None:
            raise RuntimeError("formal lifecycle requires evaluator-attested same-session Builder witness")
        witness = load_and_verify_witness(
            self.builder_witness_path, expected_submissions=expected_submissions
        )
        for number, record in enumerate(self.rounds, 1):
            verify_submission_binding(
                witness,
                number=number,
                patch=Path(str(witness["submissions"][number - 1]["patch"])),
                candidate_digest=str(record["candidate_digest"]),
            )
        self.witness = witness
        return witness

    def submit(self, patch: Path, number: int) -> dict[str, Any]:
        witness = self._require_builder()
        if self.frozen:
            raise RuntimeError("submission is forbidden after freeze")
        if number != len(self.rounds) + 1 or not 1 <= number <= self.max_dev_rounds:
            raise RuntimeError("Candidate rounds must be consecutive and within max_dev_rounds")
        patch = patch.resolve()
        expected_product = witness["submissions"][number - 1]["candidate_digest"]
        if self.lookup_product_attempt(expected_product) is not None:
            raise ProductReplayBlocked(expected_product)
        candidate = self.run_dir / f"candidate_{number:03d}"
        if candidate.exists():
            raise RuntimeError("prior Candidate worktree remains; preserve its unfinished evidence")
        build = materialize(self.repository, patch, candidate)
        candidate_value = str(build["candidate_digest"])
        verify_submission_binding(witness, number=number, patch=patch, candidate_digest=candidate_value)
        if number > 1:
            witness = self._refresh_witness(number)
            verify_submission_binding(witness, number=number, patch=patch, candidate_digest=candidate_value)
            if not self.feedback_record_path:
                raise RuntimeError("revisions require an evaluator-owned --feedback-record")
            feedback, feedback_digest = load_and_verify_feedback(
                self.feedback_record_path,
                session_id=str(witness["session_id"]),
                candidate_1_digest=str(self.rounds[-1]["candidate_digest"]),
                connection_id=str(witness["connection_id"]),
            )
            if feedback_digest != witness["feedback_digest"]:
                raise RuntimeError("Builder witness feedback digest does not match evaluator feedback bytes")
            if witness["submissions"][number - 1].get("feedback_digest") != feedback_digest:
                raise RuntimeError("revised Candidate submission is not feedback-bound")
            feedback_summary = {"feedback_digest": feedback_digest, "feedback_consumed": True,
                                "feedback_schema": feedback.get("schema_version")}
        else:
            feedback_summary = None
        self.product_attempts.claim(candidate_value, {"submission_number": number,
            "patch_sha256": build.get("patch_sha256"), "builder_session_id": witness["session_id"],
            "public_inventory": list(self.public_case_ids)})
        if build.get("build_valid") is False:
            from agentloop.evaluator.semantic_score import execution_verdict
            dev = []
            for case_id in self.public_case_ids:
                value = {"case_id": case_id, "candidate_digest": candidate_value,
                    "classification": "candidate_build_failure", "execution_attempted": True,
                    "infrastructure_invalid": False, "environment_preflight": build["environment_preflight"],
                    "failure_attribution": {"party": "candidate", "observed_by": "evaluator", "fatal": True,
                        "reason": build["build_diagnostic"], "evidence_paths": [str(candidate / ".agentloop_build.json")]},
                    "broker": {}, "trajectory": []}
                _, zero = execution_verdict(value, case_id, candidate_value)
                if zero is None:
                    raise RuntimeError("Candidate build attribution did not satisfy the shared contract")
                value.update(score=0, score_kind="candidate_zero", result_judge_contract=zero)
                write_json(self.run_dir / f"round_{number:03d}" / case_id / "measurement.json", value)
                dev.append(value)
        else:
            dev = self._run_public(candidate, number)
        record: dict[str, Any] = {
            "schema_version": "agentswe-claude-candidate-round/v1",
            "submission_number": number,
            "candidate_digest": candidate_value,
            "patch_sha256": build.get("patch_sha256"),
            "builder_session_id": witness["session_id"],
            "builder_connection_id": witness["connection_id"],
            "build": build,
            "dev": dev,
            "public_inventory": list(self.public_case_ids),
            "pilot_not_formal": self.pilot_not_formal,
            "current_binding": self.current_binding,
            "feedback": feedback_summary,
        }
        ineligible = [item.get("case_id", "unknown") for item in dev if not self._dev_is_freeze_eligible(item)]
        if ineligible:
            attempt = 1 + len(list(self.run_dir.glob(f"round_{number:03d}_infrastructure_invalid_attempt_*.json")))
            record.update({
                "accepted": False,
                "round_consumed": False,
                "classification": "public_infrastructure_failure",
                "ineligible_dev_cases": ineligible,
            })
            write_json(self.run_dir / f"round_{number:03d}_infrastructure_invalid_attempt_{attempt:03d}.json", record)
            archive = self.run_dir / "infrastructure_attempts" / f"round_{number:03d}_attempt_{attempt:03d}"
            archive.mkdir(parents=True, exist_ok=False)
            candidate.rename(archive / "candidate")
            round_output = self.run_dir / f"round_{number:03d}"
            if round_output.exists():
                round_output.rename(archive / "evaluation")
            self.product_attempts.finish(candidate_value, record, archive / "evaluation")
            raise RuntimeError("public dev infrastructure invalid; Candidate round not consumed: " + ", ".join(map(str, ineligible)))
        record["accepted"] = True
        record["round_consumed"] = True
        record["dev_score"] = sum(item["score"] for item in dev) / len(dev)
        record["dev_passed"] = record["dev_score"] > 60
        self.product_attempts.finish(candidate_value, record, self.run_dir / f"round_{number:03d}")
        if not post_freeze_superseded(self, record):
            self.rounds.append(record)
        write_json(self.run_dir / f"round_{number:03d}.json", record)
        if number < self.max_dev_rounds:
            request_path = self._write_feedback_request(record)
            record["feedback_request_path"] = str(request_path)
            record["feedback_record_required"] = True
            write_json(self.run_dir / f"round_{number:03d}.json", record)
        return record

    def freeze_latest(self, reason: str = "builder_exit") -> dict[str, Any]:
        if self.frozen:
            return self.frozen
        if not self.rounds:
            raise RuntimeError("cannot freeze without an accepted Candidate")
        latest = self.rounds[-1]
        number = int(latest["submission_number"])
        candidate = self.run_dir / f"candidate_{number:03d}"
        materialized_digest = candidate_digest(candidate)
        frozen = self.run_dir / "frozen_candidate"
        if frozen.exists():
            raise RuntimeError("frozen Candidate already exists")
        _assert_regular_tree(candidate)
        shutil.copytree(candidate, frozen, symlinks=False)
        if candidate_digest(frozen) != materialized_digest:
            raise RuntimeError("frozen Candidate digest mismatch")
        _read_only_tree(frozen)
        manifest = {
            "schema_version": "agentswe-edit-freeze-manifest/v2",
            "source_submission": number,
            "source_submission_id": f"candidate_{number:03d}",
            "accepted_submission_count": len(self.rounds),
            "max_dev_rounds": self.max_dev_rounds,
            "accepted_candidate_digests": [item["candidate_digest"] for item in self.rounds],
            "candidate_digest": materialized_digest,
            "candidate_root": str(frozen),
            "builder_session_id": latest["builder_session_id"],
            "builder_connection_id": latest["builder_connection_id"],
            "feedback_digest": feedback_digest(self.feedback_record_path) if self.feedback_record_path and self.feedback_record_path.is_file() else None,
            "feedback_consumed": True,
            "feedback_chain_complete": all(
                index == 1 or bool(record.get("feedback", {}).get("feedback_consumed"))
                for index, record in enumerate(self.rounds, start=1)
            ),
            "freeze_reason": reason,
            "hidden_gate": "issued-after-freeze",
            "frozen_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            "hidden_allowed": True,
            "dev_evaluated": True,
            "immutable_candidate": True,
            "frozen_tree_read_only": True,
            "frozen_tree_regular": True,
            "credential_mounted_to_candidate": False,
            "hidden_case_inventory": list(self.hidden_case_ids),
            "pilot_not_formal": self.pilot_not_formal,
            "readiness_profile": self.readiness_profile,
            "current_binding": self.current_binding,
        }
        write_json(self.run_dir / "freeze_manifest.json", manifest)
        freeze_path = self.run_dir / "freeze_manifest.json"
        freeze_path.chmod(stat.S_IMODE(freeze_path.stat().st_mode) & ~0o222)
        self.frozen = manifest
        return manifest

    def hidden(self) -> list[dict[str, Any]]:
        if not self.frozen:
            raise RuntimeError("hidden cases are forbidden before Candidate freeze")
        if self.hidden_cases is None:
            raise RuntimeError("real hidden execution requires evaluator-issued --hidden-cases-dir")
        if not self.hidden_broker_endpoint or self.hidden_broker_endpoint == self.broker_endpoint:
            raise RuntimeError("hidden execution requires a fresh independent evaluator-owned broker endpoint")
        run = run_hidden(
            freeze_manifest=self.run_dir / "freeze_manifest.json",
            cases_dir=self.hidden_cases,
            output=self.hidden_output,
            broker_endpoint=self.hidden_broker_endpoint,
            case_ids=list(self.hidden_case_ids),
            pilot_not_formal=self.pilot_not_formal,
        )
        # run_dir is already the lifecycle directory. Both modes use the same
        # complete attester; an acceptance subset cannot claim formal coverage.
        attest(
            run_dir=self.run_dir,
            hidden_run=self.hidden_output / "hidden_run.json",
            output=self.run_dir / "hidden_after_freeze_attestation.json",
            case_ids=list(self.hidden_case_ids), acceptance=self.pilot_not_formal,
        )
        return run["cases"]


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repository", type=Path)
    parser.add_argument("--cases", type=Path)
    parser.add_argument("--run-dir", type=Path)
    parser.add_argument("--broker-endpoint")
    parser.add_argument("--hidden-broker-endpoint")
    parser.add_argument("--hidden-cases-dir", type=Path)
    parser.add_argument("--hidden-output-dir", type=Path)
    parser.add_argument("--builder-witness", type=Path)
    parser.add_argument("--feedback-record", type=Path)
    parser.add_argument("--patch", type=Path, action="append")
    parser.add_argument("--max-dev-rounds", type=int, default=10)
    parser.add_argument("--self-test", action="store_true")
    args = parser.parse_args()
    if args.self_test:
        print(json.dumps({"controller": "loadable", "builder_provenance_required": True,
                          "lifecycle": ["candidate", "dev_001+dev_002", "feedback",
                                         "repeat_up_to_10", "freeze", "hidden_after_freeze"]}, ensure_ascii=False))
        return 0
    missing = [name for name, value in (
        ("--repository", args.repository), ("--cases", args.cases),
        ("--run-dir", args.run_dir), ("--broker-endpoint", args.broker_endpoint),
    ) if value is None]
    if missing:
        raise SystemExit("missing required lifecycle arguments: " + ", ".join(missing))
    if not args.builder_witness:
        raise SystemExit("refusing un-attested external Candidate lifecycle: --builder-witness is required")
    if not 1 <= args.max_dev_rounds <= 10:
        raise SystemExit("--max-dev-rounds must be between 1 and 10")
    if not args.patch or len(args.patch) > args.max_dev_rounds:
        raise SystemExit("provide between one and max_dev_rounds --patch paths")
    controller = Controller(repository=args.repository, cases=args.cases, run_dir=args.run_dir,
                            broker_endpoint=args.broker_endpoint, hidden_broker_endpoint=args.hidden_broker_endpoint,
                            hidden_cases=args.hidden_cases_dir, hidden_output=args.hidden_output_dir,
                            builder_witness=args.builder_witness, feedback_record=args.feedback_record,
                            max_dev_rounds=args.max_dev_rounds)
    for number, patch in enumerate(args.patch, 1):
        controller.submit(patch, number)
    controller.freeze_latest("controller_cli_complete")
    if not args.hidden_cases_dir:
        raise SystemExit("public lifecycle complete and frozen; --hidden-cases-dir is required for hidden execution")
    print(json.dumps({"freeze": controller.frozen, "hidden": controller.hidden()}, indent=2, ensure_ascii=False))
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


if __name__ == "__main__":
    raise SystemExit(main())
