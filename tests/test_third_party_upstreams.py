"""Every task names its upstream repository and license in task.json, and THIRD_PARTY.md lists each of them (stdlib
unittest). Six Creation tasks named only their role, and THIRD_PARTY.md listed neither the upstream material in the
repository nor the Creation requirement sources."""
from __future__ import annotations

import json
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


class Upstreams(unittest.TestCase):
    def test_every_task_names_repo_and_license(self):
        third_party = (ROOT / "THIRD_PARTY.md").read_text()
        tasks = sorted((ROOT / "tasks").glob("*/*/task.json"))
        self.assertEqual(len(tasks), 25)
        for path in tasks:
            upstream = json.loads(path.read_text())["upstream"]
            with self.subTest(task=path.parent.name):
                self.assertTrue(upstream.get("repo", "").startswith("https://github.com/"))
                self.assertTrue(upstream.get("commit") or upstream.get("commit_note"))
                self.assertNotIn("see upstream", upstream.get("license", "see upstream"))
                if path.parts[-3] != "editing":  # Editing targets are fetched by commit, not listed one by one
                    self.assertIn(upstream["repo"].removeprefix("https://github.com/"), third_party)

    def test_license_texts_present(self):
        for name in ("GPL-2.0.txt", "GPL-3.0.txt", "LGPL-3.0.txt", "upstream-MIT-notices.txt"):
            self.assertTrue((ROOT / "third_party" / "licenses" / name).is_file(), name)
        notices = (ROOT / "third_party" / "licenses" / "upstream-MIT-notices.txt").read_text()
        for owner in ("OpenAI", "Sierra Research", "PinchBench"):
            self.assertIn(f"Copyright (c) 20", notices)
            self.assertIn(owner, notices)


if __name__ == "__main__":
    unittest.main()
