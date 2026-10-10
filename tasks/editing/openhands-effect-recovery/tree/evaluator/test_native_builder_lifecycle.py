"""Provider-free controller tests; native acceptance is proved separately by Harbor."""
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
from harbor import formal_one_stop as f
from adapters.source_identity import source_identity

class BuilderLifecycleTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory(); self.run=Path(self.temp.name)
        self.workspace=self.run/'workspace';self.workspace.mkdir()
        (self.workspace/'solution.patch').write_text('revision 1\n')
        for name in ('edit_report.json', 'run_report.json'):(self.workspace/name).write_text('{}')
        self.life=f.BuilderLifecycle(run_dir=self.run,workspace=self.workspace,lower_endpoint='http://127.0.0.1:1',node_modules=self.run/'deps',timeout=1,max_dev_rounds=2)
        self.observation=patch.object(f,'observe_thread',return_value={'thread_id':'d19d5163-f121-4ee1-a4d1-df28ddbf063c','path':str(self.run/'unit-stream'),'size':1,'sha256':'unit-only'})
        self.observation.start();self.addCleanup(self.observation.stop);self.addCleanup(self.life.close);self.addCleanup(self.temp.cleanup)
        def build(number,attempt):
            product=self.run/f'product-{attempt}';product.mkdir()
            (product/'source.ts').write_bytes((self.workspace/'solution.patch').read_bytes())
            return 0,product,{'delivery_digest':f.directory_digest(self.workspace),'exit_code':0,'materialize_result':{'product_source_identity':source_identity(product)}}
        self.build=patch.object(self.life,'_materialize',side_effect=build).start();self.addCleanup(patch.stopall)
        def dev(candidate,case,output,**kwargs):
            return {'classification':'valid_behavior','failure_attribution':{'party':'candidate','infrastructure_invalid':False},'artifact_present':True,'agent_result':{'decision':'partial'},'result_evaluation':{'contract_valid':True,'round_consumed':True,'score':30,'feedback':{'assessment':'Need revision','major_errors':['Useful public feedback'],'oracle_summary':'PRIVATE','raw_response_path':'/data/private/path'}}}
        self.dev=patch.object(self.life.controller,'_run_public_case',side_effect=dev).start()
    def first(self):
        code,payload=self.life.submit(None);self.assertEqual(code,200);return payload
    def test_public_feedback_is_authoritative_and_created_before_delivery(self):
        payload=self.first();r=self.life.controller.records[0]
        self.assertEqual(payload['feedback'],json.loads(Path(r['feedback_path']).read_text()))
        self.assertNotIn('PRIVATE',json.dumps(payload));self.assertNotIn('/data/private',json.dumps(payload))
        self.assertEqual(payload['feedback']['dev']['dev_001']['semantic_feedback']['major_errors'],['Useful public feedback'])
        self.assertFalse(any(e['event']=='feedback_delivered' for e in self.life.events))
        self.life.feedback_written(payload)
        self.assertEqual(sum(e['event']=='feedback_delivered' for e in self.life.events),1)
    def test_duplicate_after_max_freeze_does_not_rebuild_or_evaluate(self):
        self.life.controller.max_dev_rounds=1;first=self.first()
        code,again=self.life.submit(None)
        self.assertEqual((code,again),(200,first));self.assertEqual(self.build.call_count,1);self.assertEqual(self.dev.call_count,2)
    def test_revision_requires_exact_ack_before_build(self):
        self.first();(self.workspace/'solution.patch').write_text('revision 2\n')
        code,_=self.life.submit('wrong');self.assertEqual(code,409);self.assertEqual(self.build.call_count,1)
    def test_final_round_returns_full_feedback_and_preserves_ack(self):
        first=self.first();self.life.feedback_written(first);(self.workspace/'solution.patch').write_text('revision 2\n')
        code,last=self.life.submit(first['feedback_digest'])
        self.assertEqual(code,200);self.assertTrue(last['frozen']);self.assertEqual(last['feedback_digest_ack'],first['feedback_digest'])
        self.assertEqual(set(last['feedback']['dev']),{'dev_001','dev_002'});self.assertEqual(self.dev.call_count,4)
    def test_concurrent_duplicate_submissions_have_one_materialization(self):
        from concurrent.futures import ThreadPoolExecutor
        with ThreadPoolExecutor(max_workers=2) as pool:
            results=list(pool.map(lambda _:self.life.submit(None),range(2)))
        self.assertEqual(results[0],results[1]);self.assertEqual(self.build.call_count,1);self.assertEqual(self.dev.call_count,2)
    def test_build_failure_returns_public_actionable_details(self):
        self.build.side_effect=None
        self.build.return_value=(1,self.run/'failed',{'materialize_result':{'errors':['invalid patch'],'commands':[{'exit_code':1,'stderr_tail':'/data/private/source.ts: type mismatch','stdout_tail':''}]}})
        code,payload=self.life.submit(None)
        self.assertEqual(code,422);self.assertIn('type mismatch',json.dumps(payload));self.assertNotIn('/data/private',json.dumps(payload));self.assertEqual(self.dev.call_count,0)
    def test_materialization_resource_failure_is_infrastructure(self):
        self.build.side_effect=None
        self.build.return_value=(0,self.run/'product',{'resource_valid':False})
        code,payload=self.life.submit(None)
        self.assertEqual(code,503);self.assertFalse(payload['round_consumed']);self.assertEqual(self.dev.call_count,0)
    def test_missing_native_thread_fails_before_build(self):
        with patch.object(f,'observe_thread',side_effect=ValueError('no native stream')):
            with self.assertRaises(ValueError):self.life.submit(None)
        self.assertEqual(self.build.call_count,0)
    def test_one_submission_builder_exit_permitted_with_native_proof(self):
        first=self.first();self.life.feedback_written(first);self.life.controller.freeze_latest('builder_exit')
        self.life._event('builder_invocation_started');self.life._event('builder_invocation_ended',exit_code=0)
        with patch.object(f,'verify_native',return_value={'valid':True,'revision_observed':False}):
            proof=f.builder_attestation(self.life,builder_exit_code=0,started_ns=1,ended_ns=2)
            self.assertTrue(proof['complete']);self.assertEqual(proof['accepted_candidate_count'],1)
            self.assertFalse(f.builder_attestation(self.life,builder_exit_code=1,started_ns=1,ended_ns=2)['complete'])

if __name__=='__main__':unittest.main()
