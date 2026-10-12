"""Offline Git-state controls for the actual Aider repo-map cache location."""
import os
import sqlite3
import subprocess
import tempfile
import unittest
from pathlib import Path

from evaluator.harness.run_lower_agent_case import prepare_aider_repo_cache


class CacheIsolationTests(unittest.TestCase):
    def fixture(self, root):
        repo = root / 'repo'
        subprocess.run(['git', 'init', '-q', str(repo)], check=True)
        subprocess.run(['git', '-C', str(repo), 'config', 'user.name', 'fixture'], check=True)
        subprocess.run(['git', '-C', str(repo), 'config', 'user.email', 'fixture@example.invalid'], check=True)
        (repo / 'source.py').write_text('value = 1\n')
        subprocess.run(['git', '-C', str(repo), 'add', 'source.py'], check=True)
        subprocess.run(['git', '-C', str(repo), 'commit', '-qm', 'baseline'], check=True)
        return repo

    def status(self, repo):
        return subprocess.run(['git', '-C', str(repo), 'status', '--porcelain=v1', '--untracked-files=all'],
                              capture_output=True, text=True, check=True).stdout

    def write_cache(self, repo):
        with sqlite3.connect(repo / '.aider.tags.cache.v4' / 'cache.db') as db:
            db.execute('CREATE TABLE tags (name TEXT)')
            db.execute('INSERT INTO tags VALUES (?)', ('observed tag',))

    def test_actual_cache_bytes_are_outside_clean_repository(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            repo = self.fixture(root)
            before = (repo / '.git/info/exclude').read_bytes()
            receipt = prepare_aider_repo_cache(root)
            self.write_cache(repo)
            self.assertEqual(self.status(repo), '')
            self.assertEqual((repo / '.aider.tags.cache.v4/cache.db').resolve(),
                             root / 'home/.cache/aider-repomap-v4/cache.db')
            self.assertTrue((repo / '.git/info/exclude').read_bytes().startswith(before))
            self.assertTrue(receipt['git_status_unchanged'])
            self.assertFalse((repo / '.gitignore').exists())

    def test_foreign_dirty_and_nearby_cache_names_remain_visible(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            repo = self.fixture(root)
            (repo / 'source.py').write_text('foreign tracked change\n')
            (repo / 'dirty-untracked.txt').write_bytes(b'foreign bytes\x00')
            (repo / '.aider.tags.cache.v4.foreign').write_text('foreign\n')
            before = self.status(repo)
            prepare_aider_repo_cache(root)
            self.write_cache(repo)
            self.assertEqual(self.status(repo), before)
            self.assertIn('dirty-untracked.txt', before)
            self.assertIn('.aider.tags.cache.v4.foreign', before)
            self.assertIn('source.py', before)

    def test_preexisting_cache_is_never_adopted_or_overwritten(self):
        for kind in ('directory', 'file', 'symlink'):
            with self.subTest(kind=kind), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                repo = self.fixture(root)
                path = repo / '.aider.tags.cache.v4'
                if kind == 'directory':
                    path.mkdir()
                    (path / 'foreign').write_text('owned by another actor')
                elif kind == 'file':
                    path.write_text('owned by another actor')
                else:
                    path.symlink_to('../absent-foreign-path')
                before = self.status(repo)
                exclusions = (repo / '.git/info/exclude').read_bytes()
                with self.assertRaisesRegex(ValueError, 'preexisting'):
                    prepare_aider_repo_cache(root)
                self.assertEqual(self.status(repo), before)
                self.assertEqual((repo / '.git/info/exclude').read_bytes(), exclusions)
                self.assertFalse((root / 'home').exists())

    def test_exclusion_symlink_is_rejected_without_touching_target(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            repo = self.fixture(root)
            foreign = root / 'foreign-exclusions'
            foreign.write_text('foreign exclusion bytes\n')
            (repo / '.git/info/exclude').unlink()
            (repo / '.git/info/exclude').symlink_to(foreign)
            with self.assertRaisesRegex(ValueError, 'non-regular'):
                prepare_aider_repo_cache(root)
            self.assertEqual(foreign.read_text(), 'foreign exclusion bytes\n')
            self.assertFalse((repo / '.aider.tags.cache.v4').exists())


if __name__ == '__main__':
    unittest.main()
