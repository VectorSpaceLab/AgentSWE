from __future__ import annotations
import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from audit_readiness import tree_digest, sha256

ROOT = Path(__file__).resolve().parent
VALIDATOR = ROOT / "validate_preflight_smoke_plan.py"
PLAN = ROOT / "preflight_smoke_plan.json"
MATRIX = ROOT / "case_coverage_matrix.json"
GATE = ROOT / "formal_readiness_gate.json"

class PreflightSmokePlanTests(unittest.TestCase):
    def run_validator(self, directory: Path) -> tuple[int, dict]:
        out = directory / "receipt.json"
        proc = subprocess.run(
            [sys.executable, str(VALIDATOR), "--plan", str(directory / "plan.json"),
             "--matrix", str(directory / "matrix.json"), "--gate", str(directory / "gate.json"),
             "--output", str(out)], text=True, capture_output=True, check=False,
        )
        return proc.returncode, json.loads(out.read_text(encoding="utf-8"))

    def seed(self, directory: Path) -> None:
        plan = json.loads(PLAN.read_text())
        matrix = json.loads(MATRIX.read_text())
        gate = json.loads(GATE.read_text())
        by_name = {row['task']: row for row in plan['tasks']}
        for row in matrix['tasks']:
            sibling = directory / row['task']
            sibling.mkdir()
            (sibling / 'runtime.py').write_text('VERSION = 1\n')
            contract = sibling / 'contract.json'
            contract.write_text(json.dumps({'task':row['task']}))
            row.update(sibling=str(sibling), sibling_digest=tree_digest(sibling),
                       contract_path=str(contract), contract_digest=sha256(contract))
            by_name[row['task']]['sibling_digest'] = row['sibling_digest']
        plan['gate_snapshot'] = {key:gate.get(key) for key in (
            'ready_count','expected_count','formal_ready','formal_launch_authorized','formal_evaluation_started')}
        for value, filename in ((plan,'plan.json'),(matrix,'matrix.json'),(gate,'gate.json')):
            (directory / filename).write_text(json.dumps(value))

    def test_current_plan_is_valid(self):
        with tempfile.TemporaryDirectory() as raw:
            d = Path(raw); self.seed(d)
            code, receipt = self.run_validator(d)
            self.assertEqual(code, 0)
            self.assertTrue(receipt["valid"])
            self.assertEqual(receipt["provider_calls"], 0)
            self.assertEqual(receipt["task_count"], 10)

    def test_sibling_digest_drift_is_rejected(self):
        with tempfile.TemporaryDirectory() as raw:
            d = Path(raw); self.seed(d)
            plan = json.loads((d / "plan.json").read_text())
            plan["tasks"][0]["sibling_digest"] = "drift"
            (d / "plan.json").write_text(json.dumps(plan))
            code, receipt = self.run_validator(d)
            self.assertNotEqual(code, 0)
            self.assertFalse(receipt["valid"])
            self.assertIn("deeptutor: sibling_digest drift", receipt["errors"])

    def test_gate_authorization_drift_is_rejected(self):
        with tempfile.TemporaryDirectory() as raw:
            d = Path(raw); self.seed(d)
            gate = json.loads((d / "gate.json").read_text())
            gate["formal_ready"] = True
            (d / "gate.json").write_text(json.dumps(gate))
            code, receipt = self.run_validator(d)
            self.assertNotEqual(code, 0)
            self.assertFalse(receipt["valid"])
            self.assertIn("gate drift: formal_ready", receipt["errors"])

    def test_actual_source_drift_rejected_when_metadata_agrees(self):
        with tempfile.TemporaryDirectory() as raw:
            d = Path(raw); self.seed(d)
            (d / 'claude' / 'runtime.py').write_text('VERSION = 2\n')
            code, receipt = self.run_validator(d)
            self.assertNotEqual(code, 0)
            self.assertIn('claude: actual sibling digest drift', receipt['errors'])

    def test_actual_contract_drift_is_rejected(self):
        with tempfile.TemporaryDirectory() as raw:
            d = Path(raw); self.seed(d)
            (d / 'claude' / 'contract.json').write_text('{"task":"wrong"}')
            code, receipt = self.run_validator(d)
            self.assertNotEqual(code, 0)
            self.assertIn('claude: actual contract digest drift', receipt['errors'])

if __name__ == "__main__":
    unittest.main()
