"""Synthetic resource receipts exercise attribution; no benchmark/model evidence.

Actual isolated compilation/resource controls are tested separately by the build
adapter tests. These tests use the real shared zero contract and real lifecycle.
"""
import copy
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[2]
sys.path[:0] = [str(ROOT), str(ROOT / 'evaluator'), str(ROOT / 'agentloop')]
from agentloop import two_round_controller as control
from agentloop.stable_product import product_source_digest
from agentloop.protocol import write_json, sha256_file, tree_digest
from agentloop.build_evidence import candidate_zero_contract_valid
from agentloop.run_hidden import _validate_freeze
from harbor.formal_one_stop import hidden_evidence_ready_for_finalizer


class BuildLifecycleTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.candidate = self.root / 'candidate'; self.candidate.mkdir()
        self.delivery(1)
        self.controller = control.Controller(ROOT, self.root / 'lifecycle', 'http://unused.invalid',
            run_kind='formal', result_judge_endpoint='http://unused.invalid')
        self.build = patch.object(control, 'build_candidate', side_effect=self.synthetic_build).start()
        self.addCleanup(patch.stopall)
        self.lower = patch.object(self.controller, '_run_case', side_effect=AssertionError('No lower rollout for compilation failure')).start()

    def delivery(self, revision):
        (self.candidate / 'solution.patch').write_text(f'diff --git a/bad.py b/bad.py\n+++ b/bad.py\n+def broken_{revision}(:\n')
        write_json(self.candidate / 'edit_report.json', {'synthetic_unit_fixture': True, 'revision': revision})
        write_json(self.candidate / 'run_report.json', {'synthetic_unit_fixture': True})

    @staticmethod
    def synthetic_build(_source, candidate, out):
        repository = out / 'repository'; repository.mkdir(parents=True)
        (repository / 'bad.py').write_bytes((candidate / 'solution.patch').read_bytes())
        resource = out / 'scope' / 'resource-attestation.json'
        write_json(resource, {'synthetic_unit_fixture': True, 'valid': True, 'timed_out': False,
            'purpose': 'build', 'memory_bytes': 4 * 1024**3, 'timeout_seconds': 1800,
            'cleanup': {'complete': True}, 'aggregate_parent': {'created_here': True},
            'aggregate_cleanup': {'complete': True}})
        result = {'synthetic_unit_fixture': True, 'valid': False,
            'classification': 'candidate_build_failure', 'failure': 'python_compileall',
            'causal_candidate_failure': True, 'infrastructure_health': {'valid': True,
                'baseline_compile': {'valid': True, 'compiler': '3.11.16'}},
            'product_source_digest': product_source_digest(repository), 'candidate_repo_digest': tree_digest(repository),
            'resource_attestation_path': str(resource), 'resource_attestation_sha256': sha256_file(resource),
            'compile_diagnostics': [{'path': 'bad.py', 'line': 1, 'exception': 'SyntaxError', 'message': 'invalid syntax'}]}
        write_json(out / 'build_manifest.json', result)
        return result

    def submit(self):
        return self.controller.submit(self.candidate, builder_session_id='synthetic-session',
            feedback_digest_ack=self.controller.feedback_digest)

    def test_causal_compile_zero_is_accepted_and_duplicate_is_cached(self):
        first = self.submit()
        self.assertTrue(first.get('accepted'), first)
        self.assertEqual(first['dev_scores'], {'dev_001': 0, 'dev_002': 0})
        self.assertFalse(first['frozen'])
        self.assertEqual(first['feedback']['build']['compile_diagnostics'][0]['exception'], 'SyntaxError')
        for result in first['dev'].values():
            self.assertFalse(result['real_execution'])
            self.assertTrue(candidate_zero_contract_valid(result))
        second = self.submit()
        self.assertTrue(second['duplicate_digest'])
        self.assertEqual(self.build.call_count, 1)
        self.lower.assert_not_called()

    def test_changed_build_environment_is_infrastructure_and_consumes_no_round(self):
        def broken(*args):
            result = self.synthetic_build(*args)
            result['infrastructure_health']['valid'] = False
            write_json(args[2] / 'build_manifest.json', result)
            return result
        self.build.side_effect = broken
        result = self.submit()
        self.assertFalse(result['accepted'])
        self.assertFalse(result['round_consumed'])
        self.assertEqual(len(self.controller.records), 0)
        self.assertTrue(all(row['classification_axis'] == 'infrastructure' for row in result['dev'].values()))

    def test_forged_boolean_without_shared_contract_is_rejected(self):
        result = self.submit()
        row = result['dev']['dev_001']
        row['result_evaluation']['contract_path'] = str(self.root / 'does-not-exist.json')
        self.assertFalse(candidate_zero_contract_valid(row))
        self.assertFalse(self.controller._dev_gate(result)[0])

    def test_wrong_python_version_is_not_a_candidate_zero(self):
        def wrong_version(*args):
            result = self.synthetic_build(*args)
            result['infrastructure_health']['baseline_compile']['compiler'] = '3.10.12'
            write_json(args[2] / 'build_manifest.json', result)
            return result
        self.build.side_effect = wrong_version
        result = self.submit()
        self.assertFalse(result['accepted'])
        self.assertFalse(result['round_consumed'])

    def test_tampered_resource_receipt_invalidates_candidate_zero(self):
        result = self.submit()
        resource = Path(result['build']['resource_attestation_path'])
        resource.write_text('{}')
        self.assertFalse(self.controller._dev_gate(result)[0])
        self.assertFalse(candidate_zero_contract_valid(result['dev']['dev_001']))

    def test_compile_zero_can_freeze_and_measure_hidden_without_lower(self):
        result = self.submit()
        self.assertTrue(result['accepted'], result)
        self.controller._freeze_latest(self.controller.records[-1], reason='builder_exit')
        self.controller._write_lifecycle()
        self.assertEqual(_validate_freeze(self.controller.run_dir, self.controller.frozen)[1], [])
        hidden = self.controller.run_hidden()
        self.assertTrue(hidden['formal_complete'], hidden)
        from agentloop.protocol import read_json
        attestation = read_json(Path(hidden['attestation_path']))
        self.assertFalse(attestation['all_cases_real'])
        self.assertTrue(attestation['all_cases_measured_or_causal_zero'])
        self.assertTrue(hidden_evidence_ready_for_finalizer(attestation))
        self.assertEqual(len(hidden['cases']), 6)
        self.assertTrue(all(candidate_zero_contract_valid(row) for row in hidden['cases'].values()))
        self.lower.assert_not_called()
        # Real shared publisher, with no credentials: Result can publish the
        # evidenced zero axis while independent Code stays unavailable.
        from ai_shared_finalize import load_shared, configure
        configure(load_shared(), self.root).main(['--run-dir', str(self.root)])
        aggregation = read_json(self.root / 'formal_aggregation.json')
        self.assertTrue(aggregation['formal_result_publishable'], aggregation)
        self.assertFalse(aggregation['code_score_publishable'])
        self.assertEqual(aggregation['result_axis']['score'], 0)
        self.assertIsNone(aggregation['combined_score'])

    def test_source_changed_after_compilation_cannot_be_frozen_or_scored(self):
        result = self.submit()
        repo = Path(result['attempt_paths']['repository'])
        (repo / 'bad.py').write_text('tampered = True\n')
        self.assertFalse(self.controller._dev_gate(result)[0])
        with self.assertRaisesRegex(RuntimeError, 'changed'):
            self.controller._freeze_latest(self.controller.records[-1], reason='builder_exit')

    def test_second_distinct_compile_failure_retains_feedback_chain(self):
        first = self.submit()
        self.delivery(2)
        second = self.submit()
        self.assertTrue(second['accepted'], second)
        self.assertEqual(second['feedback_digest_ack'], first['feedback_digest'])
        self.assertEqual(len(self.controller.records), 2)
        self.assertNotEqual(first['build']['candidate_repo_digest'], second['build']['candidate_repo_digest'])


if __name__ == '__main__':
    unittest.main()
