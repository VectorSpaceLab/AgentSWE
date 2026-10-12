import importlib.util
import json
import os
import tempfile
import unittest
from pathlib import Path
import readiness_judge_validation as schema
import result_judge

_LOCAL_CODE = Path(__file__).resolve().parents[1]/'authoritative_code_eval.py'
CODE=Path(os.environ.get('AGENTSWE_TEST_CODE_SCHEMA',str(_LOCAL_CODE if _LOCAL_CODE.is_file() else
    Path('@@AGENTSWE_LEGACY_HARBOR@@/0825-10create-v4/code_eval.py'))))
class JudgeSchemaTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        self.root=Path(self.tmp.name).resolve();(self.root/'evaluator').mkdir()
        (self.root/'evaluator/result_rubric.md').write_text('fixture rubric')
        self.validators=schema.make_judge_output_validators(self.root,code_implementation=CODE)
        self.inputs={'validator_source_sha256':schema.sha(Path(result_judge.__file__)),
            'rubric_relative_path':'evaluator/result_rubric.md','dimension_maxima':None}
    def result_zero(self):
        return {'case_id':'test_001','result_state':'fatal_candidate_failure','result_score':0,
            **{k:{'score':0,'max':v,'evidence':'Observed failed behavior.'} for k,v in result_judge.COMPONENT_MAXIMA.items()},
            'major_errors':['feature incomplete'],'assessment':'Readiness zero-score fixture.'}
    def test_valid_zero_result_not_blocked_by_score(self):
        self.assertEqual(self.validators['result'](self.result_zero(),self.inputs),[])
    def test_claimed_schema_valid_is_not_enough(self):
        self.assertTrue(self.validators['result']({'schema_valid':True},self.inputs))
    def test_dimension_drift_and_source_drift_rejected(self):
        (self.root/'evaluator/result_dimensions.json').write_text(json.dumps({'quality':100}))
        self.assertTrue(self.validators['result'](self.result_zero(),self.inputs))
        self.assertTrue(self.validators['result'](self.result_zero(),dict(self.inputs,validator_source_sha256='0'*64)))
    def test_code_zero_and_invalid_source_citation(self):
        spec=importlib.util.spec_from_file_location('test_code_schema',CODE)
        code=importlib.util.module_from_spec(spec);spec.loader.exec_module(code)
        payload={'code_state':'scoreable','code_dimensions':{k:{'score':0,'max':v,'evidence':'app.py:1 lacks the feature.'} for k,v in code.CODE_MAXIMA.items()},
            'code_raw_score':0,'code_score':0,'code_applied_caps':[],
            'code_major_errors':[],'code_assessment':'Zero-score fixture.'}
        inputs={'validator_source_sha256':schema.sha(CODE),'source_manifest':{'files':[{'path':'app.py','line_count':1,'type':'text','included_in_evidence_pack':True}]}}
        self.assertEqual(self.validators['code'](payload,inputs),[])
        payload['code_dimensions'][next(iter(code.CODE_MAXIMA))]['evidence']='outside.py:999'
        self.assertTrue(self.validators['code'](payload,inputs))
if __name__=='__main__':unittest.main()
