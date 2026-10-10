"""Regression tests for observed infra-zero and retry corruption, no providers."""
import importlib.util
import json
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("codex_one_stop_0909", ROOT / "harbor/formal_one_stop.py")
runner = importlib.util.module_from_spec(spec)
spec.loader.exec_module(runner)
from build_preflight import (BuildInfrastructureError, infrastructure_build_error, freeze_binary,
                             file_hash, build_environment, isolated_build_command)


class BuildAndLifecycleTests(unittest.TestCase):
    def test_compiler_namespace_has_only_run_mounts_and_no_inherited_secret(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            runtime, target, worktree = root / 'runtime', root / 'cache/target', root / 'source'
            for path in (runtime, target, worktree / 'codex-rs', target.parent / 'isolated-cargo-home'):
                path.mkdir(parents=True, exist_ok=True)
            with patch('build_preflight.shutil.which', return_value='/usr/bin/bwrap'), \
                 patch.dict('os.environ', {'DEEPSEEK_API_KEY': 'must-not-inherit'}):
                command = isolated_build_command(['cargo', 'build'], runtime=runtime, target=target, worktree=worktree)
                self.assertIn('--unshare-all', command)
                self.assertIn('--clearenv', command)
                self.assertNotIn('DEEPSEEK_API_KEY', command)
                self.assertNotIn('DEEPSEEK_API_KEY', build_environment(runtime, target))
                self.assertNotIn(str(root), command)
                self.assertIn(str(worktree.resolve()), command)

    def test_prebuilt_rejects_old_unbound_receipt(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            runner.write_json(root / 'old/candidate_build/build_result.json', {'binary_sha256': 'f' * 64})
            with self.assertRaisesRegex(BuildInfrastructureError, 'contemporaneous'):
                runner.reuse_prebuilt_build(build_run=root / 'old', benchmark=root / 'task',
                    snapshot=root / 'delivery', output=root / 'new', runtime=root / 'runtime')

    def test_prebuilt_binds_delivery_binary_toolchain_and_baseline(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            baseline = root / 'task/input/repository/codex-rs'
            baseline.mkdir(parents=True)
            (baseline / 'Cargo.lock').write_text('frozen lock')
            delivery = root / 'delivery'
            delivery.mkdir()
            (delivery / 'solution.patch').write_text('patch')
            binary = root / 'old/candidate_build/binary/codex'
            binary.parent.mkdir(parents=True)
            binary.write_bytes(b'compiled Candidate')
            built_lock = root / 'old/candidate_build/worktree/codex-rs/Cargo.lock'
            built_lock.parent.mkdir(parents=True)
            built_lock.write_text('generated actual Candidate lock')
            binding = {'schema': 'agentswe-codex-prebuilt/v1', 'measured_before_and_after_build': True,
                'candidate_digest': runner.tree_digest(delivery),
                'baseline_source_digest': runner.tree_digest(baseline.parent),
                'solution_patch_sha256': file_hash(delivery / 'solution.patch'),
                'cargo_lock_sha256': file_hash(built_lock),
                'binary_sha256': file_hash(binary), 'toolchain': {'sha256': 'toolchain-a'}}
            runner.write_json(root / 'old/candidate_build/build_result.json', {
                'binary': str(binary), 'immutable_build_binding': binding, 'cargo_build': {'exit_code': 0}})
            runner.write_json(root / 'old/build_preflight.json', {'baseline_compiled': True})
            kwargs = dict(build_run=root / 'old', benchmark=root / 'task', snapshot=delivery,
                          output=root / 'new', runtime=root / 'runtime')
            with patch.object(runner, 'check_build_environment', return_value={'valid': True}), \
                 patch.object(runner, 'toolchain_identity', return_value={'sha256': 'toolchain-a'}):
                copy, result = runner.reuse_prebuilt_build(**kwargs)
                self.assertEqual(copy.read_bytes(), binary.read_bytes())
                self.assertFalse(result['prebuilt_reuse']['historical_evidence_modified'])
                before = (root / 'new/build_result.json').read_bytes()
                with self.assertRaisesRegex(BuildInfrastructureError, 'new empty output'):
                    runner.reuse_prebuilt_build(**kwargs)
                self.assertEqual((root / 'new/build_result.json').read_bytes(), before)
                binary.write_bytes(b'tampered')
                with self.assertRaisesRegex(BuildInfrastructureError, 'binding failed'):
                    runner.reuse_prebuilt_build(**kwargs)
                binary.write_bytes(b'compiled Candidate')
            with patch.object(runner, 'check_build_environment', return_value={'valid': True}), \
                 patch.object(runner, 'toolchain_identity', return_value={'sha256': 'toolchain-b'}):
                with self.assertRaisesRegex(BuildInfrastructureError, 'toolchain identity'):
                    runner.reuse_prebuilt_build(**{**kwargs, 'output': root / 'new-toolchain'})

    def test_permission_is_not_source_failure(self):
        self.assertTrue(infrastructure_build_error("cargo_build", "failed to create directory: Permission denied (os error 13)"))
        self.assertFalse(infrastructure_build_error("cargo_build", "error[E0308]: mismatched types"))
        self.assertTrue(infrastructure_build_error("cargo_build", "no matching package named `serde` found; offline mode"))
        self.assertTrue(infrastructure_build_error("cargo_build", "unknown compiler failure"))

    def test_binary_is_immutable_between_candidate_builds(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            mutable = root / "cached-codex"
            mutable.write_bytes(b"first-candidate")
            frozen = freeze_binary(mutable, root / "build1")
            mutable.write_bytes(b"second-candidate")
            self.assertEqual(frozen.read_bytes(), b"first-candidate")

    # These tests isolate lifecycle accounting; native identity is separately
    # exercised by the actual Harbor/CLI integration diagnostics.
    @patch.object(runner.DevController, "bind_native_thread", lambda self: None)
    def test_infra_duplicate_keeps_completed_and_unknown_bytes_without_resampling(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            workspace = root / "workspace"
            workspace.mkdir()
            (workspace / "solution.patch").write_text("nonempty")
            for name in ("edit_report.json", "run_report.json"):
                (workspace / name).write_text("{}")
            calls = []
            def evaluate(number, snapshot):
                calls.append((number, snapshot))
                if len(calls) == 1:
                    result = root / 'evaluations/submission_001/dev_001/result.json'
                    result.parent.mkdir(parents=True)
                    result.write_bytes(b'{"score":99,"classification":"completed"}\n')
                    unknown = root / 'evaluations/submission_001/dev_002/provider_response.raw'
                    unknown.parent.mkdir(parents=True)
                    unknown.write_bytes(b'original partial response, delivery unknown')
                    raise BuildInfrastructureError("injected dev_002 delivery unknown after dev_001 completed")
                return {"dev_001": {"score": 0, "classification": "candidate_build_failure", "broker": {}}}, None
            controller = runner.DevController(run_dir=root, workspace=workspace, evaluate=evaluate,
                                             public_cases=("dev_001",), max_dev_rounds=2)
            code, first = controller.submit()
            self.assertEqual(code, 202)
            controller.wait_idle()
            self.assertEqual(controller.records, [])
            self.assertIsNone(controller.frozen)
            self.assertTrue((root / "infrastructure_attempts/attempt_001/candidate/solution.patch").is_file())
            completed = root / 'infrastructure_attempts/attempt_001/evaluation/dev_001/result.json'
            unknown = root / 'infrastructure_attempts/attempt_001/evaluation/dev_002/provider_response.raw'
            before = (completed.read_bytes(), unknown.read_bytes())
            code, second = controller.submit()
            self.assertEqual(code, 200)
            controller.wait_idle()
            self.assertEqual(second['state'], 'infrastructure_error')
            self.assertFalse(second['replay_allowed'])
            self.assertEqual(len(controller.records), 0)
            self.assertEqual([number for number, _ in calls], [1])
            self.assertEqual((completed.read_bytes(), unknown.read_bytes()), before)
            with self.assertRaisesRegex(BuildInfrastructureError, 'history'):
                runner.DevController(run_dir=root, workspace=workspace, evaluate=evaluate)
            self.assertEqual([number for number, _ in calls], [1])
            self.assertEqual((completed.read_bytes(), unknown.read_bytes()), before)

    @patch.object(runner.DevController, "bind_native_thread", lambda self: None)
    def test_infra_result_dictionary_is_not_a_zero_round(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            workspace = root / "workspace"
            workspace.mkdir()
            (workspace / "solution.patch").write_text("nonempty")
            for name in ("edit_report.json", "run_report.json"):
                (workspace / name).write_text("{}")
            controller = runner.DevController(run_dir=root, workspace=workspace,
                evaluate=lambda *_: ({"dev_001": {"score": 0, "classification": "provider_infrastructure_failure"}}, None),
                public_cases=("dev_001",), max_dev_rounds=2)
            controller.submit()
            controller.wait_idle()
            self.assertEqual(controller.records, [])
            self.assertFalse(controller.infrastructure_attempts[0]["round_consumed"])

    def test_hidden_transition_requires_actual_freeze_and_zero_state(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            args = type("Args", (), {"broker_script": root / "broker.py", "credential_file": root / "secret",
                                    "broker_image": "unused"})()
            kwargs = dict(args=args, run_dir=root, port=999, name="test", cidfile=root / "id")
            with patch.object(runner, "start_broker") as start, patch.object(runner, "broker_stats", return_value={"runtime": {"calls": 1}}):
                with self.assertRaises(FileNotFoundError):
                    runner.start_fresh_hidden_broker(**kwargs)
                start.assert_not_called()
                runner.write_json(root / "freeze_manifest.json", {"candidate_digest": "abc", "frozen_at": "now"})
                with self.assertRaises(RuntimeError):
                    runner.start_fresh_hidden_broker(**kwargs)
            with patch.object(runner, "start_broker"), patch.object(runner, "broker_stats", return_value={"runtime": {"calls": 0, "failures": 0}}):
                evidence = runner.start_fresh_hidden_broker(**kwargs)
                self.assertEqual(evidence["frozen_candidate_digest"], "abc")


if __name__ == "__main__":
    unittest.main()
