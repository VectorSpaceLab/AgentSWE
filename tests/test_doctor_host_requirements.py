"""`agentswe doctor <task>` checks what the task declares in task.json `host` (stdlib unittest; no docker).

Before, doctor warned below a fixed 30 GiB whatever the task needed (osworld declares 160 GiB, terminalbench 150,
codex-execution-residual 120), never looked at `cgroup_v2`, reduced `systemd_delegation` to "systemd-run exists"
(printing an empty `[FAIL] systemd-run: ` when it did not), and said nothing about the platform. It now warns when
the free space under AGENTSWE_HOME is below the task's disk_gb, when a task that needs cgroup v2 runs on another
hierarchy, when systemd is not the running init or the user is not root for a task that starts systemd units, and
when the host is not Linux x86-64; for an Editing task it also fails a configuration that setup and run refuse.
"""
from __future__ import annotations

import collections
import sys
import tempfile
import types
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from agentswe import doctor, registry  # noqa: E402
from agentswe.config import LITE_DEFAULTS, Config  # noqa: E402

Usage = collections.namedtuple("Usage", "total used free")
GIB = 2**30


def task(**host):
    return types.SimpleNamespace(id="t", data={"family": "creation", "status": "runnable", "host": host,
                                               "runner_config": {}})


class HostRequirements(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.tmp = Path(tmp.name)
        self.cfg = Config(values={**LITE_DEFAULTS, "AGENTSWE_HOME": str(self.tmp / "home"),
                                  "AGENTSWE_DEFAULT_API_KEY": "test-key"}, env_file=None)

    def checks(self, t, cfg=None):
        return {c.name: c for c in doctor.task_checks(cfg or self.cfg, t)}

    def test_platform(self):
        self.assertEqual(doctor.platform_check("Linux", "x86_64").status, doctor.OK)
        check = doctor.platform_check("Darwin", "arm64")
        self.assertEqual(check.status, doctor.WARN)
        self.assertIn("AgentSWE runs on Linux x86-64 hosts", check.detail)
        self.assertEqual(doctor.platform_check("Linux", "aarch64").status, doctor.WARN)

    def test_free_disk_below_the_task_declaration(self):
        with mock.patch.object(doctor.shutil, "disk_usage", return_value=Usage(500 * GIB, 400 * GIB, 100 * GIB)):
            low = self.checks(task(disk_gb=160))["disk for this task"]
            enough = self.checks(task(disk_gb=20))["disk for this task"]
        self.assertEqual(low.status, doctor.WARN)
        self.assertIn("100 GiB free", low.detail)
        self.assertIn("needs about 160 GiB (task.json host.disk_gb)", low.detail)
        self.assertEqual(enough.status, doctor.OK)

    def test_every_declared_disk_size_is_checked(self):
        with mock.patch.object(doctor.shutil, "disk_usage", return_value=Usage(500 * GIB, 400 * GIB, 100 * GIB)):
            for name in ("osworld", "terminalbench", "codex-execution-residual"):
                self.assertEqual(self.checks(registry.find(name))["disk for this task"].status, doctor.WARN, name)

    def test_cgroup_v2(self):
        with mock.patch.object(doctor, "CGROUP_ROOT", self.tmp):
            self.assertEqual(self.checks(task(cgroup_v2=True))["cgroup v2"].status, doctor.WARN)
            (self.tmp / "cgroup.controllers").write_text("cpu memory pids\n")
            self.assertEqual(self.checks(task(cgroup_v2=True))["cgroup v2"].status, doctor.OK)
        self.assertNotIn("cgroup v2", self.checks(task(cgroup_v2=False)))

    def test_systemd_delegation(self):
        with mock.patch.object(doctor, "SYSTEMD_RUNTIME_DIR", self.tmp / "absent"), \
                mock.patch.object(doctor.shutil, "which", return_value=None), \
                mock.patch.object(doctor.os, "geteuid", return_value=1000):
            checks = self.checks(task(systemd_delegation=True))
        self.assertEqual(checks["systemd-run"].status, doctor.FAIL)
        self.assertTrue(checks["systemd-run"].detail.startswith("not found"), checks["systemd-run"].detail)
        self.assertEqual(checks["systemd init"].status, doctor.WARN)
        self.assertEqual(checks["systemd unit permission"].status, doctor.WARN)
        with mock.patch.object(doctor, "SYSTEMD_RUNTIME_DIR", self.tmp), \
                mock.patch.object(doctor.shutil, "which", return_value="/usr/bin/systemd-run"), \
                mock.patch.object(doctor.os, "geteuid", return_value=0):
            checks = self.checks(task(systemd_delegation=True))
        self.assertEqual((checks["systemd-run"].status, checks["systemd init"].status), (doctor.OK, doctor.OK))
        self.assertNotIn("systemd unit permission", checks)

    def test_systemd_run_is_listed_once(self):
        with mock.patch.object(doctor.shutil, "which", return_value=None):
            names = [c.name for c in doctor.task_checks(self.cfg, task(systemd_delegation=True,
                                                                         host_tools=["systemd-run"]))]
        self.assertEqual([n for n in names if "systemd-run" in n], ["host tool systemd-run"])

    def test_editing_provider(self):
        openwiki = registry.find("openwiki")
        with mock.patch.object(doctor, "builder_proxy_check", return_value=doctor.Check(doctor.OK, "proxy")):
            self.assertEqual(self.checks(openwiki)["Editing provider"].status, doctor.OK)
            other = Config(values={**self.cfg.values, "AGENTSWE_DEFAULT_BASE_URL": "https://api.openai.com/v1"},
                           env_file=None)
            check = self.checks(openwiki, other)["Editing provider"]
        self.assertEqual(check.status, doctor.FAIL)
        self.assertIn("BUILDER base URL is https://api.openai.com/v1", check.detail)
        self.assertNotIn("Editing provider", self.checks(registry.find("db")))


if __name__ == "__main__":
    unittest.main()
