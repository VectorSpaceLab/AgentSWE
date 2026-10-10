"""Terminal-Bench build-transport mirrors reach the native controller from .env (stdlib unittest).

The controller builds each case's task image during the run and reads AGENTSWE_TERMINALBENCH_APT_MIRROR and
AGENTSWE_TERMINALBENCH_PIP_INDEX from its environment. The runner exports a task's runner_config.config_env settings
as resolved from the configuration; before these two were declared there, they worked only from the shell."""
from __future__ import annotations

import json
import os
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from agentswe import config  # noqa: E402
from agentswe.runners.optimization_native_v1 import task_config_env  # noqa: E402

TASK = ROOT / "tasks" / "optimization" / "terminalbench" / "task.json"
CONTROLLER = ROOT / "runners" / "optimization" / "optimization_native" / "terminalbench_controller.py"
SETTINGS = ("AGENTSWE_TERMINALBENCH_APT_MIRROR", "AGENTSWE_TERMINALBENCH_PIP_INDEX")


class TerminalBenchMirrors(unittest.TestCase):
    def exported(self, lines: str) -> dict[str, str]:
        saved = {k: os.environ.pop(k) for k in list(os.environ) if k.startswith("AGENTSWE_")}
        try:
            with tempfile.TemporaryDirectory() as tmp:
                env_file = Path(tmp) / ".env"
                env_file.write_text(lines)
                return task_config_env(config.load(env_file), json.loads(TASK.read_text())["runner_config"])
        finally:
            os.environ.update(saved)

    def test_controller_reads_them(self):
        source = CONTROLLER.read_text()
        for name in SETTINGS:
            self.assertIn(f'os.environ.get("{name}"', source)

    def test_declared_and_documented(self):
        declared = json.loads(TASK.read_text())["runner_config"].get("config_env", [])
        docs = (ROOT / "docs" / "ENV.md").read_text() + (ROOT / ".env.example").read_text()
        for name in SETTINGS:
            self.assertIn(name, declared)
            self.assertIn(name, docs)

    def test_exported_from_dotenv(self):
        got = self.exported("AGENTSWE_TERMINALBENCH_APT_MIRROR=http://mirror.example\n"
                            "AGENTSWE_TERMINALBENCH_PIP_INDEX=https://pypi.example/simple\n")
        self.assertEqual(got["AGENTSWE_TERMINALBENCH_APT_MIRROR"], "http://mirror.example")
        self.assertEqual(got["AGENTSWE_TERMINALBENCH_PIP_INDEX"], "https://pypi.example/simple")

    def test_unset_stays_unset(self):
        got = self.exported("AGENTSWE_DEFAULT_API_KEY=k\n")
        for name in SETTINGS:
            self.assertNotIn(name, got)


if __name__ == "__main__":
    unittest.main()
