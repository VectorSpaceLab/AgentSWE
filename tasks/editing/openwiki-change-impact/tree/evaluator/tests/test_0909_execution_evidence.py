from __future__ import annotations
import json
import sys
import tempfile
import unittest
from pathlib import Path
ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
from agentloop.evaluator.execution_evidence import artifact_authorship, attest


class ExecutionEvidenceTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.output = Path(self.tmp.name)
        (self.output / 'workspace').mkdir()
        self.artifact = {'schema_version': 'openwiki-agent-result/v1', 'case_id': 'test_001',
            'observations': [], 'integrity': {}, 'decision': {'completion_claim': 'incomplete'}}
        self.run = {'classification': 'candidate_product_success', 'product_started': True,
            'broker_calls': 1, 'broker_successful_calls': 1, 'exit_code': 0,
            'semantic_observer_in_case_budget': True,
            'broker_delta': {'calls': 1, 'successful_calls': 1}, 'infrastructure_invalid': False}
        (self.output / 'launcher_result.json').write_text(json.dumps(self.run))
        (self.output / 'transport_preflight.json').write_text('{"valid":true}')

    def tearDown(self):
        self.tmp.cleanup()

    def artifact_files(self):
        text = json.dumps(self.artifact)
        (self.output / 'workspace/agent_result.json').write_text(text)
        (self.output / 'stdout.log').write_text('Earlier model text\n' + text + '\n')

    def test_actual_terminal_json_required(self):
        self.artifact_files()
        self.assertTrue(artifact_authorship(self.output, 'test_001')['valid'])
        (self.output / 'stdout.log').write_text('merely a CLI event label')
        self.assertFalse(artifact_authorship(self.output, 'test_001')['valid'])

    def test_wrong_case_and_preexisting_artifact_rejected(self):
        self.artifact_files()
        wrong=artifact_authorship(self.output, 'test_002')
        self.assertTrue(wrong['valid'])
        self.assertFalse(wrong['format_valid'])
        self.assertFalse(artifact_authorship(self.output, 'test_001', preexisting=True)['valid'])

    def test_real_case_binds_digest_and_native_provenance(self):
        self.artifact_files()
        record = attest(self.run, case_id='test_001', candidate_digest='source-sha', output=self.output)
        self.assertTrue(record['artifact_validation']['valid'])
        self.assertTrue(record['real_execution'])
        self.assertEqual(record['candidate_digest'], 'source-sha')

    def test_endpoint_mapping_failure_is_infrastructure_not_zero(self):
        self.artifact_files()
        (self.output / 'transport_preflight.json').write_text('{"valid":true,"endpoint_mapping_errors":[{"path":"/bad"}]}')
        record = attest(self.run, case_id='test_001', candidate_digest='source-sha', output=self.output)
        self.assertEqual(record['classification'], 'infrastructure_invalid')
        self.assertFalse(record['environment_preflight']['valid'])

    def test_missing_budgeted_observer_cannot_be_scored_as_completed(self):
        self.artifact_files()
        self.run['semantic_observer_in_case_budget'] = False
        record = attest(self.run, case_id='test_001', candidate_digest='source-sha', output=self.output)
        self.assertEqual(record['classification'], 'infrastructure_invalid')

    def test_observed_partial_artifact_remains_scoreable_on_cleanup_timeout(self):
        self.artifact_files()
        self.run['case_resource_contract'] = {'timed_out': True}
        record = attest(self.run, case_id='test_001', candidate_digest='source-sha', output=self.output)
        self.assertEqual(record['classification'], 'candidate_product_success')

    def test_positive_process_failure_may_be_attributed_but_missing_process_may_not(self):
        record = attest(self.run, case_id='test_001', candidate_digest='source-sha', output=self.output)
        self.assertEqual(record['classification'], 'candidate_behavior_failure')
        self.assertEqual(record['failure_attribution']['party'], 'candidate')
        self.assertTrue(record['failure_attribution']['evidence_paths'])
        record = attest({'classification': 'unknown', 'product_started': False},
            case_id='test_001', candidate_digest='source-sha', output=self.output)
        self.assertNotEqual(record['classification'], 'candidate_behavior_failure')


if __name__ == '__main__':
    unittest.main()
