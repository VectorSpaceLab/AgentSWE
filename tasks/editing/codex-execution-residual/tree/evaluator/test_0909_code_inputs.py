"""Behavioral scope controls: unchanged boundaries never swallow edits."""
import tempfile
import unittest
from pathlib import Path

from evaluator.code_inputs import bounded_context_exclusions, inventory, immutable


class CodeScopeTests(unittest.TestCase):
    def worlds(self, root, files):
        baseline, source = root / 'baseline', root / 'source'
        for directory in (baseline, source):
            for name, value in files.items():
                path = directory / name
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text(value)
        return baseline, source

    def exclusions(self, baseline, source):
        old, new = inventory(baseline), inventory(source)
        mandatory = {path for path in new if old.get(path) != new[path]}
        return bounded_context_exclusions(source, baseline, old, new, set(new), mandatory)

    def test_unchanged_test_path_requires_actual_cfg_edge(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            baseline, source = self.worlds(root, {'src/main.rs': '#[cfg(test)]\n#[path = "main_tests.rs"]\nmod tests;\n',
                'src/main_tests.rs': 'unchanged tests', 'src/unreferenced_tests.rs': 'filename is not evidence'})
            excluded = self.exclusions(baseline, source)
            self.assertEqual(set(excluded), {'src/main_tests.rs'})
            self.assertEqual(excluded['src/main_tests.rs']['import_edges'][0]['owner'], 'src/main.rs')

    def test_modified_test_and_new_cfg_declaration_stay_in_pack(self):
        with tempfile.TemporaryDirectory() as directory:
            baseline, source = self.worlds(Path(directory), {'src/main.rs': 'fn main() {}\n',
                'src/main_tests.rs': 'unchanged'})
            (source / 'src/main.rs').write_text('#[cfg(test)]\n#[path="main_tests.rs"]\nmod tests;\n')
            self.assertEqual(self.exclusions(baseline, source), {})
            (baseline / 'src/main.rs').write_bytes((source / 'src/main.rs').read_bytes())
            (source / 'src/main_tests.rs').write_text('Candidate-authored test change')
            self.assertEqual(self.exclusions(baseline, source), {})

    def test_non_test_dependency_edge_prevents_test_exclusion(self):
        with tempfile.TemporaryDirectory() as directory:
            baseline, source = self.worlds(Path(directory), {'src/main.rs':
                '#[cfg(test)]\n#[path="main_tests.rs"]\nmod tests;\n#[path="main_tests.rs"]\nmod production;\n',
                'src/main_tests.rs': 'dual use'})
            self.assertEqual(self.exclusions(baseline, source), {})

    def test_all_test_unix_modules_are_recognized_without_stripping_source(self):
        with tempfile.TemporaryDirectory() as directory:
            baseline, source = self.worlds(Path(directory), {'src/main.rs':
                '#[cfg(all(test, unix))]\n#[path="main_tests.rs"]\nmod tests;\n', 'src/main_tests.rs': 'tests'})
            before = (source / 'src/main.rs').read_bytes()
            self.assertIn('src/main_tests.rs', self.exclusions(baseline, source))
            self.assertEqual((source / 'src/main.rs').read_bytes(), before)

    def test_changed_optional_doctor_file_is_mandatory(self):
        with tempfile.TemporaryDirectory() as directory:
            files = {'codex-rs/cli/src/main.rs': 'mod doctor;\nuse doctor::DoctorCommand;\ndoctor::run_doctor();\n',
                'codex-rs/cli/src/doctor.rs': 'unchanged doctor', 'codex-rs/cli/src/doctor/report.rs': 'report'}
            baseline, source = self.worlds(Path(directory), files)
            self.assertEqual(len(self.exclusions(baseline, source)), 2)
            (source / 'codex-rs/cli/src/doctor/report.rs').write_text('Candidate added dependency')
            self.assertEqual(self.exclusions(baseline, source), {})

    def test_new_doctor_execution_edge_prevents_optional_exclusion(self):
        with tempfile.TemporaryDirectory() as directory:
            files = {'codex-rs/cli/src/main.rs': 'mod doctor;\nuse doctor::DoctorCommand;\ndoctor::run_doctor();\n',
                'codex-rs/cli/src/doctor.rs': 'unchanged doctor'}
            baseline, source = self.worlds(Path(directory), files)
            (source / 'codex-rs/cli/src/main.rs').write_text(files['codex-rs/cli/src/main.rs'] + 'fn residual() {doctor::run_doctor();}\n')
            self.assertEqual(self.exclusions(baseline, source), {})

    def test_printed_doctor_hint_is_not_a_call_dependency(self):
        with tempfile.TemporaryDirectory() as directory:
            files = {'codex-rs/cli/src/main.rs': 'mod doctor;\nuse doctor::DoctorCommand;\ndoctor::run_doctor();\n',
                'codex-rs/cli/src/doctor.rs': 'unchanged doctor',
                'codex-rs/cli/src/recovery.rs': 'eprintln!("Run `codex doctor` for guidance.");\n'}
            baseline, source = self.worlds(Path(directory), files)
            self.assertIn('codex-rs/cli/src/doctor.rs', self.exclusions(baseline, source))

    def test_immutable_scope_does_not_overwrite_existing_or_symlink(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'scope.json'
            immutable(path, {'identity': 'a'})
            immutable(path, {'identity': 'a'})
            before = path.read_bytes()
            with self.assertRaisesRegex(ValueError, 'immutable'):
                immutable(path, {'identity': 'b'})
            self.assertEqual(path.read_bytes(), before)
            link = path.with_name('link.json')
            link.symlink_to(path)
            with self.assertRaisesRegex(ValueError, 'immutable'):
                immutable(link, {'identity': 'a'})


if __name__ == '__main__':
    unittest.main()
