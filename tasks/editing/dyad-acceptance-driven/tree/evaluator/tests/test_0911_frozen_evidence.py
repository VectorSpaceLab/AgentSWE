"""Zero-provider fixtures exercise real producers; no real lower is claimed."""
import hashlib, importlib.util, json, runpy, shutil, socket, subprocess, sys, tempfile, unittest
from pathlib import Path
from unittest.mock import patch
ROOT=Path(__file__).resolve().parents[2]
sys.path[:0]=[str(ROOT),str(ROOT/'harbor')]
from harbor import formal_one_stop as formal
from evaluator import frozen_evidence as evidence
from evaluator.broker.candidate_broker import BrokerState
spec=importlib.util.spec_from_file_location('dyad_v60_axes',ROOT/'evaluator/formal_axes.py')
axes=importlib.util.module_from_spec(spec);spec.loader.exec_module(axes)
def read(p):return json.loads(p.read_text())
def sha(p):return hashlib.sha256(p.read_bytes()).hexdigest()

class FreezeEvidenceTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        self.run=Path(self.temp.name)/'run';self.workspace=self.run/'submission';self.workspace.mkdir(parents=True)
        (self.workspace/'solution.patch').write_text('one')
        self.life=formal.Lifecycle(run_dir=self.run,workspace=self.workspace,public_endpoint='unused',baseline={})
        self.addCleanup(self.life.close)
        self.counter=0
        def build(trigger):
            self.counter+=1;candidate=self.run/f'build/{self.counter}';candidate.mkdir(parents=True)
            (candidate/'product.js').write_text((self.workspace/'solution.patch').read_text())
            (candidate/'empty').mkdir();(candidate/'broken-doc').symlink_to('missing-doc\n')
            return {'ready_for_submission':True,'candidate_path':str(candidate),'candidate_digest':formal.tree_digest(candidate)}
        def case(candidate,case_id,*args):
            return {'case_id':case_id,'classification':'candidate_failure','product_entry_observed':True,
                'broker_calls_delta':1,'broker_successful_calls_delta':1,
                'semantic_feedback':{'classification':'scoreable','score':31,'contract_valid':True,'round_consumed':True,'assessment':'offline controller fixture; not a real lower'}}
        with patch.object(self.life,'bind_native_thread'),patch.object(self.life,'preflight',side_effect=build),patch.object(self.life,'_run_public_case',side_effect=case):
            first=self.life.submit(None,'unused');self.assertEqual(first[0],200)
            (self.workspace/'solution.patch').write_text('two')
            second=self.life.submit(first[1]['feedback_digest'],'unused');self.assertEqual(second[0],200)
        self.accepted=Path(self.life.records[-1]['candidate_path'])
        self.life._freeze(self.accepted);self.freeze=self.life.freeze_manifest
        self.frozen=Path(self.freeze['candidate_path']);self.document=self.run/'lifecycle/freeze_manifest.json';self.seal=self.document.with_suffix('.sha256')
        self.original=self.document.read_bytes();self.original_seal=self.seal.read_bytes()
        self.intentionally_changed=False
        self.addCleanup(self.check_original)
        self.net=patch.object(socket.socket,'connect',side_effect=AssertionError('network forbidden'));self.net.start();self.addCleanup(self.net.stop)
        self.proc=patch.object(subprocess,'Popen',side_effect=AssertionError('process forbidden'));self.proc.start();self.addCleanup(self.proc.stop)

    def check_original(self):
        if not self.intentionally_changed:
            self.assertEqual(self.document.read_bytes(),self.original);self.assertEqual(self.seal.read_bytes(),self.original_seal)

    def save_records(self,records):
        formal.write_json(self.run/'lifecycle/dev_lifecycle.json',records)

    def hidden(self,command_side_effect=None,clock=None):
        specs=self.run/'specs';specs.mkdir(exist_ok=True);(specs/'test_001.json').write_text('{"input":"offline hidden fixture"}')
        def copy(source,destination,dependency):
            shutil.copytree(source,destination,symlinks=True);return destination
        def command(cmd,*a,**k):
            output=Path(cmd[cmd.index('--output')+1])
            formal.write_json(output,{'classification':'infrastructure-invalid','fixture':'No model/lower executed'})
            return {'command':cmd,'exit_code':2,'timed_out':False,'duration_seconds':0,'stdout_tail':'','stderr_tail':'offline fixture'}
        with patch.object(formal,'copy_runtime',side_effect=copy),patch.object(formal,'command_result',side_effect=command_side_effect or command),patch.object(formal,'broker_stats',return_value=BrokerState('http://127.0.0.1:1',credential=None,dry_run=True).stats()):
            if clock is not None:
                with patch.object(formal,'now',side_effect=clock):
                    return formal.execute_hidden(self.life,'unused',specs,ROOT/'input/repository',case_ids=('test_001',),pilot_not_formal=True)
            return formal.execute_hidden(self.life,'unused',specs,ROOT/'input/repository',case_ids=('test_001',),pilot_not_formal=True)

    def test_real_producer_records_seal_and_dual_identity(self):
        self.assertEqual(self.freeze['schema_version'],'dyad-agentloop-freeze-v3')
        self.assertEqual(axes.frozen_identity_errors(self.freeze,self.run),[])
        bridge=axes.code_frozen_identity(self.freeze,self.run)
        self.assertNotEqual(bridge['lifecycle_candidate_digest'],bridge['code_candidate_digest'])
        self.assertEqual(axes._shared['checked_code_frozen_identity'](bridge,self.freeze,self.run,self.frozen),bridge['code_candidate_digest'])
        self.assertFalse(self.document.stat().st_mode & 0o222)

    def test_old_v2_is_refused_without_upgrade(self):
        old={**self.freeze,'schema_version':'dyad-agentloop-freeze-v2'}
        self.assertTrue(axes.frozen_identity_errors(old,self.run))
        self.assertEqual(self.document.read_bytes(),self.original)

    def test_accepted_path_source_and_modes(self):
        self.accepted.chmod(0o700)
        self.assertTrue(axes.frozen_identity_errors(self.freeze,self.run))
        self.accepted.chmod(0o755)
        (self.accepted/'product.js').write_text('different source')
        self.assertTrue(axes.frozen_identity_errors(self.freeze,self.run))
        records=read(self.run/'lifecycle/dev_lifecycle.json');records[-1]['candidate_path']=str(self.frozen);self.save_records(records)
        self.assertTrue(axes.frozen_identity_errors(self.freeze,self.run))

    def test_feedback_chain_and_payload_tamper(self):
        records=read(self.run/'lifecycle/dev_lifecycle.json');records[1]['feedback_digest_ack']='0'*64;self.save_records(records)
        self.assertTrue(axes.frozen_identity_errors(self.freeze,self.run))
        records[1]['feedback_digest_ack']=records[0]['feedback_digest'];self.save_records(records)
        Path(records[0]['feedback_path']).write_text('{}')
        self.assertTrue(axes.frozen_identity_errors(self.freeze,self.run))

    def test_resealed_history_cannot_relabel_last_accepted_source(self):
        # Reseal only a disposable adversarial fixture to exercise semantic
        # consistency beyond byte-seal detection; historical runs are untouched.
        self.intentionally_changed=True
        records=read(self.run/'lifecycle/dev_lifecycle.json')
        feedback=Path(records[-1]['feedback_path']);payload=read(feedback)
        payload['source_candidate_digest']='a'*64
        feedback.write_text(json.dumps(payload))
        records[-1]['candidate_digest']='a'*64;records[-1]['feedback_digest']=sha(feedback)
        self.save_records(records)
        value={**self.freeze,'accepted_candidate_digests':[r['candidate_digest'] for r in records],
               'accepted_history_sha256':sha(self.run/'lifecycle/dev_lifecycle.json'),
               'feedback_digest':records[-1]['feedback_digest']}
        self.document.chmod(0o644);self.document.write_text(json.dumps(value));self.document.chmod(0o444)
        self.seal.chmod(0o644);self.seal.write_text(sha(self.document)+'\n');self.seal.chmod(0o444)
        self.assertIn('last accepted submission',axes.frozen_identity_errors(value,self.run)[0])

    def test_seal_and_timestamp_manifest_tamper(self):
        self.intentionally_changed=True
        self.seal.chmod(0o644);self.seal.write_text('0'*64);self.seal.chmod(0o444)
        self.assertTrue(axes.frozen_identity_errors(self.freeze,self.run))
        self.seal.chmod(0o644);self.seal.write_bytes(self.original_seal);self.seal.chmod(0o444)
        self.document.chmod(0o644);value={**self.freeze,'frozen_at':'2020-01-01T00:00:00+00:00'};self.document.write_text(json.dumps(value));self.document.chmod(0o444)
        self.assertTrue(axes.frozen_identity_errors(value,self.run))

    def test_escape_and_cyclic_symlinks_are_na(self):
        self.frozen.chmod(0o755);link=self.frozen/'bad';link.symlink_to('/etc/passwd');self.frozen.chmod(0o555)
        self.assertTrue(axes.frozen_identity_errors(self.freeze,self.run))
        self.frozen.chmod(0o755);link.unlink();link.symlink_to('bad');self.frozen.chmod(0o555)
        self.assertTrue(axes.frozen_identity_errors(self.freeze,self.run))

    def test_frozen_file_add_delete_and_mode_tamper(self):
        path=self.frozen/'product.js';path.chmod(0o644)
        self.assertTrue(axes.frozen_identity_errors(self.freeze,self.run));path.chmod(0o444)
        self.frozen.chmod(0o755);extra=self.frozen/'extra';extra.write_text('x');extra.chmod(0o444);self.frozen.chmod(0o555)
        self.assertTrue(axes.frozen_identity_errors(self.freeze,self.run))
        self.frozen.chmod(0o755);extra.unlink();path.unlink();self.frozen.chmod(0o555)
        self.assertTrue(axes.frozen_identity_errors(self.freeze,self.run))

    def test_duplicate_freeze_preserves_first_bytes(self):
        with self.assertRaises(RuntimeError):self.life._freeze(self.accepted)

    def test_hidden_producer_preserves_timing_through_real_normalizer(self):
        hidden=self.hidden()
        self.assertEqual(hidden['expected_cases'],['test_001']);self.assertEqual(hidden['executed_cases'],['test_001'])
        self.assertFalse(hidden['all_cases_real_and_behavior_or_candidate_failure'])
        self.assertEqual(hidden['cases']['test_001']['classification'],'infrastructure_invalid')
        self.assertEqual(axes.hidden_lifecycle_errors(hidden,self.freeze,self.run,('test_001',)),[])
        self.assertFalse(hidden['formal_result_claimed']);self.assertFalse(hidden['code_score_claimed'])

    def test_duplicate_suite_and_partial_intent_block_before_lower(self):
        hidden=self.hidden();before=sha(self.run/'hidden_after_freeze/execution_intent.json')
        with self.assertRaises(FileExistsError):self.hidden(command_side_effect=AssertionError('lower must not repeat'))
        self.assertEqual(sha(self.run/'hidden_after_freeze/execution_intent.json'),before)

    def test_unknown_lower_exception_keeps_gate(self):
        with self.assertRaises(TimeoutError):self.hidden(command_side_effect=TimeoutError('unknown lower outcome'))
        self.assertTrue((self.run/'hidden_after_freeze/execution_intent.json').is_file())
        self.assertTrue((self.run/'hidden_after_freeze/test_001/execution_started.json').is_file())
        with self.assertRaises(FileExistsError):self.hidden()

    def test_existing_empty_suite_gate_is_not_reset(self):
        (self.run/'hidden_after_freeze').mkdir()
        with self.assertRaises(FileExistsError):self.hidden()

    def test_unknown_initial_stats_do_not_count_as_zero(self):
        with patch.object(formal,'broker_stats',return_value={'runtime':{'calls':None,'successful_calls':0,'failures':0,'provider_failures':0}}),patch.object(formal,'command_result',side_effect=AssertionError('lower forbidden')):
            with self.assertRaisesRegex(RuntimeError,'fresh'):
                formal.execute_hidden(self.life,'unused',self.run/'unused',ROOT/'input/repository',case_ids=('test_001',),pilot_not_formal=True)

    def test_hidden_subset_requires_nonformal_and_no_duplicate_ids(self):
        for ids,flag in [(('test_001',),False),(('test_001','test_001'),True)]:
            with self.subTest(ids=ids),self.assertRaises(ValueError):
                formal.execute_hidden(self.life,'unused',self.run/'unused',ROOT/'input/repository',case_ids=ids,pilot_not_formal=flag)
        self.assertFalse((self.run/'hidden_after_freeze').exists())

    def test_case_timing_and_runner_tamper_are_rejected(self):
        hidden=self.hidden();record=hidden['cases']['test_001'];p=Path(record['execution_timing_path']);value=read(p)
        value['started_at']='2020-01-01T00:00:00+00:00';p.chmod(0o644);p.write_text(json.dumps(value));p.chmod(0o444);record['execution_timing_sha256']=sha(p)
        self.assertTrue(axes.hidden_lifecycle_errors(hidden,self.freeze,self.run,('test_001',)))

    def test_missing_timing_inventory_or_time_remains_na(self):
        hidden=self.hidden();bad={**hidden,'executed_cases':['test_001','test_002']}
        self.assertTrue(axes.hidden_lifecycle_errors(bad,self.freeze,self.run,('test_001',)))
        bad={**hidden};bad.pop('hidden_finished_at')
        self.assertTrue(axes.hidden_lifecycle_errors(bad,self.freeze,self.run,('test_001',)))
        Path(hidden['cases']['test_001']['execution_timing_path']).unlink()
        self.assertTrue(axes.hidden_lifecycle_errors(hidden,self.freeze,self.run,('test_001',)))

    def test_case_clock_rollback_is_na(self):
        late='2099-01-01T00:00:00+00:00';earlier='2098-01-01T00:00:00+00:00'
        hidden=self.hidden(clock=[late,late,earlier,late])
        self.assertTrue(axes.hidden_lifecycle_errors(hidden,self.freeze,self.run,('test_001',)))

    def test_other_directory_digest_and_freeze_sha_rejected_independently(self):
        bridge=axes.code_frozen_identity(self.freeze,self.run)
        for field,value in [('candidate_path',str(self.accepted)),('freeze_sha256','0'*64),('code_candidate_digest','0'*64)]:
            with self.subTest(field=field),self.assertRaises(ValueError):
                axes._shared['checked_code_frozen_identity']({**bridge,field:value},self.freeze,self.run,self.frozen)

    def test_unknown_code_request_not_released_by_bridge(self):
        bridge=axes.code_frozen_identity(self.freeze,self.run)
        output=self.run/'formal_scoring/code_axis';output.mkdir(parents=True)
        (output/'code_logical_request_started.json').write_text('{"state":"unknown"}')
        (output/'scoring_intent.json').write_text(json.dumps({'candidate_digest':self.freeze['candidate_digest']}))
        before={p.name:p.read_bytes() for p in output.iterdir()}
        req=self.run/'public';req.mkdir();rubric=self.run/'rubric';rubric.write_text('offline fixture')
        key=self.run/'key';key.write_text('placeholder');key.chmod(0o600)
        runner=runpy.run_path('@@AGENTSWE_EDITING_CONTROL@@/code_judge_runner.py')
        argv=['runner','--candidate-source',str(self.frozen),'--public-requirements',str(req),'--code-rubric',str(rubric),'--credential-file',str(key),'--output-dir',str(output),'--expected-candidate-digest',bridge['code_candidate_digest']]
        with patch.object(sys,'argv',argv),self.assertRaisesRegex(SystemExit,'refusing overwrite/resampling'):runner['main']()
        self.assertEqual({p.name:p.read_bytes() for p in output.iterdir()},before)

if __name__=='__main__':unittest.main()
