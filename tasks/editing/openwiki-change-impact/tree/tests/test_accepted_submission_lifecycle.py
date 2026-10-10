from __future__ import annotations

import tempfile
import unittest
import json
import subprocess
import shutil
import sys
from pathlib import Path
from unittest.mock import patch

from agentloop.evaluator import controller as controller_module
from agentloop.evaluator.broker import EvaluatorBrokerLifecycle
from agentloop.evaluator.controller import Controller
from agentloop.evaluator.hidden_controller import validate_freeze
from agentloop.protocol import DEV_CASES, tree_digest
from harbor import formal_one_stop


class AcceptedSubmissionLifecycleTests(unittest.TestCase):
    def test_controller_duplicate_after_freeze_is_idempotent_but_new_digest_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            candidate = root / "candidate"
            candidate.mkdir()
            (candidate / "solution.patch").write_text("accepted", encoding="utf-8")
            feedback = {"available": True, "feedback_digest": "f" * 64}
            controller = Controller(
                root / "source", Path(__file__).resolve().parents[1], root / "run",
                "http://unused", builder_session_id="builder-test", max_dev_rounds=1,
            )
            controller.records = [{
                "round": 1,
                "builder_session_id": "builder-test",
                "candidate_digest": tree_digest(candidate),
                "feedback": feedback,
            }]
            controller.frozen = {"source_submission": 1}

            duplicate = controller.submit(candidate, 99, evaluate_dev=True)

            self.assertTrue(duplicate["idempotent"])
            self.assertFalse(duplicate["submission_consumed"])
            self.assertFalse(duplicate["consumes_capability_round"])
            self.assertEqual(duplicate["accepted_round"], 1)
            self.assertIs(duplicate["feedback"], feedback)

            new_candidate = root / "new-candidate"
            shutil.copytree(candidate, new_candidate)
            (new_candidate / "solution.patch").write_text("new revision", encoding="utf-8")
            with self.assertRaisesRegex(RuntimeError, "controller is already frozen"):
                controller.submit(new_candidate, 2, evaluate_dev=True)

    def test_duplicate_candidate_is_idempotent_and_reuses_original_feedback(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            candidate = root / "candidate"
            candidate.mkdir()
            (candidate / "solution.patch").write_text("same-candidate", encoding="utf-8")
            (candidate / "edit_report.json").write_text("{}", encoding="utf-8")
            (candidate / "run_report.json").write_text(json.dumps({
                "builder_session_id": "builder-test",
                "submission_number": 1,
            }), encoding="utf-8")
            revision = root / "revision"
            revision.mkdir()
            (revision / "solution.patch").write_text("new-candidate", encoding="utf-8")
            (revision / "edit_report.json").write_text("{}", encoding="utf-8")

            build_calls = 0
            dev_calls = 0

            def fake_build(_source: Path, _candidate: Path, build_dir: Path, run_build: bool = True) -> dict:
                nonlocal build_calls
                build_calls += 1
                repository = build_dir / "repository"
                repository.mkdir(parents=True)
                (repository / "dist").mkdir()
                entry = repository / "dist" / "cli.js"
                entry.write_text("candidate", encoding="utf-8")
                return {
                    "valid": True,
                    "classification": "candidate_ready",
                    "product_entry": str(entry),
                }

            def fake_dev(_repository: Path, _round: int) -> dict:
                nonlocal dev_calls
                dev_calls += 1
                return {
                    case_id: {
                        "run": {
                            "valid": True,
                            "classification": "candidate_product_failure",
                            "infrastructure_invalid": False,
                            "broker_delta": {},
                        },
                        "classification": "candidate_product_failure",
                    }
                    for case_id in DEV_CASES
                }

            controller = Controller(
                root / "source", Path(__file__).resolve().parents[1], root / "run",
                "http://unused", builder_session_id="builder-test", max_dev_rounds=3,
            )
            with patch.object(controller_module, "build_candidate", fake_build), patch.object(
                controller, "_evaluate_dev", fake_dev
            ):
                accepted = controller.submit(candidate, 1, evaluate_dev=True)
                duplicate = controller.submit(candidate, 2, evaluate_dev=True)
                (revision / "run_report.json").write_text(json.dumps({
                    "builder_session_id": "builder-test",
                    "submission_number": 2,
                    "revision_of_candidate_digest": accepted["candidate_digest"],
                    "feedback_digest": accepted["feedback"]["feedback_digest"],
                }), encoding="utf-8")
                revised = controller.submit(revision, 2, evaluate_dev=True)

            self.assertEqual(duplicate["round"], accepted["round"])
            self.assertEqual(duplicate["accepted_round"], accepted["round"])
            self.assertIs(duplicate["feedback"], accepted["feedback"])
            self.assertEqual(duplicate["feedback_digest"], accepted["feedback"]["feedback_digest"])
            self.assertTrue(duplicate["idempotent"])
            self.assertFalse(duplicate["consumed"])
            self.assertFalse(duplicate["submission_consumed"])
            self.assertFalse(duplicate["consumes_capability_round"])
            self.assertEqual(revised["round"], 2)
            self.assertTrue(revised["submission_consumed"])
            self.assertTrue(revised["consumes_capability_round"])
            self.assertEqual(len(controller.records), 2)
            self.assertEqual(build_calls, 2)
            self.assertEqual(dev_calls, 2)

    def test_new_revision_still_requires_distinct_digest_and_exact_feedback_binding(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            first = root / "candidate-1"
            first.mkdir()
            (first / "solution.patch").write_text("first", encoding="utf-8")
            (first / "edit_report.json").write_text("{}", encoding="utf-8")
            (first / "run_report.json").write_text(json.dumps({
                "builder_session_id": "builder-test",
                "submission_number": 1,
            }), encoding="utf-8")
            second = root / "candidate-2"
            second.mkdir()
            (second / "solution.patch").write_text("second", encoding="utf-8")
            (second / "edit_report.json").write_text("{}", encoding="utf-8")

            def fake_build(_source: Path, candidate: Path, build_dir: Path, run_build: bool = True) -> dict:
                repository = build_dir / "repository"
                repository.mkdir(parents=True)
                (repository / "dist").mkdir()
                entry = repository / "dist" / "cli.js"
                entry.write_text(candidate.name, encoding="utf-8")
                return {
                    "valid": True,
                    "classification": "candidate_ready",
                    "product_entry": str(entry),
                }

            def fake_dev(_repository: Path, _round: int) -> dict:
                return {
                    case_id: {
                        "run": {
                            "valid": True,
                            "classification": "candidate_product_failure",
                            "infrastructure_invalid": False,
                            "broker_delta": {},
                        },
                        "classification": "candidate_product_failure",
                    }
                    for case_id in DEV_CASES
                }

            controller = Controller(
                root / "source", Path(__file__).resolve().parents[1], root / "run",
                "http://unused", builder_session_id="builder-test", max_dev_rounds=3,
            )
            with patch.object(controller_module, "build_candidate", fake_build), patch.object(
                controller, "_evaluate_dev", fake_dev
            ):
                accepted = controller.submit(first, 1, evaluate_dev=True)
                (second / "run_report.json").write_text(json.dumps({
                    "builder_session_id": "builder-test",
                    "submission_number": 2,
                    "revision_of_candidate_digest": accepted["candidate_digest"],
                    "feedback_digest": "wrong-feedback",
                }), encoding="utf-8")
                rejected = controller.submit(second, 2, evaluate_dev=True)

            self.assertFalse(rejected["submission_consumed"])
            self.assertTrue(rejected["retry_same_round"])
            self.assertIn("feedback_digest does not match evaluator feedback", rejected["builder_metadata"]["errors"])
            self.assertEqual(len(controller.records), 1)

    def test_live_builder_bridge_accepts_duplicate_without_preflight_and_then_consumes_new_revision(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            workspace = root / "submission"
            workspace.mkdir()
            (workspace / "solution.patch").write_text("candidate-1", encoding="utf-8")
            (workspace / "edit_report.json").write_text("{}", encoding="utf-8")
            credential = root / "credential.env"
            credential.write_text("unused", encoding="utf-8")

            build_calls = 0
            dev_calls = 0

            def fake_build(_source: Path, candidate: Path, build_dir: Path, run_build: bool = True) -> dict:
                nonlocal build_calls
                build_calls += 1
                repository = build_dir / "repository"
                (repository / "dist").mkdir(parents=True)
                entry = repository / "dist" / "cli.js"
                entry.write_text(candidate.name, encoding="utf-8")
                return {"valid": True, "classification": "candidate_ready", "product_entry": str(entry)}

            def fake_dev(_repository: Path, _round: int) -> dict:
                nonlocal dev_calls
                dev_calls += 1
                return {
                    case_id: {
                        "run": {
                            "valid": True,
                            "classification": "candidate_product_failure",
                            "infrastructure_invalid": False,
                            "broker_delta": {},
                        },
                        "classification": "candidate_product_failure",
                    }
                    for case_id in DEV_CASES
                }

            lifecycle = formal_one_stop.BuilderLifecycle(
                root / "run", workspace, "http://unused", credential, "http://unused",
                max_dev_rounds=2,
            )
            (workspace / "run_report.json").write_text(json.dumps({
                "builder_session_id": lifecycle.session_id,
                "submission_number": 1,
            }), encoding="utf-8")

            with patch.object(formal_one_stop, "build_candidate", fake_build), patch.object(
                controller_module, "build_candidate", fake_build
            ), patch.object(lifecycle.controller, "_evaluate_dev", fake_dev):
                accepted_status, accepted_payload = lifecycle.submit()
                duplicate_status, duplicate_payload = lifecycle.submit()

                accepted = accepted_payload["record"]
                (workspace / "solution.patch").write_text("candidate-2", encoding="utf-8")
                (workspace / "run_report.json").write_text(json.dumps({
                    "builder_session_id": lifecycle.session_id,
                    "submission_number": 2,
                    "revision_of_candidate_digest": accepted["candidate_digest"],
                    "feedback_digest": accepted["feedback"]["feedback_digest"],
                }), encoding="utf-8")
                revised_status, revised_payload = lifecycle.submit()
                limit_duplicate_status, limit_duplicate_payload = lifecycle.submit()

            self.assertEqual(accepted_status, 200)
            self.assertEqual(duplicate_status, 200)
            self.assertTrue(duplicate_payload["idempotent"])
            self.assertFalse(duplicate_payload["submission_consumed"])
            self.assertFalse(duplicate_payload["consumes_capability_round"])
            self.assertEqual(revised_status, 200)
            self.assertEqual(revised_payload["round"], 2)
            self.assertTrue(revised_payload["submission_consumed"])
            self.assertTrue(revised_payload["consumes_capability_round"])
            self.assertEqual(limit_duplicate_status, 200)
            self.assertTrue(limit_duplicate_payload["idempotent"])
            self.assertFalse(limit_duplicate_payload["consumes_capability_round"])
            self.assertEqual(len(lifecycle.controller.records), 2)
            self.assertEqual(build_calls, 4)
            self.assertEqual(dev_calls, 2)

    def test_live_builder_bridge_duplicate_after_freeze_succeeds_but_new_digest_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            workspace = root / "submission"
            workspace.mkdir()
            (workspace / "solution.patch").write_text("accepted", encoding="utf-8")
            (workspace / "edit_report.json").write_text("{}", encoding="utf-8")
            credential = root / "credential.env"
            credential.write_text("unused", encoding="utf-8")

            lifecycle = formal_one_stop.BuilderLifecycle(
                root / "run", workspace, "http://unused", credential, "http://unused",
                max_dev_rounds=2,
            )
            (workspace / "run_report.json").write_text(json.dumps({
                "builder_session_id": lifecycle.session_id,
                "submission_number": 1,
            }), encoding="utf-8")
            accepted_digest = tree_digest(workspace)
            lifecycle.controller.records = [{
                "round": 1,
                "builder_session_id": lifecycle.session_id,
                "candidate_digest": accepted_digest,
                "feedback": {"available": True, "feedback_digest": "f" * 64},
            }]
            lifecycle.controller.frozen = {"source_submission": 1}

            duplicate_status, duplicate_payload = lifecycle.submit()

            self.assertEqual(duplicate_status, 200)
            self.assertTrue(duplicate_payload["idempotent"])
            self.assertFalse(duplicate_payload["submission_consumed"])
            self.assertFalse(duplicate_payload["consumes_capability_round"])

            (workspace / "solution.patch").write_text("new revision", encoding="utf-8")
            (workspace / "run_report.json").write_text(json.dumps({
                "builder_session_id": lifecycle.session_id,
                "submission_number": 2,
                "revision_of_candidate_digest": accepted_digest,
                "feedback_digest": "f" * 64,
            }), encoding="utf-8")
            with patch.object(formal_one_stop, "build_candidate", return_value={
                "valid": True,
                "classification": "candidate_ready",
                "product_entry": str(root / "unused" / "dist" / "cli.js"),
            }):
                with self.assertRaisesRegex(RuntimeError, "controller is already frozen"):
                    lifecycle.submit()

    def test_controller_cli_does_not_advance_round_number_for_duplicate_paths(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            candidates = [root / name for name in ("first", "duplicate", "second")]
            for candidate in candidates:
                candidate.mkdir()
            credential = root / "credential.env"
            credential.write_text("unused", encoding="utf-8")
            submitted_rounds: list[int] = []

            class FakeController:
                def __init__(self, *_args, **_kwargs) -> None:
                    self.records: list[dict] = []

                def submit(self, candidate: Path, round_no: int) -> dict:
                    submitted_rounds.append(round_no)
                    if candidate.name == "duplicate":
                        return {"idempotent": True, "submission_consumed": False}
                    record = {"round": round_no, "submission_consumed": True}
                    self.records.append(record)
                    return record

                def freeze(self) -> dict:
                    return {"source_submission": len(self.records)}

                def hidden(self) -> dict:
                    return {"formal_result_eligible": True}

            argv = [
                "controller.py", "--source", str(root / "source"), "--cases", str(root),
                "--run-dir", str(root / "run"), "--builder-session-id", "builder-test",
                "--hidden-credential-file", str(credential),
            ]
            for candidate in candidates:
                argv.extend(("--candidate", str(candidate)))
            with patch.object(controller_module, "Controller", FakeController), patch.object(
                sys, "argv", argv
            ), patch("builtins.print"):
                result = controller_module.main()

            self.assertEqual(result, 0)
            self.assertEqual(submitted_rounds, [1, 2, 2])

    def test_builder_dependency_tree_is_a_separate_read_only_resource(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            public = root / "public"; public.mkdir()
            workspace = root / "workspace"; workspace.mkdir()
            provider = root / "provider.toml"; provider.write_text("test", encoding="utf-8")
            dependencies = root / "dependencies/node_modules"
            (dependencies / "typescript/bin").mkdir(parents=True)
            (dependencies / "typescript/bin/tsc").write_text("", encoding="utf-8")

            class FakeLifecycle:
                pilot_not_formal = True
                session_id = "builder-session-test"
                token = "controller-token-test"
                socket_path = root / "controller.sock"
                max_dev_rounds = 10

            config = formal_one_stop.stage_builder_task(
                root / "run",
                public,
                workspace,
                FakeLifecycle(),
                provider,
                "builder-image-test",
                dependencies,
            )
            self.assertEqual(config, root / "run/builder_job_config.json")
            task = root / "run/builder_task"
            compose = json.loads(
                (task / "environment/docker-compose.yaml").read_text(encoding="utf-8")
            )
            mounts = compose["services"]["main"]["volumes"]
            dependency_mounts = [
                item for item in mounts
                if item.get("target") == "/builder-dependencies/node_modules"
            ]
            self.assertEqual(len(dependency_mounts), 1)
            self.assertTrue(dependency_mounts[0]["read_only"])
            instruction = (task / "instruction.md").read_text(encoding="utf-8")
            self.assertIn("dependency tree", instruction)
            self.assertIn("not task answers", instruction)

    def test_three_accepted_submissions_chain_feedback_and_freeze_latest(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            run_dir = root / "run"
            candidates = []
            for number in range(1, 4):
                candidate = root / f"candidate-{number}"
                candidate.mkdir()
                (candidate / "solution.patch").write_text(
                    f"diff --git a/file b/file\n+index 0000000..000000{number}\n--- a/file\n+++ b/file\n@@\n+revision-{number}\n",
                    encoding="utf-8",
                )
                candidates.append(candidate)

            def fake_build(_source: Path, candidate: Path, build_dir: Path, run_build: bool = True) -> dict:
                repository = build_dir / "repository"
                repository.mkdir(parents=True)
                (repository / "dist").mkdir()
                (repository / "dist" / "cli.js").write_text(
                    candidate.name, encoding="utf-8"
                )
                return {
                    "valid": True,
                    "classification": "candidate_ready",
                    "product_entry": str(repository / "dist" / "cli.js"),
                }

            def fake_dev(_repository: Path, _round: int) -> dict:
                return {
                    case_id: {
                        "run": {
                            "valid": True,
                            "classification": "candidate_product_failure",
                            "infrastructure_invalid": False,
                            "broker_delta": {},
                        },
                        "classification": "candidate_product_failure",
                    }
                    for case_id in DEV_CASES
                }

            controller = Controller(
                root / "source", Path(__file__).resolve().parents[1], run_dir,
                "http://unused", builder_session_id="builder-test",
                max_dev_rounds=10,
            )
            with patch.object(controller_module, "build_candidate", fake_build), patch.object(
                controller, "_evaluate_dev", fake_dev
            ):
                records = []
                for number, candidate in enumerate(candidates, start=1):
                    (candidate / "run_report.json").write_text(
                        __import__("json").dumps({
                            "builder_session_id": "builder-test",
                            "submission_number": number,
                            **({
                                "revision_of_candidate_digest": records[-1]["candidate_digest"],
                                "feedback_digest": records[-1]["feedback"]["feedback_digest"],
                            } if number >= 2 else {}),
                        }),
                        encoding="utf-8",
                    )
                    records.append(controller.submit(candidate, number, evaluate_dev=True))

                self.assertEqual(len(records), 3)
                self.assertTrue(all(record["submission_consumed"] for record in records))
                self.assertEqual(
                    records[2]["revision"]["parent_candidate_digest"],
                    records[1]["candidate_digest"],
                )
                freeze = controller.freeze()

            self.assertEqual(freeze["schema_version"], 2)
            self.assertEqual(freeze["source_submission"], 3)
            self.assertEqual(freeze["accepted_submission_count"], 3)
            self.assertEqual(len(freeze["accepted_candidate_digests"]), 3)
            self.assertTrue(freeze["feedback_chain_complete"])
            validate_freeze(freeze, run_dir, run_dir / "freeze_manifest.json")

    def test_cleanup_attestation_requires_owned_components_to_be_absent(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)

            class FakeLifecycle:
                socket_path = root / "closed-controller.sock"

            class FakeProcess:
                @staticmethod
                def poll() -> int:
                    return 0

            class FakeBroker:
                def __init__(self, role: str) -> None:
                    self.run_dir = root / role
                    self.run_dir.mkdir()
                    self.lifecycle_path = self.run_dir / "broker_lifecycle.json"
                    self.stats_path = self.run_dir / "broker_stats.json"
                    self.process = FakeProcess()
                    self.stats_path.write_text(json.dumps({"calls": 0}), encoding="utf-8")
                    self.lifecycle_path.write_text(json.dumps({
                        "status": "stopped",
                        "final_stats": {"calls": 0},
                        "credential_value_recorded": False,
                    }), encoding="utf-8")

            public = FakeBroker("public")
            hidden = FakeBroker("hidden")
            docker_absent = subprocess.CompletedProcess(
                ["docker", "inspect", "builder"], 1, "", "Error: No such object: builder-id"
            )
            with patch.object(formal_one_stop.subprocess, "run", return_value=docker_absent):
                record = formal_one_stop.write_cleanup_attestation(
                    root,
                    builder_name="openwiki-builder-test",
                    builder_id="builder-id",
                    lifecycle=FakeLifecycle(),
                    brokers=(("public_lower", public), ("pilot_hidden_lower", hidden)),
                    pilot_not_formal=True,
                )
            self.assertTrue(record["completed"])
            self.assertTrue(record["builder_container_absent"])
            self.assertTrue(record["controller_socket_closed"])
            self.assertTrue(all(item["complete"] for item in record["brokers"]))
            self.assertFalse(any(item["credential_value_recorded"] for item in record["brokers"]))
            self.assertTrue(all(item["stats_present"] for item in record["brokers"]))

    def test_result_judge_broker_is_an_explicit_evaluator_owned_xhigh_stage(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            broker = EvaluatorBrokerLifecycle(
                root / "result_judge_broker", root / "credential.env", "https://upstream.invalid",
                model="gpt-5.6-sol", reasoning_effort="max",
                container_prefix="openwiki-result-judge-broker",
            )
            self.assertEqual(broker.model, "gpt-5.6-sol")
            self.assertEqual(broker.reasoning_effort, "max")
            self.assertEqual(broker.container_prefix, "openwiki-result-judge-broker")
            broker.run_dir.mkdir(parents=True)
            broker._record(status="stopped", final_stats={"model": broker.model, "reasoning_effort": broker.reasoning_effort})
            lifecycle = json.loads(broker.lifecycle_path.read_text(encoding="utf-8"))
            self.assertEqual(lifecycle["model"], "gpt-5.6-sol")
            self.assertEqual(lifecycle["reasoning_effort"], "max")


if __name__ == "__main__":
    unittest.main()
