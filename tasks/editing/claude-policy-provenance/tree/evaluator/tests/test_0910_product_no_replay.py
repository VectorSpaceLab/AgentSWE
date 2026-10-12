"""No-provider regression tests for durable materialized-product no replay."""
import hashlib
import json
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from unittest import mock

from agentloop.evaluator import controller
from agentloop.evaluator.product_attempts import ProductAttempts, ProductReplayBlocked

D = 'a' * 64

def snapshot(root):
    return {str(p.relative_to(root)): hashlib.sha256(p.read_bytes()).hexdigest()
            for p in root.rglob('*') if p.is_file() and not p.is_symlink()}

class ProductNoReplayTests(unittest.TestCase):
    def test_concurrent_claim_has_only_one_winner(self):
        with tempfile.TemporaryDirectory() as tmp:
            ledger = ProductAttempts(Path(tmp))
            def claim(i):
                try: ledger.claim(D, {'index': i}); return True
                except ProductReplayBlocked: return False
            with ThreadPoolExecutor(max_workers=8) as pool:
                self.assertEqual(sum(pool.map(claim, range(20))), 1)
            self.assertIsNotNone(ledger.lookup(D))

    def test_crash_after_directory_before_intent_still_blocks(self):
        with tempfile.TemporaryDirectory() as tmp:
            ledger = ProductAttempts(Path(tmp)); ledger.directory(D).mkdir()
            self.assertEqual(ledger.lookup(D)['state'], 'unknown_or_in_progress')
            with self.assertRaises(ProductReplayBlocked): ledger.claim(D, {})
            self.assertFalse((ledger.directory(D) / 'intent.json').exists())

    def test_corrupt_outcome_blocks_without_changing_original_bytes(self):
        with tempfile.TemporaryDirectory() as tmp:
            ledger = ProductAttempts(Path(tmp)); ledger.claim(D, {})
            (ledger.directory(D) / 'outcome.json').write_bytes(b'{incomplete')
            before = snapshot(Path(tmp))
            self.assertEqual(ledger.lookup(D)['state'], 'unknown_or_invalid_evidence')
            with self.assertRaises(ProductReplayBlocked): ledger.claim(D, {})
            self.assertEqual(snapshot(Path(tmp)), before)

    def test_partial_cases_and_result_are_preserved_across_restart(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp); ledger = ProductAttempts(root / 'attempts'); ledger.claim(D, {})
            output = root / 'evaluation'; output.mkdir()
            original = b'{"usage":null,"result_score":99,"private":"DO_NOT_EXPOSE"}\n'
            (output / 'result.json').write_bytes(original)
            unknown = {'case_id':'dev_001', 'classification':'provider_infrastructure_failure', 'score':None,
                       'provider_usage':{'total_tokens':None,'usage_complete':False}}
            complete = {'case_id':'dev_002','classification':'completed','score':99,
                        'score_kind':'independent_result_rubric','result_judge_contract':{'result_score':99}}
            ledger.record_case(D, 'dev_001', unknown, output)
            ledger.record_case(D, 'dev_002', complete, output)
            self.assertEqual(ProductAttempts(root/'attempts').lookup(D)['case_results'], [unknown, complete])
            record={'accepted':False,'dev':[unknown,complete]}
            ledger.finish(D,record,output)
            before=snapshot(root)
            other=ProductAttempts(root/'attempts')
            self.assertEqual(other.lookup(D)['case_results'],record['dev'])
            with self.assertRaises(ProductReplayBlocked): other.claim(D,{})
            with self.assertRaises(FileExistsError): other.record_case(D,'dev_002',{'score':0},output)
            with self.assertRaises(FileExistsError): other.finish(D,{'accepted':True,'dev':[]},output)
            self.assertEqual(snapshot(root),before)
            self.assertEqual((output/'result.json').read_bytes(),original)

    def test_public_usage_delta_never_relabels_unknown_as_zero(self):
        before={'runtime': {'calls':0,'total_tokens':0,'usage_complete':True}}
        for runtime in ({'calls':1,'total_tokens':None}, {'calls':2,'total_tokens':12,'usage_complete':False}):
            delta=controller._delta(before,{'runtime':runtime})
            self.assertIsNone(delta['total_tokens'])
            self.assertEqual(delta['calls'],runtime['calls'])

    def test_distinct_product_is_independently_claimable(self):
        with tempfile.TemporaryDirectory() as tmp:
            ledger=ProductAttempts(Path(tmp));ledger.claim(D,{})
            other='b'*64
            self.assertIsNone(ledger.lookup(other));ledger.claim(other,{})
            self.assertIsNotNone(ledger.lookup(other))

    def create_controller(self, root):
        obj=controller.Controller(repository=root/'repository',cases=root/'cases',run_dir=root/'run',broker_endpoint='no-network')
        obj.witness={'session_id':'fixture-native-session','submissions':[{'candidate_digest':D}]}
        return obj

    def test_restarted_backend_blocks_before_materialization_and_model(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp);original=self.create_controller(root)
            original.product_attempts.claim(D,{'builder_session_id':'fixture-native-session'})
            restarted=self.create_controller(root)
            with mock.patch.object(controller,'materialize') as materialize, mock.patch.object(restarted,'_run_public') as run:
                with self.assertRaises(ProductReplayBlocked): restarted.submit(root/'missing.patch',1)
                materialize.assert_not_called();run.assert_not_called()

    def test_legacy_completed_and_unknown_rounds_are_read_only_blockers(self):
        for accepted in (True,False):
            with self.subTest(accepted=accepted), tempfile.TemporaryDirectory() as tmp:
                root=Path(tmp);obj=self.create_controller(root)
                record={'candidate_digest':D,'accepted':accepted,'dev':[{'case_id':'dev_001','score':None},{'case_id':'dev_002','score':99}]}
                path=obj.run_dir/('round_001.json' if accepted else 'round_001_infrastructure_invalid_attempt_001.json')
                path.write_text(json.dumps(record));before=snapshot(obj.run_dir)
                self.assertEqual(obj.known_product_digests(),{D})
                self.assertEqual(obj.lookup_product_attempt(D)['case_results'],record['dev'])
                with mock.patch.object(controller,'materialize') as build:
                    with self.assertRaises(ProductReplayBlocked):obj.submit(root/'missing.patch',1)
                    build.assert_not_called()
                self.assertEqual(snapshot(obj.run_dir),before)

    def test_claim_is_durable_before_first_public_model_call(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp);obj=self.create_controller(root)
            def run(candidate,number):
                receipt=json.loads((obj.product_attempts.directory(D)/'intent.json').read_text())
                self.assertEqual(receipt['candidate_digest'],D)
                raise RuntimeError('simulated first request interruption')
            with mock.patch.object(controller,'materialize',return_value={'candidate_digest':D,'patch_sha256':'p'}), \
                 mock.patch.object(controller,'verify_submission_binding'), mock.patch.object(obj,'_run_public',side_effect=run) as lower:
                with self.assertRaisesRegex(RuntimeError,'simulated first request'):obj.submit(root/'candidate.patch',1)
                with self.assertRaises(ProductReplayBlocked):obj.submit(root/'candidate.patch',1)
                self.assertEqual(lower.call_count,1)
            self.assertIsNone(obj.product_attempts.lookup(D)['case_results'][0]['score'] if obj.product_attempts.lookup(D)['case_results'] else None)

if __name__=='__main__':unittest.main()
