import importlib.util
import json
import math
from pathlib import Path
import sys
import tempfile
import unittest
ROOT=Path(__file__).resolve().parents[2]
sys.path.insert(0,str(ROOT))
from evaluator.harness.semantic_oracle import expected_science, compare_values, observe
from evaluator.harness.execution_evidence import validate_artifact

class SemanticOracleTests(unittest.TestCase):
    def test_decay_private_formula_uses_units_and_broadcast(self):
        with tempfile.TemporaryDirectory() as raw:
            p=Path(raw); (p/"data").mkdir()
            (p/"data/times.json").write_text('[[0,1000,4000],[500,2000,8000]]')
            (p/"config.json").write_text('{"tau_seconds":[1,2,4]}')
            gold=expected_science("test_001",p)["artifact"]
            self.assertEqual(len(gold["weights"]),2)
            self.assertAlmostEqual(gold["weights"][0][1],math.exp(-0.5))
            wrong={"seed":3,"weights":[[1,1,1],[1,1,1]]}
            self.assertFalse(all(compare_values(gold,wrong).values()))
            self.assertTrue(all(compare_values(gold,gold).values()))

    def test_missing_numeric_artifact_is_not_a_provenance_pass(self):
        with tempfile.TemporaryDirectory() as raw:
            p=Path(raw)
            result=observe("test_005",project=p,workspace=p,home=p/"home")
            self.assertFalse(all(result["scientific_assertion_comparisons"].values()))
            self.assertFalse(result["evaluator_executed_product_actions"])

    def test_blocked_case_never_invents_missing_numeric_value(self):
        gold=expected_science("test_006",Path("/unused"))
        self.assertTrue(gold["numeric_artifact_must_not_be_fabricated"])
        self.assertNotIn("artifact",gold)
        self.assertEqual(gold["allowed_capsule_statuses"],["blocked"])

    def test_task_schema_not_generic_schema(self):
        with tempfile.TemporaryDirectory() as raw:
            p=Path(raw); artifact=p/"agent_result.json"; trajectory=p/"stdout.jsonl"
            value={"schema_version":"deepcode-agentloop-result/v1","case_id":"test_001", "observations":[],
                "tool_trajectory_summary":[],"state_receipts":[],"artifact_paths":[],
                "decision":{"completion_claim":"partial","rationale":"unresolved"},"safety":{}}
            artifact.write_text(json.dumps(value)); trajectory.write_text('{"msg":{"type":"agent_message","text":"incomplete"}}')
            self.assertEqual(validate_artifact(artifact,trajectory,"test_001"),[])
            self.assertEqual(validate_artifact(artifact,trajectory,"test_002"),[])  # wrong case field is a Result quality issue
            self.assertTrue(validate_artifact(artifact,trajectory,"test_001",preexisting=True))
            trajectory.write_text('{}')
            self.assertTrue(validate_artifact(artifact,trajectory,"test_001"))

if __name__=="__main__": unittest.main()
