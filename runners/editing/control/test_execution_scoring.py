import copy
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from execution_contract import classify_candidate_execution, candidate_zero_result_contract, file_digest
from execution_scoring import judge_execution_case


class ExecutionContracts(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.evidence = self.root / "execution.json"
        self.evidence.write_text('{"compiler_exit":1}')
        self.record = {
            "case_id": "test_001", "candidate_digest": "a" * 64,
            "classification": "candidate_build_failure", "execution_attempted": True,
            "environment_preflight": {"valid": True},
            "failure_attribution": {"party": "candidate", "observed_by": "evaluator", "fatal": True,
                                    "reason": "Rust type error after healthy baseline compile",
                                    "evidence_paths": [str(self.evidence)]},
        }

    def classify(self, record=None):
        return classify_candidate_execution(record or self.record, case_id="test_001", candidate_digest="a" * 64)

    def test_proven_build_failure_is_zero_without_judge(self):
        verdict = self.classify()
        self.assertEqual(verdict["classification"], "candidate_zero")
        value = candidate_zero_result_contract(verdict, case_id="test_001", candidate_digest="a" * 64)
        self.assertIsNone(value["provider_usage"])
        self.assertIsNone(value["rubric_dimensions"])
        self.assertFalse(value["judge_invoked"])

    def test_infra_wins_over_candidate_failure(self):
        self.record["infrastructure_invalid"] = True
        self.assertEqual(self.classify()["classification"], "infrastructure_invalid")

    def test_unknown_missing_model_is_not_zero(self):
        self.record.pop("failure_attribution")
        self.assertEqual(self.classify()["classification"], "unresolved")

    def test_health_evidence_required(self):
        self.record.pop("environment_preflight")
        self.assertFalse(self.classify()["contract_valid"])

    def test_candidate_self_attribution_rejected(self):
        self.record["failure_attribution"]["observed_by"] = "candidate"
        self.assertFalse(self.classify()["round_consumed"])

    def test_wrong_case_and_digest_rejected(self):
        for field in ("case_id", "candidate_digest"):
            record = copy.deepcopy(self.record)
            record[field] = "foreign"
            self.assertFalse(self.classify(record)["contract_valid"])

    def test_missing_or_changed_failure_evidence_rejected(self):
        verdict = self.classify()
        self.evidence.write_text("changed")
        with self.assertRaises(ValueError):
            candidate_zero_result_contract(verdict, case_id="test_001", candidate_digest="a" * 64)
        self.evidence.unlink()
        self.assertFalse(self.classify()["contract_valid"])

    def test_recovered_retries_are_not_infra(self):
        self.record["broker_delta"] = {"upstream_failures": 2}
        self.record["infrastructure_events"] = [{"terminal": False, "recovered": True}]
        self.assertEqual(self.classify()["classification"], "candidate_zero")

    def arguments(self, zero=False):
        paths = {}
        for key in ("case_input", "rubric", "artifact", "raw_trajectory", "native_evidence", "private_oracle"):
            paths[key] = self.root / (key + ".json")
            paths[key].write_text("{}")
        record = copy.deepcopy(self.record)
        if not zero:
            record.update(classification="candidate_execution_failure", real_execution=True,
                          broker_delta={"calls": 1, "successful_calls": 1},
                          artifact_validation={"validated_by": "evaluator", "valid": True,
                                               "sha256": file_digest(paths["artifact"])})
            record.pop("failure_attribution")
        return {**paths, "execution_record": record, "candidate_digest": "a" * 64,
                "case_id": "test_001", "output": self.root / "scoring", "broker_endpoint": "http://127.0.0.1:1/v1/responses"}

    def test_zero_path_never_calls_provider_and_is_idempotent(self):
        args = self.arguments(True)
        with patch("execution_scoring.subprocess.run") as run:
            first = judge_execution_case(**args)
            second = judge_execution_case(**args)
        run.assert_not_called()
        self.assertEqual(first["score"], 0)
        self.assertTrue(second["cached"])

    def test_nonzero_exit_with_valid_artifact_is_semantically_judged_once(self):
        args = self.arguments()
        def judge(command, **kwargs):
            contract = {"case_id": "test_001", "contract_valid": True, "result_score_publishable": True,
                        "judge": {"model": "deepseek-flash", "reasoning_effort": "max"},
                        "provider_usage": {"logical_requests": 1, "completed_responses": 1,
                                           "transport_attempts": 1, "input_tokens": 10,
                                           "output_tokens": 5, "total_tokens": 15}, "result_score": 17,
                        "assessment": "The product failed to preserve the requested tenant boundary.",
                        "major_errors": ["Foreign tenant state changed."],
                        "dimensions": {"safety": {"score": 0, "max": 20}}}
            (args["output"] / "result_score_contract.json").write_text(json.dumps(contract))
            return type("Completed", (), {"returncode": 0})()
        with patch("execution_scoring.subprocess.run", side_effect=judge) as run:
            first = judge_execution_case(**args)
            second = judge_execution_case(**args)
        self.assertEqual(run.call_count, 1)
        self.assertEqual(first["score"], 17)
        self.assertTrue(second["cached"])
        self.assertEqual(first["assessment"], "The product failed to preserve the requested tenant boundary.")
        self.assertEqual(second["major_errors"], ["Foreign tenant state changed."])
        self.assertEqual(second["dimensions"], {"safety": {"score": 0, "max": 20}})

    def test_artifact_change_after_provenance_does_not_call_judge(self):
        args = self.arguments()
        args["artifact"].write_text("changed")
        with patch("execution_scoring.subprocess.run") as run:
            value = judge_execution_case(**args)
        run.assert_not_called()
        self.assertFalse(value["contract_valid"])

    def test_uncertain_attempt_is_not_resampled(self):
        args = self.arguments()
        with patch("execution_scoring.subprocess.run", return_value=type("Completed", (), {"returncode": 1})()) as run:
            first = judge_execution_case(**args)
            second = judge_execution_case(**args)
        self.assertEqual(run.call_count, 1)
        self.assertFalse(first["round_consumed"])
        self.assertFalse(second["round_consumed"])

    def test_optional_cap_is_forwarded_and_part_of_immutable_identity(self):
        args = self.arguments()
        caps = self.root / 'score_caps.json'
        caps.write_text('{"cap_probe":"first"}')
        args['score_cap_contract'] = caps
        with patch('execution_scoring.subprocess.run',
                   return_value=type('Completed', (), {'returncode': 1})()) as run:
            judge_execution_case(**args)
            command = run.call_args.args[0]
            self.assertEqual(command[command.index('--score-cap-contract') + 1], str(caps.resolve()))
            intent = json.loads((args['output'] / 'scoring_intent.json').read_text())
            self.assertEqual(intent['identity']['inputs']['score_cap_contract']['sha256'], file_digest(caps))
            caps.write_text('{"cap_probe":"changed"}')
            second = judge_execution_case(**args)
        self.assertEqual(run.call_count, 1)
        self.assertIn('different immutable inputs', second['reason'])

    def test_missing_explicit_cap_contract_cannot_silently_disable_it(self):
        args = self.arguments()
        args['score_cap_contract'] = self.root / 'not_present.json'
        with patch('execution_scoring.subprocess.run') as run:
            value = judge_execution_case(**args)
        run.assert_not_called()
        self.assertFalse(value['contract_valid'])
        self.assertIn('score_cap_contract', value['reason'])


if __name__ == "__main__":
    unittest.main()
