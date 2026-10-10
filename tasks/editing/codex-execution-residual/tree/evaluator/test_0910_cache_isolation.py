"""Provider-free tests for Candidate cache contamination and build attribution."""
import importlib.util
import json
import os
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location('codex_0910_runner', ROOT / 'harbor/formal_one_stop.py')
runner = importlib.util.module_from_spec(spec)
spec.loader.exec_module(runner)
from build_preflight import (BuildInfrastructureError, private_candidate_target,
                             isolated_build_command, infrastructure_build_error)


class CacheIsolationTests(unittest.TestCase):
    def test_candidate_cannot_contaminate_baseline_or_next_delivery(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            baseline = root / 'baseline-target'
            baseline.mkdir()
            (baseline / 'dependency').write_text('trusted')
            first = private_candidate_target(baseline, root / 'first')
            (first / 'dependency').write_text('poison')
            (first / 'foreign-receipt').write_text('Candidate one')
            second = private_candidate_target(baseline, root / 'second')
            self.assertEqual((baseline / 'dependency').read_text(), 'trusted')
            self.assertEqual((second / 'dependency').read_text(), 'trusted')
            self.assertFalse((second / 'foreign-receipt').exists())
            self.assertNotEqual((first / 'dependency').stat().st_ino, (baseline / 'dependency').stat().st_ino)
            with self.assertRaisesRegex(BuildInfrastructureError, 'must be new'):
                private_candidate_target(baseline, root / 'first')

    def test_actual_namespace_exposes_only_current_candidate_cache(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            baseline = root / 'baseline-target'
            baseline.mkdir()
            (baseline / 'dependency').write_text('trusted')
            first = private_candidate_target(baseline, root / 'first')
            (first / 'dependency').write_text('poison')
            target = private_candidate_target(baseline, root / 'second')
            runtime, source = root / 'runtime', root / 'source'
            (runtime / 'cargo-home').mkdir(parents=True)
            (source / 'codex-rs').mkdir(parents=True)
            program = ('from pathlib import Path; import os; '
                       f'assert not Path({str(first)!r}).exists(); '
                       f'assert not Path({str(baseline)!r}).exists(); '
                       f'assert (Path({str(target)!r}) / "dependency").read_text() == "trusted"; '
                       f'(Path({str(target)!r}) / "dependency").write_text("changed"); '
                       'assert "DEEPSEEK_API_KEY" not in os.environ; print("isolated")')
            command = isolated_build_command(['/usr/bin/python3', '-I', '-c', program],
                runtime=runtime, target=target, worktree=source)
            result = subprocess.run(command, capture_output=True, text=True, timeout=30)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(result.stdout.strip(), 'isolated')
            self.assertEqual((baseline / 'dependency').read_text(), 'trusted')
            self.assertEqual((first / 'dependency').read_text(), 'poison')

    def test_unknown_compile_summary_and_kill_are_not_candidate_zero(self):
        self.assertTrue(infrastructure_build_error('cargo_build', 'error: could not compile `codex-core` (lib)'))
        self.assertTrue(infrastructure_build_error('cargo_build', 'error: could not compile `codex-core`\n(signal: 9, SIGKILL: kill)'))
        self.assertFalse(infrastructure_build_error('cargo_build', 'error[E0277]: trait bound is not satisfied\nerror: could not compile `codex-core`'))


if __name__ == '__main__':
    unittest.main()
