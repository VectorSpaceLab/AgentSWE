"""Editing runs only on the official DeepSeek API, with the Codex Builder (stdlib unittest; no network, no systemd).

The Editing task trees ignore most of .env: every tree's Builder relay accepts only a CONNECT to api.deepseek.com:443,
OpenClaw, OpenHands and OpenWiki write the Builder provider as https://api.deepseek.com/v1 whatever is configured,
and the lower-agent and Result-judge brokers send deepseek-flash requests (efforts high and max) to
https://api.deepseek.com/v1/responses. With another provider configured, runs failed when the Builder started or
sent that provider's key to api.deepseek.com, and `probe-roles --family editing` probed a configuration the runs
never used. `agentswe setup` and `agentswe run` of an Editing task now refuse such a configuration with a message
naming each setting and the variable that fixes it; `probe-roles --family editing` warns; `--builder` other than
codex is refused; and a run refuses a Builder model other than the one setup rendered into the trees.
"""
from __future__ import annotations

import contextlib
import io
import json
import os
import sys
import tempfile
import types
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from agentswe import cli, config, registry  # noqa: E402
from agentswe.config import LITE_DEFAULTS, Config  # noqa: E402
from agentswe.probe_roles import probe_roles  # noqa: E402
from agentswe.runners import editing_agentloop_v1 as ed  # noqa: E402

TASK = registry.find("openwiki-change-impact")
OTHER = "https://api.openai.com/v1"
CLEAN_ENV = {k: v for k, v in os.environ.items() if not k.startswith("AGENTSWE_")}


def refuse(*_a, **_k):
    raise AssertionError("the configuration should have been refused before this step")


class Base(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.home = Path(tmp.name) / "home"
        (self.home / "state").mkdir(parents=True)

    def cfg(self, **values) -> Config:
        return Config(values={**LITE_DEFAULTS, "AGENTSWE_HOME": str(self.home), "AGENTSWE_DEFAULT_API_KEY": "test-key",
                              **values}, env_file=None)

    def set_up_task(self, **extra):
        (self.home / "state" / "setup.json").write_text(json.dumps(
            {"tasks": {TASK.id: {"at": "2026-10-12T00:00:00+00:00"}}, **extra}))

    def run_editing(self, cfg, builder="codex"):
        with mock.patch.object(ed, "ensure_builder_proxy", refuse), mock.patch.object(ed.subprocess, "run", refuse), \
                mock.patch.object(ed.util, "run", refuse):
            return ed.run(cfg, TASK, builder=builder, seed=1, smoke=True, label=None)


class ProviderProblems(Base):
    def test_the_built_in_configuration_and_both_profiles_pass(self):
        self.assertEqual(ed.editing_provider_problems(self.cfg()), [])
        for name in config.PROFILES:
            self.assertEqual(ed.editing_provider_problems(config.profile_config(name)), [], name)

    def test_spellings_of_the_official_endpoint(self):
        for url in ("https://api.deepseek.com/v1", "https://api.deepseek.com/v1/", "https://API.deepseek.com:443/v1"):
            self.assertTrue(ed.official_deepseek_url(url), url)
        for url in ("http://api.deepseek.com/v1", "https://api.deepseek.com", "https://api.deepseek.com/beta",
                    "https://api.deepseek.com.example.net/v1", "https://user:pw" + "@api.deepseek.com/v1",
                    "https://api.deepseek.com:8443/v1", "https://api.deepseek.com:bad/v1", "", OTHER):
            self.assertFalse(ed.official_deepseek_url(url), url)

    def test_another_provider_names_every_role_and_its_variable(self):
        problems = ed.editing_provider_problems(self.cfg(AGENTSWE_DEFAULT_BASE_URL=OTHER))
        self.assertEqual(len(problems), 3)
        for role, line in zip(("BUILDER", "RUNTIME", "JUDGE"), problems):
            self.assertEqual(line, f"{role} base URL is {OTHER} (from AGENTSWE_DEFAULT_BASE_URL); set "
                                   f"AGENTSWE_EDITING_{role}_BASE_URL=https://api.deepseek.com/v1")

    def test_editing_overrides_restore_the_official_endpoint(self):
        values = {f"AGENTSWE_EDITING_{r}_BASE_URL": "https://api.deepseek.com/v1" for r in ("BUILDER", "RUNTIME", "JUDGE")}
        self.assertEqual(ed.editing_provider_problems(self.cfg(AGENTSWE_DEFAULT_BASE_URL=OTHER, **values)), [])

    def test_lower_agent_and_judge_models_and_efforts_are_pinned(self):
        problems = ed.editing_provider_problems(self.cfg(AGENTSWE_RUNTIME_MODEL="gpt-5.6-sol",
                                                         AGENTSWE_EDITING_JUDGE_EFFORT="xhigh"))
        self.assertEqual(problems, [
            "RUNTIME model is gpt-5.6-sol (from AGENTSWE_RUNTIME_MODEL); set AGENTSWE_EDITING_RUNTIME_MODEL=deepseek-flash",
            "JUDGE effort is xhigh (from AGENTSWE_EDITING_JUDGE_EFFORT); set AGENTSWE_EDITING_JUDGE_EFFORT=max"])

    def test_the_builder_model_is_free(self):
        # the Builder model and effort are switched into the trees at setup (editing_builder_model)
        self.assertEqual(ed.editing_provider_problems(self.cfg(AGENTSWE_BUILDER_MODEL="deepseek-pro",
                                                               AGENTSWE_BUILDER_EFFORT="high")), [])


class SetupAndRunRefuse(Base):
    def test_run_refuses_before_any_launch_step(self):
        self.set_up_task()
        with self.assertRaises(SystemExit) as ctx:
            self.run_editing(self.cfg(AGENTSWE_DEFAULT_BASE_URL=OTHER))
        message = str(ctx.exception.code)
        self.assertTrue(message.startswith("Editing tasks run only on the official DeepSeek API."), message)
        self.assertIn("BUILDER base URL is https://api.openai.com/v1 (from AGENTSWE_DEFAULT_BASE_URL)", message)
        self.assertIn("same DeepSeek API key", message)
        self.assertFalse(ed.credential_file(self.cfg()).exists())

    def test_freeze_writes_no_credential_for_another_provider(self):
        # `agentswe freeze` writes the control plane's credential file through the same role check
        with self.assertRaises(SystemExit):
            ed.write_credential(self.cfg(AGENTSWE_DEFAULT_BASE_URL=OTHER))
        self.assertFalse(ed.credential_file(self.cfg()).exists())

    def test_setup_refuses_before_rendering(self):
        images = json.loads((ROOT / "images" / "images.json").read_text())
        state = types.SimpleNamespace(images=lambda: images, state={})
        with mock.patch.object(ed, "render_control_plane", refuse), mock.patch.object(ed, "render_tree", refuse), \
                self.assertRaises(SystemExit) as ctx:
            ed.setup(self.cfg(AGENTSWE_EDITING_RUNTIME_MODEL="gpt-5.6-sol"), TASK, state)
        self.assertIn("RUNTIME model is gpt-5.6-sol (from AGENTSWE_EDITING_RUNTIME_MODEL)", str(ctx.exception.code))

    def test_the_setup_command_refuses_before_any_build(self):
        env = Path(self.home.parent) / "agentswe.env"
        env.write_text(f"AGENTSWE_HOME={self.home}\nAGENTSWE_DEFAULT_API_KEY=test-key\n"
                       f"AGENTSWE_DEFAULT_BASE_URL={OTHER}\n")
        with mock.patch.dict(os.environ, CLEAN_ENV, clear=True), mock.patch.object(cli, "setup_task", refuse), \
                contextlib.redirect_stdout(io.StringIO()):
            with self.assertRaises(SystemExit) as ctx:
                cli.main(["--env-file", str(env), "setup", "openwiki"])
        self.assertIn("official DeepSeek API", str(ctx.exception.code))


class BuilderChoice(Base):
    def test_only_the_codex_builder(self):
        self.set_up_task()
        with self.assertRaises(SystemExit) as ctx:
            self.run_editing(self.cfg(), builder="claude-code")
        self.assertEqual(str(ctx.exception.code), "--builder claude-code: Editing tasks support only the Codex Builder; "
                                                  "use --builder codex (the Builder model is "
                                                  "AGENTSWE_EDITING_BUILDER_MODEL or AGENTSWE_BUILDER_MODEL)")

    def test_builder_checked_before_the_setup_state(self):
        with self.assertRaises(SystemExit) as ctx:
            self.run_editing(self.cfg(), builder="foo")
        self.assertIn("--builder codex", str(ctx.exception.code))

    def test_a_builder_model_other_than_the_one_set_up_is_refused(self):
        self.set_up_task(editing_builder={"model": "deepseek-flash", "effort": "max"})
        with self.assertRaises(SystemExit) as ctx:
            self.run_editing(self.cfg(AGENTSWE_BUILDER_MODEL="deepseek-pro"))
        message = str(ctx.exception.code)
        self.assertIn("set up for the Builder deepseek-flash (effort max)", message)
        self.assertIn("configured Builder is deepseek-pro (effort max)", message)
        self.assertIn("agentswe setup", message)


class ProbeWarning(Base):
    def test_probe_roles_warns_for_editing_only(self):
        cfg = self.cfg(AGENTSWE_DEFAULT_BASE_URL=OTHER)
        report = probe_roles(cfg, roles=(), search=False, family="editing")
        self.assertEqual(len(report["warnings"]), 3)
        self.assertTrue(report["warnings"][0].startswith("Editing runs do not use this configuration as probed: "
                                                         "BUILDER base URL is https://api.openai.com/v1"))
        self.assertNotIn("warnings", probe_roles(cfg, roles=(), search=False, family="creation"))
        self.assertNotIn("warnings", probe_roles(self.cfg(), roles=(), search=False, family="editing"))


if __name__ == "__main__":
    unittest.main()
