import copy,json,tempfile,unittest
from pathlib import Path
from agentloop.evaluator.case_contract import digest_object
from agentloop.evaluator.execution_provenance import normalize
from agentloop.evaluator.semantic_score import execution_verdict
from agentloop.protocol import RESULT_SCHEMA
import hashlib

class ProvenanceTests(unittest.TestCase):
    def fixture(self,d):
        event={'kind':'target_hook_call','action_id':'read','permission':'allow','receipt_digest':None,'response_digest':'b'*64}
        obs={'action_id':'read','permission':'allow','receipt':None,'response_digest':'b'*64}
        binding={'case_id':'test_006','runtime_nonce':'case_'+'1'*24,'execution_nonce':'execution_'+'2'*24,'case_spec_sha256':'c'*64,'case_contract_digest':'d'*64,'selected_action_ids':['read'],'product_event_digests':[digest_object(event)],'observation_digest':digest_object([obs]),'product_trajectory_digest':digest_object([event])}
        answer={'schema_version':RESULT_SCHEMA,'case_id':'test_006','observations':[],'decision':{'completion_claim':'partial'},'integrity':{},'safety':{'followed_unobserved_instruction':False},'binding':binding}
        before={'protocol':{'model':'gpt-5.6-sol','reasoning_effort':'high'},'runtime':{'calls':0,'successful_calls':0,'failures':0},'request_ledger':[]}
        after=copy.deepcopy(before);after['runtime'].update(calls=1,successful_calls=1);after['request_ledger']=[{'completed':True,'submission_state':'completed','response_id':'fixture-response','status_code':200}]
        trace={'schema_version':'agentswe-claude-policy-agent-case/v1','case_id':'test_006','candidate_digest':'e'*64,'execution_attempted':True,'environment_preflight':{'valid':True},'infrastructure_invalid':False,'product_events':[event],'observations':[obs],'binding':binding,'answer':answer,'broker':{'before':before,'after':after},'artifact':{'path':'agent_result.json','origin':'lower_model_final_response','evaluator_synthesized':False,'binding_verified':True}}
        self.persist(d,trace)
        return trace,{'case_id':'test_006','candidate_digest':'e'*64,'trajectory_path':str(d/'trajectory.json')}
    def persist(self,d,trace):
        a=d/'agent_result.json';a.write_text(json.dumps(trace['answer']));trace['artifact']['sha256']=hashlib.sha256(a.read_bytes()).hexdigest();(d/'trajectory.json').write_text(json.dumps(trace))
    def test_real_fields_are_normalized_but_model_claims_do_not_authorize(self):
        with tempfile.TemporaryDirectory() as t:
            d=Path(t);trace,r=self.fixture(d)
            v,z=execution_verdict(r,'test_006','e'*64);self.assertEqual(v['classification'],'scoreable');self.assertIsNone(z)
            # Poor model claims still reach the rubric; source observations control validity.
            trace['answer']['decision']['completion_claim']='complete';trace['answer']['observations']=[{'permission':'deny','claim':'wrong'}];self.persist(d,trace)
            self.assertEqual(execution_verdict(r,'test_006','e'*64)[0]['classification'],'scoreable')
            trace['broker']['after']['request_ledger'][0]['completed']=False;self.persist(d,trace)
            self.assertEqual(execution_verdict(r,'test_006','e'*64)[0]['classification'],'unresolved')
    def test_artifact_and_product_permission_tampering_fail_closed(self):
        with tempfile.TemporaryDirectory() as t:
            d=Path(t);trace,r=self.fixture(d)
            (d/'agent_result.json').write_text('{}')
            self.assertFalse(normalize(r,case_id='test_006',candidate_digest='e'*64)[1])
            self.persist(d,trace);trace['observations'][0]['permission']='deny';self.persist(d,trace)
            self.assertFalse(normalize(r,case_id='test_006',candidate_digest='e'*64)[1])
    def test_wrong_case_model_and_snapshot_counters_fail_closed(self):
        with tempfile.TemporaryDirectory() as t:
            d=Path(t);trace,r=self.fixture(d)
            self.assertFalse(normalize(r,case_id='test_005',candidate_digest='e'*64)[1])
            trace['broker']['after']['protocol']['reasoning_effort']='max';self.persist(d,trace)
            self.assertFalse(normalize(r,case_id='test_006',candidate_digest='e'*64)[1])
