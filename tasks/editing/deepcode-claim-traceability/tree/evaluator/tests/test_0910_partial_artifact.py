"""Regression: real lower output with schema omissions still reaches semantic Result."""
import json,sys,tempfile,unittest
from pathlib import Path
ROOT=Path(__file__).resolve().parents[2]
sys.path.insert(0,str(ROOT/'evaluator/harness'))
from execution_evidence import attest,validate_artifact
from semantic_oracle import observe
class PartialArtifactTests(unittest.TestCase):
 def test_substantive_legacy_shape_is_scoreable_without_claiming_success(self):
  with tempfile.TemporaryDirectory() as t:
   root=Path(t);workspace=root/'work';output=root/'lower';workspace.mkdir();output.mkdir()
   (workspace/'agent_result.json').write_text(json.dumps({'schema_version':'1.0','scientific':{'weights':[[1.0]]},'publication':{'status':'committed'}}))
   (output/'stdout.jsonl').write_text(json.dumps({'msg':{'type':'tool_completed','name':'bash','output':'product result'}})+'\n')
   record={'classification':'candidate_behavior_failure','infra_valid':True,'execution_attempted':True,'environment_preflight':{'valid':True},'broker_delta':{'calls':2,'successful_calls':2}}
   result=attest(record,output=output,workspace=workspace,case_id='test_001',candidate_digest='frozen')
   self.assertTrue(result['artifact_validation']['valid']);self.assertTrue(result['artifact_validation']['quality_schema_findings'])
   self.assertEqual(result['classification'],'candidate_partial');self.assertNotIn('failure_attribution',result)
   (workspace/'agent_result.json').write_text('{}');self.assertTrue(validate_artifact(workspace/'agent_result.json',output/'stdout.jsonl','test_001'))
 def test_actual_native_scientific_output_is_observed_without_repair(self):
  with tempfile.TemporaryDirectory() as t:
   root=Path(t);project=root/'project';(project/'data').mkdir(parents=True);workspace=root/'work';native=workspace/'.deepcode/traceability_execution/artifacts';native.mkdir(parents=True)
   (project/'data/times.json').write_text('[[0,1000]]');(project/'config.json').write_text('{"tau_seconds":[1,2]}')
   (native/'result.json').write_text('{"seed":3,"weights":[[1.0,0.0]]}')
   before=(native/'result.json').read_bytes();value=observe('test_001',project=project,workspace=workspace,home=root/'home')
   self.assertEqual(value['scientific_output_selection'],'single_native_output')
   self.assertTrue(value['scientific_assertion_comparisons']['artifact.weights[0][0]'])
   self.assertFalse(value['scientific_assertion_comparisons']['artifact.weights[0][1]'])
   self.assertEqual((native/'result.json').read_bytes(),before)
if __name__=='__main__':unittest.main()
