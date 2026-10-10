"""No-model controls for typed public rejection and unchanged lifecycle state."""
import copy,hashlib,json,os,shutil,tempfile,threading,unittest
from pathlib import Path
from unittest.mock import patch
from evaluator.harness.public_semantic_feedback import collect_public_case_feedback,PublicCaseRejectionFeedback,rejection_feedback_payload,QUALITY
from evaluator.harness import controller
from evaluator.harness.builder_lifecycle import BuilderSession
from harbor.formal_one_stop import SocketLifecycle

def sha(b):return hashlib.sha256(b).hexdigest()
def write(p,v):p.parent.mkdir(parents=True,exist_ok=True);p.write_text(json.dumps(v,indent=2)+'\n')

def fixture(root,case_id='dev_001',valid=False,*,run=None,evaluation=None,repository=None,digest=None):
    """Entirely synthetic receipts, clearly separate from actual lower capability."""
    task=root/'task';case=task/'dev_cases'/case_id;case.mkdir(parents=True,exist_ok=True);(case/'input.md').write_text('Synthetic public case.\n')
    (task/'input/repository').mkdir(parents=True,exist_ok=True)
    run=run or root/'lifecycle';run.mkdir(parents=True,exist_ok=True)
    evaluation=evaluation or run/'evaluations/candidate_001_attempt_123'
    repository=repository or run/'candidates/candidate_001';repository.mkdir(parents=True,exist_ok=True)
    if not list(repository.iterdir()):(repository/'code.py').write_text('VALUE = 1\n')
    digest=digest or controller.tree_digest(repository)
    output=evaluation/case_id;workspace=output/'workspace';workspace.mkdir(parents=True,exist_ok=True)
    artifact=workspace/'agent_result.json';artifact.write_text('{"synthetic_control":true}\n')
    request={'repository':str(repository),'case':str(case),'output':str(output),'case_id':case_id,'candidate_digest':digest}
    write(evaluation/(case_id+'-case-request.json'),request)
    write(output/'logical-context.json',{'case_id':case_id,'candidate_digest_before':digest,'task_sha256':sha((case/'input.md').read_bytes())})
    validation={'validated_by':'evaluator','valid':True,'sha256':sha(artifact.read_bytes()),'quality_schema_findings':list(QUALITY)if valid else []}
    execution={'case_id':case_id,'candidate_digest':'f'*64,'candidate_digest_after':digest,'candidate_repository':str(repository),'classification':'candidate_partial'if valid else'candidate_valid','infra_valid':True,'artifact_validation':validation}
    write(output/'controller_execution_result.json',execution)
    normalized=dict(execution,candidate_digest=digest)
    identity={'case_id':case_id,'candidate_digest':digest,'execution_record':normalized,'model':'gpt-5.6-sol','effort':'max','inputs':{'task_input':{'path':str(case/'input.md'),'sha256':sha((case/'input.md').read_bytes())},'agent_artifact':{'path':str(artifact),'sha256':sha(artifact.read_bytes())}}}
    scoring=output/'semantic_scoring';write(scoring/'scoring_intent.json',{'identity':identity,'identity_sha256':sha(json.dumps(identity,sort_keys=True,separators=(',',':')).encode())})
    (scoring/'judge_prompt.txt').write_text('Synthetic fixture; never sent to a model.');write(scoring/'input_manifest.json',{'synthetic':True})
    (scoring/'model_response.json').write_text('synthetic bytes; never parsed\n')
    contract={'schema_version':'agentswe-edit-result-score-contract-v1','case_id':case_id,'contract_valid':valid,'result_score_publishable':valid,'evaluation_state':'scoreable'if valid else'model_output_invalid','result_score':63 if valid else None,'judge':{'model':'gpt-5.6-sol','reasoning_effort':'max'},'provider_usage':{'logical_requests':1,'completed_responses':1,'transport_attempts':1,'input_tokens':2,'output_tokens':2,'total_tokens':4},'prompt_digest':sha((scoring/'judge_prompt.txt').read_bytes()),'errors':[]if valid else['JSONDecodeError: Expecting property name enclosed in double quotes: line 10 column 5 (char 950)']}
    if valid:contract.update(input_manifest_digest=sha((scoring/'input_manifest.json').read_bytes()),model_response_digest=sha((scoring/'model_response.json').read_bytes()[:-1]))
    write(scoring/'result_score_contract.json',contract)
    result=dict(normalized,score=contract['result_score'],semantic_score_contract_valid=valid,semantic_judgement={'contract_valid':valid,'score':contract['result_score']},round_consumed=valid)
    write(output/'controller_result.json',result)
    return {'run_dir':run,'evaluation_root':evaluation,'repository':repository,'case_root':case,'case_id':case_id,'candidate_digest':digest},result

class SemanticFeedbackTests(unittest.TestCase):
    def setUp(self):self.temp=tempfile.TemporaryDirectory();self.root=Path(self.temp.name)
    def tearDown(self):self.temp.cleanup()
    def test_valid_score_and_fixed_findings(self):
        kw,_=fixture(self.root,'dev_002',True);value=collect_public_case_feedback(**kw)
        self.assertIs(type(value),PublicCaseRejectionFeedback);self.assertEqual(value.score,63);self.assertEqual(set(value.quality_findings),set(QUALITY.values()))
    def test_invalid_result_json_is_not_candidate_error(self):
        kw,_=fixture(self.root);value=collect_public_case_feedback(**kw)
        self.assertTrue(value.artifact_valid);self.assertEqual(value.classification,'candidate_valid');self.assertIsNone(value.score);self.assertEqual(value.result_judge_error,'result_score_json_invalid')
    def test_missing_and_unknown_contract_omit(self):
        kw,_=fixture(self.root);p=kw['evaluation_root']/kw['case_id']/'semantic_scoring/result_score_contract.json';p.unlink();self.assertIsNone(collect_public_case_feedback(**kw))
    def test_types_nan_duplicate_and_range(self):
        kw,_=fixture(self.root,'dev_002',True);p=kw['evaluation_root']/kw['case_id']/'semantic_scoring/result_score_contract.json';original=p.read_text()
        for score in (True,101,-1,'63',float('nan')):
            d=json.loads(original);d['result_score']=score;write(p,d);self.assertIsNone(collect_public_case_feedback(**kw))
        p.write_text(original[:-2]+',"case_id":"dev_002"}\n');self.assertIsNone(collect_public_case_feedback(**kw))
    def test_wrong_case_digest_and_unknown(self):
        kw,_=fixture(self.root)
        for field,value in (('case_id','test_001'),('candidate_digest','a'*64),('case_id',['dev_001'])):
            self.assertIsNone(collect_public_case_feedback(**dict(kw,**{field:value})))
    def test_request_context_and_intent_forgery(self):
        kw,_=fixture(self.root)
        paths=[kw['evaluation_root']/'dev_001-case-request.json',kw['evaluation_root']/'dev_001/logical-context.json',kw['evaluation_root']/'dev_001/semantic_scoring/scoring_intent.json']
        for p in paths:
            original=p.read_bytes();d=json.loads(original);d['case_id']='test_001' if 'case_id'in d else d.get('case_id');d['identity_sha256']='0'*64;write(p,d)
            self.assertIsNone(collect_public_case_feedback(**kw));p.write_bytes(original)
    def test_symlink_escape_loop_and_hardlink(self):
        kw,_=fixture(self.root);p=kw['evaluation_root']/'dev_001/semantic_scoring/result_score_contract.json';raw=p.read_bytes();outside=self.root/'outside.json';outside.write_bytes(raw)
        p.unlink();p.symlink_to(outside);self.assertIsNone(collect_public_case_feedback(**kw));p.unlink();p.symlink_to(p);self.assertIsNone(collect_public_case_feedback(**kw));p.unlink();os.link(outside,p);self.assertIsNone(collect_public_case_feedback(**kw))
    def test_tampered_artifact_prompt_and_model_bytes(self):
        kw,_=fixture(self.root,'dev_002',True);out=kw['evaluation_root']/kw['case_id']
        for p in (out/'workspace/agent_result.json',out/'semantic_scoring/judge_prompt.txt',out/'semantic_scoring/model_response.json'):
            b=p.read_bytes();p.write_bytes(b+b'changed');self.assertIsNone(collect_public_case_feedback(**kw));p.write_bytes(b)
    def test_private_fields_never_project(self):
        kw,_=fixture(self.root,'dev_002',True);p=kw['evaluation_root']/kw['case_id']/'semantic_scoring/result_score_contract.json';d=json.loads(p.read_text());d.update(assessment='/data/private/oracle secret',major_errors=['authorization SECRET'],dimensions={'private':'SECRET'},raw='SECRET');write(p,d)
        serialized=json.dumps(collect_public_case_feedback(**kw).public_dict());self.assertNotIn('SECRET',serialized);self.assertNotIn('private',serialized);self.assertNotIn('assessment',serialized)
    def test_unrecognized_quality_and_boolean_validity_omit(self):
        kw,_=fixture(self.root);p=kw['evaluation_root']/'dev_001/controller_execution_result.json';d=json.loads(p.read_text());d['artifact_validation']['quality_schema_findings']=['/data/private'];write(p,d);self.assertIsNone(collect_public_case_feedback(**kw))
    def test_fake_type_and_duplicate_case_are_not_serialized(self):
        kw,_=fixture(self.root);value=collect_public_case_feedback(**kw)
        self.assertIsNone(rejection_feedback_payload(kw['candidate_digest'],[{}]));self.assertIsNone(rejection_feedback_payload(kw['candidate_digest'],[value,value]))
        object.__setattr__(value,'score',63);self.assertIsNone(rejection_feedback_payload(kw['candidate_digest'],[value]))
    def _producer(self,collector_error=False,serializer_error=False):
        task=self.root/'task';(task/'input/repository').mkdir(parents=True);run=self.root/'lifecycle';delivery=self.root/'delivery';delivery.mkdir();(delivery/'code.py').write_text('VALUE = 1\n')
        ctrl=controller.CandidateController(base_repository=task/'input/repository',run_dir=run,launcher=task/'unused.py',broker_endpoint='http://unused.invalid',public_case_ids=('dev_001','dev_002'))
        life=SocketLifecycle.__new__(SocketLifecycle);life.lock=threading.RLock();life.controller=ctrl;life.session_id='same-native-fixture';life.session=BuilderSession(ctrl,session_id=life.session_id);life.workspace=delivery;life.accepted_deliveries={};life.events=[];life.bind_native_thread=lambda:None;life.validate=lambda why:(200,{})
        life.event=lambda name,**kw:life.events.append({'event':name,**kw});calls=[]
        def executed(repository,case,round_no):
            calls.append(case);_,result=fixture(self.root,case,case=='dev_002',run=run,evaluation=ctrl.current_evaluation_root,repository=repository,digest=controller.tree_digest(repository));return result
        import contextlib
        with contextlib.ExitStack()as stack:
            stack.enter_context(patch.object(controller,'materialize',side_effect=lambda src,dst,**kw:shutil.copytree(src,dst)))
            stack.enter_context(patch.object(controller,'build',return_value={'exit_code':0,'stdout':'','stderr':''}))
            stack.enter_context(patch.object(ctrl,'_run_dev',side_effect=executed))
            if collector_error:stack.enter_context(patch.object(controller,'collect_public_case_feedback',side_effect=RuntimeError('diagnostic failure')))
            if serializer_error:stack.enter_context(patch('harbor.formal_one_stop.rejection_feedback_payload',side_effect=RuntimeError('serialization failure')))
            code,payload=life.submit(None);intent=next((run/'product_attempts').glob('*.json'));old=intent.read_bytes();again,retry=life.submit(None)
        self.assertEqual((code,again),(422,422));self.assertEqual(calls,['dev_001','dev_002']);self.assertEqual(old,intent.read_bytes());self.assertFalse(ctrl.records);self.assertFalse(life.accepted_deliveries);self.assertIsNone(life.session.feedback_digest);self.assertFalse(life.session.feedback_consumed);self.assertIn('no replay',retry['error']);self.assertFalse(payload['submission_consumed']);self.assertFalse(payload['retry_same_candidate']);self.assertEqual(json.loads(old)['state'],'not_evaluable')
        return payload
    def test_actual_controller_session_socket_rejection_keeps_no_replay(self):
        payload=self._producer();feedback=payload['public_rejection_feedback'];self.assertFalse(feedback['accepted']);self.assertFalse(feedback['round_consumed']);self.assertEqual(feedback['dev'][1]['score'],63)
    def test_collector_failure_preserves_original_422(self):self.assertNotIn('public_rejection_feedback',self._producer(collector_error=True))
    def test_serializer_failure_preserves_original_422(self):self.assertNotIn('public_rejection_feedback',self._producer(serializer_error=True))
