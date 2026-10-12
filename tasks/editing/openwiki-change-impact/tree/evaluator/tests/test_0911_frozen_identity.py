"""Provider-free identity controls; tiny fixtures are not accepted model work."""
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import runpy
import shutil
import socket
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
from agentloop.evaluator.controller import Controller
from agentloop.protocol import tree_digest
spec = importlib.util.spec_from_file_location('openwiki_identity_axes', ROOT / 'evaluator/formal_axes.py')
axes = importlib.util.module_from_spec(spec)
spec.loader.exec_module(axes)

def digest(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()

class FrozenIdentityTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.run = Path(self.temp.name) / 'run'
        self.owner = self.run / 'lifecycle'
        accepted = self.owner / 'candidate_1/repository'
        (accepted / 'dist').mkdir(parents=True)
        (accepted / 'empty').mkdir()
        (accepted / 'dist/cli.js').write_text('export const fixture = 1;\n')
        (accepted / 'internal.js').symlink_to('dist/cli.js')
        self.accepted = accepted
        controller = Controller(ROOT / 'input/repository', ROOT, self.owner,
                                'http://offline.invalid', builder_session_id='offline-identity-fixture')
        controller.records = [{'round': 1, 'candidate_digest': 'a' * 64,
            'builder_session_id': controller.builder_session_id,
            'build': {'valid': True, 'product_entry': str(accepted / 'dist/cli.js')},
            'dev_cases': {'dev_001': {'fixture': True}, 'dev_002': {'fixture': True}},
            'feedback': {'available': True, 'infrastructure_invalid': False}, 'revision': {}}]
        self.freeze = controller.freeze()
        self.candidate = Path(self.freeze['candidate_path'])
        self.freeze_path = self.owner / 'freeze_manifest.json'
        self.seal = self.owner / 'freeze_manifest.sha256'
        self.original_bytes = self.freeze_path.read_bytes()
        self.original_seal = self.seal.read_bytes()
        self.addCleanup(self.assert_original_if_untouched)
        self.manifest_intentionally_tampered = False
        self.network = patch.object(socket.socket, 'connect', side_effect=AssertionError('network forbidden'))
        self.process = patch.object(subprocess, 'Popen', side_effect=AssertionError('process forbidden'))
        self.network.start(); self.process.start()
        self.addCleanup(self.network.stop); self.addCleanup(self.process.stop)

    def assert_original_if_untouched(self):
        if not self.manifest_intentionally_tampered:
            self.assertEqual(self.freeze_path.read_bytes(), self.original_bytes)
            self.assertEqual(self.seal.read_bytes(), self.original_seal)

    def test_correct_dual_identity_and_canonical_independent_check(self):
        self.assertEqual(axes.frozen_identity_errors(self.freeze, self.run), [])
        self.assertTrue(axes.SHARED['_canonical_frozen_identity_errors'](self.freeze, self.run))
        bridge = axes.code_frozen_identity(self.freeze, self.run)
        self.assertNotEqual(bridge['lifecycle_candidate_digest'], bridge['code_candidate_digest'])
        self.assertEqual(bridge['candidate_path'], str(self.candidate.resolve()))
        self.assertEqual(bridge['freeze_sha256'], digest(self.freeze_path))
        self.assertEqual(axes.SHARED['checked_code_frozen_identity'](bridge, self.freeze, self.run, self.candidate), bridge['code_candidate_digest'])

    def test_other_directory_digest_and_path_are_rejected(self):
        bridge = axes.code_frozen_identity(self.freeze, self.run)
        with self.assertRaises(ValueError):
            axes.SHARED['checked_code_frozen_identity']({**bridge, 'candidate_path': str(self.accepted)}, self.freeze, self.run, self.candidate)
        create = runpy.run_path('@@AGENTSWE_LEGACY_HARBOR@@/0825-10create-v4/code_eval.py')
        (self.accepted / 'dist/cli.js').write_text('different source\n')
        other = create['tree_digest'](self.accepted)
        with self.assertRaises(ValueError):
            axes.SHARED['checked_code_frozen_identity']({**bridge, 'code_candidate_digest': other}, self.freeze, self.run, self.candidate)

    def test_wrong_freeze_sha_rejected_by_shared_bridge(self):
        bridge = axes.code_frozen_identity(self.freeze, self.run)
        with self.assertRaises(ValueError):
            axes.SHARED['checked_code_frozen_identity']({**bridge, 'freeze_sha256': '0' * 64}, self.freeze, self.run, self.candidate)

    def test_file_mutation_addition_deletion_rejected_before_code(self):
        for kind in ('mutate', 'add', 'delete'):
            with self.subTest(kind=kind):
                path = self.candidate / 'dist/cli.js'
                before = path.read_bytes()
                path.parent.chmod(0o755); path.chmod(0o644)
                if kind == 'mutate': path.write_text('tampered\n')
                elif kind == 'add': (path.parent / 'extra.js').write_text('added\n')
                else: path.unlink()
                if path.exists(): path.chmod(0o444)
                if (path.parent / 'extra.js').exists(): (path.parent / 'extra.js').chmod(0o444)
                path.parent.chmod(0o555)
                with patch.object(axes.runpy, 'run_path', side_effect=AssertionError('Code digest read must not run')):
                    self.assertTrue(axes.frozen_identity_errors(self.freeze, self.run))
                    with self.assertRaises((ValueError, OSError)):
                        axes.code_frozen_identity(self.freeze, self.run)
                path.parent.chmod(0o755)
                if path.exists(): path.chmod(0o644)
                path.write_bytes(before); path.chmod(0o444)
                if (path.parent / 'extra.js').exists(): (path.parent / 'extra.js').unlink()
                path.parent.chmod(0o555)

    def test_symlink_escape_is_rejected(self):
        link = self.candidate / 'internal.js'
        self.candidate.chmod(0o755); link.unlink(); link.symlink_to('/etc/passwd'); self.candidate.chmod(0o555)
        errors = axes.frozen_identity_errors(self.freeze, self.run)
        self.assertTrue(errors)
        with self.assertRaises(ValueError): axes.code_frozen_identity(self.freeze, self.run)

    def test_seal_and_original_manifest_tamper(self):
        self.manifest_intentionally_tampered = True
        self.seal.chmod(0o644); self.seal.write_text('0' * 64); self.seal.chmod(0o444)
        self.assertTrue(axes.frozen_identity_errors(self.freeze, self.run))
        self.seal.chmod(0o644); self.seal.write_bytes(self.original_seal); self.seal.chmod(0o444)
        self.freeze_path.chmod(0o644); self.freeze_path.write_text('{}'); self.freeze_path.chmod(0o444)
        self.assertTrue(axes.frozen_identity_errors(self.freeze, self.run))

    def test_latest_accepted_history_and_source_must_match(self):
        state_path = self.owner / 'controller_state.json'
        state = json.loads(state_path.read_text())
        state['records'][-1]['candidate_digest'] = 'b' * 64
        state_path.write_text(json.dumps(state))
        self.assertTrue(axes.frozen_identity_errors(self.freeze, self.run))
        state['records'][-1]['candidate_digest'] = 'a' * 64
        state_path.write_text(json.dumps(state))
        (self.accepted / 'dist/cli.js').write_text('changed accepted bytes\n')
        self.assertTrue(axes.frozen_identity_errors(self.freeze, self.run))

    def test_missing_state_and_metadata_remain_invalid(self):
        state_path = self.owner / 'controller_state.json'
        state_path.unlink()
        self.assertTrue(axes.frozen_identity_errors(self.freeze, self.run))
        state_path.write_text('{}')
        self.assertTrue(axes.frozen_identity_errors(self.freeze, self.run))
        self.assertTrue(axes.frozen_identity_errors({**self.freeze, 'candidate_materialized_digest': 'b' * 64}, self.run))

    def test_new_output_root_cannot_relabel_old_freeze(self):
        alias = Path(self.temp.name) / 'new-output'
        (alias / 'lifecycle').mkdir(parents=True)
        shutil.copy2(self.freeze_path, alias / 'lifecycle/freeze_manifest.json')
        shutil.copy2(self.seal, alias / 'lifecycle/freeze_manifest.sha256')
        self.assertTrue(axes.frozen_identity_errors(self.freeze, alias))

    def test_legacy_missing_product_guard_does_not_gain_identity(self):
        state_path = self.owner / 'controller_state.json'
        state = json.loads(state_path.read_text()); state.pop('product_execution_guard')
        state_path.write_text(json.dumps(state))
        self.assertTrue(axes.frozen_identity_errors(self.freeze, self.run))
        with self.assertRaises(ValueError): axes.code_frozen_identity(self.freeze, self.run)

    def test_unknown_code_request_stays_blocked_after_digest_bridge(self):
        bridge = axes.code_frozen_identity(self.freeze, self.run)
        output = self.run / 'formal_scoring/code_axis'; output.mkdir(parents=True)
        intent = output / 'scoring_intent.json'; intent.write_text(json.dumps({'candidate_digest': self.freeze['candidate_digest'], 'state': 'unknown'}))
        started = output / 'code_logical_request_started.json'; started.write_text('{"state":"unknown"}\n')
        before = {p.name: p.read_bytes() for p in output.iterdir()}
        req = self.run / 'public'; req.mkdir(); (req / 'requirements.md').write_text('fixture')
        rubric = self.run / 'rubric.md'; rubric.write_text('fixture')
        credential = self.run / 'credential'; credential.write_text('placeholder'); credential.chmod(0o600)
        runner = runpy.run_path('@@AGENTSWE_EDITING_CONTROL@@/code_judge_runner.py', run_name='offline_code_guard')
        argv = ['runner', '--candidate-source', str(self.candidate), '--public-requirements', str(req), '--code-rubric', str(rubric), '--credential-file', str(credential), '--output-dir', str(output), '--expected-candidate-digest', bridge['code_candidate_digest']]
        with patch.object(sys, 'argv', argv), self.assertRaisesRegex(SystemExit, 'refusing overwrite/resampling'):
            runner['main']()
        self.assertEqual({p.name: p.read_bytes() for p in output.iterdir()}, before)

    def test_tamper_during_canonical_read_is_rejected(self):
        original = runpy.run_path('@@AGENTSWE_LEGACY_HARBOR@@/0825-10create-v4/code_eval.py')['tree_digest']
        def tamper(root):
            path = root / 'dist/cli.js'; path.chmod(0o644); path.write_text('race\n'); path.chmod(0o444)
            return original(root)
        with patch.object(axes.runpy, 'run_path', return_value={'tree_digest': tamper}):
            with self.assertRaises(ValueError): axes.code_frozen_identity(self.freeze, self.run)

if __name__ == '__main__': unittest.main()
