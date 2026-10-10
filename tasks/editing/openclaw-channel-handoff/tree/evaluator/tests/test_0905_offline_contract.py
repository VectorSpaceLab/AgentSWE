from __future__ import annotations
import json, subprocess, sys, tempfile, unittest
from pathlib import Path
ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "evaluator"))
from semantic_finalize import artifact_for, is_infrastructure, validate_result_contract

class OfflineContractTests(unittest.TestCase):
    def test_candidate_and_infrastructure_classification(self):
        self.assertFalse(is_infrastructure({"classification": "candidate_no_model_call", "classification_axis": "candidate"}))
        self.assertTrue(is_infrastructure({"classification": "credential_infrastructure_error", "classification_axis": "infrastructure"}))
    def test_foreign_artifact_rejected(self):
        with tempfile.TemporaryDirectory() as td:
            run=Path(td); base=run/"hidden/test_001"; base.mkdir(parents=True); (base/"agent_result.json").write_text(json.dumps({"case_id":"test_002"}))
            with self.assertRaises(ValueError): artifact_for("openclaw",run,"test_001",base/"case_result.json",{})
    def test_result_contract_usage_gate(self):
        with tempfile.TemporaryDirectory() as td:
            p=Path(td)/"c.json"; p.write_text(json.dumps({"contract_valid":True,"result_score_publishable":True,"case_id":"test_001","result_score":0,"judge":{"model":"gpt-5.6-sol","reasoning_effort":"max"},"provider_usage":{"logical_requests":1,"completed_responses":0}}))
            self.assertIsNotNone(validate_result_contract(p,"test_001")[1])
    def test_common_cli_rejects_invalid_round_limit(self):
        c=subprocess.run([sys.executable,str(ROOT/"harbor/formal_one_stop.py"),"--max-dev-rounds","11"],text=True,capture_output=True); self.assertEqual(c.returncode,2)
if __name__ == "__main__": unittest.main()
