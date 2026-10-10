"""`agentswe result` for an Editing --smoke run: the summaries say result_axis "N/A" and combined_score null, so the
result adds a `smoke` block read from the run dir's own files (dev-round classifications from the controller's
records, the held-out case from the hidden attestation and the readiness result contract, the failure party, the
contract paths, and the infrastructure-gate reason). The fixtures follow two smoke outcomes: DeepTutor (a
scored zero) and OpenWiki (stopped at the infrastructure gate). Their files record absolute paths of the run as it
ran; the fixtures record them under another root, as a copied run does (stdlib unittest, systemd mocked)."""
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

ORIGINAL_ROOT = "/srv/previous-host/runs/editing/smoke"


class Cfg:
    def __init__(self, home: Path):
        self.home = home

    def get(self, key, default=None):
        return default


def write(path: Path, value) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, indent=2))


class SmokeResult(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.home = Path(tmp.name)

    def launch(self, key, mode="smoke"):
        name = f"oss-{mode}-s1-20260101t000000z"
        run_dir = self.home / "runs" / "editing" / mode / key / name
        run_dir.mkdir(parents=True)
        return run_dir, f"{ORIGINAL_ROOT}/{key}/{name}", {
            "run_id": f"e-{key}-{name}", "task": key, "family": "editing", "mode": mode,
            "comparable": mode != "smoke", "builder": {"profile": "codex"},
            "unit": f"agentswe-oss-edit-{key}-{name}.service", "run_dir": str(run_dir),
            "log": str(self.home / "runs" / "editing" / f"{key}-{name}.launch.log")}

    def result(self, launch, unit_state="inactive", exit_status="0"):
        with mock.patch.object(ed, "_unit_state", return_value=unit_state), \
                mock.patch.object(ed.util, "out", return_value=exit_status):
            return ed.result(Cfg(self.home), launch)

    # DeepTutor: two dev rounds (candidate_valid, then candidate_product_failure), held-out test_001 scored 0
    def deeptutor(self):
        run_dir, original, launch = self.launch("deeptutor")
        dev = lambda classification, reason: {  # noqa: E731
            "dev_001": {"case_id": "dev_001", "classification": classification, "valid": True, "infra_valid": True,
                        "reason": reason, "score": None, "terminal": True, "judge_invoked": False}}
        write(run_dir / "lifecycle/controller_state.json", {
            "frozen": {"source_submission": 2}, "infrastructure_attempts": 0,
            "records": [{"round": 1, "consumed": True, "dev_results": dev("candidate_valid", "candidate_valid")},
                        {"round": 2, "consumed": True,
                         "dev_results": dev("candidate_product_failure", "candidate_agent_failure")}]})
        write(run_dir / "hidden/hidden-after-freeze-attestation.json", {
            "schema_version": "agentswe-deeptutor-hidden-attestation/v1", "expected_cases": ["test_001"],
            "infrastructure_case_count": 0, "candidate_fatal_cases": ["test_001"], "result_axis": "N/A",
            "results": [{"case_id": "test_001", "classification": "candidate_product_failure", "valid": True,
                         "infra_valid": True, "execution_phase": "product_case_precondition",
                         "failure_attribution": {"fatal": True, "observed_by": "evaluator", "party": "candidate",
                                                 "reason": "Candidate claim did not return the persisted delivery"}}]})
        write(run_dir / "readiness_scoring/result/result_score_contract.json", {
            "schema_version": "agentswe-edit-result-score-contract-v1", "case_id": "test_001",
            "evaluation_state": "scoreable", "contract_valid": True, "result_score_publishable": True,
            "result_state": "fatal_candidate_failure", "result_score": 0, "errors": []})
        write(run_dir / "readiness_judge_smoke.json", {
            "code_contract": None, "evaluation_mode": "readiness_smoke", "readiness_judges_complete": True,
            "result_contract": {"path": f"{original}/readiness_scoring/result/result_score_contract.json",
                                "sha256": "0" * 64}})
        write(run_dir / "summary.json", {
            "schema_version": "deeptutor-readiness-summary/v1", "status": "readiness_evidence_complete",
            "readiness_judge_smoke": "readiness_judge_smoke.json", "hidden_cases": ["test_001"]})
        write(run_dir / "one_stop_summary.json", {
            "schema_version": "agentswe-edit-one-stop-summary-v2", "status": "readiness_evidence_complete",
            "code_axis": "N/A", "result_axis": "N/A", "combined_score": None,
            "dev_lifecycle": f"{original}/lifecycle/controller_state.json",
            "hidden": {"inventory": ["test_001"], "summary": f"{original}/hidden/hidden-after-freeze-attestation.json"}})
        return run_dir, launch

    # OpenWiki: dev rounds scoreable (execution not valid), held-out test_001 infrastructure_invalid, party evaluator
    def openwiki(self):
        run_dir, original, launch = self.launch("openwiki")
        dev = {"dev_001": {"classification": "scoreable", "infrastructure_invalid": False,
                           "judgement": {"classification": "scoreable", "judge_invoked": False, "score": None},
                           "execution": {"classification": "candidate_product_success",
                                         "candidate_classification": "candidate_product_failure", "valid": False,
                                         "infrastructure_invalid": False}}}
        write(run_dir / "lifecycle/controller_state.json", {
            "schema_version": 2, "frozen": {"source_submission": 2},
            "records": [{"round": 1, "role": "initial", "dev_cases": dev},
                        {"round": 2, "role": "feedback_revision", "dev_cases": dev}]})
        case = {"case_id": "test_001", "classification": "infrastructure_invalid", "infra_valid": False,
                "infrastructure_invalid": True, "valid": False, "candidate_classification": "candidate_contract_failure",
                "failure_attribution": {"observed_by": "evaluator", "party": "evaluator", "reason":
                                        "isolated runtime/endpoint preflight or broker transport did not establish "
                                        "valid execution"}}
        for name in ("hidden-after-freeze-attestation.json", "pilot_hidden_after_freeze_attestation.json"):
            write(run_dir / name, {"schema_version": "openwiki-agentloop-pilot-hidden-attestation/v1",
                                   "expected_cases": ["test_001"], "cases": {"test_001": case},
                                   "run": dict(case, classification="evaluator_failure"), "result_axis": "N/A"})
        write(run_dir / "summary.json", {
            "schema_version": "openwiki-agentloop-pilot-summary/v1", "status": "pilot_pipeline_infrastructure_failure",
            "classification": "evaluator_infrastructure_error",
            "error": "ValueError: infrastructure-invalid hidden execution cannot enter judge smoke",
            "result_axis": "N/A", "code_axis": "N/A"})
        write(run_dir / "one_stop_summary.json", {
            "schema_version": "agentswe-edit-one-stop-summary-v2", "status": "pilot_pipeline_infrastructure_failure",
            "result_axis": "N/A", "combined_score": None, "result_judge_contracts": {},
            "hidden": {"inventory": ["test_001"], "summary": f"{original}/hidden-after-freeze-attestation.json"}})
        return run_dir, launch

    def test_scored_zero_smoke_reports_rounds_score_party_and_contract(self):
        run_dir, launch = self.deeptutor()
        res = self.result(launch)
        self.assertEqual(res["summaries"]["one_stop_summary.json"]["result_axis"], "N/A")  # existing keys kept
        self.assertEqual(res["mode"], "smoke")
        smoke = res["smoke"]
        self.assertEqual(smoke["status"], "readiness_evidence_complete")
        self.assertEqual([(r["round"], r["case_id"], r["classification"], r["valid"], r["infrastructure_valid"])
                          for r in smoke["dev_rounds"]],
                         [(1, "dev_001", "candidate_valid", True, True),
                          (2, "dev_001", "candidate_product_failure", True, True)])
        self.assertEqual(smoke["dev_rounds"][1]["reason"], "candidate_agent_failure")
        (held,) = smoke["held_out"]
        self.assertEqual((held["case_id"], held["state"], held["result_score"], held["result_score_source"]),
                         ("test_001", "fatal_candidate_failure", 0, "result_score_contract"))
        self.assertIn("not the Result", smoke["note"])
        self.assertEqual((held["evaluation_state"], held["contract_valid"]), ("scoreable", True))
        self.assertEqual(held["failure_reason"], "Candidate claim did not return the persisted delivery")
        self.assertEqual(smoke["failure_party"], "candidate")
        # the contract path the judge summary recorded, re-rooted in this (copied) run dir
        self.assertEqual(smoke["contracts"],
                         {"result": str(run_dir / "readiness_scoring/result/result_score_contract.json")})
        self.assertEqual(smoke["sources"]["held_out"], str(run_dir / "hidden/hidden-after-freeze-attestation.json"))
        self.assertNotIn("infrastructure_gate", smoke)
        written = json.loads(Path(launch["log"]).with_name(launch["run_id"] + ".result.json").read_text())
        self.assertEqual(written["smoke"]["held_out"][0]["result_score"], 0)

    def test_infrastructure_gate_smoke_reports_reason_and_evaluator_party(self):
        run_dir, launch = self.openwiki()
        res = self.result(launch, "failed", "2")
        smoke = res["smoke"]
        self.assertEqual(smoke["status"], "pilot_pipeline_infrastructure_failure")
        self.assertEqual([(r["round"], r["classification"], r["valid"], r["infrastructure_valid"],
                           r["candidate_classification"]) for r in smoke["dev_rounds"]],
                         [(1, "scoreable", False, True, "candidate_product_failure"),
                          (2, "scoreable", False, True, "candidate_product_failure")])
        (held,) = smoke["held_out"]
        self.assertEqual((held["case_id"], held["state"], held["result_score"], held["result_score_source"]),
                         ("test_001", "infrastructure_invalid", None, None))
        self.assertIs(held["infrastructure_valid"], False)
        self.assertEqual(smoke["failure_party"], "evaluator")
        self.assertEqual(smoke["contracts"], {})
        self.assertEqual(smoke["infrastructure_gate"], {
            "summary": "summary.json", "status": "pilot_pipeline_infrastructure_failure",
            "classification": "evaluator_infrastructure_error",
            "reason": "ValueError: infrastructure-invalid hidden execution cannot enter judge smoke"})
        self.assertEqual(smoke["sources"]["held_out"], str(run_dir / "hidden-after-freeze-attestation.json"))

    def test_formal_runs_have_no_smoke_block(self):
        run_dir, _, launch = self.launch("deeptutor", mode="formal")
        write(run_dir / "summary.json", {"status": "formal_evidence_complete"})
        self.assertNotIn("smoke", self.result(launch))

    def test_a_smoke_without_records_reports_what_is_missing(self):
        _, _, launch = self.launch("aider")
        smoke = self.result(launch, "failed", "2")["smoke"]
        self.assertEqual((smoke["dev_rounds"], smoke["held_out"], smoke["failure_party"], smoke["contracts"]),
                         ([], [], None, {}))
        self.assertEqual(smoke["sources"], {"dev_rounds": None, "held_out": None})

    def test_only_paths_inside_the_run_dir_are_read(self):
        run_dir = self.home / "runs" / "editing" / "smoke" / "x" / "oss-smoke-s1-t"
        self.assertEqual(ed._in_run(run_dir, "a/b.json"), run_dir / "a/b.json")
        self.assertEqual(ed._in_run(run_dir, str(run_dir / "a.json")), run_dir / "a.json")
        self.assertEqual(ed._in_run(run_dir, "/other/root/oss-smoke-s1-t/hidden/a.json"), run_dir / "hidden/a.json")
        self.assertIsNone(ed._in_run(run_dir, "/other/root/another-run/a.json"))
        self.assertIsNone(ed._in_run(run_dir, "/other/oss-smoke-s1-t/../../etc/passwd"))
        self.assertIsNone(ed._in_run(run_dir, None))


if __name__ == "__main__":
    unittest.main()
