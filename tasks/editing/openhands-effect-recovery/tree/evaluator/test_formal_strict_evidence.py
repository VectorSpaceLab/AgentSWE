"""Forged/incomplete formal artifacts must not reach an independent Result call."""
import json
import hashlib
from pathlib import Path
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'evaluator'))
from evaluator import semantic_finalize as finalize
from evaluator import case_evidence
from harbor import formal_one_stop


class StrictHiddenEvidenceTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.run = Path(self.temp.name).resolve()
        self.base = self.run / 'hidden/test_001'
        self.base.mkdir(parents=True)
        self.frozen = self.run / 'lifecycle/frozen_candidate'
        self.frozen.mkdir(parents=True)
        (self.frozen / 'product.ts').write_text('source')
        self.artifact = self.base / 'agent_result.json'
        self.artifact.write_text('{"decision": "success"}')
        self.record = self.base / 'record.json'
        self.record.write_text('{"case_id": "test_001"}')
        task = ROOT / 'test_cases/test_001/natural_task.md'
        if not task.is_file():
            task = ROOT / 'test_cases/test_001/input.md'
        self.launcher = {'case_id': 'test_001',
            'candidate_source_digest': finalize.tree_digest(self.frozen),
            'task_sha256': case_evidence.sha(task),
            'agent_artifact_sha256': case_evidence.sha(self.artifact)}
        fixtures = ROOT / 'test_cases/test_001/assets/fixtures.json'
        if not fixtures.is_file():
            fixtures = ROOT / 'test_cases/test_001/assets/scenario.json'
        if fixtures.is_file():
            self.launcher['fixture_sha256'] = case_evidence.sha(fixtures)

    def prepare(self):
        (self.base / 'launcher_result.json').write_text(json.dumps(self.launcher))
        return finalize.evidence_files(ROOT, self.run, 'openhands', 'test_001',
            self.record, json.loads(self.record.read_text()), self.artifact)

    def test_persisted_artifact_without_original_model_response_is_rejected(self):
        with self.assertRaises(FileNotFoundError):
            self.prepare()
        self.assertFalse((self.run / 'formal_scoring/result_axis/test_001/judge').exists())

    def test_different_frozen_product_is_rejected_before_scoring(self):
        self.launcher['candidate_source_digest'] = 'different-product'
        with self.assertRaisesRegex(ValueError, 'Candidate digest mismatch'):
            self.prepare()

    def test_model_response_cannot_be_replaced_by_persisted_claim(self):
        final = self.base / 'model_final_response.txt'
        final.write_text('{"decision": "other"}')
        self.launcher['model_final_response_sha256'] = case_evidence.sha(final)
        with self.assertRaisesRegex(ValueError, 'differs from captured'):
            self.prepare()

    def test_valid_captured_fixture_crosses_formal_and_public_evidence_adapters(self):
        # This is an offline synthetic capture, not a real lower/model result.
        # No origin or binding function is mocked: it exercises their contract.
        lower = case_evidence.lower
        action = sorted(lower.ACTION_NAMES)[0]
        choice = {'kind': 'action', 'action': action, 'arguments': {}}
        captured = self.base / 'model_action_001_response.txt'
        captured.write_text(json.dumps(choice))
        steps = [{'step': 1, 'action': action, 'dispatched': True,
            'product_method': action, 'observation': {'ok': True},
            'model_response_sha256': case_evidence.sha(captured)}]
        trajectory_digest = hashlib.sha256(lower.canonical_json(steps).encode()).hexdigest()
        nonce = {'diagnostic_nonce': 'offline-only'}
        nonce_digest = hashlib.sha256(lower.canonical_json(nonce).encode()).hexdigest()
        (self.base / 'private_world_config.json').write_text(json.dumps({'nonce': nonce}))
        trajectory = {'schema_version': 'agentswe-openhands-trajectory/v1',
            'case_id': 'test_001', 'steps': steps, 'product_entry': lower.PRODUCT_ENTRY,
            'trajectory_digest': trajectory_digest}
        artifact = {'schema_version': 'agentswe-openhands-agent-result/v1',
            'case_id': 'test_001', 'model': lower.MODEL, 'reasoning_effort': lower.EFFORT,
            'product': lower.PRODUCT_NAME, 'product_entry': lower.PRODUCT_ENTRY,
            'actions': [action], 'observations': [{'action': action, 'result': {'ok': True}}],
            'trajectory_digest': trajectory_digest, 'nonce_digest': nonce_digest,
            'decision': 'partial', 'rationale': 'Synthetic offline evidence fixture.'}
        self.artifact.write_text(json.dumps(artifact))
        final = self.base / 'model_final_response.txt'
        final.write_text(json.dumps(artifact))
        context = {'actual_trajectory': steps, 'trajectory_digest': trajectory_digest,
                   'nonce_digest': nonce_digest}
        payload = {'model': lower.MODEL, 'reasoning': {'effort': lower.EFFORT},
                   'input': 'Offline fixture context\n' + json.dumps(context)}
        payload_path = self.base / 'model_final_response.txt.payload.json'
        payload_path.write_text(json.dumps(payload))
        (self.base / 'model_final_response.txt.request.json').write_text(json.dumps({
            'state': 'completed', 'case_id': 'test_001', 'phase': 'artifact',
            'response_sha256': case_evidence.sha(final), 'payload_sha256': case_evidence.sha(payload_path),
            'request_sha256': hashlib.sha256(json.dumps(payload).encode()).hexdigest()}))
        for name, value in [('trajectory.json', trajectory),
            ('case_world.json', {'case_id': 'test_001', 'comparisons': {}, 'observed': {}}),
            ('environment_preflight.json', {'valid': True}),
            ('final_product_state.json', {'ready': True, 'storage': {'private': 'hidden'}})]:
            (self.base / name).write_text(json.dumps(value))
        for field, name in [('agent_artifact_sha256', 'agent_result.json'),
            ('trajectory_sha256', 'trajectory.json'), ('case_world_sha256', 'case_world.json'),
            ('model_final_response_sha256', 'model_final_response.txt'),
            ('environment_preflight_sha256', 'environment_preflight.json'),
            ('final_product_state_sha256', 'final_product_state.json')]:
            self.launcher[field] = case_evidence.sha(self.base / name)
        self.launcher.update(real_execution=True, execution_phase={
            'phase': 'complete', 'response_sha256': case_evidence.sha(final)})
        trajectory_path, native_path, oracle_path = self.prepare()
        native = json.loads(native_path.read_text())
        self.assertEqual(trajectory_path, self.base / 'trajectory.json')
        self.assertTrue(native['typed_trajectory_binding']['bound'])
        self.assertNotIn('storage', native['final_product_state'])
        self.assertEqual(json.loads(oracle_path.read_text())['case_id'], 'test_001')
        public_adapter = case_evidence.prepare_case_evidence(case_id='test_001',
            record_path=self.record, candidate=self.frozen,
            candidate_digest=self.launcher['candidate_source_digest'], output=self.run / 'public-adapter')
        self.assertEqual(public_adapter['artifact'], self.artifact)
        self.assertEqual(public_adapter['native_evidence'].read_bytes(), native_path.read_bytes())

    def test_freeze_lower_and_create_digest_agree_on_nontext_and_symlink_product(self):
        (self.frozen / 'empty-dir').mkdir()
        (self.frozen / 'binary.bin').write_bytes(b'\x00\xff\x01')
        (self.frozen / 'link').symlink_to('product.ts')
        self.assertEqual(finalize.tree_digest(self.frozen), case_evidence.tree_digest(self.frozen))
        # Product freeze uses the imported controller tree_digest. The separate
        # directory_digest belongs to the three-file Builder delivery envelope.
        self.assertEqual(finalize.tree_digest(self.frozen), formal_one_stop.tree_digest(self.frozen))
        self.assertNotEqual(finalize.tree_digest(self.frozen), formal_one_stop.directory_digest(self.frozen))


if __name__ == '__main__':
    unittest.main()
