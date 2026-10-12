import copy
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import result_judge as judge


def answer(total=63):
    scores = (31, 20, 12) if total == 63 else (15, 10, 5)
    return {
        'case_id': 'test_001', 'result_state': 'scoreable', 'result_score': sum(scores),
        **{name: {'score': score, 'max': maximum, 'evidence': 'actual task evidence'}
           for (name, maximum), score in zip(judge.COMPONENT_MAXIMA.items(), scores)},
        'major_errors': ['partial delivery'], 'assessment': 'evidence-grounded assessment',
    }


class ResultScoreCaps(unittest.TestCase):
    def fixture(self, root, status='violated'):
        inputs = {}
        for key in ('task_input', 'rubric', 'agent_artifact', 'trajectory', 'native_evidence', 'oracle_summary'):
            path = root / (key + '.md')
            path.write_text('{}' if key not in {'task_input', 'rubric'} else 'public requirement')
            inputs[key] = path
        contract = {'schema_version': 'agentswe-result-score-caps/v1', 'case_id': 'test_001',
                    **{k + '_sha256': judge.sha256_file(inputs[k])
                       for k in ('rubric', 'native_evidence', 'oracle_summary')},
                    'entries': [{'cap_id': 'existing_science_fence', 'maximum_score': 35,
                                 'status': status, 'requirement_ref': 'public constraints section 26',
                                 'reason': 'specific independently observed mismatch',
                                 'evidence_refs': ['oracle_summary.science_gate_check']}]}
        path = root / 'caps.json'
        path.write_text(json.dumps(contract))
        return inputs, path, contract

    def test_cap_checks_without_clamping_or_rescoring(self):
        verified, errors = judge.validate_response(json.dumps(answer()), 'test_001', score_cap=35)
        self.assertEqual(verified['result_score'], 63)
        self.assertTrue(any('ceiling 35' in error for error in errors))
        self.assertFalse(judge.validate_response(json.dumps(answer(30)), 'test_001', score_cap=35)[1])
        self.assertFalse(judge.validate_response(json.dumps(answer()), 'test_001')[1])

    def test_cap_contract_binds_all_scoring_evidence(self):
        with tempfile.TemporaryDirectory() as tmp:
            inputs, path, _ = self.fixture(Path(tmp))
            self.assertEqual(judge.load_score_caps(path, 'test_001', inputs)[0], 35)
            inputs['native_evidence'].write_text('changed native facts')
            with self.assertRaisesRegex(ValueError, 'input binding mismatch'):
                judge.load_score_caps(path, 'test_001', inputs)

    def test_wrong_case_and_unsupported_conditions_fail_closed(self):
        with tempfile.TemporaryDirectory() as tmp:
            inputs, path, base = self.fixture(Path(tmp))
            with self.assertRaisesRegex(ValueError, 'case identity'):
                judge.load_score_caps(path, 'test_002', inputs)
            for field, value in (('maximum_score', True), ('status', 'pass'), ('evidence_refs', [])):
                changed = copy.deepcopy(base)
                changed['entries'][0][field] = value
                path.write_text(json.dumps(changed))
                with self.assertRaises(ValueError):
                    judge.load_score_caps(path, 'test_001', inputs)

    def test_unknown_is_not_a_proven_violation_or_a_clean_bill(self):
        with tempfile.TemporaryDirectory() as tmp:
            inputs, path, _ = self.fixture(Path(tmp), 'unavailable')
            maximum, contract = judge.load_score_caps(path, 'test_001', inputs)
            self.assertIsNone(maximum)
            self.assertEqual(contract['entries'][0]['status'], 'unavailable')

    def test_semantic_judgment_must_be_explicit_and_respect_its_ceiling(self):
        caps = {'existing_science_fence': 35}
        value = answer()
        self.assertTrue(judge.validate_response(json.dumps(value), 'test_001', semantic_caps=caps)[1])
        value['ceiling_assessments'] = {'existing_science_fence': {
            'violated': False, 'evidence': 'scientifically correct paraphrase, not literal keyword match'}}
        self.assertFalse(judge.validate_response(json.dumps(value), 'test_001', semantic_caps=caps)[1])
        value['ceiling_assessments']['existing_science_fence']['violated'] = True
        self.assertTrue(judge.validate_response(json.dumps(value), 'test_001', semantic_caps=caps)[1])

    def run_cli(self, root, *, bind_error=False):
        inputs, path, _ = self.fixture(root)
        if bind_error:
            inputs['rubric'].write_text('changed rubric')
        argv = ['result_judge.py', '--case-id', 'test_001', '--broker-endpoint',
                'http://127.0.0.1:1/v1/responses', '--output-dir', str(root / 'output'),
                '--score-cap-contract', str(path), '--transport-mode', 'nonstream']
        for key, source in inputs.items():
            argv += ['--' + key.replace('_', '-'), str(source)]
        body = {'status': 'completed', 'model': judge.MODEL, 'output_text': json.dumps(answer()),
                'usage': {'input_tokens': 10, 'output_tokens': 5, 'total_tokens': 15}}
        response = type('Response', (), {'status_code': 200, 'headers': {}, 'raw': None, 'text': json.dumps(body),
                                         'json': lambda self: body})()
        with patch('sys.argv', argv), patch.object(judge.requests, 'post', return_value=response) as post, \
                patch('builtins.print'):
            exit_code = judge.main()
            before = (root / 'output/result_score_contract.json').read_bytes()
            self.assertEqual(judge.main(), 2)
            self.assertEqual((root / 'output/result_score_contract.json').read_bytes(), before)
        return exit_code, json.loads(before), post

    def test_actual_cli_rejects_completed_over_cap_response_without_resampling(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            code, contract, post = self.run_cli(root)
            self.assertEqual(code, 1)
            self.assertEqual(post.call_count, 1)
            self.assertEqual(contract['evaluation_state'], 'model_output_invalid')
            self.assertIsNone(contract['result_score'])
            self.assertEqual(contract['provider_usage']['completed_responses'], 1)
            self.assertEqual(contract['provider_usage']['logical_requests'], 1)
            raw = json.loads((root / 'output/result_eval_result.json').read_text())
            self.assertEqual(raw['result_score'], 63)
            prompt = post.call_args.kwargs['json']['input'][0]['content'][0]['text']
            self.assertIn('Effective evidenced ceiling: 35', prompt)

    def test_changed_cap_bound_input_fails_before_any_provider_request(self):
        with tempfile.TemporaryDirectory() as tmp:
            _, contract, post = self.run_cli(Path(tmp), bind_error=True)
            post.assert_not_called()
            self.assertEqual(contract['provider_usage']['logical_requests'], 0)
            self.assertFalse(contract['result_score_publishable'])


if __name__ == '__main__':
    unittest.main()
