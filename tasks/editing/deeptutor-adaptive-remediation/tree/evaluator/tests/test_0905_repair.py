from __future__ import annotations

import asyncio
import importlib.util
import json
import subprocess
import sys
import tempfile
import unittest
from unittest import mock
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
    spec.loader.exec_module(module)
    return module


class Repair0905Tests(unittest.TestCase):
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
        self.assertIsNone(axes.infrastructure_reason({
            "classification": "candidate_valid",
            "broker_delta": {"successful_calls": 1, "delivery_failures": 1},
        }))  # recovered aggregate delivery counters alone are not causal
        self.assertIsNotNone(axes.infrastructure_reason({
            "classification":"candidate_valid",
            "infrastructure_events":[{"terminal":True,"recovered":False}],
        }))

    def test_result_and_code_axes_are_not_short_circuited_together(self) -> None:
        source = (ROOT / "evaluator/formal_axes.py").read_text(encoding="utf-8")
        if "formal_axes_shared.py" in source:
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
                "provider_usage": {
                    "logical_requests": 1,
                    "completed_responses": 1,
                    "input_tokens": 10,
                    "output_tokens": 10,
                    "total_tokens": 20,
                    "transport_attempts": 1,
                },
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

    def test_public_prompts_do_not_embed_credentials_or_score_weights(self) -> None:
        for group in ("dev_cases", "test_cases"):
            for path in (ROOT / group).glob("*/input.md"):
                text = path.read_text(encoding="utf-8", errors="replace")
                self.assertNotIn("GATEWAY_API_KEY=", text)
                self.assertNotIn("OPENAI_API_KEY=", text)
                self.assertNotIn("private oracle", text.lower())
                self.assertNotIn("score weights", text.lower())

    def test_product_artifact_boundary_is_explicit_and_strict(self) -> None:
        source = (ROOT / "agentloop/lower_agent_entry.py").read_text(encoding="utf-8")
        self.assertIn("DEEPTUTOR_AGENT_RESULT", source)
        self.assertIn("lower_agent_product", source)
        self.assertIn("Required terminal artifact protocol", source)
        from agentloop.lower_agent_entry import command
        from agentloop.run_hidden import _artifact_valid

        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            prompt = root / "input.md"
            prompt.write_text("complete the task", encoding="utf-8")
            command_text = command(root / "repository", prompt, root / "test_001", sys.executable)
            self.assertIn("schema_version", command_text[5])
            self.assertIn("'test_001'", command_text[5])
            artifact = root / "test_001" / "agent_result.json"
            artifact.parent.mkdir()
            artifact.write_text(json.dumps({"schema_version": "v1"}), encoding="utf-8")
            self.assertFalse(_artifact_valid(artifact.parent, "test_001"))
            artifact.write_text(json.dumps({
                "schema_version": "v1", "case_id": "test_001", "status": "complete",
                "summary": "observed", "artifacts": {},
            }), encoding="utf-8")
            self.assertTrue(_artifact_valid(artifact.parent, "test_001"))
            self.assertFalse(_artifact_valid(artifact.parent, "test_002"))

    def test_builder_and_hidden_prompts_require_product_authored_artifact(self) -> None:
        builder = (ROOT / "harbor/formal_one_stop.py").read_text(encoding="utf-8")
        self.assertIn("DEEPTUTOR_AGENT_RESULT", builder)
        self.assertIn("common agent-loop final-result seam", builder)
        self.assertIn("Do not create a default object", builder)
        for field in ("schema_version", "case_id", "status", "summary", "artifacts"):
            self.assertIn(f'"{field}"', builder)
        for path in (ROOT / "dev_cases").glob("*/input.md"):
            text = path.read_text(encoding="utf-8")
            self.assertIn("schema_version", text)
            self.assertIn("artifacts", text)

    def test_public_mastery_tool_contract_is_complete_and_builder_visible(self) -> None:
        contract = (ROOT / "input/05_mastery_tool_contract.md").read_text(encoding="utf-8")
        required = (
            "mastery_remediation_status",
            "mastery_remediation_claim",
            "mastery_review_plan",
            "mastery_learning_events",
            "mastery_session_handoff",
            "mastery_policy_publish",
            "mastery_learner_snapshot",
            "mastery_learner_snapshot_witness",
            "mastery_learner_snapshot_verify",
            "mastery_learner_snapshot_chain_audit",
        )
        for name in required:
            self.assertIn(name, contract)
        builder = (ROOT / "harbor/formal_one_stop.py").read_text(encoding="utf-8")
        self.assertIn("complete public mastery-tool", builder)
        self.assertIn("contract in `/builder-package/input/05_mastery_tool_contract.md`", builder)
        self.assertIn("original five mastery tools alone are an incomplete Candidate", builder)
        self.assertIn("dependency-complete lower runtime", builder)
        self.assertIn("optional product import such as `yaml`", builder)
        self.assertIn("diff --git a/path b/path", builder)
        self.assertIn("do not use `diff -ruN` headers", builder)
        self.assertIn("parser-compatible path check", builder)
        self.assertIn("path == '/dev/null' or path.startswith(('a/', 'b/'))", builder)
        self.assertIn('"--stats-file", f"/evidence/{self.role}_broker_stats.json"', builder)
        self.assertIn('["git", "init", "-q"]', builder)
        self.assertIn("git -C /workspace/worktree add -N .", builder)
        self.assertIn('"GIT_CONFIG_KEY_0": "safe.directory"', builder)
        self.assertIn('"GIT_CONFIG_VALUE_0": "/workspace/worktree"', builder)
        self.assertIn("bounded first-submission source map", builder)
        self.assertIn("at most eight read-only shell/tool actions", builder)
        self.assertIn("under 200 output lines and 12,000 characters", builder)
        self.assertIn("Do not run the full DeepTutor agent loop", builder)
        self.assertIn("deeptutor/services/file_io.py", builder)
        self.assertIn("effective_max_dev_rounds = 2 if args.pilot else args.max_dev_rounds", builder)
        self.assertIn('"max_accepted_submissions": 2', builder)
        for path in (ROOT / "test_cases").glob("*/input.md"):
            text = path.read_text(encoding="utf-8")
            self.assertIn("runtime `case_id`", text)
            self.assertIn("artifacts", text)

    def test_public_tool_surface_audit_follows_split_product_registry(self) -> None:
        from agentloop import lower_agent_entry as entry

        with tempfile.TemporaryDirectory() as raw:
            repository = Path(raw)
            mastery = repository / "deeptutor/capabilities/mastery"
            mastery.mkdir(parents=True)
            (mastery / "tools.py").write_text(
                "from .extended_tools import EXTENDED_MASTERY_TOOL_NAMES\n"
                "MASTERY_TOOL_NAMES = ('mastery_status',) + EXTENDED_MASTERY_TOOL_NAMES\n",
                encoding="utf-8",
            )
            extended_names = ",\n".join(
                f"    {name!r}" for name in entry.PUBLIC_REQUIRED_TOOLS if name != "mastery_status"
            )
            (mastery / "extended_tools.py").write_text(
                "EXTENDED_MASTERY_TOOL_NAMES = (\n" + extended_names + "\n)\n",
                encoding="utf-8",
            )
            audit = entry._audit_public_tool_surface(repository, repository / "evidence")
            self.assertFalse(audit["capability_gap"])
            self.assertIn(str(mastery / "extended_tools.py"), audit["source_files"])

    def test_candidate_delivery_contains_product_writer_and_applies_cleanly(self) -> None:
        patch = (ROOT / "delivery/solution.patch").read_text(encoding="utf-8")
        self.assertIn("diff --git a/deeptutor/agents/chat/agent_loop.py", patch)
        self.assertIn("_persist_model_artifact", patch)
        self.assertIn("DEEPTUTOR_AGENT_RESULT", patch)
        self.assertIn("os.replace(temporary_name, destination)", patch)
        self.assertIn("Do not create a default object", (ROOT / "harbor/formal_one_stop.py").read_text(encoding="utf-8"))

    def test_auxiliary_service_stream_uses_locked_responses_adapter(self) -> None:
        from agentloop import responses_client_adapter as adapter
        captured: dict[str, object] = {}

        class FakeAdapter:
            def __init__(self, endpoint: str, token: str) -> None:
                captured["endpoint"] = endpoint
                captured["token"] = token

            async def create(self, **kwargs: object):
                captured["request"] = kwargs
                return adapter.StaticResponseStream([
                    adapter._chunk(content="Grounded remediation"),
                    adapter._chunk(finish_reason="stop"),
                ])

        original = adapter.ResponsesChatAdapter
        adapter.ResponsesChatAdapter = FakeAdapter
        try:
            async def collect() -> list[str]:
                return [
                    item
                    async for item in adapter.responses_text_stream(
                        endpoint="http://127.0.0.1:1234/v1/responses",
                        token="broker-only-placeholder",
                        prompt="title this",
                        system_prompt="short title",
                        max_tokens=80,
                    )
                ]

            self.assertEqual(asyncio.run(collect()), ["Grounded remediation"])
        finally:
            adapter.ResponsesChatAdapter = original

        self.assertEqual(captured["endpoint"], "http://127.0.0.1:1234/v1/responses")
        self.assertEqual(captured["token"], "broker-only-placeholder")
        request = captured["request"]
        self.assertIsInstance(request, dict)
        self.assertEqual(request["max_tokens"], 80)
        self.assertEqual(
            request["messages"],
            [
                {"role": "system", "content": "short title"},
                {"role": "user", "content": "title this"},
            ],
        )

        sitecustomize = (ROOT / "agentloop/sitecustomize.py").read_text(encoding="utf-8")
        self.assertIn("llm_module.stream = evaluator_responses_stream", sitecustomize)
        self.assertIn("llm_factory.stream = evaluator_responses_stream", sitecustomize)
        self.assertIn('"services_stream_transport": "responses"', sitecustomize)

    def test_unknown_execution_is_not_replayed_but_new_product_can_be_evaluated(self) -> None:
        harbor_path = str(ROOT / "harbor")
        sys.path.insert(0, harbor_path)
        try:
            from harbor import formal_one_stop
        finally:
            sys.path.remove(harbor_path)

        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            source = root / "source"
            source.mkdir()
            workspace = root / "submission"
            workspace.mkdir()
            (workspace / "solution.patch").write_text("patch", encoding="utf-8")
            lifecycle = formal_one_stop.BuilderLifecycle(
                run_dir=root / "run",
                workspace=workspace,
                source=source,
                public_broker="http://127.0.0.1:1/v1/responses",
                runtime_python=sys.executable,
                dev_cases=("dev_001",),
                max_dev_rounds=2,
                pilot_not_formal=True,
            )
            public_outputs: list[Path] = []

            def fake_materialize(_source, _submission, build_dir, **_kwargs):
                repository = build_dir / "repository"
                repository.mkdir(parents=True)
                (repository / "candidate.txt").write_text((_submission / "solution.patch").read_text(), encoding="utf-8")
                return {
                    "valid": True,
                    "classification": "candidate_ready",
                    "repository": str(repository),
                }

            def fake_public_case(*, output: Path, **_kwargs):
                output.mkdir(parents=True, exist_ok=False)
                public_outputs.append(output)
                if len(public_outputs) == 1:
                    return {
                        "case_id": "dev_001",
                        "terminal": True,
                        "classification": "broker_infrastructure_error",
                        "infra_valid": False,
                        "score": None,
                    }
                return {
                    "case_id": "dev_001",
                    "terminal": True,
                    "classification": "candidate_valid",
                    "infra_valid": True,
                    "score": 100,
                }

            with mock.patch.object(
                formal_one_stop, "materialize_candidate", side_effect=fake_materialize
            ), mock.patch.object(
                formal_one_stop, "run_public_case", side_effect=fake_public_case
            ), mock.patch.object(lifecycle, 'bind_native_thread'), mock.patch.object(formal_one_stop, 'validate_delivery', return_value=[]):
                # This unit isolates replay admission; a separate real native
                # integration exercises the session and delivery boundaries.
                first_code, first = lifecycle.submit("")
                second_code, second = lifecycle.submit("")
                (workspace / "solution.patch").write_text("new product", encoding="utf-8")
                third_code, third = lifecycle.submit("")

            self.assertEqual(first_code, 503)
            self.assertEqual(first["state"], "infrastructure_attempt_not_consumed")
            self.assertEqual(second_code, 503)
            self.assertEqual(second["state"], "infrastructure_attempt_not_consumed")
            self.assertEqual(third_code, 200)
            self.assertEqual(third["state"], "accepted")
            self.assertEqual(len(lifecycle.controller.records), 1)
            self.assertEqual(len(public_outputs), 2)
            self.assertNotEqual(public_outputs[0], public_outputs[1])
            self.assertIn("candidate_001_attempt_001", str(public_outputs[0]))
            self.assertIn("candidate_001_attempt_003", str(public_outputs[1]))


if __name__ == "__main__":
    unittest.main()
