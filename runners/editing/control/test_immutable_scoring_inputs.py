import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import formal_axes_shared as axes


class ImmutableScoringInputs(unittest.TestCase):
    def test_same_text_reuse_preserves_bytes_and_mtime(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / 'evidence.md'
            axes.write_immutable_text(path, 'original evidence\n')
            os.utime(path, ns=(1000000000, 1000000000))
            axes.write_immutable_text(path, 'original evidence\n')
            self.assertEqual(path.stat().st_mtime_ns, 1000000000)
            self.assertEqual(path.read_text(), 'original evidence\n')

    def test_different_text_fails_without_overwrite(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / 'evidence.md'
            axes.write_immutable_text(path, 'original evidence\n')
            with self.assertRaisesRegex(ValueError, 'new scoring directory'):
                axes.write_immutable_text(path, 'changed evaluator evidence\n')
            self.assertEqual(path.read_text(), 'original evidence\n')

    def test_equivalent_json_preserves_original_format_and_hash(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / 'scope.json'
            path.write_text('{"b":2,"a":1}')
            before = axes.sha256_file(path)
            axes.write_immutable_json(path, {'a': 1, 'b': 2})
            self.assertEqual(axes.sha256_file(path), before)
            with self.assertRaisesRegex(ValueError, 'new scoring directory'):
                axes.write_immutable_json(path, {'a': 3, 'b': 2})
            self.assertEqual(axes.sha256_file(path), before)

    def test_symlink_never_becomes_writable_evidence(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            target = root / 'unrelated'
            target.write_text('preserve')
            link = root / 'requirements.md'
            link.symlink_to(target)
            for writer, value in ((axes.write_immutable_text, 'new'), (axes.write_immutable_json, {'new': True})):
                with self.assertRaisesRegex(ValueError, 'symlink'):
                    writer(link, value)
            self.assertEqual(target.read_text(), 'preserve')

    def test_changed_public_requirements_do_not_replace_scored_inputs(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / 'input').mkdir()
            task = root / 'input/task.md'
            task.write_text('initial public task')
            destination = root / 'scoring/public_requirements'
            with patch.object(axes, 'ROOT', root):
                axes.public_requirements(destination)
                before = (destination / 'requirements.md').read_bytes()
                task.write_text('revised public task')
                with self.assertRaisesRegex(ValueError, 'new scoring directory'):
                    axes.public_requirements(destination)
            self.assertEqual((destination / 'requirements.md').read_bytes(), before)

    def test_missing_public_requirements_fail_before_creating_pack(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            with patch.object(axes, 'ROOT', root), self.assertRaisesRegex(ValueError, 'unavailable'):
                axes.public_requirements(root / 'output')
            self.assertFalse((root / 'output').exists())


if __name__ == '__main__':
    unittest.main()
