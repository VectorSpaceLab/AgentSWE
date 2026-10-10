from __future__ import annotations

import importlib.util
import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
SCRIPT = ROOT / "evaluator/formal_finalize.py"


class FormalFinalizeNegativeControls(unittest.TestCase):
    def test_partial_run_stays_na(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            run = Path(tmp) / "run"; run.mkdir()
            completed = subprocess.run([sys.executable, str(SCRIPT), "--run-dir", str(run)], capture_output=True, text=True)
            value = json.loads((run / "formal_aggregation.json").read_text())
            self.assertEqual(completed.returncode, 2)
            self.assertFalse(value["formal_result_publishable"])
            self.assertEqual(value["result_axis"], "N/A")
            self.assertEqual(value["code_axis"], "N/A")

    def test_static_code_precheck_is_rejected(self) -> None:
        spec = importlib.util.spec_from_file_location("formal_finalize", SCRIPT)
        module = importlib.util.module_from_spec(spec); assert spec and spec.loader; spec.loader.exec_module(module)
        with tempfile.TemporaryDirectory() as tmp:
            run = Path(tmp); contract = run / "code.json"
            contract.write_text(json.dumps({"axis": "Code", "total": 100, "maximum": 100, "source_evidence": ["x"], "formal_judge": False, "scoring_mode": "static_precheck"}))
            value, publishable, reasons = module.code_contract(contract, run)
            self.assertEqual(value, "N/A"); self.assertFalse(publishable); self.assertTrue(reasons)


if __name__ == "__main__": unittest.main()
