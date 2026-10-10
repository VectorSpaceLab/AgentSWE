"""No-provider checks of native consumer and no-resampling public entry."""
import json,os,tempfile,unittest
from contextlib import contextmanager
from pathlib import Path
from unittest.mock import Mock,patch
from harbor import formal_one_stop as f
from evaluator.controller.two_round_controller import TwoRoundController
class DirectBuilderTests(unittest.TestCase):
 def test_native_consumer_uses_shared_auth_proxy_and_no_broker(self):
  with tempfile.TemporaryDirectory() as raw:
   root=Path(raw);(root/'builder_task/environment').mkdir(parents=True);config=root/'builder_job_config.json';config.write_text(json.dumps({'job_name':'test-job'}))
   compose=root/'builder_task/environment/docker-compose.yaml';compose.write_text(json.dumps({'services':{'main':{'environment':{'AGENTSWE_BUILDER_BROKER_TOKEN':'placeholder'}}}}))
   trial=root/'jobs/test-job/trial';(trial/'agent').mkdir(parents=True)
   (trial/'result.json').write_text(json.dumps({'exception_info':None,'agent_setup':{'duration':1},'agent_execution':{'duration':1}}))
   for gate in ('builder_resource_gate.json','builder_dependency_gate.json'):
    (trial/'agent'/gate).write_text(json.dumps({'valid':True}))
   seen=[]
   @contextmanager
   def auth(*args):seen.append('auth');yield {'transport':'native_codex_direct'}
   @contextmanager
   def proxy(*args):seen.append('proxy');yield {'upstream':'http://127.0.0.1:7890'}
   direct=Mock(direct_auth=auth,existing_proxy_for_builder=proxy,native_stats=lambda run:{'native_completed_turns':1})
   child=Mock(pid=os.getpid(),args=['harbor']);child.wait.return_value=0;child.poll.return_value=0
   observer=Mock();observer.proof={'errors':[]};observer.finish.return_value={'valid':True};observer.cleanup_owned.return_value={'complete':True};observer.thread.is_alive.return_value=False
   with patch.object(f,'direct_builder_runtime',return_value=direct),patch.object(f.subprocess,'Popen',return_value=child),patch('harbor.builder_resources.BuilderResourceObserver',return_value=observer),patch.object(f,'start_broker',side_effect=AssertionError('Builder broker must not start')):
    result=f.run_native_builder(lifecycle=Mock(run_dir=root),config=config,credential=root/'credential',harbor=Path('/pinned/harbor'),timeout=60)
   self.assertEqual(result.returncode,0);self.assertEqual(seen,['proxy','auth']);self.assertNotIn('AGENTSWE_BUILDER_BROKER_TOKEN',compose.read_text());self.assertTrue((root/'owned_builder_process.json').is_file());self.assertTrue((root/'builder_native_stats.json').is_file())
 def test_public_infrastructure_unknown_is_never_resampled(self):
  controller=object.__new__(TwoRoundController);result={'classification':'provider_failure','broker_delta':{'calls':1,'unknown_usage_calls':1,'total_tokens':None}}
  with patch.object(controller,'_run_case',return_value=result) as run:
   actual=controller._run_public_case(Path('/candidate'),'dev_001',Path('/output'),max_retries=2)
  self.assertIs(actual,result);self.assertEqual(run.call_count,1)
class ResourceGateTests(unittest.TestCase):
 def test_actual_cgroup_contract_and_unbounded_negative(self):
  from harbor.builder_resource_check import inspect_resources
  with tempfile.TemporaryDirectory() as raw:
   base=Path(raw);(base/'cpuset.cpus.effective').write_text('0-255');(base/'memory.max').write_text(str(16*1024**3))
   for value,valid in [('800000 100000',True),('max 100000',False),('400000 100000',False)]:
    (base/'cpu.max').write_text(value);self.assertEqual(inspect_resources(base)['valid'],valid)
   (base/'cpu.max').write_text('800000 100000');(base/'memory.max').write_text(str(8*1024**3));self.assertFalse(inspect_resources(base)['valid'])
if __name__=='__main__':unittest.main()

