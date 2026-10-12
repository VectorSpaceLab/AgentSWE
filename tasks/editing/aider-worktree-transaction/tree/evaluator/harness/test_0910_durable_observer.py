"""Real Git/ledger observations reject claims without durable effects."""
import json
import tempfile
import unittest
from pathlib import Path
from evaluator.case_runtime import CaseRuntime


class DurableObserverTests(unittest.TestCase):
    def test_top_level_identity_is_bound_to_ledger_and_case(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            runtime = CaseRuntime({'case_id':'test_001','scenario':'real transaction','allowed_actions':['create']}, root/'case',root/'private.json')
            response = {'plan_id':f'test_001-{runtime.nonce[:8]}', 'transaction_id':'transaction-a',
                        'state':'created', 'ledger':{'schema_version':3,'generation':1,'digest':'sha256:'+'a'*64}}
            runtime.record('create', response, 0)
            self.assertFalse(runtime._has_bound_ledger('create'))
            runtime.state_dir.mkdir()
            (runtime.state_dir/'ledger.json').write_text(json.dumps({'schema_version':3,'events':[]}))
            runtime.record('create', response, 0)
            self.assertTrue(runtime._has_bound_ledger('create'))
            runtime.record('create', {**response,'plan_id':'another-case'}, 0)
            self.assertFalse(runtime._has_bound_ledger('create'))

    def test_forged_committed_response_does_not_prove_publication(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            runtime = CaseRuntime({'case_id':'test_001','scenario':'real transaction','allowed_actions':['run']}, root/'case',root/'private.json')
            rows=[{'repository_id':rid,'ref':'refs/heads/main','final_oid':'a'*40} for rid in runtime.repo_paths]
            response={'state':'committed','publication':{'targets':rows}}
            runtime.record('run',response,0)
            self.assertFalse(runtime.product_observations[-1]['publication_refs_match'])
            self.assertFalse(runtime.semantic_comparison()['checks']['safe_terminal_or_refusal'])
            for row in rows:
                repo=runtime.repo_paths[row['repository_id']]
                runtime._git_at(repo,'commit','--allow-empty','-q','-m','real control publication')
                row['final_oid']=runtime._git_at(repo,'rev-parse','HEAD')
            runtime.record('run',response,0)
            self.assertTrue(runtime.product_observations[-1]['publication_refs_match'])

    def test_symlink_ledger_does_not_become_trusted_observation(self):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory)
            runtime=CaseRuntime({'case_id':'dev_001','scenario':'real transaction','allowed_actions':['status']},root/'case',root/'private.json')
            runtime.state_dir.mkdir()
            elsewhere=root/'foreign.json';elsewhere.write_text('{"schema_version":3}')
            (runtime.state_dir/'ledger.json').symlink_to(elsewhere)
            runtime.record('status',{},0)
            self.assertFalse(runtime.product_observations[-1]['durable_ledger_valid'])


if __name__=='__main__': unittest.main()
