import copy
import json
from pathlib import Path
import tempfile
import unittest

import formal_provenance as checks


def write(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value))


def ref(path):
    return {'path': str(path), 'sha256': checks.digest(path)}


class FormalSourceContracts(unittest.TestCase):
    def alignment(self, root):
        profiles = {name: {'builder_agent': 'codex', 'builder_model': model,
                          'builder_reasoning_effort': 'max', 'builder_harness_version': '0.144.1',
                          'provider': 'gateway-responses'} for name, model in (
            ('codex_xhigh', 'deepseek-flash'), ('deepseek_max', 'test-model-2'),
            ('codex_gpt55_xhigh', 'gpt-5.5'), ('deepseek_pro_max', 'test-model-4'))}
        for name in checks.CREATE_FILES:
            (root / name).write_text('PROFILES = ' + repr(profiles) if name == 'suite_config.py' else '# fixture')
        snap = root / 'snapshot.json'
        write(snap, {'source_files': [ref(root / name) for name in checks.CREATE_FILES], 'profiles': profiles})
        rows, hashes = [], {}
        groups = ((('codex_xhigh', 'deepseek_max'), 'effective-final20.json'),
                  (('codex_gpt55_xhigh', 'deepseek_pro_max'), 'effective-0826-gpt55-dpsk-pro-final20.json'))
        for group, filename in groups:
            manifest_path = root / 'launch_manifests' / filename
            branches = []
            for profile in group:
                for n in range(10):
                    task = f'task-{n}'
                    run = root / 'runs' / profile / task
                    bundle = run / 'dual_axis_score_bundle.json'
                    write(bundle, {'task': task, 'builder_profile': profile,
                                   'dev_lifecycle': [{'round': 1}], 'freeze': {'reason': 'builder_exit'}})
                    branches.append({'task': task, 'profile': profile, 'run_dir': str(run)})
                    hashes[str(bundle)] = checks.digest(bundle)
                    rows.append({'manifest': str(manifest_path), 'task': task, 'profile': profile,
                                 'bundle': str(bundle), 'bundle_sha256': checks.digest(bundle),
                                 'accepted_rounds': 1, 'freeze_reason': 'builder_exit'})
            write(manifest_path, {'expected_branch_count': 20, 'branches': branches})
            hashes[str(manifest_path)] = checks.digest(manifest_path)
        selection = root / 'selection.json'
        write(selection, {'schema_version': 'agentswe-create-selected-alignment-audit/v1',
                          'source_sha256': hashes, 'selected_records': rows})
        return snap, selection, profiles

    def test_actual_bound_selected_manifests_pass(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp); snapshot, selection, profiles = self.alignment(root)
            result = checks.validate_alignment(snapshot, selection, profiles, root)
            self.assertEqual(result['selected_count'], 40)
            self.assertFalse(result['historical_score_validity_assessed'])

    def test_source_drift_and_wrong_profile_are_rejected(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp); snapshot, selection, profiles = self.alignment(root)
            altered = copy.deepcopy(profiles); altered['codex_xhigh']['builder_reasoning_effort'] = 'high'
            with self.assertRaisesRegex(ValueError, 'profile scalars'):
                checks.validate_alignment(snapshot, selection, altered, root)
            (root / 'run_branch.py').write_text('# changed')
            with self.assertRaisesRegex(ValueError, 'digest mismatch'):
                checks.validate_alignment(snapshot, selection, profiles, root)

    def test_directory_scan_is_not_selected_provenance(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp); snapshot, selection, profiles = self.alignment(root)
            write(selection, {'observed_dual_axis_bundles': {'count': 42}})
            with self.assertRaisesRegex(ValueError, 'selected-manifest'):
                checks.validate_alignment(snapshot, selection, profiles, root)

    def test_changed_bundle_and_invented_round_count_rejected(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp); snapshot, selection, profiles = self.alignment(root)
            record = json.loads(selection.read_text())
            record['selected_records'][0]['accepted_rounds'] = 10
            write(selection, record)
            with self.assertRaisesRegex(ValueError, 'round count'):
                checks.validate_alignment(snapshot, selection, profiles, root)
            bundle = Path(record['selected_records'][0]['bundle'])
            bundle.write_text('{}')
            with self.assertRaisesRegex(ValueError, 'digest mismatch'):
                checks.validate_alignment(snapshot, selection, profiles, root)

    def registry(self, root):
        evidence, source = root / 'evidence.json', root / 'task/runtime.py'
        write(evidence, {'fixture': 'unit only'}); source.parent.mkdir(); source.write_text('# runtime')
        entry = {'sibling': str(root / 'task'), 'status': 'VERIFIED', 'reports': [ref(evidence)],
                 'effective_source_files': [ref(source)], 'unresolved_issues': [],
                 'deltas': [{'id': 'isolation', 'scope': 'lower runtime', 'before': 'host network',
                             'after': 'network namespace and fixed relay', 'rationale': 'Keep evaluator services private'}]}
        path = root / 'registry.json'
        write(path, {'schema_version': 'agentswe-edit-configuration-registry/v1', 'tasks': {'task': entry}})
        return path, {'task': root / 'task'}, entry

    def test_explicit_bound_configuration_passes_but_does_not_set_readiness(self):
        with tempfile.TemporaryDirectory() as temp:
            path, tasks, _ = self.registry(Path(temp))
            result, errors = checks.validate_configuration_registry(path, tasks)
            self.assertEqual(errors, []); self.assertTrue(result['verification_complete'])
            self.assertNotIn('formal_ready', result)

    def test_empty_or_unfinished_configuration_cannot_claim_alignment(self):
        with tempfile.TemporaryDirectory() as temp:
            path, tasks, entry = self.registry(Path(temp))
            entry.update(status='REPAIR', deltas=[])
            write(path, {'schema_version': 'agentswe-edit-configuration-registry/v1', 'tasks': {'task': entry}})
            _, errors = checks.validate_configuration_registry(path, tasks)
            self.assertTrue(any('REPAIR' in message for message in errors))
            self.assertTrue(any('empty deltas' in message for message in errors))
            entry.update(status='VERIFIED', no_difference_justification='explicit evidence-backed claim', unresolved_issues=['not deployed'])
            write(path, {'schema_version': 'agentswe-edit-configuration-registry/v1', 'tasks': {'task': entry}})
            self.assertTrue(any('unresolved' in message for message in checks.validate_configuration_registry(path, tasks)[1]))

    def test_missing_task_evidence_and_changed_source_fail_closed(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp); path, tasks, entry = self.registry(root)
            with self.assertRaisesRegex(ValueError, 'every selected'):
                checks.validate_configuration_registry(path, {**tasks, 'missing': root / 'missing'})
            (root / 'task/runtime.py').write_text('# new runtime')
            self.assertTrue(any('digest mismatch' in e for e in checks.validate_configuration_registry(path, tasks)[1]))
            entry.update(reports=[], effective_source_files=[])
            write(path, {'schema_version': 'agentswe-edit-configuration-registry/v1', 'tasks': {'task': entry}})
            self.assertTrue(any('missing reports' in e for e in checks.validate_configuration_registry(path, tasks)[1]))

    def test_symlink_evidence_rejected(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp); source = root / 'source'; source.write_text('evidence')
            alias = root / 'alias'; alias.symlink_to(source)
            with self.assertRaisesRegex(ValueError, 'symlink'):
                checks.check_reference({'path': str(alias), 'sha256': checks.digest(source)})


if __name__ == '__main__':
    unittest.main()
