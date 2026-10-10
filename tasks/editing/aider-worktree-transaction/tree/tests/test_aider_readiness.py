"""Actual Aider BuilderLifecycle/controller/materialization/export; fake lower and syntax runtime."""
import contextlib,io,hashlib,json,subprocess,sys,tempfile,time,types,unittest
from pathlib import Path
from unittest.mock import patch
ROOT=Path(__file__).resolve().parents[1]
sys.path[:0]=[str(ROOT),str(ROOT/'harbor'),str(ROOT.parents[1]/'v2-control-stage/release'),'@@AGENTSWE_EDITING_CONTROL@@']
from harbor import formal_one_stop as task
from harbor import agentloop_controller as core
from evaluator import readiness_bundle as bundle,formal_finalize as axes,materialize_candidate as materialize
from v2_usage_normalizers import make_broker_record_normalizers
from v2_readiness import load_and_validate
THREAD='01a09e3b-345a-7a02-b46d-a84351a83c23'

class AiderReadinessTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        self.run=Path(self.tmp.name).resolve()/'run';self.run.mkdir()
        self.source=self.run/'source';(self.source/'aider').mkdir(parents=True)
        (self.source/'aider/worktree_plan_adapter.py').write_text('VALUE = 1\n')
        (self.source/'tests').mkdir();(self.source/'tests/test_product.py').write_text('VALUE = 1\n')
        self.workspace=self.run/'workspace';self.workspace.mkdir()
        self.binding={'task':'aider','source_digest':'a'*64,'contract_digest':'b'*64,'registry_digest':'c'*64}
        task.write_json(self.run/'builder_job_config.json',{'jobs_dir':str(self.run/'jobs'),'job_name':'job'})
        self.agent=self.run/'jobs/job/trial/agent';(self.agent/'sessions').mkdir(parents=True)
        self.stream=[{'type':'thread.started','thread_id':THREAD},{'type':'turn.started'}]
        self.native=[{'timestamp':'now','type':'session_meta','payload':{'id':THREAD,'cli_version':'0.144.1','source':'exec','cwd':'/workspace'}},
            {'timestamp':'now','type':'turn_context','payload':{'model':'gpt-5.6-sol','effort':'max'}}]
        self.logs();self.outer=task.BuilderLifecycle(run_dir=self.run,workspace=self.workspace,lower_broker='http://unused',source=self.source,
            image='fixture',pilot_not_formal=True,max_dev_rounds=2,readiness_profile='single-dev-two-round-hidden-smoke-v1',current_binding=self.binding)
        self.controller=self.outer.controller;self.outer.bind_native()
        self.actual_run=subprocess.run
        p=patch.object(subprocess,'run',side_effect=self.dispatch);p.start();self.addCleanup(p.stop)
        p=patch.object(axes,'execution_verdict',return_value=({'classification':'candidate_zero'},{}));p.start();self.addCleanup(p.stop)
        (self.run/'brokers/public-lower-transport/lower_requests').mkdir(parents=True)
        self.lower_calls=0;self.bad_build=False

    def logs(self):
        (self.agent/'codex.txt').write_text(''.join(json.dumps(v)+'\n' for v in self.stream))
        (self.agent/'sessions/rollout-test.jsonl').write_text(''.join(json.dumps(v)+'\n' for v in self.native))

    def dispatch(self,command,**kwargs):
        if len(command)>1 and str(command[1]).endswith('materialize_candidate.py'):
            args=types.SimpleNamespace(**{name:Path(command[command.index('--'+name)+1]) for name in ('source','candidate','output','patch')})
            args.output.parent.mkdir(parents=True,exist_ok=True)
            def syntax(*a):
                for p in args.output.rglob('*.py'):compile(p.read_bytes(),str(p),'exec')
                return {'valid':not self.bad_build,'exit_code':0 if not self.bad_build else 2,'runtime':'controlled Python syntax compile','image_id':'same-image','stderr_tail':''}
            with patch.object(materialize,'syntax_check',side_effect=syntax),patch.object(materialize,'syntax_classification',return_value='ready_for_lower'),contextlib.redirect_stdout(io.StringIO()) as output:
                code=materialize.materialize(args,args.output.parent/'syntax',time.monotonic()+30)
            return subprocess.CompletedProcess(command,code,output.getvalue(),'')
        if len(command)>1 and str(command[1]).endswith('run_lower_agent_case.py'):
            self.lower_calls+=1;out=Path(command[command.index('--output-dir')+1]);out.mkdir(parents=True)
            case=command[command.index('--case-id')+1]
            task.write_json(out/'result.json',{'case_id':case,'classification':'candidate_zero','candidate_digest':command[command.index('--candidate-digest')+1],
                'broker':{'calls_delta':1,'failures_delta':0}})
            (self.run/'brokers/public-lower-transport/lower_requests'/ (str(self.lower_calls)*64)).mkdir()
            return subprocess.CompletedProcess(command,0,'','')
        return self.actual_run(command,**kwargs)

    def delivery(self,number):
        previous=self.controller.records[-1] if self.controller.records else None
        paths=['aider/worktree_plan_adapter.py','tests/test_product.py']
        (self.workspace/'solution.patch').write_text(''.join(f'diff --git a/{name} b/{name}\n--- a/{name}\n+++ b/{name}\n@@ -1 +1 @@\n-VALUE = 1\n+VALUE = {number+1}\n' for name in paths))
        task.write_json(self.workspace/'edit_report.json',{'schema_version':1,'summary':'revise product','changed_paths':paths,'tests':[],
            'feedback_response':'responded to actual feedback' if number==2 else ''})
        task.write_json(self.workspace/'run_report.json',{'schema_version':1,'status':'complete','commands':[],'duration_seconds':0,'errors':[],
            **dict.fromkeys(['deepseek','gateway','gateway_image','serper','web_retrieval'],0),'builder_session_id':THREAD,'submission_number':number,
            'revision_of_candidate_digest':previous['candidate_digest'] if previous else None,'feedback_digest':previous['feedback_digest'] if previous else None})

    def accepted(self,number):
        self.delivery(number);ack=self.controller.records[-1]['feedback_digest'] if number==2 else None
        status,payload=self.outer.submit(ack);self.assertEqual(status,200,payload)
        result=task.public_payload(payload);self.outer.record_feedback_delivery(result)
        for value in [{'type':'function_call','call_id':str(number),'arguments':json.dumps({'cmd':'submit_dev_candidate --wait'+(' --feedback-digest '+ack if ack else '')})},
                {'type':'function_call_output','call_id':str(number),'output':json.dumps({'status':200,'payload':result})}]:
            self.native.append({'timestamp':'now','type':'response_item','payload':value})
        self.logs()

    def finished(self):
        self.accepted(1);self.accepted(2);self.assertIsNone(self.controller.frozen)
        self.stream.append({'type':'turn.completed','usage':{'input_tokens':10,'cached_input_tokens':0,'output_tokens':2}});self.logs()
        native=self.outer.native_attestation();self.assertTrue(native['valid'],native)
        task.write_json(self.run/'native_builder_attestation.json',native)
        task.write_json(self.run/'readiness_builder_exit.json',{'exit_code':0,'native_valid':True})
        frozen=self.controller.freeze_latest(builder_exit_evidence={'exit_code':0,'native_valid':True})
        self.assertEqual(frozen['source_submission'],2);return frozen

    def test_two_valid_zero_rounds_freeze_after_exit(self):self.finished()
    def test_freeze_before_native_exit_rejected(self):
        self.accepted(1);self.accepted(2)
        with self.assertRaisesRegex(ValueError,'native exit'):self.controller.freeze_latest()
    def test_wrong_feedback_no_new_lower(self):
        self.accepted(1);self.delivery(2)
        self.assertEqual(self.outer.submit('0'*64)[0],409);self.assertEqual(self.lower_calls,1)
    def test_replay_rejected(self):
        self.accepted(1);self.assertEqual(self.outer.submit()[0],409)
    def test_bad_metadata_rejected(self):
        self.delivery(1);task.write_json(self.workspace/'run_report.json',{})
        with self.assertRaisesRegex(ValueError,'metadata'):self.outer.submit()
        self.assertEqual(self.lower_calls,0)
    def test_same_patch_rejected(self):
        self.accepted(1);old=(self.workspace/'solution.patch').read_bytes();self.delivery(2);(self.workspace/'solution.patch').write_bytes(old)
        with self.assertRaisesRegex(ValueError,'distinct'):self.outer.submit(self.controller.feedback_digest)
        self.assertEqual(self.lower_calls,1)
    def test_bad_build_no_round(self):
        self.delivery(1);self.bad_build=True
        self.assertEqual(self.outer.submit()[0],422);self.assertEqual(self.controller.records,[]);self.assertEqual(self.lower_calls,0)
    def receipt_requests(self,role,ids):
        root=self.run/f'brokers/{role}-lower-transport/lower_requests';root.mkdir(parents=True,exist_ok=True)
        (root/'.process.lock').write_text('')
        for rid in ids:
            request=root/rid;request.mkdir(exist_ok=True)
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
            'freeze_sha256':bundle.sha(self.run/'lifecycle/freeze_manifest.json'),
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
        normalizers=make_broker_record_normalizers('aider')
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
                    trusted_binding=self.binding,broker_normalizers=make_broker_record_normalizers('aider'))


if __name__=='__main__':unittest.main()
