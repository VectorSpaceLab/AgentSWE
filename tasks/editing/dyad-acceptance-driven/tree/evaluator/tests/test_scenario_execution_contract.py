#!/usr/bin/env python3
"""Provider-free negative tests for distinct model-driven Dyad scenarios."""
from __future__ import annotations

import hashlib
import importlib.util
import inspect
import io
import json
import sys
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest import mock


ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))


def load_module(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


scenario = load_module("dyad_scenario_contract_tests", ROOT / "evaluator/scenario_contract.py")
lower = load_module("dyad_scenario_lower_tests", ROOT / "evaluator/harness/run_lower_agent_case.py")
headless = load_module("dyad_scenario_headless_tests", ROOT / "environment/headless_chat_flow.py")
axes = load_module("dyad_scenario_formal_axes_tests", ROOT / "evaluator/formal_axes.py")


def sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def binding() -> dict[str, object]:
    return {
        "app_id": 17,
        "chat_id": 29,
        "run_id": "run-observed",
        "session_id": "session-observed",
        "revision": "a" * 64,
        "target_fingerprint": "b" * 64,
    }


def stats(calls: int, successful: int) -> dict[str, object]:
    return {
        "protocol": {"model": lower.MODEL, "reasoning_effort": lower.EFFORT},
        "runtime": {
            "calls": calls,
            "successful_calls": successful,
            "provider_failures": 0,
            "broker_failures": 0,
            "failures": 0,
            "client_failures": 0,
        },
    }


# The reference terminal session a conforming product returns.  Every
# cross-cutting obligation in input/02 "independently verifiable product contract" is satisfied by
# these bytes, so the provider-free fixture keeps describing behaviour a real
# Candidate could produce.
TERMINAL_RESULT = {"passed": 1, "failed": 0, "skipped": 0}


def reference_session(target: object, sequence: int) -> dict[str, object]:
    return {
        "sessionId": "session-observed",
        "runId": "run-observed",
        "status": "passed",
        "target": target,
        "testFingerprint": "b" * 64,
        "startedRevision": "a" * 64,
        "currentRevision": "a" * 64,
        "lastEventSequence": sequence,
        "result": TERMINAL_RESULT,
    }


def reference_result_digest(target: object) -> str:
    return scenario.canonical_result_digest(reference_session(target, 3))


def action_result(case_id: str, name: str, target: object = None) -> object:
    if name == "inspect_latest":
        return {"ok": True, "value": {"runId": "run-observed", "generation": 1,
                                      "currentTarget": target}}
    if name in {"start_preview", "retry_preview"}:
        if case_id == "test_002" and name == "start_preview":
            return {
                "delivery": "lost_after_product_acceptance",
                "product_action_accepted": True,
                "operationId": "operation-observed",
                "sessionIdWithheld": True,
            }
        return {"ok": True, "value": reference_session(target, 1)}
    if name == "get_preview":
        return {"ok": True, "value": reference_session(target, 3)}
    if name == "get_run":
        return {"ok": True, "value": {"runId": "run-observed", "status": "infrastructure",
                                      "currentTarget": target}}
    if name == "get_attestation":
        return {"ok": True, "value": {"attestationId": "attestation-observed",
                                      "sessionId": "session-observed",
                                      "outcome": "passed",
                                      "testFingerprint": "b" * 64,
                                      "startedRevision": "a" * 64,
                                      "finishedRevision": "a" * 64,
                                      "resultDigest": reference_result_digest(target)}}
    if name in {"stale_control", "foreign_control"}:
        return {"ok": False, "error": {"code": "REJECTED", "message": "typed product rejection"}}
    if name == "foreign_read":
        return {"ok": True, "value": None}
    if name == "mutate_target":
        return {"mutated": True}
    if name == "restore_target":
        return {"restored": True}
    if name in {"stop_tests", "duplicate_stop"}:
        return {"ok": True, "value": {"stopped": True}}
    if name == "restart_state":
        # The real dispatcher's shape, verbatim:
        # environment/scenario_chat_flow.test.ts:606.  The stub used to answer
        # {"restarted": True, ...}, a key the product never emits, which is what
        # let the unit test stay green on a rule no real rollout could satisfy.
        return {"restart_requested": True, "same_disk_state": True,
                "automaticRerunRequested": False}
    if name == "compatibility_chat":
        return {"streamEnd": 1, "responseErrors": 0}
    if name == "finish":
        return {"captured": True, "artifactSha256": "f" * 64}
    raise AssertionError(f"no provider-free action result for {case_id}/{name}")


def action_record(case_id: str, name: str, index: int,
                  target: object = None) -> dict[str, object]:
    request: dict[str, object] = {"action": name}
    if name in {"start_preview", "retry_preview", "conflict_start"}:
        request["command"] = {"appId": 17, "chatId": 29, "runId": "run-observed",
                              "operationId": "operation-observed",
                              "expectedGeneration": 1, "target": target,
                              "presentation": "preview"}
    return {
        "sequence": index,
        "action": name,
        "request": request,
        "requested_by_model": True,
        "executor": "generic_model_action_dispatch",
        "evaluator_defaulted": False,
        "source_message_id": index,
        "source_message_sha256": f"{index:064x}",
        "dispatch_status": "completed",
        "result": action_result(case_id, name, target),
    }


def contract_evidence(target: object) -> dict[str, object]:
    """The evaluator-owned read probes a conforming rollout produces."""
    return {
        "target_bytes_sha256_at_admission": "b" * 64,
        "read_stability_probes": [
            {"kind": "terminal_preview", "sequence": 3, "status": "passed", "stable": True},
            {"kind": "attestation", "sequence": 4, "status": "passed", "stable": True},
        ],
        "acceptance_gate_probes": [
            {"sequence": 4, "attestation_outcome": "passed",
             "attestation_identity_present": True, "run_snapshot_readable": True,
             "run_status": "passed", "passing_evidence_cites_attestation": True},
        ],
        # The evaluator's own cold-restart receipt, same keys and same shape the
        # resumed harness writes (environment/scenario_chat_flow.test.ts:275-278).
        # restart_state's observation rule reads it, so a conforming rollout's
        # synthetic evidence has to carry it too.
        "cold_restart": {
            "actual_fresh_process": True,
            "previous_process_pid": 41,
            "fresh_process_pid": 42,
            "previous_namespace": "pid:[4026531836]",
            "fresh_namespace": "pid:[4026531999]",
            "persisted_app_id": 17,
            "persisted_chat_id": 29,
            "same_disk_state": True,
            "evaluator_rewrote_product_state": False,
            "automatic_test_run_count_at_start": 0,
            "presentation": "headless main-process registration; not native Electron",
        },
    }


class ScenarioExecutionContractTests(unittest.TestCase):
    def test_hidden_dispatch_has_six_distinct_axes_and_action_contracts(self) -> None:
        hidden = [scenario.SCENARIOS[f"test_{index:03d}"] for index in range(1, 7)]
        self.assertEqual(len({item.axis for item in hidden}), 6)
        self.assertEqual(len({item.required_actions for item in hidden}), 6)
        self.assertEqual(len({item.required_checks for item in hidden}), 6)
        for item in hidden:
            # A hidden case must assert at least four product-behaviour facts;
            # a single trivially satisfiable check is not a hard capability.
            self.assertGreaterEqual(len(item.required_checks), 4)
        self.assertEqual(
            [item.axis for item in hidden],
            [
                "verified terminal proof that survives a cold restart",
                "idempotent retry after a lost response and operation-identity conflict",
                "denial without disclosure and an auditable denial ledger",
                "content-addressed revision drift permanently invalidates a green run",
                "cancellation wins over a late callback and a duplicate stop",
                "cold restart reconciles interrupted work honestly and stays compatible",
            ],
        )
        with tempfile.TemporaryDirectory() as raw:
            task = Path(raw) / "input.md"
            task.write_text("unknown scenario\n", encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "no explicit Dyad scenario dispatch"):
                scenario.prepare_scenario("test_999", task, Path(raw) / "prepared")

    def test_preparation_binds_exact_natural_task_without_disclosing_private_oracle(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            task = root / "test_002" / "input.md"
            task.parent.mkdir()
            task.write_text("Please recover the preview after a lost response.\n", encoding="utf-8")
            prepared = scenario.prepare_scenario("test_002", task, root / "prepared")
            executed = Path(prepared["executed_task_path"])
            self.assertTrue(executed.read_text(encoding="utf-8").startswith(task.read_text(encoding="utf-8").strip()))
            self.assertEqual(prepared["executed_task_sha256"], sha(executed))
            public = scenario.read_json(Path(prepared["public_fixture_path"]))
            private = prepared["private_oracle"]
            private_path = Path(prepared["private_oracle_path"])
            self.assertFalse(private_path.exists())
            self.assertEqual(set(public), scenario.PUBLIC_FIXTURE_KEYS)
            scenario.validate_public_fixture(public)
            serialized_public = json.dumps(public, sort_keys=True)
            for forbidden in scenario.PRIVATE_PUBLIC_KEYS:
                self.assertNotIn(f'"{forbidden}"', serialized_public)
            self.assertNotIn("axis", public)
            self.assertIn("fault_injection", public)
            self.assertNotIn("expected_terminal", serialized_public)
            self.assertNotIn("expected_product_status", serialized_public)
            self.assertNotIn("expected status", executed.read_text(encoding="utf-8").lower())
            self.assertEqual(private["executed_task_sha256"], sha(executed))
            self.assertTrue(private["private_not_candidate_visible"])
            for leaked_key, leaked_value in (
                ("required_actions", ["inspect_latest"]),
                ("required_scenario_checks", ["terminal_passed"]),
                ("expected_terminal_status", "passed"),
            ):
                leaked = dict(public)
                leaked[leaked_key] = leaked_value
                with self.subTest(leaked_key=leaked_key), self.assertRaises(ValueError):
                    scenario.validate_public_fixture(leaked)

    def test_private_oracle_is_absent_during_lower_subprocess_and_persisted_after(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            case = root / "input.md"
            case.write_text("Exercise the failed-first Acceptance scenario.\n", encoding="utf-8")
            output = root / "result.json"
            private_path = output.with_name(output.stem + ".scenario") / "private_oracle.json"
            comparison_path = output.with_name(output.stem + ".private-oracle-comparison.json")

            def fake_launcher(command, **_kwargs):
                self.assertFalse(private_path.exists(), "private oracle existed in the Candidate subprocess namespace")
                self.assertNotIn(str(private_path), command)
                return mock.Mock(returncode=1, stdout="provider-free launcher substitute", stderr="")

            argv = [
                "run_lower_agent_case.py", "--case-deadline-monotonic", str(__import__("time").monotonic()+590),
                "--repository", str(root / "candidate"),
                "--case", str(case),
                "--case-id", "test_001",
                "--broker-endpoint", "http://127.0.0.1:1/v1/responses",
                "--output", str(output),
                "--mode", "headless",
            ]
            with mock.patch.object(lower, "broker_stats", side_effect=[stats(0, 0), stats(1, 1)]), \
                    mock.patch.object(lower.subprocess, "run", side_effect=fake_launcher), \
                    mock.patch.object(sys, "argv", argv), redirect_stdout(io.StringIO()):
                self.assertEqual(lower._main(), 0)

            self.assertTrue(private_path.is_file())
            self.assertTrue(comparison_path.is_file())
            comparison = scenario.read_json(comparison_path)
            self.assertEqual(comparison["private_oracle_path"], str(private_path))
            self.assertTrue(comparison["private_oracle_not_candidate_visible"])

    def test_headless_main_has_no_legacy_generic_flow_or_synthetic_agent_artifact_fallback(self) -> None:
        main_source = inspect.getsource(headless._main)
        driver = (ROOT / "environment/scenario_chat_flow.test.ts").read_text(encoding="utf-8")
        self.assertNotIn("TEST_SOURCE", main_source)
        self.assertIn("scenario_chat_flow.test.ts", main_source)
        self.assertIn("requested_by_model: true", driver)
        self.assertIn('executor: "generic_model_action_dispatch"', driver)
        self.assertIn("evaluator_defaulted: false", driver)
        self.assertIn("captureModelArtifact(request.artifact)", driver)
        self.assertNotIn("writeArtifact({", driver)
        self.assertNotIn("generic_fallback_used: true", driver)
        one_stop_source = (ROOT / "harbor/formal_one_stop.py").read_text(encoding="utf-8")
        self.assertIn('"--case-id", case_id', one_stop_source)
        self.assertIn('hidden-after-freeze-attestation.json', one_stop_source)

    def test_private_oracle_comparison_binds_task_actions_artifact_and_workspace(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            task = root / "test_001" / "input.md"
            task.parent.mkdir()
            task.write_text("Repair the current app after observing a failure.\n", encoding="utf-8")
            prepared = scenario.prepare_scenario("test_001", task, root / "prepared")
            private_path = scenario.persist_private_oracle(prepared)
            private = prepared["private_oracle"]
            names = private["required_actions"]
            workspace = binding()
            evidence = {
                "case_id": "test_001",
                "scenario_id": "test_001",
                "executed_task_sha256": prepared["executed_task_sha256"],
                "generic_fallback_used": False,
                "private_oracle_visible": False,
                "agent_artifact_origin": "model_finish_action",
                "workspace": workspace,
                "product_action_trajectory": [
                    action_record("test_001", name, index, private["target"])
                    for index, name in enumerate(names, start=1)
                ],
                "scenario_checks": {
                    key: True for key in private["required_scenario_checks"]
                },
                **contract_evidence(private["target"]),
            }
            artifact = {
                "schema_version": "dyad-lower-agent-artifact-v3",
                "case_id": "test_001",
                "scenario_id": "test_001",
                "task_sha256": prepared["executed_task_sha256"],
                "workspace": workspace,
                "product_actions": names,
                "blockers": [],
            }
            evidence_path = root / "native.json"
            artifact_path = root / "artifact.json"
            scenario.write_json(evidence_path, evidence)
            scenario.write_json(artifact_path, artifact)
            comparison = scenario.compare_scenario(
                private_path, evidence_path,
                artifact_path, root / "comparison.json",
            )
            self.assertTrue(comparison["passed"])
            for key in private["required_contract_checks"]:
                self.assertTrue(comparison["checks"][f"contract:{key}"], key)
            artifact["task_sha256"] = "0" * 64
            scenario.write_json(artifact_path, artifact)
            mismatch = scenario.compare_scenario(
                private_path, evidence_path,
                artifact_path, root / "comparison-mismatch.json",
            )
            self.assertFalse(mismatch["passed"])
            self.assertFalse(mismatch["checks"]["executed_task"])
            artifact["task_sha256"] = prepared["executed_task_sha256"]
            artifact["product_actions"] = names[:-1]
            scenario.write_json(artifact_path, artifact)
            missing_action = scenario.compare_scenario(
                private_path, evidence_path,
                artifact_path, root / "comparison-missing-action.json",
            )
            self.assertFalse(missing_action["passed"])
            self.assertFalse(missing_action["checks"]["artifact_binds_action_trajectory"])

    def test_required_actions_fail_closed_on_unusable_observations(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            task = root / "input.md"
            task.write_text("Complete a failed-first Acceptance repair.\n", encoding="utf-8")
            prepared = scenario.prepare_scenario("test_001", task, root / "prepared")
            private_path = scenario.persist_private_oracle(prepared)
            private = prepared["private_oracle"]
            names = private["required_actions"]
            workspace = binding()
            artifact = {
                "schema_version": "dyad-lower-agent-artifact-v3",
                "case_id": "test_001",
                "scenario_id": "test_001",
                "task_sha256": prepared["executed_task_sha256"],
                "workspace": workspace,
                "product_actions": names,
                "blockers": [],
            }
            artifact_path = root / "artifact.json"
            scenario.write_json(artifact_path, artifact)

            failures = {
                "dispatcher_error": ("get_preview", {"action_error": "IDs did not match"}, "error"),
                "missing_handler": ("get_attestation", {"missingHandler": "acceptance:get-attestation"}, "completed"),
                "absent_read": ("get_attestation", {"ok": True, "value": None}, "completed"),
            }
            for label, (action_name, bad_result, dispatch_status) in failures.items():
                with self.subTest(label=label):
                    records = [action_record("test_001", name, index) for index, name in enumerate(names, start=1)]
                    target = next(item for item in records if item["action"] == action_name)
                    target["result"] = bad_result
                    target["dispatch_status"] = dispatch_status
                    evidence = {
                        "case_id": "test_001",
                        "scenario_id": "test_001",
                        "executed_task_sha256": prepared["executed_task_sha256"],
                        "generic_fallback_used": False,
                        "private_oracle_visible": False,
                        "agent_artifact_origin": "model_finish_action",
                        "workspace": workspace,
                        "product_action_trajectory": records,
                        "scenario_checks": {key: True for key in private["required_scenario_checks"]},
                    }
                    evidence_path = root / f"{label}.native.json"
                    scenario.write_json(evidence_path, evidence)
                    comparison = scenario.compare_scenario(
                        private_path, evidence_path, artifact_path,
                        root / f"{label}.comparison.json",
                    )
                    self.assertFalse(comparison["passed"])
                    self.assertFalse(comparison["checks"]["required_action_observations_usable"])

            records = [action_record("test_001", name, index) for index, name in enumerate(names, start=1)]
            records = [item for item in records if item["action"] != "get_attestation"]
            missing_evidence = {
                "case_id": "test_001",
                "scenario_id": "test_001",
                "executed_task_sha256": prepared["executed_task_sha256"],
                "generic_fallback_used": False,
                "private_oracle_visible": False,
                "agent_artifact_origin": "model_finish_action",
                "workspace": workspace,
                "product_action_trajectory": records,
                "scenario_checks": {key: True for key in private["required_scenario_checks"]},
            }
            missing_path = root / "missing-attestation.native.json"
            scenario.write_json(missing_path, missing_evidence)
            missing_comparison = scenario.compare_scenario(
                private_path, missing_path, artifact_path,
                root / "missing-attestation.comparison.json",
            )
            self.assertFalse(missing_comparison["passed"])
            self.assertFalse(missing_comparison["checks"]["required_action_order"])
            self.assertFalse(missing_comparison["checks"]["required_action_observations_usable"])

    def test_test003_accepts_only_real_typed_rejection_observations(self) -> None:
        for action_name in ("stale_control", "foreign_control"):
            ok, _reason = scenario.required_action_observation(
                "test_003", action_name,
                {"dispatch_status": "completed", "result": {"ok": False, "error": {"code": "REJECTED"}}},
            )
            self.assertTrue(ok)
        ok, _reason = scenario.required_action_observation(
            "test_003", "foreign_read",
            {"dispatch_status": "completed", "result": {"ok": True, "value": None}},
        )
        self.assertTrue(ok)
        for unusable in (
            {"dispatch_status": "error", "result": {"action_error": "bad ownership input"}},
            {"dispatch_status": "completed", "result": {"missingHandler": "acceptance:control-run"}},
            {"dispatch_status": "completed", "result": None},
            {"dispatch_status": "completed", "result": {"status": "rejected"}},
        ):
            ok, _reason = scenario.required_action_observation("test_003", "stale_control", unusable)
            self.assertFalse(ok)

    def test_classifier_fails_closed_on_fallback_task_or_oracle_mismatch(self) -> None:
        workspace = binding()
        artifact = {
            "schema_version": "dyad-lower-agent-artifact-v3",
            "case_id": "test_004",
            "scenario_id": "test_004",
            "task_sha256": "c" * 64,
            "workspace": workspace,
        }
        native = {
            "real_product": True,
            "acceptance_surface_observed": True,
            "action_protocol_complete": True,
            "generic_fallback_used": False,
            "agent_artifact_origin": "model_finish_action",
            "case_id": "test_004",
            "scenario_id": "test_004",
            "executed_task_sha256": "c" * 64,
            "workspace": workspace,
        }
        base = dict(
            mode="headless", launcher_exit=0, artifact=artifact,
            native_evidence=native, oracle_comparison={"passed": True},
            before=stats(0, 0), after=stats(1, 1),
        )
        self.assertEqual(lower.classify(**base), ("valid", None))
        for mutation, expected in (
            ({"generic_fallback_used": True}, "generic_single_flow_fallback_detected"),
            ({"executed_task_sha256": "d" * 64}, "executed_task_identity_mismatch"),
        ):
            changed = dict(native)
            changed.update(mutation)
            self.assertEqual(
                lower.classify(**{**base, "native_evidence": changed}),
                ("candidate_failure", expected),
            )
        self.assertEqual(
            lower.classify(**{**base, "oracle_comparison": {"passed": False}}),
            ("valid", None),
        )
        self.assertEqual(
            lower.classify(**{**base, "artifact": None}),
            ("candidate_failure", "model_authored_artifact_missing"),
        )
        incomplete = dict(native)
        incomplete["action_protocol_complete"] = False
        self.assertEqual(
            lower.classify(**{**base, "native_evidence": incomplete}),
            ("candidate_failure", "model_action_trajectory_incomplete"),
        )

    def test_formal_overlay_routes_each_exact_executed_task_and_comparison(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            run_dir = Path(raw)
            records: dict[str, object] = {}
            expected: dict[str, str] = {}
            for index in range(1, 7):
                case_id = f"test_{index:03d}"
                case_dir = run_dir / "hidden_after_freeze" / case_id
                case_dir.mkdir(parents=True)
                task = case_dir / "executed_task.md"
                task.write_text(f"exact dynamic task for {case_id}\n", encoding="utf-8")
                task_sha = sha(task)
                comparison = case_dir / "private-oracle-comparison.json"
                scenario.write_json(comparison, {
                    "case_id": case_id,
                    "executed_task_sha256": task_sha,
                    "passed": False, "private_oracle_not_candidate_visible": True,
                })
                records[case_id] = {
                    "case_id": case_id,
                    "executed_task_path": str(task),
                    "executed_task_sha256": task_sha,
                    "private_oracle_comparison_path": str(comparison),
                    "private_oracle_comparison_sha256": sha(comparison),
                }
                expected[case_id] = task.read_text(encoding="utf-8")
            scenario.write_json(
                run_dir / "hidden-after-freeze-attestation.json",
                {"cases": records},
            )
            overlay, comparisons = axes.prepare_run_local_formal_root(run_dir)
            for case_id, text in expected.items():
                routed = overlay / "test_cases" / case_id / "input.md"
                self.assertEqual(routed.read_text(encoding="utf-8"), text)
                self.assertEqual(comparisons[case_id], Path(records[case_id]["private_oracle_comparison_path"]))
                self.assertNotEqual(routed.resolve(), ROOT / "test_cases" / case_id / "input.md")


if __name__ == "__main__":
    unittest.main()
