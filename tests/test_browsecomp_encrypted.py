"""BrowseComp rows and gold stay encrypted in the repository (upstream canary scheme) and are decrypted only into the
staged benchmark under $AGENTSWE_HOME (stdlib unittest, no network)."""
from __future__ import annotations

import json
import os
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from agentswe import config, profiles  # noqa: E402
from agentswe.registry import find  # noqa: E402

TASK = ROOT / "tasks" / "optimization" / "browsecomp"
BUNDLE = TASK / "encrypted" / "browsecomp_rows.json"


class EncryptedRows(unittest.TestCase):
    def test_repository_holds_no_plaintext_rows_or_gold(self):
        bench = TASK / "benchmark"
        self.assertFalse((bench / "evaluator" / "gold.json").exists())
        self.assertEqual(list(bench.glob("dev_cases/*/input.md")) + list(bench.glob("test_cases/*/input.md")), [])
        data = json.loads(BUNDLE.read_text(encoding="utf-8"))
        self.assertIn("canary GUID browsecomp:", data["canary"])
        self.assertEqual(len(data["rows"]), 150)

    def test_decrypts_to_recorded_bytes(self):
        with tempfile.TemporaryDirectory() as tmp:
            written = profiles.decrypt_rows(BUNDLE, Path(tmp))
            self.assertEqual(len(written), 151)
            gold = json.loads((Path(tmp) / "evaluator" / "gold.json").read_text(encoding="utf-8"))
            self.assertEqual(len([c for c in gold if c.startswith("dev_")]), 50)
            self.assertEqual(len([c for c in gold if c.startswith("test_")]), 100)

    def test_tampered_ciphertext_is_refused(self):
        data = json.loads(BUNDLE.read_text(encoding="utf-8"))
        row = data["rows"][0]
        row["answer"] = row["answer"][:-4] + ("AAAA" if not row["answer"].endswith("AAAA") else "BBBB")
        with tempfile.TemporaryDirectory() as tmp:
            bad = Path(tmp) / "rows.json"
            bad.write_text(json.dumps(data), encoding="utf-8")
            with self.assertRaises((SystemExit, UnicodeDecodeError, ValueError)):
                profiles.decrypt_rows(bad, Path(tmp) / "out")

    def test_staged_benchmark_is_decrypted_under_home(self):
        task = find("browsecomp")
        saved = os.environ.get("AGENTSWE_HOME")
        with tempfile.TemporaryDirectory() as home:
            os.environ["AGENTSWE_HOME"] = home
            try:
                cfg = config.load("/nonexistent/.env")
                staged = profiles.staged(cfg, task, "benchmark")
                self.assertTrue(str(staged).startswith(home))
                self.assertTrue((staged / "evaluator" / "gold.json").is_file())
                self.assertEqual(len(list(staged.glob("test_cases/*/input.md"))), 100)
                self.assertEqual((staged / "task_contract.json").read_bytes(),
                                 (task.dir / "benchmark" / "task_contract.json").read_bytes())
                self.assertEqual(profiles.staged(cfg, task, "benchmark"), staged)  # reused when unchanged
            finally:
                if saved is None:
                    os.environ.pop("AGENTSWE_HOME", None)
                else:
                    os.environ["AGENTSWE_HOME"] = saved


if __name__ == "__main__":
    unittest.main()
