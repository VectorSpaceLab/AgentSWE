"""The Repository repair harness writes its result even when it cannot remove its scratch tree.

Submitted code (public tests, hidden checks and the recovery self-test) runs as uid 65534 and may create
directories under {work}, HOME or TMPDIR, which live in the harness's scratch tree. The verifier runs without
CAP_DAC_OVERRIDE and CAP_FOWNER, so it can neither unlink inside such a directory (EACCES) nor chmod it
(EPERM). tempfile.TemporaryDirectory's cleanup retries through chmod, and that EPERM escapes even with
ignore_cleanup_errors=True: the harness used to exit before writing harness_result.json, and the verifier
reported the case as a missing result (an infrastructure error) instead of a score.

    python3 -m unittest tests/test_creation_repo_scratch_cleanup.py   (standard library and git; no bytecode in task trees)

The tests simulate the missing capabilities by making os.unlink fail with EACCES for the file the submitted code
left and os.chmod fail with EPERM for its directory, so they behave the same as root or as any other user.
"""
from __future__ import annotations

import contextlib
import errno
import importlib.util
import io
import json
import os
import shutil
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
HARNESS = ROOT / "tasks" / "creation" / "repository-bug-repair" / "benchmark" / "evaluator" / "harness" / "evaluate_case.py"
LEFT_DIRECTORY = "fixtures"
LEFT_FILE = "mixed.jsonl"


def load_harness():
    spec = importlib.util.spec_from_file_location("repo_evaluate_case_under_test", HARNESS)
    module = importlib.util.module_from_spec(spec)
    previous = sys.dont_write_bytecode
    sys.dont_write_bytecode = True
    try:
        spec.loader.exec_module(module)
    finally:
        sys.dont_write_bytecode = previous
    return module


@contextlib.contextmanager
def without_dac_override_or_fowner():
    """os.unlink of the left file fails with EACCES and os.chmod of its directory with EPERM, as in the verifier."""
    real_unlink, real_chmod = os.unlink, os.chmod

    def unlink(path, *args, **kwargs):
        if os.path.basename(os.fsdecode(path)) == LEFT_FILE:
            raise PermissionError(errno.EACCES, os.strerror(errno.EACCES), path)
        return real_unlink(path, *args, **kwargs)

    def chmod(path, *args, **kwargs):
        if os.path.basename(os.fsdecode(path)) == LEFT_DIRECTORY:
            raise PermissionError(errno.EPERM, os.strerror(errno.EPERM), path)
        return real_chmod(path, *args, **kwargs)

    with mock.patch("os.unlink", unlink), mock.patch("os.chmod", chmod):
        yield


def leave_directory(work: Path) -> None:
    """What a recovery self-test does when it keeps fixtures in a subdirectory of {work}."""
    (work / LEFT_DIRECTORY).mkdir()
    (work / LEFT_DIRECTORY / LEFT_FILE).write_text("{}\n", encoding="utf-8")


class ScratchDirectoryTests(unittest.TestCase):
    def setUp(self):
        self.harness = load_harness()
        self.outer = Path(tempfile.mkdtemp(prefix="repo-scratch-test-"))
        self.addCleanup(shutil.rmtree, self.outer, True)

    def test_failed_cleanup_is_reported_not_raised(self):
        stderr = io.StringIO()
        with mock.patch.object(tempfile, "tempdir", str(self.outer)), contextlib.redirect_stderr(stderr):
            with without_dac_override_or_fowner():
                with self.harness.evaluation_directory("repair-v4-") as name:
                    work = Path(name) / "recovery-work"
                    work.mkdir()
                    leave_directory(work)
        report = json.loads(stderr.getvalue().strip().splitlines()[-1])
        self.assertEqual(report["evaluation_directory_cleanup"], "incomplete")
        self.assertEqual(report["path"], name)
        self.assertTrue(report["remaining"])
        self.assertIn("PermissionError", report["error"])
        # Everything the evaluator itself can remove is gone; only the directory the submitted code owns is left.
        self.assertEqual(
            sorted(str(p.relative_to(name)) for p in Path(name).rglob("*")),
            ["recovery-work", "recovery-work/fixtures", "recovery-work/fixtures/mixed.jsonl"],
        )

    def test_normal_cleanup_removes_the_tree_silently(self):
        stderr = io.StringIO()
        with mock.patch.object(tempfile, "tempdir", str(self.outer)), contextlib.redirect_stderr(stderr):
            with self.harness.evaluation_directory("repair-v4-") as name:
                work = Path(name) / "recovery-work"
                work.mkdir()
                leave_directory(work)
        self.assertFalse(os.path.lexists(name))
        self.assertEqual(stderr.getvalue(), "")


@unittest.skipUnless(shutil.which("git"), "git is required to apply the candidate patch")
class HarnessResultTests(unittest.TestCase):
    """main() on a case whose submitted code leaves a directory the verifier cannot remove."""

    def setUp(self):
        self.harness = load_harness()
        self.outer = Path(tempfile.mkdtemp(prefix="repo-result-test-"))
        self.addCleanup(shutil.rmtree, self.outer, True)
        case = self.outer / "case" / "dev_009"
        repository = case / "assets" / "repository"
        (repository / "src" / "pkg").mkdir(parents=True)
        (repository / "tests").mkdir()
        (repository / "src" / "pkg" / "__init__.py").write_text("", encoding="utf-8")
        (repository / "src" / "pkg" / "core.py").write_text("def value():\n    return 1\n", encoding="utf-8")
        (repository / "tests" / "test_core.py").write_text("", encoding="utf-8")
        contract = {
            "schema_version": "1.0", "repository": "assets/repository", "allowed_paths": ["src/pkg/"],
            "public_test_command": ["{python}", "-m", "unittest", "discover", "-s", "tests", "-v"],
            "network": "closed", "recovery": "none", "max_patch_bytes": 2000000,
        }
        (case / "input.md").write_text("# case\n\n```repair_contract\n" + json.dumps(contract) + "\n```\n", encoding="utf-8")
        output = self.outer / "output"
        output.mkdir()
        (output / "solution.patch").write_text(
            "diff --git a/src/pkg/core.py b/src/pkg/core.py\n"
            "--- a/src/pkg/core.py\n+++ b/src/pkg/core.py\n"
            "@@ -1,2 +1,2 @@\n def value():\n-    return 1\n+    return 2\n",
            encoding="utf-8",
        )
        (output / "repair_report.json").write_text("{}\n", encoding="utf-8")
        self.case, self.output, self.result = case, output, self.outer / "verifier" / "harness_result.json"

    def fake_isolated_run(self, script, argv, repo, work, env, audit_dir, *, trace=False):
        if not (work / LEFT_DIRECTORY).exists():
            leave_directory(work)
        return {"exit_code": 0, "output": ""}

    def test_result_is_written_when_cleanup_fails(self):
        argv = ["evaluate_case.py", "--case-dir", str(self.case), "--output-dir", str(self.output),
                "--result", str(self.result)]
        stderr = io.StringIO()
        with mock.patch.object(tempfile, "tempdir", str(self.outer)), mock.patch.object(sys, "argv", argv), \
                mock.patch.object(self.harness, "isolated_run", self.fake_isolated_run), \
                contextlib.redirect_stderr(stderr), without_dac_override_or_fowner():
            exit_code = self.harness.main()
        self.assertEqual(exit_code, 0)
        result = json.loads(self.result.read_text(encoding="utf-8"))
        self.assertEqual(result["case"], "dev_009")
        self.assertEqual(result["evaluation_state"], "scoreable")
        self.assertTrue(result["validity_gate"])
        self.assertEqual(result["scope"]["changed_files"], ["src/pkg/core.py"])
        self.assertTrue(result["checks"]["public_pass"] and result["checks"]["hidden_pass"])
        self.assertNotIn("evaluation_directory_cleanup", json.dumps(result))
        self.assertIn('"evaluation_directory_cleanup": "incomplete"', stderr.getvalue())


if __name__ == "__main__":
    unittest.main()
