"""Docker 29 removes a stopped --rm container asynchronously: `docker rm -f` then answers "removal of container ... is
already in progress" and the container is listed for a few more seconds. `agentswe stop` (Editing) and the PinchBench
controller wait for such a container before removing its networks (stdlib unittest, docker mocked)."""
from __future__ import annotations

import importlib
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from agentswe.runners import editing_agentloop_v1 as ed  # noqa: E402

RUN = "/srv/agentswe-home/runs/editing/formal/codex_xhigh/claude/0905-edit-codex-xhigh-t-claude"
BUSY = "Error response from daemon: removal of container %s is already in progress"


class Cfg:
    def __init__(self, home: Path):
        self.home = home


class StopWaitsForDaemonRemoval(unittest.TestCase):
    def test_container_being_removed_is_awaited_before_networks(self):
        a, b = "a" * 64, "b" * 64  # a: running, removed by rm -f; b: an --rm judge the one-stop already stopped
        info = lambda cid, project: ('[{"Id": "%s", "Mounts": [{"Source": "%s/x"}], "Config": {"Labels": '  # noqa: E731
                                     '{"com.docker.compose.project": "%s"}}}]' % (cid, RUN, project))
        inspects = {b: iter([0, 0, 1])}  # still listed twice, then gone
        calls = []

        def out(argv, **kw):
            if argv[:3] == ["docker", "ps", "-aq"]:
                return f"{a}\n{b}"
            if argv[:2] == ["docker", "inspect"]:
                return info(argv[2], "builder_task__x__env")
            if argv[:3] == ["docker", "network", "ls"]:
                return "n" * 12
            if argv[:3] == ["docker", "network", "inspect"]:
                # the network is empty only once b is gone
                return "0" if "b-gone" in calls else "1"
            raise AssertionError(argv)

        def run(argv, **kw):
            calls.append(" ".join(argv[:3]))
            if argv[:3] == ["docker", "rm", "-f"]:
                return subprocess.CompletedProcess(argv, 0 if argv[3] == a else 1, "" if argv[3] == a else BUSY % b)
            if argv[:2] == ["docker", "inspect"]:
                rc = next(inspects[argv[2]], 1)
                if rc:
                    calls.append("b-gone")
                return subprocess.CompletedProcess(argv, 0 if rc == 0 else 1, "")
            if argv[:3] == ["docker", "network", "rm"]:
                return subprocess.CompletedProcess(argv, 0, "")
            raise AssertionError(argv)

        with tempfile.TemporaryDirectory() as tmp, \
                mock.patch.object(ed.util, "out", side_effect=out), \
                mock.patch.object(ed.util, "run", side_effect=run), mock.patch.object(ed.time, "sleep"):
            removed = ed._remove_run_objects(Cfg(Path(tmp)), {"run_id": "e-claude-t", "run_dir": RUN})
            log = (Path(tmp) / "DELETIONS.log").read_text()
        self.assertIn(f"container {a[:12]}", removed)
        self.assertIn(f"container {b[:12]} (removed by the daemon)", removed)
        self.assertIn(f"network {'n' * 12} (builder_task__x__env)", removed)
        self.assertEqual(log.count("\n"), 3)
        self.assertLess(calls.index("b-gone"), calls.index("docker network rm"))

    def test_other_rm_failures_are_not_reported_as_removed(self):
        a = "a" * 64

        def out(argv, **kw):
            if argv[:3] == ["docker", "ps", "-aq"]:
                return a
            if argv[:2] == ["docker", "inspect"]:
                return '[{"Id": "%s", "Mounts": [{"Source": "%s"}], "Config": {"Labels": {}}}]' % (a, RUN)
            raise AssertionError(argv)

        with tempfile.TemporaryDirectory() as tmp, mock.patch.object(ed.util, "out", side_effect=out), \
                mock.patch.object(ed.util, "run", return_value=subprocess.CompletedProcess([], 1, "permission denied")):
            self.assertEqual(ed._remove_run_objects(Cfg(Path(tmp)), {"run_id": "e", "run_dir": RUN}), [])


class PinchBenchBrokerWait(unittest.TestCase):
    def load(self):
        sys.path.insert(0, str(ROOT / "runners" / "optimization" / "optimization_native"))
        with tempfile.TemporaryDirectory() as tmp, mock.patch.dict(os.environ, {"AGENTSWE_HOME": tmp}):
            sys.modules.pop("pinchbench_controller", None)
            return importlib.import_module("pinchbench_controller")

    def test_waits_until_the_broker_is_gone(self):
        m = self.load()
        results = iter([0, 0, 1])
        with mock.patch.object(m, "run", side_effect=lambda argv, **kw: subprocess.CompletedProcess(argv, next(results))), \
                mock.patch.object(m.time, "sleep") as sleep:
            self.assertTrue(m.wait_container_absent("broker"))
        self.assertEqual(sleep.call_count, 2)

    def test_gives_up_after_the_deadline(self):
        m = self.load()
        with mock.patch.object(m, "run", return_value=subprocess.CompletedProcess([], 0)), \
                mock.patch.object(m.time, "sleep"):
            self.assertFalse(m.wait_container_absent("broker", seconds=0))

    def test_finally_waits_before_network_removal(self):
        source = (ROOT / "runners" / "optimization" / "optimization_native" / "pinchbench_controller.py").read_text()
        block = source[source.index('run(["docker", "rm", "-f", broker], timeout=30)'):]
        self.assertLess(block.index("wait_container_absent(broker)"), block.index('"network", "rm", public_network'))


if __name__ == "__main__":
    unittest.main()
