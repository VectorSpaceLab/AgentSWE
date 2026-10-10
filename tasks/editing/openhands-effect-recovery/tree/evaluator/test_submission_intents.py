"""Durable no-replay and partial completion regressions; no external provider."""
import hashlib,json,socket,tempfile,threading,time,unittest
from pathlib import Path
from unittest.mock import patch
from adapters.source_identity import source_identity
from evaluator.test_native_builder_lifecycle import BuilderLifecycleTests
from harbor import formal_one_stop as f

class SubmissionIntentsTests(BuilderLifecycleTests):
 def test_reports_change_after_success_is_cached_before_materialization(self):
  first=self.first();(self.workspace/'run_report.json').write_text('{"tests":["new report only"]}')
  self.assertEqual(self.life.submit(None),(200,first));self.assertEqual(self.build.call_count,1);self.assertEqual(self.dev.call_count,2)
 def test_unknown_product_preserves_completed_case_and_blocks_changed_reports(self):
  original=self.dev.side_effect
  def dev(candidate,case,output,**kwargs):
   entries=self.life.controller.product_intents.entries;entry=next(iter(entries.values()))
   disk=json.loads(self.life.controller.product_intents.path.read_text())['entries'][entry['source_identity']]
   self.assertEqual(disk['state'],'unknown');self.assertEqual(disk['cases'][case]['state'],'unknown')
   if case=='dev_002':
    self.assertEqual(disk['cases']['dev_001']['state'],'completed')
    return {'classification':'provider_failure','result_evaluation':{'contract_valid':False,'round_consumed':False,'score':None}}
   return original(candidate,case,output,**kwargs)
  self.dev.side_effect=dev
  result=self.life.submit(None);self.assertEqual(result[0],503);self.assertTrue(result[1]['resampling_forbidden'])
  self.assertEqual(result[1]['dev']['dev_001']['score'],30);self.assertIsNone(result[1]['dev']['dev_002']['score'])
  self.assertEqual(self.life.submit(None),result)
  (self.workspace/'run_report.json').write_text('{"commands":["changed report"]}')
  self.assertEqual(self.life.submit(None),result);self.assertEqual(self.dev.call_count,2);self.assertEqual(self.build.call_count,1)
  before={str(p):hashlib.sha256(p.read_bytes()).hexdigest() for p in self.run.rglob('*.json')}
  with self.assertRaisesRegex(RuntimeError,'automatic restart/replay is forbidden'):
   f.BuilderLifecycle(run_dir=self.run,workspace=self.workspace,lower_endpoint='unused',node_modules=self.run/'deps',timeout=1)
  self.assertEqual(before,{p:hashlib.sha256(Path(p).read_bytes()).hexdigest() for p in before})
 def test_exception_during_second_case_does_not_lose_first_case(self):
  original=self.dev.side_effect
  def dev(candidate,case,output,**kwargs):
   if case=='dev_002':raise RuntimeError('synthetic uncertain transport')
   return original(candidate,case,output,**kwargs)
  self.dev.side_effect=dev
  result=self.life.submit(None);self.assertEqual(result[0],503)
  entry=next(iter(json.loads(self.life.controller.product_intents.path.read_text())['entries'].values()))
  self.assertEqual(entry['state'],'unknown');self.assertEqual(entry['cases']['dev_001']['state'],'completed');self.assertEqual(entry['cases']['dev_002']['state'],'unknown')
  self.assertEqual(result[1]['dev']['dev_001']['score'],30);self.assertEqual(self.life.submit(None),result);self.assertEqual(self.dev.call_count,2)
 def test_changed_real_source_after_infrastructure_uses_new_snapshot(self):
  original=self.dev.side_effect
  self.dev.side_effect=lambda *args,**kwargs:{'classification':'provider_failure','result_evaluation':{'contract_valid':False,'round_consumed':False,'score':None}}
  self.assertEqual(self.life.submit(None)[0],503)
  first=self.run/'lifecycle/candidate_001';before=f.tree_digest(first)
  self.dev.side_effect=original;(self.workspace/'solution.patch').write_text('revision 2\n')
  code,_=self.life.submit(None);self.assertEqual(code,200);self.assertEqual(f.tree_digest(first),before)
  self.assertEqual(self.life.controller.records[0]['candidate_root'],'candidate_001_attempt_002');self.assertEqual(self.dev.call_count,4)
 def test_socket_disconnect_retains_completed_result(self):
  entered=threading.Event();release=threading.Event();original=self.dev.side_effect
  def dev(*args,**kwargs):entered.set();self.assertTrue(release.wait(3));return original(*args,**kwargs)
  self.dev.side_effect=dev;f.start_builder_server(self.life)
  request={'token':self.life.token,'action':'submit'}
  with socket.socket(socket.AF_UNIX,socket.SOCK_STREAM) as s:
   s.connect(str(self.life.socket_path));s.sendall((json.dumps(request)+'\n').encode());self.assertTrue(entered.wait(3))
  release.set();deadline=time.monotonic()+5
  while not any(e['event']=='feedback_write_failed' for e in self.life.events) and time.monotonic()<deadline:time.sleep(.01)
  self.assertTrue(any(e['event']=='feedback_write_failed' for e in self.life.events))
  self.assertFalse(any(e['event']=='feedback_delivered' for e in self.life.events))
  entry=next(iter(self.life.delivery_intents.entries.values()));self.assertEqual(entry['state'],'completed')
  code,payload=self.life.submit(None);self.assertEqual(code,200);self.assertEqual(self.dev.call_count,2)
  self.assertEqual(payload,entry['result']['payload'])
 def test_source_identity_ignores_only_evaluator_runtime_trees(self):
  with tempfile.TemporaryDirectory() as d:
   root=Path(d);(root/'source.ts').write_text('real source');before=source_identity(root)
   for directory in ('.git','node_modules','.react-router'):
    (root/directory).mkdir();(root/directory/'cache').write_text('different runtime metadata')
   self.assertEqual(before,source_identity(root));(root/'source.ts').write_text('real revision');self.assertNotEqual(before,source_identity(root))
 def test_revision_before_actual_feedback_write_is_rejected(self):
  first=self.first();(self.workspace/'solution.patch').write_text('revision 2\n')
  self.assertEqual(self.life.submit(first['feedback_digest'])[0],409);self.assertEqual(self.build.call_count,1)
  self.life.feedback_written(first);self.assertEqual(self.life.submit(first['feedback_digest'])[0],200);self.assertEqual(self.build.call_count,2)

if __name__=='__main__':unittest.main()
