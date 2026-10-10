"""Fail-closed provenance checks for the eventual Edit launch configuration.

These checks do not set readiness or authorize launch. They require recorded
sources and explicit task configuration evidence in addition to that gate.
"""
from __future__ import annotations
import ast
import hashlib
import json
from pathlib import Path

PROFILE_FIELDS = ('builder_agent', 'builder_model', 'builder_reasoning_effort',
                  'builder_harness_version', 'provider')
CREATE_FILES = ('suite_config.py', 'launch_two_track.py', 'run_branch.py', 'code_eval.py')


def digest(path: Path) -> str:
    if path.is_symlink() or not path.is_file():
        raise ValueError('Missing or symlink evidence: ' + str(path))
    return hashlib.sha256(path.read_bytes()).hexdigest()


def read_object(path: Path) -> dict:
    digest(path)
    value = json.loads(path.read_text())
    if not isinstance(value, dict):
        raise ValueError('Evidence must be a JSON object: ' + str(path))
    return value


def check_reference(ref: dict) -> dict:
    if not isinstance(ref, dict) or not isinstance(ref.get('path'), str):
        raise ValueError('Invalid evidence reference')
    path = Path(ref['path'])
    if not path.is_absolute() or digest(path) != ref.get('sha256'):
        raise ValueError('Evidence digest mismatch: ' + str(path))
    return {'path': str(path), 'sha256': ref['sha256']}


def literal_profiles(path: Path) -> dict:
    """Read the five scalar profile fields without executing source code."""
    tree = ast.parse(path.read_text())
    values = [node.value for node in tree.body if isinstance(node, ast.Assign)
              and any(isinstance(t, ast.Name) and t.id == 'PROFILES' for t in node.targets)]
    if len(values) != 1 or not isinstance(values[0], ast.Dict):
        raise ValueError('Create PROFILES must have one explicit dictionary')
    result = {}
    for key, value in zip(values[0].keys, values[0].values):
        name = ast.literal_eval(key)
        if not isinstance(value, ast.Dict) or name in result:
            raise ValueError('Invalid or duplicated Create profile')
        fields = {}
        for field, item in zip(value.keys, value.values):
            field = ast.literal_eval(field)
            if field in PROFILE_FIELDS:
                fields[field] = ast.literal_eval(item)
        if set(fields) != set(PROFILE_FIELDS):
            raise ValueError('Incomplete Create scalar profile: ' + str(name))
        result[name] = fields
    return result


def validate_alignment(snapshot_path: Path, selection_path: Path, profiles: dict,
                       create_root: Path) -> dict:
    snapshot = read_object(snapshot_path)
    source_refs = snapshot.get('source_files', [])
    expected_paths = {str(create_root / name) for name in CREATE_FILES}
    if len(source_refs) != 4 or {ref.get('path') for ref in source_refs} != expected_paths:
        raise ValueError('Alignment snapshot must bind exactly four authoritative Create files')
    for ref in source_refs:
        check_reference(ref)
    expected = {name: {field: value.get(field) for field in PROFILE_FIELDS}
                for name, value in profiles.items()}
    recorded = {name: {field: value.get(field) for field in PROFILE_FIELDS}
                for name, value in snapshot.get('profiles', {}).items()}
    if expected != recorded or expected != literal_profiles(create_root / 'suite_config.py'):
        raise ValueError('Current Create, snapshot, and Edit profile scalars do not agree')
    selection = read_object(selection_path)
    if selection.get('schema_version') != 'agentswe-create-selected-alignment-audit/v1':
        raise ValueError('Explicit selected-manifest provenance is required, not a directory scan')
    rows = selection.get('selected_records', [])
    if len(rows) != 40 or len({(row['task'], row['profile']) for row in rows}) != 40:
        raise ValueError('Selected historical provenance must contain forty unique task/profile pairs')
    references = selection.get('source_sha256', {})
    for path, expected_hash in references.items():
        check_reference({'path': path, 'sha256': expected_hash})
    manifests = {row['manifest'] for row in rows}
    required_manifests = {str(create_root / 'launch_manifests' / name) for name in (
        'effective-final20.json', 'effective-0826-gpt55-dpsk-pro-final20.json')}
    if manifests != required_manifests:
        raise ValueError('Historical selection does not use the two authorized effective manifests')
    selected_pairs = set()
    for manifest_path in manifests:
        if manifest_path not in references:
            raise ValueError('Selected manifest has no immutable hash reference')
        manifest = read_object(Path(manifest_path))
        branches = manifest.get('branches', [])
        if manifest.get('expected_branch_count') != 20 or len(branches) != 20:
            raise ValueError('Selected effective manifest is not twenty branches')
        for branch in branches:
            bundle = str(Path(branch['run_dir']) / 'dual_axis_score_bundle.json')
            selected_pairs.add((branch['task'], branch['profile'], bundle))
    if selected_pairs != {(row['task'], row['profile'], row['bundle']) for row in rows}:
        raise ValueError('Provenance rows do not match the actual selected branches')
    for row in rows:
        if references.get(row['bundle']) != row.get('bundle_sha256'):
            raise ValueError('Selected bundle does not have its bound hash')
        bundle = read_object(Path(row['bundle']))
        lifecycle = bundle.get('dev_lifecycle')
        if (bundle.get('task'), bundle.get('builder_profile')) != (row['task'], row['profile']):
            raise ValueError('Selected bundle identity mismatch')
        if not isinstance(lifecycle, list) or len(lifecycle) != row['accepted_rounds']:
            raise ValueError('Selected accepted-round count differs from actual lifecycle')
        if bundle.get('freeze', {}).get('reason') != row['freeze_reason']:
            raise ValueError('Selected freeze reason differs from actual bundle')
    return {'snapshot': {'path': str(snapshot_path), 'sha256': digest(snapshot_path)},
            'selected_manifest_provenance': {'path': str(selection_path), 'sha256': digest(selection_path)},
            'selected_count': 40, 'historical_score_validity_assessed': False,
            'current_profile_scalars_match': True}


def validate_configuration_registry(path: Path, tasks: dict) -> tuple[dict, list[str]]:
    registry = read_object(path)
    if registry.get('schema_version') != 'agentswe-edit-configuration-registry/v1':
        raise ValueError('Unsupported configuration delta registry')
    entries = registry.get('tasks', {})
    if set(entries) != set(tasks):
        raise ValueError('Configuration registry must explicitly cover every selected Edit task')
    errors = []
    for task, sibling in tasks.items():
        entry = entries[task]
        if entry.get('sibling') != str(sibling):
            errors.append(task + ': configuration sibling mismatch')
        if entry.get('status') != 'VERIFIED':
            errors.append(task + ': configuration evidence remains ' + str(entry.get('status')))
        reports, bindings = entry.get('reports', []), entry.get('effective_source_files', [])
        if not reports or not bindings:
            errors.append(task + ': missing reports or current deployment source bindings')
        for ref in (*reports, *bindings):
            try:
                check_reference(ref)
            except (ValueError, OSError) as exc:
                errors.append(task + ': ' + str(exc))
        deltas = entry.get('deltas')
        if not isinstance(deltas, list):
            errors.append(task + ': explicit delta list is missing')
        elif not deltas and not str(entry.get('no_difference_justification', '')).strip():
            errors.append(task + ': empty deltas do not prove Create alignment')
        elif deltas:
            seen = set()
            for delta in deltas:
                required = ('id', 'scope', 'before', 'after', 'rationale')
                if any(not str(delta.get(field, '')).strip() for field in required):
                    errors.append(task + ': incomplete configuration difference disclosure')
                if delta.get('id') in seen:
                    errors.append(task + ': duplicated configuration delta identity')
                seen.add(delta.get('id'))
        if entry.get('status') == 'VERIFIED' and entry.get('unresolved_issues'):
            errors.append(task + ': VERIFIED configuration still lists unresolved issues')
    return {'path': str(path), 'sha256': digest(path), 'tasks': entries,
            'verification_complete': not errors}, errors
