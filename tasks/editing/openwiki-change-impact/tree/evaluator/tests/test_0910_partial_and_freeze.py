import json,sys,tempfile,unittest
from pathlib import Path
ROOT=Path(__file__).resolve().parents[2];sys.path.insert(0,str(ROOT));sys.path.insert(0,'@@AGENTSWE_EDITING_CONTROL@@')
from agentloop.protocol import materialize_external_symlinks
from agentloop.evaluator.execution_evidence import attest
from execution_contract import classify_candidate_execution
class BoundaryTests(unittest.TestCase):
 def test_freeze_rejects_external_file_and_directory_without_dereference(self):
  with tempfile.TemporaryDirectory() as temp:
   root=Path(temp);repo=root/'repository';repo.mkdir();private=root/'private';private.mkdir();secret=private/'canary';secret.write_text('synthetic-not-a-secret')
   for target in (secret,private):
    link=repo/'candidate_link';link.symlink_to(target,target_is_directory=target.is_dir())
    with self.assertRaisesRegex(ValueError,'escapes repository'):materialize_external_symlinks(repo)
    self.assertTrue(link.is_symlink());self.assertEqual(secret.read_text(),'synthetic-not-a-secret');link.unlink()
   (repo/'local').write_text('source');(repo/'internal').symlink_to('local');materialize_external_symlinks(repo);self.assertTrue((repo/'internal').is_symlink())
 def test_partial_native_output_is_semantically_scoreable_but_empty_is_fatal(self):
  with tempfile.TemporaryDirectory() as temp:
   out=Path(temp);(out/'workspace').mkdir();(out/'transport_preflight.json').write_text('{"valid":true}');run={'product_started':True,'broker_calls':1,'broker_successful_calls':1,'broker_delta':{'calls':1,'successful_calls':1},'exit_code':0,'semantic_observer_in_case_budget':True};(out/'launcher_result.json').write_text(json.dumps(run))
   for artifact,expected in (({'case_id':'wrong-claim','observations':[{'status':'incomplete'}]},'scoreable'),({},'candidate_zero')):
    text=json.dumps(artifact);(out/'workspace/agent_result.json').write_text(text);(out/'stdout.log').write_text(text)
    record=attest(run,case_id='test_001',candidate_digest='candidate',output=out);self.assertEqual(classify_candidate_execution(record,case_id='test_001',candidate_digest='candidate')['classification'],expected)
   artifact={'observations':[{'status':'incomplete'}]};(out/'workspace/agent_result.json').write_text(json.dumps(artifact));(out/'stdout.log').write_text('missing native authorship')
   record=attest(run,case_id='test_001',candidate_digest='candidate',output=out);self.assertEqual(record['classification'],'infrastructure_invalid')
if __name__=='__main__':unittest.main()
