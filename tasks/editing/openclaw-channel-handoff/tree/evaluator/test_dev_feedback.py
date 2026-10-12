"""Synthetic evaluator-contract regressions, never model performance evidence."""
import copy
import json
from pathlib import Path
import subprocess
import time
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from unittest.mock import patch

from controller.dev_feedback import MAXIMA, semantic_feedback, sha
from controller.two_round_controller import DEV, TwoRoundController, tree_digest
from controller.builder_session_controller import BuilderSessionController
from evaluator.dev_result import score_public_case
from evaluator.semantic_finalize import task_rubric
from evaluator.durable_state import capture_durable_state


def fake_contract(case, score):
    left = score
    dimensions = {}
    for key, maximum in MAXIMA.items():
        value = min(left, maximum); left -= value
        dimensions[key] = {'max': maximum, 'score': value, 'evidence': 'synthetic test evidence'}
    return {'case_id': case, 'contract_valid': True, 'result_score_publishable': True,
        'evaluation_state': 'scoreable', 'result_score': score,
        'judge': {'model': 'deepseek-flash', 'reasoning_effort': 'max'},
        'provider_usage': {'logical_requests': 1, 'completed_responses': 1,
            'transport_attempts': 1, 'input_tokens': 10, 'output_tokens': 10, 'total_tokens': 20},
        'dimensions': dimensions, 'assessment': f'synthetic score {score}', 'major_errors': []}


def semantic_fixture(directory, case, digest, score):
    """Only tests may manufacture this contract; production uses Result judge."""
    directory.mkdir(parents=True, exist_ok=False)
    source = directory / 'evidence.txt'; source.write_text('synthetic evidence')
    contract = directory / 'contract.json'; contract.write_text(json.dumps(fake_contract(case, score)))
    binding = directory / 'binding.json'
    binding.write_text(json.dumps({'case_id': case, 'candidate_digest': digest,
        'contract_sha256': sha(contract), 'inputs': {'test_only': {'path': str(source), 'sha256': sha(source)}}}))
    return {'classification': 'candidate_behavior_observed', 'case_id': case,
        'semantic_result': {'case_id': case, 'candidate_digest': digest, 'classification': 'scoreable',
            'contract_path': str(contract), 'contract_sha256': sha(contract),
            'binding_path': str(binding), 'binding_sha256': sha(binding)}}


class DevFeedbackTests(unittest.TestCase):
    def test_observed_without_judge_is_not_100_and_does_not_consume(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d); source = root/'source'; source.mkdir(); (source/'x').write_text('x')
            controller = TwoRoundController(root/'run', lambda n,p: {case: {'classification': 'candidate_behavior_observed'} for case in DEV})
            record = controller.submit(source)
            self.assertEqual(record.classification, 'infrastructure-invalid')
            self.assertEqual(len(controller.records), 0)
            feedback = json.loads((root/'run/feedback/infrastructure_attempt_001.json').read_text())
            self.assertEqual(feedback['dev_scores'], {case: None for case in DEV})
            self.assertFalse(feedback['dev_passed'])
            controller.submit(source)
            self.assertTrue((root/'run/feedback/infrastructure_attempt_002.json').is_file())

    def test_real_score_values_and_strict_threshold_not_freeze(self):
        for score in (0, 19, 60, 61, 100):
            with self.subTest(score=score), tempfile.TemporaryDirectory() as d:
                root=Path(d); source=root/'source'; source.mkdir(); (source/'x').write_text('x')
                def evaluate(n,p):
                    return {case: semantic_fixture(root/'scores'/case,case,tree_digest(p),score) for case in DEV}
                controller=TwoRoundController(root/'run',evaluate)
                record=controller.submit(source)
                self.assertEqual(record.classification,'completed')
                feedback=json.loads((root/'run/feedback/candidate_001.json').read_text())
                self.assertEqual(feedback['dev_scores'],{case:score for case in DEV})
                self.assertEqual(feedback['dev_passed'],score>60)
                self.assertIsNone(controller.frozen)
                self.assertIs(controller.submit(source),record)

    def test_identity_input_and_contract_tampering_rejected(self):
        with tempfile.TemporaryDirectory() as d:
            record=semantic_fixture(Path(d)/'proof','dev_001','candidate-A',41)
            self.assertTrue(semantic_feedback(record,'dev_001','candidate-A')['valid'])
            self.assertFalse(semantic_feedback(record,'dev_002','candidate-A')['valid'])
            self.assertFalse(semantic_feedback(record,'dev_001','candidate-B')['valid'])
            (Path(d)/'proof/evidence.txt').write_text('changed')
            self.assertFalse(semantic_feedback(record,'dev_001','candidate-A')['valid'])

    def test_wrong_model_usage_and_legacy_dimensions_rejected(self):
        for variant in ('model','usage','dimensions','sum'):
            with self.subTest(variant=variant), tempfile.TemporaryDirectory() as d:
                record=semantic_fixture(Path(d)/'proof','dev_001','A',41)
                receipt=record['semantic_result']; contract=Path(receipt['contract_path'])
                value=json.loads(contract.read_text())
                if variant=='model':value['judge']['reasoning_effort']='high'
                if variant=='usage':value['provider_usage']['completed_responses']=0
                if variant=='dimensions':value['dimensions']={'task_completion':{'max':50,'score':41}}
                if variant=='sum':value['result_score']=42
                contract.write_text(json.dumps(value)); receipt['contract_sha256']=sha(contract)
                binding=Path(receipt['binding_path']); data=json.loads(binding.read_text())
                data['contract_sha256']=sha(contract); binding.write_text(json.dumps(data)); receipt['binding_sha256']=sha(binding)
                self.assertFalse(semantic_feedback(record,'dev_001','A')['valid'])

    def test_status_returns_latest_not_first_feedback(self):
        with tempfile.TemporaryDirectory() as d:
            root=Path(d)
            controller=BuilderSessionController(run_dir=root/'run',workspace=root/'workspace',source=root/'src',evaluate=lambda *_: {},require_native_evidence=False)
            for number, score in enumerate((11,47),1):
                feedback=root/f'feedback-{number}.json';feedback.write_text(json.dumps({'dev_results':{'dev_001':{'result':{'score':score}}}}))
                controller.submissions.append({'number':number,'feedback':str(feedback),'feedback_digest':sha(feedback),'candidate_digest':str(number)})
            self.assertEqual(controller.status(controller.session_id)[1]['feedback']['dev_results']['dev_001']['result']['score'],47)

    def test_staged_hidden_rubric_keeps_all_seven_dimensions(self):
        root=Path(__file__).resolve().parents[1]
        with tempfile.TemporaryDirectory() as d:
            staged=task_rubric(root,Path(d),'test_001')
            self.assertEqual(json.loads((staged.parent/'result_dimensions.json').read_text()),MAXIMA)

    def make_native(self, root):
        case=root/'case'; (case/'workspace').mkdir(parents=True)
        artifact=case/'workspace/agent_result.json'; artifact.write_text(json.dumps({'case_id':'dev_001','decision':'partial'}))
        (case/'trajectory.json').write_text('[]')
        state=case/'gateway_state';state.mkdir()
        durable=capture_durable_state(state=state,output=case/'durable-state',
            deadline=time.monotonic()+5,expected={},writers_stopped=True)
        assert durable['collection_valid'] and durable['database_present'] is False
        durable['case_binding']={'case_id':'dev_001','case_bundle_sha256':'f'*64,
                                 'owned_scope_unit':'synthetic-local-fixture'}
        return case, {'case_id':'dev_001','classification':'candidate_behavior_observed',
            'durable_state':durable,
            'case_resources':{'unit':'synthetic-local-fixture'},
            'frozen_candidate_digest_stable':True,'artifact_contract':{'valid':True},
            'agent_authored_artifact':True,'authored_artifact':str(artifact),'artifact_sha256':sha(artifact),
            'native_case':{'case_id':'dev_001','case_bundle_sha256':'f'*64,'oracle_observations':{'accepted_channel_messages':0}},
            'broker_stats_delta':{'calls':0,'successful_calls':0}}

    def test_scoring_invokes_fixed_judge_once_and_reuses_completed_result(self):
        with tempfile.TemporaryDirectory() as d:
            root=Path(d); case,record=self.make_native(root)
            package=root/'package'; (package/'evaluator').mkdir(parents=True); (package/'dev_cases/dev_001').mkdir(parents=True)
            (package/'evaluator/result_rubric.md').write_text('seven dimensions')
            (package/'evaluator/result_dimensions.json').write_text(json.dumps(MAXIMA))
            (package/'dev_cases/dev_001/input.md').write_text('perform the dev task')
            def invoke(command,**kwargs):
                self.assertIn('--rubric-dimensions',command)
                out=Path(command[command.index('--output-dir')+1]); out.mkdir()
                (out/'result_score_contract.json').write_text(json.dumps(fake_contract('dev_001',23)))
                return subprocess.CompletedProcess(command,0,'','')
            # Only the external judge subprocess is replaced. Receipt/input
            # binding and acceptance run through the real implementation.
            with patch('evaluator.dev_result.subprocess.run',side_effect=invoke) as called:
                first=score_public_case(root=package,candidate_digest='A',record=record,case_output=case,broker_endpoint='http://127.0.0.1:9/v1/responses')
                second=score_public_case(root=package,candidate_digest='A',record=record,case_output=case,broker_endpoint='http://127.0.0.1:9/v1/responses')
                self.assertEqual(called.call_count,1)
            self.assertEqual(semantic_feedback(first,'dev_001','A')['score'],23)
            self.assertEqual(first['semantic_result'],second['semantic_result'])

    def test_infrastructure_record_never_calls_judge(self):
        with tempfile.TemporaryDirectory() as d, patch('evaluator.dev_result.subprocess.run') as called:
            result=score_public_case(root=Path(d),candidate_digest='A',record={'case_id':'dev_001','classification':'broker_infrastructure_error'},case_output=Path(d)/'case',broker_endpoint='unused')
            called.assert_not_called()
            self.assertFalse(semantic_feedback(result,'dev_001','A')['valid'])

    def test_actual_shared_judge_http_roundtrip_uses_seven_dimensions_and_caches(self):
        """Actual fixed CLI and HTTP transport; local synthetic server, zero real API."""
        received=[]
        class Handler(BaseHTTPRequestHandler):
            def log_message(self,*_args):pass
            def do_POST(self):
                body=json.loads(self.rfile.read(int(self.headers['Content-Length'])))
                received.append(body)
                response=fake_contract('dev_001',37)
                response={key:response[key] for key in ('case_id','result_score','dimensions','major_errors','assessment')}
                response['result_state']='scoreable'
                payload={'id':'resp_local_synthetic','model':'deepseek-flash','status':'completed',
                    'usage':{'input_tokens':10,'output_tokens':10,'total_tokens':20},
                    'output':[{'type':'message','role':'assistant','content':[{'type':'output_text','text':json.dumps(response)}]}]}
                encoded=json.dumps(payload).encode()
                self.send_response(200);self.send_header('Content-Type','application/json')
                self.send_header('Content-Length',str(len(encoded)));self.end_headers();self.wfile.write(encoded)
        server=ThreadingHTTPServer(('127.0.0.1',0),Handler)
        thread=threading.Thread(target=server.serve_forever,daemon=True);thread.start()
        try:
            with tempfile.TemporaryDirectory() as d:
                case,record=self.make_native(Path(d))
                package=Path(__file__).resolve().parents[1]
                endpoint=f'http://127.0.0.1:{server.server_port}/v1/responses'
                result=score_public_case(root=package,candidate_digest='synthetic-candidate',record=record,case_output=case,broker_endpoint=endpoint)
                feedback=semantic_feedback(result,'dev_001','synthetic-candidate')
                contract_path=case/'semantic_result/judge/result_score_contract.json'
                detail=contract_path.read_text() if contract_path.is_file() else result.get('semantic_result')
                self.assertTrue(feedback['valid'],detail)
                self.assertEqual(feedback['score'],37)
                score_public_case(root=package,candidate_digest='synthetic-candidate',record=record,case_output=case,broker_endpoint=endpoint)
                self.assertEqual(len(received),1)
                self.assertEqual(received[0]['model'],'deepseek-flash')
                self.assertEqual(received[0]['reasoning']['effort'],'max')
                for key in MAXIMA:self.assertIn(key,json.dumps(received[0]))
        finally:
            server.shutdown();server.server_close();thread.join(timeout=2)

    def test_failed_judge_preserves_lower_artifact_and_does_not_resample(self):
        with tempfile.TemporaryDirectory() as d:
            root=Path(d);case,record=self.make_native(root)
            original=sha(record['authored_artifact'])
            package=Path(__file__).resolve().parents[1]
            with patch('evaluator.dev_result.subprocess.run',return_value=subprocess.CompletedProcess([],1,'','unresolved')) as called:
                first=score_public_case(root=package,candidate_digest='A',record=record,case_output=case,broker_endpoint='unused')
                second=score_public_case(root=package,candidate_digest='A',record=record,case_output=case,broker_endpoint='unused')
                self.assertEqual(called.call_count,1)
            self.assertFalse(semantic_feedback(first,'dev_001','A')['valid'])
            self.assertIn('no resampling',second['semantic_result']['reason'])
            self.assertEqual(sha(record['authored_artifact']),original)


if __name__=='__main__':unittest.main()
