"""`comparable` in launch manifests and results depends on the profile, not only on --smoke (stdlib unittest).

Before, every runner wrote `"comparable": not smoke`: a formal run with AGENTSWE_PROFILE unset (the built-in smoke
defaults, deepseek-flash everywhere, the Creation runtime without reasoning, judge format release) was labelled
comparable with the paper. A run is now comparable only when it is not a smoke, a named profile is set explicitly,
and the family's RUNTIME and JUDGE model, effort and wire, the Creation judge format and the model-call settings the
task declares are that profile's; lite-v1.1 covers only the Lite tasks. `comparable_basis` says which profile, or
why not, in the launch manifest, in `agentswe run`'s output and in the result. Scores are unaffected.
"""
from __future__ import annotations

import contextlib
import io
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

from agentswe import cli, config, registry  # noqa: E402
from agentswe.runners import creation_harbor_v1 as creation  # noqa: E402
from agentswe.runners import editing_agentloop_v1 as editing  # noqa: E402
from agentswe.runners import optimization_native_v1 as optimization  # noqa: E402

CLEAN_ENV = {k: v for k, v in os.environ.items() if not k.startswith("AGENTSWE_")}


def load(**values) -> config.Config:
    """The configuration a process with these AGENTSWE_* variables and no .env resolves."""
    with mock.patch.dict(os.environ, {**CLEAN_ENV, **values}, clear=True):
        return config.load("/nonexistent/.env")


def task(name: str) -> dict:
    return registry.find(name).data


class Comparability(unittest.TestCase):
    def test_smoke_is_never_comparable(self):
        comparable, basis = config.comparability(load(AGENTSWE_PROFILE="lite-v1.1"), task("db"), smoke=True)
        self.assertFalse(comparable)
        self.assertTrue(basis.startswith("smoke run"), basis)

    def test_no_profile_is_not_comparable(self):
        # the configuration the audit found labelled comparable: no profile, formal run
        for name in ("db", "openwiki", "tau3"):
            comparable, basis = config.comparability(load(), task(name), smoke=False)
            self.assertFalse(comparable, name)
            self.assertIn("AGENTSWE_PROFILE is not set", basis)

    def test_lite_profile_on_a_lite_task(self):
        cfg = load(AGENTSWE_PROFILE="lite-v1.1")
        for name, family in (("db", "creation"), ("openwiki", "editing"), ("tau3", "optimization")):
            comparable, basis = config.comparability(cfg, task(name), smoke=False)
            self.assertTrue(comparable, basis)
            self.assertTrue(basis.startswith(f"profile lite-v1.1: the {family} runtime and judge settings"), basis)

    def test_lite_profile_covers_only_the_lite_tasks(self):
        comparable, basis = config.comparability(load(AGENTSWE_PROFILE="lite-v1.1"), task("qa"), smoke=False)
        self.assertFalse(comparable)
        self.assertEqual(basis, "profile lite-v1.1 covers only the Lite tasks, and evidence-grounded-document-qa is "
                                "not one of them")

    def test_a_changed_role_setting_is_not_comparable(self):
        cases = [({"AGENTSWE_CREATION_RUNTIME_MODEL": "deepseek-pro"}, "db",
                  "CREATION RUNTIME model is deepseek-pro, the profile uses deepseek-flash"),
                 ({"AGENTSWE_JUDGE_EFFORT": "high", "AGENTSWE_CREATION_JUDGE_EFFORT": "high"}, "db",
                  "CREATION JUDGE effort is high, the profile uses max"),
                 ({"AGENTSWE_DEFAULT_WIRE": "chat"}, "tau3", "OPTIMIZATION RUNTIME wire is chat, the profile uses responses"),
                 ({"AGENTSWE_OPTIMIZATION_JUDGE_EFFORT": "low"}, "pinch",
                  "OPTIMIZATION JUDGE effort is low, the profile uses max")]
        for values, name, expected in cases:
            comparable, basis = config.comparability(load(AGENTSWE_PROFILE="lite-v1.1", **values), task(name), False)
            self.assertFalse(comparable, values)
            self.assertEqual(basis, "profile lite-v1.1 is set, but " + expected)

    def test_the_builder_is_not_compared(self):
        cfg = load(AGENTSWE_PROFILE="lite-v1.1", AGENTSWE_BUILDER_MODEL="gpt-5.6-sol", AGENTSWE_BUILDER_EFFORT="xhigh",
                   AGENTSWE_BUILDER_WIRE="chat")
        self.assertTrue(config.comparability(cfg, task("repo"), smoke=False)[0])

    def test_paper_profile_needs_the_exact_judge_format_for_creation(self):
        comparable, basis = config.comparability(load(AGENTSWE_PROFILE="paper"), task("security"), smoke=False)
        self.assertFalse(comparable)
        self.assertEqual(basis, "profile paper is set, but judge format is release, the profile uses exact "
                                "(AGENTSWE_JUDGE_FORMAT=exact)")
        cfg = load(AGENTSWE_PROFILE="paper", AGENTSWE_JUDGE_FORMAT="exact")
        self.assertTrue(config.comparability(cfg, task("security"), smoke=False)[0])
        # the judge format is a Creation setting only
        self.assertTrue(config.comparability(load(AGENTSWE_PROFILE="paper"), task("claude"), smoke=False)[0])

    def test_task_model_call_settings(self):
        cfg = load(AGENTSWE_PROFILE="paper")
        self.assertTrue(config.comparability(cfg, task("osworld"), smoke=False)[0])
        comparable, basis = config.comparability(load(AGENTSWE_PROFILE="paper", AGENTSWE_OSWORLD_SCHEMA_STRICT="0"),
                                                 task("osworld"), smoke=False)
        self.assertFalse(comparable)
        self.assertIn("AGENTSWE_OSWORLD_SCHEMA_STRICT is 0, the profile uses 1", basis)
        comparable, basis = config.comparability(load(AGENTSWE_PROFILE="paper", AGENTSWE_OSWORLD_EFFORT="explicit-none"),
                                                 task("osworld"), smoke=False)
        self.assertIn("AGENTSWE_OSWORLD_EFFORT is explicit-none, the profile uses high", basis)
        self.assertTrue(config.comparability(load(AGENTSWE_PROFILE="paper", AGENTSWE_OSWORLD_EFFORT="high"),
                                             task("osworld"), smoke=False)[0])


class Outputs(unittest.TestCase):
    """The basis travels from the launch manifest to the result, and `agentswe run` prints it."""

    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        self.basis = "profile lite-v1.1: the creation runtime and judge settings and judge format are the profile's"

    def launch(self, family: str) -> dict:
        log = self.root / f"{family}.one_stop.log"
        log.write_text("===== 2026-10-12T00:00:00Z one_stop exit 1; broker and key files removed\n")
        return {"run_id": f"{family}-run", "task": "t", "family": family, "mode": "formal", "comparable": True,
                "comparable_basis": self.basis, "profile": {"name": "lite-v1.1", "explicit": True},
                "builder": {"profile": "codex"}, "host": socket.gethostname(), "pid": 0, "log": str(log),
                "run_dir": str(self.root / family), "protocol": {"heldout_cases": []}}

    def test_ended_without_result_carries_the_basis(self):
        for module, family in ((creation, "creation"), (optimization, "optimization")):
            res = module.ended_without_result(self.launch(family), None)
            self.assertEqual((res["comparable"], res["comparable_basis"]), (True, self.basis))
            self.assertEqual(res["profile"], {"name": "lite-v1.1", "explicit": True})

    def test_results_carry_the_basis(self):
        launch = self.launch("creation")
        run_dir = Path(launch["run_dir"])
        run_dir.mkdir()
        (run_dir / "one_stop_summary.json").write_text(json.dumps({"status": "completed", "hidden_mean": 50.0}))
        launch["broker"] = {"ledger": str(self.root / "ledger")}
        launch["judge_format"] = "release"
        res = creation.result(None, launch)
        self.assertEqual((res["comparable_basis"], res["judge_format"]), (self.basis, "release"))
        launch = self.launch("optimization")
        Path(launch["run_dir"]).mkdir()
        (Path(launch["run_dir"]) / "one_stop_summary.json").write_text(json.dumps({"status": "completed"}))
        self.assertEqual(optimization.result(None, launch)["comparable_basis"], self.basis)

    def test_a_launch_from_before_the_basis_still_reads(self):
        launch = self.launch("creation")
        del launch["comparable_basis"]
        self.assertIsNone(creation.ended_without_result(launch, None)["comparable_basis"])

    def test_editing_result_carries_the_basis(self):
        launch = {**self.launch("editing"), "unit": "agentswe-oss-test.service", "run_dir": ""}
        with mock.patch.object(editing, "_unit_state", return_value="inactive"), \
                mock.patch.object(editing.util, "out", return_value="0"):
            res = editing.result(type("Cfg", (), {"home": self.root, "get": lambda *a: None})(), launch)
        self.assertEqual(res["comparable_basis"], self.basis)

    def test_run_prints_comparable_and_basis(self):
        env = self.root / "agentswe.env"
        env.write_text(f"AGENTSWE_HOME={self.root / 'home'}\n")
        manifest = {"run_id": "r", "pid": 1, "log": "l", "mode": "formal", "comparable": False,
                    "comparable_basis": "no profile: AGENTSWE_PROFILE is not set"}
        runner = type("Runner", (), {"run": staticmethod(lambda *a, **k: manifest)})
        out = io.StringIO()
        with mock.patch.dict(os.environ, CLEAN_ENV, clear=True), mock.patch.object(cli.runners, "load",
                                                                                  return_value=runner), \
                contextlib.redirect_stdout(out):
            self.assertEqual(cli.main(["--env-file", str(env), "run", "db"]), 0)
        printed = json.loads(out.getvalue())
        self.assertEqual((printed["comparable"], printed["comparable_basis"]),
                         (False, "no profile: AGENTSWE_PROFILE is not set"))


if __name__ == "__main__":
    unittest.main()
