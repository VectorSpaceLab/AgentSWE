"""A failed relay may stop only the exact evaluator-owned lower container."""
import json,subprocess,tempfile,unittest
from pathlib import Path
from unittest.mock import patch
from evaluator.harness.turn_lifecycle import stop_owned_turn

class TurnOwnershipTests(unittest.TestCase):
    def test_missing_or_malformed_private_cid_cannot_control_a_process(self):
        with tempfile.TemporaryDirectory() as d,patch('evaluator.harness.turn_lifecycle.subprocess.run') as run:
            p=Path(d)/'turn.cid'
            for value in ('','unrelated-name','abc'):
                p.write_text(value)
                with self.assertRaises(RuntimeError):stop_owned_turn(['docker','run','--name','owned'],p)
            run.assert_not_called()

    def test_existing_foreign_name_is_never_killed(self):
        with tempfile.TemporaryDirectory() as d:
            p=Path(d)/'turn.cid';p.write_text('a'*64)
            response=subprocess.CompletedProcess([],0,json.dumps([{'Id':'a'*64,'Name':'/foreign'}]),'')
            with patch('evaluator.harness.turn_lifecycle.subprocess.run',return_value=response) as run:
                with self.assertRaisesRegex(RuntimeError,'name mismatch'):stop_owned_turn(['docker','run','--name','owned'],p)
                self.assertEqual(run.call_count,1)
                self.assertEqual(run.call_args.args[0][:2],['docker','inspect'])

    def test_inspection_permission_error_is_not_absence(self):
        with tempfile.TemporaryDirectory() as d:
            p=Path(d)/'turn.cid';p.write_text('a'*64)
            with patch('evaluator.harness.turn_lifecycle.subprocess.run',return_value=subprocess.CompletedProcess([],1,'','permission denied')) as run:
                with self.assertRaisesRegex(RuntimeError,'inspection failed'):stop_owned_turn(['docker','run','--name','owned'],p)
                self.assertEqual(run.call_count,1)

    def test_already_absent_owned_cid_needs_no_kill(self):
        with tempfile.TemporaryDirectory() as d:
            p=Path(d)/'turn.cid';p.write_text('a'*64)
            with patch('evaluator.harness.turn_lifecycle.subprocess.run',return_value=subprocess.CompletedProcess([],1,'','No such container')) as run:
                self.assertTrue(stop_owned_turn(['docker','run','--name','owned'],p)['already_absent'])
                self.assertEqual(run.call_count,1)

if __name__=='__main__':unittest.main()
