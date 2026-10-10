from __future__ import annotations
import hashlib
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "lower_agent"))
import openhands_lower_agent as lower
sys.path.insert(0, str(ROOT / "evaluator/controller"))
from two_round_controller import TwoRoundController, classify_launch


class RuntimeRepairTests(unittest.TestCase):
    def test_driver_routes_public_api_to_its_declared_product_surface(self):
        driver = lower.js_driver("test_001", "user task", "a" * 32, Path("/output/agent_result.json"))
        self.assertIn('case "create_checkpoint": return adapter.createCheckpoint(args)', driver)
        self.assertIn('case "acquire_recovery_lease": return adapter.acquireRecoveryLease(args)', driver)
        self.assertIn('AgentServerRuntimeService as any).executeRecoverableEffect(', driver)
        self.assertNotIn('productCall("createCheckpoint"', driver)
        self.assertIn('productCall("inspectRecovery"', driver)
        self.assertIn('productCall("createRecoveryEventDispatcher"', driver)

    def test_timeout_is_attributed_to_observed_stage(self):
        for phase, expected in (("model_request", "provider_failure"), ("artifact_model_request", "provider_failure"), ("product_action", "candidate_behavior_failure"), ("startup", "lower_agent_infrastructure_failure")):
            self.assertEqual(lower.classify_process(124, "timeout", {"phase": phase}, None, None), expected)

    def test_error_sanitizer_drops_values_not_just_labels(self):
        result = lower.redact_runtime_text("GATEWAY_API_KEY=synthetic-secret Bearer synthetic-token")
        self.assertNotIn("synthetic-secret", result)
        self.assertNotIn("synthetic-token", result)

    def test_rejected_product_action_can_be_honestly_reported_and_scored(self):
        steps = [{"action": "inspect_recovery", "dispatched": False, "attempted": True, "product_method": None, "observation": {"status": "action_rejected", "error_code": "TypeError", "message": "Candidate method is missing"}}]
        digest = hashlib.sha256(lower.canonical_json(steps).encode()).hexdigest()
        artifact = {"schema_version": "agentswe-openhands-agent-result/v1", "case_id": "test_001", "model": lower.MODEL, "reasoning_effort": lower.EFFORT, "product": lower.PRODUCT_NAME, "product_entry": lower.PRODUCT_ENTRY, "actions": ["inspect_recovery"], "observations": [{"action": "inspect_recovery", "result": steps[0]["observation"]}], "trajectory_digest": digest, "nonce_digest": "a" * 64, "decision": "blocked", "rationale": "The required Candidate method is absent."}
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            (root / "trajectory.json").write_text(json.dumps({"schema_version": "agentswe-openhands-trajectory/v1", "case_id": "test_001", "product_entry": lower.PRODUCT_ENTRY, "steps": steps, "trajectory_digest": digest}))
            self.assertTrue(lower.validate_trajectory_binding(root, "test_001", artifact)["bound"])
            artifact["observations"][0]["result"] = {"status": "success"}
            binding = lower.validate_trajectory_binding(root, "test_001", artifact)
            self.assertEqual(binding["author_factual_claim_mismatches"][0]["field"], "observations")
            self.assertFalse(binding["author_factual_claim_mismatches"][0]["origin_failure"])
            artifact["trajectory_digest"] = "f" * 64
            with self.assertRaises(ValueError): lower.validate_trajectory_binding(root, "test_001", artifact)

    def test_recovered_transient_broker_failure_does_not_override_product_evidence(self):
        artifact = {"schema_version": "agentswe-openhands-agent-result/v1", "case_id": "dev_001", "model": "gpt-5.6-sol", "reasoning_effort": "high", "actions": ["inspect_recovery"], "observations": [], "decision": "complete", "rationale": "observed"}
        self.assertEqual(classify_launch(exit_code=0, launcher={"classification": "candidate_behavior_observed"}, artifact=artifact, broker_delta={"calls": 3, "failures": 1}), "valid_behavior")

    def test_dev_round_uses_semantic_scores_not_validity_bonus(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp); candidate = root / "candidate"; candidate.mkdir(); (candidate / "code.ts").write_text("fixture")
            controller = TwoRoundController(candidate, root / "run", [], case_root=ROOT,
                                            broker_endpoint="http://lower.invalid", node_modules=root / "deps",
                                            result_judge_endpoint="http://judge.invalid")
            def result(_candidate, case, _output, **_kwargs):
                return {"case_id": case, "classification": "valid_behavior", "artifact_present": True,
                        "failure_attribution": {}, "result_evaluation": {"classification": "scoreable", "score": 41 if case == "dev_001" else 59,
                            "contract_valid": True, "round_consumed": True, "feedback": {"assessment": "actual semantic evidence"}}}
            with patch.object(controller, "_run_public_case", side_effect=result):
                record = controller.submit()
            self.assertTrue(record["accepted"])
            self.assertEqual(record["dev_scores"], {"dev_001": 41, "dev_002": 59})
            self.assertEqual(record["dev_mean"], 50)

    def test_no_semantic_contract_does_not_consume_dev_round(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp); candidate = root / "candidate"; candidate.mkdir(); (candidate / "code.ts").write_text("fixture")
            controller = TwoRoundController(candidate, root / "run", [], case_root=ROOT,
                                            broker_endpoint="http://lower.invalid", node_modules=root / "deps")
            with patch.object(controller, "_run_public_case", return_value={"classification": "valid_behavior"}):
                record = controller.submit()
            self.assertFalse(record["accepted"])
            self.assertFalse(record["round_consumed"])
            self.assertEqual(controller.records, [])


if __name__ == "__main__": unittest.main()
