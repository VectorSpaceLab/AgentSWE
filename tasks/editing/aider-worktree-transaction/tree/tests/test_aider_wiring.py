"""Actual staged launcher fence, Builder config and judge CLI routing."""
import hashlib
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import types
import unittest
from unittest.mock import patch
ROOT=Path(__file__).resolve().parents[1]
sys.path[:0]=[str(ROOT),str(ROOT/'harbor'),str(ROOT.parents[1]/'v2-control-stage/release'),'@@AGENTSWE_EDITING_CONTROL@@']
from harbor import formal_one_stop as task
from harbor import readiness_launcher
from evaluator import readiness_smoke, formal_finalize, code_inputs

class AiderWiringTests(unittest.TestCase):
    def test_binding_rejection_precedes_run_directory_and_broker(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp).resolve();binding=root/'binding.json';binding.write_text('{"task":"codex"}')
            args=types.SimpleNamespace(benchmark=root,run_dir=root/'fresh',readiness_binding_file=binding,
                readiness_binding_sha256=hashlib.sha256(binding.read_bytes()).hexdigest())
            with patch.dict(sys.modules, {'readiness_binding':types.SimpleNamespace(verify_binding=lambda *a: (_ for _ in ()).throw(ValueError('source drift')))}), \
                    patch('judge_broker_runtime.start_judge_broker') as broker:
                with self.assertRaisesRegex(ValueError,'source drift'):
                    readiness_launcher.run(args, args.run_dir)
            self.assertFalse(args.run_dir.exists());broker.assert_not_called()

    def test_builder_config_one_case_retains_resources(self):
        with tempfile.TemporaryDirectory() as tmp:
            run=Path(tmp).resolve();public=run/'public';(public/'input/repository/aider').mkdir(parents=True)
            (public/'input/repository/aider/main.py').write_text('pass\n')
            workspace=run/'submission';workspace.mkdir();provider=run/'provider.toml';provider.write_text('')
            controller=types.SimpleNamespace(controller=types.SimpleNamespace(readiness_profile='single-dev-two-round-hidden-smoke-v1',max_dev_rounds=2),token='fixture',socket_path=run/'socket')
            config=task.builder_config(run,public,workspace,controller,provider,pilot_not_formal=True)
            self.assertFalse(task.read_json(config)['environment']['delete'])
            prompt=(run/'builder_task/instruction.md').read_text()
            self.assertIn('exactly TWO',prompt);self.assertIn('uninterrupted',prompt);self.assertNotIn('dev_002',prompt)

    def test_readiness_prompt_includes_focused_source_test_contract(self):
        with tempfile.TemporaryDirectory() as tmp:
            run=Path(tmp).resolve();public=run/'public';(public/'input/repository/aider').mkdir(parents=True)
            (public/'input/repository/aider/main.py').write_text('pass\n')
            workspace=run/'submission';workspace.mkdir();provider=run/'provider.toml';provider.write_text('')
            controller=types.SimpleNamespace(controller=types.SimpleNamespace(readiness_profile='single-dev-two-round-hidden-smoke-v1',max_dev_rounds=2),token='fixture',socket_path=run/'socket')
            task.builder_config(run,public,workspace,controller,provider,pilot_not_formal=True)
            prompt=(run/'builder_task/instruction.md').read_text()
            self.assertIn('focused source-adjacent Python test',prompt)
            self.assertIn('run that test locally',prompt)

    def test_zero_hidden_runs_both_single_request_judge_entrypoints(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp).resolve();run=root/'run';run.mkdir()
            task.write_json(run/'lifecycle/freeze_manifest.json',{'readiness_profile':'single-dev-two-round-hidden-smoke-v1','candidate_digest':'a'*64})
            case=run/'lifecycle/evaluations/hidden/test_001';case.mkdir(parents=True)
            for name in ('lower.stdout.log','native_evidence.json','oracle_comparison.json','result.json'):
                (case/name).write_text('{}')
            (root/'test_cases/test_001').mkdir(parents=True);(root/'test_cases/test_001/input.md').write_text('task')
            (root/'evaluator').mkdir()
            for name in ('rubric.md','result_dimensions.json','code_rubric.md','result_judge.py','code_eval.py'):
                (root/'evaluator'/name).write_text('{}')
            (root/'meta').mkdir();(root/'meta/code_rubric.md').write_text('rubric')
            source=run/'code-source';source.mkdir();requirements=run/'requirements';requirements.mkdir()
            calls=[];usage={'input_tokens':3,'output_tokens':2,'total_tokens':5,'transport_attempts':1,'logical_requests':1,'completed_responses':1}
            def dispatch(command,**kwargs):
                calls.append(command);output=Path(command[command.index('--output-dir')+1]);role=output.name
                contract={'contract_valid':True,'candidate_digest':'d'*64,'code_score_publishable':True,'result_score_publishable':True,
                    'case_id':'test_001','judge':{'model':'gpt-5.6-sol','reasoning_effort':'max'},'provider_usage':usage,
                    'result_score':0,'code_score':0,
                    # The judge publishes each rubric dimension as its own
                    # {score,max,evidence} component and no aggregate map.
                    'task_completion':{'score':0,'max':50,'evidence':'fixture'},
                    'evidence_grounding':{'score':0,'max':30,'evidence':'fixture'},
                    'recovery_and_safety':{'score':0,'max':20,'evidence':'fixture'}}
                task.write_json(output/(role+'_score_contract.json'),contract)
                if role=='result':task.write_json(output/'model_response.json',{'synthetic_fixture':True})
                else:
                    task.write_json(output/'code_model_response.json',{'synthetic_fixture':True})
                    task.write_json(output/'source_manifest.json',{'tree_digest':'d'*64})
                    task.write_json(output/'code_provider_response-attempts.json',{'attempts':[{'response_id':'code-request'}]})
                return subprocess.CompletedProcess(command,0,'','')
            with patch.object(formal_finalize,'result_task_input',return_value=(root/'test_cases/test_001/input.md',{})), \
                    patch.object(code_inputs,'prepare_code_inputs',return_value=(source,requirements,'d'*64,[])), \
                    patch.object(formal_finalize,'RESULT_JUDGE',Path('@@AGENTSWE_EDITING_CONTROL@@/result_judge.py')), \
                    patch.object(formal_finalize,'CREATE_CODE_JUDGE',root/'evaluator/code_eval.py'), \
                    patch.object(readiness_smoke.subprocess,'run',dispatch), \
                    patch.object(task,'stats',side_effect=[{'runtime':{'calls':0}},{'runtime':{'calls':1},'attempts':[{'request_id':'result-request'}]}]):
                result=readiness_smoke.run(root,run,{'classification':'candidate_zero'},root/'credential','http://127.0.0.1:9999/v1/responses')
            self.assertTrue(result['complete'],result)
            self.assertEqual(len(calls),2)
            # readiness_judge_validation recomputes these from this task's own
            # rubric and refuses the bundle unless they match exactly, so the
            # expectation is recomputed the same way rather than written out: a
            # constant here is what let the original defect through.
            import result_judge
            expected_maxima = result_judge.load_dimensions(
                result_judge.rubric_dimensions_path(ROOT/readiness_smoke.RUBRIC_RELATIVE))
            self.assertEqual(task.read_json(run/'readiness_scoring/result_validation_input.json')['dimension_maxima'],
                             expected_maxima)
            self.assertEqual(calls[0][calls[0].index('--max-transport-attempts')+1],'1')
            self.assertFalse(result['formal_result_publishable']);self.assertFalse(result['code_score_publishable'])
            self.assertFalse(task.read_json(run/'readiness_scoring/artifact_absence.json')['candidate_authored'])

if __name__=='__main__':unittest.main()
