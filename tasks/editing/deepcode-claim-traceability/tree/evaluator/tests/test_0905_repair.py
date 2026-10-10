from __future__ import annotations

import importlib.util
import json
import subprocess
import sys
import tempfile
import unittest
from types import SimpleNamespace
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
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


class Repair0905Tests(unittest.TestCase):
    def test_protocol_lock_matches_create_aligned_ten_round_lifecycle(self) -> None:
        lock = json.loads((ROOT / "protocol_lock.json").read_text(encoding="utf-8"))
        self.assertNotIn("rounds", lock["builder"])
        self.assertEqual(lock["lifecycle"]["candidate_rounds"], "up_to_10")
        self.assertEqual(lock["lifecycle"]["max_dev_rounds"], 10)
        self.assertEqual(lock["lifecycle"]["feedback_revisions"], "fresh_after_each_acceptance")
        self.assertFalse(lock["lifecycle"]["dev_passed_is_automatic_freeze"])

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
        self.assertIsNotNone(axes.infrastructure_reason({
            "classification": "broker_infrastructure_error", "infra_valid": False,
            "broker_delta": {"successful_calls": 1, "delivery_failures": 1},
        }))

    def test_result_and_code_axes_are_not_short_circuited_together(self) -> None:
        source = Path("@@AGENTSWE_EDITING_CONTROL@@/formal_axes_shared.py").read_text(encoding="utf-8")
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
                "provider_usage": {"logical_requests": 1, "completed_responses": 1, "input_tokens": 12, "output_tokens": 8, "total_tokens": 20, "transport_attempts": 1},
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

    def test_formal_entrypoint_prepares_dependency_complete_runtime(self) -> None:
        source = (ROOT / "harbor/formal_one_stop.py").read_text(encoding="utf-8")
        self.assertIn("prepare_task_environment.py", source)
        self.assertIn("deepcode_runtime", source)
        self.assertIn("loguru", source)
        self.assertIn('args.runtime_python = ensure_runtime_python', source)

    def test_builder_task_mounts_writable_git_backed_product_worktree(self) -> None:
        sys.path.insert(0, str(ROOT / "harbor"))
        one_stop = load("repair_one_stop_mount", ROOT / "harbor/formal_one_stop.py")
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            public = root / "public"
            repository = public / "input" / "repository"
            repository.mkdir(parents=True)
            (repository / "deepcode.py").write_text("baseline\n", encoding="utf-8")
            workspace = root / "submission"
            workspace.mkdir()
            worktree = one_stop.prepare_builder_worktree(public, root / "worktree")
            self.assertTrue((worktree / ".git").is_dir())
            provider = root / "builder_provider.toml"
            provider.write_text("provider = 'test'\n", encoding="utf-8")
            lifecycle = SimpleNamespace(
                socket_path=root / "controller.sock",
                max_dev_rounds=10,
                token="test-controller-token",
            )
            task = one_stop.stage_builder_task(
                run_dir=root / "run",
                public=public,
                workspace=workspace,
                builder_worktree=worktree,
                lifecycle=lifecycle,
                provider=provider,
                image="test-image",
                pilot=True,
            )
            task_dir = task.parent / "builder_task"
            compose = json.loads((task_dir / "environment" / "docker-compose.yaml").read_text(encoding="utf-8"))
            mounts = compose["services"]["main"]["volumes"]
            self.assertIn(
                {"type": "bind", "source": str(worktree), "target": "/workspace/worktree"},
                mounts,
            )
            instruction = (task_dir / "instruction.md").read_text(encoding="utf-8")
            self.assertIn("safe.directory=/workspace/worktree", instruction)
            self.assertIn("do not change global Git configuration", instruction)

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
