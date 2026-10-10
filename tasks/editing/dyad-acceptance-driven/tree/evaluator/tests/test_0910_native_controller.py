"""Candidate identity and no-replay boundaries; scripted dev only."""
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[2]
sys.path[:0] = [str(ROOT), str(ROOT / 'harbor')]
from harbor import formal_one_stop as formal

class ControllerTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(); self.addCleanup(self.tmp.cleanup)
        self.run = Path(self.tmp.name); self.workspace = self.run / 'submission'; self.workspace.mkdir()
        (self.workspace / 'solution.patch').write_text('first')
        self.life = formal.Lifecycle(run_dir=self.run, workspace=self.workspace, public_endpoint='unused', baseline={})
        self.addCleanup(self.life.close)
        self.calls = []; self.builds = []
        self.addCleanup(patch.stopall)
        patch.object(self.life, 'bind_native_thread').start()
        patch.object(self.life, 'preflight', side_effect=self.build).start()
        patch.object(self.life, '_run_public_case', side_effect=self.case).start()

    def build(self, trigger):
        candidate = self.run / 'build' / str(len(self.builds)); candidate.mkdir(parents=True)
        (candidate / 'product').write_bytes((self.workspace / 'solution.patch').read_bytes())
        self.builds.append(candidate)
        return {'ready_for_submission': True, 'candidate_path': str(candidate), 'candidate_digest': formal.tree_digest(candidate)}

    def case(self, candidate, case_id, endpoint, destination):
        self.calls.append(case_id)
        return {'case_id': case_id, 'classification': 'candidate_failure', 'product_entry_observed': True,
            'broker_calls_delta': 1, 'broker_successful_calls_delta': 1, 'private_canary': 'PRIVATE_CANARY', 'semantic_feedback': {'classification': 'scoreable', 'score': 31, 'contract_valid': True, 'round_consumed': True, 'assessment': 'Scripted judge feedback for controller test'}}

    def test_exact_duplicate_without_ack_skips_build_and_dev(self):
        first = self.life.submit(None, 'unused')
        duplicate = self.life.submit(None, 'unused')
        self.assertEqual(first[0], 200); self.assertEqual(duplicate[0], 200)
        self.assertTrue(duplicate[1]['idempotent']); self.assertEqual(len(self.builds), 1)
        self.assertEqual(len(self.calls), 2); self.assertEqual(len(self.life.records), 1)
        self.assertNotIn('PRIVATE_CANARY', json.dumps(duplicate))

    def test_unknown_attempt_is_durable_and_not_replayed(self):
        def incomplete(*args):
            self.calls.append(args[1])
            if args[1] == 'dev_002': raise TimeoutError('unknown response')
            return {'classification': 'infrastructure-invalid'}
        with patch.object(self.life, '_run_public_case', side_effect=incomplete):
            with self.assertRaises(TimeoutError): self.life.submit(None, 'unused')
        previous = len(self.calls)
        result = self.life.submit(None, 'unused')
        self.assertEqual(result[0], 503); self.assertEqual(len(self.calls), previous)
        self.assertEqual(len(self.life.records), 0)
        saved = json.loads(next((self.run / 'public_attempts').glob('*.json')).read_text())
        self.assertEqual(saved['state'], 'in_progress'); self.assertEqual(len(saved['dev']), 1)
        (self.workspace / 'solution.patch').write_text('revised')
        self.assertEqual(self.life.submit(None, 'unused')[0], 200)
        self.assertEqual(len(self.calls), previous + 2)

    def test_infra_completed_case_is_saved_and_same_product_not_replayed(self):
        with patch.object(self.life, '_public_valid', return_value=False):
            result = self.life.submit(None, 'unused')
        self.assertEqual(result[0], 503)
        self.assertEqual(self.life.submit(None, 'unused')[0], 503)
        self.assertEqual(len(self.calls), 2)
        saved = json.loads(next((self.run / 'public_attempts').glob('*.json')).read_text())
        self.assertEqual(len(saved['dev']), 2)

    def test_product_duplicate_with_metadata_change_is_cached(self):
        first = self.life.submit(None, 'unused')
        (self.workspace / 'edit_report.json').write_text('{}')
        result = self.life.submit(first[1]['feedback_digest'], 'unused')
        self.assertEqual(result[0], 200); self.assertTrue(result[1]['idempotent'])
        self.assertEqual(len(self.calls), 2)

    def test_revision_requires_previous_feedback(self):
        first = self.life.submit(None, 'unused')
        (self.workspace / 'solution.patch').write_text('revised')
        self.assertEqual(self.life.submit(None, 'unused')[0], 422)
        self.assertEqual(self.life.submit(first[1]['feedback_digest'], 'unused')[0], 200)
        self.assertEqual(len(self.life.records), 2)

    def test_infra_same_product_with_changed_git_metadata_and_reports_is_not_replayed(self):
        def git_build(trigger):
            result = self.build(trigger)
            candidate = Path(result['candidate_path']); (candidate/'.git').mkdir()
            (candidate/'.git/index').write_text(str(len(self.builds)))
            result['candidate_digest'] = formal.tree_digest(candidate)
            return result
        with patch.object(self.life, 'preflight', side_effect=git_build):
            with patch.object(self.life, '_public_valid', return_value=False):
                self.assertEqual(self.life.submit(None, 'unused')[0], 503)
            (self.workspace/'edit_report.json').write_text('{"metadata":2}')
            self.assertEqual(self.life.submit(None, 'unused')[0], 503)
        self.assertEqual(len(self.calls), 2)
        self.assertNotEqual(formal.tree_digest(self.builds[0]), formal.tree_digest(self.builds[1]))

    def test_long_run_uses_short_socket(self):
        self.assertLess(len(str(self.life.socket_path).encode()), 108)
        self.assertNotEqual(self.life.socket_path.parent, self.run)

    def test_compile_diagnostics_keep_source_errors_without_private_paths(self):
        feedback = self.life.public_preflight({'candidate_path': '/data/private/candidate',
            'candidate_typecheck': {'exit_code': 2, 'stdout_tail': 'src/example.ts:12 type mismatch',
                'stderr_tail': '/home/private/cache/file failed', 'command': ['private']}})
        self.assertEqual(feedback['candidate_typecheck']['exit_code'], 2)
        self.assertIn('src/example.ts:12 type mismatch', json.dumps(feedback))
        self.assertNotIn('/home/private', json.dumps(feedback)); self.assertNotIn('/data/private', json.dumps(feedback))

    def test_initial_intent_cannot_replace_an_existing_attempt(self):
        path = self.run / 'public_attempts/test.json'
        self.life.write_attempt(path, {'state': 'reserved'}, initial=True)
        with self.assertRaises(FileExistsError): self.life.write_attempt(path, {'state': 'replaced'}, initial=True)
        self.assertEqual(json.loads(path.read_text()), {'state': 'reserved'})

if __name__ == '__main__': unittest.main()
