"""Profiles: role resolution per family and the per-task overlay staging (stdlib unittest, no network)."""
from __future__ import annotations

import os
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from agentswe import config, profiles  # noqa: E402
from agentswe.registry import find  # noqa: E402


def load(profile: str | None) -> config.Config:
    saved = os.environ.get("AGENTSWE_PROFILE")
    try:
        if profile:
            os.environ["AGENTSWE_PROFILE"] = profile
        else:
            os.environ.pop("AGENTSWE_PROFILE", None)
        return config.load("/nonexistent/.env")
    finally:
        if saved is None:
            os.environ.pop("AGENTSWE_PROFILE", None)
        else:
            os.environ["AGENTSWE_PROFILE"] = saved


class RoleResolution(unittest.TestCase):
    def test_unset_profile_is_paper_bytes_with_smoke_models(self):
        cfg = load(None)
        self.assertEqual(cfg.profile, "paper")
        for fam in (None, "CREATION", "EDITING", "OPTIMIZATION"):
            self.assertEqual(cfg.role("RUNTIME", fam).model, "deepseek-flash")

    def test_paper_profile_pins_family_models(self):
        cfg = load("paper")
        self.assertEqual((cfg.role("RUNTIME", "CREATION").model, cfg.role("RUNTIME", "CREATION").effort),
                         ("gpt-5.6-sol", "medium"))
        self.assertEqual(cfg.role("JUDGE", "CREATION").effort, "xhigh")
        self.assertEqual(cfg.role("RUNTIME", "EDITING").effort, "high")
        self.assertEqual(cfg.role("RUNTIME", "OPTIMIZATION").model, "gpt-5.6-sol")
        self.assertEqual(cfg.role("RUNTIME").model, "deepseek-flash")  # no family: the generic setting

    def test_lite_profile_is_flash_everywhere(self):
        cfg = load("lite-v1.1")
        for fam in ("CREATION", "EDITING", "OPTIMIZATION"):
            self.assertEqual((cfg.role("RUNTIME", fam).model, cfg.role("JUDGE", fam).effort), ("deepseek-flash", "max"))

    def test_smoke_default_creation_runtime_runs_without_reasoning(self):
        cfg = load(None)
        self.assertEqual(cfg.role("RUNTIME", "CREATION").effort, "explicit-none")
        for fam in (None, "EDITING", "OPTIMIZATION"):  # only the Creation runtime changes
            self.assertEqual(cfg.role("RUNTIME", fam).effort, "high")
        self.assertEqual(cfg.role("JUDGE", "CREATION").effort, "max")

    def test_profiles_keep_their_creation_runtime_effort(self):
        self.assertEqual(load("lite-v1.1").role("RUNTIME", "CREATION").effort, "high")
        self.assertEqual(load("paper").role("RUNTIME", "CREATION").effort, "medium")

    def test_user_runtime_setting_wins_over_the_smoke_default(self):
        for key, value in (("AGENTSWE_RUNTIME_EFFORT", "high"), ("AGENTSWE_RUNTIME_MODEL", "some-model"),
                           ("AGENTSWE_DEFAULT_BASE_URL", "https://provider.example/v1")):
            saved = os.environ.get(key)
            os.environ[key] = value
            try:
                self.assertNotEqual(load(None).role("RUNTIME", "CREATION").effort, "explicit-none", key)
            finally:
                if saved is None:
                    os.environ.pop(key, None)
                else:
                    os.environ[key] = saved

    def test_smoke_default_osworld_broker_runs_without_reasoning(self):
        cfg = load(None)
        self.assertEqual(cfg.get("AGENTSWE_OSWORLD_EFFORT"), "explicit-none")
        self.assertEqual(cfg.role("RUNTIME", "OPTIMIZATION").effort, "high")  # the other Optimization brokers keep theirs

    def test_profiles_keep_the_osworld_broker_effort(self):
        for profile in ("paper", "lite-v1.1"):
            self.assertIsNone(load(profile).get("AGENTSWE_OSWORLD_EFFORT"), profile)

    def test_user_setting_wins_over_the_osworld_smoke_default(self):
        for key, value, expected in (("AGENTSWE_OSWORLD_EFFORT", "high", "high"),
                                     ("AGENTSWE_OPTIMIZATION_RUNTIME_MODEL", "some-model", None),
                                     ("AGENTSWE_RUNTIME_MODEL", "some-model", None),
                                     ("AGENTSWE_OPTIMIZATION_RUNTIME_BASE_URL", "https://provider.example/v1", None),
                                     ("AGENTSWE_DEFAULT_BASE_URL", "https://provider.example/v1", None)):
            saved = os.environ.get(key)
            os.environ[key] = value
            try:
                self.assertEqual(load(None).get("AGENTSWE_OSWORLD_EFFORT"), expected, key)
            finally:
                if saved is None:
                    os.environ.pop(key, None)
                else:
                    os.environ[key] = saved

    def test_unknown_profile_is_refused(self):
        with self.assertRaises(SystemExit):
            load("nope")


class Staging(unittest.TestCase):
    def test_lite_overlay_replaces_only_listed_files(self):
        task = find("database-analytics")
        with tempfile.TemporaryDirectory() as home:
            os.environ["AGENTSWE_HOME"] = home
            try:
                cfg = load("lite-v1.1")
                staged = profiles.staged(cfg, task, "builder_package")
                self.assertNotEqual(staged, task.dir / "builder_package")
                overlay = task.dir / "profiles" / "lite-v1.1" / "builder_package" / "input" / "04_resources.md"
                self.assertEqual((staged / "input" / "04_resources.md").read_bytes(), overlay.read_bytes())
                self.assertNotEqual(overlay.read_bytes(),
                                    (task.dir / "builder_package" / "input" / "04_resources.md").read_bytes())
                self.assertEqual((staged / "input" / "02_interface_and_delivery.md").read_bytes(),
                                 (task.dir / "builder_package" / "input" / "02_interface_and_delivery.md").read_bytes())
                self.assertEqual(profiles.staged(cfg, task, "builder_package"), staged)  # reused when unchanged
                self.assertEqual(profiles.staged(load("paper"), task, "builder_package"),
                                 task.dir / "builder_package")  # paper bytes are the task directory itself
            finally:
                os.environ.pop("AGENTSWE_HOME", None)


if __name__ == "__main__":
    unittest.main()
