"""Real controller persistence controls; no provider, product or model claims."""
import hashlib
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from controller.two_round_controller import TwoRoundController, HIDDEN, tree_digest
from evaluator.formal_gates import validate_hidden_freeze_binding
from evaluator.hidden_executor import run_suite


class ImmutableHiddenFreezeTests(unittest.TestCase):
    def test_formal_subset_is_rejected_before_owned_worker(self):
        with patch('evaluator.suite_runtime.run_hidden_owned') as owned:
            with self.assertRaisesRegex(RuntimeError, 'all six'):
                run_suite(freeze_manifest_path=Path('/not-read'), hidden_root=Path('/not-read'),
                    output=Path('/not-written'), broker_endpoint='unused', runtime=None,
                    timeout_seconds=600, formal=True, case_ids=('test_001',))
            owned.assert_not_called()

    def test_bounded_smoke_uses_same_owned_suite_path(self):
        with patch('evaluator.suite_runtime.run_hidden_owned', return_value={'diagnostic': True}) as owned:
            result = run_suite(freeze_manifest_path=Path('/not-read'), hidden_root=Path('/not-read'),
                output=Path('/not-written'), broker_endpoint='unused', runtime=None,
                timeout_seconds=600, smoke=True, case_ids=('test_001',))
            self.assertEqual(result, {'diagnostic': True})
            self.assertTrue(owned.call_args.kwargs['smoke'])
            self.assertFalse(owned.call_args.kwargs['formal'])
            self.assertEqual(owned.call_args.kwargs['case_ids'], ('test_001',))

    def setup_controller(self, root):
        root.mkdir(parents=True, exist_ok=True)
        candidate = root/'frozen_candidate'
        candidate.mkdir()
        (candidate/'product.py').write_text('print("fixture only")\n')
        (candidate/'product.py').chmod(0o444)
        candidate.chmod(0o555)
        freeze = {'candidate_digest': tree_digest(candidate), 'frozen_at': '2026-09-12T00:00:00+00:00',
                  'hidden_started_at': None, 'hidden_completed_at': None, 'hidden_attestation': None}
        (root/'freeze_manifest.json').write_text(json.dumps(freeze, indent=2)+'\n')
        controller = TwoRoundController(root, lambda *_: {})
        controller.frozen = freeze
        return controller

    def test_success_preserves_freeze_bytes_and_writes_separate_state(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            controller = self.setup_controller(root)
            before = (root/'freeze_manifest.json').read_bytes()
            controller.run_hidden(lambda _: {case: {'case_id': case} for case in HIDDEN})
            self.assertEqual(before, (root/'freeze_manifest.json').read_bytes())
            state = json.loads((root/'hidden_execution_state.json').read_text())
            self.assertEqual(state['state'], 'completed')
            self.assertEqual(state['freeze_manifest_sha256'], hashlib.sha256(before).hexdigest())
            self.assertIsNone(controller.frozen['hidden_started_at'])

    def test_failed_callback_cannot_be_replayed_by_new_controller(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            controller = self.setup_controller(root)
            before = (root/'freeze_manifest.json').read_bytes()
            def failed(_):
                raise RuntimeError('synthetic delivery unknown')
            with self.assertRaisesRegex(RuntimeError, 'unknown'):
                controller.run_hidden(failed)
            resumed = TwoRoundController(root, lambda *_: {})
            resumed.frozen = json.loads(before)
            with self.assertRaisesRegex(RuntimeError, 'replay'):
                resumed.run_hidden(lambda _: self.fail('must not dispatch again'))
            self.assertEqual(before, (root/'freeze_manifest.json').read_bytes())

    def test_manifest_tampering_during_callback_is_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            controller = self.setup_controller(root)
            def tamper(_):
                with (root/'freeze_manifest.json').open('a') as stream:
                    stream.write(' ')
                return {case: {} for case in HIDDEN}
            with self.assertRaisesRegex(RuntimeError, 'immutable freeze'):
                controller.run_hidden(tamper)

    def test_formal_consumer_rejects_changed_freeze_or_incomplete_state(self):
        with tempfile.TemporaryDirectory() as directory:
            run = Path(directory)
            root = run/'lifecycle'
            controller = self.setup_controller(root)
            controller.run_hidden(lambda _: {case: {} for case in HIDDEN})
            state = json.loads((root/'hidden_execution_state.json').read_text())
            suite = {**state, 'freeze_before_hidden': True,
                     'evidence_kind': 'formal', 'case_inventory': list(HIDDEN)}
            (run/'hidden').mkdir()
            (run/'hidden/hidden-after-freeze-attestation.json').write_text(json.dumps(suite))
            self.assertEqual(validate_hidden_freeze_binding(run), [])
            with (root/'freeze_manifest.json').open('a') as stream:
                stream.write(' ')
            self.assertIn('immutable freeze', '; '.join(validate_hidden_freeze_binding(run)))
            state['state'] = 'started'
            (root/'hidden_execution_state.json').write_text(json.dumps(state))
            self.assertIn('incomplete', '; '.join(validate_hidden_freeze_binding(run)))


if __name__ == '__main__':
    unittest.main()
