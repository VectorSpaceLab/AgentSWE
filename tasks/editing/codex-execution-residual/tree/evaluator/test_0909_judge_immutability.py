"""Zero-provider behavioral checks for the task's judge boundary."""
import hashlib
import json
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from evaluator import formal_finalize as finalizer


class JudgeImmutabilityTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.output = self.root / 'score'
        self.input = self.root / 'input.txt'
        self.input.write_text('public input')
        self.command = ['python', '/mock/evaluator_judge.py']

    def tearDown(self):
        self.temporary.cleanup()

    def invoke(self, *, exit_code=0, body=None):
        raw = json.dumps(body if body is not None else {'contract_valid': True}, indent=3) + '\n'
        def cli(*args, **kwargs):
            (self.output / 'result_score_contract.json').write_text(raw)
            return subprocess.CompletedProcess(args[0], exit_code, 'stdout', 'stderr')
        with mock.patch.object(finalizer.subprocess, 'run', side_effect=cli) as run:
            evidence = finalizer.invoke_once(self.command, self.output, [self.input])
        return evidence, raw, run.call_count

    def test_success_preserves_raw_bytes_and_records_separate_invocation(self):
        evidence, raw, count = self.invoke(body={'contract_valid': True, 'case_id': 'test_001'})
        contract = finalizer.checked_raw_contract(self.output, 'result_score_contract.json', evidence)
        self.assertEqual(count, 1)
        self.assertEqual(contract, json.loads(raw))
        self.assertNotIn('judge_invocation', contract)
        self.assertEqual((self.output / 'result_score_contract.json').read_text(), raw)
        self.assertTrue((self.output / 'task_judge_invocation.json').is_file())
        self.assertEqual((self.output / 'task_cli_stdout.log').read_text(), 'stdout')

    def test_existing_phase_refuses_before_subprocess_and_preserves_old_contract(self):
        self.invoke()
        path = self.output / 'result_score_contract.json'
        before = path.read_bytes()
        with mock.patch.object(finalizer.subprocess, 'run') as run:
            with self.assertRaises(FileExistsError):
                finalizer.invoke_once(self.command, self.output, [self.input])
        run.assert_not_called()
        self.assertEqual(path.read_bytes(), before)

    def test_nonzero_cli_cannot_publish_valid_looking_contract(self):
        evidence, raw, _ = self.invoke(exit_code=2, body={'contract_valid': True, 'result_score_publishable': True})
        result = finalizer.checked_raw_contract(self.output, 'result_score_contract.json', evidence)
        self.assertFalse(result['contract_valid'])
        self.assertFalse(result['result_score_publishable'])
        self.assertEqual(result['raw_contract_sha256'], hashlib.sha256(raw.encode()).hexdigest())
        self.assertEqual((self.output / 'result_score_contract.json').read_text(), raw)

    def test_bad_json_retained_without_synthetic_replacement(self):
        evidence, _, _ = self.invoke()
        path = self.output / 'result_score_contract.json'
        path.write_text('{ malformed provider contract')
        result = finalizer.checked_raw_contract(self.output, path.name, evidence)
        self.assertFalse(result['contract_valid'])
        self.assertEqual(path.read_text(), '{ malformed provider contract')

    def test_code_identity_mismatch_is_wrapper_not_raw_mutation(self):
        evidence, raw, _ = self.invoke(body={'contract_valid': True, 'candidate_digest': 'different'})
        result = finalizer.checked_raw_contract(self.output, 'result_score_contract.json', evidence, expected_digest='expected')
        self.assertFalse(result['code_score_publishable'])
        self.assertEqual((self.output / 'result_score_contract.json').read_text(), raw)

    def test_transport_exception_claims_phase_and_no_automatic_reentry(self):
        with mock.patch.object(finalizer.subprocess, 'run', side_effect=OSError('mock unavailable')) as run:
            evidence = finalizer.invoke_once(self.command, self.output, [self.input])
        self.assertEqual(run.call_count, 1)
        self.assertIsNone(evidence['exit_code'])
        self.assertTrue((self.output / 'task_judge_intent.json').is_file())
        result = finalizer.checked_raw_contract(self.output, 'result_score_contract.json', evidence)
        self.assertFalse(result['contract_valid'])
        self.assertFalse((self.output / 'result_score_contract.json').exists())

    def test_missing_input_never_invokes_judge(self):
        self.input.unlink()
        with mock.patch.object(finalizer.subprocess, 'run') as run:
            with self.assertRaises(FileNotFoundError):
                finalizer.invoke_once(self.command, self.output, [self.input])
        run.assert_not_called()
        self.assertFalse(self.output.exists())

    def test_public_input_reuse_identical_only_and_symlinks_refused(self):
        finalizer.immutable_text(self.input, 'public input')
        with self.assertRaisesRegex(ValueError, 'changed'):
            finalizer.immutable_text(self.input, 'changed requirements')
        self.assertEqual(self.input.read_text(), 'public input')
        link = self.root / 'link'
        link.symlink_to(self.input)
        with self.assertRaisesRegex(ValueError, 'symlink'):
            finalizer.immutable_text(link, 'public input')

    def test_usage_totals_must_match(self):
        usage = {'input_tokens': 10, 'output_tokens': 4, 'total_tokens': 14, 'transport_attempts': 1}
        self.assertTrue(finalizer.valid_judge_usage(usage))
        self.assertFalse(finalizer.valid_judge_usage(dict(usage, total_tokens=10)))


if __name__ == '__main__':
    unittest.main()
