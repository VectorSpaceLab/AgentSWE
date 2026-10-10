import json
import tempfile
import unittest
from pathlib import Path

from presubmit_validator import META, REQUIRED, paths, tree, validate


PATCH = "diff --git a/a.txt b/a.txt\n--- a/a.txt\n+++ b/a.txt\n@@ -1 +1 @@\n-a\n+b\n"


class PresubmitTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()
        self.source = self.root / "source"
        self.source.mkdir()
        (self.source / "a.txt").write_text("a\n")
        self.candidate = self.root / "candidate"
        self.candidate.mkdir()
        self.meta = dict(builder_session_id="native-session", submission_number=1,
                         revision_of_candidate_digest=None, feedback_digest=None)
        self.edit = dict(schema_version="1.0", feature_summary="Implement public correction", changed_paths=["a.txt"],
                         commands=[], compatibility_notes=[], limitations=[])
        self.report = dict(schema_version="1.0", status="success", artifact_paths=list(REQUIRED), errors=[],
                           runtime_seconds=0, peak_memory_bytes=0, api_calls=dict(gateway=0, serper=0, web_retrieval=0), **self.meta)
        self.write()
        self.builds = 0

    def write(self, patch=PATCH):
        (self.candidate / "solution.patch").write_text(patch)
        (self.candidate / "edit_report.json").write_text(json.dumps(self.edit))
        (self.candidate / "run_report.json").write_text(json.dumps(self.report))

    def build(self, repo):
        self.builds += 1
        self.assertEqual((repo / "a.txt").read_text(), "b\n")
        return dict(exit_code=0, toolchain_digest="a" * 64)

    def validate(self, **kwargs):
        args = dict(expected_metadata=self.meta, build_runner=self.build, expected_toolchain_digest="a" * 64)
        args.update(kwargs)
        return validate(self.candidate, self.source, **args)

    def test_controlled_build_and_pristine_immutability(self):
        before = tree(self.source)
        result = self.validate()
        self.assertTrue(result["valid"], result)
        self.assertEqual(self.builds, 1)
        self.assertEqual(tree(self.source), before)
        self.assertEqual(result["checks"]["materialized_source_digest_before_build"], result["checks"]["materialized_source_digest_after_build"])

    def test_mutating_callback_cannot_forge_digest(self):
        def malicious(repo):
            (repo / "Cargo.lock").write_text("changed")
            return dict(exit_code=0, toolchain_digest="a" * 64, before_digest=tree(repo), after_digest=tree(repo))
        result = self.validate(build_runner=malicious)
        self.assertFalse(result["valid"])
        self.assertIn("mutated", result["errors"][0])

    def test_failed_patch_never_builds(self):
        self.write(PATCH.replace("-a\n", "-missing\n"))
        self.assertFalse(self.validate()["valid"])
        self.assertEqual(self.builds, 0)

    def test_metadata_bound_to_evaluator(self):
        self.report["builder_session_id"] = "attacker"
        self.write()
        self.assertFalse(self.validate()["valid"])
        self.assertEqual(self.builds, 0)
        self.assertFalse(self.validate(expected_metadata=None)["valid"])

    def test_full_schema_validated(self):
        mutations = [dict(runtime_seconds=-1), dict(runtime_seconds=True), dict(runtime_seconds=float("nan")),
                     dict(peak_memory_bytes=-1), dict(api_calls={"gateway": 0}), dict(status=""), dict(errors=[1])]
        for values in mutations:
            old = dict(self.report)
            self.report.update(values); self.write()
            self.assertFalse(self.validate()["valid"], values)
            self.report = old
        self.assertEqual(self.builds, 0)

    def test_changed_paths_and_command_schema(self):
        self.edit["changed_paths"] = ["other.txt"]
        self.write()
        self.assertFalse(self.validate()["valid"])
        self.edit["changed_paths"] = ["a.txt"]
        self.edit["commands"] = [{"command": "test", "exit_code": False, "result": "ok"}]
        self.write()
        self.assertFalse(self.validate()["valid"])

    def test_symlink_submission_rejected(self):
        p = self.candidate / "solution.patch"
        p.rename(self.root / "patch")
        p.symlink_to(self.root / "patch")
        self.assertFalse(self.validate()["valid"])
        self.assertEqual(self.builds, 0)

    def test_parent_symlink_rejected(self):
        alias = self.root / "alias"
        alias.symlink_to(self.candidate, target_is_directory=True)
        result = validate(alias, self.source, expected_metadata=self.meta, build_runner=self.build, expected_toolchain_digest="a" * 64)
        self.assertFalse(result["valid"])

    def test_extra_file_and_directory_rejected(self):
        (self.candidate / "extra").write_text("extra")
        self.assertFalse(self.validate()["valid"])
        (self.candidate / "extra").unlink()
        (self.candidate / "edit_report.json").unlink()
        (self.candidate / "edit_report.json").mkdir()
        self.assertFalse(self.validate()["valid"])

    def test_all_git_path_headers_checked(self):
        unsafe = [
            "diff --git a/../../outside b/a.txt\n",
            "diff --git a/a.txt b/a.txt\n--- a/a.txt\n+++ /etc/passwd\n",
            "diff --git a/a.txt b/a.txt\nrename from ../../outside\nrename to a.txt\n",
            "diff --git a/a.txt b/a.txt\ncopy from a.txt\ncopy to .git/config\n",
            "diff --git a/.env b/.env\n",
            "diff --git a/a.txt b/a.txt\nnew file mode 120000\n",
            "diff --git a/a.txt b/a.txt\nindex abc..def 160000\n",
            "diff --git a/a.txt b/a.txt\nGIT binary patch\n",
            "diff --git a/a.txt b/a.txt\nrename from C:/outside\n",
            PATCH + "--- a/a.txt\n+++ /etc/passwd\n",
        ]
        for patch in unsafe:
            self.write(patch)
            with self.assertRaises(ValueError, msg=patch):
                paths(self.candidate / "solution.patch")
        self.assertEqual(self.builds, 0)

    def test_safe_new_file_and_rename_headers(self):
        patch = "diff --git a/new.txt b/new.txt\nnew file mode 100644\n--- /dev/null\n+++ b/new.txt\n@@ -0,0 +1 @@\n+new\n"
        self.write(patch)
        self.assertEqual(paths(self.candidate / "solution.patch"), ["new.txt"])
        self.write("diff --git a/a.txt b/new.txt\nsimilarity index 100%\nrename from a.txt\nrename to new.txt\n")
        self.assertEqual(paths(self.candidate / "solution.patch"), ["a.txt", "new.txt"])

    def test_source_link_escape_and_toolchain_mismatch(self):
        (self.source / "link").symlink_to("/etc")
        self.assertFalse(self.validate()["valid"])
        self.assertEqual(self.builds, 0)
        (self.source / "link").unlink()
        self.assertFalse(self.validate(expected_toolchain_digest="b" * 64)["valid"])

    def test_second_round_requires_exact_feedback_and_explanation(self):
        self.meta.update(submission_number=2, revision_of_candidate_digest="b" * 64, feedback_digest="c" * 64)
        self.report.update(self.meta)
        self.write()
        self.assertFalse(self.validate()["valid"])
        self.edit["feedback_response"] = "Corrected the public feedback invariant."
        self.write()
        self.assertTrue(self.validate()["valid"])
        self.report["feedback_digest"] = "d" * 64
        self.write()
        self.assertFalse(self.validate()["valid"])

    def test_previous_materialization_cannot_be_resubmitted(self):
        first = self.validate()
        result = self.validate(previous_materialized_digest=first["checks"]["materialized_source_digest_before_build"])
        self.assertFalse(result["valid"])
        self.assertEqual(self.builds, 1)


if __name__ == "__main__":
    unittest.main()
