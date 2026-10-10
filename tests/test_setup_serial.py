"""Concurrent `agentswe setup` in one AGENTSWE_HOME, and Editing setups next to a live Editing run (stdlib unittest; no
docker, no network, systemd mocked).

Two setups that both needed the trusted-browser export rmtree'd and moved
each other's cache/export-trusted-browser, and two Editing setups re-rendered the shared trees under each other.
setup_task now holds one flock on state/setup.run.lock for the whole setup. Editing setup renders control/ and tools/
into a staging dir and keeps the installed ones (same inodes) when the render is the same; a changed control plane,
or a task tree that must be re-rendered, is refused while an Editing run that uses it is live in the home."""
from __future__ import annotations

import io
import json
import multiprocessing
import os
import sys
import tempfile
import time
import types
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from agentswe import setup as setup_mod  # noqa: E402
from agentswe.runners import editing_agentloop_v1 as ed  # noqa: E402


class Cfg:
    profile = "lite-v1.1"

    def __init__(self, home: Path):
        self.home = home

    def get(self, key, default=None):
        return default


def task(name: str):
    return types.SimpleNamespace(id=name, label=name, runner="fake", data={"images": [], "envs": [],
                                                                         "runner_config": {}})


def serial_worker(home: str, name: str, record: str) -> None:
    """setup_task with its heavy body replaced: Setup records when it read the state and how long its work took."""
    log = Path(record) / f"{name}.jsonl"

    class FakeSetup:
        def __init__(self, cfg):
            self.state = {}
            with log.open("a") as fh:
                fh.write(json.dumps({"read_state": time.time()}) + "\n")

        def ensure_docker_plugins(self):
            with log.open("a") as fh:
                fh.write(json.dumps({"start": time.time()}) + "\n")
            time.sleep(1.0)
            with log.open("a") as fh:
                fh.write(json.dumps({"end": time.time()}) + "\n")

        def ensure_harbor(self, profile):
            pass

        def save(self):
            pass

    out = io.StringIO()
    with mock.patch.object(setup_mod, "Setup", FakeSetup), \
            mock.patch("agentswe.runners.load", return_value=types.SimpleNamespace()), redirect_stdout(out):
        setup_mod.setup_task(Cfg(Path(home)), task(name))
    (Path(record) / f"{name}.out").write_text(out.getvalue())


class SetupRunsOneAtATime(unittest.TestCase):
    def test_a_second_setup_in_the_home_waits_for_the_first(self):
        with tempfile.TemporaryDirectory() as tmp:
            home, record = Path(tmp) / "home", Path(tmp) / "record"
            record.mkdir()
            context = multiprocessing.get_context("fork")
            first = context.Process(target=serial_worker, args=(str(home), "web", str(record)))
            first.start()
            deadline = time.time() + 10
            while not (record / "web.jsonl").is_file() or "start" not in (record / "web.jsonl").read_text():
                self.assertLess(time.time(), deadline, "first setup never started")
                time.sleep(0.02)
            second = context.Process(target=serial_worker, args=(str(home), "pptx", str(record)))
            second.start()
            for p in (first, second):
                p.join(30)
                self.assertEqual(p.exitcode, 0)
            read = lambda name: {k: v for line in (record / f"{name}.jsonl").read_text().splitlines()  # noqa: E731
                                 for k, v in json.loads(line).items()}
            web, pptx = read("web"), read("pptx")
            self.assertGreaterEqual(pptx["read_state"], web["end"])  # the second read setup.json after the first
            self.assertGreaterEqual(pptx["start"], web["end"])
            self.assertIn(f"another setup is running in {home}; waiting", (record / "pptx.out").read_text())
            self.assertNotIn("waiting", (record / "web.out").read_text())
            self.assertTrue((home / "state" / "setup.run.lock").is_file())


class EditingControlPlane(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.home = Path(tmp.name) / "home"
        self.cfg = Cfg(self.home)
        self.e = ed.editing_root(self.cfg)
        self.s = types.SimpleNamespace(state={})
        self.mapping = ed.tokens(self.cfg)
        self.key = {"model": ed.editing_builder_model.DEFAULT_MODEL, "effort": ed.editing_builder_model.DEFAULT_EFFORT}

    def render(self, live=None):
        out = io.StringIO()
        with redirect_stdout(out):
            ed.render_control_plane(self.cfg, self.s, self.e, self.mapping, live or {}, self.key, False)
        return out.getvalue()

    def installed(self):
        self.render()
        (self.e / "control" / "readiness_admissions").mkdir()
        (self.e / "control" / "readiness_admissions" / "a.json").write_text("{}")
        # what rebind, the release snapshot and the audit leave next to the render
        (self.e / "control" / "configuration_delta_registry.lock").touch()
        (self.e / "control" / "pre_repair_tree_snapshot.paper.json").write_text("{}")
        (self.e / "control" / "pre_repair_tree_snapshot.json").write_text('{"provenance_mode": "release"}')
        (self.e / "control" / "__pycache__").mkdir()

    def stat(self, path: Path):
        st = path.stat()
        return st.st_ino, st.st_mtime_ns

    def test_an_unchanged_control_plane_keeps_its_inodes_and_mtime(self):
        self.installed()
        control, tools = self.e / "control", self.e / "tools"
        module = control / "result_judge.py"
        before = [self.stat(control), self.stat(tools), self.stat(module)]
        out = self.render(live={"e-aider-oss-smoke-s1-x": "aider-worktree-transaction"})  # a live run does not matter
        self.assertEqual([self.stat(control), self.stat(tools), self.stat(module)], before)
        self.assertIn("editing/control unchanged; kept as installed", out)
        self.assertFalse((self.e / ".render-staging").exists())
        self.assertEqual((control / "pre_repair_tree_snapshot.json").read_text(), '{"provenance_mode": "release"}')

    def test_a_changed_control_plane_is_refused_while_a_run_is_live(self):
        self.installed()
        module = self.e / "control" / "result_judge.py"
        module.write_text(module.read_text() + "\n# an older release\n")
        before = self.stat(self.e / "control")
        with self.assertRaises(SystemExit) as caught:
            self.render(live={"e-claude-oss-smoke-s1-x": "claude-policy-provenance"})
        message = str(caught.exception)
        self.assertIn("e-claude-oss-smoke-s1-x", message)
        self.assertIn("wait for them to finish or use another AGENTSWE_HOME", message)
        self.assertEqual(self.stat(self.e / "control"), before)
        self.assertTrue(module.read_text().endswith("# an older release\n"))
        self.assertFalse((self.e / ".render-staging").exists())

    def test_a_changed_control_plane_is_replaced_when_no_run_is_live(self):
        self.installed()
        module = self.e / "control" / "result_judge.py"
        module.write_text(module.read_text() + "\n# an older release\n")
        self.render()
        self.assertFalse(module.read_text().endswith("# an older release\n"))
        self.assertEqual((self.e / "control" / "readiness_admissions" / "a.json").read_text(), "{}")  # run state kept
        self.assertTrue((self.e / "control" / "configuration_delta_registry.json").is_file())  # templates rewritten

    def test_setup_of_a_task_with_a_live_run_is_refused_before_anything_is_rendered(self):
        runs = self.home / "runs" / "editing"
        runs.mkdir(parents=True)
        (runs / "e-aider-oss-smoke-s1-x.launch.json").write_text(json.dumps(
            {"run_id": "e-aider-oss-smoke-s1-x", "task": "aider-worktree-transaction",
             "unit": "agentswe-oss-edit-aider-oss-smoke-s1-x.service"}))
        images = json.loads((ROOT / "images" / "images.json").read_text())
        s = types.SimpleNamespace(state={}, images=lambda: images)
        with mock.patch.object(ed, "_unit_state", return_value="active"), \
                mock.patch.object(ed, "render_tree", side_effect=AssertionError("rendered under a live run")):
            with self.assertRaises(SystemExit) as caught:
                ed.setup(self.cfg, types.SimpleNamespace(id="aider-worktree-transaction", data={}), s)
        self.assertIn("e-aider-oss-smoke-s1-x", str(caught.exception))
        self.assertIn("use another AGENTSWE_HOME", str(caught.exception))

    def test_live_runs_are_launch_manifests_whose_unit_is_active_or_activating(self):
        runs = self.home / "runs" / "editing"
        runs.mkdir(parents=True)
        for name, unit in (("a", "u-active"), ("b", "u-activating"), ("c", "u-failed"), ("d", None)):
            (runs / f"e-{name}.launch.json").write_text(json.dumps({"run_id": f"e-{name}", "task": name, "unit": unit}))
        states = {"u-active": "active", "u-activating": "activating", "u-failed": "failed"}
        with mock.patch.object(ed, "_unit_state", side_effect=lambda unit: states[unit]):
            self.assertEqual(ed.live_runs(self.cfg), {"e-a": "a", "e-b": "b"})


if __name__ == "__main__":
    unittest.main()
