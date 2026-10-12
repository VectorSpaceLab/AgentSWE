from __future__ import annotations

from dataclasses import asdict
import hashlib
import json
from pathlib import Path
import re
import shutil
import subprocess
import sys
import tempfile
import unittest


ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "dev_cases"))
sys.path.insert(0, str(ROOT / "evaluator" / "harness"))

import benchmark_harness  # noqa: E402
import public_harness  # noqa: E402
import public_runner  # noqa: E402
from oracles import SPECS as HIDDEN_SPECS  # noqa: E402
from public_specs import SPECS as PUBLIC_SPECS  # noqa: E402


class PublicPackageIsolationTest(unittest.TestCase):
    def test_public_package_has_exact_inventory_and_no_private_leakage(self):
        self.assertIs(benchmark_harness.prepare_submission, public_harness.prepare_submission)
        self.assertIs(public_runner.prepare_submission, public_harness.prepare_submission)
        self.assertEqual(tuple(PUBLIC_SPECS), ("dev_001", "dev_002"))
        for case_id in PUBLIC_SPECS:
            assets = ROOT / "dev_cases" / case_id / "assets"
            self.assertTrue((assets / "attestation_policy.json").is_file())
            self.assertTrue((assets / "notification_policy.json").is_file())
        public_files = [path for path in (ROOT / "dev_cases").rglob("*") if path.is_file()]
        public_text = "\n".join(path.read_text(encoding="utf-8", errors="ignore").lower() for path in public_files if path.suffix in {".py", ".json", ".md", ".txt", ".csv"})
        self.assertNotRegex(public_text, r"test_00[1-6]")
        for token in ("evaluator/", "test_cases/", "meta/", "hidden_evaluations", "formal_submissions"):
            self.assertNotIn(token, public_text)
        hidden_hashes: set[str] = set()
        for path in (ROOT / "test_cases").rglob("*"):
            if path.is_file():
                hidden_hashes.add(hashlib.sha256(path.read_bytes()).hexdigest())
                hidden_hashes.update(re.findall(r"\b[0-9a-f]{64}\b", path.read_text(encoding="utf-8", errors="ignore")))
        public_hashes = set(re.findall(r"\b[0-9a-f]{64}\b", public_text))
        self.assertFalse(hidden_hashes & public_hashes)

    def test_package_runs_from_only_input_and_dev_cases(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            package = root / "package"
            package.mkdir()
            shutil.copytree(ROOT / "input", package / "input")
            shutil.copytree(ROOT / "dev_cases", package / "dev_cases")
            submission = root / "submission"
            submission.mkdir()
            patch = "diff --git a/README.md b/README.md\n--- a/README.md\n+++ b/README.md\n@@ -1,1 +1,2 @@\n+<!-- isolation probe -->\n <div align=\"center\">\n"
            (submission / "solution.patch").write_text(patch)
            (submission / "edit_report.json").write_text(json.dumps({"schema_version":1,"feature_summary":"probe","changed_paths":["README.md"],"commands_and_results":[],"compatibility_notes":[],"limitations":[]}))
            (submission / "run_report.json").write_text(json.dumps({"schema_version":"1.0","status":"not_run","artifact_paths":["solution.patch","edit_report.json","run_report.json"],"errors":[],"runtime_seconds":0,"peak_memory_bytes":0,"api_calls":{"gateway":0,"serper":0,"web_retrieval":0}}))
            result = subprocess.run([sys.executable, "dev_cases/run_public.py", "--submission", str(submission), "--case", "dev_001", "--work-dir", str(root / "work")], cwd=package, text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=120)
            self.assertEqual(result.returncode, 0, result.stderr)
            payload = json.loads(result.stdout)
            self.assertTrue(payload["valid"])
            self.assertEqual(payload["score"], 0)
            self.assertEqual(sum(item["possible"] for item in payload["assertions"]), 100)
            self.assertTrue((root / "work" / "repository" / "README.md").read_text().startswith("<!-- isolation probe -->"))


if __name__ == "__main__":
    unittest.main()
