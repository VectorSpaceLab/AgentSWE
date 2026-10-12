"""Mirror settings documented in docs/ENV.md reach the Editing host builds from .env, not only from the shell
(stdlib unittest).

`agentswe setup` runs each Editing env's build-host.sh with the process environment plus the .env values named in
editing_agentloop_v1.HOST_BUILD_CONFIG. A setting a build script reads but HOST_BUILD_CONFIG leaves out works only when
exported in the shell: Dyad's AGENTSWE_CFT_BASE_URL and AGENTSWE_PLAYWRIGHT_DOWNLOAD_HOST were such settings."""
from __future__ import annotations

import re
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from agentswe.runners import editing_agentloop_v1 as ed  # noqa: E402

SET_BY_SETUP = {"AGENTSWE_HOME", "AGENTSWE_ENV_ARCHIVE_DIR"}  # setup sets these itself (home; staged archives)


class HostBuildConfig(unittest.TestCase):
    def test_documented_settings_read_by_build_scripts_come_from_dotenv(self):
        documented = set(re.findall(r"AGENTSWE_[A-Z0-9_]+", (ROOT / "docs" / "ENV.md").read_text()))
        read = {}
        for script in sorted((ROOT / "tasks" / "editing").glob("*/env/*.sh")):
            for name in re.findall(r"\$\{?(AGENTSWE_[A-Z0-9_]+)", script.read_text()):
                read.setdefault(name, set()).add(script.parent.parent.name)
        missing = {name: sorted(tasks) for name, tasks in read.items()
                   if name in documented and name not in SET_BY_SETUP and name not in ed.HOST_BUILD_CONFIG}
        self.assertEqual(missing, {})

    def test_a0_fetch_settings_come_from_dotenv(self):
        read = set()
        for script in sorted((ROOT / "tasks" / "editing").glob("*/a0/*.sh")):
            read |= set(re.findall(r"\$\{?(AGENTSWE_[A-Z0-9_]+)", script.read_text()))
        self.assertTrue(read)
        self.assertEqual(sorted(read - {"AGENTSWE_PROFILE"} - set(ed.A0_FETCH_CONFIG)), [])

    def test_dyad_browser_mirrors(self):
        script = (ROOT / "tasks" / "editing" / "dyad-acceptance-driven" / "env" / "build-host.sh").read_text()
        for name in ("AGENTSWE_CFT_BASE_URL", "AGENTSWE_PLAYWRIGHT_DOWNLOAD_HOST"):
            self.assertIn("${" + name + ":-", script)
            self.assertIn(name, ed.HOST_BUILD_CONFIG)


if __name__ == "__main__":
    unittest.main()
