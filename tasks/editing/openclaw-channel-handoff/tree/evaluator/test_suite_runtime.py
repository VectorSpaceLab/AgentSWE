"""Budget and ownership regressions; no real model or formal case execution."""
import json
from pathlib import Path
import subprocess
import tempfile
import time
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from evaluator.suite_runtime import SuiteClock, SuiteBudgetExpired, cleanup_nested, run_hidden_owned
from lower_agent.gateway_cluster import GatewayCluster
from lower_agent.owned_resources import run_owned, MEMORY_BYTES

CASES=tuple(f'test_{i:03d}' for i in range(1,7))


class SuiteRuntimeTests(unittest.TestCase):
    def clock(self, elapsed=0):
        current=[1000+elapsed]
        return SuiteClock(started=1000,deadline=7900,case_ids=CASES,clock=lambda:current[0]),current

    def test_build_reserves_six_full_case_budgets_and_final_30_seconds(self):
        clock,current=self.clock(17)
        # work_deadline 7870 minus six 930s case envelopes = 2290.
        self.assertEqual(clock.build_deadline(),2290)
        self.assertEqual(clock.build_deadline()-current[0],1273)
        self.assertEqual(clock.snapshot()['final_reserve_seconds'],30)

    def test_hashing_and_setup_time_are_not_reset(self):
        clock,current=self.clock(900)
        self.assertEqual(clock.build_deadline()-current[0],390)
        current[0]=2290
        with self.assertRaises(SuiteBudgetExpired):clock.build_deadline()

    def test_all_six_cases_receive_full_budgets_without_extending_suite(self):
        clock,current=self.clock(160)
        for case in CASES:
            started,end=clock.begin_case(case)
            self.assertEqual(end-started,930)
            self.assertLessEqual(end,7870)
            current[0]=end
        value=clock.snapshot()
        self.assertEqual(value['admitted_cases'],6)
        self.assertTrue(value['within_total_budget'])

    def test_no_shortened_case_when_build_or_overhead_uses_envelope(self):
        clock,_=self.clock(1291)
        with self.assertRaises(SuiteBudgetExpired):clock.begin_case('test_001')
        self.assertEqual(clock.next_case,0)

    def test_case_order_and_duplicates_fail_closed(self):
        clock,_=self.clock()
        with self.assertRaises(ValueError):clock.begin_case('test_002')
        clock.begin_case('test_001')
        with self.assertRaises(ValueError):clock.begin_case('test_001')

    def test_nonfinite_and_extended_envelopes_rejected(self):
        for start,end in ((0,6901),(0,float('inf')),(float('nan'),6900),(1000,999)):
            with self.subTest(start=start,end=end),self.assertRaises(ValueError):
                SuiteClock(started=start,deadline=end,case_ids=CASES)

    def test_both_gateway_starts_get_separate_120s_with_same_outer_deadline(self):
        with tempfile.TemporaryDirectory() as d:
            current=[1000.]; deadlines=[]
            cluster=GatewayCluster.__new__(GatewayCluster)
            cluster.primary=None;cluster.closed=False;cluster.world=SimpleNamespace(started=True)
            cluster.deadline=1250.;cluster.output=Path(d);cluster.node=Path('/node');cluster.product=Path('/product')
            cluster.ports={'A':1,'B':2};cluster.token='local';cluster.env={};cluster.health={};cluster.events=[]
            cluster.ids={'A':'A','B':'B'};cluster.command=lambda role:[role]
            cluster.generations={'A':0,'B':0}
            cluster.sandboxes={role:SimpleNamespace(start=lambda *a,**k:object(),namespace={'net_inode':n})
                               for n,role in enumerate(('A','B'),1)}
            cluster.sandbox=cluster.sandboxes['B']
            def health(*args,**kwargs):
                deadlines.append(kwargs['startup_deadline']);current[0]+=80
                return {'status':'ok'}
            cluster.native=SimpleNamespace(GATEWAY_STARTUP_SECONDS=120,wait_gateway_health=health)
            with patch('lower_agent.gateway_cluster.time.monotonic',side_effect=lambda:current[0]):
                cluster.start()
            self.assertEqual(deadlines,[1120,1200])

    def test_cleanup_checks_only_declared_scope_ownership_not_candidate_workspace(self):
        with tempfile.TemporaryDirectory() as d:
            root=Path(d);control=root/'control';output=root/'hidden'
            path=output/'test_001/case_resources/scope-ownership.json';path.parent.mkdir(parents=True)
            path.write_text(json.dumps({'unit':'owned-real'}))
            fake=output/'test_001/workspace/scope-ownership.json';fake.parent.mkdir(parents=True)
            fake.write_text(json.dumps({'unit':'unrelated-user-session'}))
            with patch('evaluator.suite_runtime.stop_owned',return_value={'complete':True}) as stop:
                result=cleanup_nested(control,output,CASES)
                self.assertEqual(stop.call_args.args[0]['unit'],'owned-real')
                self.assertEqual(stop.call_count,1)
            self.assertEqual(sum(r['started'] for r in result),1)

    def test_existing_suite_control_is_not_reused(self):
        with tempfile.TemporaryDirectory() as d:
            output=Path(d)/'hidden';(Path(d)/'hidden-suite-control').mkdir()
            with patch('evaluator.suite_runtime.run_owned') as owned,self.assertRaises(FileExistsError):
                run_hidden_owned(output=output,case_ids=CASES)
            owned.assert_not_called()

    def test_owned_wrapper_does_not_invent_valid_results_after_missing_worker_output(self):
        with tempfile.TemporaryDirectory() as d:
            resource={'valid':True,'cleanup':{'complete':True},'timed_out':False}
            with patch('evaluator.suite_runtime.run_owned',return_value=(subprocess.CompletedProcess([],0,'',''),resource)) as owned:
                result=run_hidden_owned(output=Path(d)/'hidden',case_ids=CASES)
            self.assertFalse(result['formal_evidence_valid'])
            self.assertEqual(result['result_axis'],'N/A')
            self.assertEqual(owned.call_args.kwargs['purpose'],'suite')
            self.assertLessEqual(owned.call_args.kwargs['timeout'],6900)
            self.assertEqual(owned.call_args.kwargs['memory_bytes'],MEMORY_BYTES)

    def test_real_suite_scope_manager_deadline_and_cleanup(self):
        """Actual Linux systemd scope, deliberately tiny diagnostic budget."""
        with tempfile.TemporaryDirectory() as d:
            start=time.monotonic()
            process,resource=run_owned(['/usr/bin/python3','-c','import time; time.sleep(30)'],
                cwd=Path(d),env={'PATH':'/usr/bin:/bin','LANG':'C.UTF-8'},output=Path(d)/'owned',
                timeout=2,memory_bytes=128*1024*1024,purpose='suite')
            self.assertTrue(resource['valid'])
            self.assertTrue(resource['cleanup']['complete'])
            self.assertLess(time.monotonic()-start,15)
            self.assertNotEqual(process.returncode,0)
            self.assertEqual(resource['systemd_properties']['RuntimeMaxUSec'],'2s')

    def test_nested_scope_is_reclaimed_after_outer_worker_timeout(self):
        """Actual orphan recovery by exact evaluator-owned scope identity."""
        with tempfile.TemporaryDirectory() as d:
            root=Path(d);control=root/'control';output=root/'hidden'
            inner=control/'runtime-product-build-evidence/owned-resources'
            source_root=Path(__file__).resolve().parents[1]
            script=('import sys; from pathlib import Path; '
                'sys.path.insert(0,sys.argv[1]); from lower_agent.owned_resources import run_owned; '
                'run_owned(["/usr/bin/python3","-c","import time; time.sleep(30)"], '
                'cwd=Path(sys.argv[2]),env={"PATH":"/usr/bin:/bin","LANG":"C.UTF-8"}, '
                'output=Path(sys.argv[3]),timeout=20,memory_bytes=134217728,purpose="build")')
            try:
                process,resource=run_owned(['/usr/bin/python3','-B','-c',script,str(source_root),str(root),str(inner)],
                    cwd=root,env={'PATH':'/usr/bin:/bin','LANG':'C.UTF-8'},output=root/'outer',
                    timeout=3,memory_bytes=128*1024*1024,purpose='suite')
                self.assertNotEqual(process.returncode,0)
                self.assertTrue(resource['cleanup']['complete'])
                self.assertTrue((inner/'scope-ownership.json').is_file())
            finally:
                cleanup=cleanup_nested(control,output,CASES)
            owned=[item for item in cleanup if item['started']]
            self.assertEqual(len(owned),1)
            self.assertTrue(owned[0]['complete'])


if __name__=='__main__':unittest.main()
