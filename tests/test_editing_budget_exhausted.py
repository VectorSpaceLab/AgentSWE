"""`agentswe result` for a formal Editing run whose Builder the time budget cut off (stdlib unittest, systemd mocked).

The protocol freezes the last accepted submission when the Builder exits, uses up its submissions or uses up its
5-hour budget, and scores a Builder with no accepted submission 0. The task trees freeze and run the held-out cases
only after a Builder session that ended with a successful terminal event, so a session the budget cut off stops at
the tree's lifecycle gate: the unit ends failed (exit 2), no held-out case runs and the summaries carry no score.
`result` now recognizes such a run (budget_cut_state) and adds a `budget_exhausted` block: the accepted submissions
and the latest one, the rule, "held_out": "not run", and either `scored: false` with what to do next, or (no accepted
submission) `score: 0` with its basis.

The fixtures follow the files the trees write at that gate: a DeepTutor-like run (records in
lifecycle/controller_state.json, native evidence in builder_native_attestation.json, status formal_evidence_incomplete),
an Aider-like run (records and native evidence in builder_session_attestation.json, the latest submission already
frozen before the gate, status builder_integration_incomplete), a Codex run (the cut recorded as exit 125 in
one_stop_summary.json, whose only trial exception is AgentTimeoutError) and a DeepCode run (the cut recorded as 125 by
its pre-agent gate, which refuses a Harbor trial with any exception; one record file per accepted submission). The negative controls are a smoke run, a
formal run that finished normally, and formal runs that stopped for other infrastructure reasons.
"""
from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from agentswe.runners import editing_agentloop_v1 as ed  # noqa: E402

TERMINAL = "native Builder has no successful terminal event"
DEADLINE = 1_800_018_000.0
STARTED = DEADLINE - 18_000
LABEL = "oss-formal-s1-20260101t000000z"
DIGEST = {n: f"{n:x}" * 64 for n in range(1, 6)}


class Cfg:
    def __init__(self, home: Path):
        self.home = home

    def get(self, key, default=None):
        return default


def write(path: Path, value) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, indent=2))


def receipt(exit_reason="infrastructure_cut", exit_code=0, exception="AgentTimeoutError", left=65.0, resume=False):
    """builder_segment_receipt.json of a one-segment Builder session that ended `left` seconds before its deadline."""
    harbor = {"exception_type": exception, "exception_message": "", "exception_traceback": "",
              "occurred_at": "2027-01-15T05:00:00Z"} if exception else None
    row = {"segment_index": 1, "trial": "builder_task__abc1234", "session_id": "0199-session",
           "started_at_epoch": STARTED, "ended_at_epoch": DEADLINE - left, "exit_reason": exit_reason,
           "exit_code": exit_code, "rollout_sha256": "0" * 64, "harbor_exception": harbor, "promoted": True,
           "decision": {"resume": resume, "refusals": [] if resume else ["the segment completed its turn"],
                        "remaining_seconds": left, "exit_code": exit_code, "stream": {}}}
    return {"schema_version": "agentswe-builder-segments/v1", "resume_cap": 2, "builder_deadline_epoch": DEADLINE,
            "budget_seconds": 18000.0, "attempts": [row], "segment_count": 1}


class BudgetExhausted(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.home = Path(tmp.name)

    def launch(self, key, task, mode="formal"):
        if mode == "formal":
            launch_id = f"0905-edit-codex-xhigh-{LABEL}"
            run_dir = self.home / "runs/editing/formal/codex_xhigh" / key / f"{launch_id}-{key}"
            log = self.home / "runs/editing/formal/launch_control" / launch_id / key / "orchestrator.log"
            log.parent.mkdir(parents=True)
            log.write_text("Traceback ...\nformal one-stop exit 2\n")
            unit = f"agentswe-oss-formal-{key}-{LABEL}.service"
        else:
            run_dir = self.home / "runs/editing/smoke" / key / f"oss-smoke-s1-20260101t000000z"
            unit = f"agentswe-oss-edit-{key}-oss-smoke-s1-20260101t000000z.service"
        run_dir.mkdir(parents=True)
        return run_dir, {"run_id": f"e-{key}-{LABEL}", "task": task, "family": "editing", "mode": mode,
                         "comparable": mode != "smoke", "builder": {"profile": "codex"}, "unit": unit,
                         "run_dir": str(run_dir), "log": str(self.home / "runs/editing" / f"{key}-{LABEL}.launch.log")}

    def result(self, launch, unit_state="failed", exit_status="2"):
        with mock.patch.object(ed, "_unit_state", return_value=unit_state), \
                mock.patch.object(ed.util, "out", return_value=exit_status):
            return ed.result(Cfg(self.home), launch)

    # --- fixtures -------------------------------------------------------------------------------------------------

    def deeptutor(self, accepted=3, segment=None):
        """DeepTutor at its gate: the controller's records, strict native evidence, no freeze, no hidden directory."""
        run_dir, launch = self.launch("deeptutor", "deeptutor-adaptive-remediation")
        write(run_dir / "builder_segment_receipt.json", segment or receipt())
        write(run_dir / "builder_native_attestation.json", {
            "schema_version": "agentswe-native-builder-evidence/v1", "valid": False, "errors": [TERMINAL],
            "builder_exit_code": 0, "accepted_count": accepted})
        dev = {"dev_001": {"case_id": "dev_001", "terminal": True, "score": 40}, "dev_002": {"terminal": True}}
        write(run_dir / "lifecycle/controller_state.json", {
            "records": [{"schema_version": "agentswe-deeptutor-accepted-candidate/v2", "round": n,
                         "session_id": "builder-session-1", "candidate_digest": DIGEST[n],
                         "candidate_path": str(run_dir / f"lifecycle/candidate_{n:03d}"), "dev_results": dev,
                         "consumed": True} for n in range(1, accepted + 1)],
            "infrastructure_attempts": [{"round": accepted + 1, "consumed": False}], "frozen": None})
        write(run_dir / "builder_process.json", {"exit_code": 0, "timed_out": False, "lifecycle_ready_for_hidden": False})
        summary = {"schema_version": "agentswe-deeptutor-one-stop-summary/v1", "status": "formal_evidence_incomplete",
                   "builder_exit_code": 0, "candidate_rounds_consumed": accepted, "freeze_manifest": None,
                   "hidden_attestation": None, "formal_finalizer_exit": None, "formal_aggregation": None,
                   "result_axis": "N/A", "code_axis": "N/A", "combined_score": None}
        write(run_dir / "summary.json", summary)
        write(run_dir / "one_stop_summary.json", summary)
        return run_dir, launch

    def aider(self, accepted=2, segment=None, native_errors=(TERMINAL,), exit_code=0):
        """Aider at its gate: it froze the latest accepted submission before the gate refused the native evidence."""
        run_dir, launch = self.launch("aider", "aider-worktree-transaction")
        write(run_dir / "builder_segment_receipt.json", segment or receipt())
        records = [{"submission_number": n, "candidate_digest": DIGEST[n], "accepted": True, "round_consumed": True,
                    "state": "completed", "role": "initial" if n == 1 else "feedback_revision"}
                   for n in range(1, accepted + 1)]
        freeze = {"schema_version": "agentswe-aider-freeze-manifest-v2", "source_submission": accepted,
                  "source_submission_id": f"candidate-{accepted:03d}", "candidate_delivery_digest": DIGEST[accepted],
                  "freeze_reason": "builder_exit"} if accepted else None
        write(run_dir / "builder_session_attestation.json", {
            "schema_version": "aider-builder-session-attestation-v1", "same_session": False,
            "native_evidence": {"valid": False, "errors": list(native_errors)}, "candidate_records": records,
            "builder_exit_code": exit_code, "builder_timed_out": exit_code == 124, "accepted_rounds": accepted,
            "max_dev_rounds": 5, "freeze": freeze})
        write(run_dir / "lifecycle/dev_lifecycle.json", {"records": records, "frozen": freeze})
        if freeze:
            write(run_dir / "freeze_manifest.json", freeze)
        write(run_dir / "summary.json", {"status": "builder_integration_incomplete", "builder_exit_code": exit_code,
                                         "builder_timed_out": exit_code == 124, "public_dev_complete": bool(records),
                                         "records": records})
        return run_dir, launch

    def deepcode(self, accepted=4, resources_valid=True, exception="AgentTimeoutError"):
        """DeepCode at its gate: its pre-agent gate refuses a Harbor trial with any exception, so the cut is a 125."""
        run_dir, launch = self.launch("deepcode", "deepcode-claim-traceability")
        write(run_dir / "builder_segment_receipt.json", receipt())
        native = {"valid": False, "errors": [TERMINAL], "builder_exit_code": 125}
        write(run_dir / "builder_native_attestation.json", native)
        write(run_dir / "builder_session_attestation.json", {
            "schema_version": "deepcode-agentloop-builder-session-attestation-v2", "same_continuous_session": False,
            "native_attestation": native, "builder_process": {"exit_code": 125, "timed_out": False},
            "accepted_submission_count": accepted, "freeze": None, "formal_lifecycle_eligible": False})
        write(run_dir / "builder_preagent_gate_attestation.json", {
            "valid": False, "errors": ["Harbor trial failed or never started the native Agent"],
            "trial": {"path": str(run_dir / "jobs/deepcode-builder/builder_task__abc1234/result.json"),
                      "sha256": "2" * 64, "exception_info": {"exception_type": exception},
                      "agent_setup": {"started_at": "2027-01-15T00:01:00Z"},
                      "agent_execution": {"started_at": "2027-01-15T00:02:00Z"}}})
        write(run_dir / "builder_resource_attestation.json", {"valid": resources_valid, "errors": []})
        for n in range(1, accepted + 1):
            write(run_dir / f"lifecycle/dev_feedback_candidate_{n:03d}.json", {
                "round": n, "source_submission": n, "candidate_digest": DIGEST[n], "accepted": True, "dev": []})
        write(run_dir / "summary.json", {"status": "builder_lifecycle_incomplete",
                                         "builder": {"exit_code": 125, "timed_out": False},
                                         "builder_session_attestation": str(run_dir / "builder_session_attestation.json")})
        return run_dir, launch

    def codex(self, resources_valid=True, exception="AgentTimeoutError"):
        """Codex at its gate: Harbor exited 0 on AgentTimeoutError and the tree recorded the trial as invalid (125)."""
        run_dir, launch = self.launch("codex", "codex-execution-residual")
        write(run_dir / "builder_segment_receipt.json", receipt())
        trial = {"path": str(run_dir / "jobs/codex-builder/builder_task__abc1234/result.json"), "sha256": "1" * 64,
                 "exception_info": {"exception_type": exception, "exception_message": "Agent execution timed out"}}
        write(run_dir / "builder_trial_attestation.json", {"valid": False, "trials": [trial]})
        builder = {"exit_code": 125, "infrastructure_invalid": {
            "resources_valid": resources_valid, "cleanup_complete": True, "cleanup_retained_terminal": False,
            "trial_valid": False, "trials": [trial]}}
        records = [{"submission_number": n, "submission_id": f"candidate-{n:03d}-{DIGEST[n][:12]}",
                    "candidate_digest": DIGEST[n], "state": "completed"} for n in (1, 2)]
        records.append({"submission_number": 3, "submission_id": f"candidate-003-{DIGEST[3][:12]}",
                        "candidate_digest": DIGEST[3], "state": "running"})  # in evaluation when the budget ended
        write(run_dir / "one_stop_summary.json", {
            "schema_version": "agentswe-edit-agentloop-formal-v1", "status": "builder_lifecycle_incomplete",
            "builder": builder, "builder_interrupted_at_max_dev_rounds": False,
            "gate_errors": [f"Builder native feedback evidence incomplete: {TERMINAL}",
                            f"Builder Harbor failed: {json.dumps(builder)}"],
            "native_evidence": {"valid": False, "errors": [TERMINAL]}, "dev_lifecycle": records, "freeze": None,
            "hidden": {"status": "not_started", "reason": "Builder lifecycle gate failed"},
            "formal_aggregation": {"formal_result_publishable": False, "result_axis": "N/A"},
            "result_axis": "N/A", "combined_score": None})
        return run_dir, launch

    # --- the budget cut --------------------------------------------------------------------------------------------

    def test_deeptutor_layout_budget_cut_reports_accepted_submissions(self):
        for name, segment in (("agent timeout", receipt()),
                              ("outer deadline", receipt(exit_code=124, exception="MissingTrialResult", left=12.0))):
            with self.subTest(name):
                self.setUp()
                run_dir, launch = self.deeptutor(accepted=3, segment=segment)
                block = self.result(launch)["budget_exhausted"]
                self.assertEqual(block["accepted_submissions"], 3)
                self.assertEqual(block["latest_accepted"], {"submission": 3, "digest": DIGEST[3]})
                self.assertEqual(block["held_out"], "not run")
                self.assertIs(block["scored"], False)
                self.assertNotIn("score", block)
                # DeepTutor is one of the tasks `agentswe freeze` supports (BUDGET_FREEZE_TASKS)
                self.assertEqual(block["next"].split(":")[0], f"agentswe freeze {launch['run_id']}")
                self.assertIn("last accepted submission is frozen", block["rule"])
                self.assertEqual(block["gate_status"], "formal_evidence_incomplete")
                self.assertEqual(block["builder"]["harbor_exit_code"], segment["attempts"][0]["exit_code"])
                self.assertEqual(ed.budget_cut_state(run_dir)["records"],
                                 str(run_dir / "lifecycle/controller_state.json"))

    def test_aider_layout_budget_cut_with_a_freeze_before_the_gate(self):
        run_dir, launch = self.aider(accepted=2)
        res = self.result(launch)
        block = res["budget_exhausted"]
        self.assertEqual(block["accepted_submissions"], 2)
        self.assertEqual(block["latest_accepted"], {"submission": 2, "digest": DIGEST[2]})
        self.assertEqual((block["held_out"], block["scored"]), ("not run", False))
        self.assertEqual(block["gate_status"], "builder_integration_incomplete")
        self.assertNotIn("result_axis", res)
        self.assertNotIn("formal_result_publishable", res)
        written = json.loads((self.home / "runs/editing" / f"{launch['run_id']}.result.json").read_text())
        self.assertEqual(written["budget_exhausted"], block)

    def test_codex_budget_cut_recorded_as_exit_125(self):
        run_dir, launch = self.codex()
        block = self.result(launch)["budget_exhausted"]
        self.assertEqual(block["accepted_submissions"], 2)  # the third was still in evaluation
        self.assertEqual(block["latest_accepted"],
                         {"submission": 2, "id": f"candidate-002-{DIGEST[2][:12]}", "digest": DIGEST[2]})
        self.assertEqual(block["gate_status"], "builder_lifecycle_incomplete")
        self.assertEqual(block["summary"], "one_stop_summary.json")
        self.assertEqual((block["builder"]["harbor_exit_code"], block["builder"]["recorded_exit_code"]), (0, 125))

    def test_preagent_gate_125_from_the_agent_timeout(self):
        run_dir, launch = self.deepcode(accepted=4)
        block = self.result(launch)["budget_exhausted"]
        self.assertEqual(block["accepted_submissions"], 4)
        self.assertEqual(block["latest_accepted"], {"submission": 4, "digest": DIGEST[4]})
        self.assertEqual((block["gate_status"], block["scored"]), ("builder_lifecycle_incomplete", False))
        self.assertEqual(block["builder"]["recorded_exit_code"], 125)

    def test_zero_accepted_scores_zero(self):
        run_dir, launch = self.aider(accepted=0)
        res = self.result(launch)
        block = res["budget_exhausted"]
        self.assertEqual(block["accepted_submissions"], 0)
        self.assertIsNone(block["latest_accepted"])
        self.assertEqual((block["scored"], block["score"]), (True, 0))
        self.assertIn("No submission was accepted before the Builder budget ended", block["score_basis"])
        self.assertNotIn("next", block)
        self.assertNotIn("formal_result_publishable", res)  # a protocol 0, not a finalizer's verdict

    def test_supported_task_names_the_freeze_command(self):
        run_dir, launch = self.aider(accepted=2)
        with mock.patch.object(ed, "BUDGET_FREEZE_TASKS", frozenset({"aider"})):
            block = self.result(launch)["budget_exhausted"]
        self.assertEqual(block["next"].split(":")[0], f"agentswe freeze {launch['run_id']}")

    # --- negative controls -----------------------------------------------------------------------------------------

    def test_smoke_run_is_never_a_budget_cut(self):
        run_dir, launch = self.launch("aider", "aider-worktree-transaction", mode="smoke")
        write(run_dir / "builder_segment_receipt.json", receipt())
        write(run_dir / "builder_session_attestation.json", {
            "native_evidence": {"valid": False, "errors": [TERMINAL]}, "builder_exit_code": 0,
            "candidate_records": [{"submission_number": 1, "candidate_digest": DIGEST[1], "accepted": True}]})
        write(run_dir / "summary.json", {"status": "builder_integration_incomplete"})
        write(run_dir / "readiness_current_binding.json", {"profile": "single-dev-two-round-hidden-smoke-v1"})
        self.assertIsNone(ed.budget_cut_state(run_dir, task="aider"))
        res = self.result(launch)
        self.assertNotIn("budget_exhausted", res)
        self.assertIn("smoke", res)

    def test_normally_finished_formal_run(self):
        run_dir, launch = self.aider(accepted=5, segment=receipt(exit_reason="completed", exception=None, left=13265.4),
                                     native_errors=())
        write(run_dir / "summary.json", {"status": "formal_result_ready"})
        write(run_dir / "formal_aggregation.json", {"formal_result_publishable": True,
                                                    "result_axis": {"score": 39.3, "case_scores": {}}})
        write(run_dir / "lifecycle/hidden-after-freeze-attestation.json", {"cases": {}})
        self.assertIsNone(ed.budget_cut_state(run_dir))
        res = self.result(launch, unit_state="inactive", exit_status="0")
        self.assertNotIn("budget_exhausted", res)
        self.assertEqual(res["result_axis"]["score"], 39.3)
        # a Builder that ended its session itself 30 s before the deadline, its run stopped at the gate for another
        # reason before the held-out cases: not a budget cut either
        write(run_dir / "builder_segment_receipt.json", receipt(exit_reason="completed", exception=None, left=30.0))
        write(run_dir / "summary.json", {"status": "builder_integration_incomplete", "builder_exit_code": 0})
        (run_dir / "formal_aggregation.json").unlink()
        (run_dir / "lifecycle/hidden-after-freeze-attestation.json").unlink()
        self.assertIsNone(ed.budget_cut_state(run_dir))

    def test_other_infrastructure_failures_are_not_budget_cuts(self):
        cases = {
            # the Builder stream was cut three hours in (a provider or container failure) and could not resume
            "early cut": lambda: self.aider(segment=receipt(exception=None, left=10_800.0)),
            # Harbor's agent timeout, but the tree records the resource observer's 125
            "resource failure": lambda: self.aider(exit_code=125),
            # the native evidence also failed for another reason
            "other native error": lambda: self.aider(native_errors=(TERMINAL, "rollout identity disagrees with "
                                                                    "thread.started")),
            # a resume was decided after the cut (the session went on)
            "resumed": lambda: self.aider(segment=receipt(exception=None, left=4_000.0, resume=True)),
            # Codex's 125 with an invalid resource proof, and with a trial exception other than AgentTimeoutError
            "codex resources": lambda: self.codex(resources_valid=False),
            "codex exception": lambda: self.codex(exception="HealthcheckError"),
            # the pre-agent gate's 125 for a trial that failed otherwise, or with the resource proof invalid
            "preagent exception": lambda: self.deepcode(exception="HealthcheckError"),
            "preagent resources": lambda: self.deepcode(resources_valid=False),
        }
        for name, build in cases.items():
            with self.subTest(name):
                self.setUp()
                run_dir, launch = build()
                self.assertIsNone(ed.budget_cut_state(run_dir))
                self.assertNotIn("budget_exhausted", self.result(launch))
        # a live unit is never a cut, and a run whose held-out cases started is past the gate
        run_dir, launch = self.deeptutor()
        self.assertIsNone(ed.budget_cut_state(run_dir, unit_state="active"))
        self.assertIsNotNone(ed.budget_cut_state(run_dir))
        (run_dir / "hidden").mkdir()
        self.assertIsNone(ed.budget_cut_state(run_dir))


if __name__ == "__main__":
    unittest.main()
