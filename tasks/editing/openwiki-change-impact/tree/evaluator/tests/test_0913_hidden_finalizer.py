"""Provider-free routing controls with explicitly mocked lower executions.

Tiny accepted-source fixtures and infrastructure-failure records exercise the
real Controller freeze, hidden/pilot routing, and shared finalizer. They are
not Candidate execution, semantic scores, or formal acceptance evidence.
"""
import contextlib
import copy
import importlib.util
import io
import json
from pathlib import Path
import socket
import subprocess
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
from agentloop.evaluator.controller import Controller
from agentloop.evaluator import execution_evidence, hidden_controller as hidden
from agentloop.protocol import DEV_CASES, HIDDEN_CASES, file_sha256, write_json
from harbor import formal_one_stop as one_stop

spec = importlib.util.spec_from_file_location('openwiki_hidden_finalizer_0913', ROOT / 'evaluator/formal_axes.py')
axes = importlib.util.module_from_spec(spec)
spec.loader.exec_module(axes)


class HiddenFinalizerTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.run = Path(self.temp.name) / 'mock-routing-run'
        self.owner = self.run / 'lifecycle'
        self.network = patch.object(socket.socket, 'connect', side_effect=AssertionError('network forbidden'))
        self.process = patch.object(subprocess, 'Popen', side_effect=AssertionError('process forbidden'))
        self.network.start()
        self.process.start()
        self.addCleanup(self.network.stop)
        self.addCleanup(self.process.stop)

    def controller(self, *, pilot=False, inventory=None):
        selected = inventory or (('test_001',) if pilot else HIDDEN_CASES)
        controller = Controller(ROOT / 'input/repository', ROOT, self.owner,
            'http://offline.invalid', builder_session_id='mock-hidden-routing-fixture',
            hidden_cases=selected, pilot_not_formal=pilot)
        accepted = self.owner / 'candidate_1/repository'
        (accepted / 'dist').mkdir(parents=True)
        (accepted / 'dist/cli.js').write_text('// Mock source fixture; never executed.\n')
        controller.records = [{'round': 1, 'candidate_digest': 'a' * 64,
            'builder_session_id': controller.builder_session_id,
            'mock_fixture_not_model_work': True,
            'build': {'valid': True, 'product_entry': str(accepted / 'dist/cli.js')},
            'dev_cases': {case_id: {'mock_fixture': True} for case_id in DEV_CASES},
            'feedback': {'available': True, 'infrastructure_invalid': False}, 'revision': {}}]
        controller.freeze()
        return controller

    @staticmethod
    def mock_lower(*args, **kwargs):
        return {'classification': 'evaluator_failure', 'infrastructure_invalid': True,
            'mock_fixture_no_product_execution': True, 'product_started': False,
            'broker_delta': {'calls': 0, 'successful_calls': 0}}

    @staticmethod
    def mock_prepare(run, *, case_id, candidate_digest, repository, output,
                     request, cases_root, initial):
        output = Path(output)
        output.mkdir(parents=True, exist_ok=True)
        task = output / 'executed_task.md'
        task.write_bytes(Path(request).read_bytes())
        comparison = output / 'private-oracle-comparison.json'
        write_json(comparison, {'case_id': case_id, 'candidate_visible': False,
            'mock_fixture_no_semantic_observation': True})
        record = dict(run, case_id=case_id, candidate_digest=candidate_digest,
            executed_task_path=str(task), executed_task_sha256=file_sha256(task),
            private_oracle_comparison_path=str(comparison),
            private_oracle_comparison_sha256=file_sha256(comparison),
            execution_record_path=str(output / 'execution_record.json'))
        write_json(output / 'execution_record.json', record)
        return record

    def normal_attestation(self, controller):
        freeze_path = self.owner / 'freeze_manifest.json'
        gate = hidden._claim_hidden_once(self.owner, freeze_path, controller.frozen)
        with patch.object(hidden, 'run_case', side_effect=self.mock_lower) as lower, \
                patch.object(execution_evidence, 'prepare_evidence', side_effect=self.mock_prepare):
            result, attestation = hidden._execute_cases(freeze_path,
                self.owner / 'hidden-result.json', 'http://offline.invalid', 'mock-broker',
                ROOT, 600, controller.frozen, Path(controller.frozen['candidate_path']), gate)
        self.assertEqual(lower.call_count, 6)
        return result, attestation

    def assert_routed_finalizer(self, controller, attestation, selected, *, pilot=False):
        self.assertEqual(axes._SHARED_HIDDEN_LIFECYCLE_ERRORS(
            attestation, controller.frozen, self.run, selected), [])
        self.assertEqual(axes.hidden_lifecycle_errors(
            attestation, controller.frozen, self.run, selected), [])
        overlay, comparisons = axes.prepare_run_local_formal_root(self.run, selected)
        self.assertEqual(tuple(comparisons), selected)
        for case_id in selected:
            record = attestation['cases'][case_id]
            self.assertEqual((overlay / 'test_cases' / case_id / 'input.md').read_bytes(),
                Path(record['executed_task_path']).read_bytes())
        argv = ['--run-dir', str(self.run)]
        if pilot:
            argv += ['--acceptance-cases', 'test_001']
        with contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(axes.main(argv), 2)
        name = 'acceptance_aggregation.json' if pilot else 'formal_aggregation.json'
        aggregation = json.loads((self.run / name).read_text())
        self.assertEqual(set(aggregation['infrastructure_invalid_cases']), set(selected))
        self.assertEqual(len(aggregation['result_reasons']), len(selected))
        self.assertTrue(all('infrastructure-invalid/N/A' in reason for reason in aggregation['result_reasons']))
        self.assertEqual(aggregation['code_reasons'], ['evaluator Code judge credential file is unavailable'])
        self.assertEqual(aggregation['candidate_zero_cases'], [])
        self.assertEqual(aggregation['fresh_semantic_judge_cases'], [])

    def test_normal_six_case_route_reaches_actual_shared_finalizer(self):
        controller = self.controller()
        result, attestation = self.normal_attestation(controller)
        self.assertEqual(attestation['cases'], result['cases'])
        self.assert_routed_finalizer(controller, attestation, HIDDEN_CASES)

    def test_actual_pilot_route_and_explicit_freeze_adapter(self):
        controller = self.controller(pilot=True)
        broker = SimpleNamespace(endpoint='http://offline.invalid')
        stats = {'calls': 0, 'successful_calls': 0, 'failures': 0,
            'provider_failures': 0, 'broker_instance_id': 'mock-broker'}
        with patch.object(one_stop, 'read_broker_stats', return_value=stats), \
                patch.object(one_stop, 'run_case', side_effect=self.mock_lower) as lower, \
                patch.object(one_stop, 'prepare_evidence', side_effect=self.mock_prepare):
            attestation = one_stop.run_pilot_hidden(SimpleNamespace(
                controller=controller, run_dir=self.run), broker)
        self.assertEqual(lower.call_count, 1)
        self.assertFalse(attestation['behavior_evaluable'])
        self.assertEqual(axes.frozen_identity_errors(controller.frozen, self.run,
            allow_pilot_test_001=True), [])
        self.assertTrue(axes.frozen_identity_errors(controller.frozen, self.run))
        self.assert_routed_finalizer(controller, attestation, ('test_001',), pilot=True)
        with self.assertRaisesRegex(ValueError, 'already consumed'):
            hidden._claim_hidden_once(self.owner, self.owner / 'freeze_manifest.json',
                controller.frozen, expected_cases=('test_001',))

    def test_pilot_cannot_enter_normal_six_case_runner(self):
        controller = self.controller(pilot=True)
        with patch.object(hidden, 'EvaluatorBrokerLifecycle', side_effect=AssertionError('broker forbidden')):
            with self.assertRaisesRegex(ValueError, 'pilot freeze cannot authorize'):
                hidden.run_hidden_suite(self.owner / 'freeze_manifest.json',
                    self.owner / 'hidden-result.json', self.run / 'missing-credential', ROOT)
        self.assertFalse((self.owner / 'hidden-once-gate.json').exists())

    def test_other_pilot_subset_cannot_pass_test001_adapter(self):
        controller = self.controller(pilot=True, inventory=('test_002',))
        self.assertTrue(axes.frozen_identity_errors(controller.frozen, self.run,
            allow_pilot_test_001=True))

    def test_claimed_lifecycle_booleans_do_not_override_actual_records(self):
        controller = self.controller()
        _, attestation = self.normal_attestation(controller)
        for kind in ('early_start', 'missing_start', 'digest', 'case_inventory', 'gate_time', 'freeze_sha'):
            with self.subTest(kind=kind):
                bad = copy.deepcopy(attestation)
                first = bad['cases']['test_001']
                if kind == 'early_start':
                    first['case_started_at'] = controller.frozen['frozen_at']
                elif kind == 'missing_start':
                    first.pop('case_started_at')
                elif kind == 'digest':
                    first['frozen_digest_after'] = 'b' * 64
                elif kind == 'case_inventory':
                    bad['cases'].pop('test_006')
                elif kind == 'gate_time':
                    bad['hidden_started_at'] = controller.frozen['frozen_at']
                else:
                    bad['freeze_manifest_sha256'] = '0' * 64
                self.assertTrue(axes.hidden_lifecycle_errors(bad, controller.frozen, self.run))

    def test_missing_infra_inputs_do_not_prevent_shared_code_axis(self):
        controller = self.controller()
        _, attestation = self.normal_attestation(controller)
        task = Path(attestation['cases']['test_001']['executed_task_path'])
        comparison = Path(attestation['cases']['test_002']['private_oracle_comparison_path'])
        task.unlink()
        comparison.unlink()
        with contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(axes.main(['--run-dir', str(self.run)]), 2)
        aggregation = json.loads((self.run / 'formal_aggregation.json').read_text())
        self.assertEqual(set(aggregation['infrastructure_invalid_cases']), set(HIDDEN_CASES))
        self.assertEqual(aggregation['code_reasons'], ['evaluator Code judge credential file is unavailable'])
        self.assertFalse(task.exists())
        self.assertFalse(comparison.exists())

    def test_missing_other_inputs_are_explicit_routing_infrastructure(self):
        controller = self.controller()
        _, attestation = self.normal_attestation(controller)
        record = attestation['cases']['test_001']
        record.update(infrastructure_invalid=False, classification='candidate_timeout')
        Path(record['executed_task_path']).unlink()
        write_json(self.owner / 'hidden-after-freeze-attestation.json', attestation)
        with contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(axes.main(['--run-dir', str(self.run)]), 2)
        aggregation = json.loads((self.run / 'formal_aggregation.json').read_text())
        self.assertIn('exact-task/oracle routing failed', aggregation['infrastructure_invalid_cases']['test_001'])
        self.assertEqual(aggregation['candidate_zero_cases'], [])
        self.assertEqual(aggregation['code_reasons'], ['evaluator Code judge credential file is unavailable'])


if __name__ == '__main__':
    unittest.main()
