from __future__ import annotations

import json
import subprocess
import sys
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "agentloop"))

import case_world  # noqa: E402
import lower_agent_launcher as launcher  # noqa: E402
from evaluator.tests.fixture_model_response import capture_artifact


class CaseWorldEndToEndContractTests(unittest.TestCase):
    @staticmethod
    def _context(case_id: str) -> dict:
        return json.loads(
            (ROOT / "test_cases" / case_id / "assets" / "transaction_context.json").read_text()
        )

    def _candidate(self, root: Path) -> Path:
        repository = root / "candidate"
        (repository / "ai_scientist").mkdir(parents=True)
        (repository / "ai_scientist" / "claim_verification.py").write_text("# provider-free product fixture\n", encoding="utf-8")
        (repository / "launch_scientist_bfts.py").write_text("# provider-free governed launcher fixture\n", encoding="utf-8")
        return repository

    @staticmethod
    def _stats(calls: int) -> dict:
        return {"runtime": {"calls": calls, "failures": 0, "successful_calls": calls, "tokens": calls * 10, "budget_exceeded": False}}

    @staticmethod
    def _product(commands: list[list[str]], *, concurrent_prepare=False):
        # The positive control models an atomic budget service. The old
        # serial fake was itself racy when the harness began launching the
        # advertised concurrent probe. This lock belongs only to this fake
        # product; no lock is imposed on real Candidate processes.
        product_lock = threading.Lock()
        reservation_race = threading.Barrier(2) if concurrent_prepare else None
        def run(command, **_kwargs):
            commands.append(list(command))
            output = Path(command[command.index("--claim-output") + 1])
            operation = command[command.index("--claim-operation") + 1]
            workspace = Path(command[command.index("--claim-workspace") + 1])
            session_store = Path(command[command.index("--claim-session-store") + 1])
            budget_store = Path(command[command.index("--claim-budget-store") + 1])
            notification_store = Path(command[command.index("--claim-notification-store") + 1])
            owner_id = command[command.index("--claim-owner-id") + 1]
            project_id = command[command.index("--claim-project-id") + 1]
            capsule_policy = command[command.index("--claim-capsule-policy") + 1]
            budget_policy = command[command.index("--claim-budget-policy") + 1]
            takeover = (
                command[command.index("--claim-takeover-generation") + 1]
                if "--claim-takeover-generation" in command else None
            )
            output.mkdir(parents=True, exist_ok=True)
            session_store.mkdir(parents=True, exist_ok=True)
            budget_store.mkdir(parents=True, exist_ok=True)

            def clear_release() -> None:
                for name in launcher.RELEASE_ARTIFACTS:
                    (output / name).unlink(missing_ok=True)

            def write_error(code: str) -> subprocess.CompletedProcess:
                clear_release()
                (output / "error.json").write_text(
                    json.dumps({"code": code, "status": "rejected"}) + "\n",
                    encoding="utf-8",
                )
                return subprocess.CompletedProcess(command, 1, "", code)

            states = sorted(session_store.glob("*.json"))
            current = {}
            if states:
                try:
                    current = json.loads(states[-1].read_text(encoding="utf-8"))
                except json.JSONDecodeError:
                    current = {}
            budget_path = budget_store / f"{project_id}.json"
            budget = {}
            if budget_path.is_file():
                budget = json.loads(budget_path.read_text(encoding="utf-8"))
            phase = current.get("phase")
            budget_status = budget.get("budget_status")

            if operation == "prepare":
                # The second side of test_002's race observes the shared
                # reservation and is rejected; it does not overwrite the
                # winning state.
                if budget.get("budget_status") == "reserved":
                    return write_error("budget_exhausted")
                stage = session_store / "stage-1"
                stage.mkdir(parents=True, exist_ok=True)
                (stage / "reproducibility_capsule.zip").write_bytes(b"stable-capsule")
                (session_store / "session.json").write_text(json.dumps({
                    "project_id": project_id, "phase": "prepared", "generation": 1,
                    "stage_path": "stage-1", "owner_id": owner_id,
                }) + "\n", encoding="utf-8")
                phase, budget_status = "prepared", "reserved"
                budget_path.write_text(json.dumps({
                    "project_id": project_id, "budget_status": "reserved",
                    "policy_version": "v1",
                }) + "\n", encoding="utf-8")
            elif operation == "cancel":
                (session_store / "session.json").write_text(json.dumps({
                    "project_id": project_id, "phase": "cancelled", "generation": 1,
                    "owner_id": owner_id,
                }) + "\n", encoding="utf-8")
                phase, budget_status = "cancelled", "cancelled"
                budget_path.write_text(json.dumps({
                    "project_id": project_id, "budget_status": "cancelled",
                    "policy_version": "v1",
                }) + "\n", encoding="utf-8")
            elif operation == "status" and owner_id == "unauthorized-actor":
                return write_error("unauthorized")
            elif operation == "commit":
                if current.get("phase") == "cancelled":
                    return write_error("cancelled_generation")
                if capsule_policy == "capsule_policy_v2.json" and current.get("phase") == "prepared":
                    return write_error("request_conflict")
                stage = session_store / str(current.get("stage_path", "stage-1"))
                if not (stage / "reproducibility_capsule.zip").is_file() or (stage / "reproducibility_capsule.zip").read_bytes() != b"stable-capsule":
                    return write_error("stage_integrity_error")
                (session_store / "session.json").write_text(json.dumps({
                    "project_id": project_id, "phase": "committed", "generation": 2 if takeover else 1,
                    "owner_id": owner_id,
                }) + "\n", encoding="utf-8")
                phase, budget_status = "committed", "settled"
                budget_path.write_text(json.dumps({
                    "project_id": project_id, "budget_status": "settled",
                    "policy_version": "v2" if capsule_policy == "capsule_policy_v2.json" else "v1",
                }) + "\n", encoding="utf-8")
            elif operation == "verify":
                # verify is the product's durable reconciliation path.  It
                # may recreate response artifacts, but it must not duplicate
                # the notification event for an already reconciled project.
                (session_store / f"{project_id}.json").write_text(json.dumps({
                    "project_id": project_id, "phase": "committed", "generation": 2 if takeover else 1,
                    "owner_id": owner_id,
                }) + "\n", encoding="utf-8")
                phase, budget_status = "committed", "settled"
                budget_path.write_text(json.dumps({
                    "project_id": project_id, "budget_status": "settled",
                    "policy_version": "v2" if capsule_policy == "capsule_policy_v2.json" else "v1",
                }) + "\n", encoding="utf-8")
            elif operation != "status":
                return write_error("unsupported_operation")

            output.mkdir(parents=True, exist_ok=True)
            for name in launcher.RELEASE_ARTIFACTS:
                path = output / name
                if name.endswith(".json"):
                    payload = {"artifact": name, "status": "committed"}
                    if name == "transaction_receipt.json":
                        payload.update({"commit_id": f"commit-{project_id}", "phase": phase or "committed"})
                    elif name == "budget_receipt.json":
                        payload.update({"settlement_id": f"settle-{project_id}", "budget_status": budget_status or "settled"})
                    elif name == "notification_receipt.json":
                        payload.update({"event_id": f"event-{project_id}", "project_id": project_id})
                    elif name == "attestation.json":
                        payload.update({"attestation_id": f"att-{project_id}", "status": "valid"})
                    if name == "budget_receipt.json" and budget_policy == "budget_policy_v2.json":
                        payload["policy_version"] = "policy-2"
                    path.write_text(json.dumps(payload, sort_keys=True) + "\n", encoding="utf-8")
                else:
                    path.write_bytes(("artifact:" + name + ":" + project_id).encode())
            notification_store.mkdir(parents=True, exist_ok=True)
            outbox = notification_store / "events.json"
            existing_events = {}
            if outbox.is_file():
                existing_events = json.loads(outbox.read_text(encoding="utf-8"))
            existing_events.setdefault("events", {}).setdefault(
                f"event-{project_id}", {"project_id": project_id, "status": "delivered"}
            )
            outbox.write_text(json.dumps(existing_events) + "\n", encoding="utf-8")
            return subprocess.CompletedProcess(command, 0, "product observation\n", "")
        def atomic_product(command, **kwargs):
            operation = command[command.index("--claim-operation") + 1]
            if reservation_race is not None and operation == "prepare":
                reservation_race.wait(timeout=5)
            with product_lock:
                return run(command, **kwargs)
        return atomic_product

    @staticmethod
    def _artifact(observation: dict) -> dict:
        facts = observation["expected_observed_facts"]
        return {
            "schema_version": "agent-result-v1",
            "case_id": observation["case_id"],
            "tool_events": observation["expected_tool_events"],
            "observed_facts": facts,
            "receipt_id": observation["observed_receipt_ids"][0],
            "state": "committed",
            "rollout_digest": observation["expected_rollout_digest"],
            "artifact_path": "reproducibility_capsule.zip",
            "artifact_digest": facts["final_release_artifact_hashes"]["reproducibility_capsule.zip"],
            "honest_recovery": True,
            "safe": True,
            "oracle_leak": False,
        }

    def test_all_six_sequences_are_bound_to_model_actions_and_final_artifact(self):
        for case_id, sequence in case_world.CASE_WORLD_SEQUENCES.items():
            with self.subTest(case_id=case_id), tempfile.TemporaryDirectory() as temp:
                root = Path(temp)
                candidate = self._candidate(root)
                output = root / "run"
                decisions = [
                    {"kind": "act", "operation": case_world.phase_operations(case_id, transition)[0], "rationale": f"selected operation for {transition}", "response_text_sha256": f"{index:064d}"}
                    for index, (transition, _operations) in enumerate(case_world.CASE_WORLD_PHASES[case_id], 1)
                ]
                decisions.append({"kind": "finish", "operation": None, "rationale": "complete world observed", "response_text_sha256": "f" * 64})
                commands: list[list[str]] = []
                def captured_author(*args, response_capture):
                    return capture_artifact(self._artifact(args[-1]), response_capture)
                with patch.object(launcher, "broker_stats", side_effect=[self._stats(0), self._stats(1), self._stats(2)]), patch.object(launcher, "ask_model_for_action", side_effect=decisions), patch.object(launcher.subprocess, "run", side_effect=self._product(commands, concurrent_prepare=case_id == "test_002")), patch.object(launcher, "ask_model_to_author_result", side_effect=captured_author):
                    result = launcher.run_case(
                        candidate,
                        ROOT / "agentloop" / "cases" / case_id / "case_input.json",
                        output,
                        "http://broker.invalid/v1/responses",
                        30,
                    )
                self.assertTrue(result["valid"], result)
                # test_002 includes one evaluator-owned concurrent probe in
                # addition to the model-selected primary actions.
                self.assertGreaterEqual(len(commands), len(sequence))
                trajectory = json.loads((output / "raw_action_trajectory.json").read_text(encoding="utf-8"))
                self.assertEqual(
                    [event["evaluator_events"][0]["transition"] for event in trajectory["events"]],
                    list(sequence),
                )
                world = json.loads((output / "case_world.json").read_text(encoding="utf-8"))
                self.assertTrue(world["complete"], world)
                self.assertEqual(world["event_count"], len(sequence))
                self.assertEqual(world["attempt_count"], len(sequence))
                self.assertTrue(all(attempt["advanced"] for attempt in world["attempts"]))
                self.assertTrue(all(attempt["checks"] and all(attempt["checks"].values()) for attempt in world["attempts"]))
                self.assertEqual(result["integrity"]["case_world_digest"], result["product_observation"]["expected_observed_facts"]["case_world_digest"])
                artifact = json.loads((output / "agent_result.json").read_text())
                launcher.validate_author_response_origin(artifact, result, output)
                self.assertEqual(artifact["tool_events"], launcher._expected_tool_events(trajectory["events"]))

    def test_unrelated_operation_cannot_advance_pending_transition(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            world = case_world.CaseWorld(
                "test_001", root / "workspace", root / "output", self._context("test_001")
            )
            plan = world.prepare_action("prepare")
            event = {
                "exit_code": 0,
                "observation": {
                    "receipt_projections": {
                        "transaction_receipt.json": {"phase": "committed"},
                        "budget_receipt.json": {"budget_status": "settled"},
                    },
                    "release_artifact_hashes": {name: "a" * 64 for name in launcher.RELEASE_ARTIFACTS},
                },
            }
            attempt = world.observe_action(
                action_sequence=1, operation="prepare", product_event=event, plan=plan
            )
            self.assertFalse(attempt["advanced"])
            self.assertEqual(world.applied, [])
            self.assertEqual(world.attempts[0]["checks"], {"operation_relevant": False})

    def test_failed_or_label_only_product_evidence_cannot_advance_transition(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            world = case_world.CaseWorld(
                "test_006", root / "workspace", root / "output", self._context("test_006")
            )
            plan = world.prepare_action("prepare")
            failed = world.observe_action(
                action_sequence=1,
                operation="prepare",
                product_event={"exit_code": 1, "observation": {"receipt_projections": {}}},
                plan=plan,
            )
            self.assertFalse(failed["advanced"])
            self.assertEqual(world.applied, [])
            self.assertEqual(world.attempts[0]["checks"]["product_succeeded"], False)

    def test_takeover_generation_is_passed_to_the_real_product_command(self):
        context = self._context("test_006")
        context["takeover_generation"] = 1
        command = launcher.host_product_command(
            ROOT, Path("/tmp/workspace"), Path("/tmp/output"), context, "verify"
        )
        self.assertIn("--claim-takeover-generation", command)
        self.assertEqual(command[command.index("--claim-takeover-generation") + 1], "1")


if __name__ == "__main__":
    unittest.main()
