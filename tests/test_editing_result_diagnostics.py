"""`agentswe status/result` for Editing runs: the exit status, the headline result and, for a run that stopped before
writing its summaries, the control-plane log that says why; a formal dry run leaves no launch_control directory
(stdlib unittest, systemd mocked)."""
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


class Cfg:
    def __init__(self, home: Path):
        self.home = home

    def get(self, key, default=None):
        return default


class ResultDiagnostics(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.home = Path(tmp.name)
        formal = self.home / "runs" / "editing" / "formal"
        self.run_dir = formal / "codex_xhigh" / "deeptutor" / "0905-edit-codex-xhigh-oss-formal-s1-x-deeptutor"
        self.run_dir.mkdir(parents=True)
        self.log = formal / "launch_control" / "0905-edit-codex-xhigh-oss-formal-s1-x" / "deeptutor" / "orchestrator.log"
        self.log.parent.mkdir(parents=True)
        self.launch = {"run_id": "e-deeptutor-oss-formal-s1-x", "task": "deeptutor-adaptive-remediation",
                       "family": "editing", "mode": "formal", "comparable": True, "builder": {"profile": "codex"},
                       "unit": "agentswe-oss-formal-deeptutor-oss-formal-s1-x.service", "run_dir": str(self.run_dir),
                       "log": str(self.home / "runs" / "editing" / "deeptutor-oss-formal-s1-x.launch.log")}

    def result(self, unit_state, exit_status):
        with mock.patch.object(ed, "_unit_state", return_value=unit_state), \
                mock.patch.object(ed.util, "out", return_value=exit_status):
            return ed.result(Cfg(self.home), self.launch)

    def test_a_run_that_stopped_at_startup_says_why(self):
        self.log.write_text("usage: formal_one_stop.py [-h]\nformal_one_stop.py: error: formal readiness refused: x\n")
        res = self.result("failed", "2")
        self.assertEqual(res["exit_status"], "2")
        self.assertEqual(res["summaries"], {})
        self.assertEqual(res["orchestrator_log"], str(self.log))
        self.assertIn("formal readiness refused", res["orchestrator_log_tail"][-1])
        written = json.loads((self.home / "runs" / "editing" / "e-deeptutor-oss-formal-s1-x.result.json").read_text())
        self.assertEqual(written["orchestrator_log_tail"], res["orchestrator_log_tail"])

    def test_a_finished_run_carries_its_headline_result(self):
        self.log.write_text("launched\n")
        (self.run_dir / "summary.json").write_text(json.dumps({"status": "formal_evidence_complete"}))
        (self.run_dir / "formal_aggregation.json").write_text(json.dumps(
            {"formal_result_publishable": True, "result_axis": {"score": 80.3333, "case_scores": {"test_001": 99}}}))
        res = self.result("inactive", "0")
        self.assertEqual(res["exit_status"], "0")
        self.assertTrue(res["formal_result_publishable"])
        self.assertEqual(res["result_axis"]["score"], 80.3333)
        self.assertNotIn("orchestrator_log_tail", res)

    def test_status_names_the_log(self):
        self.log.write_text("x\n")
        with mock.patch.object(ed, "_unit_state", return_value="active"), \
                mock.patch.object(ed.util, "out", return_value=""):
            st = ed.status(Cfg(self.home), self.launch)
        self.assertEqual(st["orchestrator_log"], str(self.log))
        self.assertTrue(st["alive"])

    def test_smoke_or_unknown_layout_has_no_log(self):
        self.assertIsNone(ed._orchestrator_log(self.home / "runs" / "editing" / "smoke" / "aider" / "tag"))
        self.assertIsNone(ed._orchestrator_log(None))


class DryRunLeavesNothing(unittest.TestCase):
    def test_launch_control_is_created_after_the_dry_run_return(self):
        source = (ROOT / "runners" / "editing" / "tools" / "launch_formal_task.py").read_text()
        self.assertLess(source.index("print('dry run; not launched'); return 0"), source.index("control.mkdir("))


if __name__ == "__main__":
    unittest.main()
