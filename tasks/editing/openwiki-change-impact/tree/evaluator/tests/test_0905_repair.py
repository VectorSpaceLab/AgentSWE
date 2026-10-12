from __future__ import annotations

import importlib.util
import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(globals().get("ROOT_OVERRIDE", Path(__file__).resolve().parents[2])).resolve()
REQUIRED = {
    "task", "sibling", "dev_cases", "hidden_cases", "primary_user_goal_by_case",
    "primary_failure_axis_by_case", "secondary_failure_axes_by_case", "dynamic_facts",
    "candidate_visible_inputs", "evaluator_only_oracle", "real_lower_entry",
    "product_api_entry", "broker_model", "broker_effort", "agent_authored_artifact",
    "native_harness_role", "result_judge_entry", "code_judge_entry",
    "formal_one_stop_entry", "known_overlap_or_leakage", "readiness",
}


def load(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    assert spec and spec.loader
    spec.loader.exec_module(module)
    return module


class Repair0905Tests(unittest.TestCase):
    def test_contract_inventory_and_entries(self) -> None:
        contract = json.loads((ROOT / "meta/0905_case_contract.json").read_text(encoding="utf-8"))
        self.assertFalse(REQUIRED - set(contract))
        self.assertEqual(contract["dev_cases"], ["dev_001", "dev_002"])
        self.assertEqual(contract["hidden_cases"], [f"test_{i:03d}" for i in range(1, 7)])
        self.assertEqual(contract["broker_model"], "gpt-5.6-sol")
        self.assertEqual(contract["broker_effort"], "high")
        for key in ("real_lower_entry", "product_api_entry", "formal_one_stop_entry"):
            self.assertTrue((ROOT / contract[key]).exists(), key)
        self.assertTrue(Path(contract["result_judge_entry"]).is_file())
        self.assertTrue(Path(contract["code_judge_entry"]).is_file())

    def test_checked_in_protocol_lock_matches_bounded_lifecycle(self) -> None:
        lock = json.loads((ROOT / "schemas/protocol_lock.json").read_text(encoding="utf-8"))
        self.assertEqual(lock["max_dev_rounds"], 10)
        self.assertEqual(lock["accepted_submission_range"], "1..10")
        self.assertEqual(lock["feedback_revisions"], "fresh_after_each_acceptance")
        self.assertFalse(lock["dev_passed_is_automatic_freeze"])
        self.assertIn("freeze_latest_accepted_on_builder_exit_or_limit", lock["lifecycle"])

    def test_dev_lifecycle_round_accounting(self) -> None:
        lifecycle_module = load("repair_dev_lifecycle", ROOT / "evaluator/dev_lifecycle.py")
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            first = root / "first"; first.mkdir(); (first / "x").write_text("1")
            second = root / "second"; second.mkdir(); (second / "x").write_text("2")
            lifecycle = lifecycle_module.DevLifecycle(root / "run", "builder-session", max_dev_rounds=3, n_concurrent=1)
            dev = {"dev_001": {"score": 80}, "dev_002": {"score": 70}}
            accepted = lifecycle.submit(first, dev)
            self.assertEqual(accepted["accepted_round"], 1)
            self.assertTrue(accepted["dev_passed"])
            self.assertIsNone(lifecycle.freeze_record, "dev mean >60 must not auto-freeze")
            duplicate = lifecycle.submit(first, dev)
            self.assertTrue(duplicate["idempotent"])
            self.assertEqual(len(lifecycle.accepted), 1)
            infra = lifecycle.submit(second, {"dev_001": {"score": None, "infrastructure_invalid": True}, "dev_002": {"score": None}})
            self.assertFalse(infra["consumes_capability_round"])
            self.assertEqual(len(lifecycle.accepted), 1)
            lifecycle.submit(second, dev)
            frozen = lifecycle.freeze("builder_exit")
            self.assertEqual(frozen["source_submission"], 2)
            self.assertEqual(frozen["freeze_reason"], "builder_exit")

    def test_candidate_no_model_call_is_scoreable_but_provider_failure_is_na(self) -> None:
        axes = load("repair_formal_axes", ROOT / "evaluator/formal_axes.py")
        self.assertIsNone(axes.infrastructure_reason({"classification": "candidate_failure", "failure_class": "agent_no_model_call", "broker": {"provider_failures": 0}}))
        self.assertIsNotNone(axes.infrastructure_reason({"classification": "provider_failure", "provider_failures": 1}))
        self.assertIsNone(axes.infrastructure_reason({
            "classification": "candidate_valid",
            "broker_delta": {"successful_calls": 1, "delivery_failures": 1},
        }))

    def test_result_and_code_axes_are_not_short_circuited_together(self) -> None:
        source = (ROOT / "evaluator/formal_axes.py").read_text(encoding="utf-8")
        if "formal_axes_shared.py" in source:source=Path("@@AGENTSWE_EDITING_CONTROL@@/formal_axes_shared.py").read_text()
        self.assertNotIn("if not reasons and freeze_path is not None", source)
        self.assertNotIn("code_ok = result_ok", source)
        self.assertIn("result_reasons", source)
        self.assertIn("code_errors", source)

    def test_wrong_provenance_and_synthesized_artifact_are_flagged(self) -> None:
        axes = load("repair_formal_axes_provenance", ROOT / "evaluator/formal_axes.py")
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            artifact = root / "artifact.json"
            artifact.write_text(json.dumps({"case_id": "test_999", "generation": 1, "artifact_owner": "evaluator", "evaluator_synthesized": True}))
            output = axes.provenance_summary("test_001", {}, artifact, root / "oracle.json")
            value = json.loads(output.read_text())
            self.assertFalse(value["provenance_comparisons"]["case_id"]["match"])
            self.assertTrue(value["artifact_integrity"]["evaluator_synthesized_claimed"])

    def test_judge_contracts_require_exact_usage(self) -> None:
        axes = load("repair_formal_axes_contract", ROOT / "evaluator/formal_axes.py")
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            contract = root / "result.json"
            contract.write_text(json.dumps({
                "case_id": "test_001", "contract_valid": True, "result_score_publishable": True,
                "result_score": 0, "judge": {"model": "gpt-5.6-sol", "reasoning_effort": "max"},
                "provider_usage": {
                    "logical_requests": 1,
                    "completed_responses": 1,
                    "input_tokens": 10,
                    "output_tokens": 10,
                    "total_tokens": 20,
                    "transport_attempts": 1,
                },
            }))
            _value, errors = axes.valid_result_contract(contract, "test_001")
            self.assertEqual(errors, [])
            data = json.loads(contract.read_text()); data["provider_usage"]["logical_requests"] = 0
            contract.write_text(json.dumps(data))
            _value, errors = axes.valid_result_contract(contract, "test_001")
            self.assertTrue(errors)

    def test_common_cli_rejects_invalid_concurrency_without_provider(self) -> None:
        completed = subprocess.run([
            sys.executable, str(ROOT / "harbor/formal_one_stop.py"),
            "--run-dir", "/tmp/unused-agentswe-0905", "--n-concurrent", "2",
        ], text=True, capture_output=True, check=False)
        self.assertNotEqual(completed.returncode, 0)
        self.assertIn("fixed at 1", completed.stderr)

    def test_public_prompts_do_not_embed_credentials_or_score_weights(self) -> None:
        for group in ("dev_cases", "test_cases"):
            for path in (ROOT / group).glob("*/input.md"):
                text = path.read_text(encoding="utf-8", errors="replace")
                self.assertNotIn("GATEWAY_API_KEY=", text)
                self.assertNotIn("OPENAI_API_KEY=", text)
                self.assertNotIn("private oracle", text.lower())
                self.assertNotIn("score weights", text.lower())


if __name__ == "__main__":
    unittest.main()
