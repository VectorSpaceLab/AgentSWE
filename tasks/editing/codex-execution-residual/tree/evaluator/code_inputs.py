"""Code evaluates the baseline+patch product tree, never the delivery envelope."""
from __future__ import annotations

import hashlib
import json
import os
import re
import runpy
import shutil
import subprocess
import sys
import tempfile
from functools import lru_cache
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'harbor'))
from formal_one_stop import tree_digest as delivery_digest

CREATE = Path('@@AGENTSWE_EDITING_CONTROL@@/code_eval.py')
SHARED = Path('@@AGENTSWE_EDITING_CONTROL@@/formal_axes_shared.py')
# English release: control/code_eval.py tree_digest of the en-upstream a0 (fe01054a).
# Paper as-run (zh-asrun a0) value: 37bd1c05fd0e061c3524311889b3f86ec4fbbbbad9ede33f640d76ed69ab4c94
BASELINE_DIGEST = 'aba04760755fd0aed49fd282dc7780cc2c6fc15b55d36b0abb37da2c58cc2fc3'
CONTEXT_ROOTS = ('codex-rs/cli/src', 'codex-rs/exec/src', 'codex-rs/protocol/src',
    'codex-rs/core/src/tools', 'codex-rs/core/src/unified_exec', 'codex-rs/core/src/sandboxing',
    'codex-rs/core/src/exec.rs', 'codex-rs/core/src/exec_policy.rs',
    'codex-rs/core/src/session/mod.rs', 'codex-rs/core/src/session/state.rs',
    'codex-rs/core/src/lib.rs', 'codex-rs/core/Cargo.toml', 'codex-rs/cli/Cargo.toml',
    'codex-rs/exec/Cargo.toml', 'codex-rs/protocol/Cargo.toml', 'codex-rs/Cargo.toml',
    'codex-rs/Cargo.lock', 'README.md', 'AGENTS.md')


def bounded_context_exclusions(source, baseline, old, new, seeds, mandatory):
    """Exclude only baseline-identical optional context with explicit edges.

    This is not a Rust compiler dependency graph. It recognizes unambiguous
    cfg(test) path modules and the separately disclosed doctor CLI branch.
    Changes always override exclusions; in-file test blocks are never edited.
    """
    declarations = re.compile(r'(?P<attrs>(?:\s*#\[[^\]]+\]\s*)+)(?:pub(?:\([^)]*\))?\s+)?mod\s+\w+\s*;')
    edges, production_edges = {}, set()
    for owner in sorted(seeds):
        if not owner.endswith('.rs') or new[owner].get('kind') != 'file':
            continue
        text = (source / owner).read_text()
        before = (baseline / owner).read_text() if owner in old and old[owner].get('kind') == 'file' else ''
        for declaration in declarations.finditer(text):
            attrs = declaration.group('attrs')
            linked = re.search(r'#\[path\s*=\s*"([^"\\]+)"\]', attrs)
            if not linked or '..' in Path(linked.group(1)).parts:
                continue
            target = (Path(owner).parent / linked.group(1)).as_posix()
            if target not in seeds:
                continue
            cfg_test = bool(re.search(r'#\[cfg\(\s*test\s*\)\]', attrs)
                or re.search(r'#\[cfg\(\s*all\(\s*test\s*,[^\]]*\)\)\]', attrs))
            if not cfg_test:
                production_edges.add(target)
            elif declaration.group().strip() in before and target.endswith('_tests.rs'):
                edges.setdefault(target, []).append({'owner': owner,
                    'owner_sha256': new[owner]['sha256'],
                    'declaration': declaration.group().strip(),
                    'line': text[:declaration.start()].count('\n') + 1})
    exclusions = {}
    for path, references in edges.items():
        if path not in mandatory and path not in production_edges and old.get(path) == new[path]:
            exclusions[path] = {'kind': 'unchanged_cfg_test_module', 'import_edges': references,
                'reason': 'Independent cfg(test) module is baseline-identical; production module including its import remains packed. Any Candidate change forces inclusion.'}
    main = 'codex-rs/cli/src/main.rs'
    if main in seeds:
        main_text = (source / main).read_text()
        before = (baseline / main).read_text()
        markers = ('mod doctor;', 'use doctor::DoctorCommand;', 'doctor::run_doctor(')
        family = {path for path in seeds if path == 'codex-rs/cli/src/doctor.rs'
                  or path.startswith('codex-rs/cli/src/doctor/')}
        doctor_word = re.compile(r'\b(?:doctor|Doctor|DoctorCommand)\b')
        refs = {}
        for path in seeds - family:
            if path.endswith('.rs') and new[path].get('kind') == 'file':
                # A printed "run codex doctor" hint is not a call dependency.
                # Preserve source lines in evidence but scan Rust code tokens
                # outside ordinary/raw string literals and comments.
                original = (source / path).read_text()
                masked = re.sub(r'r(#{0,16})".*?"\1|"(?:\\.|[^"\\])*"|//[^\n]*|/\*.*?\*/',
                    lambda found: '\n' * found.group().count('\n'), original, flags=re.S)
                code_lines, original_lines = masked.splitlines(), original.splitlines()
                matches = [{'line': number, 'text': original_lines[number - 1].strip()} for number, line in
                    enumerate(code_lines, 1) if doctor_word.search(line)]
                if matches:
                    refs[path] = matches
        unchanged_main_refs = ([line.strip() for line in main_text.splitlines() if doctor_word.search(line)]
            == [line.strip() for line in before.splitlines() if doctor_word.search(line)])
        if (all(marker in main_text and marker in before for marker in markers)
                and unchanged_main_refs and set(refs) <= {main} and not (family & mandatory)):
            edge = {'owner': main, 'owner_sha256': new[main]['sha256'],
                'retained_declaration_and_call_markers': list(markers),
                'production_family_reference_audit': refs,
                'all_doctor_references_in_retained_main_equal_baseline': True,
                'boundary': 'Separate Doctor CLI subcommand, not the execution-residual API or execution runtime; full changed CLI dispatch is retained.'}
            for path in family:
                if path not in mandatory and old.get(path) == new[path]:
                    exclusions[path] = {'kind': 'unchanged_optional_cli_branch', 'import_edges': [edge],
                        'reason': 'Baseline-identical Doctor diagnostics branch; omitted as a declared unchanged task-irrelevant branch, not claimed to be absent from the CLI dependency graph.'}
    return exclusions


@lru_cache(maxsize=1)
def packer():
    return runpy.run_path(str(CREATE), run_name='codex_code_packer')


def sha(path):
    digest = hashlib.sha256()
    with path.open('rb') as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b''):
            digest.update(chunk)
    return digest.hexdigest()


def immutable(path, value):
    data = (json.dumps(value, sort_keys=True, indent=2) + '\n').encode()
    if path.is_symlink() or (path.exists() and path.read_bytes() != data):
        raise ValueError('immutable Code inputs changed: ' + str(path))
    if not path.exists():
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open('xb') as stream:
            stream.write(data)


def inventory(root):
    result = {}
    for path in sorted(root.rglob('*')):
        rel = path.relative_to(root).as_posix()
        if path.is_symlink():
            result[rel] = {'kind': 'symlink', 'target': os.readlink(path)}
        elif path.is_file():
            result[rel] = {'kind': 'file', 'sha256': sha(path), 'bytes': path.stat().st_size}
    return result


def materialize_product(run_dir, freeze):
    delivery = Path(freeze['path']).resolve()
    if delivery_digest(delivery) != freeze['candidate_digest']:
        raise ValueError('delivery changed before materialized Code input')
    if packer()['tree_digest'](ROOT / 'input/repository') != BASELINE_DIGEST:
        raise ValueError('trusted Code baseline changed')
    directory = run_dir / 'formal_scoring/code_inputs'
    source, binding_path = directory / 'frozen_product_source', directory / 'materialized_binding.json'
    if binding_path.exists():
        binding = json.loads(binding_path.read_text())
        if binding['delivery_digest'] != freeze['candidate_digest'] or packer()['tree_digest'](source) != binding['product_digest']:
            raise ValueError('materialized Code source identity changed')
        return source, binding
    if source.exists():
        raise ValueError('incomplete prior Code source preparation; choose a new scoring directory')
    binary = Path(freeze['binary']).resolve() if freeze.get('binary') else None
    if binary:
        proof_path = binary.parent.parent / 'build_result.json'
    else:
        possible = [run_dir / 'candidate_build/build_result.json',
                    *run_dir.glob('evaluations/submission_*/build/build_result.json')]
        bound = [path for path in possible if path.is_file() and json.loads(path.read_text()).get('candidate_digest') == freeze['candidate_digest']]
        if len(bound) != 1:
            raise ValueError('no unique build receipt for frozen delivery')
        proof_path = bound[0]
    proof = json.loads(proof_path.read_text())
    if proof.get('candidate_digest') != freeze['candidate_digest']:
        raise ValueError('Code build receipt is not bound to the delivery')
    built = proof_path.parent / 'worktree'
    reuse = proof.get('prebuilt_reuse')
    if reuse:
        old = Path(reuse['build_run']) / 'candidate_build'
        if sha(old / 'build_result.json') != reuse['proof_sha256']:
            raise ValueError('prebuilt source receipt changed')
        built = old / 'worktree'
    directory.mkdir(parents=True, exist_ok=True)
    shutil.copytree(ROOT / 'input/repository', source, symlinks=True)
    env = {'PATH': '/usr/bin:/bin', 'HOME': str(directory), 'GIT_CONFIG_NOSYSTEM': '1', 'LC_ALL': 'C.UTF-8'}
    for command in (['git', '-c', 'init.defaultBranch=master', 'init', '-q', '--template=/usr/share/git-core/templates'],
                    ['git', 'apply', '--check', str(delivery / 'solution.patch')],
                    ['git', 'apply', str(delivery / 'solution.patch')]):
        done = subprocess.run(command, cwd=source, env=env, capture_output=True, text=True, timeout=60)
        if done.returncode:
            raise ValueError('delivery cannot be materialized for independent Code review: ' + done.stderr[-500:])
    reconstructed, observed = inventory(source), inventory(built)
    lock = 'codex-rs/Cargo.lock'
    if {key: value for key, value in reconstructed.items() if key != lock} != {key: value for key, value in observed.items() if key != lock}:
        raise ValueError('actual compiled worktree differs from baseline plus delivery patch beyond Cargo.lock')
    if binary:
        build_binding = proof.get('immutable_build_binding') or {}
        if (sha(binary) != build_binding.get('binary_sha256')
                or sha(built / lock) != build_binding.get('cargo_lock_sha256')):
            raise ValueError('compiled product/binary/lock receipt mismatch')
    if reconstructed.get(lock) != observed.get(lock):
        shutil.copyfile(built / lock, source / lock)
    product = packer()['tree_digest'](source)
    if product != packer()['tree_digest'](built):
        raise ValueError('reconstructed Code product does not equal actual compiled source')
    for path in [source, *source.rglob('*')]:
        if not path.is_symlink():
            path.chmod(path.stat().st_mode & ~0o222)
    binding = {'delivery': str(delivery), 'delivery_digest': freeze['candidate_digest'],
        'product_source': str(source), 'product_digest': product,
        'actual_build_source': str(built), 'build_receipt': str(proof_path), 'build_receipt_sha256': sha(proof_path),
        'binary_sha256': sha(binary) if binary else None, 'baseline_Create_digest': BASELINE_DIGEST,
        'materialization': 'trusted baseline plus exact delivery patch; only receipt-bound Cargo.lock generation allowed',
        'candidate_code_executed_in_preparation': False, 'provider_calls': 0}
    immutable(binding_path, binding)
    return source, binding


def prepare_code_inputs(run_dir, freeze):
    source, binding = materialize_product(run_dir, freeze)
    full = binding['product_digest']
    baseline = ROOT / 'input/repository'
    old, new = inventory(baseline), inventory(source)
    added, deleted = sorted(new.keys() - old.keys()), sorted(old.keys() - new.keys())
    changed = sorted(path for path in new.keys() & old.keys() if new[path] != old[path])
    # Every .git addition has already been byte-compared to trusted git init
    # during reconstruction. Such evaluator metadata is not Candidate source.
    generated = [path for path in added if path.startswith('.git/')]
    mandatory = set(added + changed) - set(generated)
    seeds = mandatory | {path for path in new if any(path == root or path.startswith(root + '/') for root in CONTEXT_ROOTS)}
    bounded_exclusions = bounded_context_exclusions(source, baseline, old, new, seeds, mandatory)
    seeds -= set(bounded_exclusions)
    manifest, pack = packer()['source_manifest_and_pack'](source,
        max_pack_bytes=packer()['MAX_SOURCE_PACK_BYTES'], evidence_paths=sorted(seeds))
    included = {entry['path'] for entry in manifest['files'] if entry.get('included_in_evidence_pack')}
    missing = sorted(mandatory - included)
    output = run_dir / 'formal_scoring/code_inputs'
    excluded = {path: new[path] for path in sorted(new.keys() - seeds - set(generated))}
    exclusion_path = output / 'unchanged_exclusion_inventory.json'
    immutable(exclusion_path, {'baseline_digest': BASELINE_DIGEST, 'candidate_digest': full,
        'every_entry_equals_baseline': all(old.get(path) == item for path, item in excluded.items()),
        'entries': excluded})
    groups = {}
    for path, item in excluded.items():
        prefix = '/'.join(Path(path).parts[:2]) if len(Path(path).parts) > 2 else Path(path).parts[0]
        groups.setdefault(prefix, {})[path] = item
    scope = {'schema_version': 'agentswe-edit-code-scope/v1', 'candidate_digest': full,
        'baseline_expected_digest': BASELINE_DIGEST, 'baseline_observed_digest': packer()['tree_digest'](baseline),
        'complete_change_coverage': not missing, 'valid': not missing and manifest['tree_digest'] == full,
        'errors': [{'changed_content_not_in_context': missing}] if missing else [],
        'evidence_paths': sorted(seeds), 'added_paths': [path for path in added if path not in generated],
        'changed_paths': changed, 'deleted_paths': deleted, 'mandatory_included_paths': sorted(mandatory & included),
        'mandatory_excluded_paths': missing, 'estimated_pack_bytes': len(pack.encode()),
        'source_manifest_tree_digest': manifest['tree_digest'], 'evidence_pack_sha256': hashlib.sha256(pack.encode()).hexdigest(),
        'selection_policy': 'All Candidate changes and Cargo.lock plus native CLI, execution, tool dispatch, unified exec, protocol and sandbox module families, excluding only disclosed unchanged cfg(test) modules and the separate Doctor branch; no source truncation or case-specific subset.',
        'scope_basis': {'context_roots': CONTEXT_ROOTS, 'public_requirements': {p.name: sha(p) for p in sorted((ROOT / 'input').glob('*.md'))},
            'retained_public_mechanism_context': ['receipt identity', 'execution residual identity/store/export', 'bounded head/tail and output digest',
                'native shell and unified-exec lifecycle', 'tool context/session dispatch', 'CLI query/inspect plumbing', 'protocol compatibility and safety'],
            'excluded_inventory': {'path': str(exclusion_path), 'sha256': sha(exclusion_path),
                'count': len(excluded), 'all_excluded_bytes_equal_pinned_baseline': True,
                'reason_for_grouping': 'Group unchanged source context, not Candidate changes, to keep public Code context within Create input limits. Exact per-file hashes remain in immutable inventory and complete source manifest.'},
            'dependency_scope_limit': 'Task-wide native module families with explicit unchanged cfg(test)/Doctor boundaries, not a claim of whole-repository Rust dependency closure. Every future change remains mandatory.'},
        'unchanged_optional_dependency_exclusions': [{'path': path, **new[path], **details}
            for path, details in sorted(bounded_exclusions.items())],
        'evaluator_generated_metadata_exclusions': [{'path': path, **new[path],
            'reason': 'exact reconstructed trusted git-init bytes, outside patch; actual compiled tree matched'} for path in generated],
        'unchanged_dependency_pack_exclusions': [{'path_prefix': prefix, 'path_count': len(entries),
            'inventory_sha256': hashlib.sha256(json.dumps(entries, sort_keys=True).encode()).hexdigest(),
            'reason': 'all grouped bytes equal pinned baseline; outside task-native mechanism context or separately declared cfg(test)/Doctor boundary; no added/changed file excluded'}
            for prefix, entries in sorted(groups.items())],
        'changed_file_evidence': {path: {'before': old.get(path), 'after': new.get(path)} for path in sorted(mandatory)},
        'deleted_file_evidence': {path: old[path] for path in deleted}}
    scope['manifest_path'] = str(output / 'code_evidence_scope.json')
    immutable(Path(scope['manifest_path']), scope)
    shared = runpy.run_path(str(SHARED), run_name='codex_code_scope_validator')
    paths = shared['checked_code_scope'](scope, source, full)
    requirements = run_dir / 'formal_scoring/code_public_requirements'
    requirements.mkdir(parents=True, exist_ok=True)
    public = {p.name: p.read_bytes() for p in sorted((ROOT / 'input').glob('*.md'))}
    public['90_evaluator_code_scope_context.md'] = shared['code_scope_context_text'](scope).encode()
    for name, data in public.items():
        target = requirements / name
        if target.is_symlink() or (target.exists() and target.read_bytes() != data):
            raise ValueError('Code public requirements changed in this run')
        if not target.exists():
            with target.open('xb') as stream:
                stream.write(data)
    immutable(output / 'code_source_binding.json', {**binding,
        'public_requirements_digest': packer()['tree_digest'](requirements),
        'Create_implementation_sha256': sha(CREATE), 'native_digest_implementation_sha256': sha(ROOT / 'harbor/formal_one_stop.py'),
        'Code_input_adapter_sha256': sha(Path(__file__)), 'scope': scope['manifest_path']})
    return source, requirements, full, paths
