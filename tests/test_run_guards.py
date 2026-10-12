"""The Creation evaluator broker's judge role switch, Editing runs in the concurrency cap, and Editing run tags."""
from __future__ import annotations

import importlib.util
import json
import os
import socket
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from agentswe import config  # noqa: E402
from agentswe.runners import creation_harbor_v1 as creation  # noqa: E402
from agentswe.runners import editing_agentloop_v1 as editing  # noqa: E402


def load_broker(switch: str | None):
    sys.path.insert(0, str(ROOT / "runners" / "creation"))
    env = dict(os.environ)
    env.pop("AGENTSWE_EVALUATOR_JUDGE_ROLE", None)
    if switch is not None:
        env["AGENTSWE_EVALUATOR_JUDGE_ROLE"] = switch
    with mock.patch.dict(os.environ, env, clear=True):
        spec = importlib.util.spec_from_file_location(f"broker_v2_{switch}", ROOT / "runners" / "creation" / "broker_v2.py")
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
    return module


class EvaluatorJudgeRole(unittest.TestCase):
    def test_judge_role_is_off_by_default(self):
        b = load_broker(None)
        self.assertIsNone(b.request_role("judge-only-placeholder"))
        self.assertEqual(b.request_role("runtime-only-placeholder"), "runtime")
        self.assertEqual(b.request_role("broker-only-placeholder"), "runtime")
        self.assertIsNone(b.request_role("stats-only-placeholder"))

    def test_on_restores_the_paper_broker(self):
        b = load_broker("on")
        self.assertEqual(b.request_role("judge-only-placeholder"), "judge")
        self.assertEqual(b.request_role("runtime-only-placeholder"), "runtime")

    def test_runner_passes_the_switch(self):
        cfg = config.Config({}, None)
        self.assertEqual(creation.evaluator_judge_role(cfg), "off")
        self.assertEqual(creation.evaluator_judge_role(config.Config({"AGENTSWE_EVALUATOR_JUDGE_ROLE": "on"}, None)), "on")
        with self.assertRaises(SystemExit):
            creation.evaluator_judge_role(config.Config({"AGENTSWE_EVALUATOR_JUDGE_ROLE": "maybe"}, None))


class EditingInConcurrencyCap(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.home = Path(self.tmp.name)
        runs = self.home / "runs" / "editing"
        runs.mkdir(parents=True)
        (runs / "e-x.launch.json").write_text(json.dumps(
            {"run_id": "e-x", "unit": "agentswe-oss-edit-x.service", "pid": 0, "host": socket.gethostname()}))
        self.cfg = config.Config({"AGENTSWE_HOME": str(self.home), "AGENTSWE_MAX_CONCURRENT_RUNS": "1"}, None)

    def tearDown(self):
        self.tmp.cleanup()

    def test_an_active_editing_unit_counts(self):
        with mock.patch.object(creation, "unit_active", return_value=True):
            self.assertEqual([m["run_id"] for m in creation.active_runs(self.cfg)], ["e-x"])
        with mock.patch.object(creation, "unit_active", return_value=False):
            self.assertEqual(creation.active_runs(self.cfg), [])

    def test_editing_run_refuses_at_the_cap(self):
        task = mock.Mock(id="editing/x", short="x", family="editing")
        with mock.patch.object(creation, "unit_active", return_value=True), \
                mock.patch.object(editing, "require_editing_builder"):
            with self.assertRaises(SystemExit) as ctx:
                editing.run(self.cfg, task, builder="codex", seed=1, smoke=True, label=None)
        self.assertIn("AGENTSWE_MAX_CONCURRENT_RUNS=1", str(ctx.exception))


class EditingRunTag(unittest.TestCase):
    def test_label_keeps_mode_and_seed(self):
        self.assertEqual(editing.run_tag(smoke=False, seed=2, stamp="20261012t000000z", label="mine"),
                         "mine-formal-s2-20261012t000000z")
        self.assertEqual(editing.run_tag(smoke=True, seed=3, stamp="20261012t000000z", label=None),
                         "oss-smoke-s3-20261012t000000z")


if __name__ == "__main__":
    unittest.main()
