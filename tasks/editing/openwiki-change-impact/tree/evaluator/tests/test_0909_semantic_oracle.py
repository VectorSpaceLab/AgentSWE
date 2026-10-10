from __future__ import annotations
import copy
import hashlib
import json
import shutil
import sys
import tempfile
import unittest
from pathlib import Path
ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
from agentloop.evaluator.semantic_oracle import compare, _read


class SemanticOracleTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.initial, self.observed = self.root / 'initial', self.root / 'observed'
        (self.initial / 'openwiki/api').mkdir(parents=True)
        (self.initial / 'openwiki/api/rename.md').write_text('formatName()\n<a id="public-anchor"></a>\nHANDWRITTEN: keep\n')
        (self.initial / 'openwiki/stable.md').write_text('unaffected\n')
        shutil.copytree(self.initial, self.observed)
        self.spec = {'expected_pages': ['openwiki/api/rename.md'],
            'required_text': {'openwiki/api/rename.md': ['renderName()']},
            'forbidden_text': {'openwiki/api/rename.md': ['formatName()']},
            'unaffected_pages': ['openwiki/stable.md'], 'preserved_tokens': ['HANDWRITTEN: keep'],
            'preserved_anchors': ['public-anchor'],
            'transaction': {'tenant_id': 'team', 'request_id': 'r1', 'generation': 1, 'payload_digest': 'digest'}}

    def tearDown(self):
        self.temp.cleanup()

    def checks(self):
        result = compare('dev_001', self.spec, self.initial, self.observed)
        return {item['id']: item for item in result['semantic_comparisons']}

    def test_unchanged_stale_docs_are_not_a_semantic_success(self):
        checks = self.checks()
        self.assertFalse(checks['required_facts:openwiki/api/rename.md']['passed'])
        self.assertFalse(checks['stale_facts_removed:openwiki/api/rename.md']['passed'])
        self.assertFalse(checks['required_impacted_docs_changed']['passed'])

    def test_actual_doc_fix_changes_oracle_but_fake_receipt_cannot_pass(self):
        file = self.observed / 'openwiki/api/rename.md'
        file.write_text(file.read_text().replace('formatName()', 'renderName()'))
        receipt = self.observed / '.openwiki-impact/tenants/team/receipts/r1.json'
        receipt.parent.mkdir(parents=True)
        receipt.write_text(json.dumps({**self.spec['transaction'], 'status': 'committed',
            'changed_paths': ['openwiki/api/rename.md'], 'documentation_hashes': {'openwiki/api/rename.md': 'invented'}}))
        checks = self.checks()
        self.assertTrue(checks['required_facts:openwiki/api/rename.md']['passed'])
        self.assertTrue(checks['durable_receipt_identity']['passed'])
        self.assertFalse(checks['durable_receipt_document_binding']['passed'])

    def test_unaffected_changes_detected_and_oracle_does_not_repair(self):
        file = self.observed / 'openwiki/stable.md'
        file.write_text('damaged')
        before = {p.relative_to(self.observed).as_posix(): p.read_bytes() for p in self.observed.rglob('*') if p.is_file()}
        self.assertFalse(self.checks()['unaffected_document:openwiki/stable.md']['passed'])
        after = {p.relative_to(self.observed).as_posix(): p.read_bytes() for p in self.observed.rglob('*') if p.is_file()}
        self.assertEqual(before, after)

    def test_external_symlink_not_read(self):
        secret = self.root / 'secret.txt'
        secret.write_text('do-not-read')
        (self.observed / 'escape').symlink_to(secret)
        self.assertIsNone(_read(self.observed, 'escape'))
        self.assertIsNone(_read(self.observed, '../secret.txt'))


if __name__ == '__main__':
    unittest.main()
