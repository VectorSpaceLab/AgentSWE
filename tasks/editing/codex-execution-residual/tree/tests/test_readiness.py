"""Actual controller -> actual exporter -> shared validator; no provider or Docker."""
import copy
import hashlib
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
CONTROL = ROOT.parents[1]/'v2-control-stage/release'
sys.path[:0] = [str(ROOT), str(ROOT/'harbor'), str(CONTROL),'@@AGENTSWE_EDITING_CONTROL@@']
from harbor import formal_one_stop as task
from evaluator import readiness_bundle as bundle
from evaluator import formal_finalize as axes
from v2_usage_normalizers import make_broker_record_normalizers
from v2_readiness import load_and_validate

THREAD='01a09e3b-345a-7a02-b46d-a84351a83c23'

class ReadinessTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        self.run=Path(self.temp.name).resolve()/'run';self.run.mkdir()
        self.workspace=self.run/'workspace';self.workspace.mkdir()
        self.binding={'task':'codex','source_digest':'a'*64,'contract_digest':'b'*64,'registry_digest':'c'*64}
        task.write_json(self.run/'builder_job_config.json',{'jobs_dir':str(self.run/'jobs'),'job_name':'job'})
        self.agent=self.run/'jobs/job/trial/agent';(self.agent/'sessions').mkdir(parents=True)
        self.stream=[{'type':'thread.started','thread_id':THREAD},{'type':'turn.started'}]
        self.native=[{'timestamp':'now','type':'session_meta','payload':{'id':THREAD,'cli_version':'0.144.1','source':'exec','cwd':'/workspace'}},
            {'timestamp':'now','type':'turn_context','payload':{'model':'gpt-5.6-sol','effort':'max'}}]
        self.logs()
        self.controller=task.DevController(run_dir=self.run,workspace=self.workspace,evaluate=self.evaluate,
            max_dev_rounds=2,public_cases=('dev_001',),readiness_profile=task.READINESS_PROFILE,current_binding=self.binding)
        self.controller.bind_native_thread()

    def logs(self):
        (self.agent/'codex.txt').write_text(''.join(json.dumps(v)+'\n' for v in self.stream))
        (self.agent/'sessions/rollout-test.jsonl').write_text(''.join(json.dumps(v)+'\n' for v in self.native))

    def evaluate(self,number,snapshot):
        out=self.run/f'evaluations/submission_{number:03d}'
        source=out/'build/worktree';source.mkdir(parents=True)
        (source/'main.rs').write_text('revision '+str(number))
        binary=out/'build/binary/codex';binary.parent.mkdir();binary.write_bytes(str(number).encode())
        build={'candidate_digest':task.tree_digest(snapshot),'candidate_repo_digest':task.tree_digest(source),
            'product_source_digest':task.product_source_digest(source),'product_source_digest_stage':'after_patch_apply_before_compilation',
            'cargo_build':{'exit_code':0}}
        task.write_json(out/'build/build_result.json',build)
        result={'case_id':'dev_001','classification':'candidate_zero','score':None,'readiness_execution_valid':True}
        task.write_json(out/'dev_001/result.json',result)
        task.write_json(out/'readiness_request_binding.json',{'candidate_digest':task.tree_digest(snapshot),
            'before':['1'*64] if number==2 else [], 'after':['1'*64,'2'*64] if number==2 else ['1'*64], 'request_ids':[str(number)*64]})
        return {'dev_001':result},binary

    def delivery(self,number):
        previous=self.controller.records[-1] if self.controller.records else None
        (self.workspace/'solution.patch').write_text('patch '+str(number))
        task.write_json(self.workspace/'edit_report.json',{'feedback_response':'Responded to observed feedback' if number==2 else ''})
        task.write_json(self.workspace/'run_report.json',{'builder_session_id':THREAD,'submission_number':number,
            'revision_of_candidate_digest':previous['candidate_digest'] if previous else None,
            'feedback_digest':previous['feedback_digest'] if previous else None})

    def accepted(self,number):
        self.delivery(number)
        ack=self.controller.records[-1]['feedback_digest'] if number==2 else None
        status,payload=self.controller.submit(ack);self.assertEqual(status,202,payload)
        self.controller.wait_idle();record=self.controller.records[-1]
        self.assertEqual(record['state'],'completed',record)
        result=self.controller.public(record);self.controller.feedback_written(result)
        for value in [{'type':'function_call','call_id':str(number),'arguments':json.dumps({'cmd':'submit_dev_candidate --wait'+(' --feedback-digest '+ack if ack else '')})},
                {'type':'function_call_output','call_id':str(number),'output':json.dumps({'status':200,'payload':result})}]:
            self.native.append({'timestamp':'now','type':'response_item','payload':value})
        self.logs()

    def finished(self):
        self.accepted(1);self.accepted(2)
        self.assertIsNone(self.controller.frozen)
        self.stream.append({'type':'turn.completed','usage':{'input_tokens':10,'cached_input_tokens':0,'output_tokens':2}});self.logs()
        native=self.controller.native_attestation();self.assertTrue(native['valid'],native)
        frozen=self.controller.freeze_latest(builder_exit_evidence={'exit_code':0,'native_valid':True,'builder_session_id':THREAD})
        self.assertEqual(frozen['source_submission'],2)
        return frozen

    def test_two_zero_score_rounds_freeze_after_native_exit_only(self):
        self.finished()

    def test_no_freeze_before_builder_exit(self):
        self.accepted(1);self.accepted(2)
        with self.assertRaisesRegex(RuntimeError,'exit evidence'):
            self.controller.freeze_latest()
        self.assertIsNone(self.controller.frozen)

    def test_wrong_feedback_does_not_launch_new_round(self):
        self.accepted(1);self.delivery(2)
        code,_=self.controller.submit('0'*64);self.assertEqual(code,409);self.assertEqual(len(self.controller.records),1)

    def test_exact_replay_is_rejected(self):
        self.accepted(1)
        code,payload=self.controller.submit();self.assertEqual(code,409);self.assertIn('replay',payload['error'])

    def test_metadata_mismatch_is_rejected_before_evaluation(self):
        self.delivery(1);task.write_json(self.workspace/'run_report.json',{})
        code,_=self.controller.submit();self.assertEqual(code,422);self.assertEqual(self.controller.records,[])

    def test_changed_source_after_round_two_refuses_freeze(self):
        self.accepted(1);self.accepted(2)
        self.stream.append({'type':'turn.completed','usage':{'input_tokens':10,'cached_input_tokens':0,'output_tokens':2}});self.logs()
        self.controller.native_attestation()
        (self.run/'evaluations/submission_002/build/worktree/main.rs').write_text('tampered')
        with self.assertRaisesRegex(RuntimeError,'source changed'):
            self.controller.freeze_latest(builder_exit_evidence={'exit_code':0,'native_valid':True,'builder_session_id':THREAD})

    def receipt_requests(self,role,ids):
        root=self.run/f'brokers/{role}-lower-transport/lower_requests';root.mkdir(parents=True)
        (root/'.process.lock').write_text('')
        for rid in ids:
            request=root/rid;request.mkdir()
            payload=json.dumps({'status':'completed','id':'response-'+rid,'model':'gpt-5.6-sol','error':None,
                'usage':{'input_tokens':3,'output_tokens':2,'total_tokens':5}}).encode()
            (request/'response.bin').write_bytes(payload)
            task.write_json(request/'intent.json',{'identity':rid,'protocol':'agentswe-lower-single-upstream/v1','body_sha256':'e'*64})
            task.write_json(request/'upstream_started.json',{'identity':rid,'actual_upstream_requests':1})
            task.write_json(request/'completed.json',{'identity':rid,'status':200,'actual_upstream_requests':1,'completed_responses':1,
                'payload_sha256':hashlib.sha256(payload).hexdigest(),'content_type':'application/json',
                'usage':{'input_tokens':3,'output_tokens':2,'total_tokens':5}})
        return root

    def evidence(self):
        self.finished()
        self.receipt_requests('public',['1'*64,'2'*64]);self.receipt_requests('hidden',['3'*64])
        for role,cid in [('public','a'*64),('hidden','b'*64)]:
            (self.run/f'brokers/{role}.cid').write_text(cid)
        usage={'input_tokens':3,'output_tokens':2,'total_tokens':5}
        task.write_json(self.run/'brokers/judge-judge-transport/broker_stats.json',{'schema_version':'agentswe-judge-broker-stats/v1',
            'protocol':{'model':'gpt-5.6-sol','reasoning_effort':'max'},'runtime':{'calls':1,'completed_calls':1,'successful_calls':1,
                'failures':0,'in_flight_calls':0,'upstream_attempts':1,'usage_unknown_calls':0,**usage},
            'attempts':[{'request_id':'r','state':'terminal','usage_unknown':False,'completed_response':True,'worker_reaped':True,
                'upstream_attempts':1,'usage':usage}]})
        task.write_json(self.run/'readiness_scoring/code/code_provider_response-attempts.json',{'schema_version':'agentswe-judge-http-attempts/v1',
            'logical_requests':1,'attempts':[{'attempt':1,'state':'terminal','http_status':200,'response_status':'completed',
                'response_id':'c','usage_known':True,'error_type':None,'usage':usage}]})
        frozen=self.controller.frozen
        task.write_json(self.run/'readiness_hidden_intent.json', {'case':'test_001',
            'freeze_sha256':bundle.sha(self.run/'freeze_manifest.json'),
            'candidate_digest':frozen['candidate_digest'],'started_at':task.now()})
        task.write_json(self.run/'readiness_hidden_attestation.json',{'case':'test_001','freeze_digest_stable':True,'result':{'classification':'candidate_zero'}})
        for role,rid in [('result','r'),('code','c')]:
            base=self.run/'readiness_scoring';inp=base/(role+'_input.json');out=base/(role+'_output.json')
            task.write_json(inp,{'role':role});task.write_json(out,{'role':role,'judge_session_id':role+':'+rid,'payload':{'synthetic_test_only':True}})
            task.write_json(base/(role+'_observation.json'),{'owner':'evaluator','run_id':self.run.name,'role':role,
                'judge_session_id':role+':'+rid,'request_id':rid,'model':'gpt-5.6-sol','effort':'max','state':'terminal',
                'formal':False,'input':{'path':str(inp),'sha256':bundle.sha(inp)},'output':{'path':str(out),'sha256':bundle.sha(out)}})
        resources=['a'*64,'b'*64]
        cleanup=self.run/'coordinator_cleanup';cleanup.mkdir()
        before=cleanup/'before.json';after=cleanup/'after.json'
        task.write_json(before,{'resources':{rid:{'run_id':self.run.name,'state':'terminal'} for rid in resources}})
        task.write_json(after,{'resources':{}})
        refs={}
        for rid in resources:
            path=cleanup/(rid+'.json');task.write_json(path,{'resource_id':rid});refs[rid]={'path':str(path),'sha256':bundle.sha(path)}
        path=cleanup/'cleanup.json'
        task.write_json(path,{'owner':'evaluator','run_id':self.run.name,'unit_state':'terminal','harbor_state':'terminal','builder_state':'terminal',
            'owned_resources':resources,'before':{'path':str(before),'sha256':bundle.sha(before)},
            'after':{'path':str(after),'sha256':bundle.sha(after)},'stats':refs})
        return path

    def test_actual_controller_export_and_shared_admission_with_original_receipt_bytes(self):
        cleanup=self.evidence()
        normalizers=make_broker_record_normalizers('codex')
        with patch.object(axes,'execution_verdict',return_value=({'classification':'candidate_zero'},{})):
            receipt=bundle.export(self.run,self.run/'bundle',cleanup_receipt=cleanup,trusted_binding=self.binding,broker_normalizers=normalizers)
        root=Path(receipt['bundle_root'])
        valid,errors=load_and_validate(root/'manifest.json',bundle_root=root,expected_manifest_sha256=receipt['manifest_sha256'],
            trusted_current_binding=self.binding,judge_output_validators={'result':lambda *a:[],'code':lambda *a:[]},broker_record_normalizers=normalizers)
        self.assertTrue(valid,errors)
        self.assertFalse(receipt['pipeline_ready'])
        original=self.run/'brokers/public-lower-transport/lower_requests'/('1'*64)/'response.bin'
        copied=root/'raw'/original.relative_to(self.run)
        self.assertEqual(original.read_bytes(),copied.read_bytes())
        copied.write_bytes(b'tampered')
        valid,errors=load_and_validate(root/'manifest.json',bundle_root=root,expected_manifest_sha256=receipt['manifest_sha256'],
            trusted_current_binding=self.binding,judge_output_validators={'result':lambda *a:[],'code':lambda *a:[]},broker_record_normalizers=normalizers)
        self.assertFalse(valid)

    def test_unknown_lower_receipt_never_exports_known_usage(self):
        cleanup=self.evidence()
        pending=self.run/'brokers/public-lower-transport/lower_requests'/('1'*64)
        (pending/'completed.json').unlink()
        with patch.object(axes,'execution_verdict',return_value=({'classification':'candidate_zero'},{})):
            with self.assertRaisesRegex(ValueError,'incomplete'):
                bundle.export(self.run,self.run/'bundle',cleanup_receipt=cleanup,
                    trusted_binding=self.binding,broker_normalizers=make_broker_record_normalizers('codex'))

    def test_build_failure_does_not_consume_a_readiness_round(self):
        self.delivery(1)
        self.controller.evaluate=lambda *a: ({'dev_001':{'classification':'candidate_zero','score':0,'readiness_execution_valid':True}},None)
        self.assertEqual(self.controller.submit()[0],202)
        self.controller.wait_idle()
        self.assertEqual(self.controller.records,[])
        self.assertEqual(len(self.controller.infrastructure_attempts),1)
        self.assertIsNone(self.controller.frozen)

    def test_same_patch_report_revision_is_rejected_before_lower(self):
        self.accepted(1);patch_bytes=(self.workspace/'solution.patch').read_bytes();self.delivery(2)
        (self.workspace/'solution.patch').write_bytes(patch_bytes)
        code,payload=self.controller.submit(self.controller.records[-1]['feedback_digest'])
        self.assertEqual(code,422);self.assertEqual(payload['error'],'revision_must_change_product_patch')

    def test_tampered_first_feedback_is_rejected_before_revision(self):
        self.accepted(1);self.delivery(2)
        Path(self.controller.records[-1]['feedback']).write_text('modified')
        code,payload=self.controller.submit(self.controller.records[-1]['feedback_digest'])
        self.assertEqual(code,409);self.assertEqual(payload['error'],'authoritative_feedback_bytes_changed')

    def test_same_product_materialization_cannot_freeze_even_with_different_patch(self):
        self.accepted(1);self.accepted(2)
        first=self.run/'evaluations/submission_001/build/worktree/main.rs'
        second=self.run/'evaluations/submission_002/build/worktree/main.rs';second.write_bytes(first.read_bytes())
        path=second.parent.parent/'build_result.json';build=task.read_json(path)
        build.update(candidate_repo_digest=task.tree_digest(second.parent),product_source_digest=task.product_source_digest(second.parent))
        task.write_json(path,build)
        self.stream.append({'type':'turn.completed','usage':{'input_tokens':10,'cached_input_tokens':0,'output_tokens':2}});self.logs()
        proof=self.controller.native_attestation()
        self.assertFalse(proof['valid']);self.assertIn('revision did not change product source',proof['errors'])

if __name__=='__main__': unittest.main()
