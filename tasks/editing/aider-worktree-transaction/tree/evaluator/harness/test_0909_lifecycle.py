"""Executable lifecycle regressions; provider and Docker calls are forbidden."""
import importlib.util
import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parents[2]
spec = importlib.util.spec_from_file_location("aider_controller_0909", ROOT / "harbor/agentloop_controller.py")
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)


class AiderLifecycleTests(unittest.TestCase):
    def _execute(self, initial_infra, final_score):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        root = Path(temporary.name)
        candidate = root / "submission"
        candidate.mkdir()
        (candidate / "solution.patch").write_text("nonempty")
        controller = module.Controller(root / "run", root / "source", "unused", True,
                         public_cases=("dev_001",), hidden_cases=("test_001",), pilot_not_formal=True)
        lower_count = 0
        def simulated_command(command, **_):
            nonlocal lower_count
            if "materialize_candidate.py" in command[1]:
                output = Path(command[command.index("--output") + 1])
                output.mkdir()
                (output / "aider").mkdir()
                (output / "aider/product.py").write_text((candidate / "solution.patch").read_text())
                (output / "build_result.json").write_text(json.dumps({"classification": "ready_for_lower"}))
            else:
                self.assertIn("run_lower_agent_case.py", command[1])
                lower_count += 1
                output = Path(command[command.index("--output-dir") + 1])
                output.mkdir(parents=True)
                infra = initial_infra and lower_count == 1
                (output / "result.json").write_text(json.dumps({"case_id": "dev_001",
                    "classification": "provider_infrastructure_failure" if infra else "candidate_behavior_failure",
                    "score": None if infra else final_score, "broker": {"calls_delta": 0, "failures_delta": 0}}))
            return subprocess.CompletedProcess(command, 0, "{}", "")
        with patch.object(module.subprocess, "run", side_effect=simulated_command), patch.object(controller, "_score_public") as scorer:
            first = controller.submit(candidate)
            if initial_infra:
                self.assertFalse(first["accepted"])
                self.assertFalse(first["round_consumed"])
                self.assertEqual(controller.records, [])
                self.assertTrue((Path(first["candidate_path"]) / "solution.patch").is_file())
                repeated = controller.submit(candidate)
                self.assertTrue(repeated["duplicate"])
                self.assertFalse(repeated["accepted"])
                self.assertEqual(lower_count, 1)
                (candidate / "solution.patch").write_text("genuine product revision")
                accepted = controller.submit(candidate)
            else:
                accepted = first
            self.assertTrue(accepted["accepted"])
            self.assertEqual(scorer.call_args.args[2], module.digest(Path(accepted["materialized_path"])))
            self.assertEqual(len(controller.records), 1)
            self.assertEqual(controller.feedback["dev"]["dev_001"]["score"], final_score)
            self.assertEqual(controller.feedback["dev_score"], final_score)
            self.assertIsNone(controller.frozen)
            duplicate = controller.submit(candidate)
            self.assertTrue(duplicate["duplicate"])
            self.assertFalse(duplicate["round_consumed"])

    def test_infra_preserved_same_candidate_cached_new_product_can_execute(self):
        self._execute(initial_infra=True, final_score=37)

    def test_healthy_candidate_zero_is_valid_feedback_not_infra(self):
        self._execute(initial_infra=False, final_score=0)

    def test_feedback_pass_does_not_auto_freeze(self):
        self._execute(initial_infra=False, final_score=88)

    def test_missing_score_is_not_accepted_measurement(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            result = root / "result.json"
            result.write_text(json.dumps({"classification": "candidate_valid", "broker": {"calls_delta": 1, "failures_delta": 0}}))
            controller = module.Controller(root / "run", root / "source", "unused", True,
                      public_cases=("dev_001",), hidden_cases=("test_001",), pilot_not_formal=True)
            invalid, reasons = controller._dev_has_infrastructure_failure({"build_exit_code": 0,
                                 "dev": {"dev_001": {"result": str(result)}}})
            self.assertTrue(invalid)
            self.assertIn("dev_001:missing_authoritative_score", reasons)

    def test_candidate_artifact_failure_is_zero_without_result_judge(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            run = root / "run"
            run.mkdir()
            evidence = root / "artifact-evidence.json"
            evidence.write_text('{"authorship":"invalid"}', encoding="utf-8")
            result = root / "result.json"
            candidate_digest = "a" * 64
            result.write_text(json.dumps({
                "case_id": "dev_001", "candidate_digest": candidate_digest,
                "classification": "candidate_artifact_failure",
                "execution_attempted": True,
                "environment_preflight": {"valid": True},
                "failure_attribution": {
                    "party": "candidate", "observed_by": "evaluator", "fatal": True,
                    "reason": "artifact contract failed", "evidence_paths": [str(evidence)],
                },
            }), encoding="utf-8")
            controller = module.Controller(
                run, root / "source", "unused", True,
                public_cases=("dev_001",), hidden_cases=("test_001",), pilot_not_formal=True,
            )
            controller._score_public("dev_001", result, candidate_digest)
            scored = json.loads(result.read_text(encoding="utf-8"))
            self.assertEqual(scored["score"], 0)
            self.assertEqual(scored["dev_score_kind"], "candidate_zero")
            self.assertFalse(scored["result_judge_contract"]["judge_invoked"])

    def test_unresolved_execution_verdict_is_typed_and_unconsumed(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            result = root / "result.json"
            candidate_digest = "b" * 64
            result.write_text(json.dumps({
                "case_id": "dev_001", "candidate_digest": candidate_digest,
                "classification": "candidate_valid", "broker": {"calls_delta": 1},
            }), encoding="utf-8")
            controller = module.Controller(
                root / "run", root / "source", "unused", True,
                public_cases=("dev_001",), hidden_cases=("test_001",), pilot_not_formal=True,
            )
            with self.assertRaises(module.PublicScoringBoundaryError) as raised:
                controller._score_public("dev_001", result, candidate_digest)
            self.assertEqual(raised.exception.classification, "unresolved_evaluator_boundary")

    def test_acceptance_stage_is_immutable_and_does_not_fabricate_builder(self):
        sys.path.insert(0, str(ROOT))
        from harbor import acceptance_reuse
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = root / "source"
            (source / "aider").mkdir(parents=True)
            (source / "aider/product.py").write_text("original\n")
            (source / "aider/alias.py").symlink_to("product.py")
            args = SimpleNamespace(run_dir=root / "run", candidate_source=source,
                                   cases=["test_001"], stage_only=True)
            with patch.object(acceptance_reuse, "start_broker") as broker:
                self.assertEqual(acceptance_reuse.run(args), 0)
                broker.assert_not_called()
            frozen = root / "run/lifecycle/frozen_candidate"
            self.assertEqual(module.digest(frozen), module.digest(source))
            (source / "aider/product.py").write_text("later change\n")
            self.assertEqual((frozen / "aider/product.py").read_text(), "original\n")
            self.assertTrue((frozen / "aider/alias.py").is_symlink())
            source_record = json.loads((root / "run/acceptance_source.json").read_text())
            self.assertFalse(source_record["builder_lifecycle_replayed"])
            self.assertFalse((root / "run/builder_session_attestation.json").exists())

    def test_acceptance_rejects_escaping_candidate_symlink(self):
        sys.path.insert(0, str(ROOT))
        from harbor import acceptance_reuse
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "source/aider").mkdir(parents=True)
            (root / "external").write_text("not candidate data")
            (root / "source/aider/escape").symlink_to(root / "external")
            args = SimpleNamespace(run_dir=root / "run", candidate_source=root / "source",
                                   cases=["test_001"], stage_only=True)
            with self.assertRaisesRegex(ValueError, "escaping symlinks"):
                acceptance_reuse.run(args)


if __name__ == "__main__":
    unittest.main()
