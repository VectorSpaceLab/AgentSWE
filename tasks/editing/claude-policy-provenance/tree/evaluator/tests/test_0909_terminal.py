"""No-provider regressions for terminal reports and evidence-based timeouts."""
import json
import subprocess
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from agentloop.evaluator import controller, product_lifecycle, semantic_score
from agentloop.protocol import candidate_tree_digest
from evaluator import formal_finalize
from harbor import acceptance_reuse


class TerminalTests(unittest.TestCase):
    def test_outer_timeout_missing_evidence_is_unresolved_not_candidate_zero(self):
        value = self.public_timeout(None)
        self.assertEqual(value['classification'], 'unresolved_execution_failure')
        self.assertIsNone(value['score'])
        self.assertFalse(value['infrastructure_invalid'])
        self.assertFalse(value['round_consumed'])

    def test_outer_timeout_preserves_observed_candidate_failure(self):
        value = self.public_timeout({'classification': 'candidate_zero', 'contract_valid': True},
                                   {'score': 0, 'classification': 'candidate_zero'})
        self.assertEqual(value['score'], 0)
        self.assertEqual(value['score_kind'], 'candidate_zero')
        self.assertTrue(value['outer_timeout'])

    def test_outer_timeout_preserves_provider_failure(self):
        value = self.public_timeout({'classification': 'infrastructure_invalid', 'reason': 'provider failure'})
        self.assertTrue(value['infrastructure_invalid'])
        self.assertIsNone(value['score'])

    def test_outer_timeout_unknown_trajectory_does_not_call_semantic_judge(self):
        value = self.public_timeout({'classification': 'unresolved', 'reason': 'insufficient attribution'})
        self.assertEqual(value['classification'], 'unresolved_execution_failure')
        self.assertIsNone(value['score'])

    def public_timeout(self, verdict, zero=None):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            candidate = root / 'candidate'
            candidate.mkdir()
            (candidate / 'source.py').write_text('pass\n')
            instance = object.__new__(controller.Controller)
            instance.run_dir, instance.cases = root / 'run', Path(__file__).resolve().parents[2] / 'agentloop/cases'
            instance.product_attempts = controller.ProductAttempts(instance.run_dir / 'product_attempts')
            instance.product_attempts.claim(candidate_tree_digest(candidate), {'test_fixture': True})
            instance.public_case_ids = ('dev_001',)
            instance.broker_endpoint, instance.judge_endpoint = 'unused', 'unused'
            def timeout(*args, output, **kwargs):
                output.mkdir(parents=True, exist_ok=True)
                if verdict is not None:
                    (output / 'trajectory.json').write_text(json.dumps({'case_id': 'dev_001',
                        'candidate_digest': candidate_tree_digest(candidate), 'classification': 'native_observation'}))
                raise subprocess.TimeoutExpired(['scoped-launcher'], 600)
            with mock.patch.object(product_lifecycle, 'run_scoped_launcher', side_effect=timeout), \
                 mock.patch.object(semantic_score, 'execution_verdict', return_value=(verdict, zero)), \
                 mock.patch.object(semantic_score, 'judge_evidence') as judge:
                values = instance._run_public(candidate, 1)
                judge.assert_not_called()
            self.assertEqual(len(values), 1)
            return values[0]

    def test_invalid_result_contract_survives_as_structured_exception(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            artifact = {'case_id': 'test_001'}
            trace = {'case_id': 'test_001', 'answer': artifact, 'binding': {},
                     'artifact': {'origin': 'lower_model_final_response', 'evaluator_synthesized': False}}
            files = {name: root / name for name in ('artifact', 'trajectory', 'task', 'rubric', 'native', 'oracle')}
            for name, path in files.items():
                path.write_text(json.dumps(artifact if name == 'artifact' else trace if name == 'trajectory' else {}))
            original = {'case_id': 'test_001', 'contract_valid': False, 'evaluation_state': 'infrastructure_error',
                        'provider_usage': {'logical_requests': 1, 'completed_responses': 0}, 'errors': ['HTTP 502']}
            output = root / 'judge'
            output.mkdir()
            contract_path = output / 'result_score_contract.json'
            contract_path.write_text(json.dumps(original))
            before = contract_path.read_bytes()
            with mock.patch.object(semantic_score, 'valid_agent_result', return_value=True), \
                 mock.patch.object(semantic_score.subprocess, 'run', return_value=subprocess.CompletedProcess([], 1, '', '')):
                with self.assertRaises(semantic_score.SemanticMeasurementUnavailable) as caught:
                    semantic_score.judge_evidence(case_id='test_001', artifact=files['artifact'],
                        trajectory=files['trajectory'], task_input=files['task'], rubric=files['rubric'],
                        native=files['native'], oracle=files['oracle'], endpoint='unused', output=output)
            self.assertEqual(caught.exception.contract, original)
            self.assertEqual(contract_path.read_bytes(), before)

    def test_acceptance_exception_writes_terminal_summary_and_cleans_owned_brokers(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            run = root / 'run'
            run.mkdir()
            args = SimpleNamespace(run_dir=run, delivery=root / 'delivery', cases=['test_001'],
                hidden_cases_dir=root / 'cases', resume_preflight=True, credential_file=root / 'no-real-credential')
            with mock.patch.object(acceptance_reuse, 'validate_delivery', return_value=[]), \
                 mock.patch.object(acceptance_reuse, 'load_case_bundle'), \
                 mock.patch.object(acceptance_reuse, 'resume_preflight', return_value=(root / 'frozen', 'd' * 64)), \
                 mock.patch.object(acceptance_reuse, 'start_acceptance_lower'), \
                 mock.patch.object(acceptance_reuse, 'run_hidden', side_effect=RuntimeError('fixture failure')), \
                 mock.patch.object(acceptance_reuse, 'broker_stats', return_value={'calls': 0}), \
                 mock.patch.object(acceptance_reuse, 'cleanup_owned_container', return_value={'absent_after_cleanup': True}) as cleanup:
                self.assertEqual(acceptance_reuse.run(args), 2)
            value = json.loads((run / 'summary.json').read_text())
            self.assertEqual(value['failed_phase'], 'hidden_execution')
            self.assertFalse(value['formal_result_publishable'])
            self.assertFalse(value['automatic_retry'])
            self.assertEqual(value['result_axis'], 'N/A')
            self.assertEqual(cleanup.call_count, 2)
            self.assertTrue(json.loads((run / 'cleanup_attestation.json').read_text())['cleanup_complete'])

    def test_legacy_external_code_dimensions_fail_closed(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            path = root / 'old.json'
            path.write_text(json.dumps({'candidate_digest': 'd' * 64, 'dimensions': {}}))
            with self.assertRaisesRegex(ValueError, 'native Create'):
                formal_finalize.validate_code_contract(root, path, expected_digest='d' * 64)

    def test_external_native_code_contract_preserves_score_and_checks_proofs(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            usage = dict(logical_requests=1, transport_attempts=1, completed_responses=1,
                         input_tokens=7, output_tokens=3, total_tokens=10)
            contract = {'schema_version': '0825-code-score-contract-v3', 'score_policy': 'raw_score_final',
                'contract_valid': True, 'code_score_publishable': True, 'errors': [],
                'candidate_digest': 'd' * 64, 'public_requirements_digest': 'e' * 64,
                'code_rubric_digest': formal_finalize.sha256(formal_finalize.ROOT / 'evaluator/code_rubric.md'),
                'judge': {'model': 'gpt-5.6-sol', 'reasoning_effort': 'max'},
                'provider_usage': usage, 'code_raw_score': 8, 'code_score': 8,
                'code_dimensions': {key: {'score': 1, 'max': weight, 'evidence': 'a.py:1',
                    'verified_citations': [{'path': 'a.py', 'start_line': 1, 'end_line': 1}]}
                    for key, weight in formal_finalize.CODE_WEIGHTS.items()}}
            records = {'source_manifest.json': {'tree_digest': 'd' * 64,
                'files': [{'path': 'a.py', 'type': 'text', 'line_count': 1, 'included_in_evidence_pack': True}]},
                'public_requirements_manifest.json': {'tree_digest': 'e' * 64},
                'code_judge_invocation.json': {'schema_version': 'agentswe-edit-code-judge-invocation-v1',
                    'authoritative_judge': '@@AGENTSWE_LEGACY_HARBOR@@/0825-10create-v4/code_eval.py',
                    'candidate_mount': 'read-only', 'public_requirements_mount': 'read-only', 'rubric_mount': 'read-only',
                    'exit_code': 0, 'credential_values_recorded': False},
                'code_judge_lifecycle.json': {'cleanup': {'complete': True}},
                'code_transport_ledger.json': usage}
            for name, value in records.items():
                (root / name).write_text(json.dumps(value))
            contract['source_manifest_digest'] = formal_finalize.sha256(root / 'source_manifest.json')
            path = root / 'code_score_contract.json'
            path.write_text(json.dumps(contract))
            before = path.read_bytes()
            value = formal_finalize.validate_code_contract(root, path, expected_digest='d' * 64)
            self.assertEqual(value['code_score'], 8)
            self.assertEqual(value['code_dimensions'], contract['code_dimensions'])
            self.assertEqual(path.read_bytes(), before)
            for key, invalid in [('code_score', 7), ('candidate_digest', 'f' * 64),
                                 ('code_dimensions', {**contract['code_dimensions'], 'extra': {}})]:
                path.write_text(json.dumps({**contract, key: invalid}))
                with self.assertRaises(ValueError):
                    formal_finalize.validate_code_contract(root, path, expected_digest='d' * 64)
            path.write_text(json.dumps(contract))
            (root / 'code_transport_ledger.json').write_text(json.dumps({**usage, 'logical_requests': 2}))
            with self.assertRaisesRegex(ValueError, 'invocation/transport'):
                formal_finalize.validate_code_contract(root, path, expected_digest='d' * 64)


if __name__ == '__main__':
    unittest.main()
