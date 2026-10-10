import copy,json,shutil,sys,tempfile,unittest
from pathlib import Path
ROOT=Path(__file__).resolve().parents[1];sys.path.insert(0,str(ROOT/'lower_agent'))
import openhands_lower_agent as lower
from model_origin import decision,completed_origin,value
NATIVE=Path('@@AGENTSWE_LEGACY_DATA@@/0910-edit-repair/diagnostics/openhands/protocol-native-terminal-001')
class ModelOriginTests(unittest.TestCase):
 def setUp(self):
  self.temp=tempfile.TemporaryDirectory();self.out=Path(self.temp.name)
  for name in ['model_final_response.txt','model_final_response.txt.request.json','model_final_response.txt.payload.json','trajectory.json','private_world_config.json']:
   shutil.copyfile(NATIVE/name,self.out/name)
 def tearDown(self):self.temp.cleanup()
 def test_terminal_blocked_is_not_product_action(self):
  raw={'kind':'blocked','rationale':'observed denial'};d=decision(raw,lower.ACTION_NAMES)
  self.assertEqual(d['kind'],'terminal');self.assertNotIn('action',d);self.assertEqual(d['raw_choice'],raw)
  native=json.loads((NATIVE/'trajectory.json').read_text())
  self.assertEqual(len(native['steps']),5);self.assertTrue(all(s['action'] for s in native['steps']))
 def test_completed_bad_claims_have_real_capture_origin(self):
  artifact=value(self.out/'model_final_response.txt');origin=completed_origin(lower,self.out,'test_006',artifact)
  bound=lower.validate_trajectory_binding(self.out,'test_006',artifact,trusted_origin=origin)
  self.assertTrue(bound['bound']);self.assertEqual({x['field'] for x in bound['author_factual_claim_mismatches']},{'model','reasoning_effort','trajectory_digest'})
 def test_swapped_authored_bytes_rejected(self):
  artifact=value(self.out/'model_final_response.txt');artifact['rationale']='substituted'
  with self.assertRaisesRegex(ValueError,'differs from original'):completed_origin(lower,self.out,'test_006',artifact)
 def test_changed_request_payload_rejected(self):
  p=self.out/'model_final_response.txt.payload.json';data=json.loads(p.read_text());data['input']+=' ';p.write_text(json.dumps(data))
  with self.assertRaisesRegex(ValueError,'payload changed'):completed_origin(lower,self.out,'test_006',value(self.out/'model_final_response.txt'))
 def test_unknown_capture_not_reclassified_completed(self):
  p=self.out/'model_final_response.txt.request.json';data=json.loads(p.read_text());data['state']='submitted_or_unknown';p.write_text(json.dumps(data))
  with self.assertRaisesRegex(ValueError,'matching completed'):completed_origin(lower,self.out,'test_006',value(self.out/'model_final_response.txt'))
 def test_wrong_world_and_case_rejected(self):
  with self.assertRaisesRegex(ValueError,'matching completed'):completed_origin(lower,self.out,'test_005',value(self.out/'model_final_response.txt'))
  p=self.out/'private_world_config.json';data=json.loads(p.read_text());data['nonce']='foreign';p.write_text(json.dumps(data))
  with self.assertRaisesRegex(ValueError,'captured product world'):completed_origin(lower,self.out,'test_006',value(self.out/'model_final_response.txt'))
if __name__=='__main__':unittest.main()
