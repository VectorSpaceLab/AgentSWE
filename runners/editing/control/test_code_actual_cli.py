"""Full unchanged Create CLI + Edit entry, with HTTP mocked and sockets denied.

These are synthetic evaluator regression contracts, never benchmark scores or
real model usage evidence. The actual Create source must exist; no skips.
"""
import contextlib
import io
import json
import os
from pathlib import Path
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch, Mock

import code_judge_entry as entry

CREATE = Path('@@AGENTSWE_LEGACY_HARBOR@@/0825-10create-v4/code_eval.py')


class ActualCreateCliTests(unittest.TestCase):
    def exercise(self, *, malformed=False, missing_usage=False, wrong_digest=False, disconnected=False):
        with tempfile.TemporaryDirectory(prefix='synthetic-code-cli-') as directory:
            root = Path(directory)
            source, requirements, output = (root / name for name in ('source', 'requirements', 'output'))
            for path in (source, requirements, output):
                path.mkdir()
            (source / 'entry.py').write_text('def run():\n    return 1\n')
            (requirements / 'task.md').write_text('Synthetic regression input, not a benchmark task.\n')
            rubric = root / 'rubric.md'
            rubric.write_text('Synthetic regression rubric; validate the unchanged Code contract.\n')
            credential = root / 'credential-fixture.env'
            credential.write_text('DEEPSEEK_API_KEY=synthetic-fixture-not-a-real-credential\n')
            create = entry.load_create(CREATE)
            digest = create.tree_digest(source)
            dimensions = {key: {'score': maximum // 2, 'max': maximum,
                'evidence': 'entry.py:1-2 is a synthetic citation used to test the original validator.'}
                for key, maximum in create.CODE_MAXIMA.items()}
            score = sum(row['score'] for row in dimensions.values())
            value = {'code_state': 'scoreable', 'code_dimensions': dimensions,
                     'code_raw_score': score, 'code_score': score, 'code_applied_caps': [],
                     'code_major_errors': [], 'code_assessment': 'Synthetic mocked response, not a real judgement.'}
            body = {'id': 'synthetic-response', 'status': 'completed', 'model': 'deepseek-flash',
                    'output_text': 'invalid score JSON' if malformed else json.dumps(value),
                    'usage': {} if missing_usage else {'input_tokens': 30, 'output_tokens': 12, 'total_tokens': 42}}
            class SyntheticHTTP:
                code = 200
                headers = {'Content-Type': 'application/json'}
                def __init__(self): self.raw = io.BytesIO(json.dumps(body).encode())
                def read1(self, size):
                    if disconnected:
                        raise ConnectionResetError('synthetic partial stream disconnect')
                    return self.raw.read1(size)
                def __enter__(self): return self
                def __exit__(self, *_): self.raw.close()
            opener = SimpleNamespace(open=Mock(return_value=SyntheticHTTP()))
            argv = ['code_judge_entry.py', '--candidate-source', str(source), '--public-requirements', str(requirements),
                    '--code-rubric', str(rubric), '--credential-file', str(credential), '--output-dir', str(output),
                    '--expected-candidate-digest', '0' * 64 if wrong_digest else digest]
            with patch.object(entry, 'load_create', return_value=create), patch.object(sys, 'argv', argv), \
                    patch.dict(os.environ, {'AGENTSWE_CODE_JUDGE_PREFLIGHT': '0'}), \
                    patch('socket.socket.connect', side_effect=AssertionError('network forbidden in regression')), \
                    patch.object(entry.transport, 'direct_opener', return_value=opener), \
                    contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
                result = entry.main()
                contract = json.loads((output / 'code_score_contract.json').read_text())
                raw_contract = (output / 'code_score_contract.json').read_bytes()
                second = entry.main()
                self.assertEqual(second, 2, 'reentry must preserve an existing contract even before provider use')
                self.assertEqual((output / 'code_score_contract.json').read_bytes(), raw_contract)
                if opener.open.call_count:
                    sent = json.loads(opener.open.call_args.args[0].data)
                    self.assertIs(sent['stream'], True)
            return {'exit_code': result, 'reentry_code': second, 'contract': contract,
                    'mock_http_calls': opener.open.call_count, 'model_response_002': (output / 'code_model_response_002.json').exists(),
                    'candidate_digest': digest, 'expected_score': score}

    def test_valid_full_cli_contract(self):
        result = self.exercise()
        self.assertEqual(result['exit_code'], 0)
        self.assertEqual(result['mock_http_calls'], 1)
        self.assertEqual(result['reentry_code'], 2)
        self.assertTrue(result['contract']['code_score_publishable'])
        self.assertEqual(result['contract']['code_score'], result['expected_score'])
        self.assertEqual(result['contract']['candidate_digest'], result['candidate_digest'])
        self.assertEqual(result['contract']['provider_usage']['total_tokens'], 42)

    def test_completed_invalid_json_not_resampled(self):
        result = self.exercise(malformed=True)
        self.assertEqual(result['mock_http_calls'], 1)
        self.assertNotEqual(result['exit_code'], 0)
        self.assertFalse(result['contract']['contract_valid'])
        self.assertFalse(result['model_response_002'])
        self.assertEqual(result['contract']['provider_usage']['logical_requests'], 1)
        self.assertEqual(result['contract']['provider_usage']['completed_responses'], 1)
        self.assertEqual(result['contract']['provider_usage']['total_tokens'], 42)

    def test_missing_usage_cannot_publish(self):
        result = self.exercise(missing_usage=True)
        self.assertEqual(result['mock_http_calls'], 1)
        self.assertFalse(result['contract']['contract_valid'])
        self.assertIsNone(result['contract']['code_score'])
        self.assertIsNone(result['contract']['provider_usage']['total_tokens'])

    def test_uncertain_stream_full_cli_keeps_usage_null_and_no_reentry(self):
        result = self.exercise(disconnected=True)
        self.assertEqual(result['mock_http_calls'], 1)
        self.assertFalse(result['contract']['contract_valid'])
        self.assertIsNone(result['contract']['code_score'])
        self.assertIsNone(result['contract']['provider_usage']['total_tokens'])
        self.assertEqual(result['contract']['provider_usage']['completed_responses'], 0)
        self.assertEqual(result['contract']['provider_usage']['unknown_usage_attempts'], 1)

    def test_wrong_frozen_digest_never_calls_http(self):
        result = self.exercise(wrong_digest=True)
        self.assertEqual(result['mock_http_calls'], 0)
        self.assertFalse(result['contract']['contract_valid'])


if __name__ == '__main__':
    unittest.main()
