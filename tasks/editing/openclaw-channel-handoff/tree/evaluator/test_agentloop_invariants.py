#!/usr/bin/env python3
"""Fast evaluator-side invariant tests; no Gateway or provider is started."""
from __future__ import annotations

import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from controller.builder_session_controller import (
    BuilderSessionController,
    PROBE_MARKER,
    validate_delivery,
)
from controller.two_round_controller import DEV, HIDDEN, TwoRoundController, tree_digest
from evaluator.case_service import make_private_facts, redacted_projection
from evaluator.formal_gates import validate_builder_attestation
from evaluator.hidden_executor import freeze_run_mode, route_params, scrub_environment
from evaluator.public_runner import run_public_lifecycle
from lower_agent.launcher import classify_execution, validate_agent_result, validate_agent_run_report
from evaluator.test_dev_feedback import semantic_fixture


class AgentLoopInvariantTests(unittest.TestCase):
    def test_protocol_lock_matches_create_aligned_ten_round_lifecycle(self) -> None:
        lock = json.loads((Path(__file__).resolve().parents[1] / "schemas/protocol_lock.json").read_text(encoding="utf-8"))
        self.assertEqual(lock["max_candidate_rounds"], 10)
        self.assertEqual(lock["candidate_rounds"], "up_to_10")
        self.assertEqual(lock["feedback_revisions"], "fresh_after_each_acceptance")
        self.assertFalse(lock["dev_passed_is_automatic_freeze"])

    def _controller(self, root: Path) -> TwoRoundController:
        c1, c2 = root / "c1", root / "c2"
        c1.mkdir(); c2.mkdir()
        (c1 / "candidate.txt").write_text("one", encoding="utf-8")
        (c2 / "candidate.txt").write_text("two", encoding="utf-8")
        controller = TwoRoundController(
            root / "lifecycle",
            lambda number, candidate: {case: semantic_fixture(root/'scores'/str(number)/case,case,tree_digest(candidate),45) for case in DEV},
        )
        controller.submit(c1); controller.submit(c2)
        return controller

    def test_freeze_is_immutable_and_digest_bound(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            controller = self._controller(Path(directory))
            manifest = controller.freeze_candidate_2()
            frozen = Path(directory) / "lifecycle" / "frozen_candidate"
            self.assertEqual(manifest["candidate_digest"], tree_digest(frozen))
            self.assertEqual(manifest["accepted_submission_count"], 2)
            self.assertEqual(manifest["accepted_candidate_digests"], [tree_digest(Path(directory) / "lifecycle" / "candidates" / "candidate_001"), tree_digest(Path(directory) / "lifecycle" / "candidates" / "candidate_002")])
            self.assertTrue(manifest["frozen_tree_read_only"])
            with self.assertRaises(RuntimeError):
                controller.run_hidden(lambda _path: {case: {} for case in HIDDEN[:1]})

    def test_hidden_requires_all_cases_and_rejects_replay(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            controller = self._controller(Path(directory))
            controller.freeze_candidate_2()
            result = controller.run_hidden(lambda _path: {case: {"classification": "smoke"} for case in HIDDEN})
            self.assertEqual(set(result), set(HIDDEN))
            with self.assertRaisesRegex(RuntimeError, "replay"):
                controller.run_hidden(lambda _path: {case: {} for case in HIDDEN})

    def test_hidden_detects_frozen_tree_mutation(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            controller = self._controller(Path(directory))
            controller.freeze_candidate_2()
            frozen = Path(directory) / "lifecycle" / "frozen_candidate"

            def mutate(_path: Path) -> dict[str, object]:
                (frozen / "tampered.txt").write_text("tampered", encoding="utf-8")
                return {case: {} for case in HIDDEN}

            with self.assertRaises((PermissionError, RuntimeError)):
                controller.run_hidden(mutate)

    def test_artifact_validation_never_creates_files(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            self.assertEqual(validate_agent_result(root / "agent_result.json", "test_001")[0], False)
            self.assertEqual(validate_agent_run_report(root / "run_report.json")[0], False)
            self.assertFalse((root / "agent_result.json").exists())

    def test_artifact_validation_rejects_private_values_and_extra_fields(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            result = {"schema_version": "openclaw-agent-result-v1", "case_id": "test_001", "decision": "unknown", "observed_actions": [], "evidence": [], "uncertainty": [], "extra": True}
            (root / "agent_result.json").write_text(json.dumps(result), encoding="utf-8")
            self.assertFalse(validate_agent_result(root / "agent_result.json", "test_001")[0])
            result.pop("extra")
            result["decision"] = "contains-secret-nonce"
            (root / "agent_result.json").write_text(json.dumps(result), encoding="utf-8")
            self.assertFalse(validate_agent_result(root / "agent_result.json", "test_001", ["secret-nonce"])[0])

    def test_failure_classification_separates_broker_from_candidate(self) -> None:
        report = {"status": "partial", "health": {"status": "ok"}, "errors": ["503 provider"]}
        self.assertEqual(
            classify_execution(report, {"calls": 2, "failures": 2, "successful_calls": 0, "input_tokens": 0, "output_tokens": 0, "total_tokens": 0}, False, "missing")[0],
            "broker_infrastructure_error",
        )
        self.assertEqual(
            classify_execution(report, {"calls": 2, "failures": 0, "successful_calls": 2, "input_tokens": 1, "output_tokens": 1, "total_tokens": 2}, False, "missing")[0],
            "candidate_product_failure",
        )

    def test_case_projection_excludes_private_oracle_values(self) -> None:
        facts = make_private_facts("test_004")
        view = redacted_projection(facts)
        serialized = json.dumps(view, ensure_ascii=False)
        self.assertNotIn(facts.task_nonce, serialized)
        self.assertNotIn(facts.callback_token, serialized)
        self.assertNotIn(facts.oracle_decision, serialized)
        self.assertFalse(view["oracle_disclosed"])

    def test_production_rpc_uses_registered_webchat_channel(self) -> None:
        params = route_params("test_004", {"channel": {"provider": "not-a-real-adapter", "account_id": "x", "peer_id": "y"}}, "task")
        self.assertEqual(params["channel"], "webchat")
        self.assertNotEqual(params["channel"], "loopback-channel")

    def _write_delivery(self, root: Path, name: str, marker: bool = False) -> None:
        root.mkdir(parents=True, exist_ok=True)
        changed = f"src/gateway/{name}.ts"
        patch_body = f"""diff --git a/{changed} b/{changed}
new file mode 100644
--- /dev/null
+++ b/{changed}
@@ -0,0 +1 @@
+export const value = {name!r};
"""
        if marker:
            patch_body += PROBE_MARKER
        (root / "solution.patch").write_text(patch_body, encoding="utf-8")
        (root / "edit_report.json").write_text(json.dumps({
            "summary": "test delivery",
            "changed_paths": [changed],
            "production_seams": [changed],
            "known_limits": [],
        }), encoding="utf-8")
        (root / "run_report.json").write_text(json.dumps({
            "status": "ok", "commands": [], "tests": [], "errors": [],
        }), encoding="utf-8")

    def test_builder_delivery_rejects_probe_marker_and_unapproved_paths(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            delivery = root / "delivery"
            self._write_delivery(delivery, "safe", marker=True)
            errors = validate_delivery(delivery)
            self.assertTrue(any("probe marker" in error for error in errors))
            self.assertEqual(freeze_run_mode(delivery), ("probe", "explicit probe run; marker paths=['solution.patch']"))
            with self.assertRaisesRegex(RuntimeError, "formal hidden run refuses"):
                freeze_run_mode(delivery, formal=True)

            disallowed = root / "disallowed"
            self._write_delivery(disallowed, "unsafe")
            patch = disallowed / "solution.patch"
            patch.write_text(patch.read_text(encoding="utf-8").replace("src/gateway/unsafe.ts", "package.json"), encoding="utf-8")
            self.assertTrue(any("outside allowed" in error for error in validate_delivery(disallowed)))

    def test_builder_session_requires_ack_and_attests_two_public_rounds(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "source"
            source.mkdir()
            workspace = root / "workspace"
            self._write_delivery(workspace, "one")

            def evaluate(_number: int, candidate: Path) -> dict[str, object]:
                digest = tree_digest(candidate)
                return {
                    case_id: {
                        "case_id": case_id,
            "production_entry": "openclaw-native-embedded-v1: node openclaw.mjs agent --local -> native case client -> two native Gateway CLIs",
                        "model": "deepseek-flash", "reasoning_effort": "high",
                        "credential_mode": "placeholder-only", "run_mode": "formal",
                        "candidate_sensitive_environment_keys": [],
                        "candidate_runtime_ready": True, "classification": "candidate_behavior_observed",
                        "frozen_candidate_digest_before": digest, "frozen_candidate_digest_after": digest,
                        "frozen_candidate_digest_stable": True,
                        "broker_stats_delta": {"calls": 1, "failures": 0, "successful_calls": 1, "input_tokens": 1, "output_tokens": 1, "total_tokens": 2, "tokens": 2},
                        "semantic_result": semantic_fixture(root/'scores'/str(_number)/case_id,case_id,digest,45)['semantic_result'],
                    } for case_id in DEV
                }

            controller = BuilderSessionController(run_dir=root / "run", workspace=workspace, source=source, evaluate=evaluate, max_dev_rounds=2, require_native_evidence=False)
            first_code, first = controller.submit(controller.session_id)
            self.assertEqual(first_code, 200)
            self._write_delivery(workspace, "two")
            self.assertEqual(controller.submit(controller.session_id, first["feedback_digest"])[0], 409)
            controller.record_delivery(first)
            second_code, second = controller.submit(controller.session_id, first["feedback_digest"])
            self.assertEqual(second_code, 200)
            self.assertTrue(second["freeze"])
            self.assertNotEqual(controller.submissions[0]["candidate_digest"], controller.submissions[1]["candidate_digest"])
            attestation = controller.write_attestation(builder_exit_code=0, builder_started_at="2026-09-02T00:00:00+00:00", builder_finished_at="2026-09-02T00:01:00+00:00")
            self.assertFalse(attestation["formal_lifecycle_eligible"])
            self.assertFalse(attestation["native_evidence"]["valid"])
            _, errors = validate_builder_attestation(
                attestation_path=root / "run/builder_session_attestation.json",
                freeze_manifest_path=root / "run/lifecycle/freeze_manifest.json",
                frozen_candidate=root / "run/lifecycle/frozen_candidate",
            )
            self.assertTrue(any("native Builder evidence" in error for error in errors))

    def test_candidate_environment_is_placeholder_only(self) -> None:
        with patch.dict(os.environ, {"OPENAI_API_KEY": "real", "AWS_SECRET_ACCESS_KEY": "real"}, clear=False):
            env = scrub_environment()
        self.assertEqual(env["OPENAI_API_KEY"], "broker-only-placeholder")
        self.assertEqual(env["OPENCLAW_SKIP_CHANNELS"], "1")
        self.assertNotIn("AWS_SECRET_ACCESS_KEY", env)

    def test_direct_public_runner_is_not_a_formal_builder_path(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            with self.assertRaisesRegex(RuntimeError, "formal public lifecycle requires"):
                run_public_lifecycle(
                    candidate_1=root / "candidate-1", candidate_2=root / "candidate-2",
                    run_dir=root / "run", broker_endpoint="http://127.0.0.1:1/v1/responses",
                    runtime=None, builder_session_id="pretend-session", timeout_seconds=1,
                )


if __name__ == "__main__":
    unittest.main()
