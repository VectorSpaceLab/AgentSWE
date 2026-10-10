"""Provider-free complete-change Code scope and immutable evidence tests."""
import hashlib
import json
import shutil
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from evaluator import code_inputs as inputs


class CodeInputTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name)
        self.baseline, self.source = self.root / 'baseline', self.root / 'source'
        self.baseline.mkdir()
        (self.baseline / 'aider').mkdir()
        (self.baseline / 'aider/website').mkdir()
        (self.baseline / 'aider/main.py').write_text('import helper\n')
        (self.baseline / 'helper.py').write_text('API = 1\n')
        (self.baseline / 'aider/website/old.md').write_text('unchanged website\n')
        shutil.copytree(self.baseline, self.source)
        (self.source / 'aider/worktree_plan_adapter.py').write_text('import helper\n')
        (self.source / 'build_result.json').write_text(json.dumps({'classification': 'ready_for_lower',
            'changed_paths': ['aider/worktree_plan_adapter.py']}))
        (self.root / 'input').mkdir()
        (self.root / 'input/01.md').write_text('full public transaction requirements')
        self.addCleanup(mock.patch.stopall)
        mock.patch.object(inputs, 'ROOT', self.root).start()
        mock.patch.object(inputs, 'packer', return_value={'tree_digest': self.digest,
            'MAX_SOURCE_PACK_BYTES': 6000000, 'source_manifest_and_pack': self.pack}).start()

    @staticmethod
    def digest(path):
        return hashlib.sha256(json.dumps(inputs.inventory(path), sort_keys=True).encode()).hexdigest()

    @staticmethod
    def pack(source, *, max_pack_bytes, evidence_paths):
        entries = [{'path': path, 'included_in_evidence_pack': '.git' not in Path(path).parts}
                   for path in evidence_paths]
        return {'tree_digest': CodeInputTests.digest(source), 'files': entries}, '\n'.join(evidence_paths)

    def scope(self, output='out'):
        return inputs.build_scope(self.source, self.digest(self.source), self.root / output,
            baseline=self.baseline, expected_baseline=self.digest(self.baseline))

    def test_all_changed_and_direct_imports_included(self):
        scope = self.scope()
        self.assertTrue(scope['valid'])
        self.assertIn('aider/worktree_plan_adapter.py', scope['mandatory_included_paths'])
        self.assertIn('helper.py', scope['evidence_paths'])
        self.assertNotIn('aider/website/old.md', scope['evidence_paths'])

    def test_new_change_in_unchanged_exclusion_is_never_hidden(self):
        (self.source / 'aider/website/old.md').write_text('candidate changed website')
        self.assertIn('aider/website/old.md', self.scope()['mandatory_included_paths'])

    def test_changed_git_metadata_cannot_be_skipped(self):
        (self.source / '.git').mkdir()
        (self.source / '.git/config').write_text('candidate edited metadata')
        with self.assertRaisesRegex(ValueError, 'uncovered'):
            self.scope()

    def test_baseline_drift_rejected(self):
        expected = self.digest(self.baseline)
        (self.baseline / 'aider/main.py').write_text('changed after baseline lock')
        with self.assertRaisesRegex(ValueError, 'identity mismatch'):
            inputs.build_scope(self.source, self.digest(self.source), self.root / 'out',
                baseline=self.baseline, expected_baseline=expected)

    def test_same_scope_repeat_is_immutable(self):
        first = self.scope()
        self.assertEqual(first, self.scope())
        (self.source / 'aider/worktree_plan_adapter.py').write_text('API = 2\n')
        with self.assertRaisesRegex(ValueError, 'changed; use a new'):
            self.scope()

    def test_deleted_source_is_attested_not_false_source_citation(self):
        (self.source / 'helper.py').unlink()
        scope = self.scope()
        self.assertIn('helper.py', scope['deleted_paths'])
        self.assertNotIn('helper.py', scope['evidence_paths'])
        self.assertEqual(scope['deleted_file_evidence']['helper.py']['sha256'], inputs.sha(self.baseline / 'helper.py'))

    def test_declared_candidate_bytecode_edit_fails_closed(self):
        (self.source / 'build_result.json').write_text(json.dumps({'classification': 'ready_for_lower',
            'changed_paths': ['aider/__pycache__/evil.pyc']}))
        with self.assertRaisesRegex(ValueError, 'cache bytes'):
            self.scope()


if __name__ == '__main__':
    unittest.main()
