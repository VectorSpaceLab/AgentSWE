"""Terminal-Bench tasks on an end-of-life Debian base fetch their apt packages from the Debian archive (stdlib unittest).

qemu-startup and qemu-alpine-ssh build FROM debian:bullseye-slim (Debian 11). Its security updates have left
deb.debian.org and the regular mirrors (the indexes still name them, the pool files return 404), so the controller's
build-transport stabilization points every bullseye apt source of these two tasks at the Debian archive, with or
without AGENTSWE_TERMINALBENCH_APT_MIRROR, and leaves every other task's apt sources as they were. The archive base is
the setting AGENTSWE_TERMINALBENCH_DEBIAN_ARCHIVE (default http://archive.debian.org), declared in the task's
config_env so the runner exports it from .env. The sed preamble is applied here to the image's own sources.list
lines in Python, so no container runs.
"""
from __future__ import annotations

import importlib.util
import json
import os
import re
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from agentswe import config  # noqa: E402
from agentswe.runners.optimization_native_v1 import task_config_env  # noqa: E402

TASK = ROOT / "tasks" / "optimization" / "terminalbench" / "task.json"
NATIVE = ROOT / "tasks" / "optimization" / "terminalbench" / "benchmark" / "evaluator" / "native_tasks"
CONTROLLER = ROOT / "runners" / "optimization" / "optimization_native" / "terminalbench_controller.py"
SETTING = "AGENTSWE_TERMINALBENCH_DEBIAN_ARCHIVE"
ARCHIVE_TASKS = ("qemu-startup", "qemu-alpine-ssh")
# /etc/apt/sources.list of debian:bullseye-slim (the three suites its apt-get update fetched)
BULLSEYE_SOURCES = ("deb http://deb.debian.org/debian bullseye main\n"
                    "deb http://deb.debian.org/debian-security bullseye-security main\n"
                    "deb http://deb.debian.org/debian bullseye-updates main\n")


def controller(**env: str):
    """A fresh import of the controller with exactly these AGENTSWE_TERMINALBENCH_* settings."""
    clean = {k: v for k, v in os.environ.items() if not k.startswith("AGENTSWE_TERMINALBENCH_")}
    with mock.patch.dict(os.environ, {**clean, **env}, clear=True):
        spec = importlib.util.spec_from_file_location(f"tb_controller_{len(env)}_{id(env)}", CONTROLLER)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
    return module


def stabilized(module, name: str) -> str:
    """The environment Dockerfile of the frozen native task after stabilize_build_transport."""
    with tempfile.TemporaryDirectory() as tmp:
        task = Path(tmp) / name
        (task / "environment").mkdir(parents=True)
        dockerfile = task / "environment" / "Dockerfile"
        dockerfile.write_text((NATIVE / name / "Dockerfile").read_text())
        module.stabilize_build_transport(task)
        return dockerfile.read_text()


def apply_preamble(run_line: str, sources: str) -> str:
    """Apply the s|old|new|g commands of the sed preambles in a RUN line, in order, to a sources.list text."""
    for script in re.findall(r"sed -i '([^']*)'", run_line):
        for old, new in re.findall(r"s\|([^|]*)\|([^|]*)\|g", script):
            sources = sources.replace(old, new)
    return sources


def apt_runs(text: str) -> list[str]:
    return [line for line in text.splitlines() if re.match(r"RUN .*\bapt(-get)? update", line)]


class DebianArchiveTransport(unittest.TestCase):
    def test_native_tasks_are_bullseye(self):
        for name in ARCHIVE_TASKS:
            self.assertRegex((NATIVE / name / "Dockerfile").read_text(), r"(?m)^FROM debian:bullseye-slim\s*$")

    def test_rewrite_without_mirror(self):
        module = controller()
        for name in ARCHIVE_TASKS:
            runs = apt_runs(stabilized(module, name))
            self.assertEqual(len(runs), 2, name)  # `apt-get update && install` and the later `apt update`
            for run in runs:
                self.assertEqual(apply_preamble(run, BULLSEYE_SOURCES),
                                 "deb http://archive.debian.org/debian bullseye main\n"
                                 "deb http://archive.debian.org/debian-security bullseye-security main\n"
                                 "deb http://archive.debian.org/debian bullseye-updates main\n")

    def test_rewrite_with_mirror(self):
        module = controller(AGENTSWE_TERMINALBENCH_APT_MIRROR="http://mirror.example")
        for name in ARCHIVE_TASKS:
            for run in apt_runs(stabilized(module, name)):
                got = apply_preamble(run, BULLSEYE_SOURCES)
                self.assertNotIn("mirror.example", got)
                self.assertNotIn("deb.debian.org", got)
                self.assertEqual(got.count("http://archive.debian.org/debian"), 3)

    def test_archive_base_setting(self):
        module = controller(AGENTSWE_TERMINALBENCH_DEBIAN_ARCHIVE="http://archive-mirror.example/debian-archive/",
                            AGENTSWE_TERMINALBENCH_APT_MIRROR="http://mirror.example")
        run = apt_runs(stabilized(module, "qemu-startup"))[0]
        self.assertEqual(apply_preamble(run, BULLSEYE_SOURCES),
                         "deb http://archive-mirror.example/debian-archive/debian bullseye main\n"
                         "deb http://archive-mirror.example/debian-archive/debian-security bullseye-security main\n"
                         "deb http://archive-mirror.example/debian-archive/debian bullseye-updates main\n")
        self.assertEqual(controller(AGENTSWE_TERMINALBENCH_DEBIAN_ARCHIVE="").DEBIAN_ARCHIVE, "http://archive.debian.org")

    def test_other_debian_tasks_unchanged(self):
        others = [p.name for p in sorted(NATIVE.iterdir()) if p.name not in ARCHIVE_TASKS
                  and re.search(r"(?m)^FROM (debian|python):", (p / "Dockerfile").read_text())]
        self.assertTrue(others)  # build-pmars, fix-code-vulnerability, sam-cell-seg, mteb-leaderboard
        plain, mirrored = controller(), controller(AGENTSWE_TERMINALBENCH_APT_MIRROR="http://mirror.example")
        for name in others:
            for run in apt_runs(stabilized(plain, name)):
                self.assertNotIn("sed -i", run, name)
            for run in apt_runs(stabilized(mirrored, name)):
                self.assertNotIn("archive.debian.org", run, name)
                self.assertIn("http://mirror.example/debian-security", run, name)

    def test_setting_declared_and_exported(self):
        self.assertIn(f'os.environ.get("{SETTING}"', CONTROLLER.read_text())
        self.assertIn(SETTING, json.loads(TASK.read_text())["runner_config"]["config_env"])
        self.assertIn(SETTING, (ROOT / "docs" / "ENV.md").read_text())
        saved = {k: os.environ.pop(k) for k in list(os.environ) if k.startswith("AGENTSWE_")}
        try:
            with tempfile.TemporaryDirectory() as tmp:
                env_file = Path(tmp) / ".env"
                env_file.write_text(f"{SETTING}=http://archive-mirror.example\n")
                got = task_config_env(config.load(env_file), json.loads(TASK.read_text())["runner_config"])
        finally:
            os.environ.update(saved)
        self.assertEqual(got[SETTING], "http://archive-mirror.example")


if __name__ == "__main__":
    unittest.main()
