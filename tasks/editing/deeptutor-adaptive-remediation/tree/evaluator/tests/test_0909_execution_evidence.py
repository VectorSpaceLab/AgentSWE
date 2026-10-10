import json
import tempfile
import unittest
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from agentloop.execution_evidence import artifact_authorship, attest_execution


class ExecutionEvidenceTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.launcher = {"executed": True, "exit_code": 0, "runtime_probe": {"infra_valid": True}, "transport_preflight": {"valid": True}}
        (self.root / "launcher_result.json").write_text(json.dumps(self.launcher))
        self.record = {"classification": "candidate_valid", "infra_valid": True,
                       "broker": {"delta": {"calls": 2, "successful_calls": 2}}}

    def tearDown(self):
        self.temp.cleanup()

    def authored(self):
        artifact = {"schema_version": "v1", "case_id": "test_001", "status": "incomplete",
                    "summary": "Could not safely finish.", "artifacts": {"observed": []}}
        (self.root / "agent_result.json").write_text(json.dumps(artifact))
        (self.root / "trajectory.jsonl").write_text(json.dumps({"type": "result", "metadata": {"response": json.dumps(artifact)}}))

    def test_honest_incomplete_is_scoreable_not_zero(self):
        self.authored()
        result = attest_execution(self.record, self.launcher, output=self.root, case_id="test_001", candidate_digest="abc")
        self.assertTrue(result["artifact_validation"]["valid"])
        self.assertTrue(result["real_execution"])
        self.assertEqual(result["broker_delta"]["calls"], 2)
        self.assertNotIn("failure_attribution", result)

    def test_parseable_but_not_authored_artifact_is_not_valid(self):
        self.authored()
        (self.root / "trajectory.jsonl").write_text('{"type":"tool_result"}')
        self.assertFalse(artifact_authorship(self.root, "test_001")["valid"])

    def test_observed_terminal_missing_artifact_is_candidate_failure(self):
        result = attest_execution(self.record, self.launcher, output=self.root, case_id="test_001", candidate_digest="abc")
        self.assertEqual(result["classification"], "candidate_artifact_failure")
        self.assertTrue(result["failure_attribution"]["fatal"])

    def test_unknown_missing_launcher_never_becomes_candidate_zero(self):
        result = attest_execution(self.record, {}, output=self.root, case_id="test_001", candidate_digest="abc")
        self.assertNotIn("failure_attribution", result)
        self.assertFalse(result["execution_attempted"])

    def test_infrastructure_remains_dominant(self):
        result = attest_execution({**self.record, "classification": "provider_infrastructure_error", "infra_valid": False},
            self.launcher, output=self.root, case_id="test_001", candidate_digest="abc")
        self.assertEqual(result["classification"], "provider_infrastructure_error")
        self.assertNotIn("failure_attribution", result)

    def test_fixture_failure_is_not_a_lower_agent_execution(self):
        result = attest_execution({**self.record, "classification": "candidate_capability_gap"},
            {"executed": False, "runtime_probe": {"infra_valid": True}}, output=self.root, case_id="test_001", candidate_digest="abc")
        self.assertFalse(result["execution_attempted"])
        self.assertNotIn("failure_attribution", result)

if __name__ == "__main__":
    unittest.main()
