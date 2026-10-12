from __future__ import annotations

import os
import sys
import shutil
import tempfile
import unittest
from pathlib import Path

HARNESS = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(HARNESS))

import evaluate_case as subject


class EvaluateCaseHelpersTest(unittest.TestCase):
    def test_extracts_multiline_proof_and_counts_only_code(self) -> None:
        source = """theorem target (n : Nat) :
    n + 0 = n := by
  -- comment
  rw [Nat.add_zero]

theorem next : True := by
  trivial
"""
        proof = subject.declaration_proof(source, "target")
        self.assertIn("rw [Nat.add_zero]", proof["body"])
        self.assertEqual(subject.proof_noncomment_lines(proof["body"]), 1)

    def test_duplicate_leaf_declarations_are_rejected(self) -> None:
        source = "theorem target : True := by\n  trivial\n\ntheorem target : True := by\n  trivial\n"
        with self.assertRaisesRegex(ValueError, "found 2"):
            subject.declaration_proof(source, "target")

    def test_mask_allows_only_target_body_changes(self) -> None:
        before = "theorem target : True := by\n  trivial\n\ntheorem fixed : True := by\n  trivial\n"
        after = "theorem target : True := by exact True.intro\n\ntheorem fixed : True := by\n  trivial\n"
        self.assertEqual(
            subject.mask_target_proofs(before, ["target"]),
            subject.mask_target_proofs(after, ["target"]),
        )
        changed_fixed = after.replace("theorem fixed : True", "theorem fixed : False")
        self.assertNotEqual(
            subject.mask_target_proofs(before, ["target"]),
            subject.mask_target_proofs(changed_fixed, ["target"]),
        )

    def test_comment_tokens_do_not_trigger(self) -> None:
        clean = subject.strip_lean_comments("by\n  -- sorry native_decide\n  exact True.intro\n")
        self.assertFalse(subject.contains_token(clean, "sorry"))
        self.assertFalse(subject.contains_token(clean, "native_decide"))

    def test_patch_safety_rejects_traversal_and_symlink_mode(self) -> None:
        patch = "diff --git a/../escape b/../escape\n--- a/../escape\n+++ b/../escape\nnew file mode 120000\n"
        errors = subject.safe_patch_text(patch)
        self.assertTrue(any("unsafe diff path" in error for error in errors))
        self.assertTrue(any("unsafe patch marker path" in error for error in errors))
        self.assertTrue(any("regular mode" in error for error in errors))

    def test_policies_have_eight_cases(self) -> None:
        policies = subject.load_policies()
        self.assertEqual(len(policies), 8)
        self.assertEqual(sum(1 for policy in policies.values() if policy["mode"] == "unprovable"), 1)

    def test_exact_report_types_reject_booleans_nonfinite_and_extra_validation_fields(self) -> None:
        valid = [{"command": "lake build", "exit_code": 0, "observation": "passed"}]
        self.assertTrue(subject.valid_validation(valid))
        self.assertFalse(subject.valid_validation([
            {"command": "lake build", "exit_code": False, "observation": "passed"}
        ]))
        self.assertFalse(subject.valid_validation([
            {"command": "lake build", "exit_code": 0, "observation": "passed", "extra": 1}
        ]))
        self.assertFalse(subject.is_plain_int(True))
        self.assertFalse(subject.is_finite_nonnegative_number(True))
        self.assertFalse(subject.is_finite_nonnegative_number(float("nan")))
        self.assertFalse(subject.is_finite_nonnegative_number(-0.1))
        self.assertTrue(subject.is_finite_nonnegative_number(10**10000))

    def test_json_loader_rejects_duplicate_keys(self) -> None:
        with tempfile.TemporaryDirectory(prefix="formal-v4-json-test-") as temp:
            path = Path(temp) / "report.json"
            path.write_text('{"status":"success","status":"error"}\n', encoding="utf-8")
            self.assertIsNone(subject.load_json_object(path))

    def test_validation_command_evidence_must_be_exact(self) -> None:
        exact = {"validation": [{"command": "/toolchain/bin/lake build", "exit_code": 0, "observation": "ok"}]}
        wrapped = {"validation": [{"command": "echo lake build", "exit_code": 0, "observation": "not real"}]}
        suffix = {"validation": [{"command": "lake build --help", "exit_code": 0, "observation": "not build"}]}
        self.assertTrue(subject.validation_has(exact, ["lake", "build"]))
        self.assertFalse(subject.validation_has(wrapped, ["lake", "build"]))
        self.assertFalse(subject.validation_has(suffix, ["lake", "build"]))

    def test_subprocess_environment_does_not_inherit_credentials(self) -> None:
        with tempfile.TemporaryDirectory(prefix="formal-v4-env-test-") as temp:
            root = Path(temp)
            home = root / "home"
            home.mkdir()
            previous = os.environ.get("GATEWAY_API_KEY")
            os.environ["GATEWAY_API_KEY"] = "sentinel-secret-that-must-not-leak"
            try:
                observed = subject.run_command(
                    [sys.executable, "-c", "import os; print(os.environ.get('GATEWAY_API_KEY'))"],
                    root,
                    home,
                    30,
                )
            finally:
                if previous is None:
                    os.environ.pop("GATEWAY_API_KEY", None)
                else:
                    os.environ["GATEWAY_API_KEY"] = previous
            self.assertEqual(observed["exit_code"], 0, observed["output"])
            self.assertEqual(observed["output"].strip(), "None")

    @unittest.skipUnless(shutil.which("lake"), "Lean/Lake is not available")
    def test_semantic_dependency_checker_rejects_dead_reference_mutation(self) -> None:
        with tempfile.TemporaryDirectory(prefix="formal-v4-dependency-test-") as temp:
            root = Path(temp)
            repo = root / "repo"
            home = root / "home"
            repo.mkdir()
            home.mkdir()
            (repo / "lakefile.toml").write_text(
                'name = "dependency_mutation_test"\ndefaultTargets = ["Main"]\n[[lean_lib]]\nname = "Main"\n',
                encoding="utf-8",
            )
            (repo / "lean-toolchain").write_text("leanprover/lean4:v4.19.0\n", encoding="utf-8")
            (repo / "Main.lean").write_text(
                """theorem premise (n : Nat) : n = n := by
  rfl

theorem genuine (n : Nat) : n = n := by
  exact premise n

theorem dead (n : Nat) : n = n := by
  have _required := premise n
  rfl
""",
                encoding="utf-8",
            )
            build = subject.run_command(["lake", "build"], repo, home, 120)
            self.assertEqual(build["exit_code"], 0, build["output"])
            genuine = subject.run_dependency_checker(repo, home, "Main", [("genuine", "premise")])
            dead = subject.run_dependency_checker(repo, home, "Main", [("dead", "premise")])
            self.assertIsNotNone(genuine)
            self.assertIsNotNone(dead)
            assert genuine is not None and dead is not None
            self.assertEqual(genuine["exit_code"], 0, genuine["output"])
            self.assertNotEqual(dead["exit_code"], 0)
            self.assertIn("not retained by the proof", dead["output"])


if __name__ == "__main__":
    unittest.main()
