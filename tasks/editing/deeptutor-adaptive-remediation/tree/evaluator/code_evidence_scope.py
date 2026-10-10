"""Evaluator-owned, baseline-locked Code context selection, never source editing.

Every changed path is mandatory. Stable task entrypoints and their statically
resolvable local imports supply context; a future edit outside that surface is
still included. The authoritative Create packer decides whether it fits.
"""
from __future__ import annotations

import ast
import hashlib
import json
import os
import runpy
import subprocess
import sys
import tempfile
from pathlib import Path

TASK_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(TASK_ROOT))
from agentloop.protocol import AUTHORITATIVE_SOURCE, AUTHORITATIVE_SOURCE_DIGEST, tree_digest

CREATE_PACKER = Path('@@AGENTSWE_EDITING_CONTROL@@/code_eval.py')
CONTEXT_ROOTS = (
    'deeptutor/learning', 'deeptutor/capabilities/mastery',
    'deeptutor/agents/chat', 'deeptutor/services/session',
    'deeptutor_cli/main.py', 'deeptutor_cli/common.py', 'deeptutor_cli/__main__.py', 'deeptutor_cli/__init__.py',
    'deeptutor/core', 'deeptutor/utils',
    'deeptutor/api/routers/mastery_path.py', 'deeptutor/services/path_service.py',
    'deeptutor/services/llm', 'deeptutor/config', 'deeptutor/tools/mastery_tool.py',
    'requirements', 'requirements.txt', 'pyproject.toml', 'AGENTS.md',
)

OPTIONAL_PREFIXES = {
    'deeptutor/services/rag/': 'RAG ingestion/retrieval is not the offline mastery/remediation task',
    'deeptutor/services/embedding/': 'Embedding is not part of the published adaptive-remediation edit mechanisms',
    'deeptutor/services/parsing/': 'Document ingestion/parsing is an unrelated optional capability',
    'deeptutor/services/memory/': 'Optional generic chat memory implementation, not LearningStore remediation state',
    'deeptutor/services/partners/': 'IM partner integration is unrelated to this CLI mastery task',
    'deeptutor/services/subagent/': 'An external replacement agent is prohibited; unchanged optional subagent service',
    'deeptutor/services/skill/': 'Optional skill installation is unrelated to mastery state transitions',
    'deeptutor/services/mcp/': 'The public adaptive-remediation interface requires no external MCP service',
    'deeptutor/services/codex_auth/': 'OAuth Codex provider is not the configured OpenAI-compatible medium lower',
    'deeptutor/services/search/': 'External/general search service is not the LearningStore event subscriber',
    'deeptutor/services/cli_apps/': 'Unrelated optional CLI app integrations',
    'deeptutor/services/sandbox/': 'Unchanged optional code-execution service; task-specific safety callsites remain visible',
    'deeptutor/services/voice/': 'No voice/audio input in the declared task',
    'deeptutor/services/cron/': 'External scheduling is excluded by the public runtime constraints',
    'deeptutor/book/': 'Book generation is a different capability registered by the generic CLI',
    'deeptutor/knowledge/': 'Knowledge ingestion/database subsystem is outside the complete published adaptive-remediation edit surface',
    'deeptutor/partners/': 'IM channel adapters are a different CLI capability',
    'deeptutor/tools/builtin/': 'Generic non-mastery builtin implementations; mastery tool registration is retained',
    'deeptutor/services/session/pocketbase_store.py': 'PocketBase is explicitly excluded by this task; SQLite session store is retained',
    'deeptutor/services/config/knowledge_base_config.py': 'Knowledge-base settings do not govern mastery persistence',
    'deeptutor/services/config/embedding_endpoint.py': 'No embedding endpoint is used by the offline mastery task',
    'deeptutor/utils/document_extractor.py': 'Unrelated document ingestion helper',
    'deeptutor/services/llm/provider_core/anthropic_provider.py': 'Unselected provider; the locked lower is OpenAI-compatible',
    'deeptutor/services/llm/provider_core/azure_openai_provider.py': 'Unselected Azure provider',
    'deeptutor/services/llm/provider_core/github_copilot_provider.py': 'Unselected Copilot provider',
    'deeptutor/services/llm/provider_core/openai_codex_provider.py': 'Unselected OAuth Codex provider',
    'deeptutor/services/llm/providers/anthropic.py': 'Unselected Anthropic provider',
    'deeptutor/services/llm/local_provider.py': 'No local/GPU model provider in the task',
}


def _optional_reason(path):
    for prefix, reason in OPTIONAL_PREFIXES.items():
        if path == prefix or path.startswith(prefix):
            return reason
    if path.startswith('deeptutor_cli/') and path not in CONTEXT_ROOTS:
        return 'Different CLI command registered by main.py; run mastery entry and common.py are retained'
    if path.startswith('deeptutor/capabilities/') and len(Path(path).parts) > 3 and not path.startswith('deeptutor/capabilities/mastery/'):
        return 'Different capability imported by generic registry; mastery capability and registry callsites are retained'
    if path.startswith('deeptutor/agents/') and len(Path(path).parts) > 3 and not path.startswith('deeptutor/agents/chat/'):
        return 'Different capability agent, not the mastery chat agent loop'
    if path.startswith('deeptutor/api/routers/') and path != 'deeptutor/api/routers/mastery_path.py':
        return 'Different API route; the public mastery route is retained'
    return None


def _inventory(root):
    values = {}
    for path in sorted(root.rglob('*')):
        rel = path.relative_to(root).as_posix()
        if any(part in {'__pycache__', '.pytest_cache'} for part in path.relative_to(root).parts):
            continue
        if path.is_symlink():
            values[rel] = {'kind': 'symlink', 'target': os.readlink(path)}
        elif path.is_file():
            values[rel] = {'kind': 'file', 'sha256': hashlib.sha256(path.read_bytes()).hexdigest(), 'bytes': path.stat().st_size}
    return values


def _module_candidates(module, inventory):
    raw = module.replace('.', '/')
    return {path for path in (raw + '.py', raw + '/__init__.py') if path in inventory}


def _dependencies(candidate, paths, inventory, mandatory=()):
    mandatory = set(mandatory)
    selected, pending, errors, exclusions = set(paths), list(paths), [], {}
    while pending:
        rel = pending.pop()
        if not rel.endswith('.py') or inventory[rel]['kind'] != 'file':
            continue
        try:
            node = ast.parse((candidate / rel).read_text(encoding='utf-8'), filename=rel)
        except (SyntaxError, UnicodeError) as exc:
            errors.append({'path': rel, 'error': type(exc).__name__})
            continue
        package = rel[:-3].split('/')[:-1]
        imported = set()
        for item in ast.walk(node):
            if isinstance(item, ast.Import):
                imported.update(alias.name for alias in item.names)
            elif isinstance(item, ast.ImportFrom):
                prefix = package[:len(package) - item.level + 1] if item.level else []
                name = '.'.join(prefix + ([item.module] if item.module else []))
                imported.add(name)
                imported.update(name + '.' + alias.name if name else alias.name for alias in item.names if alias.name != '*')
        found = set()
        for module in imported:
            found.update(_module_candidates(module, inventory))
            parts = module.split('.')
            for index in range(1, len(parts)):
                found.update(_module_candidates('.'.join(parts[:index]), inventory))
        for dependency in found - selected:
            reason = _optional_reason(dependency)
            if reason and dependency not in mandatory and rel not in mandatory:
                exclusions.setdefault(dependency, {'path': dependency, 'imported_by': [],
                    'reason': reason, 'sha256': inventory[dependency].get('sha256')})['imported_by'].append(rel)
                continue
            selected.add(dependency)
            pending.append(dependency)
    return selected, errors, [exclusions[path] for path in sorted(exclusions) if path not in selected]


def _verified_git_init_metadata(candidate, added, new, output_dir):
    """Recognize only exact trusted Git init bytes, never arbitrary .git edits."""
    metadata = [path for path in added if path.startswith('.git/')]
    if not metadata:
        return []
    with tempfile.TemporaryDirectory(prefix='git-init-proof-', dir=output_dir) as raw:
        root = Path(raw)
        subprocess.run(['git', '-c', 'init.defaultBranch=master', 'init', '-q',
            '--template=/usr/share/git-core/templates', str(root / 'repository')],
            env={'PATH': '/usr/bin:/bin', 'HOME': str(root), 'GIT_CONFIG_NOSYSTEM': '1'},
            check=True, capture_output=True, timeout=10)
        expected = _inventory(root / 'repository')
    return [path for path in metadata if new[path] == expected.get(path)]


def build_scope(candidate, digest, output_dir, *, baseline, expected_baseline_digest, packer):
    candidate, baseline, output_dir = candidate.resolve(), baseline.resolve(), output_dir.resolve()
    output_dir.mkdir(parents=True, exist_ok=True)
    observed_baseline = tree_digest(baseline)
    observed_candidate = tree_digest(candidate)
    manifest = {
        'schema_version': 'agentswe-edit-code-scope/v1',
        'candidate_digest': digest, 'candidate_observed_digest': observed_candidate,
        'baseline_path': str(baseline), 'baseline_expected_digest': expected_baseline_digest,
        'baseline_observed_digest': observed_baseline,
        'complete_change_coverage': False, 'valid': False,
        'errors': [], 'evidence_paths': [], 'estimated_pack_bytes': None,
        'selection_policy': 'all changes and their direct local imports, plus mastery mechanisms and recursive local imports with explicit unchanged optional-subsystem boundaries',
        'public_requirement_basis': {path.name: hashlib.sha256(path.read_bytes()).hexdigest()
            for path in sorted((TASK_ROOT / 'input').glob('*.md'))},
        'mechanism_scope': ['production mastery tool registration and agent loop', 'remediation claim/ack/leases/fencing',
            'review/retest lifecycle', 'learning events and subscriber checkpoints', 'session handoffs and policy registry',
            'snapshot attestation/restore', 'monotonic witness chain, proofs and read-only audit',
            'path isolation, deterministic as_of behavior, terminal artifact authorship and ordinary-call compatibility'],
        'scope_case_policy': 'Covers the complete public Edit requirements and all two-dev/six-hidden mechanisms; not selected for test_001 or a favorable score. No private expected answers enter Code context.',
        'result_artifact_policy': 'Generated agent_result.json is covered as a changed source-tree file, never evidence of Result quality.',
    }
    manifest['scope_basis'] = {key: manifest[key] for key in ('public_requirement_basis', 'mechanism_scope', 'scope_case_policy')}
    if observed_baseline != expected_baseline_digest:
        manifest['errors'].append('trusted_baseline_digest_mismatch')
    if observed_candidate != digest:
        manifest['errors'].append('frozen_candidate_digest_mismatch')
    if manifest['errors']:
        return _finish(output_dir, manifest)
    old, new = _inventory(baseline), _inventory(candidate)
    added, deleted = sorted(new.keys() - old.keys()), sorted(old.keys() - new.keys())
    modified = sorted(path for path in new.keys() & old.keys() if new[path] != old[path])
    generated_git = _verified_git_init_metadata(candidate, added, new, output_dir)
    mandatory = set(added + modified) - set(generated_git)
    seeds = mandatory | {path for path in new if not _optional_reason(path) and any(path == root or path.startswith(root + '/') for root in CONTEXT_ROOTS)}
    selected, ast_errors, optional_exclusions = _dependencies(candidate, seeds, new, mandatory)
    selected.update(generated_git)
    manifest.update(
        changed_paths=modified, added_paths=added, deleted_paths=deleted,
        dependency_paths=sorted(selected - seeds), context_paths=sorted(seeds - mandatory),
        evidence_paths=sorted(selected),
        excluded_unchanged_count=len(new.keys() - selected),
        changed_file_evidence={path: {'before': old.get(path), 'after': new.get(path)} for path in sorted(mandatory | set(deleted))},
        dependency_parse_failures=ast_errors,
        unchanged_optional_dependency_exclusions=optional_exclusions,
        dependency_resolution='Static local imports only; dynamic imports are not claimed complete. All new/modified files remain mandatory regardless of imports.',
        evaluator_generated_metadata_exclusions=[{'path': path, **new[path],
            'reason': 'exact bytes of trusted /usr/share/git-core/templates git init with default master; evaluator materializer creates this metadata'}
            for path in generated_git],
    )
    # A deletion cannot be cited as a frozen-source file. Keep evaluator-only
    # tombstones bound to both trees; the judge integration must supply them.
    tombstone = output_dir / 'code_deleted_paths.json'
    _write_immutable_json(tombstone, {'schema_version': 'agentswe-code-deletion-tombstones/v1',
        'candidate_digest': digest, 'baseline_digest': observed_baseline,
        'entries': [{'path': path, 'baseline': old[path], 'candidate_exists': False} for path in deleted]})
    manifest['deleted_paths_evidence'] = str(tombstone)
    manifest['deleted_paths_evidence_sha256'] = hashlib.sha256(tombstone.read_bytes()).hexdigest()
    manifest['deleted_paths_judge_context_required'] = bool(deleted)
    skip = set(packer['SKIP_PARTS'])
    omitted = sorted(path for path in selected if any(part in skip for part in Path(path).parts))
    manifest['create_skip_parts'] = sorted(skip)
    manifest['unchanged_dependency_pack_exclusions'] = [
        {'path': path, 'reason': 'authoritative_Create_SKIP_PARTS; unchanged verified baseline dependency', 'sha256': new[path].get('sha256')}
        for path in omitted if path not in mandatory and path not in generated_git]
    hidden_changes = sorted(mandatory & set(omitted))
    if hidden_changes:
        manifest['errors'].append({'changed_paths_skipped_by_authoritative_Create_packer': hidden_changes})
    try:
        source_manifest, pack = packer['source_manifest_and_pack'](
            candidate, max_pack_bytes=packer['MAX_SOURCE_PACK_BYTES'], evidence_paths=sorted(selected))
        included = {entry['path'] for entry in source_manifest['files'] if entry.get('included_in_evidence_pack')}
        missing = sorted(mandatory - included)
        manifest.update(estimated_pack_bytes=len(pack.encode('utf-8')),
            estimated_tokens_at_4_bytes_per_token=(len(pack.encode('utf-8')) + 3) // 4,
            estimated_tokens_note='Rough byte heuristic only, not tokenizer measurement or provider context-capacity approval.',
            evidence_pack_sha256=hashlib.sha256(pack.encode('utf-8')).hexdigest(),
            mandatory_included_paths=sorted(mandatory & included), mandatory_excluded_paths=missing,
            source_manifest_tree_digest=source_manifest['tree_digest'])
        if source_manifest['tree_digest'] != digest:
            manifest['errors'].append('authoritative_Create_source_manifest_digest_mismatch')
        if missing:
            manifest['errors'].append({'changed_content_not_in_judge_context': missing})
        manifest['complete_change_coverage'] = not missing
    except (OSError, ValueError) as exc:
        manifest['errors'].append({'source_pack_preflight': str(exc)})
    manifest['valid'] = not manifest['errors'] and manifest['complete_change_coverage']
    # Detect concurrent changes without changing either tree.
    if tree_digest(candidate) != digest or tree_digest(baseline) != expected_baseline_digest:
        manifest.update(valid=False, complete_change_coverage=False)
        manifest['errors'].append('source_changed_during_scope_preflight')
    return _finish(output_dir, manifest)


def _finish(output_dir, manifest):
    path = output_dir / 'code_evidence_scope.json'
    manifest['manifest_path'] = str(path)
    _write_immutable_json(path, manifest)
    return manifest


def _write_immutable_json(path, value):
    payload = (json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + '\n').encode('utf-8')
    if path.exists():
        if path.is_symlink() or path.read_bytes() != payload:
            raise ValueError('refusing to overwrite different Code scope evidence: ' + str(path))
        return
    with path.open('xb') as handle:
        handle.write(payload)


def prepare_code_evidence_scope(candidate, digest, output_dir):
    packer = runpy.run_path(str(CREATE_PACKER), run_name='agentswe_code_scope_packer')
    return build_scope(Path(candidate), digest, Path(output_dir), baseline=AUTHORITATIVE_SOURCE,
        expected_baseline_digest=AUTHORITATIVE_SOURCE_DIGEST, packer=packer)
