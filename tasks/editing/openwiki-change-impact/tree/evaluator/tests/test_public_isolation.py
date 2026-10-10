from __future__ import annotations

import shutil
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]


class PublicPackageIsolationTests(unittest.TestCase):
    def test_public_package_contains_only_documented_builder_surfaces(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            package = Path(temp) / "public-package"
            package.mkdir()
            shutil.copy2(ROOT / "README.md", package / "README.md")
            shutil.copytree(ROOT / "input", package / "input")
            shutil.copytree(ROOT / "dev_cases", package / "dev_cases", ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))

            self.assertFalse((package / "test_cases").exists())
            self.assertFalse((package / "evaluator").exists())
            self.assertFalse((package / "meta").exists())
            self.assertEqual(len(list((package / "dev_cases").glob("dev_???"))), 2)
            self.assertFalse(list(package.rglob("case.json")))

    def test_public_runner_has_no_private_import_or_path_dependency(self) -> None:
        support = (ROOT / "dev_cases" / "harness_support.py").read_text(encoding="utf-8")
        runner = (ROOT / "dev_cases" / "run_public.py").read_text(encoding="utf-8")
        for forbidden in ("evaluator/manifests", "test_cases/", "meta/"):
            self.assertNotIn(forbidden, support)
            self.assertNotIn(forbidden, runner)


if __name__ == "__main__":
    unittest.main()
