"""Real scoring-intent/case routing with explicitly scripted judge output."""
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[2]
sys.path[:0] = [str(ROOT), str(ROOT / 'harbor')]
from evaluator import semantic_execution as semantic
from harbor import formal_one_stop as formal


class SemanticExecutionTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(); self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        def save(name, value):
            p = self.root / name
            p.write_text(json.dumps(value))
            return str(p)
        self.record = {
            'case_id': 'dev_001', 'classification': 'valid', 'infra_valid': True,
            'product_entry_observed': True, 'broker_calls_delta': 4,
            'broker_successful_calls_delta': 4, 'artifact_present': True,
            'executed_task_path': save('task.json', {'task': 'exact randomized public task'}),
            'artifact_path': save('artifact.json', {'case_id': 'WRONG', 'summary': 'incorrect result'}),
            'trajectory_path': save('trajectory.json', {'model_selected_actions_only': True, 'events': ['finish']}),
            'native_evidence_path': save('native.json', {'real_product': True, 'case_id': 'dev_001',
                                                       'agent_artifact_origin': 'model_finish_action'}),
        }
        r = self.record
        native = json.loads(Path(r['native_evidence_path']).read_text())
        native.update(executed_task_sha256=semantic.digest(r['executed_task_path']),
                      private_oracle_visible=False)
        Path(r['native_evidence_path']).write_text(json.dumps(native))
        for key in ('executed_task', 'artifact', 'native_evidence'):
            r[key + '_sha256'] = semantic.digest(r[key + '_path'])
        r['private_oracle_comparison_path'] = save('comparison.json', {
            'case_id': 'dev_001', 'private_oracle_not_candidate_visible': True,
            'executed_task_sha256': r['executed_task_sha256'], 'private_canary': 'ORACLE_SECRET'})
        r['private_oracle_comparison_sha256'] = semantic.digest(r['private_oracle_comparison_path'])
        r['artifact_provenance'] = {'artifact_owner': 'model_via_dyad_typed_chat',
            'producer_entry': 'model finish action captured from Dyad persisted typed chat',
            'evaluator_synthesized': False, 'exists': True, 'sha256': r['artifact_sha256']}
        self.output = Path(save('result.json', r))
        self.rubric = Path(save('rubric.md', 'Assess task quality independently.'))

    def normalize(self):
        return semantic.normalize_execution(self.record, candidate_digest='candidate-source',
            case_id='dev_001', output=self.output, evidence_root=self.root)

    def score(self, record):
        return semantic.score_public(record, output=self.root / 'score', evidence_root=self.root,
            rubric=self.rubric, broker_endpoint='http://127.0.0.1:1/v1/responses')

    def complete_judge(self, command, **kwargs):
        # Exercise the real immutable shared scoring intent, not a scorer stub.
        out = Path(command[command.index('--output-dir') + 1])
        self.judge_inputs = {flag: command[command.index(flag)+1] for flag in
            ('--task-input', '--agent-artifact', '--native-evidence', '--oracle-summary', '--trajectory')}
        (out/'result_score_contract.json').write_text(json.dumps({
            'case_id': 'dev_001', 'contract_valid': True, 'result_score_publishable': True,
            'result_score': 17, 'assessment': 'Claims contradict the executed public task.',
            'judge': {'model': 'gpt-5.6-sol', 'reasoning_effort': 'max'},
            'provider_usage': {'logical_requests': 1, 'completed_responses': 1,
                'transport_attempts': 1, 'input_tokens': 100, 'output_tokens': 20, 'total_tokens': 120}}))
        from subprocess import CompletedProcess
        return CompletedProcess(command, 0, '', '')

    def test_wrong_claims_are_independently_scored_with_exact_inputs_and_cached(self):
        record = self.normalize()
        with patch('execution_scoring.subprocess.run', side_effect=self.complete_judge) as judge:
            first = self.score(record); second = self.score(record)
        self.assertEqual(first['score'], 17); self.assertTrue(second['cached'])
        self.assertEqual(judge.call_count, 1)
        self.assertEqual(self.judge_inputs['--task-input'], self.record['executed_task_path'])
        self.assertEqual(self.judge_inputs['--oracle-summary'], self.record['private_oracle_comparison_path'])
        feedback = semantic.public_case_feedback({**record, 'semantic_feedback': first})
        self.assertEqual(feedback['result']['score'], 17)
        self.assertNotIn('ORACLE_SECRET', json.dumps(feedback))
        self.assertNotIn('contract_path', json.dumps(feedback))

    def test_unknown_judge_is_not_repeated(self):
        from subprocess import CompletedProcess
        with patch('execution_scoring.subprocess.run', return_value=CompletedProcess([], 2, '', '')) as judge:
            first = self.score(self.normalize()); second = self.score(self.normalize())
        self.assertIsNone(first['score']); self.assertIsNone(second['score'])
        self.assertEqual(judge.call_count, 1); self.assertFalse(first['round_consumed'])

    def test_infrastructure_never_invokes_judge_or_consumes(self):
        self.record.update(classification='infrastructure-invalid', infra_valid=False)
        with patch('execution_scoring.subprocess.run') as judge:
            result = self.score(self.normalize())
        judge.assert_not_called(); self.assertIsNone(result['score']); self.assertFalse(result['round_consumed'])

    def test_missing_core_artifact_after_healthy_execution_is_evidenced_zero(self):
        self.record.update(classification='candidate_failure', failure_class='model_authored_artifact_missing',
                           artifact_present=False)
        Path(self.record['artifact_path']).unlink()
        with patch('execution_scoring.subprocess.run') as judge:
            result = self.score(self.normalize())
        judge.assert_not_called(); self.assertEqual(result['classification'], 'candidate_zero')
        self.assertEqual(result['score'], 0); self.assertTrue(result['round_consumed'])

    def test_changed_or_escaping_capture_cannot_be_scored(self):
        for mode in ('hash', 'escape', 'oracle'):
            with self.subTest(mode=mode):
                prior = dict(self.record)
                if mode == 'hash': self.record['artifact_sha256'] = 'wrong'
                elif mode == 'escape': self.record['artifact_path'] = '/etc/passwd'
                else: self.record['private_oracle_comparison_sha256'] = 'wrong'
                with patch('execution_scoring.subprocess.run') as judge:
                    result = self.score(self.normalize())
                judge.assert_not_called(); self.assertIsNone(result['score'])
                self.record = prior

    def test_score_is_required_for_accepted_round(self):
        self.assertFalse(formal.Lifecycle._public_valid(self.record))
        self.assertTrue(formal.Lifecycle._public_valid({'semantic_feedback': {
            'classification': 'scoreable', 'score': 17, 'round_consumed': True, 'contract_valid': True}}))

    def test_live_public_wrapper_routes_execution_to_independent_score(self):
        candidate = self.root / 'candidate'; candidate.mkdir()
        (candidate / 'product.py').write_text('product = 1')
        workspace = self.root / 'submission'; workspace.mkdir()
        life = formal.Lifecycle(run_dir=self.root, workspace=workspace, public_endpoint='unused',
            baseline={}, result_judge_endpoint='http://127.0.0.1:1/v1/responses')
        self.addCleanup(life.close)
        with patch.object(formal, 'copy_runtime', return_value=candidate), \
                patch.object(formal, 'command_result', return_value={'exit_code': 0}), \
                patch('execution_scoring.subprocess.run', side_effect=self.complete_judge) as judge:
            result = life._run_public_case(candidate, 'dev_001', 'unused', self.root)
        self.assertEqual(judge.call_count, 1)
        self.assertEqual(result['semantic_feedback']['score'], 17)
        self.assertTrue(life._public_valid(result))
        self.assertEqual(result['candidate_digest'], formal.tree_digest(candidate))
        self.assertTrue((self.root / 'scored_execution.json').is_file())

    def test_hidden_wrapper_reads_the_actual_record_paths(self):
        import importlib.util
        spec = importlib.util.spec_from_file_location('dyad_axes_test', ROOT/'evaluator/formal_axes.py')
        module = importlib.util.module_from_spec(spec); spec.loader.exec_module(module)
        files = module._shared['case_files'](self.root, 'dev_001', self.normalize(), self.output)
        self.assertEqual(files['case_input'], Path(self.record['executed_task_path']))
        self.assertEqual(module._shared['validate_model_artifact_provenance'](
            self.normalize(), files['artifact'], files['trajectory'], 'dev_001'), [])


if __name__ == '__main__': unittest.main()
