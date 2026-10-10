#!/usr/bin/env python3
"""The smoke block of `agentswe result` says why an OpenHands smoke stopped at its public infrastructure gate
(stdlib unittest; no docker, no network). OpenHands records that stop only in lifecycle/dev_lifecycle.json."""
from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from agentswe.runners import editing_agentloop_v1 as ed  # noqa: E402


class OpenHandsInfrastructureGate(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.run_dir = Path(tmp.name)
        self.summaries = {"summary.json": {"status": "pilot_builder_integration_incomplete", "result_axis": "N/A"}}

    def lifecycle(self, phase, attempts):
        path = self.run_dir / "lifecycle" / "dev_lifecycle.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps({"phase": phase, "records": [], "infrastructure_attempts": attempts}))

    def test_gate_reason_from_the_dev_lifecycle(self):
        self.lifecycle("public_infrastructure_invalid", [
            {"submission": 1, "infrastructure_failures": {"dev_001": "candidate_product_failure"},
             "infrastructure_case_reasons": {"dev_001": "inspectRecovery threw"}},
            {"submission": 2, "infrastructure_failures": {"dev_001": "candidate_product_failure"},
             "infrastructure_case_reasons": {"dev_001": "no final artifact was written for this case"}}])
        gate = ed._infrastructure_gate(self.summaries, self.run_dir)
        self.assertEqual((gate["phase"], gate["attempts"], gate["status"]),
                         ("public_infrastructure_invalid", 2, "pilot_builder_integration_incomplete"))
        self.assertEqual(gate["reason"], {"dev_001": "no final artifact was written for this case"})
        self.assertEqual(gate["classification"], {"dev_001": "candidate_product_failure"})

    def test_no_gate_without_an_infrastructure_phase(self):
        self.lifecycle("frozen", [{"submission": 1, "infrastructure_case_reasons": {"dev_001": "transient"}}])
        self.assertIsNone(ed._infrastructure_gate(self.summaries, self.run_dir))
        self.lifecycle("public_infrastructure_invalid", [])
        self.assertIsNone(ed._infrastructure_gate(self.summaries, self.run_dir))


if __name__ == "__main__":
    unittest.main()
