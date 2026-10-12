#!/usr/bin/env python3
"""Provider-free regressions for artifact-less Candidate failure acceptance."""
from __future__ import annotations

import importlib.util
import sys
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "harbor"))


def load_module(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


one_stop = load_module("acceptance_repair_one_stop", ROOT / "harbor/formal_one_stop.py")
controller_module = load_module(
    "acceptance_repair_controller", ROOT / "controller/two_round_controller.py"
)


def result(*, classification: str, artifact_present: bool, successful: int = 1,
           product_entry: bool = True) -> dict[str, object]:
    return {
        "classification": classification,
        "artifact_present": artifact_present,
        "product_entry_observed": product_entry,
        "broker_calls_delta": 1,
        "broker_successful_calls_delta": successful,
    }


def model_artifact_result(*, classification: str = "valid") -> dict[str, object]:
    value = result(classification=classification, artifact_present=True)
    value.update({
        "artifact": {"schema_version": "dyad-lower-agent-artifact-v3"},
        "artifact_provenance": {
            "artifact_owner": "model_via_dyad_typed_chat",
            "producer_entry": "model finish action captured from Dyad persisted typed chat",
            "evaluator_synthesized": False,
            "exists": True,
        },
    })
    return value


class AcceptanceClassificationRepairTests(unittest.TestCase):
    def test_public_gate_requires_semantic_or_evidenced_zero_feedback(self):
        missing = result(classification="candidate_failure", artifact_present=False)
        self.assertFalse(one_stop.Lifecycle._public_valid(missing))
        missing['semantic_feedback'] = {'classification': 'candidate_zero', 'score': 0,
            'contract_valid': True, 'round_consumed': True}
        self.assertTrue(one_stop.Lifecycle._public_valid(missing))

    def test_valid_requires_model_finish_artifact_provenance(self) -> None:
        valid = model_artifact_result()
        self.assertFalse(one_stop.Lifecycle._public_valid(valid))  # Raw execution still needs the independent score.
        self.assertTrue(one_stop.hidden_result_real(valid))
        self.assertTrue(controller_module.TwoRoundController._real_lower_result(valid))

        for mutation in (
            {"artifact_present": False},
            {"artifact": {"schema_version": "evaluator-synthesized"}},
            {"artifact_provenance": {"artifact_owner": "evaluator"}},
        ):
            invalid = model_artifact_result()
            invalid.update(mutation)
            with self.subTest(mutation=mutation):
                self.assertFalse(one_stop.Lifecycle._public_valid(invalid))
                self.assertFalse(one_stop.hidden_result_real(invalid))
                self.assertFalse(controller_module.TwoRoundController._real_lower_result(invalid))

    def test_formal_public_and_hidden_gates_do_not_loosen_live_evidence(self) -> None:
        for gate in (
            one_stop.Lifecycle._public_valid,
            one_stop.hidden_result_real,
        ):
            with self.subTest(gate=gate.__qualname__):
                self.assertFalse(gate(result(
                    classification="candidate_failure", artifact_present=False,
                    product_entry=False,
                )))
                self.assertFalse(gate(result(
                    classification="candidate_failure", artifact_present=False,
                    successful=0,
                )))
                for failure_class in (
                    "provider_error",
                    "broker_error",
                    "candidate_mount_or_launcher_failure",
                    "headless_product_entry_failed",
                    "transport_error",
                ):
                    self.assertFalse(gate({
                        "classification": "infrastructure-invalid",
                        "failure_class": failure_class,
                        "artifact_present": False,
                        "product_entry_observed": True,
                        "broker_calls_delta": 1,
                        "broker_successful_calls_delta": 1,
                    }))

    def test_compatibility_controller_accepts_missing_or_invalid_candidate_artifact(self) -> None:
        missing = result(classification="candidate_failure", artifact_present=False)
        invalid = result(classification="candidate_failure", artifact_present=False)
        invalid["artifact"] = {"real_product": True, "success": False}
        self.assertTrue(controller_module.TwoRoundController._real_lower_result(missing))
        self.assertTrue(controller_module.TwoRoundController._real_lower_result(invalid))
        self.assertFalse(controller_module.TwoRoundController._real_lower_result(
            result(classification="valid", artifact_present=False)
        ))

        invalid_valid = model_artifact_result()
        invalid_valid["artifact"] = {"schema_version": "dyad-lower-agent-artifact-v3"}
        invalid_valid["artifact_provenance"] = {
            "artifact_owner": "evaluator",
            "evaluator_synthesized": True,
        }
        self.assertFalse(controller_module.TwoRoundController._real_lower_result(invalid_valid))

    def test_compatibility_hidden_gate_requires_artifact_only_for_valid_cases(self) -> None:
        cases = {
            f"test_{index:03d}": result(
                classification="candidate_failure", artifact_present=False
            )
            for index in range(1, 7)
        }
        accepted = {
            "classification": "candidate_failure",
            "static_stage_a": False,
            "cases": cases,
        }
        for item in cases.values():
            item["product_entry_observed"] = True
        self.assertTrue(controller_module.TwoRoundController._real_hidden_result(accepted))

        cases["test_001"] = result(classification="valid", artifact_present=False)
        self.assertFalse(controller_module.TwoRoundController._real_hidden_result(accepted))

        cases["test_001"] = model_artifact_result()
        self.assertTrue(controller_module.TwoRoundController._real_hidden_result(accepted))

        cases["test_001"]["artifact_provenance"] = {"artifact_owner": "evaluator"}
        self.assertFalse(controller_module.TwoRoundController._real_hidden_result(accepted))

        self.assertTrue(controller_module.TwoRoundController._real_hidden_result(
            result(classification="candidate_failure", artifact_present=False)
        ))
        self.assertFalse(controller_module.TwoRoundController._real_hidden_result({
            "classification": "candidate_failure",
            "artifact_present": False,
            "product_entry_observed": False,
            "broker_calls_delta": 1,
            "broker_successful_calls_delta": 1,
        }))
        self.assertFalse(controller_module.TwoRoundController._real_hidden_result(
            result(classification="valid", artifact_present=False)
        ))

    def test_infrastructure_failure_is_not_consumed_by_compatibility_controller(self) -> None:
        failure_classes = (
            "provider_error",
            "broker_error",
            "candidate_mount_or_launcher_failure",
            "headless_product_entry_failed",
            "transport_error",
        )
        for failure_class in failure_classes:
            with self.subTest(failure_class=failure_class), tempfile.TemporaryDirectory() as raw:
                root = Path(raw)
                candidate = root / "candidate"
                candidate.mkdir()

                def evaluate(_number: int, _path: Path) -> dict[str, object]:
                    return {
                        f"dev_{index:03d}": {
                            "classification": "infrastructure-invalid",
                            "failure_class": failure_class,
                        }
                        for index in (1, 2)
                    }

                controller = controller_module.TwoRoundController(root / "run", evaluate)
                record = controller.submit(candidate)
                self.assertEqual(record.state, "infrastructure-invalid")
                self.assertEqual(controller.records, [])

    def test_compatibility_controller_can_commit_artifactless_candidate_feedback(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            candidate = root / "candidate"
            candidate.mkdir()

            def evaluate(_number: int, _path: Path) -> dict[str, object]:
                return {
                    f"dev_{index:03d}": result(
                        classification="candidate_failure", artifact_present=False
                    )
                    for index in (1, 2)
                }

            controller = controller_module.TwoRoundController(root / "run", evaluate)
            record = controller.submit(candidate)
            self.assertEqual(record.state, "completed")
            self.assertTrue(record.feedback)
            self.assertTrue(Path(record.feedback).is_file())


if __name__ == "__main__":
    unittest.main()
