from __future__ import annotations

import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "evaluator"))
from semantic_finalize import artifact_for, is_infrastructure, validate_result_contract
from broker.candidate_broker import normalize_responses_input


class OfflineContractTests(unittest.TestCase):
    def test_lower_broker_normalizes_text_to_structured_nonstreaming_responses_input(self) -> None:
        normalized = normalize_responses_input("choose the next product action")
        self.assertEqual(normalized, [{
            "type": "message",
            "role": "user",
            "content": [{"type": "input_text", "text": "choose the next product action"}],
        }])
        source = (ROOT / "evaluator/broker/candidate_broker.py").read_text(encoding="utf-8")
        self.assertIn('payload["input"] = normalize_responses_input(payload.get("input"))', source)
        self.assertIn('payload["stream"] = False', source)
        self.assertIn('payload.pop("metadata", None)', source)

    def test_result_judge_starts_only_after_hidden_execution(self):
        source = (ROOT / "harbor" / "formal_one_stop.py").read_text(encoding="utf-8")
        self.assertLess(
            source.index("hidden = lifecycle.controller.run_all_hidden()"),
            source.index('stats_name="judge.json"'),
        )
        self.assertNotIn('pilot_not_formal=True,\n            "result_judge_broker"', source)

    def test_formal_hidden_phase_has_fresh_zero_call_lower_broker(self):
        source = (ROOT / "harbor" / "formal_one_stop.py").read_text(encoding="utf-8")
        self.assertIn('stats_name="hidden.json"', source)
        self.assertIn('hidden_lower_broker_requires_zero_call_start', source)
        self.assertIn('formal hidden lower broker must start with zero calls and zero failures', source)
        self.assertLess(source.index('freeze_latest("builder_exit")'), source.index('stats_name="hidden.json"'))
        self.assertIn('lifecycle.controller.broker_endpoint = hidden_lower_endpoint', source)
        self.assertIn('lifecycle.controller.stats_file = run_dir / "brokers/hidden.json"', source)

    def test_lower_driver_is_model_selected_and_trajectory_bound(self):
        source = (ROOT / "lower_agent" / "openhands_lower_agent.py").read_text(encoding="utf-8")
        for marker in (
            "MAX_ACTIONS = 12", "allowedActions", "ConversationService",
            "createRecoveryEventDispatcher", "createRecoverySyncCoordinator",
            "createWorkspaceRecoveryReconciler", "production_",
            "trajectory.json", "validateArtifact", "trajectory_digest",
            "model artifact observations do not bind to product trajectory",
        ):
            self.assertIn(marker, source)
        self.assertNotIn('const actions = Array.isArray(plan.actions)', source)

    def test_candidate_no_model_call_is_scoreable_but_provider_failure_is_na(self) -> None:
        self.assertFalse(is_infrastructure({"classification": "candidate_no_model_call", "classification_axis": "candidate"}))
        self.assertTrue(is_infrastructure({"classification": "provider_infrastructure_error", "classification_axis": "infrastructure"}))

    def test_wrong_or_synthesized_artifact_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            run = Path(td); base = run / "lifecycle/hidden/test_001"; base.mkdir(parents=True)
            artifact = base / "agent_result.json"
            artifact.write_text(json.dumps({"case_id": "test_999", "source": "evaluator"}))
            with self.assertRaises(ValueError):
                artifact_for("openhands", run, "test_001", base / "case_result.json", {})

    def test_result_contract_requires_exact_single_logical_request(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "contract.json"
            path.write_text(json.dumps({"contract_valid": True, "result_score_publishable": True, "case_id": "test_001", "result_score": 50, "judge": {"model": "gpt-5.6-sol", "reasoning_effort": "max"}, "provider_usage": {"logical_requests": 2, "completed_responses": 1}}))
            self.assertIsNotNone(validate_result_contract(path, "test_001")[1])

    def test_common_cli_rejects_parallel_formalism_without_provider_call(self) -> None:
        completed = subprocess.run([sys.executable, str(ROOT / "harbor/formal_one_stop.py"), "--n-concurrent", "2"], text=True, capture_output=True)
        self.assertEqual(completed.returncode, 2)

    def test_checked_in_lifecycle_metadata_matches_up_to_ten_controller(self) -> None:
        lock = json.loads((ROOT / "protocol_lock.json").read_text(encoding="utf-8"))
        schema_lock = json.loads((ROOT / "evaluator/schemas/protocol_lock.json").read_text(encoding="utf-8"))
        self.assertEqual(lock["lifecycle"]["max_dev_rounds"], 10)
        self.assertEqual(lock["lifecycle"]["candidate_rounds"], "up_to_10")
        self.assertEqual(schema_lock["candidate"]["max_dev_rounds"], 10)
        self.assertEqual(schema_lock["candidate"]["rounds"], "up_to_10")
        source = (ROOT / "harbor/formal_one_stop.py").read_text(encoding="utf-8")
        self.assertIn("up-to-ten-round feedback lifecycle", source)

    def test_builder_instruction_requires_early_coherent_public_submission(self) -> None:
        source = (ROOT / "harbor/formal_one_stop.py").read_text(encoding="utf-8")
        self.assertIn("smallest coherent product slice", source)
        self.assertIn("submit immediately once", source)
        self.assertIn("Do not exhaustively implement hidden recovery surfaces", source)

    def test_builder_compose_uses_non_overlapping_run_local_subnet(self) -> None:
        sys.path.insert(0, str(ROOT / "harbor"))
        try:
            from formal_one_stop import _select_compose_subnet
        finally:
            sys.path.remove(str(ROOT / "harbor"))

        selected = _select_compose_subnet(
            run_dir=Path("@@AGENTSWE_EDITING_RUNS@@/smoke/openhands/network-test"),
            existing_subnets=["172.18.0.0/16", "198.18.7.0/24"],
            host_subnets=["10.0.0.0/8", "192.168.1.0/24"],
        )
        chosen = __import__("ipaddress").ip_network(selected)
        self.assertEqual(chosen.prefixlen, 24)
        self.assertFalse(chosen.overlaps(__import__("ipaddress").ip_network("10.0.0.0/8")))
        self.assertFalse(chosen.overlaps(__import__("ipaddress").ip_network("172.18.0.0/16")))
        self.assertFalse(chosen.overlaps(__import__("ipaddress").ip_network("198.18.7.0/24")))


if __name__ == "__main__": unittest.main()
