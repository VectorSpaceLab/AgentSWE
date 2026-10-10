#!/usr/bin/env python3
"""agentswe result names how a frozen formal Editing run's development ended when its summary records no reason
(stdlib unittest; no docker, no network). DeepTutor's freeze manifest carries no reason, so one_stop_summary.json
shows freeze.reason null; the result reads the Builder record, or the budget freeze record when there is one."""
from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from agentswe.runners import editing_agentloop_v1 as ed  # noqa: E402


def write(path: Path, value) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value))


class DerivedFreezeReason(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.run_dir = Path(tmp.name)
        self.summaries = {"one_stop_summary.json": {"freeze": {"digest": "fc40", "reason": None, "source_submission": 5}}}

    def test_builder_exit_from_the_builder_record(self):
        write(self.run_dir / "builder_process.json", {"exit_code": 0, "timed_out": False})
        self.assertEqual(ed.derived_freeze_reason(self.run_dir, self.summaries),
                         {"reason": "builder_exit", "source": "builder_process.json"})

    def test_budget_freeze_record_wins(self):
        write(self.run_dir / "builder_process.json", {"exit_code": 0, "timed_out": False})
        write(self.run_dir / "manual_freeze" / "manual_freeze_record.json",
              {"reason": "budget_exhausted_freeze_latest_accepted", "manual": True})
        self.assertEqual(ed.derived_freeze_reason(self.run_dir, self.summaries)["reason"],
                         "budget_exhausted_freeze_latest_accepted")

    def test_no_claim_without_evidence(self):
        write(self.run_dir / "builder_process.json", {"exit_code": 0, "timed_out": True})
        self.assertIsNone(ed.derived_freeze_reason(self.run_dir, self.summaries))
        (self.run_dir / "builder_process.json").unlink()
        self.assertIsNone(ed.derived_freeze_reason(self.run_dir, self.summaries))

    def test_recorded_reason_or_no_freeze_is_left_alone(self):
        write(self.run_dir / "builder_process.json", {"exit_code": 0, "timed_out": False})
        recorded = {"one_stop_summary.json": {"freeze": {"digest": "fc40", "reason": "max_dev_rounds"}}}
        self.assertIsNone(ed.derived_freeze_reason(self.run_dir, recorded))
        self.assertIsNone(ed.derived_freeze_reason(self.run_dir, {"one_stop_summary.json": {"freeze": None}}))
        self.assertIsNone(ed.derived_freeze_reason(self.run_dir, {}))


if __name__ == "__main__":
    unittest.main()
