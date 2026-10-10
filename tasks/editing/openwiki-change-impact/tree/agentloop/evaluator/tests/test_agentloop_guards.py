from __future__ import annotations

import json
import subprocess
import tempfile
import threading
import unittest
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from unittest.mock import patch

from agentloop.evaluator.broker import BrokerState, EvaluatorBrokerLifecycle, resolve_credential
from agentloop.evaluator.controller import Controller
from agentloop.evaluator.dynamic_case_service import issue
from agentloop.evaluator.hidden_controller import run_hidden_suite, validate_freeze
from agentloop.evaluator.lower_agent_launcher import (
    _broker_classification,
    _candidate_environment,
    _valid_agent_result,
    read_broker_stats,
    runtime_preflight,
    run_case,
)
from agentloop.evaluator.smoke_observer import State, _join_endpoint
from agentloop.protocol import HIDDEN_CASES, file_sha256, make_tree_read_only, tree_digest, write_json


ROOT = Path(__file__).resolve().parents[3]


def freeze_value(run_dir: Path, repository: Path) -> dict[str, object]:
    digest = tree_digest(repository)
    return {
        "schema_version": 1,
        "source_submission": 2,
        "source_submission_id": "candidate-002-test",
        "candidate_1_digest": "1" * 64,
        "candidate_2_digest": "2" * 64,
        "candidate_digest": digest,
        "repository_digest": digest,
        "repository_digest_algorithm": "sha256-tree-v1",
        "candidate_path": str(repository),
        "builder_session_id": "builder-test-session",
        "feedback_digest": "3" * 64,
        "feedback_consumed": True,
        "frozen_at": "2026-09-01T00:00:00+00:00",
        "hidden_started_at": None,
        "hidden_allowed": True,
        "dev_evaluated": True,
        "immutable_repository": True,
        "credential_mounted_to_candidate": False,
        "hidden_case_inventory": list(HIDDEN_CASES),
    }


def write_freeze(run_dir: Path, repository: Path) -> Path:
    make_tree_read_only(repository)
    path = run_dir / "freeze_manifest.json"
    write_json(path, freeze_value(run_dir, repository))
    seal = run_dir / "freeze_manifest.sha256"
    seal.write_text(file_sha256(path) + "\n", encoding="ascii")
    path.chmod(0o444)
    seal.chmod(0o444)
    return path


class FakeBroker:
    def __init__(self, run_dir: Path, *_args, **_kwargs) -> None:
        self.run_dir = run_dir
        self.endpoint = "http://127.0.0.1:1/v1/responses"
        self.instance_id = "broker-test-instance"
        self.lifecycle_path = run_dir / "broker_lifecycle.json"
        self.stats_path = run_dir / "broker_stats.json"

    def __enter__(self):
        self.run_dir.mkdir(parents=True)
        write_json(self.lifecycle_path, {"status": "ready", "broker_instance_id": self.instance_id})
        write_json(self.stats_path, broker_stats())
        return self

    def __exit__(self, *_args):
        write_json(self.lifecycle_path, {"status": "stopped", "broker_instance_id": self.instance_id})


def broker_stats(**updates: int) -> dict[str, object]:
    value: dict[str, object] = {
        "schema_version": 2,
        "broker_instance_id": "broker-test-instance",
        "model": "gpt-5.6-sol",
        "reasoning_effort": "high",
        "calls": 0,
        "successful_calls": 0,
        "failures": 0,
        "broker_failures": 0,
        "provider_failures": 0,
        "credential_failures": 0,
        "prompt_tokens": 0,
        "completion_tokens": 0,
        "total_tokens": 0,
        "forced_overrides": 0,
    }
    value.update(updates)
    return value


class GuardTests(unittest.TestCase):
    def test_broker_state_supports_result_judge_model_without_network(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            credential = root / "credential.env"
            credential.write_text("OPENAI_API_KEY=test-token\n", encoding="utf-8")
            state = BrokerState(
                credential, root / "stats.json", "result-judge-test",
                model="gpt-5.6-sol", reasoning_effort="max",
            )
            self.assertEqual(state.stats["model"], "gpt-5.6-sol")
            self.assertEqual(state.stats["reasoning_effort"], "max")

    def test_docker_broker_close_removes_and_verifies_owned_container(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)

            class FinishedProcess:
                returncode = 0

                @staticmethod
                def poll() -> int:
                    return 0

            lifecycle = EvaluatorBrokerLifecycle(
                root / "broker", root / "unreadable.env", "http://127.0.0.1:9"
            )
            lifecycle.run_dir.mkdir()
            lifecycle.transport = "docker_read_only_secret_mount"
            lifecycle.container_name = "openwiki-broker-owned-test"
            lifecycle.container_id = "owned-container-id"
            lifecycle.process = FinishedProcess()
            calls: list[list[str]] = []

            def fake_run(argv, **_kwargs):
                calls.append(list(argv))
                return subprocess.CompletedProcess(
                    argv, 1 if argv[:2] == ["docker", "inspect"] else 0,
                    stdout="",
                    stderr="Error: No such object: owned-container-id" if argv[:2] == ["docker", "inspect"] else "",
                )

            with patch("agentloop.evaluator.broker.subprocess.run", side_effect=fake_run):
                lifecycle.close()

            self.assertIn(
                ["docker", "rm", "-f", "owned-container-id"], calls
            )
            self.assertIn(
                ["docker", "inspect", "owned-container-id"], calls
            )
            record = json.loads(lifecycle.lifecycle_path.read_text(encoding="utf-8"))
            self.assertTrue(record["container_absent"])
            self.assertEqual(record["container_name"], "openwiki-broker-owned-test")

    def test_dynamic_request_withholds_oracle_and_evaluator_action_plan(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            issued = issue(
                "test_001",
                {
                    "scenario": "private-scenario",
                    "change_kind": "git",
                    "required_actions": ["private-action"],
                    "expected_pages": ["private.md"],
                    "request": {"tenant": "private-tenant"},
                    "task": "runtime task",
                },
                root / "evaluator",
                root / "candidate",
                "public task text",
            )
            visible = json.loads(Path(issued["candidate_request"]).read_text())
            self.assertEqual(visible["task"], "public task text")
            self.assertEqual(set(visible), {"case_id", "runtime_nonce", "oracle", "task"})
            oracle = json.loads(Path(issued["oracle_path"]).read_text())
            self.assertIn("private-action", json.dumps(oracle))
            self.assertNotIn("private-action", json.dumps(visible))

    def test_missing_product_result_is_not_accepted(self) -> None:
        self.assertFalse(_valid_agent_result(None, "test_001"))
        self.assertFalse(_valid_agent_result({"schema_version": "openwiki-agent-result/v1"}, "test_001"))

    def test_freeze_requires_digest_seal_read_only_and_run_local_path(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            run_dir = Path(temp) / "run"
            repository = run_dir / "frozen_candidate" / "repository"
            repository.mkdir(parents=True)
            (repository / "dist.js").write_text("frozen", encoding="utf-8")
            freeze_path = write_freeze(run_dir, repository)
            value = json.loads(freeze_path.read_text())
            self.assertEqual(validate_freeze(value, run_dir, freeze_path), repository.resolve())
            freeze_path.chmod(0o644)
            value["repository_digest"] = "4" * 64
            write_json(freeze_path, value)
            with self.assertRaisesRegex(ValueError, "digest changed|seal mismatch"):
                validate_freeze(value, run_dir, freeze_path)

    def test_controller_freezes_only_candidate2_as_a_sealed_read_only_repository(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            run_dir = root / "run"
            repository = run_dir / "candidate_2" / "repository"
            (repository / "dist").mkdir(parents=True)
            (repository / "dist" / "cli.js").write_text("compiled", encoding="utf-8")
            controller = Controller(root / "source", ROOT, run_dir, "http://127.0.0.1:1/v1/responses", "builder-test")
            dev_cases = {
                case_id: {"run": {"valid": False, "classification": "candidate_contract_failure", "infrastructure_invalid": False}}
                for case_id in ("dev_001", "dev_002")
            }
            controller.records = [
                {
                    "candidate_digest": "1" * 64,
                    "build": {"valid": True},
                    "dev_cases": dev_cases,
                    "feedback": {"available": True, "infrastructure_invalid": False},
                    "revision": {},
                    "builder_metadata": {"valid": True},
                },
                {
                    "candidate_digest": "2" * 64,
                    "build": {"valid": True, "product_entry": str(repository / "dist" / "cli.js")},
                    "dev_cases": dev_cases,
                    "feedback": {"available": True, "infrastructure_invalid": False},
                    "revision": {
                        "same_builder_session": True,
                        "feedback_digest": "3" * 64,
                        "feedback_bound_submission": True,
                    },
                    "builder_metadata": {"valid": True},
                },
            ]
            freeze = controller.freeze()
            frozen_repository = Path(freeze["candidate_path"])
            self.assertEqual(freeze["source_submission"], 2)
            self.assertEqual(freeze["repository_digest"], tree_digest(frozen_repository))
            self.assertEqual(freeze["candidate_digest"], freeze["repository_digest"])
            self.assertFalse(frozen_repository.stat().st_mode & 0o222)
            self.assertFalse((frozen_repository / "dist" / "cli.js").stat().st_mode & 0o222)
            self.assertEqual(
                (run_dir / "freeze_manifest.sha256").read_text().strip(),
                file_sha256(run_dir / "freeze_manifest.json"),
            )
            with self.assertRaisesRegex(RuntimeError, "already been frozen"):
                controller.freeze()

    def test_controller_rejects_infrastructure_invalid_candidate1_feedback(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            run_dir = root / "run"
            repository = run_dir / "candidate_2" / "repository"
            (repository / "dist").mkdir(parents=True)
            (repository / "dist" / "cli.js").write_text("compiled", encoding="utf-8")
            controller = Controller(root / "source", ROOT, run_dir, "http://127.0.0.1:1/v1/responses", "builder-test")
            dev_cases = {case_id: {"run": {"infrastructure_invalid": False}} for case_id in ("dev_001", "dev_002")}
            controller.records = [
                {
                    "candidate_digest": "1" * 64,
                    "build": {"valid": True},
                    "dev_cases": dev_cases,
                    "feedback": {"available": True, "infrastructure_invalid": True},
                    "revision": {},
                    "builder_metadata": {"valid": True},
                },
                {
                    "candidate_digest": "2" * 64,
                    "build": {"valid": True, "product_entry": str(repository / "dist" / "cli.js")},
                    "dev_cases": dev_cases,
                    "feedback": {"available": True, "infrastructure_invalid": False},
                    "revision": {"same_builder_session": True, "feedback_digest": "3" * 64, "feedback_bound_submission": True},
                    "builder_metadata": {"valid": True},
                },
            ]
            with self.assertRaisesRegex(RuntimeError, "Candidate 1 dev provider/broker evidence is invalid"):
                controller.freeze()

    def test_hidden_suite_stops_on_pre_case_tamper_and_attests_partial_inventory(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            run_dir = root / "run"
            repository = run_dir / "frozen_candidate" / "repository"
            repository.mkdir(parents=True)
            (repository / "dist.js").write_text("frozen", encoding="utf-8")
            freeze_path = write_freeze(run_dir, repository)

            def tampering_run(*_args, **_kwargs):
                (repository / "dist.js").chmod(0o644)
                (repository / "dist.js").write_text("tampered", encoding="utf-8")
                return {
                    "valid": False,
                    "classification": "candidate_contract_failure",
                    "candidate_classification": "candidate_contract_failure",
                    "infrastructure_invalid": False,
                }

            with (
                patch("agentloop.evaluator.hidden_controller.EvaluatorBrokerLifecycle", FakeBroker),
                patch("agentloop.evaluator.hidden_controller.read_broker_stats", return_value=broker_stats()),
                patch("agentloop.evaluator.hidden_controller.run_case", side_effect=tampering_run),
            ):
                result = run_hidden_suite(
                    freeze_path,
                    run_dir / "hidden-result.json",
                    root / "credential.env",
                    ROOT / "test_cases",
                )
            self.assertFalse(result["complete_inventory"])
            self.assertEqual(result["cases"]["test_001"]["classification"], "mount_isolation_failure")
            attestation = json.loads((run_dir / "hidden-after-freeze-attestation.json").read_text())
            self.assertTrue(attestation["stopped_on_tamper"])
            self.assertEqual(attestation["executed_case_count"], 1)

    def test_candidate_failures_are_measured_and_each_case_has_evidence(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            run_dir = root / "run"
            repository = run_dir / "frozen_candidate" / "repository"
            repository.mkdir(parents=True)
            (repository / "dist.js").write_text("frozen", encoding="utf-8")
            freeze_path = write_freeze(run_dir, repository)
            run_value = {
                "valid": False,
                "classification": "candidate_contract_failure",
                "candidate_classification": "candidate_contract_failure",
                "infrastructure_invalid": False,
                "broker_delta": {"calls": 1, "successful_calls": 1, "failures": 0},
            }
            with (
                patch("agentloop.evaluator.hidden_controller.EvaluatorBrokerLifecycle", FakeBroker),
                patch("agentloop.evaluator.hidden_controller.read_broker_stats", return_value=broker_stats()),
                patch("agentloop.evaluator.hidden_controller.run_case", return_value=run_value),
            ):
                result = run_hidden_suite(
                    freeze_path,
                    run_dir / "hidden-result.json",
                    root / "credential.env",
                    ROOT / "test_cases",
                )
            self.assertEqual(list(result["cases"]), list(HIDDEN_CASES))
            self.assertTrue(result["formal_result_eligible"])
            for case_id in HIDDEN_CASES:
                self.assertEqual(
                    result["cases"][case_id]["repository_digest_before"],
                    result["cases"][case_id]["repository_digest_after"],
                )
                evidence = Path(result["cases"][case_id]["evidence_paths"]["evidence_manifest"])
                self.assertTrue(evidence.is_file())

    def test_hidden_suite_records_provider_failure_as_infrastructure_invalid(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            run_dir = root / "run"
            repository = run_dir / "frozen_candidate" / "repository"
            repository.mkdir(parents=True)
            (repository / "dist.js").write_text("frozen", encoding="utf-8")
            freeze_path = write_freeze(run_dir, repository)
            run_value = {
                "valid": False,
                "classification": "provider_failure",
                "candidate_classification": "candidate_contract_failure",
                "infrastructure_invalid": True,
                "broker_delta": {"calls": 1, "successful_calls": 0, "failures": 1},
            }
            with (
                patch("agentloop.evaluator.hidden_controller.EvaluatorBrokerLifecycle", FakeBroker),
                patch("agentloop.evaluator.hidden_controller.read_broker_stats", return_value=broker_stats()),
                patch("agentloop.evaluator.hidden_controller.run_case", return_value=run_value),
            ):
                result = run_hidden_suite(
                    freeze_path,
                    run_dir / "hidden-result.json",
                    root / "credential.env",
                    ROOT / "test_cases",
                )
            self.assertFalse(result["formal_result_eligible"])
            self.assertEqual(result["result_axis_status"], "N/A")
            self.assertEqual(result["cases"]["test_006"]["run"]["classification"], "provider_failure")

    def test_hidden_gate_is_atomic_and_single_use(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            run_dir = root / "run"
            repository = run_dir / "frozen_candidate" / "repository"
            repository.mkdir(parents=True)
            (repository / "dist.js").write_text("frozen", encoding="utf-8")
            freeze_path = write_freeze(run_dir, repository)
            run_value = {
                "valid": False,
                "classification": "candidate_no_observable_behavior",
                "candidate_classification": "candidate_no_observable_behavior",
                "infrastructure_invalid": False,
                "broker_delta": {"calls": 0, "successful_calls": 0, "failures": 0},
            }
            with (
                patch("agentloop.evaluator.hidden_controller.EvaluatorBrokerLifecycle", FakeBroker),
                patch("agentloop.evaluator.hidden_controller.read_broker_stats", return_value=broker_stats()),
                patch("agentloop.evaluator.hidden_controller.run_case", return_value=run_value),
            ):
                run_hidden_suite(freeze_path, run_dir / "hidden-result.json", root / "credential.env", ROOT / "test_cases")
            (run_dir / "hidden-result.json").unlink()
            with self.assertRaisesRegex(ValueError, "already consumed"):
                run_hidden_suite(freeze_path, run_dir / "hidden-result.json", root / "credential.env", ROOT / "test_cases")

    def test_missing_evaluator_credential_is_infrastructure_invalid_and_consumes_gate(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            run_dir = root / "run"
            repository = run_dir / "frozen_candidate" / "repository"
            repository.mkdir(parents=True)
            (repository / "dist.js").write_text("frozen", encoding="utf-8")
            freeze_path = write_freeze(run_dir, repository)
            result = run_hidden_suite(
                freeze_path,
                run_dir / "hidden-result.json",
                root / "missing-credential.env",
                ROOT / "test_cases",
            )
            self.assertEqual(result["classification"], "credential_mount_failure")
            self.assertTrue(result["infrastructure_invalid"])
            self.assertEqual(result["result_axis_status"], "N/A")
            self.assertTrue((run_dir / "hidden-once-gate.json").is_file())

    def test_launcher_spawn_failure_is_not_candidate_failure(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            repository = root / "candidate"
            (repository / "dist").mkdir(parents=True)
            (repository / "dist" / "cli.js").write_text("compiled", encoding="utf-8")
            request = root / "test_001" / "request.json"
            request.parent.mkdir()
            request.write_text("{}", encoding="utf-8")
            workspace = root / "fixture"
            workspace.mkdir()
            with (
                patch("agentloop.evaluator.lower_agent_launcher.runtime_preflight", return_value={"valid": True}),
                patch("agentloop.evaluator.lower_agent_launcher.read_broker_stats", return_value=broker_stats()),
                patch("agentloop.evaluator.lower_agent_launcher.subprocess.run", side_effect=OSError("spawn denied")),
            ):
                result = run_case(
                    repository,
                    request,
                    root / "case-output",
                    "http://127.0.0.1:1/v1/responses",
                    working_directory=workspace,
                )
            self.assertEqual(result["classification"], "launcher_failure")
            self.assertTrue(result["infrastructure_invalid"])
            self.assertIsNone(result["candidate_classification"])

    def test_native_runtime_preflight_uses_node22_and_real_database_round_trip(self) -> None:
        repository = ROOT / ".runtime" / "candidate-smoke" / "repository"
        if not repository.is_dir():
            self.skipTest("prepared OpenWiki runtime is unavailable")
        with tempfile.TemporaryDirectory() as temp:
            value = runtime_preflight(repository, Path(temp))
        self.assertTrue(value["valid"])
        self.assertEqual(value["classification"], "runtime_ready")
        self.assertEqual(value["observed"]["node"], "v22.12.0")
        self.assertEqual(value["observed"]["sqlite"], "node22-native-ok")
        self.assertTrue(value["observed"]["closed"])
        self.assertTrue(value["binding_sha256"])

    def test_pre_broker_native_failure_is_evaluator_runtime_infrastructure(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            repository = root / "candidate"
            (repository / "dist").mkdir(parents=True)
            (repository / "dist" / "cli.js").write_text("compiled", encoding="utf-8")
            request = root / "dev_001" / "request.json"
            request.parent.mkdir()
            request.write_text("{}", encoding="utf-8")
            with patch(
                "agentloop.evaluator.lower_agent_launcher.read_broker_stats",
                side_effect=AssertionError("broker must not be touched before runtime readiness"),
            ):
                value = run_case(repository, request, root / "output", "http://127.0.0.1:1/v1/responses")
            self.assertEqual(value["classification"], "native_binding_failure")
            self.assertTrue(value["infrastructure_invalid"])
            self.assertIsNone(value["candidate_classification"])
            self.assertFalse(value["product_started"])
            self.assertEqual(value["broker_calls"], 0)
            self.assertEqual(value["failure_attribution"]["owner"], "evaluator_runtime")

    def test_broker_and_provider_counter_classification_are_separate(self) -> None:
        self.assertEqual(_broker_classification({"broker_failures": 1}), "broker_failure")
        self.assertEqual(_broker_classification({"provider_failures": 1}), "provider_failure")
        self.assertEqual(_broker_classification({"credential_failures": 1}), "credential_mount_failure")
        self.assertIsNone(_broker_classification({"failures": 0}))

    def test_smoke_observer_paths_and_status_are_not_counter_accumulated(self) -> None:
        self.assertEqual(
            _join_endpoint("http://127.0.0.1:33507/v1/responses", "/v1/responses"),
            "http://127.0.0.1:33507/v1/responses",
        )
        self.assertEqual(
            _join_endpoint("http://127.0.0.1:33507/v1", "/v1/chat/completions"),
            "http://127.0.0.1:33507/v1/chat/completions",
        )
        with tempfile.TemporaryDirectory() as temp:
            state = State(Path(temp) / "stats.json", "http://127.0.0.1:1/v1/responses")
            state.add(calls=1, last_upstream_status=502)
            state.add(calls=1, last_upstream_status=200)
            self.assertEqual(state.value["calls"], 2)
            self.assertEqual(state.value["last_upstream_status"], 200)

    def test_gateway_dotenv_mapping_and_candidate_environment_do_not_leak_secret(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            credential = root / "provider.env"
            credential.write_text("# evaluator only\nexport GATEWAY_API_KEY=\"secret-test-token\"\n", encoding="utf-8")
            value, source = resolve_credential(credential)
            self.assertEqual(value, "secret-test-token")
            self.assertEqual(source, "GATEWAY_API_KEY")
            with patch.dict("os.environ", {"GATEWAY_API_KEY": "must-not-leak", "OPENAI_API_KEY": "also-secret"}, clear=False):
                env = _candidate_environment(root / "output", "http://127.0.0.1:1/v1/responses", root / "workspace", "test_001")
            self.assertNotIn("GATEWAY_API_KEY", env)
            self.assertNotIn("must-not-leak", json.dumps(env))
            self.assertEqual(env["OPENAI_API_KEY"], "broker-only-placeholder")

    def test_real_broker_lifecycle_health_smoke_records_no_secret(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            credential = root / "provider.env"
            credential.write_text("GATEWAY_API_KEY='secret-health-only'\n", encoding="utf-8")
            lifecycle = EvaluatorBrokerLifecycle(root / "broker", credential, "http://127.0.0.1:9")
            with lifecycle as broker:
                stats = read_broker_stats(broker.endpoint or "")
                self.assertEqual(stats["calls"], 0)
                self.assertEqual(stats["credential_source_key"], "GATEWAY_API_KEY")
            lifecycle_record = json.loads(lifecycle.lifecycle_path.read_text())
            self.assertEqual(lifecycle_record["status"], "stopped")
            evidence = "\n".join(path.read_text(encoding="utf-8", errors="replace") for path in (root / "broker").glob("*"))
            self.assertNotIn("secret-health-only", evidence)

    def test_local_broker_forwarding_forces_protocol_and_counts_success(self) -> None:
        received: list[dict[str, object]] = []

        class Upstream(BaseHTTPRequestHandler):
            def log_message(self, *_args) -> None:
                return

            def do_POST(self) -> None:  # noqa: N802
                payload = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
                received.append({
                    "path": self.path,
                    "payload": payload,
                    "authorization": self.headers.get("Authorization"),
                })
                body = json.dumps({
                    "id": "response-local-smoke",
                    "status": "completed",
                    "usage": {"input_tokens": 3, "output_tokens": 2},
                }).encode()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

        upstream = ThreadingHTTPServer(("127.0.0.1", 0), Upstream)
        thread = threading.Thread(target=upstream.serve_forever, daemon=True)
        thread.start()
        self.addCleanup(upstream.server_close)
        self.addCleanup(upstream.shutdown)
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            credential = root / "provider.env"
            credential.write_text("GATEWAY_API_KEY='secret-forward-only'\n", encoding="utf-8")
            upstream_url = f"http://127.0.0.1:{upstream.server_address[1]}"
            lifecycle = EvaluatorBrokerLifecycle(root / "broker", credential, upstream_url)
            with lifecycle as broker:
                request = urllib.request.Request(
                    broker.endpoint or "",
                    data=json.dumps({
                        "model": "candidate-selected-model",
                        "reasoning_effort": "low",
                        "input": "local smoke",
                    }).encode(),
                    method="POST",
                    headers={
                        "Content-Type": "application/json",
                        "Authorization": "Bearer broker-only-placeholder",
                    },
                )
                with urllib.request.urlopen(request, timeout=5) as response:
                    body = json.loads(response.read())
                self.assertEqual(body["status"], "completed")
                stats = read_broker_stats(broker.endpoint or "")
                self.assertEqual(stats["calls"], 1)
                self.assertEqual(stats["successful_calls"], 1)
                self.assertEqual(stats["forced_overrides"], 1)
                self.assertEqual(stats["total_tokens"], 5)
            self.assertEqual(received[0]["path"], "/v1/responses")
            self.assertEqual(received[0]["payload"]["model"], "gpt-5.6-sol")
            self.assertEqual(received[0]["payload"]["reasoning_effort"], "high")
            self.assertEqual(received[0]["authorization"], "Bearer secret-forward-only")
            evidence = "\n".join(path.read_text(encoding="utf-8", errors="replace") for path in (root / "broker").glob("*"))
            self.assertNotIn("secret-forward-only", evidence)


if __name__ == "__main__":
    unittest.main()
