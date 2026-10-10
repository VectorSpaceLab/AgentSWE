import json
import hashlib
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import formal_axes_shared as axes


class AcceptanceBoundary(unittest.TestCase):
    def test_selection_is_explicit_and_validated(self):
        self.assertEqual(axes.selected_case_ids([]), axes.CASES)
        self.assertEqual(axes.selected_case_ids(["--acceptance-cases", "test_006", "test_001"]), ("test_001", "test_006"))
        for values in (("test_001", "test_001"), ("dev_001",)):
            with self.assertRaises(ValueError):
                axes.selected_case_ids(["--acceptance-cases", *values])

    def run_case(self, selected, acceptance):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            run = root / "run"
            run.mkdir()
            (root / "input").mkdir()
            (root / "input" / "goal.md").write_text("public")
            (root / "evaluator").mkdir()
            (root / "evaluator" / "code_rubric.md").write_text("code")
            fake_code_judge = root / "evaluator" / "code_judge.py"
            fake_code_judge.write_text("# test-only command intercepted before execution")
            (run / "candidate").mkdir()
            (run / "credential-placeholder").write_text("not-a-secret")
            (run / "build.json").write_text('{"error":"genuine Candidate compile failure"}')
            digest = hashlib.sha256(b"").hexdigest()
            freeze = {"hidden_allowed": True, "candidate_digest": digest, "candidate_path": str(run / "candidate")}
            (run / "freeze_manifest.json").write_text(json.dumps(freeze))
            records = [{"case_id": c, "candidate_digest": digest, "classification": "candidate_build_failure",
                        "execution_attempted": True, "environment_preflight": {"valid": True},
                        "failure_attribution": {"party": "candidate", "observed_by": "evaluator", "fatal": True,
                                                "reason": "source type error", "evidence_paths": [str(run / "build.json")]}}
                       for c in selected]
            hidden = {"expected_cases": list(selected), "executed_cases": list(selected), "cases": records,
                      **{k: True for k in ("complete_inventory", "all_cases_materialized", "all_cases_real",
                                           "all_cases_started_after_freeze", "frozen_digest_stable")}}
            (run / "hidden-after-freeze-attestation.json").write_text(json.dumps(hidden))
            def code_judge(command, **kwargs):
                self.assertIn("--candidate-source", command)
                out = Path(command[command.index("--output-dir") + 1])
                out.mkdir(parents=True, exist_ok=True)
                contract = {"candidate_digest": digest, "contract_valid": True, "code_score_publishable": True,
                            "code_score": 0, "code_raw_score": 0,
                            "code_dimensions": {name: {"score": 0, "max": maximum} for name, maximum in axes.CODE_MAXIMA.items()},
                            "judge": {"model": "deepseek-flash", "reasoning_effort": "max"},
                            "provider_usage": {"logical_requests": 1, "completed_responses": 1, "transport_attempts": 1,
                                               "input_tokens": 10, "output_tokens": 5, "total_tokens": 15}}
                (out / "code_score_contract.json").write_text(json.dumps(contract))
                return type("Completed", (), {"returncode": 0, "stdout": "", "stderr": ""})()
            argv = ["--run-dir", str(run), "--credential-file", str(run / "credential-placeholder")]
            if acceptance:
                argv += ["--acceptance-cases", *selected]
            with patch.object(axes, "ROOT", root), patch.object(axes, "CODE_JUDGE", fake_code_judge), \
                    patch.object(axes.subprocess, "run", side_effect=code_judge) as invoke, \
                    patch.object(axes, "judge_broker_stats") as stats, patch("builtins.print"):
                code = axes.main(argv)
            stats.assert_not_called()
            # The Code axis is retired (2026-09-19, CODE_AXIS_POLICY skipped_by_policy): no Code judge is dispatched.
            self.assertEqual(invoke.call_count, 0)
            name = "acceptance_aggregation.json" if acceptance else "formal_aggregation.json"
            result = json.loads((run / name).read_text())
            self.assertEqual(result["code_axis"], axes.CODE_AXIS_POLICY_RECORD)
            return code, result

    def test_single_case_zero_is_acceptance_not_formal(self):
        code, result = self.run_case(("test_001",), True)
        self.assertEqual(code, 0)
        self.assertTrue(result["acceptance_complete"])
        self.assertFalse(result["formal_result_publishable"])
        self.assertEqual(result["result_axis"]["score"], 0)

    def test_six_case_acceptance_cannot_claim_formal(self):
        code, result = self.run_case(axes.CASES, True)
        self.assertEqual(code, 0)
        self.assertTrue(result["acceptance_complete"])
        self.assertFalse(result["formal_complete"])

    def test_six_valid_candidate_zeros_aggregate_without_result_judge(self):
        code, result = self.run_case(axes.CASES, False)
        self.assertEqual(code, 0)
        self.assertTrue(result["formal_result_publishable"])
        self.assertEqual(len(result["candidate_zero_cases"]), 6)
        self.assertEqual(result["semantically_judged_cases"], [])

    def test_frozen_bytes_are_rechecked_even_when_manifest_is_unchanged(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = root / "source"
            source.mkdir()
            freeze = {"candidate_path": str(source), "candidate_digest": hashlib.sha256(b"").hexdigest()}
            self.assertEqual(axes.frozen_identity_errors(freeze, root), [])
            (source / "changed.py").write_text("tampered after freeze")
            self.assertIn("actual frozen source digest mismatch", axes.frozen_identity_errors(freeze, root))

    def test_code_scope_cannot_omit_new_candidate_code(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "entry.py").write_text("pass")
            (root / "new.py").write_text("new behavior")
            scope = {"schema_version": "agentswe-edit-code-scope/v1", "candidate_digest": "current",
                     "valid": True, "errors": [],
                     "baseline_expected_digest": "baseline", "baseline_observed_digest": "baseline",
                     "complete_change_coverage": True, "evidence_paths": ["entry.py"],
                     "changed_paths": ["entry.py"], "added_paths": ["new.py"], "deleted_paths": []}
            with self.assertRaisesRegex(ValueError, "omits changed"):
                axes.checked_code_scope(scope, root, "current")
            scope["evidence_paths"].append("new.py")
            self.assertEqual(axes.checked_code_scope(scope, root, "current"), ["entry.py", "new.py"])

    def test_code_scope_rejects_planner_errors_even_with_complete_coverage(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "entry.py").write_text("pass")
            scope = {"schema_version": "agentswe-edit-code-scope/v1", "candidate_digest": "current",
                     "valid": True, "errors": [], "baseline_expected_digest": "baseline",
                     "baseline_observed_digest": "baseline", "complete_change_coverage": True,
                     "evidence_paths": ["entry.py"], "changed_paths": ["entry.py"],
                     "added_paths": [], "deleted_paths": []}
            self.assertEqual(axes.checked_code_scope(scope, root, "current"), ["entry.py"])
            for changes in ({"valid": False}, {"errors": ["dependency evidence incomplete"]}):
                with self.assertRaisesRegex(ValueError, "provenance"):
                    axes.checked_code_scope({**scope, **changes}, root, "current")

    def test_code_scope_requires_locked_baseline(self):
        with tempfile.TemporaryDirectory() as tmp:
            with self.assertRaisesRegex(ValueError, "provenance"):
                axes.checked_code_scope({"schema_version": "agentswe-edit-code-scope/v1",
                    "candidate_digest": "current", "complete_change_coverage": True,
                    "baseline_expected_digest": "before", "baseline_observed_digest": "modified"},
                    Path(tmp), "current")

    def test_code_scope_context_discloses_nonbaseline_metadata_exceptions(self):
        scope = {"evaluator_generated_metadata_exclusions": [
            {"path": ".git/HEAD", "sha256": "metadata-sha", "reason": "trusted evaluator git init"}],
            "unchanged_optional_dependency_exclusions": [
                {"path": "optional.py", "sha256": "baseline-sha", "reason": "unrelated unchanged service"}]}
        context = axes.code_scope_context_text(scope)
        for value in ("not new requirements", "Candidate-authored", ".git/HEAD", "metadata-sha",
                      "optional.py", "baseline-sha", "unrelated unchanged service"):
            self.assertIn(value, context)


if __name__ == "__main__":
    unittest.main()
