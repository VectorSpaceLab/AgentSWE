"""`agentswe doctor <task>`: a task set up earlier whose setup exports are gone since is a WARN, not ok (stdlib
unittest; no docker, no network).

A concurrent `setup pptx` rmtree'd $AGENTSWE_HOME/deps/trusted-browser-v1 after `setup web` had
recorded success, so doctor said web was set up while its export was gone. The per-task "setup <task>" check now
also confirms that every images.json export the task needs (kind=export images, with their depends_on) and every
runner_config.host_exports destination exists."""
from __future__ import annotations

import json
import sys
import tempfile
import types
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from agentswe import doctor  # noqa: E402


class Cfg:
    def __init__(self, home: Path):
        self.home = home


def task(task_id, images=(), host_exports=()):
    return types.SimpleNamespace(id=task_id, data={"images": list(images), "family": "creation", "status": "runnable",
                                                   "host": {}, "runner_config": {"host_exports": list(host_exports)}})


class SetupExports(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.home = Path(tmp.name) / "home"
        (self.home / "state").mkdir(parents=True)
        (self.home / "state" / "setup.json").write_text(json.dumps(
            {"tasks": {"web-research-report": {"at": "2026-10-09T12:00:00+00:00"},
                       "tau3-retail": {"at": "2026-10-09T12:00:00+00:00"}}}))
        exports = json.loads((ROOT / "images" / "images.json").read_text())["images"]["trusted-browser"]["exports"]
        self.browser = [Path(v.replace("$AGENTSWE_HOME", str(self.home))) for v in exports.values()]

    def setup_check(self, t):
        (check,) = [c for c in doctor.task_checks(Cfg(self.home), t) if c.name == f"setup {t.id}"]
        return check

    def test_a_set_up_task_whose_export_is_gone_is_a_warning(self):
        for path in self.browser:
            path.mkdir(parents=True)
        web = task("web-research-report", images=["trusted-browser"])
        self.assertEqual(self.setup_check(web).status, doctor.OK)
        self.browser[0].rmdir()  # what the losing concurrent setup did
        check = self.setup_check(web)
        self.assertEqual(check.status, doctor.WARN)
        self.assertIn(str(self.browser[0]), check.detail)
        self.assertIn("set up again: agentswe setup web-research-report", check.detail)

    def test_host_exports_are_checked_too(self):
        tau3 = task("tau3-retail", host_exports=[{"image": "opt-python312", "path": "/python",
                                                  "dest": "tools/cpython-3.12"}])
        check = self.setup_check(tau3)
        self.assertEqual(check.status, doctor.WARN)
        self.assertIn(str(self.home / "tools" / "cpython-3.12"), check.detail)
        (self.home / "tools" / "cpython-3.12").mkdir(parents=True)
        self.assertEqual(self.setup_check(tau3).status, doctor.OK)

    def test_a_task_never_set_up_says_so_as_before(self):
        check = self.setup_check(task("document-to-editable-pptx", images=["pptx-libreoffice"]))
        self.assertEqual(check.status, doctor.WARN)
        self.assertEqual(check.detail, "not set up yet: agentswe setup document-to-editable-pptx")


if __name__ == "__main__":
    unittest.main()
