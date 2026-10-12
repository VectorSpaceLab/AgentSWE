from __future__ import annotations

import importlib.util
import json
import os
import tempfile
import unittest
from pathlib import Path


REPAIR_ROOT = Path(__file__).resolve().parent


def load_module(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"cannot load {path}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


formal_axes = load_module("formal_axes_shared_contract_test", REPAIR_ROOT / "formal_axes_shared.py")
builder_broker = load_module("builder_broker_xhigh_contract_test", REPAIR_ROOT / "builder_broker_xhigh.py")


class SharedFormalContractTests(unittest.TestCase):
    def test_candidate_paths_do_not_fall_back_to_trajectory_or_native_result(self):
        record = {
            "trajectory": "/run/hidden/test_001/trajectory.json",
            "launcher_result": "/run/hidden/test_001/launcher_result.json",
            "result": "/run/hidden/test_001/result.json",
        }
        self.assertEqual(formal_axes.candidate_paths(record), [])

    def test_zero_call_lower_execution_is_not_scoreable(self):
        record = {
            "real_execution": True,
            "broker": {"calls": 0, "successful_calls": 0},
            "artifact_provenance": {"evaluator_synthesized": False},
        }
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            artifact = root / "agent_result.json"
            trajectory = root / "trajectory.json"
            artifact.write_text(json.dumps({"case_id": "test_001"}) + "\n", encoding="utf-8")
            trajectory.write_text("{}\n", encoding="utf-8")
            errors = formal_axes.validate_model_artifact_provenance(
                record, artifact, trajectory, "test_001"
            )
        self.assertIn("lower broker has no successful model call", errors)

    def test_hidden_attestation_requires_canonical_inventory_and_freeze_proof(self):
        hidden = {
            "expected_cases": ["test_001"],
            "executed_cases": ["test_001"],
            "complete_inventory": True,
            "all_cases_materialized": True,
            "all_cases_real": True,
            "all_cases_started_after_freeze": True,
            "frozen_digest_stable": True,
        }
        freeze = {"hidden_allowed": True, "candidate_digest": "a" * 64}
        with tempfile.TemporaryDirectory() as directory:
            errors = formal_axes.hidden_lifecycle_errors(hidden, freeze, Path(directory))
        self.assertTrue(any("exactly test_001..test_006" in error for error in errors))

    def test_model_artifact_provenance_requires_private_oracle(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            formal_axes.ROOT = root
            artifact = root / "agent_result.json"
            artifact.write_text(json.dumps({"case_id": "test_001"}) + "\n", encoding="utf-8")
            with self.assertRaises(ValueError):
                formal_axes.provenance_summary(
                    "test_001", {}, artifact, root / "oracle_comparison.json"
                )


class CodeJudgeUsageCaptureTests(unittest.TestCase):
    def test_usage_capture_extracts_responses_usage_and_records_positive_values(self):
        with tempfile.TemporaryDirectory() as directory:
            capture_path = Path(directory) / "capture.json"
            previous = os.environ.get("CODE_JUDGE_USAGE_CAPTURE")
            os.environ["CODE_JUDGE_USAGE_CAPTURE"] = str(capture_path)
            try:
                module = load_module(
                    "code_judge_sitecustomize_contract_test",
                    REPAIR_ROOT / "code_judge_sitecustomize.py",
                )
                module._record(
                    {
                        "type": "response.completed",
                        "response": {
                            "usage": {
                                "input_tokens": 11,
                                "output_tokens": 7,
                                "total_tokens": 18,
                            }
                        },
                    }
                )
                value = json.loads(capture_path.read_text(encoding="utf-8"))
            finally:
                if previous is None:
                    os.environ.pop("CODE_JUDGE_USAGE_CAPTURE", None)
                else:
                    os.environ["CODE_JUDGE_USAGE_CAPTURE"] = previous
        self.assertEqual(value["input_tokens"], 11)
        self.assertEqual(value["output_tokens"], 7)
        self.assertEqual(value["total_tokens"], 18)
        self.assertEqual(value["observations"], 1)

    def test_code_runner_mounts_evaluator_only_capture_and_merges_it(self):
        source = (REPAIR_ROOT / "code_judge_runner.py").read_text(encoding="utf-8")
        for marker in (
            "code_judge_sitecustomize.py",
            "PYTHONPATH=/opt/agentswe_code_judge",
            "CODE_JUDGE_USAGE_CAPTURE=/output/provider_usage_capture.json",
            'contract["provider_usage_capture"]',
        ):
            self.assertIn(marker, source)


class BuilderBrokerEvidenceTests(unittest.TestCase):
    def test_stats_file_is_durable_across_state_transitions(self):
        with tempfile.TemporaryDirectory() as directory:
            stats_path = Path(directory) / "builder_stats.json"
            state = builder_broker.State(stats_path)
            initial = json.loads(stats_path.read_text(encoding="utf-8"))
            self.assertEqual(initial["runtime"]["calls"], 0)
            self.assertEqual(initial["runtime"]["in_flight_calls"], 0)

            state.reserve()
            reserved = json.loads(stats_path.read_text(encoding="utf-8"))
            self.assertEqual(reserved["runtime"]["calls"], 1)
            self.assertEqual(reserved["runtime"]["in_flight_calls"], 1)

            state.finish(
                failure=None,
                status=200,
                usage={"input_tokens": 11, "output_tokens": 7, "total_tokens": 18},
            )
            completed = json.loads(stats_path.read_text(encoding="utf-8"))
            self.assertEqual(completed["runtime"]["completed_calls"], 1)
            self.assertEqual(completed["runtime"]["successful_calls"], 1)
            self.assertEqual(completed["runtime"]["total_tokens"], 18)
            self.assertEqual(completed["upstream"]["status_counts"], {"200": 1})


if __name__ == "__main__":
    unittest.main()
