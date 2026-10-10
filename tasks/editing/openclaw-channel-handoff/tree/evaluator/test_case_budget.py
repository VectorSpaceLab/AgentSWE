"""No-API regressions for total case time and structured failure attribution."""
import hashlib
import json
import math
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

from controller.two_round_controller import tree_digest
from evaluator.case_budget import CaseBudget
from evaluator import hidden_executor as hidden
from lower_agent import launcher as lower


class CaseBudgetTests(unittest.TestCase):
    def test_original_anchor_includes_preparation_and_finalization(self):
        budget=CaseBudget(1000,1600)
        with patch('time.monotonic',return_value=1040):
            self.assertEqual(budget.remaining_for_launcher(),540)
        self.assertEqual(budget.product_deadline,1570)
        self.assertEqual(budget.launcher_deadline,1580)
        self.assertEqual(budget.evidence_deadline,1598)
        self.assertEqual(budget.snapshot()['extra_model_or_case_seconds'],0)
        with patch('time.monotonic',return_value=1570):
            with self.assertRaises(TimeoutError):budget.remaining_for_launcher()

    def test_nonfinite_reversed_or_extended_envelope_is_rejected(self):
        for start,end in ((0,931),(0,0),(2,1),(0,math.inf),(math.nan,930)):
            with self.subTest(start=start,end=end),self.assertRaises(ValueError):
                CaseBudget(start,end)

    def test_launcher_cannot_borrow_finalization_time(self):
        for deadline in ('1581','inf','nan'):
            with patch('time.monotonic',return_value=1000),patch.object(sys,'argv',[
                'launcher','--product','/not-read','--case','/not-read',
                '--broker-endpoint','http://127.0.0.1:9/v1/responses','--output','/not-created',
                '--case-deadline-monotonic','1600','--launcher-deadline-monotonic',deadline]):
                with self.assertRaisesRegex(SystemExit,'finalization'):lower.main()

    def test_deadline_digest_matches_original_full_byte_contract(self):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory);(root/'z').mkdir();(root/'a').write_bytes(b'hello\x00world')
            (root/'z'/'big').write_bytes(b'X'*(2*1024*1024+17))
            (root/'link').symlink_to('missing-target');os.mkfifo(root/'fifo')
            reference=hashlib.sha256()
            for path in sorted(root.rglob('*'),key=lambda item:item.relative_to(root).as_posix()):
                relative=path.relative_to(root).as_posix().encode()
                if path.is_symlink():kind,data=b'L',os.readlink(path).encode()
                elif path.is_file():kind,data=b'F',path.read_bytes()
                elif path.is_dir():continue
                else:kind,data=b'O',b''
                reference.update(kind+len(relative).to_bytes(8,'big')+relative)
                reference.update(len(data).to_bytes(8,'big')+data)
            with patch('time.monotonic',return_value=1):
                self.assertEqual(tree_digest(root,deadline=2),reference.hexdigest())
                self.assertEqual(tree_digest(root),reference.hexdigest())

    def test_hash_expiry_never_returns_a_partial_digest(self):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory);(root/'large').write_bytes(b'X'*(3*1024*1024))
            with patch('time.monotonic',return_value=10),patch.object(Path,'rglob') as scan:
                with self.assertRaises(TimeoutError):tree_digest(root,deadline=10)
                scan.assert_not_called()
            ticks=iter(range(100))
            with patch('time.monotonic',side_effect=lambda:next(ticks)):
                with self.assertRaises(TimeoutError):tree_digest(root,deadline=7)
            with patch('time.monotonic',side_effect=[1,3]):
                with self.assertRaises(TimeoutError):hidden.sha256_file(root/'large',deadline=2)

    def test_expired_stats_budget_does_not_dispatch(self):
        with patch.object(lower.urllib.request,'urlopen') as request:
            with self.assertRaises(TimeoutError):lower.read_broker_stats('http://127.0.0.1:9/v1/responses',timeout=0)
            request.assert_not_called()

    def _run_case(self, *, digest_timeout=False, publication_overrun=False, unknown_usage=False):
        clock=[1000.0];seen={}
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory);case=root/'test_001';case.mkdir();(case/'input.md').write_text('Synthetic budget request.')
            product=root/'candidate';product.mkdir();(product/'source').write_text('unchanged')
            private=root/'private.json';private.write_text('{}')
            output=root/'case';real_write=hidden.write_json
            def owned(command,**kwargs):
                seen['command']=command;seen['scope_timeout']=kwargs['timeout']
                clock[0]=1580.0
                real_write(output/'run_report.json',{'classification':'candidate_product_failure',
                    'classification_reason':'synthetic missing core artifact',
                    'artifact_contract':{'valid':False},'native_case':{}})
                return subprocess.CompletedProcess(command,1,'',''),{
                    'valid':True,'timed_out':False,'cleanup':{'complete':True},'unit':'synthetic-owned-scope'}
            def hashed(path,*,deadline=None):
                seen.setdefault('digest_deadlines',[]).append(deadline)
                if clock[0]>=1580:
                    clock[0]+=12
                    if digest_timeout:raise TimeoutError('synthetic slow hash')
                return 'same-digest'
            def capture(**kwargs):
                seen['capture_deadline']=kwargs['deadline'];clock[0]+=3
                return {'collection_valid':True,'database_present':False}
            def write(path,value):
                real_write(path,value)
                if publication_overrun and path.name=='hidden_case_attestation.json' and clock[0]<1600:
                    clock[0]=1601.0
            before={'runtime':{'calls':0,'usage_unknown_calls':0}}
            after={'runtime':{'calls':23,'successful_calls':22,'failures':1,'usage_unknown_calls':1}} if unknown_usage else before
            with patch('time.monotonic',side_effect=lambda:clock[0]), \
                 patch.object(hidden,'tree_digest',side_effect=hashed), \
                 patch.object(hidden,'safe_stats',side_effect=[(before,None),(after,None)]), \
                 patch.object(hidden,'run_owned',side_effect=owned), \
                 patch.object(hidden,'expected_from_facts',return_value=({},[])), \
                 patch.object(hidden,'capture_durable_state',side_effect=capture), \
                 patch.object(hidden,'write_json',side_effect=write):
                result=hidden.launch_case(case_id='test_001',hidden_case=case,frozen_candidate=product,
                    output=output,broker_endpoint='http://127.0.0.1:9/v1/responses',runtime=None,
                    private_file=private,view={},timeout_seconds=600,
                    evaluation_started_monotonic=1000,absolute_case_deadline=1600)
            seen['saved_result']=json.loads((output/'result.json').read_text())
            seen['saved_attestation']=json.loads((output/'hidden_case_attestation.json').read_text())
            return result,seen

    def test_stopped_scope_hash_and_capture_fit_inside_original_case(self):
        result,seen=self._run_case()
        self.assertEqual(seen['scope_timeout'],580)
        command=seen['command']
        self.assertEqual(command[command.index('--launcher-deadline-monotonic')+1],'1580')
        self.assertEqual(seen['digest_deadlines'],[1570,1598])
        self.assertEqual(seen['capture_deadline'],1598)
        self.assertEqual(result['classification'],'candidate_product_failure')
        self.assertEqual(result['timing_contract']['elapsed_seconds'],595)
        self.assertTrue(result['frozen_candidate_digest_stable'])

    def test_incomplete_hash_is_infrastructure_not_candidate_mutation(self):
        result,_=self._run_case(digest_timeout=True)
        self.assertEqual(result['classification'],'launcher_infrastructure_error')
        self.assertIn('digest could not be verified',result['classification_reason'])
        self.assertIsNone(result['frozen_candidate_digest_after'])

    def test_record_write_overrun_cannot_publish_candidate_scoreable_status(self):
        result,seen=self._run_case(publication_overrun=True)
        for value in (result,seen['saved_result'],seen['saved_attestation']):
            self.assertEqual(value['classification'],'launcher_infrastructure_error')
        self.assertTrue(result['timing_contract']['record_publication_overrun'])

    def test_outer_runner_independently_rejects_unresolved_usage(self):
        result,_=self._run_case(unknown_usage=True)
        self.assertEqual(result['classification'],'broker_infrastructure_error')

    def test_22_successes_do_not_hide_the_last_unresolved_model_request(self):
        delta=lower.broker_stats_delta({'runtime':{}},{'runtime':{'calls':23,
            'successful_calls':22,'failures':1,'provider_failures':1,'usage_unknown_calls':1}})
        report={'status':'partial','health':{'status':'ok'},'errors':['native embedded Agent timeout']}
        for valid in (False,True):
            self.assertEqual(lower.classify_execution(report,delta,valid,'missing')[0],
                             'broker_infrastructure_error')
        self.assertEqual(delta['usage_unknown_calls'],1)

    def test_prior_unknown_request_does_not_contaminate_a_new_case_delta(self):
        before={'runtime':{'calls':23,'failures':1,'successful_calls':22,'usage_unknown_calls':1}}
        after={'runtime':{'calls':24,'failures':1,'successful_calls':23,'usage_unknown_calls':1}}
        delta=lower.broker_stats_delta(before,after)
        self.assertEqual(delta['usage_unknown_calls'],0)
        report={'status':'completed','health':{'status':'ok'}}
        self.assertEqual(lower.classify_execution(report,delta,True,None)[0],'candidate_behavior_observed')

    def test_no_call_and_no_core_artifact_is_not_automatically_infrastructure(self):
        delta=lower.broker_stats_delta({'runtime':{}},{'runtime':{}})
        report={'status':'completed','health':{'status':'ok'}}
        self.assertEqual(lower.classify_execution(report,delta,False,'missing')[0],'candidate_product_failure')


if __name__=='__main__':unittest.main()
