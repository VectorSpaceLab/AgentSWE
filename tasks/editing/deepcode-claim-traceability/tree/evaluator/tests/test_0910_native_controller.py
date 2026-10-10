"""No-replay and immutable public feedback boundaries; no provider requests."""
import json
from pathlib import Path
import sys, tempfile, unittest
from unittest.mock import patch
ROOT=Path(__file__).resolve().parents[2];sys.path[:0]=[str(ROOT),str(ROOT/'harbor')]
from evaluator.harness import controller
from evaluator.harness.builder_lifecycle import BuilderSession
from evaluator.harness.candidate_adapter import tree_digest

class NativeControllerTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        self.root=Path(self.tmp.name);self.delivery=self.root/'delivery';self.delivery.mkdir()
        (self.delivery/'solution.patch').write_text('first')
        self.owner=self.make_controller();self.calls=[];self.builds=[]
        self.addCleanup(patch.stopall)
        patch.object(controller,'materialize',side_effect=self.materialize).start()
        patch.object(controller,'build',side_effect=self.build).start()
        patch.object(controller.CandidateController,'_run_dev',side_effect=self.case).start()
    def make_controller(self):
        return controller.CandidateController(base_repository=self.root/'base',run_dir=self.root/'lifecycle',
            launcher=self.root/'unused',broker_endpoint='http://unused.invalid')
    def materialize(self,candidate,destination,**kwargs):
        destination.mkdir(parents=True);(destination/'product.py').write_bytes((candidate/'solution.patch').read_bytes());return destination
    def build(self,repo,output):
        self.builds.append(str(repo));return {'exit_code':0,'digest':tree_digest(repo)}
    def case(self,repo,case_id,round_no):
        self.calls.append(case_id)
        return {'case_id':case_id,'classification':'candidate_failure','semantic_score_contract_valid':True,'score':30,
            'semantic_judgement':{'feedback':{'assessment':'synthetic'}},'private_canary':'PRIVATE_CANARY'}
    def test_feedback_history_is_immutable_after_consumption(self):
        session=BuilderSession(self.owner,session_id='test-session',require_feedback_ack=True)
        session.submit(self.delivery);path=session.feedback_history_path(1);before=path.read_bytes()
        feedback=session.consume_feedback(session.feedback_digest)
        self.assertTrue(session.feedback_consumed);self.assertEqual(before,path.read_bytes())
        self.assertNotIn('PRIVATE_CANARY',json.dumps(feedback))
        (self.delivery/'solution.patch').write_text('revision')
        session.submit(self.delivery,feedback_digest_ack=session.feedback_digest)
        self.assertEqual(len(self.calls),4)
    def test_same_product_report_change_is_cached_without_new_dev_or_build(self):
        first=self.owner.submit(self.delivery);(self.delivery/'edit_report.json').write_text('{"metadata":2}')
        duplicate=self.owner.submit(self.delivery)
        self.assertEqual(first,duplicate);self.assertEqual(len(self.calls),2);self.assertEqual(len(self.builds),1)
    def test_infra_same_product_new_reports_and_new_controller_cannot_replay(self):
        def invalid(*args):
            value=self.case(*args);value['semantic_score_contract_valid']=False;return value
        with patch.object(controller.CandidateController,'_run_dev',side_effect=invalid):
            with self.assertRaises(controller.ProtocolError):self.owner.submit(self.delivery)
        (self.delivery/'edit_report.json').write_text('{"metadata":2}')
        for obj in [self.owner,self.make_controller()]:
            with self.assertRaisesRegex(controller.ProtocolError,'no replay'):obj.submit(self.delivery)
        self.assertEqual(len(self.calls),2)
        intent=json.loads(next((self.owner.run_dir/'product_attempts').glob('*.json')).read_text())
        self.assertEqual(intent['state'],'not_evaluable');self.assertEqual(len(intent['record']['dev']),2)
    def test_unknown_second_case_keeps_completed_first_case_and_durable_reservation(self):
        def incomplete(repo,case_id,round_no):
            if case_id=='dev_002':raise TimeoutError('unknown completion')
            return self.case(repo,case_id,round_no)
        with patch.object(controller.CandidateController,'_run_dev',side_effect=incomplete):
            with self.assertRaises(TimeoutError):self.owner.submit(self.delivery)
        intent=json.loads(next((self.owner.run_dir/'product_attempts').glob('*.json')).read_text())
        self.assertEqual(intent['state'],'case_in_progress');self.assertEqual(intent['active_case'],'dev_002')
        self.assertEqual(len(intent['record']['dev']),1)
        with self.assertRaisesRegex(controller.ProtocolError,'no replay'):self.make_controller().submit(self.delivery)
        self.assertEqual(len(self.calls),1)

if __name__=='__main__':unittest.main()
