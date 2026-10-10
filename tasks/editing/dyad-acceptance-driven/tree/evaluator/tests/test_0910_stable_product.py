"""Actual Git materialization checks; no model calls or score claims."""
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch
ROOT = Path(__file__).resolve().parents[2]
sys.path[:0] = [str(ROOT), str(ROOT/'harbor')]
from harbor.stable_product import product_source_digest
from adapters.candidate_materialize import materialize, tree_digest

class StableProductTests(unittest.TestCase):
    def test_real_materialization_is_stable_across_git_commit_times_and_reports(self):
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw); base = root/'base'; (base/'src').mkdir(parents=True)
            (base/'src/example.ts').write_text('const x = 1;\n')
            submission = root/'submission'; submission.mkdir()
            (submission/'solution.patch').write_text('diff --git a/src/example.ts b/src/example.ts\n--- a/src/example.ts\n+++ b/src/example.ts\n@@ -1 +1 @@\n-const x = 1;\n+const x = 2;\n')
            first, second, third = root/'first', root/'second', root/'third'
            with patch.dict(os.environ, {'GIT_AUTHOR_DATE':'2001-01-01T00:00:00Z','GIT_COMMITTER_DATE':'2001-01-01T00:00:00Z'}):
                materialize(base, submission, first)
            (submission/'edit_report.json').write_text('{"changed":"metadata only"}')
            with patch.dict(os.environ, {'GIT_AUTHOR_DATE':'2002-01-01T00:00:00Z','GIT_COMMITTER_DATE':'2002-01-01T00:00:00Z'}):
                materialize(base, submission, second)
            self.assertNotEqual(tree_digest(first), tree_digest(second))
            self.assertEqual(product_source_digest(first), product_source_digest(second))
            patch_path = submission/'solution.patch'; patch_path.write_text(patch_path.read_text().replace('+const x = 2;', '+const x = 3;'))
            materialize(base, submission, third)
            self.assertNotEqual(product_source_digest(first), product_source_digest(third))

    def test_only_owned_root_git_is_excluded_and_external_links_not_followed(self):
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw); product = root/'product'; product.mkdir()
            (product/'.git').mkdir(); (product/'.git/index').write_text('one')
            external = root/'external'; external.write_text('private one'); (product/'link').symlink_to(external)
            first = product_source_digest(product)
            (product/'.git/index').write_text('two'); external.write_text('private two')
            self.assertEqual(first, product_source_digest(product))
            (product/'nested/.git').mkdir(parents=True); (product/'nested/.git/source').write_text('public source')
            self.assertNotEqual(first, product_source_digest(product))

if __name__ == '__main__': unittest.main()
