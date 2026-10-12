#!/usr/bin/env python3
"""Provider-free regressions for the real Aider lower lifecycle boundary."""
from __future__ import annotations

import ast
import hashlib
import importlib.util
import json
import subprocess
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]


def load_module(path: Path, name: str):
    spec = importlib.util.spec_from_file_location(name, path)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"unable to load {path}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class AiderRuntimeContractTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.lower = load_module(ROOT / "evaluator/harness/run_lower_agent_case.py", "aider_lower_contract")
        cls.finalizer = load_module(ROOT / "evaluator/formal_finalize.py", "aider_finalizer_contract")

    def test_post_run_state_is_loaded_before_result_is_constructed(self) -> None:
        path = ROOT / "evaluator/harness/run_lower_agent_case.py"
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        main = next(node for node in tree.body if isinstance(node, ast.FunctionDef) and node.name == "main")
        state_assignments = [
            node.lineno
            for node in ast.walk(main)
            if isinstance(node, ast.Assign)
            and any(isinstance(target, ast.Name) and target.id == "runtime_state" for target in node.targets)
        ]
        result_uses = [
            node.lineno
            for node in ast.walk(main)
            if isinstance(node, ast.Name) and isinstance(node.ctx, ast.Load) and node.id == "runtime_state"
        ]
        self.assertTrue(state_assignments, "main must load evaluator state after the lower exits")
        self.assertTrue(result_uses, "result/evidence materialization must use the loaded state")
        self.assertLess(min(state_assignments), max(result_uses))

    def test_formal_run_rejects_non_empty_directory_without_deleting_it(self) -> None:
        module = load_module(ROOT / "harbor/formal_one_stop.py", "aider_formal_one_stop_contract")
        with tempfile.TemporaryDirectory(prefix="aider-run-dir-contract-") as temporary:
            run_dir = Path(temporary) / "run"
            run_dir.mkdir()
            sentinel = run_dir / "sentinel.txt"
            sentinel.write_text("preserve me\n", encoding="utf-8")
            args = type("Args", (), {})()
            with self.assertRaises(RuntimeError):
                module._run_formal(args, run_dir)
            self.assertEqual(sentinel.read_text(encoding="utf-8"), "preserve me\n")

    def test_formal_finalizer_names_fixed_create_code_judge(self) -> None:
        finalizer = load_module(ROOT / "evaluator/formal_finalize.py", "aider_code_judge_contract")
        self.assertEqual(
            finalizer.CREATE_CODE_JUDGE,
            Path("@@AGENTSWE_LEGACY_HARBOR@@/0825-10create-v4/code_eval.py"),
        )

    def test_healthy_zero_call_is_candidate_behavior_failure(self) -> None:
        self.assertEqual(
            self.lower.classify_lower_result(
                broker_stats_invalid=False, calls=0, failures=0,
                state_error=None, process_exit=0, answer={},
            ),
            "candidate_behavior_failure",
        )

    def test_launcher_failure_is_infrastructure(self) -> None:
        self.assertEqual(
            self.lower.classify_lower_result(
                broker_stats_invalid=False, calls=0, failures=0,
                state_error=None, process_exit=125, answer={},
            ),
            "launcher_infrastructure_failure",
        )

    def test_lower_command_explicitly_enables_shell_and_isolates_client_mount(self) -> None:
        path = ROOT / "evaluator/harness/run_lower_agent_case.py"
        source = path.read_text(encoding="utf-8")
        self.assertIn('"--suggest-shell-commands"', source)
        self.assertIn("input_text=AUTO_CONFIRM_INPUT", source)
        self.assertIn('"--restore-chat-history"', source)
        self.assertIn("LOWER_TURN_LIMIT = 12", source)
        self.assertIn("restored_chat_history_continuations", source)
        # Aider maps --yes-always to a hard-coded "no" for prompts marked
        # explicit_yes_required (shell commands).  The evaluator supplies
        # affirmative stdin instead, so the product confirmation boundary is
        # crossed without changing the model-selected command.
        self.assertNotIn('"--yes-always"', source)
        self.assertIn("model_selected_commands_unchanged", source)
        self.assertIn(":{CLIENT_CONTAINER_PATH}:ro", source)
        self.assertIn('CLIENT_CONTAINER_PATH = "/case-client/run_case"', source)
        self.assertNotIn(":/case-work/run_case:ro", source)

    def test_lower_prompt_requires_real_action_execution_and_exact_artifact_filename(self) -> None:
        path = ROOT / "evaluator/harness/run_lower_agent_case.py"
        source = path.read_text(encoding="utf-8")
        self.assertIn("first operational action MUST be an actual shell execution", source)
        self.assertIn("python3 {CLIENT_CONTAINER_PATH} inspect", source)
        self.assertIn("agent_result.json` alone on the line immediately before the opening fence", source)
        self.assertIn('ARTIFACT_CONTAINER_PATH = "/case-work/repo/agent_result.json"', source)
        self.assertIn("Do not author or mention `agent_result.json` in this initial response", source)
        self.assertIn("do not also write the final artifact in a", source)
        self.assertIn("not create or overwrite this artifact through `/run`", source)

    def test_lower_credentials_remain_placeholder_only(self) -> None:
        path = ROOT / "evaluator/harness/run_lower_agent_case.py"
        source = path.read_text(encoding="utf-8")
        self.assertIn('"-e","OPENAI_API_KEY=broker-only-placeholder"', source)
        self.assertIn('"credential":"placeholder-only"', source)

    def test_action_client_stages_adapter_files_on_writable_tmpfs(self) -> None:
        path = ROOT / "evaluator/case_runtime.py"
        source = path.read_text(encoding="utf-8")
        self.assertIn("TemporaryDirectory(dir='/tmp')", source)
        self.assertNotIn("TemporaryDirectory(dir='/workspace')", source)

    def test_builder_instruction_requires_flat_zero_provider_counts(self) -> None:
        path = ROOT / "harbor/formal_one_stop.py"
        source = path.read_text(encoding="utf-8")
        self.assertIn("`deepseek`, `gateway`, `gateway_image`, `serper`,", source)
        self.assertIn("Do not put those five counters inside a nested", source)
        self.assertIn("`tool_counts` object", source)

    def test_builder_instruction_bounds_unavailable_local_fixture_dependencies(self) -> None:
        path = ROOT / "harbor/formal_one_stop.py"
        source = path.read_text(encoding="utf-8")
        self.assertIn("evaluator-only Python environment", source)
        self.assertIn("do not install packages, create import shims", source)
        self.assertIn("bounded, focused test", source)
        self.assertIn("one final check", source)

    def test_aider_artifact_authorship_requires_structured_edit_evidence(self) -> None:
        launcher = (ROOT / "evaluator/harness/run_lower_agent_case.py").read_text(encoding="utf-8")
        finalizer = (ROOT / "evaluator/formal_finalize.py").read_text(encoding="utf-8")
        self.assertIn("structured_artifact_write_evidence", launcher)
        self.assertIn('"aider_search_replace_block"', launcher)
        self.assertIn("structured Aider edit evidence is missing", finalizer)

    def test_filename_mention_does_not_count_as_aider_artifact_authorship(self) -> None:
        source = load_module(ROOT / "evaluator/harness/run_lower_agent_case.py", "aider_artifact_evidence_contract")
        with tempfile.TemporaryDirectory(prefix="aider-artifact-evidence-contract-") as temporary:
            artifact = Path(temporary) / "agent_result.json"
            artifact.write_text("{}\n", encoding="utf-8")
            valid_chat = ("```bash\npython3 /case-client/run_case inspect\n```agent_result.json\n"
                          "```json\n<<<<<<< SEARCH\n=======\n{}\n>>>>>>> REPLACE\n```\n"
                          "> Applied edit to agent_result.json\n")
            invalid_chat = "I will write /case-work/repo/agent_result.json later.\n"
            valid = source.structured_artifact_write_evidence(
                artifact, valid_chat, "", preexisting_before_launch=False, lower_started_epoch_ns=0,
            )
            invalid = source.structured_artifact_write_evidence(
                artifact, invalid_chat, "", preexisting_before_launch=False, lower_started_epoch_ns=0,
            )
            self.assertTrue(valid["accepted"])
            self.assertTrue(valid["aider_search_replace_block"])
            self.assertTrue(valid["replacement_matches_artifact"])
            self.assertTrue(valid["applied_edit_confirmation"])
            self.assertFalse(invalid["accepted"])

    def test_absolute_aider_edit_proves_authorship_but_violates_relative_contract(self) -> None:
        source = load_module(ROOT / "evaluator/harness/run_lower_agent_case.py", "aider_absolute_artifact_contract")
        with tempfile.TemporaryDirectory(prefix="aider-absolute-artifact-contract-") as temporary:
            artifact = Path(temporary) / "agent_result.json"
            artifact.write_text('{"case_id":"dev_002"}\n', encoding="utf-8")
            chat = ("/case-work/repo/agent_result.json\n```json\n<<<<<<< SEARCH\n=======\n"
                    '{"case_id":"dev_002"}\n>>>>>>> REPLACE\n```\n'
                    "> Applied edit to /case-work/repo/agent_result.json\n")
            evidence = source.structured_artifact_write_evidence(
                artifact, chat, "+/run python3 /case-client/run_case inspect\n",
                preexisting_before_launch=False, lower_started_epoch_ns=0,
            )
            self.assertTrue(evidence["authorship_proven"])
            self.assertFalse(evidence["exact_relative_filename"])
            self.assertFalse(evidence["accepted"])
            self.assertTrue(evidence["executed_shell_history_present"])

    def test_artifact_failure_is_candidate_only_after_infrastructure_is_healthy(self) -> None:
        source = load_module(ROOT / "evaluator/harness/run_lower_agent_case.py", "aider_artifact_classification_contract")
        base = dict(broker_stats_invalid=False, calls=2, failures=0, state_error=None,
                    process_exit=0, answer={"case_id": "dev_001"},
                    transport_invalid=False, artifact_contract_valid=False)
        self.assertEqual(source.classify_lower_result(**base), "candidate_artifact_failure")
        for override in ({"transport_invalid": True}, {"failures": 1},
                         {"state_error": "missing runtime"}, {"process_exit": 124}):
            value = dict(base)
            value.update(override)
            self.assertIn("infrastructure", source.classify_lower_result(**value))

    def test_cumulative_applied_edits_prove_authorship(self) -> None:
        """0920-fh-003 test_005: a whole-file create plus Aider's own lint fix.

        Aider's auto-lint asked the model to remove a trailing comma; the
        follow-up SEARCH/REPLACE is a fragment, so no single block equals the
        final file.  Replaying the confirmed blocks does.
        """
        source = load_module(ROOT / "evaluator/harness/run_lower_agent_case.py",
                             "aider_cumulative_authorship_contract")
        with tempfile.TemporaryDirectory(prefix="aider-cumulative-authorship-") as temporary:
            artifact = Path(temporary) / "agent_result.json"
            final = '{\n  "case_id": "test_005",\n  "decision": "keep"\n}\n'
            artifact.write_text(final, encoding="utf-8")
            chat = (
                "agent_result.json\n```json\n<<<<<<< SEARCH\n=======\n"
                '{\n  "case_id": "test_005",\n  "decision": "keep",\n}\n'
                ">>>>>>> REPLACE\n```\n> Applied edit to agent_result.json\n\n"
                "agent_result.json\n```json\n<<<<<<< SEARCH\n"
                '  "decision": "keep",\n=======\n  "decision": "keep"\n'
                ">>>>>>> REPLACE\n```\n> Applied edit to agent_result.json\n")
            evidence = source.structured_artifact_write_evidence(
                artifact, chat, "+/run python3 /case-client/run_case inspect\n",
                preexisting_before_launch=False, lower_started_epoch_ns=0, case_id="test_005")
            self.assertEqual(evidence["applied_edit_blocks_replayed"], 2)
            self.assertFalse(evidence["whole_file_block_matches_artifact"])
            self.assertTrue(evidence["replacement_matches_artifact"])
            self.assertTrue(evidence["artifact_case_bound"])
            self.assertTrue(evidence["accepted"])

    def test_unapplied_edit_and_wrong_case_never_prove_authorship(self) -> None:
        source = load_module(ROOT / "evaluator/harness/run_lower_agent_case.py",
                             "aider_authorship_negative_contract")
        with tempfile.TemporaryDirectory(prefix="aider-authorship-negative-") as temporary:
            artifact = Path(temporary) / "agent_result.json"
            artifact.write_text('{"case_id": "test_001"}\n', encoding="utf-8")
            unapplied = ("agent_result.json\n```json\n<<<<<<< SEARCH\n=======\n"
                         '{"case_id": "test_001"}\n>>>>>>> REPLACE\n```\n'
                         "Only 3 reflections allowed, stopping.\n")
            evidence = source.structured_artifact_write_evidence(
                artifact, unapplied, "", preexisting_before_launch=False,
                lower_started_epoch_ns=0, case_id="test_001")
            self.assertTrue(evidence["aider_search_replace_block"])
            self.assertFalse(evidence["applied_edit_confirmation"])
            self.assertFalse(evidence["accepted"])
            applied = ("agent_result.json\n```json\n<<<<<<< SEARCH\n=======\n"
                       '{"case_id": "test_001"}\n>>>>>>> REPLACE\n```\n'
                       "> Applied edit to agent_result.json\n")
            wrong_case = source.structured_artifact_write_evidence(
                artifact, applied, "", preexisting_before_launch=False,
                lower_started_epoch_ns=0, case_id="test_002")
            self.assertTrue(wrong_case["replacement_matches_artifact"])
            self.assertFalse(wrong_case["artifact_case_bound"])
            self.assertFalse(wrong_case["accepted"])

    def test_restored_stdout_mentions_are_ignored_by_aider(self) -> None:
        source_text = (ROOT / "evaluator/harness/run_lower_agent_case.py").read_text(encoding="utf-8")
        self.assertIn('"--aiderignore",AIDERIGNORE_CONTAINER_PATH', source_text)
        self.assertIn('AIDERIGNORE_CONTAINER_PATH = "/case-work/aiderignore"', source_text)
        # The reflection ceiling is a Candidate class attribute with no CLI
        # override; the evaluator must never pass a flag Aider does not define.
        self.assertNotIn("--max-reflections", source_text)
        source = load_module(ROOT / "evaluator/harness/run_lower_agent_case.py",
                             "aider_ignore_preflight_contract")
        with tempfile.TemporaryDirectory(prefix="aider-ignore-preflight-") as temporary:
            case_root = Path(temporary)
            (case_root / "repo").mkdir()
            receipt = source.prepare_aider_ignore(
                case_root, [{"id": "root"}, {"id": "component-a"}, {"id": "component-b"}])
            self.assertEqual(receipt["gitlink_paths"],
                             ["components/component-a", "components/component-b"])
            body = (case_root / "aiderignore").read_text(encoding="utf-8")
            self.assertIn("/components/component-a\n", body)
            self.assertIn("/components/component-b\n", body)
            self.assertFalse((case_root / "repo" / ".aiderignore").exists())
            with self.assertRaises(ValueError):
                source.prepare_aider_ignore(case_root, [{"id": "root"}])

    def test_harness_reflection_ceiling_is_not_candidate_failure(self) -> None:
        """0920-fh-003 test_001/test_003: 6 and 4 reflection stops, 0 edits."""
        source = load_module(ROOT / "evaluator/harness/run_lower_agent_case.py",
                             "aider_mention_ceiling_contract")
        log = ("components/component-a\n"
               "/case-work/repo/components/component-a: is a directory\n"
               "Dropping components/component-a from the chat.\n"
               "Only 3 reflections allowed, stopping.\n")
        ceiling = source.harness_file_mention_ceiling(log)
        self.assertTrue(ceiling["exhausted"])
        self.assertEqual(ceiling["reflection_ceiling_stops"], 1)
        base = dict(broker_stats_invalid=False, calls=9, failures=0, state_error=None,
                    process_exit=0, answer={}, transport_invalid=False,
                    artifact_contract_valid=False)
        self.assertEqual(source.classify_lower_result(**base), "candidate_behavior_failure")
        self.assertEqual(
            source.classify_lower_result(**base, harness_mention_ceiling_exhausted=True),
            "evaluator_infrastructure_failure")
        healthy = dict(base, artifact_contract_valid=True, answer={"case_id": "test_001"})
        self.assertEqual(
            source.classify_lower_result(**healthy, harness_mention_ceiling_exhausted=True),
            "candidate_valid")
        self.assertFalse(source.harness_file_mention_ceiling("nothing to see")["exhausted"])

    def test_dev_worlds_carry_the_declared_component_repositories(self) -> None:
        source_text = (ROOT / "evaluator/case_runtime.py").read_text(encoding="utf-8")
        self.assertIn('"dev_001": ["root", "component"]', source_text)
        self.assertIn('"dev_002": ["root", "component-a", "component-b"]', source_text)

    def test_action_count_accepts_persisted_plus_run_prefix(self) -> None:
        source = load_module(ROOT / "evaluator/harness/run_lower_agent_case.py", "aider_action_count_contract")
        history = ("+/run python3 /case-client/run_case inspect\n"
                   "/run python3 /case-client/run_case status\n")
        self.assertEqual(source.executed_product_action_count(history), 2)

    def test_case_task_is_persisted_separately_from_private_spec(self) -> None:
        launcher = (ROOT / "evaluator/harness/run_lower_agent_case.py").read_text(encoding="utf-8")
        finalizer = (ROOT / "evaluator/formal_finalize.py").read_text(encoding="utf-8")
        self.assertIn('"task_input.md"', launcher)
        self.assertIn('case_dir / "task_input.md"', finalizer)

    def test_case_runtime_uses_case_assets_and_arms_crash_once(self) -> None:
        runtime_module = load_module(ROOT / "evaluator/case_runtime.py", "aider_case_runtime_asset_contract")
        with tempfile.TemporaryDirectory(prefix="aider-case-asset-contract-") as temporary:
            root = Path(temporary)
            scenario = json.loads((ROOT / "test_cases/test_003/assets/scenario.json").read_text(encoding="utf-8"))
            runtime = runtime_module.CaseRuntime({
                "case_id": "test_003", "scenario": "durable-decision-prefix",
                "task_input": "recover the durable prefix", "allowed_actions": ["inspect", "run", "recover", "attest"],
                "scenario_asset": scenario,
            }, root / "case", root / "state.json")
            self.assertEqual([item["id"] for item in runtime.repo_specs], ["root", "shape-component", "nested-component"])
            first = runtime.adapter_request("run")
            second = runtime.adapter_request("run")
            self.assertIsNotNone(first.get("crash"))
            self.assertIsNone(second.get("crash"))
            self.assertTrue(runtime.crash_armed)
            comparison = runtime.semantic_comparison()
            self.assertEqual(comparison["primary_axis"], "durable_decision_prefix_recovery")
            self.assertTrue(comparison["checks"]["no_duplicate_run_after_crash"])

    def test_dirty_fixture_is_created_after_base_snapshot(self) -> None:
        runtime_module = load_module(ROOT / "evaluator/case_runtime.py", "aider_dirty_fixture_contract")
        with tempfile.TemporaryDirectory(prefix="aider-dirty-fixture-contract-") as temporary:
            root = Path(temporary)
            runtime = runtime_module.CaseRuntime({
                "case_id": "test_005", "scenario": "reverse-decision-dirty-state",
                "task_input": "restore the dirty state", "allowed_actions": ["inspect", "create", "rollback", "attest"],
                "scenario_asset": json.loads((ROOT / "test_cases/test_005/assets/scenario.json").read_text(encoding="utf-8")),
            }, root / "case", root / "state.json")
            component = runtime.repo_paths["component"]
            status = subprocess.run(["git", "status", "--porcelain"], cwd=component, text=True, capture_output=True, check=True)
            self.assertTrue(status.stdout.strip())
            self.assertEqual(runtime.base_oids["component"], runtime._git_at(component, "rev-parse", "HEAD"))
            self.assertTrue((component / "dirty-untracked.txt").is_file())

    def test_hidden_runtime_fixture_changes_with_case_axis(self) -> None:
        runtime_module = load_module(ROOT / "evaluator/case_runtime.py", "aider_case_runtime_contract")
        with tempfile.TemporaryDirectory(prefix="aider-case-runtime-contract-") as temporary:
            root = Path(temporary)
            common = {"allowed_actions": ["inspect", "create", "run", "status", "recover", "attest"]}
            one = runtime_module.CaseRuntime({"case_id": "test_001", "scenario": "admission", **common}, root / "one", root / "one-state.json")
            two = runtime_module.CaseRuntime({"case_id": "test_002", "scenario": "quarantine", **common}, root / "two", root / "two-state.json")
            dirty = runtime_module.CaseRuntime({"case_id": "test_005", "scenario": "rollback", "allowed_actions": common["allowed_actions"] + ["rollback"]}, root / "dirty", root / "dirty-state.json")
            self.assertEqual([item["id"] for item in one.repo_specs], ["root", "component-a", "component-b"])
            self.assertEqual([item["id"] for item in two.repo_specs], ["root", "component"])
            self.assertEqual(dirty.plan()["repositories"][1]["base_state_policy"], "snapshot")
            self.assertTrue(dirty.public_state()["fixture_facts"]["has_dirty_snapshot"])
            self.assertEqual(two.adapter_request("run")["crash"]["after"], "federation_prepared")
            self.assertIsNone(one.adapter_request("run")["crash"])

    def test_all_six_live_hidden_specs_materialize_distinct_behavioral_worlds(self) -> None:
        runtime_module = load_module(ROOT / "evaluator/case_runtime.py", "aider_all_hidden_worlds")
        controller = load_module(ROOT / "harbor/agentloop_controller.py", "aider_all_hidden_specs")
        facts, axes = {}, set()
        with tempfile.TemporaryDirectory(prefix="aider-all-six-worlds-") as temporary:
            root = Path(temporary)
            for case_id, base in controller.CASES["hidden"].items():
                spec = controller.case_spec(case_id, base)
                self.assertTrue(spec["task_input"].strip())
                self.assertTrue(spec["scenario_asset"])
                runtime = runtime_module.CaseRuntime(spec, root / case_id, root / f"{case_id}-state.json")
                declared = spec["scenario_asset"].get("repositories")
                expected_count = len(declared) if declared else (3 if case_id in {"test_001", "test_003"} else 2)
                self.assertEqual(len(runtime.repo_paths), expected_count)
                for name, child in runtime.repo_paths.items():
                    if name == 'root':
                        continue
                    entry = runtime._git_at(runtime.repo, 'ls-tree', runtime.base_oid, '--', 'components/' + name)
                    self.assertEqual(entry.split()[:3], ['160000', 'commit', runtime.base_oids[name]])
                submodule = subprocess.run(['git', 'submodule', 'status'], cwd=runtime.repo,
                    capture_output=True, text=True)
                self.assertEqual(submodule.returncode, 0, submodule.stderr)
                plan = runtime.plan()
                self.assertEqual(set(plan['integration_order']), {row['id'] for row in plan['subtasks']})
                self.assertEqual(plan['integration_order'][-1], 'edit-root')
                self.assertEqual(set(plan['publication']['participant_order']), set(runtime.repo_paths))
                self.assertEqual(plan['publication']['participant_order'][-1], 'root')
                if case_id in {'test_001', 'test_003'}:
                    root_task = next(row for row in plan['subtasks'] if row['repository_id'] == 'root')
                    self.assertEqual(set(root_task['depends_on']), {'edit-' + name for name in runtime.repo_paths if name != 'root'})
                    for repo in runtime.repo_paths.values():
                        self.assertEqual(runtime._git_at(repo, 'status', '--porcelain', '--untracked-files=all'), '')
                if case_id == 'test_005':
                    self.assertTrue(runtime._preservation_unchanged(case_id))
                    self.assertNotEqual(runtime._git_at(runtime.repo, 'status', '--porcelain'), '')
                    (runtime.repo / 'transaction.txt').write_text('root snapshot lost')
                    self.assertFalse(runtime._preservation_unchanged(case_id))
                facts[case_id] = runtime.public_state()["fixture_facts"]
                axes.add(runtime.semantic_comparison()["primary_axis"])
                if case_id == "test_006":
                    self.assertTrue(runtime.semantic_comparison()["checks"]["integrity_guard_fixture_present"])
                    self.assertTrue(runtime._preservation_unchanged(case_id))
                    (runtime.repo_paths["component"] / ".git/aider/transactions/global-decision.json").write_text("tampered")
                    self.assertFalse(runtime._preservation_unchanged(case_id))
                if case_id == "test_004":
                    self.assertTrue(runtime._preservation_unchanged(case_id))
                    (runtime.repo_paths["component"] / "rename-source.txt").write_text("foreign bytes replaced")
                    self.assertFalse(runtime._preservation_unchanged(case_id))
            self.assertEqual(len(axes), 6)
            self.assertTrue(facts["test_004"]["foreign_writer_present"])
            self.assertFalse(facts["test_001"]["foreign_writer_present"])
            self.assertTrue(facts["test_005"]["has_dirty_snapshot"])
            self.assertTrue(facts["test_006"]["admission_guard_present"])

    def test_case_specific_action_and_preservation_predicates_fail_closed(self) -> None:
        runtime_module = load_module(ROOT / "evaluator/case_runtime.py", "aider_case_specific_predicate_contract")
        with tempfile.TemporaryDirectory(prefix="aider-case-specific-predicate-contract-") as temporary:
            root = Path(temporary)
            runtime = runtime_module.CaseRuntime({
                "case_id": "test_004", "scenario": "component-conflict-ownership",
                "task_input": "resolve the component conflict without taking foreign work",
                "allowed_actions": ["inspect", "create", "status", "attest"],
                "scenario_asset": json.loads((ROOT / "test_cases/test_004/assets/scenario.json").read_text(encoding="utf-8")),
            }, root / "case", root / "state.json")
            private = runtime._private_predicates()
            self.assertEqual(private["required_action_sequence"], ["inspect", "create"])
            self.assertEqual(private["required_action_any"], ["status", "attest"])
            self.assertFalse(runtime.semantic_comparison()["checks"]["required_action_sequence"])
            runtime.invocations = [{"action": "inspect"}, {"action": "create"}]
            comparison = runtime.semantic_comparison()
            self.assertTrue(comparison["checks"]["required_action_sequence"])
            self.assertFalse(comparison["checks"]["required_followup_observation"])

    def test_builder_baseline_discards_stale_index_without_changing_bytes(self) -> None:
        formal = load_module(ROOT / "harbor/formal_one_stop.py", "aider_builder_baseline_contract")
        with tempfile.TemporaryDirectory(prefix="aider-builder-baseline-contract-") as temporary:
            worktree = Path(temporary) / "worktree"
            worktree.mkdir()
            (worktree / "tracked.txt").write_text("copied source\n", encoding="utf-8")
            subprocess.run(["git", "init", "-q", str(worktree)], check=True)
            subprocess.run(["git", "config", "user.name", "contract"], cwd=worktree, check=True)
            subprocess.run(["git", "config", "user.email", "contract@example.invalid"], cwd=worktree, check=True)
            (worktree / "tracked.txt").write_text("stale index content\n", encoding="utf-8")
            subprocess.run(["git", "add", "tracked.txt"], cwd=worktree, check=True)
            subprocess.run(["git", "commit", "-q", "-m", "stale baseline"], cwd=worktree, check=True)
            (worktree / "tracked.txt").write_text("copied source\n", encoding="utf-8")
            formal._initialize_builder_worktree(worktree)
            status = subprocess.run(["git", "status", "--porcelain"], cwd=worktree, text=True, capture_output=True, check=True)
            self.assertEqual(status.stdout, "")
            self.assertEqual((worktree / "tracked.txt").read_text(encoding="utf-8"), "copied source\n")

    def test_finalizer_rejects_changed_persisted_trajectory(self) -> None:
        with tempfile.TemporaryDirectory(prefix="aider-provenance-contract-") as temporary:
            run_dir = Path(temporary)
            case_dir = run_dir / "evaluations/hidden/test_001"
            case_dir.mkdir(parents=True)
            artifact = case_dir / "agent_artifact.json"
            artifact.write_text(json.dumps({
                "schema_version": "agentswe-aider-agent-result/v1",
                "case_id": "test_001", "observations": [], "decision": {},
            }), encoding="utf-8")
            trajectory = case_dir / "trajectory.log"
            trajectory.write_text("lower trajectory\n", encoding="utf-8")
            digest = hashlib.sha256(trajectory.read_bytes()).hexdigest()
            (case_dir / "lower.stdout.log").write_text("stdout\n", encoding="utf-8")
            (case_dir / "native_evidence.json").write_text("{}", encoding="utf-8")
            (case_dir / "oracle_comparison.json").write_text("{}", encoding="utf-8")
            (run_dir / "spec_test_001.json").write_text("{}", encoding="utf-8")
            (case_dir / "result.json").write_text("{}", encoding="utf-8")
            record = {
                "result": str(case_dir / "result.json"),
                "artifact_sha256": hashlib.sha256(artifact.read_bytes()).hexdigest(),
                "trajectory_digest": digest,
                "broker": {"successful_calls": 1},
                "artifact_contract": {
                    "source": "lower_product_workspace", "evaluator_synthesized": False,
                    "copied_after_lower_exit": True, "candidate_mount_read_only": True,
                    "preexisting_before_launch": False, "trajectory_artifact_reference": True,
                    "trajectory_digest": digest, "copy_path": str(artifact),
                    "exists": True, "valid_json": True, "case_id_matches": True,
                    "write_evidence": {"accepted": True, "aider_search_replace_block": True,
                                       "created_after_lower_start": True, "preexisting_before_launch": False},
                },
            }
            trajectory.write_text("tampered trajectory\n", encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "trajectory digest"):
                self.finalizer.run_result_judge(run_dir, "test_001", record, "http://127.0.0.1:1/v1/responses")


if __name__ == "__main__":
    unittest.main(verbosity=2)
