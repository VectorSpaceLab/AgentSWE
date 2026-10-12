"""Actual lifecycle/feedback/freeze routing with explicitly synthetic work.

Product construction, case evaluation and native-thread binding are synthetic.
These tests prove only structural_complete; native completion is tested separately. No model runs,
semantic scores, or generated Candidate below constitute benchmark evidence.
"""
import copy
import json
import socket
from pathlib import Path
import subprocess
import sys
import tempfile
import threading
import unittest
from unittest.mock import patch

ROOT=Path(__file__).resolve().parents[2]
sys.path[:0]=[str(ROOT),str(ROOT/'agentloop')]
from agentloop import two_round_controller as control
from agentloop.stable_product import product_source_digest
from agentloop.protocol import tree_digest, write_json
from agentloop.run_hidden import _validate_freeze
from harbor import formal_one_stop as formal


class BuilderLifecycleContractTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root=Path(self.temp.name)
        self.workspace=self.root/'submission';self.workspace.mkdir()
        self.life=formal.BuilderLifecycle(self.root,self.workspace,'http://unused.invalid',
            'synthetic-image-not-started',run_kind='smoke',max_dev_rounds=10)
        self.assertIs(type(self.life.controller),control.Controller)
        self.addCleanup(self.life.close)
        self.build=self.start_patch(patch.object(control,'build_candidate',side_effect=self.fake_build))
        self.run_case=self.start_patch(patch.object(self.life.controller,'_run_case',side_effect=self.fake_case))
        self.start_patch(patch.object(formal, 'observe_thread', return_value={
            'thread_id':'11111111-1111-4111-8111-111111111111',
            'path':str(self.root/'synthetic-native-stream'), 'size':0, 'sha256':'0'*64}))
        self.write_delivery(1)

    def start_patch(self,patcher):
        value=patcher.start()
        self.addCleanup(patcher.stop)
        return value

    def write_delivery(self,number):
        (self.workspace/'solution.patch').write_text(
            'diff --git a/fixture.py b/fixture.py\nnew file mode 100644\n--- /dev/null\n'
            f'+++ b/fixture.py\n@@ -0,0 +1 @@\n+revision = {number}\n')
        write_json(self.workspace/'edit_report.json',{'synthetic_unit_fixture':True,'revision':number})
        write_json(self.workspace/'run_report.json',{'synthetic_unit_fixture':True})

    @staticmethod
    def fake_build(_source,delivery,out):
        repository=out/'repository';repository.mkdir(parents=True)
        (repository/'fixture.patch.txt').write_bytes((delivery/'solution.patch').read_bytes())
        return {'valid':True,'product_source_digest':product_source_digest(repository),'candidate_repo_digest':tree_digest(repository),'synthetic_unit_fixture':True}

    @staticmethod
    def fake_case(_candidate,case_id,out,*,hidden=False):
        result={'case_id':case_id,'valid':True,'classification':'candidate_valid',
            'classification_axis':'candidate','real_execution':False,
            'synthetic_unit_fixture':True,'broker':{'calls_delta':0,'successful_calls':0},
            'result_evaluation':{'score':80,'contract_valid':True,'round_consumed':True,
                'feedback':{'assessment':'Synthetic lifecycle routing fixture; not semantic judgment'}}}
        write_json(out/'launcher_result.json',result)
        return result

    def submit(self,number):
        self.write_delivery(number)
        status,value=self.life.submit(self.life.controller.feedback_digest)
        self.assertEqual(status,200,value)
        self.assertTrue(value['accepted'],value)
        self.life.feedback_written(value)  # Synthetic delivery; actual socket tested below.
        return value

    def freeze(self):
        value=formal.freeze_after_builder_exit(self.life,0)
        self.assertIsNotNone(value)
        return value

    def assert_hidden_gate(self,valid,pattern=None):
        _,errors=_validate_freeze(self.life.controller.run_dir,self.life.controller.frozen)
        self.assertEqual(not errors,valid,errors)
        if pattern:self.assertTrue(any(pattern in error for error in errors),errors)

    def persist_manifest(self,value):
        self.life.controller.frozen=value
        write_json(self.life.controller.run_dir/'freeze_manifest.json',value)

    def start_socket_client(self):
        # AF_UNIX has a short pathname limit; only this ephemeral socket is
        # placed in /tmp. All test data stays under the isolated output root.
        self.life.socket_path=Path('/tmp')/f'agentswe-v31-{self.life.token[:16]}.sock'
        self.assertFalse(self.life.socket_path.exists())
        formal.start_submission_socket(self.life)
        self.helper=self.root/'submit_dev_candidate'
        formal.write_submit_helper(self.helper)

    def client(self,*arguments,authorized=True):
        env={'PATH':'/usr/bin:/bin','PYTHONDONTWRITEBYTECODE':'1',
            'AGENTSWE_DEV_CONTROLLER_SOCKET':str(self.life.socket_path),
            'AGENTSWE_DEV_CONTROLLER_TOKEN':self.life.token if authorized else 'incorrect-unit-token'}
        process=subprocess.run([sys.executable,'-B',str(self.helper),*arguments],env=env,
            text=True,capture_output=True,timeout=5)
        return process,json.loads(process.stdout)

    def test_real_submit_helper_socket_returns_first_and_cached_feedback(self):
        self.start_socket_client()
        first,response=self.client('--wait')
        self.assertEqual(first.returncode,0,first.stderr)
        self.assertEqual(response['status'],200)
        self.assertIsInstance(response['payload']['feedback'],dict)
        second,replay=self.client('--wait')
        self.assertEqual(second.returncode,0,second.stderr)
        self.assertTrue(replay['payload']['duplicate_digest'])
        self.assertEqual(replay['payload']['feedback'],response['payload']['feedback'])
        self.assertEqual(replay['payload']['feedback_digest'],response['payload']['feedback_digest'])
        self.assertEqual(self.build.call_count,1)
        self.freeze()
        self.assertTrue(formal.builder_attestation(self.life,0)['structural_complete'])
        self.assert_hidden_gate(True)
        self.life.close();self.assertFalse(self.life.socket_path.exists())

    def test_real_submit_helper_acks_feedback_in_same_controller_session(self):
        self.start_socket_client()
        first,response=self.client('--wait')
        self.assertEqual(first.returncode,0,first.stderr)
        digest=response['payload']['feedback_digest']
        self.write_delivery(2)
        second,revision=self.client('--feedback-digest',digest,'--wait')
        self.assertEqual(second.returncode,0,second.stderr)
        self.assertEqual(revision['payload']['feedback_digest_ack'],digest)
        self.assertEqual(revision['payload']['builder_session_id'],response['payload']['builder_session_id'])
        self.assertEqual(len(self.life.controller.records),2)
        self.freeze();self.assertTrue(formal.builder_attestation(self.life,0)['structural_complete'])
        self.assert_hidden_gate(True)

    def test_real_submit_helper_unauthorized_request_never_evaluates(self):
        self.start_socket_client()
        process,response=self.client('--wait',authorized=False)
        self.assertEqual(process.returncode,1)
        self.assertEqual(response['status'],401)
        self.assertEqual(self.life.controller.records,[])
        self.build.assert_not_called();self.run_case.assert_not_called()

    def test_direct_submit_does_not_claim_feedback_delivery(self):
        status, result = self.life.submit(None)
        self.assertEqual(status, 200, result)
        self.assertFalse(any(event['event']=='feedback_delivered' for event in self.life.events))
        self.freeze()
        self.assertFalse(formal.builder_attestation(self.life,0)['structural_complete'])

    def test_disconnected_socket_never_claims_feedback_was_sent(self):
        self.start_socket_client()
        entered = threading.Event(); release = threading.Event(); failed = threading.Event()
        original_event = self.life.event
        def event(name, **fields):
            original_event(name, **fields)
            if name == 'feedback_write_failed': failed.set()
        self.life.event = event
        def case(*args, **kwargs):
            entered.set(); self.assertTrue(release.wait(3))
            return self.fake_case(*args, **kwargs)
        self.run_case.side_effect = case
        client=socket.socket(socket.AF_UNIX,socket.SOCK_STREAM)
        try:
            client.connect(str(self.life.socket_path))
            client.sendall((json.dumps({'token':self.life.token,'action':'submit'})+'\n').encode())
            self.assertTrue(entered.wait(3))
            client.shutdown(socket.SHUT_RDWR);client.close();release.set()
            self.assertTrue(failed.wait(3))
            self.assertEqual(len(self.life.controller.records),1)
            self.assertFalse(any(e['event']=='feedback_delivered' for e in self.life.events))
            # Recovery is the cached accepted digest, never a second evaluation.
            self.run_case.side_effect=self.fake_case
            process,response=self.client('--wait')
            self.assertEqual(process.returncode,0,process.stderr)
            self.assertTrue(response['payload']['duplicate_digest'])
            self.assertEqual(self.build.call_count,1)
            self.freeze();self.assertTrue(formal.builder_attestation(self.life,0)['structural_complete'])
        finally:
            release.set();client.close()

    def test_builder_payload_does_not_expose_execution_ledger(self):
        def case(*args, **kwargs):
            value=self.fake_case(*args, **kwargs)
            value['private_oracle']='PRIVATE-ORACLE-SENTINEL'
            value['result_evaluation']['judge_prompt']='PRIVATE-JUDGE-SENTINEL'
            value['artifact_paths']=['@@AGENTSWE_LEGACY_DATA@@/secret/test_006']
            return value
        self.run_case.side_effect=case
        result=self.submit(1)
        for payload in (result,self.life.status()[1]):
            serialized=json.dumps(payload)
            self.assertNotIn('PRIVATE-',serialized)
            self.assertNotIn('/data/',serialized)
            self.assertNotIn('attempt_paths',serialized)
            self.assertNotIn('feedback_path',serialized)
        self.assertEqual(result['feedback'],json.loads(Path(self.life.controller.records[0]['feedback_path']).read_text()))

    def test_smoke_structural_completion_is_not_formal_lifecycle_evidence(self):
        self.submit(1);self.freeze()
        value=formal.builder_attestation(self.life,0)
        self.assertTrue(value['structural_complete'])
        self.assertEqual(value['evidence_kind'],'smoke')
        self.assertFalse(value['formal_lifecycle_eligible'])
        self.assertFalse(value['feedback_revision_observed'])

    def test_pilot_single_dev_stays_pilot_and_uses_its_explicit_hidden_gate(self):
        controller=self.life.controller
        controller.run_kind='pilot'
        controller.public_case_ids=('dev_001',)
        controller.hidden_case_ids=('test_001',)
        self.submit(1);self.freeze()
        value=formal.builder_attestation(self.life,0,pilot=True)
        self.assertTrue(value['structural_complete'],value)
        self.assertFalse(value['pilot_lifecycle_eligible'])  # No native Builder in this fixture.
        self.assertFalse(value['complete'])
        self.assertFalse(value['formal_lifecycle_eligible'])
        self.assertFalse(formal.builder_attestation(self.life,0,pilot=False)['structural_complete'])
        self.run_case.reset_mock()
        result=controller.run_hidden()
        self.assertTrue(result['valid'],result)
        self.assertEqual(result['case_ids'],['test_001'])
        self.assertEqual(self.run_case.call_count,1)
        self.assertFalse(result['formal_complete'])
        self.assertFalse(result['pilot_complete'])  # no real Agent was run

    def test_single_submission_can_exit_and_reach_all_hidden_dispatches(self):
        self.submit(1)
        self.assertIsNone(self.life.controller.frozen)
        self.assertTrue(self.life.controller.records[0]['dev_passed'])  # >60 is only feedback
        freeze=self.freeze()
        self.assertEqual(freeze['freeze_reason'],'builder_exit')
        self.assertFalse(freeze['revision_contract']['requires_feedback_bound_later_distinct_candidate'])
        self.assertFalse(freeze['revision_contract']['later_distinct_candidate_present'])
        self.assertTrue(freeze['feedback_chain_complete'])
        self.assertTrue(formal.builder_attestation(self.life,0)['structural_complete'])
        self.assert_hidden_gate(True)
        persisted=json.loads((self.life.controller.run_dir/'dev_lifecycle.json').read_text())
        self.assertEqual(persisted['frozen'],freeze)
        self.run_case.reset_mock()
        value=self.life.controller.run_hidden()
        self.assertTrue(value['valid'],value)
        self.assertEqual([call.args[1] for call in self.run_case.call_args_list],list(control.HIDDEN_CASES))
        self.assertFalse(value['formal_complete'])  # mocked work never becomes live evidence

    def test_single_round_with_explicit_one_round_budget_freezes_at_limit(self):
        self.life.controller.max_dev_rounds=1
        result=self.submit(1)
        self.assertTrue(result['frozen'])
        self.assertEqual(result['freeze']['freeze_reason'],'max_dev_rounds')
        self.assert_hidden_gate(True)

    def test_direct_controller_first_exit_passes_independent_freeze_gate(self):
        self.submit(1)
        self.life.controller.freeze_latest('builder_exit')
        self.assert_hidden_gate(True)

    def test_direct_early_exit_persists_freeze_for_controller_reload(self):
        self.submit(1)
        frozen=self.life.controller.freeze_latest('builder_exit')
        reloaded=control.Controller(ROOT,self.life.controller.run_dir,'http://unused.invalid',run_kind='smoke')
        self.assertEqual(reloaded.frozen,frozen)
        self.assertEqual(reloaded.feedback_digest,self.life.controller.feedback_digest)

    def test_two_feedback_bound_revisions_can_exit(self):
        first=self.submit(1);second=self.submit(2)
        self.assertEqual(second['feedback_digest_ack'],first['feedback_digest'])
        freeze=self.freeze()
        self.assertTrue(freeze['revision_contract']['requires_feedback_bound_later_distinct_candidate'])
        self.assertTrue(freeze['revision_contract']['later_distinct_candidate_present'])
        self.assertTrue(formal.builder_attestation(self.life,0)['structural_complete'])
        self.assert_hidden_gate(True)

    def test_ten_distinct_submissions_freeze_once_without_early_score_freeze(self):
        for number in range(1,11):
            result=self.submit(number)
            self.assertEqual(result['frozen'],number==10)
        frozen=self.life.controller.frozen
        self.assertEqual(frozen['freeze_reason'],'max_dev_rounds')
        self.assertEqual(frozen['accepted_submission_count'],10)
        self.assertTrue(formal.builder_attestation(self.life,0)['structural_complete'])
        self.assert_hidden_gate(True)
        self.write_delivery(11)
        status,result=self.life.submit(self.life.controller.feedback_digest)
        self.assertEqual(status,409);self.assertEqual(result['error'],'frozen')
        self.assertEqual(len(self.life.controller.records),10)
        self.assertEqual(self.build.call_count,10)

    def test_duplicate_feedback_query_is_idempotent_before_and_after_freeze(self):
        first=self.submit(1)
        for after_freeze in (False,True):
            if after_freeze:self.freeze()
            status,replay=self.life.submit(None)
            self.assertEqual(status,200,replay)
            self.assertTrue(replay['duplicate_digest'])
            self.assertFalse(replay['round_consumed'])
            self.assertEqual(replay['feedback'],first['feedback'])
            self.assertEqual(replay['feedback_digest'],first['feedback_digest'])
            self.assertEqual(len(self.life.controller.records),1)
            self.assertEqual(self.build.call_count,1)
            self.assertEqual(self.run_case.call_count,2)
        self.assertTrue(formal.builder_attestation(self.life,0)['structural_complete'])
        self.assert_hidden_gate(True)

    def test_missing_cached_feedback_is_not_accepted_as_a_duplicate(self):
        first=self.submit(1)
        Path(self.life.controller.records[0]['feedback_path']).write_text('{}')
        status,result=self.life.submit(None)
        self.assertEqual(status,503,result)
        self.assertFalse(result['accepted'])
        self.assertEqual(result['classification_axis'],'infrastructure')
        self.assertFalse(result['round_consumed'])

    def test_wrong_feedback_ack_does_not_build_or_consume_round(self):
        self.submit(1);self.write_delivery(2)
        status,result=self.life.submit('f'*64)
        self.assertEqual(status,409)
        self.assertEqual(result['error'],'latest_feedback_digest_ack_mismatch')
        self.assertEqual(len(self.life.controller.records),1)
        self.assertEqual(self.build.call_count,1)

    def test_builder_session_switch_is_rejected(self):
        self.submit(1);self.write_delivery(2)
        self.life.session_id='foreign-synthetic-session'
        status,result=self.life.submit(self.life.controller.feedback_digest)
        self.assertEqual(status,503)
        self.assertEqual(result['error'],'builder_session_changed_between_submissions')
        self.assertEqual(len(self.life.controller.records),1)

    def test_overlapping_submission_is_rejected_without_another_evaluation(self):
        entered=threading.Event();release=threading.Event();outcomes=[]
        def slow_case(*args,**kwargs):
            entered.set()
            if not release.wait(5):raise AssertionError('synthetic test barrier expired')
            return self.fake_case(*args,**kwargs)
        self.run_case.side_effect=slow_case
        worker=threading.Thread(target=lambda:outcomes.append(self.life.submit(None)))
        worker.start()
        try:
            self.assertTrue(entered.wait(5))
            status,result=self.life.submit(None)
            self.assertEqual(status,409,result)
            self.assertEqual(result['error'],'dev_evaluation_already_active')
            self.assertFalse(result['round_consumed'])
        finally:
            release.set();worker.join(6)
        self.assertFalse(worker.is_alive())
        self.assertEqual(len(outcomes),1)
        self.assertEqual(outcomes[0][0],200)
        self.assertEqual(len(self.life.controller.records),1)
        self.assertEqual(self.build.call_count,1)

    def test_no_submission_cannot_freeze_or_dispatch_hidden(self):
        with self.assertRaisesRegex(RuntimeError,'no structurally valid'):
            self.life.controller.freeze_latest()
        self.assertFalse(self.life.controller.run_hidden()['valid'])
        self.run_case.assert_not_called()

    def test_invalid_submission_does_not_consume_round(self):
        (self.workspace/'solution.patch').write_text('')
        status,result=self.life.submit(None)
        self.assertEqual(status,409)
        self.assertEqual(result['error'],'invalid_submission')
        self.assertEqual(self.life.controller.records,[])
        self.build.assert_not_called()

    def test_infrastructure_case_is_not_an_accepted_dev_round(self):
        def infrastructure(*args,**kwargs):
            value=self.fake_case(*args,**kwargs)
            value.update(valid=False,classification='provider_infrastructure_error',classification_axis='infrastructure')
            value['result_evaluation']={'contract_valid':False,'round_consumed':False,'score':None}
            return value
        self.run_case.side_effect=infrastructure
        status,result=self.life.submit(None)
        self.assertEqual(status,503,result)
        self.assertFalse(result['accepted']);self.assertFalse(result['round_consumed'])
        self.assertEqual(self.life.controller.records,[])
        self.assertEqual(len(self.life.controller.infrastructure_attempts),1)
        self.assertIsNone(formal.freeze_after_builder_exit(self.life,0))

    def test_max_round_freeze_cannot_be_used_before_limit(self):
        self.submit(1)
        with self.assertRaisesRegex(RuntimeError,'submission limit'):
            self.life.controller.freeze_latest('max_dev_rounds')
        self.assertIsNone(self.life.controller.frozen)

    def test_invalid_freeze_reason_is_rejected(self):
        self.submit(1)
        with self.assertRaisesRegex(RuntimeError,'invalid freeze reason'):
            self.life.controller.freeze_latest('dev_passed')

    def test_interrupted_builder_does_not_turn_partial_work_into_normal_exit(self):
        self.submit(1)
        self.assertIsNone(formal.freeze_after_builder_exit(self.life,124))
        self.assertFalse(formal.builder_attestation(self.life,124)['structural_complete'])
        self.assertEqual(len(self.life.controller.records),1)
        self.run_case.reset_mock()
        self.assertFalse(self.life.controller.run_hidden()['valid'])
        self.run_case.assert_not_called()

    def test_already_budget_complete_freeze_survives_later_outer_interruption(self):
        self.life.controller.max_dev_rounds=1;self.submit(1)
        freeze=copy.deepcopy(self.life.controller.frozen)
        self.assertEqual(formal.freeze_after_builder_exit(self.life,124),freeze)
        self.assertTrue(formal.builder_attestation(self.life,124)['structural_complete'])

    def test_wrong_feedback_delivery_event_cannot_attest_complete(self):
        self.submit(1);self.freeze()
        event=next(event for event in self.life.events if event['event']=='feedback_delivered')
        event['feedback_digest']='f'*64
        self.assertFalse(formal.builder_attestation(self.life,0)['structural_complete'])

    def test_tampered_feedback_file_blocks_actual_hidden_dispatch(self):
        first=self.submit(1);self.freeze()
        Path(self.life.controller.records[0]['feedback_path']).write_text('{}')
        self.assert_hidden_gate(False,'feedback_evidence')
        self.run_case.reset_mock()
        result=self.life.controller.run_hidden()
        self.assertFalse(result['valid'],result)
        self.run_case.assert_not_called()

    def test_tampered_frozen_product_blocks_hidden(self):
        self.submit(1);freeze=self.freeze()
        item=Path(freeze['frozen_candidate_path'])/'fixture.patch.txt'
        item.chmod(0o600);item.write_text('tampered')
        self.assert_hidden_gate(False,'frozen_candidate_digest_changed')
        self.run_case.reset_mock()
        self.assertFalse(self.life.controller.run_hidden()['valid'])
        self.run_case.assert_not_called()

    def test_tampered_persisted_manifest_blocks_actual_hidden(self):
        self.submit(1);freeze=self.freeze()
        write_json(self.life.controller.run_dir/'freeze_manifest.json',{**freeze,'feedback_consumed':False})
        self.assert_hidden_gate(False,'persisted_freeze_manifest')
        self.run_case.reset_mock()
        self.assertFalse(self.life.controller.run_hidden()['valid'])
        self.run_case.assert_not_called()

    def test_wrong_latest_source_and_feedback_cannot_be_hidden_by_flags(self):
        self.submit(1);self.submit(2);freeze=copy.deepcopy(self.freeze())
        freeze.update(source_submission=1,feedback_digest='f'*64)
        self.persist_manifest(freeze)
        self.assert_hidden_gate(False,'latest_accepted')

    def test_forged_max_round_reason_is_rejected_by_independent_gate(self):
        self.submit(1);freeze=copy.deepcopy(self.freeze())
        freeze['freeze_reason']='max_dev_rounds';self.persist_manifest(freeze)
        self.assert_hidden_gate(False,'before_submission_limit')

    def test_feedback_chain_tamper_remains_rejected_for_two_rounds(self):
        self.submit(1);self.submit(2);self.freeze()
        ledger=self.life.controller.run_dir/'dev_lifecycle.json'
        data=json.loads(ledger.read_text());data['records'][1]['feedback_digest_ack']='f'*64
        write_json(ledger,data)
        self.assert_hidden_gate(False,'feedback_chain_broken')

    def test_single_submission_cannot_claim_a_nonexistent_revision(self):
        self.submit(1);freeze=copy.deepcopy(self.freeze())
        freeze['revision_contract']['later_distinct_candidate_present']=True
        self.persist_manifest(freeze)
        self.assert_hidden_gate(False,'nonexistent_revision')

    def test_single_submission_still_requires_valid_feedback_chain_flags(self):
        self.submit(1);freeze=copy.deepcopy(self.freeze())
        freeze['feedback_chain_complete']=False
        self.persist_manifest(freeze)
        self.assertFalse(formal.builder_attestation(self.life,0)['structural_complete'])
        self.assert_hidden_gate(False,'accepted_feedback_chain_is_not_complete')

    def test_formal_inventory_cannot_silently_drop_second_dev(self):
        self.submit(1);freeze=copy.deepcopy(self.freeze())
        freeze['public_case_inventory']=['dev_001'];self.persist_manifest(freeze)
        self.assert_hidden_gate(False,'public_case_inventory')

if __name__=='__main__':unittest.main()
