"""Real Git patch/materialization controls; external models are never called."""
import importlib.util,json,subprocess,tempfile,unittest
from pathlib import Path
from unittest.mock import patch
ROOT=Path(__file__).resolve().parents[2]
spec=importlib.util.spec_from_file_location('aider_product_attempts_v55',ROOT/'harbor/agentloop_controller.py')
m=importlib.util.module_from_spec(spec);spec.loader.exec_module(m)

class ProductAttemptTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        self.root=Path(self.tmp.name);self.source=self.root/'source'
        (self.source/'aider').mkdir(parents=True);(self.source/'tests').mkdir()
        (self.source/'aider/worktree_plan_adapter.py').write_text('VALUE = 1\n')
        (self.source/'tests/test_product.py').write_text('VALUE = 1\n')
        self.delivery=self.root/'submission';self.delivery.mkdir();self.write_delivery(2)
        self.calls=[];self.scores=[];self.actual_run=subprocess.run
    def write_delivery(self,value,reports=0,invalid=False):
        line='VALUE = (' if invalid else f'VALUE = {value}'
        body=''
        paths=['aider/worktree_plan_adapter.py','tests/test_product.py']
        for p in paths:
            body+=f'diff --git a/{p} b/{p}\n--- a/{p}\n+++ b/{p}\n@@ -1 +1 @@\n-VALUE = 1\n+{line}\n'
        (self.delivery/'solution.patch').write_text(body)
        (self.delivery/'edit_report.json').write_text(json.dumps({'schema_version':1,'changed_paths':paths,'summary':f'product change {reports}','tests':[]}))
        (self.delivery/'run_report.json').write_text(json.dumps({'schema_version':1,'status':'complete','commands':[],'duration_seconds':reports,'errors':[],**dict.fromkeys(['deepseek','gateway','gateway_image','serper','web_retrieval'],0)}))
    def controller(self):
        return m.Controller(self.root/'run',self.source,'http://127.0.0.1:1/v1',True)
    def dispatch(self,command,**kw):
        if len(command)>1 and 'run_lower_agent_case.py' in str(command[1]):
            case=command[command.index('--case-id')+1];out=Path(command[command.index('--output-dir')+1])
            self.assertFalse(out.exists() and any(out.iterdir()))
            out.mkdir(parents=True,exist_ok=True);self.calls.append(case)
            (out/'result.json').write_text(json.dumps({'case_id':case,'classification':'candidate_valid','candidate_digest':command[command.index('--candidate-digest')+1],'answer':{'decision':'actual product control'},'broker':{'calls_delta':1,'failures_delta':0}}))
            return subprocess.CompletedProcess(command,0,'{}','')
        return self.actual_run(command,**kw)
    def score(self,case,path,digest):
        self.scores.append(case)
        if case=='dev_002' and self.fail_second:raise RuntimeError('provider result unknown')
        value=json.loads(path.read_text());value.update(score=73,maximum=100,dev_score_kind='controlled_result_rubric');path.write_text(json.dumps(value))
    def test_completed_unknown_report_retry_restart_and_new_product(self):
        c=self.controller();self.fail_second=True
        with patch.object(m.subprocess,'run',side_effect=self.dispatch),patch.object(m.Controller,'_score_public',side_effect=self.score):
            first=c.submit(self.delivery);self.assertFalse(first['accepted']);self.assertEqual(self.calls,['dev_001','dev_002'])
            prior={p:p.read_bytes() for p in Path(first['attempt_path']).rglob('*') if p.is_file()}
            again=c.submit(self.delivery);self.assertTrue(again['duplicate'])
            self.write_delivery(2,reports=1)
            report=c.submit(self.delivery);self.assertTrue(report['same_product'])
            c=self.controller();again=c.submit(self.delivery);self.assertTrue(again['duplicate'])
            self.assertEqual(self.calls,['dev_001','dev_002']);self.assertEqual(self.scores,['dev_001','dev_002'])
            self.assertTrue(all(p.read_bytes()==v for p,v in prior.items()))
            raw=Path(first['attempt_path'])/'evaluations/dev_002/result.json'
            self.assertEqual(json.loads(raw.read_text())['classification'],'candidate_valid')
            self.assertEqual(json.loads((raw.parent/'scoring_failure.json').read_text())['score'],None)
            self.write_delivery(3);self.fail_second=False
            accepted=c.submit(self.delivery);self.assertTrue(accepted['accepted']);self.assertEqual(accepted['submission_number'],1)
            self.assertEqual(self.calls,['dev_001','dev_002']*2)
            frozen=c.freeze_latest();self.assertEqual(frozen['freeze_reason'],'builder_exit')
            self.assertEqual(len(c.records),1)
    def test_completed_reports_only_keeps_round_then_real_revision_accepts(self):
        c=self.controller();self.fail_second=False
        with patch.object(m.subprocess,'run',side_effect=self.dispatch),patch.object(m.Controller,'_score_public',side_effect=self.score):
            first=c.submit(self.delivery);self.assertTrue(first['accepted']);feedback=c.feedback_digest
            self.assertIsNone(c.frozen)
            self.write_delivery(2,reports=4)
            duplicate=c.submit(self.delivery,feedback);self.assertTrue(duplicate['same_product'])
            self.assertFalse(duplicate['round_consumed']);self.assertEqual(len(c.records),1)
            self.assertEqual(c.feedback_digest,feedback);self.assertEqual(len(self.calls),2)
            self.write_delivery(3)
            second=c.submit(self.delivery,feedback);self.assertTrue(second['accepted'])
            self.assertEqual(second['submission_number'],2);self.assertEqual(len(self.calls),4)
            self.assertEqual(c.freeze_latest()['source_submission'],2)

    def test_interruption_after_lower_reserves_unknown(self):
        c=self.controller();self.fail_second=False
        def interrupted(*args):raise KeyboardInterrupt('simulated disconnect before score completion')
        with patch.object(m.subprocess,'run',side_effect=self.dispatch),patch.object(m.Controller,'_score_public',side_effect=interrupted):
            with self.assertRaises(KeyboardInterrupt):c.submit(self.delivery)
            self.write_delivery(2,reports=2);c=self.controller();value=c.submit(self.delivery)
            self.assertEqual(value['state'],'unresolved_previous_attempt');self.assertEqual(self.calls,['dev_001'])
    def test_real_candidate_syntax_error_is_two_zero_and_first_exit(self):
        self.write_delivery(2,invalid=True);c=self.controller()
        with patch.object(m.subprocess,'run',side_effect=self.dispatch):
            value=c.submit(self.delivery)
        self.assertTrue(value['accepted']);self.assertEqual(c.feedback['dev_score'],0);self.assertEqual(self.calls,[])
        self.assertTrue(all(json.loads(Path(v['result']).read_text())['score']==0 for v in value['dev'].values()))
        self.assertEqual(c.freeze_latest()['accepted_submission_count'],1)
    def test_cached_evidence_tamper_refused(self):
        c=self.controller();self.fail_second=True
        with patch.object(m.subprocess,'run',side_effect=self.dispatch),patch.object(m.Controller,'_score_public',side_effect=self.score):
            row=c.submit(self.delivery);Path(row['dev']['dev_001']['result']).write_text('{}')
            with self.assertRaisesRegex(RuntimeError,'evidence changed'):c.submit(self.delivery)
            self.assertEqual(len(self.calls),2)
    def test_legacy_run_is_read_only(self):
        run=self.root/'run';run.mkdir();old=run/'old_unknown.json';old.write_text('{"unknown":true}')
        c=self.controller()
        with patch.object(m.subprocess,'run') as invoked:
            row=c.submit(self.delivery)
        self.assertFalse(row['accepted']);invoked.assert_not_called();self.assertEqual(old.read_text(),'{"unknown":true}')

if __name__=='__main__':unittest.main()
