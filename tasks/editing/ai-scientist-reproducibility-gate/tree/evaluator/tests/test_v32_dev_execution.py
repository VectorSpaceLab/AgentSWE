"""Actual dev dispatcher and durable attempt paths, with no real model requests.

Product builds, lower subprocesses and the semantic scorer are explicitly
synthetic. Unlike the earlier lifecycle tests, Controller._run_case is real.
"""
import copy
import importlib.util
import json
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import types
import unittest
from unittest.mock import Mock, patch

ROOT = Path(__file__).resolve().parents[2]
sys.path[:0] = [str(ROOT), str(ROOT / 'agentloop')]
from agentloop import two_round_controller as control
from agentloop.stable_product import product_source_digest
from agentloop.protocol import tree_digest, write_json
from agentloop.run_hidden import _validate_freeze


class DevExecutionTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        self.workspace = self.root / 'submission'
        self.workspace.mkdir()
        self.write_delivery(1)
        self.controller = self.reload()
        self.build = self.patch(patch.object(control, 'build_candidate', side_effect=self.fake_build))
        self.lower = self.patch(patch.object(control.subprocess, 'run', side_effect=self.fake_lower))
        self.scorer = Mock(side_effect=self.fake_score)
        module = types.ModuleType('case_evidence')
        module.score_case = self.scorer
        self.patch(patch.dict(sys.modules, {'case_evidence': module}))

    def patch(self, patcher):
        value = patcher.start()
        self.addCleanup(patcher.stop)
        return value

    def reload(self):
        return control.Controller(ROOT, self.root / 'lifecycle', 'http://unused-lower.invalid',
            run_kind='pilot', result_judge_endpoint='http://unused-judge.invalid')

    def write_delivery(self, revision):
        (self.workspace / 'solution.patch').write_text(
            'diff --git a/fixture.py b/fixture.py\nnew file mode 100644\n--- /dev/null\n'
            f'+++ b/fixture.py\n@@ -0,0 +1 @@\n+revision = {revision}\n')
        for name in ('edit_report.json', 'run_report.json'):
            write_json(self.workspace / name, {'synthetic_unit_fixture': True, 'revision': revision})

    @staticmethod
    def fake_build(_source, delivery, out):
        repository = out / 'repository'
        repository.mkdir(parents=True)
        (repository / 'fixture.patch.txt').write_bytes((delivery / 'solution.patch').read_bytes())
        return {'valid': True, 'product_source_digest': product_source_digest(repository), 'candidate_repo_digest': tree_digest(repository), 'synthetic_unit_fixture': True}

    def fake_lower(self, command, **_kwargs):
        out = Path(command[command.index('--output-dir') + 1])
        case = Path(command[command.index('--case-file') + 1]).parent.name
        value = {'case_id': case, 'valid': True, 'classification': 'candidate_valid',
            'classification_axis': 'candidate', 'real_execution': False,
            'synthetic_unit_fixture': True, 'broker': {'calls_delta': 0, 'successful_calls': 0},
            'probe_invocation': self.lower.call_count, 'artifact_paths': [str(out / 'product.txt')]}
        write_json(out / 'launcher_result.json', value)
        (out / 'product.txt').write_text(f'Synthetic lower invocation {self.lower.call_count}\n')
        return subprocess.CompletedProcess(command, 0, '', '')

    @staticmethod
    def fake_score(**kwargs):
        record = json.loads(kwargs['record_path'].read_text())
        assert record['case_id'] == kwargs['case_id']
        assert tree_digest(kwargs['candidate']) == kwargs['candidate_digest']
        return {'score': 80, 'contract_valid': True, 'round_consumed': True,
            'feedback': {'assessment': 'Synthetic scorer fixture, not benchmark judgment'}}

    def submit(self):
        return self.controller.submit(self.workspace, builder_session_id='synthetic-v32-session',
            feedback_digest_ack=self.controller.feedback_digest)

    def fail_scoring(self):
        self.scorer.side_effect = RuntimeError('synthetic evaluator unavailable')

    def test_actual_two_dev_dispatch_reaches_scorer_and_authoritative_feedback(self):
        result = self.submit()
        self.assertTrue(result['accepted'], result)
        self.assertEqual(self.lower.call_count, 2)
        self.assertEqual(self.scorer.call_count, 2)
        self.assertEqual([call.kwargs['case_id'] for call in self.scorer.call_args_list], list(control.DEV_CASES))
        self.assertTrue(all(call.kwargs['broker_endpoint'] == 'http://unused-judge.invalid'
            for call in self.scorer.call_args_list))
        self.assertEqual(result['feedback']['dev_scores'], {'dev_001': 80, 'dev_002': 80})
        self.assertEqual(result['feedback']['dev_mean'], 80)
        self.assertIsNone(self.controller.frozen)

    def test_real_hidden_dispatch_does_not_run_public_dev_scorer(self):
        for case in control.HIDDEN_CASES:
            value = self.controller._run_case(self.workspace, case, self.root / 'hidden' / case, hidden=True)
            self.assertNotIn('result_evaluation', value)
        self.assertEqual(self.lower.call_count, 6)
        self.scorer.assert_not_called()

    def test_scorer_exception_is_infrastructure_and_no_accepted_round(self):
        self.fail_scoring()
        result = self.submit()
        self.assertFalse(result['accepted'])
        self.assertFalse(result['round_consumed'])
        self.assertEqual(self.controller.records, [])
        self.assertEqual(self.scorer.call_count, 2)
        for value in result['dev'].values():
            self.assertEqual(value['classification_axis'], 'infrastructure')
            self.assertIn('RuntimeError', value['result_evaluation']['reason'])

    def test_two_failed_attempts_preserve_original_dev_files_and_paths(self):
        self.fail_scoring()
        first = self.submit()
        paths = {case: Path(value['output_path']) / 'launcher_result.json' for case, value in first['dev'].items()}
        before = {case: path.read_bytes() for case, path in paths.items()}
        second = self.submit()
        self.assertFalse(second['accepted'])
        for case, path in paths.items():
            self.assertEqual(path.read_bytes(), before[case], 'new attempt overwrote original lower evidence')
            self.assertEqual(path.parent, Path(second['dev'][case]['output_path']))
        self.assertEqual(self.lower.call_count, 2, 'failed scoring must not repeat completed lower work')
        self.assertNotEqual(first['attempt_paths']['record'], second['attempt_paths']['record'])
        self.assertEqual(len(self.controller.infrastructure_attempts), 2)

    def test_reload_preserves_infrastructure_ledger_exactly(self):
        self.fail_scoring()
        self.submit()
        before = copy.deepcopy(self.controller.infrastructure_attempts)
        self.controller = self.reload()
        self.assertEqual(self.controller.infrastructure_attempts, before)
        self.submit()
        persisted = json.loads((self.controller.run_dir / 'dev_lifecycle.json').read_text())
        self.assertEqual(len(persisted['infrastructure_attempts']), 2)
        self.assertEqual(persisted['infrastructure_attempts'][0], before[0])

    def test_malformed_infrastructure_ledger_is_not_silently_erased(self):
        self.fail_scoring()
        self.submit()
        path = self.controller.run_dir / 'dev_lifecycle.json'
        state = json.loads(path.read_text())
        for invalid in ({}, 'invalid', [None], [42]):
            state['infrastructure_attempts'] = invalid
            write_json(path, state)
            with self.subTest(invalid=invalid), self.assertRaisesRegex(RuntimeError, 'infrastructure'):
                self.reload()

    def test_success_after_failed_attempt_keeps_evidence_and_can_freeze(self):
        self.fail_scoring()
        first = self.submit()
        evidence = Path(first['dev']['dev_001']['output_path']) / 'launcher_result.json'
        before = evidence.read_bytes()
        self.scorer.side_effect = self.fake_score
        result = self.submit()
        self.assertTrue(result['accepted'], result)
        self.assertEqual(len(self.controller.records), 1)
        self.assertEqual(len(self.controller.infrastructure_attempts), 1)
        self.assertEqual(evidence.read_bytes(), before)
        freeze = self.controller.freeze_latest('builder_exit')
        _, errors = _validate_freeze(self.controller.run_dir, freeze)
        self.assertEqual(errors, [])
        self.assertEqual(self.reload().frozen, freeze)

    def test_all_attempt_paths_remain_valid_after_failure(self):
        self.fail_scoring()
        result = self.submit()
        paths = result.get('attempt_paths')
        self.assertIsInstance(paths, dict)
        self.assertTrue(Path(paths['candidate']).is_dir())
        self.assertTrue(Path(paths['build']).is_dir())
        self.assertTrue(Path(paths['repository']).is_dir())
        self.assertEqual(tree_digest(Path(paths['candidate'])), result['candidate_digest'])
        self.assertEqual(tree_digest(Path(paths['repository'])), result['build']['candidate_repo_digest'])
        self.assertEqual(json.loads(Path(paths['record']).read_text()), result)

    def test_tampered_materialized_candidate_cannot_be_frozen(self):
        result = self.submit()
        repository = Path(result['attempt_paths']['repository'])
        (repository / 'fixture.patch.txt').write_text('unsubmitted change')
        with self.assertRaisesRegex(RuntimeError, 'repository changed'):
            self.controller.freeze_latest('builder_exit')
        self.assertIsNone(self.controller.frozen)

    def test_invalid_attempt_path_cannot_redirect_freeze(self):
        result = self.submit()
        original = copy.deepcopy(result['attempt_paths'])
        for invalid in ('invalid', {'root': str(self.root), 'repository': str(self.workspace)},
                        {**original, 'repository': str(self.workspace)}):
            self.controller.records[-1]['attempt_paths'] = invalid
            with self.subTest(invalid=invalid), self.assertRaisesRegex(RuntimeError, 'attempt paths'):
                self.controller.freeze_latest('builder_exit')

    def test_legacy_accepted_build_path_remains_read_compatible(self):
        result = self.submit()
        legacy = self.controller.run_dir / 'build' / 'candidate_001'
        shutil.copytree(Path(result['attempt_paths']['build']), legacy)
        self.controller.records[-1].pop('attempt_paths')
        freeze = self.controller.freeze_latest('builder_exit')
        _, errors = _validate_freeze(self.controller.run_dir, freeze)
        self.assertEqual(errors, [])

    def test_accepted_attempt_record_is_bound_to_ledger_and_start_receipt(self):
        result = self.submit()
        paths = result['attempt_paths']
        record = json.loads(Path(paths['record']).read_text())
        self.assertEqual(record, self.controller.records[-1])
        started = json.loads((Path(paths['root']) / 'attempt_started.json').read_text())
        self.assertEqual(started['candidate_digest'], record['candidate_digest'])
        self.assertEqual(started['builder_session_id'], record['builder_session_id'])
        self.assertEqual(started['attempt_paths'], paths)
        self.assertFalse(started['round_consumed'])  # startup is not acceptance

    def test_actual_task_evidence_adapter_is_reached_without_provider_request(self):
        # The real adapter must reject this deliberately incomplete fixture
        # before calling the judge. This verifies the import boundary as well
        # as the isolated scorer fixture used by the other tests.
        spec = importlib.util.spec_from_file_location('v32_actual_case_evidence', ROOT / 'evaluator/case_evidence.py')
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        with patch.dict(sys.modules, {'case_evidence': module}):
            result = self.submit()
        self.assertFalse(result['accepted'])
        for value in result['dev'].values():
            self.assertEqual(value['result_evaluation']['reason'], 'evaluator evidence/scoring failed: ValueError')
        self.scorer.assert_not_called()


if __name__ == '__main__':
    unittest.main()
