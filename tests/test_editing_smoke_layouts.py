"""The `smoke` block of `agentswe result` for every Editing tree's smoke layout (stdlib unittest; no systemd).

DeepTutor and OpenWiki are covered in test_editing_smoke_result.py. The fixtures here follow real readiness smokes of
the other eight trees: where each one keeps its dev rounds and its
held-out case, and which record points to which. Recorded paths are absolute paths of the run as it ran, under
another root; the result reads them re-rooted in the run dir."""
from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from agentswe.runners import editing_agentloop_v1 as ed  # noqa: E402

NAME = "oss-smoke-s1-20260101t000000z"
ORIGINAL = "/srv/previous-host/runs/editing/smoke/task/" + NAME
CONTRACT = "readiness_scoring/result/result_score_contract.json"


def write(path: Path, value) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, indent=2))


class SmokeLayouts(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.run_dir = Path(tmp.name) / "smoke" / "task" / NAME

    def contract(self, score, state="scoreable", result_state="candidate_partial"):
        write(self.run_dir / CONTRACT, {"case_id": "test_001", "evaluation_state": state, "contract_valid": True,
                                        "result_state": result_state, "result_score": score})

    def outcome(self):
        summaries = {p.name: json.loads(p.read_text()) for p in sorted(self.run_dir.glob("*summary*.json"))}
        return ed.smoke_outcome(self.run_dir, summaries)

    def check(self, smoke, rounds, held, party, score):
        self.assertEqual([(r["round"], r["case_id"], r["classification"]) for r in smoke["dev_rounds"]], rounds)
        (case,) = smoke["held_out"]
        self.assertEqual((case["case_id"], case["classification"]), ("test_001", held))
        self.assertEqual(smoke["failure_party"], party)
        self.assertEqual((case["result_score"], case["result_score_source"]), (score, "result_score_contract"))
        return case

    def test_ai_scientist(self):
        dev = lambda: {"dev_001": {"case_id": "dev_001", "classification": "candidate_valid", "valid": True,  # noqa
                                   "classification_axis": "candidate"}}
        write(self.run_dir / "lifecycle/dev_lifecycle.json", {"schema_version": "x", "records": [
            {"submission_number": 1, "state": "public_complete", "accepted": True, "dev": dev()},
            {"submission_number": 2, "state": "public_complete", "accepted": True, "dev": dev()}]})
        write(self.run_dir / "lifecycle/hidden-after-freeze-attestation.json", {"cases": [
            {"case_id": "test_001", "classification": "candidate_behavior_failure", "classification_axis": "candidate"}]})
        write(self.run_dir / "summary.json", {"status": "pilot_pipeline_complete",
                                              "hidden_attestation": "lifecycle/hidden-after-freeze-attestation.json"})
        write(self.run_dir / "one_stop_summary.json", {"status": "pilot_pipeline_complete",
                                                       "dev_lifecycle": "lifecycle/dev_lifecycle.json"})
        self.contract(12)
        smoke = self.outcome()
        self.check(smoke, [(1, "dev_001", "candidate_valid"), (2, "dev_001", "candidate_valid")],
                   "candidate_behavior_failure", "candidate", 12)
        self.assertEqual(smoke["sources"]["held_out"], str(self.run_dir / "lifecycle/hidden-after-freeze-attestation.json"))

    def test_aider(self):
        for n, digest in ((1, "72fa"), (2, "554e")):
            write(self.run_dir / f"lifecycle/product_attempts/deliveries/{digest}/evaluations/dev_001/result.json",
                  {"case_id": "dev_001", "classification": "candidate_valid", "infrastructure_invalid": False})
        write(self.run_dir / "lifecycle/dev_lifecycle.json", {"records": [
            {"submission_number": n, "state": "completed", "dev": {"dev_001": {
                "exit_code": 0, "stdout_tail": "...", "result": f"{ORIGINAL}/lifecycle/product_attempts/deliveries/"
                                                                 f"{digest}/evaluations/dev_001/result.json"}}}
            for n, digest in ((1, "72fa"), (2, "554e"))]})
        write(self.run_dir / "lifecycle/hidden-after-freeze-attestation.json", {"results": [
            {"case_id": "test_001", "classification": "candidate_valid", "infrastructure_invalid": False,
             "result": f"{ORIGINAL}/lifecycle/evaluations/hidden/test_001/result.json",
             "failure_attribution": {"party": "candidate", "reason": "candidate_valid"}}]})
        write(self.run_dir / "readiness_summary.json", {"complete": True, "errors": [], "readiness_only": True})
        self.contract(33)
        smoke = self.outcome()
        case = self.check(smoke, [(1, "dev_001", "candidate_valid"), (2, "dev_001", "candidate_valid")],
                          "candidate_valid", "candidate", 33)
        self.assertTrue(case["infrastructure_valid"])
        self.assertTrue(all(r["infrastructure_valid"] for r in smoke["dev_rounds"]))
        self.assertEqual(smoke["status"], "readiness_complete")

    def test_claude(self):
        for n in (1, 2):
            write(self.run_dir / f"lifecycle/round_{n:03d}.json", {"accepted": True, "dev": [
                {"case_id": "dev_001", "classification": "candidate_behavior_observed", "infrastructure_invalid": False,
                 "failure_attribution": {"party": "candidate"}}]})
        write(self.run_dir / "lifecycle/hidden_after_freeze_attestation.json", {
            "classification": "REAL_EXECUTION_EVIDENCE_UNSCORED", "cases": [
                {"case_id": "test_001", "classification": "candidate_behavior_observed", "infrastructure_invalid": False,
                 "failure_attribution": {"party": "candidate", "reason": "candidate_behavior_observed"}}]})
        write(self.run_dir / "summary.json", {"status": "readiness_evidence_complete", "mode": "pilot"})
        # Claude's finalizer scores per case: readiness_scoring/result/test_001/result_score_contract.json
        write(self.run_dir / "readiness_scoring/result/test_001/result_score_contract.json",
              {"case_id": "test_001", "evaluation_state": "scoreable", "contract_valid": True,
               "result_state": "candidate_partial", "result_score": 40})
        write(self.run_dir / "lifecycle/round_001/dev_001/result_judge/result_score_contract.json",
              {"case_id": "dev_001", "result_score": 90})  # a dev round's own judge, not the held-out Result
        smoke = self.outcome()
        self.check(smoke, [(1, "dev_001", "candidate_behavior_observed"), (2, "dev_001", "candidate_behavior_observed")],
                   "candidate_behavior_observed", "candidate", 40)
        self.assertEqual(smoke["sources"]["dev_rounds"], str(self.run_dir / "lifecycle"))
        self.assertEqual(smoke["contracts"], {"result": str(self.run_dir / "readiness_scoring/result/test_001/"
                                                                           "result_score_contract.json")})

    def test_codex(self):
        records = []
        for n in (1, 2):
            write(self.run_dir / f"evaluations/submission_{n:03d}/dev_001/result.json",
                  {"case_id": "dev_001", "classification": "candidate_valid", "infrastructure_invalid": False,
                   "contract_valid": True})
            records.append({"submission_number": n, "state": "completed", "dev_scores": {"dev_001": None},
                            "dev_results": {"dev_001": f"{ORIGINAL}/evaluations/submission_{n:03d}/dev_001/result.json"}})
        write(self.run_dir / "dev_lifecycle.json", records)
        write(self.run_dir / "readiness_hidden_attestation.json", {"case": "test_001", "execution_valid": True, "result": {
            "case_id": "test_001", "classification": "candidate_valid", "infrastructure_invalid": False,
            "failure_attribution": {"party": "candidate"}}})
        write(self.run_dir / "readiness_summary.json", {"complete": False, "errors": ["x"]})
        self.contract(0, result_state="fatal_candidate_failure")
        smoke = self.outcome()
        self.check(smoke, [(1, "dev_001", "candidate_valid"), (2, "dev_001", "candidate_valid")],
                   "candidate_valid", "candidate", 0)
        self.assertEqual(smoke["status"], "readiness_incomplete")

    def test_deepcode(self):
        write(self.run_dir / "lifecycle/controller_state.json", {"frozen": {}, "records": [
            {"round": n, "accepted": True, "dev": [{"case_id": "dev_001", "classification": "candidate_valid",
                                                    "infra_valid": True, "score": None}]} for n in (1, 2)]})
        write(self.run_dir / "lifecycle/hidden-after-freeze-attestation.json", {"cases": [
            {"case_id": "test_001", "classification": "candidate_valid", "infra_valid": True,
             "failure_attribution": {"party": "candidate"}}]})
        write(self.run_dir / "summary.json", {"status": "pilot_pipeline_complete",
                                              "hidden_attestation": f"{ORIGINAL}/lifecycle/hidden-after-freeze-attestation.json"})
        write(self.run_dir / "one_stop_summary.json", {"dev_lifecycle": f"{ORIGINAL}/lifecycle/controller_state.json",
                                                       "result_axis": "N/A"})
        self.contract(55)
        self.check(self.outcome(), [(1, "dev_001", "candidate_valid"), (2, "dev_001", "candidate_valid")],
                   "candidate_valid", "candidate", 55)

    def test_dyad(self):
        attribution = {"party": "candidate", "fatal": True, "reason": "The Candidate exhausted the case budget."}
        write(self.run_dir / "lifecycle/dev_lifecycle.json", [
            {"submission_number": n, "dev": [{"case_id": "dev_001", "classification": "candidate_timeout",
                                              "infra_valid": True, "failure_attribution": attribution}]} for n in (1, 2)])
        write(self.run_dir / "hidden-after-freeze-attestation.json", {"cases": {"test_001": {
            "case_id": "test_001", "classification": "candidate_failure", "infra_valid": True,
            "failure_attribution": attribution}}})
        write(self.run_dir / "summary.json", {"status": "readiness_evidence_complete",
                                              "hidden_attestation": f"{ORIGINAL}/hidden-after-freeze-attestation.json"})
        self.contract(5)
        self.check(self.outcome(), [(1, "dev_001", "candidate_timeout"), (2, "dev_001", "candidate_timeout")],
                   "candidate_failure", "candidate", 5)

    def test_openclaw(self):
        write(self.run_dir / "lifecycle/dev_lifecycle.json", {"records": [
            {"classification": "completed", "dev": {"dev_001": {"case_id": "dev_001", "classification": c}}}
            for c in ("candidate_behavior_observed", "candidate_product_failure")]})
        # the attestation proves ordering only; the case record is in hidden_result.json
        write(self.run_dir / "lifecycle/hidden-after-freeze-attestation.json", {"case_ids": ["test_001"],
                                                                                "freeze_before_hidden": True})
        write(self.run_dir / "lifecycle/hidden_result.json", {"test_001": {
            "case_id": "test_001", "classification": "candidate_behavior_observed",
            "classification_reason": "real OpenClaw run produced a valid case-bound terminal artifact"}})
        write(self.run_dir / "one_stop_summary.json", {"status": "readiness_evidence_complete",
                                                       "dev_lifecycle": "lifecycle/dev_lifecycle.json",
                                                       "hidden_summary": "N/A"})
        write(self.run_dir / "summary.json", {"status": "readiness_evidence_complete"})
        self.contract(71)
        smoke = self.outcome()
        self.check(smoke, [(1, "dev_001", "candidate_behavior_observed"), (2, "dev_001", "candidate_product_failure")],
                   "candidate_behavior_observed", None, 71)
        self.assertEqual(smoke["sources"]["held_out"], str(self.run_dir / "lifecycle/hidden_result.json"))

    def test_openhands_names_the_judged_result_not_the_agentloop_score(self):
        write(self.run_dir / "lifecycle/dev_lifecycle.json", {"dev_cases": ["dev_001"], "records": [
            {"accepted": True, "dev_scores": {"dev_001": 100}, "dev": {"dev_001": {
                "case_id": "dev_001", "classification": "valid_behavior",
                "failure_attribution": {"party": "candidate_behavior", "infrastructure_invalid": False}}}}
            for _ in (1, 2)]})
        write(self.run_dir / "lifecycle/hidden/test_001/case_result.json", {
            "case_id": "test_001", "failure_attribution": {"party": "candidate_behavior", "infrastructure_invalid": False}})
        write(self.run_dir / "lifecycle/pilot-hidden-after-freeze-attestation.json", {
            "cases": [{"case_id": "test_001", "result_path": f"{ORIGINAL}/lifecycle/hidden/test_001/case_result.json"}],
            "classifications": {"test_001": "candidate_behavior_failure"}})
        write(self.run_dir / "summary.json", {
            "status": "pilot_complete", "hidden_attestation": f"{ORIGINAL}/lifecycle/pilot-hidden-after-freeze-attestation.json",
            "pilot_evaluation": {"scores": {"test_001": {"score": 100,
                                                         "schema_version": "agentswe-openhands-agentloop-result-score/v1"}}}})
        self.contract(17)
        smoke = self.outcome()
        case = self.check(smoke, [(1, "dev_001", "valid_behavior"), (2, "dev_001", "valid_behavior")],
                          "candidate_behavior_failure", "candidate_behavior", 17)
        self.assertTrue(case["infrastructure_valid"])
        self.assertIn("pilot_evaluation", smoke["note"])
        self.assertEqual(smoke["sources"]["held_out"],
                         str(self.run_dir / "lifecycle/pilot-hidden-after-freeze-attestation.json"))


class SmokeShapesOfTheReleaseSmokes(unittest.TestCase):
    """Shapes real smokes recorded that the layouts above do not: each tree's own validity field,
    OpenClaw's pilot hidden suite in summary.json, AI-Scientist's launcher record, Dyad's `attribution`, and a Claude
    run that ended because its last submission was not consumed for an infrastructure failure."""
    setUp, contract, outcome, check = SmokeLayouts.setUp, SmokeLayouts.contract, SmokeLayouts.outcome, SmokeLayouts.check

    def held(self, smoke):
        (case,) = smoke["held_out"]
        return case

    def test_each_tree_names_its_validity_field(self):
        cases = {"aider": {"classification": "candidate_valid", "artifact_validation": {"valid": True},
                           "readiness_execution_valid": True},
                 "codex": {"classification": "candidate_timeout", "validity_gate": False,
                           "artifact_validation": {"valid": False}, "readiness_execution_valid": True},
                 "openhands": {"classification": "valid_behavior",
                               "failure_attribution": {"party": "candidate_behavior", "candidate_result_valid": True}},
                 "deepcode": {"classification": "candidate_partial", "lower_result_valid": True},
                 "claude": {"classification": "candidate_behavior_observed", "environment_preflight": {"valid": True}}}
        expected = {"aider": (True, "artifact_validation.valid"), "codex": (False, "validity_gate"),
                    "openhands": (True, "failure_attribution.candidate_result_valid"),
                    "deepcode": (True, "lower_result_valid"), "claude": (None, None)}
        for key, case in cases.items():
            with self.subTest(key):
                self.assertEqual(ed._case_validity(case), expected[key])
        write(self.run_dir / "dev_lifecycle.json", [{"submission_number": 1, "dev_results": {"dev_001": dict(
            cases["codex"], case_id="dev_001", infrastructure_invalid=False)}}])
        (row,) = self.outcome()["dev_rounds"]
        self.assertEqual((row["valid"], row["valid_field"], row["infrastructure_valid"]), (False, "validity_gate", True))

    def test_openclaw_pilot_hidden_suite_in_the_summary(self):
        case = {"case_id": "test_001", "classification": "candidate_behavior_observed",
                "artifact_contract": {"valid": True}, "native_case": {"infrastructure_errors": []}}
        write(self.run_dir / "lifecycle/dev_lifecycle.json", {"records": [
            {"number": 1, "classification": "completed", "dev": {"dev_001": dict(case, case_id="dev_001")}}]})
        write(self.run_dir / "lifecycle/hidden-after-freeze-attestation.json", {"case_ids": ["test_001"]})
        write(self.run_dir / "pilot_hidden/summary.json", {"cases": {"test_001": dict(case, classification="stale")}})
        write(self.run_dir / "summary.json", {"status": "readiness_evidence_complete",
                                              "pilot_hidden": {"executed_count": 1, "cases": {"test_001": case}}})
        self.contract(40)
        smoke = self.outcome()
        held = self.check(smoke, [(1, "dev_001", "candidate_behavior_observed")], "candidate_behavior_observed", None, 40)
        self.assertEqual((held["valid"], held["valid_field"], held["infrastructure_valid"]),
                         (True, "artifact_contract.valid", True))
        self.assertEqual(smoke["dev_rounds"][0]["infrastructure_valid"], True)
        self.assertEqual(smoke["sources"]["held_out"], f"{self.run_dir / 'summary.json'}#pilot_hidden")
        # without the summary's record: the latest retried suite, then the first suite
        write(self.run_dir / "summary.json", {"status": "readiness_evidence_complete",
                                              "pilot_hidden": {"status": "not_started"}})
        self.assertEqual(self.outcome()["sources"]["held_out"], str(self.run_dir / "pilot_hidden/summary.json"))
        write(self.run_dir / "pilot_hidden_retry_001/summary.json", {"cases": {"test_001": case}})
        smoke = self.outcome()
        self.assertEqual(smoke["sources"]["held_out"], str(self.run_dir / "pilot_hidden_retry_001/summary.json"))
        self.assertEqual(self.held(smoke)["classification"], "candidate_behavior_observed")
        self.assertEqual(ed._infrastructure_valid({"native_case": {"infrastructure_errors": ["seed failed"]}}), False)

    def test_ai_scientist_held_out_case_adds_its_launcher_record(self):
        write(self.run_dir / "lifecycle/hidden/test_001/launcher_result.json", {
            "case_id": "test_001", "classification": "launcher-view", "valid": False, "classification_axis": "candidate"})
        write(self.run_dir / "lifecycle/hidden-after-freeze-attestation.json", {"cases": [
            {"case_id": "test_001", "classification": "candidate_behavior_failure", "classification_axis": "candidate",
             "result_path": f"{ORIGINAL}/lifecycle/hidden/test_001/launcher_result.json"}]})
        write(self.run_dir / "summary.json", {"status": "pilot_pipeline_complete"})
        self.contract(18)
        held = self.held(self.outcome())
        self.assertEqual((held["classification"], held["valid"], held["valid_field"], held["failure_party"]),
                         ("candidate_behavior_failure", False, "valid", "candidate"))  # the attestation's own fields win

    def test_dyad_attribution_owner(self):
        def case(classification, owner):
            return {"case_id": "test_001", "classification": classification, "infra_valid": True,
                    "failure_class": None if classification == "valid" else "deadline",
                    "attribution": {"owner": owner, "candidate_behavior_evaluable": True},
                    "artifact_validation": {"valid": classification == "valid"}}
        self.assertEqual(ed._failure_party(case("valid", "evaluator/provider")), (None, None))  # no failure
        self.assertEqual(ed._failure_party(case("candidate_failure", "candidate")), ("candidate", "deadline"))
        write(self.run_dir / "hidden-after-freeze-attestation.json", {"cases": {"test_001": case("valid", "evaluator/provider")}})
        write(self.run_dir / "summary.json", {"status": "readiness_evidence_complete"})
        self.contract(20)
        held = self.held(self.outcome())
        self.assertEqual((held["valid"], held["valid_field"], held["failure_party"]),
                         (True, "artifact_validation.valid", None))

    def claude_run(self, events):
        write(self.run_dir / "lifecycle/round_001.json", {"accepted": True, "submission_number": 1, "dev": [
            {"case_id": "dev_001", "classification": "candidate_behavior_observed", "infrastructure_invalid": False,
             "failure_attribution": {"party": "candidate"}}]})
        write(self.run_dir / "summary.json", {"status": "pilot_builder_integration_incomplete",
                                              "classification": "latest_candidate_freeze_gate_failure"})
        (self.run_dir / "builder_observer_events.jsonl").write_text("".join(json.dumps(e) + "\n" for e in events))
        return self.outcome()

    def test_claude_last_submission_not_consumed_for_infrastructure(self):
        finished = {"event": "submission_finished", "candidate_number": 1, "accepted": True}
        lost = [{"event": "submission_not_consumed", "at": "t1", "candidate_number": 2,
                 "classification": "public_or_launcher_infrastructure_failure", "error_type": "OSError",
                 "error_detail": "case aggregate cleanup or total wall budget could not be verified"},
                {"event": "submission_not_consumed", "at": "t2", "candidate_number": 2,
                 "classification": "public_or_launcher_infrastructure_failure", "error_type": "RuntimeError",
                 "error_detail": "prior Candidate worktree remains; preserve its unfinished evidence"}]
        smoke = self.claude_run([{"event": "builder_invocation_started"}, finished, *lost,
                                 {"event": "builder_invocation_finished", "exit_code": 0}])
        self.assertEqual(smoke["held_out"], [])
        self.assertEqual([(r["round"], r["classification"]) for r in smoke["dev_rounds"]],
                         [(1, "candidate_behavior_observed")])
        gate = smoke["infrastructure_gate"]
        self.assertEqual((gate["summary"], gate["source"], gate["status"], gate["classification"],
                          gate["summary_classification"]),
                         (None, str(self.run_dir / "builder_observer_events.jsonl"), "pilot_builder_integration_incomplete",
                          "public_or_launcher_infrastructure_failure", "latest_candidate_freeze_gate_failure"))
        self.assertEqual(gate["reason"], "RuntimeError: prior Candidate worktree remains; preserve its unfinished evidence")
        self.assertEqual([e["error_type"] for e in gate["unconsumed_submissions"]], ["OSError", "RuntimeError"])
        # a later accepted submission: the run did not end on that failure
        self.assertNotIn("infrastructure_gate", self.claude_run([*lost, dict(finished, candidate_number=2)]))
        # the 2026-10-10 Claude tree names it in the summary; the events add the reason the summary does not carry
        self.claude_run([finished, *lost])
        write(self.run_dir / "summary.json", {"status": "pilot_builder_integration_incomplete",
                                              "classification": "public_or_launcher_infrastructure_failure"})
        gate = self.outcome()["infrastructure_gate"]
        self.assertEqual((gate["summary"], gate["reason"]), ("summary.json", "RuntimeError: prior Candidate worktree "
                                                                           "remains; preserve its unfinished evidence"))


if __name__ == "__main__":
    unittest.main()
