"""Concurrent `agentswe setup` in one AGENTSWE_HOME keeps every record in state/setup.json (stdlib unittest, no docker).

Setup reads setup.json once; save() used to rewrite it whole, so two setups in one home (two Lite tasks set up at once)
dropped each other's records. save() now takes an exclusive flock on setup.json.lock, re-reads the file and applies
only this process's changes, per top-level section and per entry (harbor per profile)."""
from __future__ import annotations

import json
import multiprocessing
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from agentswe import setup as setup_mod  # noqa: E402

ROUNDS = 25


class Cfg:
    def __init__(self, home: Path):
        self.home = home


def worker(home: str, name: str, barrier, rounds: int) -> None:
    s = setup_mod.Setup(Cfg(Path(home)))
    barrier.wait()  # both processes have read setup.json before either saves
    for k in range(rounds):
        s.state.setdefault("envs", {})[f"{name}-env-{k}"] = {"spec_digest": f"{name}{k}", "at": "t"}
        s.state.setdefault("images", {})[f"{name}-image-{k}"] = {"tag": f"agentswe-os/{name}:{k}", "id": "sha256:x"}
        s.save()
    s.state.setdefault("harbor", {}).setdefault("profiles", {})[name] = {"version": "0.20.0"}
    s.state.setdefault("tasks", {})[name] = {"at": "t"}
    s.save()


class ConcurrentSetups(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.home = Path(tmp.name) / "home"
        self.path = self.home / "state" / "setup.json"

    def read(self) -> dict:
        return json.loads(self.path.read_text())

    def test_two_processes_saving_different_envs_and_images_both_survive(self):
        seed = setup_mod.Setup(Cfg(self.home))
        seed.state.update(tasks={"repo": {"at": "t0"}}, images={"base": {"tag": "agentswe-os/base:1"}})
        seed.save()
        context = multiprocessing.get_context("fork")
        barrier = context.Barrier(2)
        processes = [context.Process(target=worker, args=(str(self.home), name, barrier, ROUNDS))
                     for name in ("deeptutor", "openwiki")]
        for p in processes:
            p.start()
        for p in processes:
            p.join(120)
            self.assertEqual(p.exitcode, 0)
        state = self.read()
        for name in ("deeptutor", "openwiki"):
            for k in range(ROUNDS):
                self.assertIn(f"{name}-env-{k}", state["envs"])
                self.assertIn(f"{name}-image-{k}", state["images"])
            self.assertIn(name, state["tasks"])
            self.assertIn(name, state["harbor"]["profiles"])
        self.assertEqual(state["tasks"]["repo"], {"at": "t0"})
        self.assertEqual(state["images"]["base"], {"tag": "agentswe-os/base:1"})

    def test_a_save_keeps_records_another_setup_wrote_since_this_one_read(self):
        a, b = setup_mod.Setup(Cfg(self.home)), setup_mod.Setup(Cfg(self.home))
        b.state.setdefault("envs", {})["b-env"] = {"at": "b"}
        b.save()
        a.state.setdefault("envs", {})["a-env"] = {"at": "a"}
        a.save()
        self.assertEqual(set(self.read()["envs"]), {"a-env", "b-env"})
        self.assertIn("b-env", a.state["envs"])  # the saving process now sees the other's records too

    def test_removals_and_changed_entries_are_this_process_changes_only(self):
        seed = setup_mod.Setup(Cfg(self.home))
        seed.state.update(tasks={"x": {"at": "0"}, "y": {"at": "0"}}, images={"i": {"tag": "old"}})
        seed.save()
        a, b = setup_mod.Setup(Cfg(self.home)), setup_mod.Setup(Cfg(self.home))
        a.state["tasks"].pop("x")  # a re-rendered x's tree
        a.state["images"]["i"] = {"tag": "new"}
        b.state["tasks"]["z"] = {"at": "1"}
        b.save()
        a.save()
        state = self.read()
        self.assertEqual(set(state["tasks"]), {"y", "z"})
        self.assertEqual(state["images"]["i"], {"tag": "new"})


class SingleProcess(unittest.TestCase):
    def test_a_single_setup_writes_its_state_as_before(self):
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp) / "home"
            s = setup_mod.Setup(Cfg(home))
            s.state["docker_plugins"] = {"compose": "2.40.3", "at": "t"}
            s.state.setdefault("tasks", {})["repo"] = {"at": "t"}
            s.save()
            path = home / "state" / "setup.json"
            self.assertEqual(path.read_text(), json.dumps(s.state, indent=2, sort_keys=False) + "\n")
            s.state["tasks"].pop("repo")
            s.state["harbor"] = {"profiles": {"creation": {"version": "0.20.0"}}}
            s.save()
            self.assertEqual(json.loads(path.read_text()), {"docker_plugins": {"compose": "2.40.3", "at": "t"},
                                                            "tasks": {}, "harbor": {"profiles": {"creation":
                                                                                                 {"version": "0.20.0"}}}})
            self.assertEqual(setup_mod.Setup(Cfg(home)).state, s.state)
            self.assertFalse((home / "state" / "setup.json.tmp").exists())

    def test_the_lock_file_sits_next_to_setup_json(self):
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp) / "home"
            setup_mod.Setup(Cfg(home)).save()
            self.assertTrue((home / "state" / "setup.json.lock").is_file())


if __name__ == "__main__":
    unittest.main()
