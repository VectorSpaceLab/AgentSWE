"""Actual systemd/Docker resource controls with synthetic product, no provider."""
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
import uuid

ROOT=Path(__file__).resolve().parents[2]
sys.path.insert(0,str(ROOT/'agentloop'))
from owned_resources import run_owned,verify_identity,MEMORY_BYTES

IMAGE='agentswe/edit-candidate-python311:0826'

class AggregateResources(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        self.output=Path(self.temp.name)

    def test_actual_short_product_is_observed_before_release(self):
        script=self.output/'run.py'
        script.write_text('''import json,sys
from pathlib import Path
sys.path.insert(0,sys.argv[1])
from owned_resources import _ambient_aggregate
from product_resource_gate import run_gated_product
parent=_ambient_aggregate()
command=['docker','run','--rm','--name',sys.argv[4],'--network','none','--cap-drop=ALL','--memory=4g','--cgroup-parent',parent,sys.argv[3],'python3','-c',"print('native_product_completed')"]
r=run_gated_product(command,output=Path(sys.argv[2]),parent=parent,timeout=25,text=True,capture_output=True,check=False)
assert r.returncode==0 and r.stdout.strip()=='native_product_completed'
''')
        result,att=run_owned([sys.executable,'-E','-s','-B',str(script),str(ROOT/'agentloop'),str(self.output/'product'),IMAGE,'ai-resource-'+uuid.uuid4().hex],cwd=ROOT,
            env={'PATH':'/usr/bin:/bin','LANG':'C.UTF-8'},output=self.output/'scope',timeout=40)
        self.assertEqual(result.returncode,0,result.stderr)
        observed=json.loads((self.output/'product/verified_before_product.json').read_text())
        self.assertTrue(observed['valid']);self.assertTrue(observed['actual_cgroup'].startswith(att['aggregate_parent']['cgroup']+'/'))
        self.assertEqual(observed['aggregate_before']['memory.max'],str(MEMORY_BYTES))
        self.assertTrue(json.loads((self.output/'product/resource_cleanup.json').read_text())['container_absent'])
        self.assertTrue(att['aggregate_cleanup']['complete'])

    def test_scope_deadline_kills_real_child_and_cleans_parent(self):
        result,att=run_owned([sys.executable,'-E','-s','-B','-c','import time; time.sleep(10)'],cwd=ROOT,
            env={'PATH':'/usr/bin:/bin','LANG':'C.UTF-8'},output=self.output/'scope',timeout=2)
        self.assertNotEqual(result.returncode,0)
        self.assertLess(att['elapsed_seconds'],8)
        self.assertTrue(att['aggregate_cleanup']['complete'])

    def test_foreign_scope_identity_is_never_controlled(self):
        expected={'unit':'agentswe-edit-ai-scientist-'+uuid.uuid4().hex+'.scope','description':'owned','cgroup':'/system.slice/owned','memory_bytes':MEMORY_BYTES,'timeout_seconds':2}
        with self.assertRaises(RuntimeError):verify_identity({'Id':'foreign.scope'},expected)

if __name__=='__main__':unittest.main()
