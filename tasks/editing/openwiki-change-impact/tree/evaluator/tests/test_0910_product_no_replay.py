"""Product execution controls; scripted results never count as real acceptance."""
import concurrent.futures,hashlib,json,shutil,tempfile,unittest
from pathlib import Path
from unittest.mock import patch
from agentloop.evaluator import controller as module
from agentloop.evaluator.controller import Controller
from agentloop.evaluator.product_attempts import ProductAttempts,ProductReplayBlocked
from agentloop.product_identity import product_source_identity
from agentloop.protocol import write_json

def materialize_fixture(source,candidate,output,run_build=True):
    repository=output/'repository';(repository/'dist').mkdir(parents=True)
    (repository/'dist/cli.js').write_text((candidate/'solution.patch').read_text())
    identity=product_source_identity(repository);path=output/'product-source-identity.json'
    write_json(path,identity)
    return {'valid':True,'product_entry':str(repository/'dist/cli.js'),
        'product_source_digest':identity['product_source_digest'],
        'product_source_identity_path':str(path),
        'product_source_identity_sha256':hashlib.sha256(path.read_bytes()).hexdigest()}

def synthetic_results(infra=True):
    return {case:{'classification':'provider_failure' if infra and case=='dev_001' else 'candidate_partial',
        'infrastructure_invalid':infra and case=='dev_001','run':{'valid':True,'broker_delta':{'calls':0}},
        'judgement':{'contract_valid':not(infra and case=='dev_001'),
            'score':None if infra and case=='dev_001' else 99,'assessment':'Synthetic only',
            'judge_prompt':'PRIVATE_PROMPT','raw_response_path':'/data/private'}}
        for case in ('dev_001','dev_002')}

class NoReplay(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.root=Path(self.temp.name)
        self.candidate=self.root/'delivery';self.candidate.mkdir()
        (self.candidate/'solution.patch').write_text('first source')
        (self.candidate/'edit_report.json').write_text('{}')
        self.ctrl=self.make()
    def tearDown(self):self.temp.cleanup()
    def make(self):return Controller(self.root/'source',self.root/'cases',self.root/'run','http://unused')
    def submit(self,ctrl=None,infra=True):
        ctrl=ctrl or self.ctrl
        with patch.object(module,'build_candidate',side_effect=materialize_fixture),patch.object(ctrl,'_evaluate_dev',return_value=synthetic_results(infra)) as dev:
            result=ctrl.submit(self.candidate,len(ctrl.records)+1)
        return result,dev.call_count
    def test_reports_restart_keep_unknown_and_99_without_new_dev(self):
        first,count=self.submit();self.assertEqual(count,1);self.assertFalse(first['retry_allowed'])
        original=json.loads(Path(first['feedback']['json']).read_text())
        self.assertIsNone(original['cases']['dev_001']['result_score'])
        self.assertEqual(original['cases']['dev_002']['result_score'],99)
        snapshot={p:p.read_bytes() for p in (self.root/'run/product_attempts').rglob('*') if p.is_file()}
        (self.candidate/'edit_report.json').write_text('{"changed_report":true}')
        for ctrl in (self.ctrl,self.make()):
            value,count=self.submit(ctrl);self.assertEqual(count,0);self.assertTrue(value['replay_blocked'])
            self.assertFalse(value['retry_allowed']);self.assertEqual(value['feedback'],first['feedback'])
        self.assertTrue(all(p.read_bytes()==b for p,b in snapshot.items()))
        self.assertNotIn('PRIVATE',Path(first['feedback']['json']).read_text())
    def test_completed_duplicate_cache_and_reports_only_block(self):
        first,count=self.submit(infra=False);self.assertTrue(first['submission_consumed'])
        repeated,count=self.submit();self.assertTrue(repeated['idempotent']);self.assertEqual(count,0)
        (self.candidate/'edit_report.json').write_text('{"report":2}')
        repeated,count=self.submit();self.assertTrue(repeated['replay_blocked']);self.assertEqual(count,0)
        self.assertEqual(repeated['feedback'],first['feedback'])
    def test_two_distinct_products_and_first_version_freeze(self):
        first,count=self.submit(infra=False);self.assertTrue(first['submission_consumed'])
        one=self.ctrl.freeze();self.assertEqual(one['accepted_submission_count'],1)
        # A separate disposable controller exercises two distinct source versions.
        self.root=self.root/'second';self.root.mkdir();self.candidate=self.root/'delivery';self.candidate.mkdir()
        (self.candidate/'solution.patch').write_text('source A');self.ctrl=self.make()
        first,_=self.submit(infra=False);(self.candidate/'solution.patch').write_text('source B')
        second,count=self.submit(infra=False)
        self.assertEqual(count,1);self.assertTrue(second['submission_consumed'])
        self.assertNotEqual(first['product_source_digest'],second['product_source_digest'])
    def test_reserve_is_durable_before_first_dev(self):
        def observe(repository,number):
            dirs=list((self.root/'run/product_attempts').iterdir());self.assertEqual(len(dirs),1)
            self.assertTrue((dirs[0]/'intent.json').is_file())
            self.assertTrue((self.root/'run/controller_state.json').is_file())
            raise RuntimeError('synthetic interruption before completion')
        with patch.object(module,'build_candidate',side_effect=materialize_fixture),patch.object(self.ctrl,'_evaluate_dev',side_effect=observe):
            value=self.ctrl.submit(self.candidate,1)
        self.assertFalse(value['retry_allowed']);_,count=self.submit(self.make());self.assertEqual(count,0)
    def test_missing_state_with_claim_is_read_only(self):
        self.submit();(self.root/'run/controller_state.json').unlink()
        with self.assertRaisesRegex(RuntimeError,'read-only'):self.submit(self.make())
    def test_legacy_attempts_cannot_be_resampled(self):
        (self.root/'run/non_consuming_attempts').mkdir();self.ctrl=self.make()
        with self.assertRaisesRegex(RuntimeError,'read-only'):self.submit()
    def test_corrupted_identity_receipt_blocks_before_dev(self):
        def broken(*a,**kw):
            result=materialize_fixture(*a,**kw);Path(result['product_source_identity_path']).write_text('{}');return result
        with patch.object(module,'build_candidate',side_effect=broken),patch.object(self.ctrl,'_evaluate_dev') as dev:
            value=self.ctrl.submit(self.candidate,1)
        self.assertFalse(value['build']['valid']);self.assertEqual(dev.call_count,0)
    def test_concurrent_claim_has_one_winner(self):
        ledger=self.ctrl.product_attempts;digest='a'*64
        def claim(_):
            try:ledger.claim(digest,{'builder_session_id':'fixture'});return 1
            except ProductReplayBlocked:return 0
        with concurrent.futures.ThreadPoolExecutor(max_workers=8) as pool:counts=list(pool.map(claim,range(8)))
        self.assertEqual(sum(counts),1)
    def test_empty_claim_corrupt_intent_and_outcome_remain_blocking(self):
        ledger=self.ctrl.product_attempts
        for index in range(3):
            digest=str(index)*64
            if index==0:ledger.directory(digest).mkdir()
            else:
                ledger.claim(digest,{})
                (ledger.directory(digest)/('intent.json' if index==1 else 'outcome.json')).write_text('{corrupted')
            self.assertEqual(ledger.lookup(digest)['state'],'unknown_or_invalid_evidence')
            with self.assertRaises(ProductReplayBlocked):ledger.claim(digest,{})
    def test_case_bytes_retained_and_tampering_fails_closed(self):
        ledger=self.ctrl.product_attempts;digest='c'*64;ledger.claim(digest,{})
        output=self.root/'case';output.mkdir();p=output/'raw.json';p.write_text('original99')
        ledger.record_case(digest,'dev_002',{'judgement':{'score':99}},output)
        self.assertEqual(ledger.lookup(digest)['case_results']['dev_002']['judgement']['score'],99)
        p.write_text('different');self.assertEqual(ledger.lookup(digest)['state'],'unknown_or_invalid_evidence')
    def test_stable_identity_git_reports_and_external_symlink(self):
        repo=self.root/'repo';repo.mkdir();(repo/'source.ts').write_text('source');(repo/'.git').mkdir()
        first=product_source_identity(repo);(repo/'.git/index').write_text('volatile evaluator data')
        self.assertEqual(first,product_source_identity(repo))
        private=self.root/'private';private.write_text('canary');(repo/'link').symlink_to(private)
        with self.assertRaisesRegex(ValueError,'external'):product_source_identity(repo)

if __name__=='__main__':unittest.main()
