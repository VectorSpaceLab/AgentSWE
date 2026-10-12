#!/usr/bin/env python3
"""Provider-free regression checks for the September 5 Codex repair."""
from __future__ import annotations

import hashlib
import importlib.util
import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock


ROOT = Path(__file__).resolve().parents[1]


def load_module(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


runner = load_module("codex_0905_lower", ROOT / "evaluator/harness/run_lower_agent_case.py")
finalizer = load_module("codex_0905_finalizer", ROOT / "evaluator/formal_finalize.py")
one_stop = load_module("codex_0905_one_stop", ROOT / "harbor/formal_one_stop.py")


def stats(*, calls: int = 0, failures: int = 0, upstream_failures: int = 0) -> dict:
    return {
        "runtime": {
            "calls": calls,
            "failures": failures,
            "upstream_failures": upstream_failures,
        }
    }


class RepairContractTests(unittest.TestCase):
    def test_provider_and_candidate_failures_are_separate(self) -> None:
        self.assertEqual(
            runner.execution_classification(
                before=stats(), after=stats(calls=1, failures=1, upstream_failures=1),
                artifact_present=False, artifact_valid=False, process_exit=1,
            ),
            "provider_infrastructure_failure",
        )
        self.assertEqual(
            runner.execution_classification(
                before=stats(), after=stats(calls=1),
                artifact_present=False, artifact_valid=False, process_exit=0,
            ),
            "candidate_contract_failure",
        )
        self.assertEqual(
            runner.execution_classification(
                before=None, after=None,
                artifact_present=True, artifact_valid=True, process_exit=0,
            ),
            "evaluator_infrastructure_failure",
        )
        self.assertEqual(
            runner.execution_classification(
                before=stats(), after=stats(), artifact_present=False, artifact_valid=False,
                process_exit=125, launcher_failure=True,
            ),
            "launcher_infrastructure_failure",
        )
        self.assertEqual(
            runner.execution_classification(
                before=stats(), after=stats(calls=1), artifact_present=False, artifact_valid=False,
                process_exit=124, timed_out=True,
            ),
            "evaluator_infrastructure_failure",
        )

    def test_agent_artifact_provenance_is_case_and_digest_bound(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            run_dir = Path(raw)
            artifact = run_dir / "evaluations/hidden/test_001/agent_artifact.json"
            artifact.parent.mkdir(parents=True)
            artifact.write_text(json.dumps({
                "schema_version": "agentswe-codex-residual-agent-result/v1",
                "case_id": "test_001",
            }), encoding="utf-8")
            digest = hashlib.sha256(artifact.read_bytes()).hexdigest()
            trajectory = run_dir / "evaluations/hidden/test_001/candidate.stdout.jsonl"
            trajectory_text = json.dumps({
                "type": "item.completed",
                "item": {"type": "command_execution", "command": "cat > agent_result.json"},
            }) + "\n"
            trajectory.write_text(trajectory_text, encoding="utf-8")
            trajectory_digest = hashlib.sha256(trajectory.read_bytes()).hexdigest()
            record = {
                "score_kind": "native_evaluator_measurement",
                "formal_result_publishable": False,
                "code_score_publishable": False,
                "agent_authored_artifact": True,
                "artifact_sha256": digest,
                "artifact_provenance": {
                    "source_path": "workspace/agent_result.json",
                    "evaluator_synthesized": False,
                    "case_id": "test_001",
                    "sha256": digest,
                    "preexisting_before_launch": False,
                    "broker_successful_calls": 1,
                    "trajectory_artifact_reference": True,
                    "trajectory_artifact_write_event": True,
                    "trajectory_digest": "trajectory-digest",
                },
                "trajectory_digest": trajectory_digest,
            }
            record["artifact_provenance"]["trajectory_digest"] = trajectory_digest
            self.assertIsNone(finalizer.artifact_provenance_error(run_dir, "test_001", record))
            artifact.write_text(artifact.read_text(encoding="utf-8") + "\n", encoding="utf-8")
            self.assertIn("digest", finalizer.artifact_provenance_error(run_dir, "test_001", record) or "")

    def test_trajectory_write_proof_rejects_assistant_text_only(self) -> None:
        assistant_text = json.dumps({
            "type": "item.completed",
            "item": {"type": "agent_message", "text": "I would write agent_result.json with cat > agent_result.json"},
        })
        with tempfile.TemporaryDirectory() as raw:
            case_home = Path(raw) / "codex-home"
            case_home.mkdir()
            self.assertFalse(runner.trajectory_writes_artifact(case_home, assistant_text))

    def test_missing_artifact_never_reaches_shared_result_judge(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            with self.assertRaisesRegex(ValueError, "agent-authored artifact"):
                finalizer.judge_case(Path(raw), "test_001", {}, "http://127.0.0.1:1/v1/responses")

    def test_judges_are_fixed_independent_entries(self) -> None:
        self.assertEqual(
            finalizer.RESULT_JUDGE,
            Path("@@AGENTSWE_EDITING_CONTROL@@/result_judge.py"),
        )
        self.assertEqual(
            finalizer.CODE_JUDGE,
            Path("@@AGENTSWE_EDITING_CONTROL@@/code_judge_runner.py"),
        )
        self.assertEqual(
            finalizer.CREATE_CODE_JUDGE,
            Path("@@AGENTSWE_LEGACY_HARBOR@@/0825-10create-v4/code_eval.py"),
        )
        self.assertEqual(
            finalizer.ROOT / "evaluator/code_rubric.md",
            ROOT / "evaluator/code_rubric.md",
        )

    def test_checked_in_protocol_lock_declares_generalized_lifecycle(self) -> None:
        lock = json.loads((ROOT / "protocol_lock.json").read_text(encoding="utf-8"))
        self.assertEqual(lock["model"], "deepseek-flash")
        self.assertEqual(lock["reasoning_effort"], "high")
        self.assertEqual(lock["candidate_rounds"], "up_to_10")
        self.assertEqual(lock["required_dev_cases_per_round"], ["dev_001", "dev_002"])
        self.assertEqual(lock["hidden_cases_after_freeze"], [f"test_{i:03d}" for i in range(1, 7)])
        self.assertTrue(lock["executed_inventories_derived_from_records"])

    def test_hidden_finalization_requires_measured_freeze_stability(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            run_dir = Path(raw)
            write = lambda name, value: (run_dir / name).write_text(json.dumps(value), encoding="utf-8")
            write("freeze_manifest.json", {"candidate_digest": "abc", "path": str(run_dir)})
            write("dev_lifecycle.json", [{"state": "completed"}])
            write("hidden_attestation.json", {
                "case_inventory": [f"test_{i:03d}" for i in range(1, 7)],
                "executed_cases": [f"test_{i:03d}" for i in range(1, 7)],
                "pilot_not_formal": False,
                "frozen_digest_stable": True,
                "frozen_digest_before": "wrong",
                "frozen_digest_after": "wrong",
            })
            code, value = finalizer.finalize(run_dir, Path("/nonexistent"), "http://127.0.0.1:1/v1/responses")
            self.assertEqual(code, 2)
            self.assertIn("frozen-source stability", value["reasons"][0])

    def test_run_dir_refuses_nonempty_and_cleanup_skips_unstarted(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            run_dir = Path(raw) / "run"
            run_dir.mkdir()
            (run_dir / "keep.txt").write_text("do not delete", encoding="utf-8")
            with self.assertRaisesRegex(RuntimeError, "new and empty"):
                one_stop.prepare_run_dir(run_dir)
            self.assertEqual((run_dir / "keep.txt").read_text(encoding="utf-8"), "do not delete")
            receipt = one_stop.cleanup_broker(
                role="never-started", name="must-not-touch-docker", port=1,
                attempted=False, run_dir=run_dir, cidfile=run_dir / "never.cid",
            )
            self.assertTrue(receipt["absent_after_cleanup"])
            self.assertIsNone(receipt["remove_exit_code"])

    def test_attempted_startup_has_provider_free_cleanup_receipt(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            run_dir = Path(raw)
            with mock.patch.object(
                one_stop.urllib.request, "urlopen", side_effect=OSError("broker unavailable")
            ), mock.patch.object(
                one_stop.subprocess,
                "run",
                side_effect=[mock.Mock(returncode=0), mock.Mock(returncode=1, stdout="", stderr="Error: No such object: current-id")],
            ) as run:
                cidfile = run_dir / "current.cid"
                cidfile.write_text("current-id\n", encoding="ascii")
                receipt = one_stop.cleanup_broker(
                    role="candidate", name="failed-start", port=9,
                    attempted=True, run_dir=run_dir, cidfile=cidfile,
                )
            self.assertEqual(run.call_count, 2)
            self.assertTrue(receipt["startup_attempted"])
            self.assertFalse(receipt["stats_saved"])
            self.assertTrue(receipt["absent_after_cleanup"])
            self.assertEqual(receipt["remove_exit_code"], 0)
            self.assertTrue((run_dir / "candidate_broker_stats_error.json").is_file())

    def test_pilot_is_reduced_and_never_formal_publishable(self) -> None:
        result = one_stop.pilot_self_test(ROOT)
        self.assertEqual(result["status"], "PASS")
        self.assertTrue(result["pilot_not_formal"])
        self.assertEqual(result["public_cases"], ["dev_001", "dev_002"])
        self.assertEqual(result["hidden_cases"], ["test_001", "test_002"])
        self.assertFalse(result["formal_execution_started"])
        self.assertFalse(result["formal_result_claimed"])
        self.assertEqual(result["network_calls"], 0)
        self.assertFalse(result["docker_started"])

    # Isolate the mean calculation; actual native identity has a separate
    # Harbor/CLI integration diagnostic and is never claimed by this unit test.
    @mock.patch.object(one_stop.DevController, "bind_native_thread", lambda self: None)
    def test_single_case_controller_uses_single_case_mean(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            run_dir = Path(raw)
            workspace = run_dir / "submission"
            workspace.mkdir()
            (workspace / "solution.patch").write_text("diff --git a/a b/a\n", encoding="utf-8")
            (workspace / "edit_report.json").write_text("{}", encoding="utf-8")
            (workspace / "run_report.json").write_text("{}", encoding="utf-8")
            controller = one_stop.DevController(
                run_dir=run_dir, workspace=workspace,
                evaluate=lambda _number, _snapshot: ({"dev_001": {"case_id": "dev_001", "score": 80}}, None),
                max_dev_rounds=2, public_cases=("dev_001",),
            )
            controller.start()
            try:
                code, record = controller.submit()
                self.assertEqual(code, 202)
                controller.wait_idle()
                self.assertEqual(controller.records[0]["dev_score"], 80.0)
                self.assertTrue(controller.records[0]["dev_passed"])
                self.assertIn("public-case mean: 80.0/100", controller.records[0]["feedback_text"])
            finally:
                controller.stop()

    def test_compose_subnet_selection_avoids_blocked_ranges(self) -> None:
        selected = one_stop.select_compose_subnet(
            run_dir=Path("/tmp/agentswe-subnet-test"),
            existing_subnets=["198.18.0.0/24", "10.0.0.0/8"],
            host_subnets=["198.19.0.0/24"],
        )
        candidate = one_stop.ipaddress.ip_network(selected)
        self.assertFalse(candidate.overlaps(one_stop.ipaddress.ip_network("198.18.0.0/24")))
        self.assertFalse(candidate.overlaps(one_stop.ipaddress.ip_network("198.19.0.0/24")))
        self.assertFalse(candidate.overlaps(one_stop.ipaddress.ip_network("10.0.0.0/8")))

    def test_pilot_requires_proven_public_cleanup_before_hidden_phase(self) -> None:
        source = (ROOT / "harbor/formal_one_stop.py").read_text(encoding="utf-8")
        self.assertIn('pilot hidden phase requires proven cleanup of the public lower broker', source)
        self.assertIn('public_cleanup.get("absent_after_cleanup") is not True', source)


if __name__ == "__main__":
    unittest.main()
