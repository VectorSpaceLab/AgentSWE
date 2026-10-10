"""No provider requests: scope must grow with unknown future Candidate edits."""
from __future__ import annotations
import hashlib
import importlib.util
import json
import shutil
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
spec = importlib.util.spec_from_file_location('scope_0909', ROOT / 'evaluator/code_evidence_scope.py')
scope = importlib.util.module_from_spec(spec)
spec.loader.exec_module(scope)


def pack(root, *, max_pack_bytes, evidence_paths):
    text, files = '', []
    for rel in evidence_paths:
        if any(part in {'dist', '.git', 'runtime'} for part in Path(rel).parts):
            continue
        path = root / rel
        text += path.read_text()
        files.append({'path': rel, 'included_in_evidence_pack': True})
    if len(text.encode()) > max_pack_bytes:
        raise ValueError('source_context_exceeded')
    return {'files': files, 'tree_digest': scope.tree_digest(root)}, text


PACKER = {'source_manifest_and_pack': pack, 'SKIP_PARTS': {'.git', 'dist', 'runtime'}, 'MAX_SOURCE_PACK_BYTES': 6_000_000}


class ScopeTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.base = self.root / 'baseline'
        self.base.mkdir()
        (self.base / 'base.py').write_text('baseline = True\n')
        self.candidate = self.root / 'candidate'
        shutil.copytree(self.base, self.candidate)
        self.lock = scope.tree_digest(self.base)

    def tearDown(self):
        self.tmp.cleanup()

    def run_scope(self, **kwargs):
        return scope.build_scope(self.candidate, scope.tree_digest(self.candidate), self.root / 'output',
            baseline=self.base, expected_baseline_digest=kwargs.get('lock', self.lock), packer=PACKER)

    def test_unknown_future_file_and_local_import_covered(self):
        (self.candidate / 'brand_new.py').write_text('from secret_dependency import value\n')
        (self.candidate / 'secret_dependency.py').write_text('value = 4\n')
        result = self.run_scope()
        self.assertTrue(result['valid'])
        self.assertTrue(result['complete_change_coverage'])
        self.assertIn('brand_new.py', result['mandatory_included_paths'])
        self.assertIn('secret_dependency.py', result['mandatory_included_paths'])

    def test_baseline_mismatch_is_not_silently_accepted(self):
        result = self.run_scope(lock='bad')
        self.assertFalse(result['valid'])
        self.assertIn('trusted_baseline_digest_mismatch', result['errors'])

    def test_skip_parts_change_fails_closed(self):
        (self.candidate / 'runtime').mkdir()
        (self.candidate / 'runtime/hidden.py').write_text('malicious = True\n')
        result = self.run_scope()
        self.assertFalse(result['valid'])
        self.assertEqual(result['mandatory_excluded_paths'], ['runtime/hidden.py'])

    def test_new_optional_subsystem_file_cannot_be_trimmed(self):
        path = self.candidate / 'deeptutor/services/rag/future_candidate.py'
        path.parent.mkdir(parents=True)
        path.write_text('new_candidate_mechanism = True\n')
        result = self.run_scope()
        self.assertTrue(result['valid'])
        self.assertIn('deeptutor/services/rag/future_candidate.py', result['mandatory_included_paths'])
        self.assertNotIn('deeptutor/services/rag/future_candidate.py',
            [item['path'] for item in result['unchanged_optional_dependency_exclusions']])

    def prepare_optional_baseline(self):
        path = self.base / 'deeptutor/services/rag/existing.py'
        path.parent.mkdir(parents=True)
        path.write_text('value = 1\n')
        (self.base / 'registry.py').write_text('from deeptutor.services.rag.existing import value\n')
        shutil.copytree(self.base, self.candidate, dirs_exist_ok=True)
        self.lock = scope.tree_digest(self.base)

    def test_direct_import_from_candidate_change_is_included(self):
        self.prepare_optional_baseline()
        (self.candidate / 'future.py').write_text('from deeptutor.services.rag.existing import value\n')
        result = self.run_scope()
        self.assertTrue(result['valid'])
        self.assertIn('deeptutor/services/rag/existing.py', result['evidence_paths'])

    def test_only_indirect_unchanged_optional_branch_can_be_excluded(self):
        self.prepare_optional_baseline()
        (self.candidate / 'future.py').write_text('from registry import value\n')
        result = self.run_scope()
        self.assertTrue(result['valid'])
        self.assertIn('registry.py', result['evidence_paths'])
        self.assertNotIn('deeptutor/services/rag/existing.py', result['evidence_paths'])
        item = next(item for item in result['unchanged_optional_dependency_exclusions']
            if item['path'] == 'deeptutor/services/rag/existing.py')
        self.assertEqual(item['imported_by'], ['registry.py'])
        self.assertEqual(item['sha256'], hashlib.sha256((self.base / item['path']).read_bytes()).hexdigest())

    def test_deletion_has_tombstone_not_forged_frozen_source(self):
        (self.candidate / 'base.py').unlink()
        result = self.run_scope()
        self.assertTrue(result['valid'])
        tombstone = json.loads(Path(result['deleted_paths_evidence']).read_text())
        self.assertEqual(tombstone['entries'][0]['path'], 'base.py')
        self.assertFalse(tombstone['entries'][0]['candidate_exists'])
        self.assertFalse((self.candidate / 'base.py').exists())
        self.assertTrue(result['deleted_paths_judge_context_required'])

    def test_digest_mismatch_fails_before_packing(self):
        result = scope.build_scope(self.candidate, 'wrong', self.root / 'output',
            baseline=self.base, expected_baseline_digest=self.lock, packer=PACKER)
        self.assertFalse(result['valid'])
        self.assertIn('frozen_candidate_digest_mismatch', result['errors'])

    def test_scope_reentry_identical_only_and_different_inputs_preserved(self):
        self.run_scope()
        path = self.root / 'output/code_evidence_scope.json'
        original = path.read_bytes()
        self.run_scope()
        self.assertEqual(path.read_bytes(), original)
        (self.candidate / 'new.py').write_text('new = 1\n')
        with self.assertRaisesRegex(ValueError, 'refusing to overwrite'):
            self.run_scope()
        self.assertEqual(path.read_bytes(), original)


if __name__ == '__main__':
    unittest.main()
