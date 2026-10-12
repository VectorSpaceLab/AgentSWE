"""Synthetic terminal-evidence and real controller routing regressions; no API."""
import copy
import json
import os
from pathlib import Path
import subprocess
import tempfile
import time
import unittest
from unittest.mock import patch

from controller.dev_feedback import MAXIMA,semantic_feedback
from controller.two_round_controller import TwoRoundController,DEV,tree_digest
from evaluator.candidate_outcome import inspect_core,prove_terminal_failure,candidate_zero_receipt,validate_zero_contract,sha
from evaluator.dev_result import score_public_case
from evaluator.durable_state import capture_durable_state
from evaluator.semantic_finalize import artifact_for,evidence_files
from evaluator import semantic_finalize as finalizer
from evaluator.test_dev_feedback import fake_contract
from lower_agent.entry_contract import PRODUCTION_ENTRY
from lower_agent.launcher import broker_stats_delta

ROOT=Path(__file__).resolve().parents[1]


def write(path,value):
    path.parent.mkdir(parents=True,exist_ok=True);path.write_text(json.dumps(value))


def terminal_fixture(case_output,case_id='dev_001',candidate_digest='a'*64,body=None,status='completed'):
    """Explicit synthetic evidence; never a real product/model performance claim."""
    case_output.mkdir(parents=True,exist_ok=False);workspace=case_output/'workspace';workspace.mkdir()
    if body is not None:(workspace/'agent_result.json').write_bytes(body)
    admission=inspect_core(workspace/'agent_result.json',case_id,deadline=time.monotonic()+5)
    contract={'valid':False,'core_admission':admission,'error':'synthetic schema/absent auxiliary'}
    sandbox={'valid':True,'gateway_wrapper_reaped':True,'transport':{'incomplete':False}}
    native={'case_id':case_id,'case_bundle_sha256':'b'*64,'infrastructure_errors':[],
        'world':{'cleanup_complete':True,'failure':None},'cluster':{'sandbox_evidence':[copy.deepcopy(sandbox),copy.deepcopy(sandbox)]},
        'oracle_observations':{'accepted_channel_messages':0}}
    agent={'status':status,'exit_code':0 if status=='completed' else 1 if status=='candidate_process_error' else None,
        'evaluator_authored_result':False,
        'cleanup_complete':True,'started_monotonic':100,'ended_monotonic':102 if status!='timeout' else 570,'deadline_monotonic':570}
    scope={'unit':'synthetic-test-owned-scope','valid':True,'timed_out':False,'cleanup':{'complete':True}}
    counters={key:0 for key in ('calls','failures','successful_calls','client_failures','provider_failures','usage_unknown_calls','in_flight_calls')}
    stats={'broker_instance_id':'synthetic-test-broker','runtime':counters,
        'protocol':{'model':'deepseek-flash','reasoning_effort':'high'}}
    runtime={'candidate_runtime_ready':True,'source_digest_stable':True,'candidate_source_digest':candidate_digest,
        'candidate_source_digest_after_build':candidate_digest,'runtime_source_digest':'c'*64}
    runtime_path=case_output/'test-runtime-manifest.json';write(runtime_path,runtime)
    state=case_output/'gateway_state';state.mkdir()
    durable=capture_durable_state(state=state,output=case_output/'durable-state',
        deadline=time.monotonic()+5,expected={},writers_stopped=True)
    durable['case_binding']={'case_id':case_id,'case_bundle_sha256':'b'*64,'owned_scope_unit':scope['unit']}
    lower={'case_id':case_id,'command_entry':PRODUCTION_ENTRY,'status':'completed' if status=='completed' else 'partial',
        'health':{'status':'ok'},'artifact_contract':contract,'sandbox':sandbox,'native_case':native,'native_agent':agent}
    record={'case_id':case_id,'classification':'candidate_product_failure','production_entry':PRODUCTION_ENTRY,
        'artifact_contract':contract,'native_case':native,'native_agent':agent,'case_resources':scope,'durable_state':durable,
        'broker_stats_delta':broker_stats_delta(stats,stats),'frozen_candidate_digest_before':'c'*64,
        'frozen_candidate_digest_after':'c'*64,'frozen_candidate_digest_stable':True,
        'candidate_runtime_manifest':str(runtime_path),'candidate_runtime_manifest_sha256':sha(runtime_path),
        'timing_contract':{'elapsed_seconds':580},'model':'deepseek-flash','reasoning_effort':'high',
        'credential_mode':'placeholder-only','candidate_sensitive_environment_keys':[],'run_mode':'formal',
        'agent_authored_artifact':admission['state']=='semantic_review',
        'authored_artifact':str(workspace/'agent_result.json') if admission['state']=='semantic_review' else None,
        'artifact_sha256':admission['sha256']}
    for name,value in (('hidden_case_attestation.json',record),('run_report.json',lower),
                       ('case_resources/resource-attestation.json',scope),('broker_before_suite_case.json',stats),
                       ('broker_after_suite_case.json',stats),('trajectory.json',[{'method':'synthetic-no-op'}])):
        write(case_output/name,value)
    return record


class CandidateOutcomeTests(unittest.TestCase):
    def test_real_private_value_is_not_forwarded_to_judge(self):
        with tempfile.TemporaryDirectory() as directory:
            case=Path(directory)/'case';record=terminal_fixture(case,body=b'{"decision":"test-private-token"}')
            admission=inspect_core(case/'workspace/agent_result.json','dev_001',deadline=time.monotonic()+5,
                private_values=['test-private-token'])
            record['artifact_contract']['core_admission']=admission
            lower=json.loads((case/'run_report.json').read_text());lower['artifact_contract']=record['artifact_contract']
            write(case/'run_report.json',lower);write(case/'hidden_case_attestation.json',record)
            with patch('evaluator.dev_result.subprocess.run') as provider:
                scored=score_public_case(root=ROOT,candidate_digest='a'*64,record=record,case_output=case,broker_endpoint='unused')
            provider.assert_not_called();self.assertEqual(semantic_feedback(scored,'dev_001','a'*64)['score'],0)
            contract=Path(scored['semantic_result']['contract_path']).read_text()
            self.assertNotIn('test-private-token',contract)

    def test_core_shape_missing_and_nonregular_are_not_field_quality_errors(self):
        with tempfile.TemporaryDirectory() as directory:
            path=Path(directory)/'agent_result.json'
            self.assertEqual(inspect_core(path,'dev_001',deadline=time.monotonic()+5)['reason'],'missing_core')
            for body,expected in ((b'{','non_json_core'),(b'[]','non_object_core'),(b'{}','parseable_core')):
                path.write_bytes(body)
                self.assertEqual(inspect_core(path,'dev_001',deadline=time.monotonic()+5)['reason'],expected)
            path.unlink();os.mkfifo(path)
            self.assertEqual(inspect_core(path,'dev_001',deadline=time.monotonic()+5)['reason'],'non_regular_core')

    def test_symlink_does_not_read_outside_and_capacity_is_not_candidate_zero(self):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory);source=root/'private';source.write_text('private bytes')
            path=root/'agent_result.json';path.symlink_to(source)
            result=inspect_core(path,'dev_001',deadline=time.monotonic()+5)
            self.assertEqual(result['reason'],'non_regular_core');self.assertIsNone(result['sha256'])
            path.unlink();path.write_bytes(b'X'*1_500_001)
            self.assertEqual(inspect_core(path,'dev_001',deadline=time.monotonic()+5)['state'],'evidence_unavailable')

    def test_unknown_evaluator_observation_and_expired_time_cannot_be_fatal(self):
        with tempfile.TemporaryDirectory() as directory:
            path=Path(directory)/'agent_result.json'
            self.assertEqual(inspect_core(path,'dev_001',deadline=time.monotonic()-1)['state'],'evidence_unavailable')
            with patch.object(Path,'lstat',side_effect=PermissionError('synthetic')):
                self.assertEqual(inspect_core(path,'dev_001',deadline=time.monotonic()+5)['state'],'evidence_unavailable')

    def test_evaluator_authorship_and_missing_credential_isolation_do_not_get_zero(self):
        for field in ('authorship','credential'):
            with self.subTest(field=field),tempfile.TemporaryDirectory() as directory:
                case=Path(directory)/'case';record=terminal_fixture(case)
                lower=json.loads((case/'run_report.json').read_text())
                if field=='authorship':
                    record['native_agent']['evaluator_authored_result']=True
                    lower['native_agent']=record['native_agent'];write(case/'run_report.json',lower)
                else:record['credential_mode']='unverified'
                write(case/'hidden_case_attestation.json',record)
                with self.assertRaises(ValueError):prove_terminal_failure(record,case,'a'*64)

    def test_healthy_terminal_absence_and_unparseable_core_get_bound_zero_without_judge(self):
        for body in (None,b'{',b'[]'):
            with self.subTest(body=body),tempfile.TemporaryDirectory() as directory:
                case=Path(directory)/'case';record=terminal_fixture(case,body=body)
                with patch('evaluator.dev_result.subprocess.run') as provider:
                    first=score_public_case(root=ROOT,candidate_digest='a'*64,record=record,case_output=case,broker_endpoint='unused')
                    second=score_public_case(root=ROOT,candidate_digest='a'*64,record=record,case_output=case,broker_endpoint='unused')
                provider.assert_not_called();feedback=semantic_feedback(first,'dev_001','a'*64)
                self.assertTrue(feedback['valid'],first.get('semantic_result'));self.assertEqual(feedback['score'],0)
                self.assertEqual(first['semantic_result'],second['semantic_result'])

    def test_timeout_or_candidate_exit_error_needs_healthy_case_not_a_provider_failure(self):
        for status in ('timeout','candidate_process_error'):
            with self.subTest(status=status),tempfile.TemporaryDirectory() as directory:
                case=Path(directory)/'case';record=terminal_fixture(case,status=status)
                proof,_=prove_terminal_failure(record,case,'a'*64)
                self.assertEqual(proof['native_terminal_status'],status)

    def test_forged_status_stale_case_scope_unknown_broker_and_runtime_mismatch_are_rejected(self):
        for mutation in ('status','scope','unknown','provider','case','digest','cleanup','early_timeout'):
            with self.subTest(mutation=mutation),tempfile.TemporaryDirectory() as directory:
                case=Path(directory)/'case';record=terminal_fixture(case)
                if mutation=='status':record['classification']='candidate_behavior_observed'
                if mutation=='scope':record['case_resources']['unit']='foreign'
                if mutation in ('unknown','provider'):
                    path=case/'broker_after_suite_case.json';stats=json.loads(path.read_text())
                    stats['runtime']['usage_unknown_calls' if mutation=='unknown' else 'provider_failures']=1;write(path,stats)
                if mutation=='case':record['native_case']['case_id']='test_006'
                if mutation=='digest':record['frozen_candidate_digest_after']='d'*64
                if mutation=='cleanup':record['native_agent']['cleanup_complete']=False
                if mutation=='early_timeout':record['native_agent'].update(status='timeout',ended_monotonic=101)
                with self.assertRaises(ValueError):prove_terminal_failure(record,case,'a'*64)

    def test_cached_zero_invalidates_if_artifact_appears_or_evidence_is_changed(self):
        with tempfile.TemporaryDirectory() as directory:
            case=Path(directory)/'case';record=terminal_fixture(case)
            scored=score_public_case(root=ROOT,candidate_digest='a'*64,record=record,case_output=case,broker_endpoint='unused')
            self.assertTrue(semantic_feedback(scored,'dev_001','a'*64)['valid'])
            (case/'workspace/agent_result.json').write_text('{}')
            self.assertFalse(semantic_feedback(scored,'dev_001','a'*64)['valid'])

    def test_wrong_candidate_and_transplanted_rollout_do_not_consume_a_zero(self):
        with tempfile.TemporaryDirectory() as directory:
            case=Path(directory)/'case';record=terminal_fixture(case)
            scored=score_public_case(root=ROOT,candidate_digest='a'*64,record=record,case_output=case,broker_endpoint='unused')
            self.assertFalse(semantic_feedback(scored,'dev_001','z'*64)['valid'])
            scored['native_case']['case_bundle_sha256']='e'*64
            self.assertFalse(semantic_feedback(scored,'dev_001','a'*64)['valid'])

    def test_auxiliary_schema_and_wrong_claimed_case_still_reach_semantic_judge(self):
        for body in (b'{"case_id":"dev_001","decision":"partial"}',b'{"case_id":"test_006"}',b'{}'):
            with self.subTest(body=body),tempfile.TemporaryDirectory() as directory:
                case=Path(directory)/'case';record=terminal_fixture(case,body=body)
                record['classification']='candidate_behavior_observed'
                def invoke(command,**kwargs):
                    out=Path(command[command.index('--output-dir')+1]);out.mkdir()
                    write(out/'result_score_contract.json',fake_contract('dev_001',23))
                    return subprocess.CompletedProcess(command,0,'','')
                with patch('evaluator.dev_result.subprocess.run',side_effect=invoke) as provider:
                    scored=score_public_case(root=ROOT,candidate_digest='a'*64,record=record,case_output=case,broker_endpoint='unused')
                self.assertEqual(provider.call_count,1)
                self.assertEqual(semantic_feedback(scored,'dev_001','a'*64)['score'],23)
                self.assertFalse(record['artifact_contract']['valid'])

    def test_hidden_partial_uses_original_bytes_and_array_trajectory(self):
        with tempfile.TemporaryDirectory() as directory:
            run=Path(directory);case=run/'hidden/test_001'
            record=terminal_fixture(case,'test_001',body=b'{"case_id":"wrong","decision":"partial"}')
            artifact=artifact_for('openclaw',run,'test_001',case/'hidden_case_attestation.json',record)
            trajectory,_,_=evidence_files(ROOT,run,'openclaw','test_001',case/'hidden_case_attestation.json',record,artifact)
            self.assertEqual(json.loads(trajectory.read_text())['events'][0],[{'method':'synthetic-no-op'}])
            self.assertEqual(artifact.read_bytes(),b'{"case_id":"wrong","decision":"partial"}')

    def test_two_dev_zeroes_consume_one_round_and_duplicate_digest_is_idempotent(self):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory);source=root/'source';source.mkdir();(source/'x').write_text('one')
            def evaluate(number,candidate):
                digest=tree_digest(candidate);results={}
                for case_id in DEV:
                    output=root/'lower'/str(number)/case_id;record=terminal_fixture(output,case_id,digest)
                    results[case_id]=score_public_case(root=ROOT,candidate_digest=digest,record=record,case_output=output,broker_endpoint='unused')
                return results
            controller=TwoRoundController(root/'controller',evaluate)
            first=controller.submit(source);self.assertEqual(first.classification,'completed')
            self.assertEqual(len(controller.records),1);self.assertIs(controller.submit(source),first)
            feedback=json.loads((root/'controller/feedback/candidate_001.json').read_text())
            self.assertEqual(feedback['dev_scores'],{'dev_001':0,'dev_002':0})
            self.assertFalse(feedback['dev_passed']);self.assertIsNone(controller.frozen)

    def test_candidate_zero_and_infrastructure_mix_does_not_consume_round(self):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory);source=root/'source';source.mkdir();(source/'x').write_text('one')
            def evaluate(number,candidate):
                case=root/'lower/dev_001';digest=tree_digest(candidate);record=terminal_fixture(case,'dev_001',digest)
                return {'dev_001':score_public_case(root=ROOT,candidate_digest=digest,record=record,case_output=case,broker_endpoint='unused'),
                    'dev_002':{'case_id':'dev_002','classification':'broker_infrastructure_error'}}
            controller=TwoRoundController(root/'controller',evaluate)
            self.assertEqual(controller.submit(source).classification,'infrastructure-invalid')
            self.assertEqual(len(controller.records),0)

    def test_real_hidden_finalizer_aggregates_six_evidenced_zeroes_and_keeps_code_independent(self):
        with tempfile.TemporaryDirectory() as directory:
            run=Path(directory);frozen=run/'lifecycle/frozen_candidate';frozen.mkdir(parents=True)
            (frozen/'source').write_text('synthetic frozen implementation');digest=tree_digest(frozen)
            freeze={'candidate_digest':digest,'source_submission':1,'freeze_reason':'builder_exit',
                'max_dev_rounds':10,'n_concurrent':1,'dev_passed_is_automatic_freeze':False}
            write(run/'lifecycle/freeze_manifest.json',freeze)
            write(run/'lifecycle/dev_lifecycle.json',{'records':[{'dev':{'dev_001':{},'dev_002':{}}}],
                'max_dev_rounds':10,'n_concurrent':1,'dev_passed_is_automatic_freeze':False})
            records={case:terminal_fixture(run/'hidden'/case,case,digest) for case in finalizer.CASES}
            write(run/'hidden/hidden_result.json',records)
            hidden_state={'state':'completed','candidate_digest':digest,
                'freeze_manifest_sha256':sha(run/'lifecycle/freeze_manifest.json'),
                'hidden_started_at':'2026-09-12T01:00:00+00:00',
                'hidden_completed_at':'2026-09-12T01:10:00+00:00'}
            write(run/'lifecycle/hidden_execution_state.json',hidden_state)
            write(run/'hidden/hidden-after-freeze-attestation.json',{**hidden_state,'freeze_before_hidden':True,'evidence_kind':'formal','case_inventory':list(finalizer.CASES)})
            calls=[]
            def invoke(command,**kwargs):
                calls.append(command);self.assertIn(str(finalizer.CREATE_CODE_JUDGE),command)
                out=Path(command[command.index('--output-dir')+1]);out.mkdir(parents=True)
                value=fake_contract('unused',17)
                write(out/'code_score_contract.json',{'contract_valid':True,'code_score_publishable':True,
                    'code_score':17,'judge':value['judge'],'provider_usage':value['provider_usage']})
                return subprocess.CompletedProcess(command,0,'','')
            with patch.object(finalizer.subprocess,'run',side_effect=invoke):
                code=finalizer.finalize(['--layout','openclaw','--run-dir',str(run),
                    '--credential-file','/unused-no-key-read','--result-broker-endpoint','unused',
                    '--code-rubric',str(ROOT/'evaluator/result_rubric.md')])
            result=json.loads((run/'formal_aggregation.json').read_text())
            self.assertEqual(code,0,result);self.assertEqual(result['result_axis']['score'],0)
            self.assertEqual(result['code_axis']['score'],17);self.assertIsNone(result['combined_score'])
            self.assertEqual(len(calls),1)
            self.assertTrue(all(not item['invoked'] for item in result['result_judge_contracts'].values()))

    def test_missing_lifecycle_cannot_publish_even_complete_hidden_candidate_failures(self):
        with tempfile.TemporaryDirectory() as directory:
            run=Path(directory);frozen=run/'lifecycle/frozen_candidate';frozen.mkdir(parents=True)
            (frozen/'source').write_text('synthetic');write(run/'lifecycle/freeze_manifest.json',{'candidate_digest':tree_digest(frozen)})
            with patch.object(finalizer.subprocess,'run') as provider:
                self.assertEqual(finalizer.finalize(['--layout','openclaw','--run-dir',str(run),
                    '--credential-file','/unused','--result-broker-endpoint','unused',
                    '--code-rubric',str(ROOT/'evaluator/result_rubric.md')]),2)
            provider.assert_not_called()


if __name__=='__main__':unittest.main()
