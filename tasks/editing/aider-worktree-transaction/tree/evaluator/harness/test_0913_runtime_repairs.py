import hashlib,json,tempfile,unittest
from pathlib import Path
from jsonschema import Draft202012Validator
from evaluator.case_runtime import CaseRuntime
from evaluator.harness.run_lower_agent_case import restore_product_stdout
ROOT=Path(__file__).resolve().parents[2]

class RuntimeRepairs(unittest.TestCase):
    def test_all_eight_case_requests_match_published_schema(self):
        validator=Draft202012Validator(json.loads((ROOT/'input/schemas/request.schema.json').read_text()))
        for case in ['dev_001','dev_002']+[f'test_{n:03d}' for n in range(1,7)]:
            with self.subTest(case=case), tempfile.TemporaryDirectory() as d:
                root=Path(d)
                runtime=CaseRuntime({'case_id':case,'scenario':'schema control','allowed_actions':['inspect','create','status','run','recover','rollback','attest']},root/'case',root/'private.json')
                validator.validate(runtime.adapter_request('create'))
                runtime.responses['create']={'coordination':{'owner_id':'agent-owner','lease_token':'control-lease-token-123456789','fence':1}}
                for action in ['status','run','recover','rollback']:
                    with self.subTest(case=case,action=action):validator.validate(runtime.adapter_request(action))

    def receipt(self,root):
        d=root/'turn_001_transport';d.mkdir();raw='Running python3 /case-client/run_case attest\n{"nonce":"case-generated-fact","observed":true}\n'
        (d/'stdout.log').write_text(raw)
        (d/'receipt.json').write_text(json.dumps({'product_output_identity':{'case_id':'dev_001','candidate_digest':'candidate-current','turn':1},'product_stdout_sha256':hashlib.sha256(raw.encode()).hexdigest()}))
        (root/'case_state.json').write_text('PRIVATE-ORACLE-NOT-FOR-CANDIDATE')
        (d/'stderr.log').write_text('PRIVATE-EVALUATOR-STDERR')
        return raw

    def deliver(self,root,**kw):
        args={'output':root,'case_id':'dev_001','candidate_digest':'candidate-current','previous_turn':1,'next_turn':2};args.update(kw)
        return restore_product_stdout(root/'continue.md','Choose next action from observations.',**args)

    def test_complete_product_stdout_is_preserved_without_private_inputs(self):
        with tempfile.TemporaryDirectory() as d:
            root=Path(d);raw=self.receipt(root);r=self.deliver(root);prompt=(root/'continue.md').read_text()
            self.assertIn(raw,prompt);self.assertNotIn('PRIVATE-',prompt);self.assertTrue(r['complete']);self.assertFalse(r['truncated']);self.assertEqual(r['stdout_bytes'],len(raw.encode()))

    def test_duplicate_delivery_rejected(self):
        with tempfile.TemporaryDirectory() as d:
            root=Path(d);self.receipt(root);self.deliver(root)
            with self.assertRaisesRegex(ValueError,'already delivered'):self.deliver(root)

    def test_cross_case_candidate_and_turn_rejected(self):
        for override in [{'case_id':'test_001'},{'candidate_digest':'foreign'},{'next_turn':3}]:
            with tempfile.TemporaryDirectory() as d:
                root=Path(d);self.receipt(root)
                with self.assertRaises(ValueError):self.deliver(root,**override)
                self.assertFalse((root/'continue.md').exists())

    def test_mutated_stdout_rejected(self):
        with tempfile.TemporaryDirectory() as d:
            root=Path(d);self.receipt(root);(root/'turn_001_transport/stdout.log').write_text('forged output')
            with self.assertRaisesRegex(ValueError,'digest changed'):self.deliver(root)

if __name__=='__main__':unittest.main()
