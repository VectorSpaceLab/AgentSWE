from __future__ import annotations

import hashlib
import inspect
import json
import subprocess
import sys
import tempfile
import threading
import unittest
import urllib.error
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "agentloop"))

import lower_agent_launcher as launcher  # noqa: E402
from evaluator.tests.fixture_model_response import capture_artifact


class LowerActionLoopTests(unittest.TestCase):
    case_file = ROOT / "agentloop" / "cases" / "test_001" / "case_input.json"

    def test_evaluator_secret_asset_is_not_copied_to_candidate_workspace(self):
        assets = ROOT / "test_cases" / "test_005" / "assets"
        launcher = __import__("lower_agent_launcher")
        with tempfile.TemporaryDirectory() as temp:
            workspace = Path(temp) / "workspace"
            workspace.mkdir()
            excluded = launcher.copy_candidate_visible_assets(assets, workspace)
            self.assertIn("operator_secret.txt", excluded)
            self.assertFalse((workspace / "operator_secret.txt").exists())

    def _candidate(self, root: Path) -> Path:
        repository = root / "candidate"
        (repository / "ai_scientist").mkdir(parents=True)
        (repository / "ai_scientist" / "claim_verification.py").write_text("# mocked product\n", encoding="utf-8")
        (repository / "launch_scientist_bfts.py").write_text("# mocked governed launcher\n", encoding="utf-8")
        return repository

    def _broker_stats(self, calls: int, failures: int = 0) -> dict:
        return {
            "runtime": {
                "calls": calls,
                "failures": failures,
                "successful_calls": calls - failures,
                "tokens": calls * 10,
                "budget_exceeded": False,
            }
        }

    def _fake_product(self, commands: list[list[str]], *, concurrent_prepare=False):
        # Only this synthetic product is serialized. The launcher still
        # starts two independent attempts; a barrier proves their observed
        # intervals overlap without relying on scheduling or a sleep.
        product_lock = threading.Lock()
        reservation_race = threading.Barrier(2) if concurrent_prepare else None
        def run(command, **_kwargs):
            commands.append(list(command))
            output = Path(command[command.index("--claim-output") + 1])
            operation = command[command.index("--claim-operation") + 1]
            session_store = Path(command[command.index("--claim-session-store") + 1])
            budget_store = Path(command[command.index("--claim-budget-store") + 1])
            project_id = command[command.index("--claim-project-id") + 1]
            owner_id = command[command.index("--claim-owner-id") + 1]
            takeover = "--claim-takeover-generation" in command
            output.mkdir(parents=True, exist_ok=True)
            session_store.mkdir(parents=True, exist_ok=True)
            budget_store.mkdir(parents=True, exist_ok=True)
            state_path = session_store / "session.json"
            budget_path = budget_store / f"{project_id}.json"
            state = json.loads(state_path.read_text()) if state_path.is_file() else {}
            budget = json.loads(budget_path.read_text()) if budget_path.is_file() else {}
            phase = state.get("phase")
            budget_status = budget.get("budget_status")

            def reject(code: str) -> subprocess.CompletedProcess:
                for name in launcher.RELEASE_ARTIFACTS:
                    (output / name).unlink(missing_ok=True)
                (output / "error.json").write_text(json.dumps({"code": code}) + "\n", encoding="utf-8")
                return subprocess.CompletedProcess(command, 1, "", code)

            if operation == "prepare":
                if budget_status == "reserved":
                    return reject("budget_exhausted")
                stage = session_store / "stage-1"
                stage.mkdir(parents=True, exist_ok=True)
                (stage / "reproducibility_capsule.zip").write_bytes(b"stable-capsule")
                phase, budget_status = "prepared", "reserved"
                state_path.write_text(json.dumps({"phase": phase, "stage_path": "stage-1", "project_id": project_id, "owner_id": owner_id}) + "\n")
                budget_path.write_text(json.dumps({"budget_status": budget_status, "project_id": project_id}) + "\n")
            elif operation == "cancel":
                phase, budget_status = "cancelled", "cancelled"
                state_path.write_text(json.dumps({"phase": phase, "project_id": project_id, "owner_id": owner_id}) + "\n")
                budget_path.write_text(json.dumps({"budget_status": budget_status, "project_id": project_id}) + "\n")
            elif operation == "commit":
                if phase == "cancelled":
                    return reject("cancelled_generation")
                stage = session_store / str(state.get("stage_path", "stage-1"))
                if not (stage / "reproducibility_capsule.zip").is_file():
                    return reject("stage_integrity_error")
                phase, budget_status = "committed", "settled"
                state_path.write_text(json.dumps({"phase": phase, "project_id": project_id, "owner_id": owner_id, "generation": 2 if takeover else 1}) + "\n")
                budget_path.write_text(json.dumps({"budget_status": budget_status, "project_id": project_id}) + "\n")
            elif operation == "verify":
                phase, budget_status = "committed", "settled"
                state_path.write_text(json.dumps({"phase": phase, "project_id": project_id, "owner_id": owner_id, "generation": 2 if takeover else 1}) + "\n")
                budget_path.write_text(json.dumps({"budget_status": budget_status, "project_id": project_id}) + "\n")
            elif operation != "status":
                return reject("unsupported_operation")

            for name in launcher.RELEASE_ARTIFACTS:
                path = output / name
                if name.endswith(".json"):
                    payload = {"artifact": name, "status": phase or "committed"}
                    if name == "transaction_receipt.json":
                        payload.update({"commit_id": "commit-1", "phase": phase or "committed"})
                    elif name == "budget_receipt.json":
                        payload.update({"settlement_id": "settle-1", "budget_status": budget_status or "settled"})
                    elif name == "attestation.json":
                        payload.update({"attestation_id": "att-1", "status": "valid"})
                    elif name == "notification_receipt.json":
                        payload.update({"event_id": "event-1", "status": "delivered"})
                    path.write_text(json.dumps(payload, sort_keys=True) + "\n", encoding="utf-8")
                else:
                    path.write_bytes(("artifact:" + name).encode("utf-8"))
            return subprocess.CompletedProcess(command, 0, "product observation\n", "")

        def atomic_product(command, **kwargs):
            operation = command[command.index("--claim-operation") + 1]
            if reservation_race is not None and operation == "prepare":
                reservation_race.wait(timeout=5)
            with product_lock:
                return run(command, **kwargs)
        return atomic_product

    def _author_artifact(self, observation: dict) -> dict:
        facts = observation["expected_observed_facts"]
        return {
            "schema_version": "agent-result-v1",
            "case_id": observation["case_id"],
            "tool_events": observation["expected_tool_events"],
            "observed_facts": facts,
            "receipt_id": "commit-1",
            "state": "committed",
            "rollout_digest": observation["expected_rollout_digest"],
            "artifact_path": "reproducibility_capsule.zip",
            "artifact_digest": facts["final_release_artifact_hashes"]["reproducibility_capsule.zip"],
            "honest_recovery": True,
            "safe": True,
            "oracle_leak": False,
        }

    def _run(self, root: Path, decisions, *, author=None, stats=None, product=True, case_file=None):
        candidate = self._candidate(root)
        output = root / "run"
        output.mkdir()
        commands: list[list[str]] = []
        stats = stats or [self._broker_stats(0), self._broker_stats(1), self._broker_stats(2)]
        def captured_author(*args, response_capture):
            value = author(args[-1]) if author is not None else self._author_artifact(args[-1])
            return capture_artifact(value, response_capture)
        with patch.object(launcher, "broker_stats", side_effect=stats), patch.object(
            launcher, "ask_model_for_action", side_effect=decisions
        ), patch.object(launcher, "subprocess") as subprocess_module:
            if product:
                subprocess_module.run.side_effect = self._fake_product(
                    commands, concurrent_prepare=(case_file or self.case_file).parent.name == "test_002"
                )
            else:
                subprocess_module.run.side_effect = AssertionError("product must not be dispatched")
            with patch.object(
                launcher,
                "ask_model_to_author_result",
                side_effect=captured_author,
            ) as author_call:
                result = launcher.run_case(
                    candidate,
                    case_file or self.case_file,
                    output,
                    "http://broker.invalid/v1/responses",
                    30,
                )
        return result, commands, output, author_call

    def test_model_selected_prepare_is_dispatched_not_fixed_verify(self):
        decisions = [
            {"kind": "act", "operation": "prepare", "rationale": "inspect and durably stage the release", "response_text_sha256": "1" * 64},
            {"kind": "act", "operation": "commit", "rationale": "reconcile the durable state", "response_text_sha256": "2" * 64},
            {"kind": "act", "operation": "verify", "rationale": "complete the evaluator-owned recovery sequence", "response_text_sha256": "3" * 64},
            {"kind": "finish", "operation": None, "rationale": "the complete observed release state is sufficient", "response_text_sha256": "4" * 64},
        ]
        with tempfile.TemporaryDirectory() as temp:
            result, commands, output, _ = self._run(
                Path(temp), decisions,
                case_file=ROOT / "agentloop" / "cases" / "test_002" / "case_input.json",
            )
            self.assertTrue(result["valid"], result)
            operations = [command[command.index("--claim-operation") + 1] for command in commands]
            # The evaluator-owned race probe contributes a second product
            # invocation, while the trajectory still contains only the
            # three model-selected primary operations.
            self.assertEqual(operations, ["prepare", "prepare", "commit", "verify"])
            self.assertNotEqual(operations, ["verify"])
            trajectory = json.loads((output / "raw_action_trajectory.json").read_text(encoding="utf-8"))
            self.assertEqual(trajectory["event_count"], 3)
            self.assertEqual(trajectory["events"][0]["operation"], "prepare")
            self.assertEqual(result["action_loop"]["operations"], ["prepare", "commit", "verify"])
            self.assertEqual(
                [event["evaluator_events"][0]["transition"] for event in trajectory["events"]],
                ["concurrent_reservation_race", "winner_commit_response_lost", "winner_reconciled"],
            )
            world = json.loads((output / "case_world.json").read_text())
            self.assertTrue(world["complete"], world)
            self.assertTrue(all(attempt["advanced"] for attempt in world["attempts"]))
            race = world["attempts"][0]
            self.assertTrue(race["checks"]["concurrent_process_intervals_overlap"])
            self.assertTrue(race["checks"]["exactly_one_winner"])
            self.assertTrue(race["checks"]["exactly_one_budget_rejection"])

    def test_finish_without_action_is_candidate_failure_and_no_authoring(self):
        decisions = [{"kind": "finish", "operation": None, "rationale": "nothing can be verified", "response_text_sha256": "1" * 64}]
        with tempfile.TemporaryDirectory() as temp:
            result, commands, output, author_call = self._run(Path(temp), decisions, product=False)
            self.assertFalse(result["valid"])
            self.assertEqual(result["classification"], "candidate_behavior_failure")
            self.assertEqual(result["classification_axis"], "candidate")
            self.assertEqual(commands, [])
            self.assertFalse(author_call.called)
            trajectory = json.loads((output / "raw_action_trajectory.json").read_text(encoding="utf-8"))
            self.assertEqual(trajectory["event_count"], 0)
            self.assertIn("before selecting any product action", trajectory["action_error"])

    def test_mismatched_redundant_tool_claim_is_retained_for_semantic_scoring(self):
        decisions = [
            {"kind": "act", "operation": "verify", "rationale": "run the governed verification", "response_text_sha256": "1" * 64},
            {"kind": "act", "operation": "verify", "rationale": "reconcile the durable state", "response_text_sha256": "2" * 64},
            {"kind": "finish", "operation": None, "rationale": "report the observed result", "response_text_sha256": "4" * 64},
        ]

        def mismatched_author(observation):
            artifact = self._author_artifact(observation)
            artifact["tool_events"] = []
            return artifact

        with tempfile.TemporaryDirectory() as temp:
            result, _, output, author_call = self._run(Path(temp), decisions, author=mismatched_author)
            self.assertTrue(result["valid"], result)
            self.assertEqual(result["classification"], "candidate_valid")
            self.assertEqual(result["classification_axis"], "candidate")
            self.assertTrue(author_call.called)
            artifact = json.loads((output / "agent_result.json").read_text())
            self.assertEqual(artifact["tool_events"], [])  # never silently repair the claim
            self.assertFalse((output / "agent_result.rejected.json").exists())
            claims = result["author_claim_mismatches"]
            self.assertTrue(any(row["field"] == "tool_events" and row["origin_failure"] is False for row in claims))
            self.assertEqual(result["action_loop"]["operations"], ["verify", "verify"])
            launcher.validate_author_response_origin(artifact, result, output)
            self.assertIsNone(result["authoring_error"])

    def test_foreign_primary_origin_is_still_rejected_and_response_preserved(self):
        decisions = [
            {"kind":"act", "operation":"prepare", "rationale":"inspect", "response_text_sha256":"1"*64},
            {"kind":"finish", "operation":None, "rationale":"report partial", "response_text_sha256":"2"*64},
        ]
        def foreign(observation):
            value = self._author_artifact(observation)
            value["case_id"] = "foreign-case"
            return value
        with tempfile.TemporaryDirectory() as temp:
            result, _, output, _ = self._run(Path(temp), decisions, author=foreign)
            self.assertFalse(result["valid"])
            self.assertEqual(result["classification"], "candidate_behavior_failure")
            self.assertIn("case_id mismatch", result["authoring_error"])
            self.assertFalse((output / "agent_result.json").exists())
            self.assertTrue((output / "agent_result.rejected.json").exists())
            captured = json.loads((output / "model_final_response.txt").read_text())
            self.assertEqual(captured["case_id"], "foreign-case")

    def test_provider_failure_is_infrastructure_not_candidate_failure(self):
        decisions = [urllib.error.URLError("provider unavailable")]
        with tempfile.TemporaryDirectory() as temp:
            result, commands, output, author_call = self._run(
                Path(temp),
                decisions,
                stats=[self._broker_stats(0), self._broker_stats(1, 1), self._broker_stats(1, 1)],
                product=False,
            )
            self.assertFalse(result["valid"])
            self.assertEqual(result["classification"], "provider_infrastructure_error")
            self.assertEqual(result["classification_axis"], "infrastructure")
            self.assertEqual(commands, [])
            self.assertFalse(author_call.called)
            self.assertTrue((output / "raw_action_trajectory.json").exists())

    def test_unsupported_operation_is_rejected_before_dispatch(self):
        with patch.object(
            launcher,
            "_request_model_json",
            return_value=(
                {"kind": "act", "operation": "delete", "rationale": "unsafe",},
                hashlib.sha256(b"unsupported").hexdigest(),
            ),
        ):
            with self.assertRaises(launcher.ModelContentError):
                launcher.ask_model_for_action(
                    "http://broker.invalid/v1/responses", "task", {"case_id": "test_001"}, [], 1
                )

    def test_finish_before_world_completion_preserves_partial_delivery_and_failure_evidence(self):
        decisions = [
            {"kind": "act", "operation": "prepare", "rationale": "begin the governed action sequence", "response_text_sha256": "1" * 64},
            {"kind": "finish", "operation": None, "rationale": "stop early", "response_text_sha256": "2" * 64},
        ]
        with tempfile.TemporaryDirectory() as temp:
            result, _, output, author_call = self._run(Path(temp), decisions)
            self.assertTrue(result["valid"], result)  # execution validity, not task success
            self.assertEqual(result["classification"], "candidate_valid")
            self.assertTrue(author_call.called)
            trajectory = json.loads((output / "raw_action_trajectory.json").read_text(encoding="utf-8"))
            self.assertEqual(trajectory["event_count"], 1)
            self.assertEqual(trajectory["events"][0]["operation"], "prepare")
            world = json.loads((output / "case_world.json").read_text())
            self.assertFalse(world["complete"])
            self.assertEqual(world["event_count"], 0)
            self.assertFalse(world["attempts"][0]["advanced"])
            self.assertFalse(world["attempts"][0]["checks"]["operation_relevant"])
            artifact = json.loads((output / "agent_result.json").read_text())
            launcher.validate_author_response_origin(artifact, result, output)
            self.assertEqual(artifact["observed_facts"]["case_world_digest"], world["world_digest"])
            self.assertNotIn("result_score", result)

    def test_authoring_prompt_names_case_world_digest_binding(self):
        source = inspect.getsource(launcher.ask_model_to_author_result)
        self.assertIn("case_world_digest", source)
        self.assertIn("complete adversarial", source)
        self.assertIn("transition sequence", source)


if __name__ == "__main__":
    unittest.main()
