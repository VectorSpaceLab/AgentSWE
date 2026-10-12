"""Stable product identity and report-only no replay, without provider calls."""
import hashlib,json,tempfile,unittest
from pathlib import Path
from concurrent.futures import ThreadPoolExecutor
from agentloop.stable_product import product_source_digest
from agentloop.product_registry import ProductRegistry
from agentloop.stage_recovery import RecoveryError
from agentloop.protocol import write_json
import test_v32_dev_execution as fixtures

class StableProductTests(unittest.TestCase):
    def test_only_root_git_is_excluded(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp);(root/'a.py').write_text('x=1\n');initial=product_source_digest(root)
            (root/'.git').mkdir();(root/'.git/config').write_text('evaluator random state')
            self.assertEqual(product_source_digest(root),initial)
            (root/'nested').mkdir();(root/'nested/.git').mkdir();(root/'nested/.git/config').write_text('product data')
            nested=product_source_digest(root);self.assertNotEqual(nested,initial)
            (root/'nested/.git/config').write_text('changed product data');self.assertNotEqual(product_source_digest(root),nested)
            (root/'__pycache__').mkdir();self.assertNotEqual(product_source_digest(root),nested)

    def test_reservation_crash_and_corruption_block(self):
        with tempfile.TemporaryDirectory() as tmp:
            registry=ProductRegistry(Path(tmp));d='a'*64;registry.directory(d).mkdir()
            self.assertIsNotNone(registry.lookup(d))
            with self.assertRaises(RecoveryError):registry.claim(d,{'product_source_digest':d})
            (registry.directory(d)/'owner.json').write_text('{partial')
            self.assertEqual(registry.lookup(d)['state'],'unknown_reservation')
            with self.assertRaises(RecoveryError):registry.claim(d,{})

    def test_concurrent_reservation_has_single_owner(self):
        with tempfile.TemporaryDirectory() as tmp:
            registry=ProductRegistry(Path(tmp));d='a'*64
            def run(n):
                try:registry.claim(d,{'product_source_digest':d,'ordinal':n});return 1
                except RecoveryError:return 0
            with ThreadPoolExecutor(max_workers=8) as pool:self.assertEqual(sum(pool.map(run,range(20))),1)

class ReportReplayTests(unittest.TestCase):
    setUp=fixtures.DevExecutionTests.setUp
    patch=fixtures.DevExecutionTests.patch
    reload=fixtures.DevExecutionTests.reload
    write_delivery=fixtures.DevExecutionTests.write_delivery
    fake_build=staticmethod(fixtures.DevExecutionTests.fake_build)
    fake_lower=fixtures.DevExecutionTests.fake_lower
    fake_score=staticmethod(fixtures.DevExecutionTests.fake_score)
    submit=fixtures.DevExecutionTests.submit

    def test_report_change_preserves_partial_completed_case_and_unknown(self):
        def score(**kw):
            if kw['case_id']=='dev_001':raise RuntimeError('unknown preexisting provider result')
            return self.fake_score(**kw)
        self.scorer.side_effect=score
        first=self.submit();self.assertFalse(first['accepted']);self.assertEqual(self.lower.call_count,2)
        files={p:hashlib.sha256(p.read_bytes()).hexdigest() for p in Path(first['attempt_paths']['root']).rglob('*') if p.is_file()}
        write_json(self.workspace/'edit_report.json',{'new_report_only':True})
        second=self.submit();self.assertFalse(second['accepted']);self.assertFalse(second['retry_allowed'])
        self.assertEqual(second['error'],'product_replay_forbidden')
        self.assertEqual(second['dev'],first['dev']);self.assertEqual(self.lower.call_count,2);self.assertEqual(self.scorer.call_count,2)
        self.assertEqual({p:hashlib.sha256(p.read_bytes()).hexdigest() for p in files},files)
        self.controller=self.reload();third=self.submit();self.assertFalse(third['accepted']);self.assertEqual(self.lower.call_count,2)
        self.assertEqual(third['dev'],first['dev'])

    def test_completed_product_report_change_is_not_second_revision(self):
        first=self.submit();self.assertTrue(first['accepted'])
        before=Path(first['feedback_path']).read_bytes()
        write_json(self.workspace/'run_report.json',{'new_report_only':True})
        second=self.submit();self.assertFalse(second['accepted']);self.assertEqual(len(self.controller.records),1)
        self.assertEqual(self.lower.call_count,2);self.assertEqual(self.scorer.call_count,2)
        self.assertEqual(second['feedback'],first['feedback']);self.assertEqual(Path(first['feedback_path']).read_bytes(),before)

    def test_legacy_missing_stable_digest_is_read_only_blocker(self):
        first=self.submit();self.assertTrue(first['accepted'])
        state_path=self.controller.run_dir/'dev_lifecycle.json';state=json.loads(state_path.read_text())
        state['records'][0]['build'].pop('product_source_digest');write_json(state_path,state)
        import shutil
        shutil.rmtree(self.controller.product_registry.root);self.controller=self.reload()
        write_json(self.workspace/'run_report.json',{'new_report_only':True})
        second=self.submit();self.assertFalse(second['accepted']);self.assertEqual(second['error'],'product_replay_forbidden')
        self.assertEqual(self.lower.call_count,2);self.assertNotIn('product_source_digest',self.controller.records[0]['build'])

    def test_missing_precompile_identity_cannot_dispatch_lower(self):
        def build(*args):
            row=self.fake_build(*args);row.pop('product_source_digest');return row
        self.build.side_effect=build
        result=self.submit();self.assertFalse(result['accepted']);self.assertEqual(self.lower.call_count,0)
        self.assertIn('stable pre-compile',result['details'])

if __name__=='__main__':unittest.main()
