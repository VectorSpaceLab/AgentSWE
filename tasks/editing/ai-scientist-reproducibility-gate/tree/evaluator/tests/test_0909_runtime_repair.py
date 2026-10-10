"""Provider-free regression tests for execution, retry and visibility boundaries."""
from __future__ import annotations

import io
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch
import urllib.error

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "agentloop"))
import lower_agent_launcher as launcher
from case_world import CaseWorld, CASE_WORLD_PHASES
from two_round_controller import Controller


class Response(io.BytesIO):
    def __enter__(self): return self
    def __exit__(self, *_args): self.close()


class RuntimeRepairTests(unittest.TestCase):
    def test_final_authoring_502_never_resamples_unknown_completion(self):
        payload = {name: None for name in (
            "schema_version", "case_id", "tool_events", "observed_facts", "receipt_id", "state",
            "rollout_digest", "artifact_path", "artifact_digest", "honest_recovery", "safe", "oracle_leak")}
        response = Response(json.dumps({"model": "gpt-5.6-sol", "status": "completed", "output_text": json.dumps(payload)}).encode())
        error = urllib.error.HTTPError("http://local.invalid/v1/responses", 502, "transient", {}, io.BytesIO(b"transient"))
        with patch.object(launcher.urllib.request, "urlopen", side_effect=[error, response]) as send, patch.object(launcher.time, "sleep"):
            with self.assertRaises(urllib.error.HTTPError):
                launcher.ask_model_to_author_result("http://local.invalid/v1/responses", "task", {}, {})
        self.assertEqual(send.call_count, 1)

    def test_invalid_completed_model_json_is_not_retried(self):
        with patch.object(launcher.urllib.request, "urlopen", return_value=Response(b'{"model":"gpt-5.6-sol","status":"completed","output_text":"bad-json"}')) as send:
            with self.assertRaises(launcher.ModelContentError):
                launcher._request_model_json("http://local.invalid", "task", "action")
        self.assertEqual(send.call_count, 1)

    def test_ambiguous_socket_timeout_is_not_retried(self):
        with patch.object(launcher.urllib.request, "urlopen", side_effect=TimeoutError("socket")) as send:
            with self.assertRaises(TimeoutError):
                launcher._request_model_json("http://local.invalid", "task", "action")
        self.assertEqual(send.call_count, 1)

    def test_candidate_environment_scrubs_all_inherited_authority(self):
        with patch.dict(os.environ, {"GATEWAY_API_KEY": "fake-secret", "AWS_SECRET_ACCESS_KEY": "fake-aws", "SERVICE_AUTH_TOKEN": "fake-token", "UPSTREAM_URL": "private", "RESULT_JUDGE_ENDPOINT": "private-judge", "EVALUATOR_PORT": "12345", "PATH": "/bin"}, clear=True):
            env = launcher.candidate_environment(Path("/candidate"), "http://broker/v1/responses")
        self.assertNotIn("fake-secret", str(env))
        self.assertNotIn("fake-aws", str(env))
        self.assertNotIn("fake-token", str(env))
        self.assertNotIn("UPSTREAM_URL", env)
        self.assertNotIn("RESULT_JUDGE_ENDPOINT", env)
        self.assertNotIn("EVALUATOR_PORT", env)
        self.assertEqual(env["GATEWAY_API_KEY"], launcher.PLACEHOLDER_KEY)

    def test_safe_text_removes_value_not_just_credential_prefix(self):
        text = launcher._safe_text("GATEWAY_API_KEY=synthetic-secret Authorization: Bearer synthetic-token")
        self.assertNotIn("synthetic-secret", text)
        self.assertNotIn("synthetic-token", text)

    def test_six_world_projections_do_not_reveal_oracle_sequence(self):
        with tempfile.TemporaryDirectory() as temp:
            for case_id in CASE_WORLD_PHASES:
                world = CaseWorld(case_id, Path(temp), Path(temp), {})
                projection = world.next_projection()
                self.assertEqual(set(projection), {"case_id", "current_incident", "notification_sequence"})
                self.assertNotIn("relevant_operations", projection)
                self.assertNotIn("sequence", projection)

    def test_model_trajectory_excludes_private_checks(self):
        event = {"sequence": 1, "operation": "verify", "exit_code": 0, "observation_digest": "a" * 64, "observation": {"exit_code": 0, "receipt_digests": {}}, "evaluator_events": [{"checks": {"private_oracle": True}}]}
        view = launcher.model_visible_trajectory([event])
        self.assertNotIn("private_oracle", json.dumps(view))
        self.assertNotIn("private_oracle", json.dumps(launcher._expected_tool_events([event])))

    def test_docker_mounts_exclude_evaluator_evidence_and_share_probe_state(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            context = {key: "fixture" for key in ("tenant_id", "verification_id", "request_id", "owner_id", "project_id", "capsule_policy", "usage_statement", "budget_policy")}
            context["generation"] = 1
            context.update({key: root / "state" / leaf for key, leaf in (("_ledger_path", "ledger.json"), ("_session_store_path", "sessions"), ("_budget_store_path", "budgets"), ("_attestation_store_path", "attestations"), ("_notification_store_path", "outbox"))})
            output = root / "probes" / "attempt"
            command = launcher.docker_product_command(root / "candidate", output, context, "prepare", "http://broker/v1/responses", "fixture-image", None, workspace=root / "fresh_workspace")
            mounts = [command[index + 1] for index, value in enumerate(command[:-1]) if value == "-v"]
            self.assertIn(str(root / "state") + ":/product-state:rw", mounts)
            self.assertIn(str(root / "fresh_workspace") + ":/product-input:ro", mounts)
            self.assertNotIn(str(output) + ":/case-work:rw", mounts)
            self.assertEqual(command[command.index("--claim-session-store") + 1], "/product-state/sessions")
            self.assertEqual(command[command.index("--claim-output") + 1], "/product-output")

    def test_product_timeout_and_oserror_have_distinct_axes(self):
        with tempfile.TemporaryDirectory() as temp:
            base = dict(candidate_repo=Path(temp), workspace=Path(temp), output_dir=Path(temp) / "out", context={}, endpoint="http://broker/v1/responses", timeout=1, operation="verify", sequence=1, decision={"rationale": "test", "response_text_sha256": "a" * 64}, image=None, dependency_overlay=None)
            for exc, axis in ((subprocess.TimeoutExpired(["product"], 1, output=b"partial"), "candidate"), (PermissionError("launch denied"), "infrastructure")):
                base["output_dir"] = Path(temp) / axis
                with patch.object(launcher, "host_product_command", return_value=["product"]), patch.object(launcher.subprocess, "run", side_effect=exc):
                    event = launcher._run_product_action(**base)
                self.assertEqual(event["observation"]["execution_failure_axis"], axis)
                self.assertLessEqual(event["observation"]["started_ns"], event["observation"]["ended_ns"])

    def test_public_feedback_uses_semantic_score_not_validity_bonus(self):
        with tempfile.TemporaryDirectory() as temp:
            controller = Controller(ROOT, Path(temp), "http://lower.invalid", result_judge_endpoint="http://judge.invalid")
            record = {"submission_number": 1, "candidate_digest": "a" * 64,
                      "dev": {case: {"valid": True, "result_evaluation": {"score": value, "contract_valid": True, "round_consumed": True, "feedback": {"assessment": "fixture semantic assessment"}}} for case, value in (("dev_001", 41), ("dev_002", 59))}}
            with patch.object(controller, "_dev_gate", return_value=(True, [])):
                feedback = controller._write_feedback(record)
            self.assertEqual(feedback["dev_scores"], {"dev_001": 41, "dev_002": 59})
            self.assertEqual(feedback["dev_mean"], 50)
            self.assertFalse(feedback["dev_passed"])
            self.assertEqual(feedback["public_cases"]["dev_001"]["semantic_feedback"]["assessment"], "fixture semantic assessment")

    def test_live_finalizer_preserves_module_global_overrides(self):
        sys.path.insert(0, str(ROOT / "evaluator"))
        from ai_shared_finalize import load_shared, translate_arguments
        with tempfile.TemporaryDirectory() as temp:
            script = Path(temp) / "shared.py"
            script.write_text("ROOT = 'wrong-root'\ndef main(): return ROOT\n")
            shared = load_shared(script)
            self.assertIs(shared.main.__globals__, vars(shared))
            self.assertEqual(shared.main(), ROOT)
        arguments = translate_arguments(["--layout", "ai_scientist", "--code-rubric", "fixed.md", "--run-dir", "/run", "--result-broker-endpoint", "http://judge", "--acceptance-cases", "test_001", "test_006"])
        self.assertNotIn("--layout", arguments)
        self.assertNotIn("--code-rubric", arguments)
        self.assertIn("--result-judge-broker-endpoint", arguments)
        self.assertEqual(arguments[-3:], ["--acceptance-cases", "test_001", "test_006"])

    def test_private_typed_projection_does_not_require_oracle_in_artifact(self):
        sys.path.insert(0, str(ROOT / "evaluator"))
        from semantic_finalize import _trajectory_event_projection
        event = {"sequence": 1, "operation": "verify", "exit_code": 0,
                 "observation_digest": "a" * 64,
                 "observation": {"receipt_digests": {}},
                 "evaluator_events": [{"checks": {"oracle": True}}]}
        self.assertEqual(_trajectory_event_projection([event]), launcher._expected_tool_events([event]))

    @unittest.skipUnless((ROOT / "test_cases/test_001/assets/transaction_context.json").is_file(), "requires real scientific fixture assets")
    def test_healthy_zero_call_broker_does_not_turn_transport_failure_into_candidate_zero(self):
        with tempfile.TemporaryDirectory() as temp:
            candidate = Path(temp) / "candidate"
            (candidate / "ai_scientist").mkdir(parents=True)
            (candidate / "ai_scientist/claim_verification.py").write_text("# fixture product")
            (candidate / "launch_scientist_bfts.py").write_text("# fixture entry")
            healthy = {"runtime": {"calls": 0, "failures": 0, "successful_calls": 0, "budget_exceeded": False}}
            with patch.object(launcher, "broker_stats", return_value=healthy), patch.object(launcher, "ask_model_for_action", side_effect=urllib.error.URLError("local Responses route unavailable")), patch.object(launcher, "_run_product_action") as product:
                result = launcher.run_case(candidate, ROOT / "agentloop/cases/test_001/case_input.json", Path(temp) / "result", "http://invalid-route/v1/responses", 1)
            self.assertEqual(result["classification_axis"], "infrastructure")
            self.assertFalse(result["valid"])
            self.assertEqual(result["broker"]["calls_delta"], 0)
            product.assert_not_called()

    def test_replay_refuses_silent_total_budget_expansion(self):
        sys.path.insert(0, str(ROOT / "evaluator"))
        from immutable_replay import parser, replay
        args = parser().parse_args(["--candidate", "/unused", "--expected-candidate-digest", "a" * 64, "--output", "/unused-out", "--case-wall-timeout", "1800"])
        with self.assertRaisesRegex(ValueError, "600"):
            replay(args)

    def test_cancel_uses_public_semantics_not_private_receipt_phase(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            world = CaseWorld("test_006", root, root, {})
            event = {"exit_code": 0, "observation": {"receipt_projections": {
                "transaction_receipt.json": {"phase": "prepared"},
                "budget_receipt.json": {"request_status": "cancelled"},
            }}}
            with patch.object(world, "_has_durable_phase", return_value=True):
                _mutation, checks, extra = world._evaluate_transition("run_cancelled", event, None, {"protected_state_before": world._protected_state_snapshot()})
            self.assertTrue(all(checks.values()))
            self.assertEqual(extra["transaction_phase_comparison"]["observed_receipt_phase"], "prepared")
            with patch.object(world, "_has_durable_phase", return_value=False):
                _mutation, checks, _extra = world._evaluate_transition("run_cancelled", event, None, {"protected_state_before": world._protected_state_snapshot()})
            self.assertFalse(all(checks.values()))

    def test_stale_rejection_accepts_published_error_shape_but_not_empty_error(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            world = CaseWorld("test_006", root, root, {})
            event = {"exit_code": 2, "observation": {"receipt_projections": {
                "error.json": {"schema_version": 1, "errors": ["no prepared release"]},
            }}}
            with patch.object(world, "_has_durable_phase", return_value=True):
                _mutation, checks, _extra = world._evaluate_transition("stale_commit_rejected", event, None, {"protected_state_before": world._protected_state_snapshot()})
                self.assertTrue(all(checks.values()))
                event["observation"]["receipt_projections"]["error.json"] = {}
                _mutation, checks, _extra = world._evaluate_transition("stale_commit_rejected", event, None, {"protected_state_before": world._protected_state_snapshot()})
                self.assertFalse(all(checks.values()))

    def test_stale_error_cannot_hide_new_settlement_or_damaged_committed_bytes(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            world = CaseWorld("test_006", root, root, {})
            committed = root / "state/sessions/committed/prior/reproducibility_capsule.zip"
            committed.parent.mkdir(parents=True)
            committed.write_bytes(b"prior published release")
            budget = root / "state/budgets/project.json"
            budget.parent.mkdir(parents=True)
            budget.write_text(json.dumps({"project_id": "project", "settled_micros": 10}))
            before = world._protected_state_snapshot()
            event = {"exit_code": 2, "observation": {"receipt_projections": {"error.json": {"errors": ["cancelled"]}}}}
            for damage in ("settlement", "prior-bytes"):
                if damage == "settlement": budget.write_text(json.dumps({"project_id": "project", "settled_micros": 11}))
                else: committed.write_bytes(b"destroyed")
                with patch.object(world, "_has_durable_phase", return_value=True):
                    _mutation, checks, extra = world._evaluate_transition("stale_commit_rejected", event, None, {"protected_state_before": before})
                self.assertFalse(checks["no_new_publication_or_settlement_and_peer_preserved"])
                self.assertFalse(extra["protected_state_comparison"]["equal"])

    def test_owned_cleanup_query_failure_never_claims_absence(self):
        sys.path.insert(0, str(ROOT / "evaluator"))
        from immutable_replay import cleanup_owned_runtime
        with patch("immutable_replay.subprocess.run", return_value=subprocess.CompletedProcess([], 1, "", "daemon unavailable")) as run:
            result = cleanup_owned_runtime("owned-test")
        self.assertFalse(result["all_absent"])
        self.assertEqual(run.call_count, 1)


if __name__ == "__main__": unittest.main()
