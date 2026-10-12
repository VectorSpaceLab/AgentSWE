"""Actual systemd/cgroup tests, no model or network calls."""
import json,os,sys,tempfile,unittest
from pathlib import Path
ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT/'lower_agent'))
import owned_resources as owned
ENV={'PATH':'/usr/bin:/bin','LANG':'C.UTF-8'}
class AggregateResources(unittest.TestCase):
 def setUp(self):
  self.output=Path(tempfile.mkdtemp(prefix='aggregate-',dir=os.environ['AGENTSWE_DIAGNOSTIC_OUTPUT']))
 def nested(self,inner_code,*,outer_code='',timeout=25,memory=128*1024*1024):
  script='import sys,json\nfrom pathlib import Path\nsys.path.insert(0,sys.argv[1])\nfrom owned_resources import run_owned\n'+outer_code+'\np,r=run_owned(["/usr/bin/python3","-c",sys.argv[3]],cwd="/",env={"PATH":"/usr/bin:/bin"},output=Path(sys.argv[2]),timeout=20,memory_bytes=128*1024*1024)\nprint(json.dumps({"resource":r,"stdout":p.stdout,"returncode":p.returncode}))'
  return owned.run_owned(['/usr/bin/python3','-c',script,str(ROOT/'lower_agent'),str(self.output/'inner'),inner_code],cwd='/',env=ENV,output=self.output/'outer',timeout=timeout,memory_bytes=memory,purpose='suite')
 def test_nested_scopes_share_a_single_actual_memory_parent(self):
  p,r=self.nested('print("native child reached")');self.assertEqual(p.returncode,0,p.stderr)
  inner=json.loads(p.stdout)['resource'];self.assertNotEqual(r['cgroup'],inner['cgroup'])
  self.assertEqual(r['aggregate_parent']['cgroup'],inner['aggregate_parent']['cgroup'])
  self.assertTrue(r['aggregate_parent']['created_here']);self.assertFalse(inner['aggregate_parent']['created_here'])
  self.assertTrue(r['aggregate_cleanup']['complete']);self.assertFalse((Path('/sys/fs/cgroup')/r['aggregate_parent']['cgroup'].lstrip('/')).exists())
 def test_combined_allocation_triggers_parent_oom_before_individual_limits(self):
  p,r=self.nested('import time; x=bytearray(75*1024*1024); time.sleep(1)',outer_code='retained=bytearray(60*1024*1024)')
  events=r['aggregate_final_observed']['controller_files']['memory.events']
  counters=dict(line.split() for line in events.splitlines())
  self.assertGreater(int(counters['oom_kill']),0)
  self.assertEqual(r['aggregate_observed']['controller_files']['memory.max'],str(128*1024*1024))
  self.assertTrue(r['aggregate_cleanup']['complete'])
 def test_outer_timeout_removes_inner_sibling_scope(self):
  p,r=self.nested('import time; time.sleep(30)',timeout=2)
  self.assertEqual(p.returncode,124);self.assertTrue(r['timed_out']);self.assertTrue(r['aggregate_cleanup']['complete'])
  path=self.output/'inner/scope-ownership.json'
  if path.exists():
   inner=json.loads(path.read_text());actual=owned._show(inner['unit']);self.assertIn(actual.get('ActiveState'),(None,'inactive','failed'))
 def test_rejects_foreign_parent_identity(self):
  with self.assertRaises(RuntimeError):owned.verify_aggregate({'unit':'system.slice','cgroup':'/system.slice','memory_bytes':128*1024*1024})
if __name__=='__main__':unittest.main(verbosity=2)
