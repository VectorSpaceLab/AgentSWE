"""Real local SQLite and explicit synthetic dispatch tests, zero model calls."""
from dataclasses import asdict
import copy
import hashlib
import json
import os
from pathlib import Path
import sqlite3
import subprocess
import tempfile
import time
import unittest
from unittest.mock import patch

from evaluator.durable_state import capture_durable_state, expected_from_facts, bound_durable_evidence, FILES
from evaluator.case_service import make_private_facts, write_private_case
from evaluator.dev_result import score_public_case
from evaluator.hidden_executor import launch_case
from evaluator.semantic_finalize import evidence_files


class DurableStateTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory(prefix='oc-durable-')
        self.root=Path(self.temp.name).resolve();self.state=self.root/'gateway_state'
        (self.state/'state').mkdir(parents=True)
        self.database=self.state/'state/openclaw.sqlite'
    def tearDown(self):self.temp.cleanup()
    def capture(self,**overrides):
        args=dict(state=self.state,output=self.root/'evidence',deadline=time.monotonic()+10,
                  expected={'task_id':'task-synthetic'},writers_stopped=True)
        args.update(overrides);return capture_durable_state(**args)
    def database_with(self,table='arbitrary_ledger',state='accepted_unverified',task='task-synthetic'):
        connection=sqlite3.connect(self.database)
        connection.execute('CREATE TABLE "'+table+'" (task TEXT, delivery_state TEXT, owner_epoch INTEGER, detail TEXT)')
        connection.execute('INSERT INTO "'+table+'" VALUES(?,?,?,?)',(task,state,9,json.dumps({'state':state,'taskId':task})))
        connection.commit();connection.close()
    def hashes(self):
        return {name:hashlib.sha256((self.database.parent/name).read_bytes()).hexdigest()
                for name in FILES if (self.database.parent/name).is_file()}

    def test_actual_wal_only_commit_is_observed_and_source_bytes_preserved(self):
        command=('import sqlite3,sys,os; c=sqlite3.connect(sys.argv[1]); '
            'c.execute("PRAGMA journal_mode=WAL"); c.execute("PRAGMA wal_autocheckpoint=0"); '
            'c.execute("CREATE TABLE novel_store(task TEXT,state TEXT,epoch INTEGER)"); '
            'c.execute("INSERT INTO novel_store VALUES(?,?,?)",("task-synthetic","pending",7)); '
            'c.commit(); os._exit(0)')
        subprocess.run(['/usr/bin/python3','-c',command,str(self.database)],check=True)
        self.assertGreater((self.database.parent/'openclaw.sqlite-wal').stat().st_size,0)
        before=self.hashes();result=self.capture()
        self.assertTrue(result['collection_valid'],result)
        self.assertEqual(before,self.hashes())
        self.assertEqual(result['observations']['expected_value_occurrences']['task_id'],1)
        self.assertEqual(result['observations']['tables'][0]['rows'][0][2],7)
        self.assertFalse(result['observations']['semantic_success_inferred'])

    def test_normalized_alternate_schema_and_weak_state_are_not_rpc_success(self):
        self.database_with()
        result=self.capture()
        self.assertTrue(result['collection_valid'],result)
        row=result['observations']['tables'][0]['rows'][0]
        self.assertEqual(row[1]['contract_enum'],'accepted_unverified')
        self.assertNotEqual(row[1]['contract_enum'],'verified')
        self.assertEqual(row[2],9)
        self.assertEqual(result['observations']['expected_value_occurrences']['task_id'],2)

    def test_foreign_task_is_observed_but_never_matches_current_task(self):
        self.database_with(task='foreign-task')
        result=self.capture()
        self.assertTrue(result['collection_valid'])
        self.assertNotIn('task_id',result['observations']['expected_value_occurrences'])
        self.assertNotIn('foreign-task',json.dumps(result))

    def test_private_body_authority_and_identifier_names_are_redacted(self):
        secret='secretAuthority123';body='private message body'
        connection=sqlite3.connect(self.database)
        connection.execute('CREATE TABLE "'+secret+'" (token TEXT, body TEXT, data BLOB)')
        connection.execute('INSERT INTO "'+secret+'" VALUES(?,?,?)',(secret,body,body.encode()))
        connection.commit();connection.close()
        result=self.capture(private_values=[secret],expected={'requested_result_body':body.encode()})
        self.assertTrue(result['collection_valid'],result)
        text=json.dumps(result)
        self.assertNotIn(secret,text);self.assertNotIn(body,text)
        self.assertIn('requested_result_body',text)
        self.assertFalse(result['candidate_code_executed'])

    def test_views_and_generated_expressions_are_not_executed(self):
        connection=sqlite3.connect(self.database)
        connection.execute('CREATE TABLE facts(task TEXT, derived TEXT GENERATED ALWAYS AS (upper(task)) VIRTUAL)')
        connection.execute('INSERT INTO facts(task) VALUES(?)',('task-synthetic',))
        connection.execute('CREATE VIEW trap AS SELECT load_extension(\'/unrelated/not-allowed\')')
        connection.commit();connection.close()
        result=self.capture()
        self.assertTrue(result['collection_valid'],result)
        self.assertFalse(result['observations']['all_schema_objects_observed'])
        self.assertEqual(result['observations']['tables'][0]['nonstored_columns_not_evaluated'],['derived'])
        self.assertNotIn('/unrelated/not-allowed',json.dumps(result))

    def test_absent_database_is_a_fact_not_infrastructure_or_automatic_zero(self):
        result=self.capture()
        self.assertTrue(result['collection_valid'])
        self.assertFalse(result['database_present'])
        self.assertNotIn('score',result)
        second=self.root/'no-child';second.mkdir()
        self.assertTrue(self.capture(state=second,output=self.root/'second')['collection_valid'])

    def test_symlink_hardlink_and_fifo_are_rejected_without_following(self):
        for kind in ('symlink','hardlink','fifo'):
            with self.subTest(kind=kind):
                target=self.root/(kind+'-external');target.write_bytes(b'untouched')
                if kind=='symlink':self.database.symlink_to(target)
                elif kind=='hardlink':os.link(target,self.database)
                else:os.mkfifo(self.database)
                result=self.capture(output=self.root/(kind+'-output'))
                self.assertFalse(result['collection_valid'])
                self.assertEqual(target.read_bytes(),b'untouched')
                self.database.unlink()

    def test_no_snapshot_without_verified_scope_cleanup_or_remaining_budget(self):
        self.database_with()
        for fields in ({'writers_stopped':False},{'deadline':time.monotonic()-1}):
            result=self.capture(**fields)
            self.assertFalse(result['collection_valid'])
            self.assertFalse((self.root/'evidence').exists())

    def test_existing_evidence_is_not_overwritten(self):
        self.database_with();first=self.capture();before=self.hashes()
        result=self.capture()
        self.assertTrue(first['collection_valid'])
        self.assertFalse(result['collection_valid'])
        self.assertEqual(before,self.hashes())

    def test_projection_capacity_is_explicit_not_silent_truncation_or_zero(self):
        self.database_with()
        with patch('evaluator.durable_state.MAX_ROWS',0):result=self.capture()
        self.assertFalse(result['collection_valid'])
        self.assertIn('reader_capacity',result['error'])
        self.assertNotIn('result_score',result)

    def test_expected_identities_come_from_case_facts_and_wrong_bundle_rejected(self):
        facts=asdict(make_private_facts('test_005'))
        expected,private=expected_from_facts(facts,'test_005')
        self.assertEqual(len(expected['requested_attachment_bytes']),32771)
        self.assertIn(facts['task_nonce'],private)
        with self.assertRaises(ValueError):expected_from_facts(facts,'test_001')

    def test_launch_collects_after_owned_execution_and_passes_independent_rows(self):
        candidate=self.root/'candidate';candidate.mkdir();(candidate/'file').write_text('source')
        hidden=self.root/'test_001';hidden.mkdir();(hidden/'input.md').write_text('Synthetic test task')
        private,view=write_private_case(self.root/'private','test_001')
        output=self.root/'case';order=[]
        def owned(*args,**kwargs):
            order.append('owned_returned')
            state=output/'gateway_state/state';state.mkdir(parents=True)
            connection=sqlite3.connect(state/'openclaw.sqlite')
            connection.execute('CREATE TABLE retained_state(state TEXT)')
            connection.execute("INSERT INTO retained_state VALUES('pending')")
            connection.commit();connection.close()
            (output/'run_report.json').write_text(json.dumps({'classification':'candidate_behavior_observed','artifact_contract':{'valid':False}}))
            return subprocess.CompletedProcess([],0,'',''),{'valid':True,'cleanup':{'complete':True},'timed_out':False}
        original=capture_durable_state
        def observe(**kwargs):
            self.assertEqual(order,['owned_returned']);order.append('capture')
            return original(**kwargs)
        with patch('evaluator.hidden_executor.run_owned',side_effect=owned), \
             patch('evaluator.hidden_executor.capture_durable_state',side_effect=observe), \
             patch('evaluator.hidden_executor.safe_stats',return_value=({'calls':0,'successful_calls':0,'failures':0},None)):
            result=launch_case(case_id='test_001',hidden_case=hidden,frozen_candidate=candidate,output=output,
                broker_endpoint='unused',runtime=None,private_file=private,view=view,timeout_seconds=600)
        self.assertEqual(order,['owned_returned','capture'])
        self.assertTrue(result['durable_state']['collection_valid'],result['durable_state'])
        self.assertEqual(result['durable_state']['observations']['tables'][0]['rows'][0][0]['contract_enum'],'pending')

    def test_public_and_hidden_judges_refuse_missing_durable_evidence(self):
        with patch('evaluator.dev_result.subprocess.run') as provider:
            record=score_public_case(root=self.root,candidate_digest='synthetic',
                record={'case_id':'dev_001','frozen_candidate_digest_stable':True},case_output=self.root/'case',broker_endpoint='unused')
        provider.assert_not_called()
        self.assertIn('durable',record['semantic_result']['reason'])
        with self.assertRaisesRegex(ValueError,'durable'):
            evidence_files(self.root,self.root,'openclaw','test_001',self.root/'record.json',{},self.root/'artifact.json')

    def test_foreign_bundle_scope_and_case_binding_are_rejected(self):
        value={'case_resources':{'unit':'synthetic-owned-case'},'native_case':{'case_bundle_sha256':'a'*64},
            'durable_state':{'collection_valid':True,'writers_stopped':True,'source_bytes_unchanged':True,
                'case_binding':{'case_id':'test_001','case_bundle_sha256':'a'*64,'owned_scope_unit':'synthetic-owned-case'}}}
        self.assertTrue(bound_durable_evidence(value,'test_001'))
        for field,replacement in (('case_id','test_002'),('case_bundle_sha256','b'*64),('owned_scope_unit','foreign-scope')):
            changed=copy.deepcopy(value);changed['durable_state']['case_binding'][field]=replacement
            self.assertFalse(bound_durable_evidence(changed,'test_001'))

    def test_hidden_judge_inputs_include_independent_rows_not_just_a_marker(self):
        self.database_with();durable=self.capture()
        durable['case_binding']={'case_id':'test_001','case_bundle_sha256':'a'*64,'owned_scope_unit':'synthetic-owned-case'}
        record={'case_id':'test_001','durable_state':durable,'case_resources':{'unit':'synthetic-owned-case'},
            'native_case':{'case_bundle_sha256':'a'*64},'classification':'candidate_behavior_observed'}
        artifact=self.root/'artifact.json';artifact.write_text(json.dumps({'case_id':'test_001','decision':'verified'}))
        _,native,oracle=evidence_files(self.root,self.root,'openclaw','test_001',self.root/'record.json',record,artifact)
        submitted=json.loads(native.read_text())
        self.assertEqual(submitted['facts']['durable_state']['observations']['tables'][0]['rows'][0][1]['contract_enum'],'accepted_unverified')
        self.assertTrue(json.loads(oracle.read_text())['durable_state']['collection_valid'])

    def test_actual_standalone_finalizer_import_from_unrelated_directory(self):
        entry=Path(__file__).resolve().parents[1]/'evaluator/formal_finalize.py'
        process=subprocess.run(['/usr/bin/python3','-E','-s','-B',str(entry),'--help'],
            cwd=self.root,env={'PATH':'/usr/bin:/bin','LANG':'C.UTF-8'},capture_output=True,text=True,timeout=10)
        self.assertEqual(process.returncode,0,process.stderr)
        self.assertIn('--run-dir',process.stdout)


if __name__=='__main__':unittest.main()
