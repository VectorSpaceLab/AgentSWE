"""Environment roots are 0755 after setup, as in the paper environments (stdlib unittest, no docker, no network).

The BuildKit local export that setup moves into $AGENTSWE_HOME/envs/<name> arrives with a 0700 root. These
tests drive ensure_env, ensure_image_env and export_from_image with the docker build replaced by a fake that produces
such an export, plus the already-installed path that repairs installs set up before the fix, and check that only the
root's mode changes.
"""
from __future__ import annotations

import os
import stat
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from agentswe import config, setup as setup_mod  # noqa: E402


def mode(path: Path) -> int:
    return stat.S_IMODE(path.stat().st_mode)


def fake_export(dest: Path) -> None:
    """What a BuildKit `--output type=local` export looks like: 0700 root, contents with their own modes."""
    (dest / "bin").mkdir(parents=True)
    (dest / "bin" / "python").write_text("#!/bin/sh\n")
    (dest / "bin" / "python").chmod(0o755)
    (dest / "lib").mkdir()
    (dest / "lib" / "private").mkdir(mode=0o700)
    (dest / "lib" / "private").chmod(0o700)
    dest.chmod(0o700)


class EnvRoot(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        home = Path(self.tmp.name) / "home"
        cfg = config.Config(values={**config.LITE_DEFAULTS, "AGENTSWE_HOME": str(home)}, env_file=None)
        with mock.patch.object(config.Config, "home", new_callable=mock.PropertyMock, return_value=home):
            self.s = setup_mod.Setup(cfg)
        self.s.home = home
        self.spec = Path(self.tmp.name) / "spec"
        self.spec.mkdir()
        (self.spec / "env.json").write_text("{}")

    def tearDown(self):
        self.tmp.cleanup()

    def build(self, cmd, **kwargs):
        dest = Path(next(a for a in cmd if a.startswith("type=local,dest=")).split("=", 2)[2])
        fake_export(dest)

    def check(self, target: Path) -> None:
        self.assertEqual(mode(target), 0o755)
        self.assertEqual(mode(target / "bin" / "python"), 0o755)
        self.assertEqual(mode(target / "lib" / "private"), 0o700)  # only the root changes

    def test_ensure_env_fresh_build(self):
        meta = {"name": "demo-env", "kind": "venv"}
        with mock.patch.object(self.s, "find_env_spec", return_value=(self.spec, meta)), \
             mock.patch.object(self.s, "ensure_image"), \
             mock.patch.object(self.s, "images", return_value={"images": {"env-builder": {"tag": "env-builder:t"}}}), \
             mock.patch.object(self.s, "build_proxy_args", return_value=[]), \
             mock.patch.object(self.s, "spec_context_with_downloads", return_value=(self.spec, [])), \
             mock.patch.object(setup_mod.util, "run", side_effect=self.build):
            self.s.ensure_env("demo-env")
        self.check(self.s.home / "envs" / "demo-env")

    def test_ensure_env_repairs_an_existing_install(self):
        meta = {"name": "old-env", "kind": "venv"}
        target = self.s.home / "envs" / "old-env"
        fake_export(target)
        self.s.state["envs"] = {"old-env": {"spec_digest": setup_mod.tree_digest(self.spec)}}
        with mock.patch.object(self.s, "find_env_spec", return_value=(self.spec, meta)), \
             mock.patch.object(setup_mod.util, "run", side_effect=AssertionError("no rebuild expected")):
            self.s.ensure_env("old-env")
        self.check(target)

    def test_ensure_image_env(self):
        meta = {"name": "img-env", "kind": "image", "image": "src", "dockerfile": "Dockerfile", "image_build_arg": "SRC"}
        target = self.s.home / "envs" / "img-env"
        with mock.patch.object(self.s, "ensure_image"), \
             mock.patch.object(self.s, "images", return_value={"images": {"src": {"tag": "src:t"}}}), \
             mock.patch.object(setup_mod.util, "run", side_effect=self.build):
            self.s.ensure_image_env(self.spec, meta, target, "digest")
        self.check(target)

    def test_export_from_image_fresh_and_existing(self):
        dest = self.s.home / "deps" / "exported"
        with mock.patch.object(self.s, "images", return_value={"images": {"img": {"tag": "img:t"}}}), \
             mock.patch.object(setup_mod.util, "out", return_value="sha256:abc"), \
             mock.patch.object(setup_mod.util, "run", side_effect=self.build):
            setup_mod.export_from_image(self.s, "img", "/opt/x", dest)
            self.check(dest)
            dest.chmod(0o700)  # an install exported before this fix
            setup_mod.export_from_image(self.s, "img", "/opt/x", dest)
        self.check(dest)


if __name__ == "__main__":
    unittest.main()
