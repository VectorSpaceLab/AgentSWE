"""Read-only task-native oracle: observe actual edited docs and durable surfaces.

The oracle never invokes the solution workflow, creates a receipt, repairs a
publication, or substitutes an expected answer. Boolean comparisons are judge
evidence, not a field-presence point formula.
"""
from __future__ import annotations
import hashlib
import json
import re
from pathlib import Path

from .fixture_service import _load_spec
from . import surface_assertions


def _safe_file(root, relative):
    root = Path(root).resolve()
    relative = Path(relative)
    if relative.is_absolute() or '..' in relative.parts:
        return None
    path = root / relative
    if any(root.joinpath(*relative.parts[:index]).is_symlink() for index in range(1, len(relative.parts) + 1)):
        return None
    try:
        path.resolve().relative_to(root.resolve())
    except ValueError:
        return None
    return path if path.is_file() else None


def _read(root, relative):
    path = _safe_file(root, relative)
    if path is None or path.stat().st_size > 1_000_000:
        return None
    try:
        return path.read_text(encoding='utf-8')
    except (OSError, UnicodeError):
        return None


def _json(root, relative):
    text = _read(root, relative)
    try:
        value = json.loads(text) if text is not None else None
    except (ValueError, TypeError):
        return None
    return value if isinstance(value, dict) else None


def _sha(root, relative):
    path = _safe_file(root, relative)
    return hashlib.sha256(path.read_bytes()).hexdigest() if path else None


def _docs(root):
    values = {}
    consumed = 0
    for path in sorted((root / 'openwiki').rglob('*.md')):
        if len(values) >= 200 or consumed > 1_000_000:
            values['<observation_limit>'] = {'readable_regular_file': False,
                'reason': 'Candidate documentation exceeds bounded oracle capture', 'sha256': None, 'text': None}
            break
        rel = path.relative_to(root).as_posix()
        text = _read(root, rel)
        consumed += len(text.encode('utf-8')) if text else 0
        values[rel] = {'sha256': _sha(root, rel), 'text': text,
            'readable_regular_file': text is not None}
    return values


def compare(case_id, spec, initial, observed):
    initial, observed = Path(initial), Path(observed)
    before, after = _docs(initial), _docs(observed)
    checks = []
    def check(name, passed, expected, actual):
        checks.append({'id': name, 'passed': bool(passed), 'expected': expected, 'observed': actual})
    expected_pages = set(spec.get('expected_pages', []))
    changed_pages = {path for path in before.keys() | after.keys()
        if before.get(path, {}).get('sha256') != after.get(path, {}).get('sha256')}
    check('required_impacted_docs_changed', expected_pages <= changed_pages, sorted(expected_pages), sorted(changed_pages))
    for path in spec.get('unaffected_pages', []):
        check('unaffected_document:' + path, path in before and path in after and before[path]['sha256'] == after[path]['sha256'],
            before.get(path, {}).get('sha256'), after.get(path, {}).get('sha256'))
    for path, needles in spec.get('required_text', {}).items():
        text = after.get(path, {}).get('text') or ''
        check('required_facts:' + path, all(needle in text for needle in needles), needles,
            {needle: needle in text for needle in needles})
    for path, needles in spec.get('forbidden_text', {}).items():
        text = after.get(path, {}).get('text') or ''
        check('stale_facts_removed:' + path, path in after and not any(needle in text for needle in needles),
            {'must_be_absent': needles}, {needle: needle in text for needle in needles})
    corpus = '\n'.join(item.get('text') or '' for item in after.values())
    for token in spec.get('preserved_tokens', []):
        check('handwritten_preserved', token in corpus, token, token in corpus)
    for anchor in spec.get('preserved_anchors', []):
        check('anchor_preserved:' + anchor, bool(re.search(r'id=[\"\']' + re.escape(anchor) + r'[\"\']', corpus)), anchor,
            bool(re.search(r'id=[\"\']' + re.escape(anchor) + r'[\"\']', corpus)))
    report = _json(observed, 'impact-report.json')
    tx, publication, search = (spec.get(key, {}) for key in ('transaction', 'publication', 'search'))
    receipt_rel = str(Path(tx.get('state_dir', '.openwiki-impact')) / 'tenants' / str(tx.get('tenant_id', '')) / 'receipts' / (str(tx.get('request_id', '')) + '.json'))
    receipt = _json(observed, receipt_rel)
    transaction_expected = {key: tx.get(key) for key in ('tenant_id', 'request_id', 'generation', 'payload_digest')}
    check('durable_receipt_identity', isinstance(receipt, dict) and all(receipt.get(k) == v for k, v in transaction_expected.items()),
        transaction_expected, receipt)
    expected_receipt_hashes = {path: after.get(path, {}).get('sha256') for path in sorted(expected_pages)}
    check('durable_receipt_document_binding', isinstance(receipt, dict) and receipt.get('status') == 'committed'
        and receipt.get('changed_paths') == sorted(expected_pages)
        and receipt.get('documentation_hashes') == expected_receipt_hashes,
        {'status': 'committed', 'changed_paths': sorted(expected_pages), 'documentation_hashes': expected_receipt_hashes}, receipt)
    public_root = Path(publication.get('root', '.openwiki-public')) / 'sites' / str(publication.get('site_id', ''))
    release = public_root / 'releases' / str(publication.get('generation', ''))
    public_manifest = _json(observed, str(release / 'manifest.json'))
    public_active = _json(observed, str(public_root / 'active.json'))
    search_root = Path(search.get('root', '.openwiki-search')) / 'indexes' / str(search.get('index_id', ''))
    generation = search_root / 'generations' / str(search.get('generation', ''))
    search_manifest = _json(observed, str(generation / 'manifest.json'))
    search_active = _json(observed, str(search_root / 'active.json'))
    index = _json(observed, str(generation / 'index.json'))
    # Canonical corpus excludes product-reserved planning/log files, as required
    # by the original executable-publication/search interface.
    corpus_hashes = {path: item['sha256'] for path, item in after.items()
        if Path(path).name not in {'INSTRUCTIONS.md', '_plan.md', 'log.md'}}
    for kind, manifest, active, config, rel, id_key in (
        ('publication', public_manifest, public_active, publication, release, 'site_id'),
        ('search', search_manifest, search_active, search, generation, 'index_id')):
        identity = {id_key: config.get(id_key), 'generation': config.get('generation'), 'payload_digest': tx.get('payload_digest')}
        check(kind + '_identity', isinstance(manifest, dict) and isinstance(active, dict)
            and all(manifest.get(k) == v and active.get(k) == v for k, v in identity.items()), identity,
            {'manifest': manifest, 'active': active})
        check(kind + '_actual_docs_hashes', isinstance(manifest, dict) and manifest.get('documentation_hashes') == corpus_hashes,
            corpus_hashes, manifest.get('documentation_hashes') if manifest else None)
        check(kind + '_active_manifest_digest', isinstance(active, dict) and active.get('manifest_sha256') == _sha(observed, str(rel / 'manifest.json')),
            _sha(observed, str(rel / 'manifest.json')), active.get('manifest_sha256') if active else None)
    check('search_index_digest', isinstance(search_manifest, dict) and search_manifest.get('index_sha256') == _sha(observed, str(generation / 'index.json')),
        _sha(observed, str(generation / 'index.json')), search_manifest.get('index_sha256') if search_manifest else None)
    documents = index.get('documents') if isinstance(index, dict) else None
    check('search_document_corpus', isinstance(documents, list) and [item.get('path') for item in documents if isinstance(item, dict)] == sorted(corpus_hashes),
        sorted(corpus_hashes), documents)
    # Deterministic, read-only assertions over the product's own persisted bytes
    # (0920 hardening).  Additive: the legacy `semantic_comparisons` list above is
    # untouched.  An observation defect must never fail the case, so a failure here
    # degrades to an explicit note instead of raising.
    try:
        deterministic = surface_assertions.evaluate(case_id, spec, initial, observed)
    except Exception as exc:  # noqa: BLE001
        deterministic = {'schema_version': 'openwiki-deterministic-surface-assertions/v1',
            'case_id': case_id, 'assertions': [], 'assertion_results': {},
            'surface_families_satisfied': {}, 'surface_families_satisfied_count': 0,
            'surface_families_total': 0, 'complete_active_generation_observed': False,
            'observation_notes': ['%s: %s' % (type(exc).__name__, exc)]}
    return {'schema_version': 'openwiki-semantic-oracle-comparison/v1', 'case_id': case_id,
        'candidate_visible': False, 'private_oracle_not_candidate_visible': True,
        'semantic_comparisons': checks,
        'deterministic_surface_assertions': deterministic,
        'expected_task_facts': {key: spec.get(key) for key in ('scenario', 'preseed', 'preseed_surfaces', 'expected_pages',
            'direct_pages', 'unaffected_pages', 'required_text', 'forbidden_text', 'expected_examples',
            'expected_stale_kinds', 'search_queries', 'preserved_tokens', 'preserved_anchors')},
        'observed': {'documentation': after, 'impact_report': report, 'transaction_receipt': receipt,
            'publication_manifest': public_manifest, 'publication_active': public_active,
            'search_manifest': search_manifest, 'search_active': search_active, 'search_index': index},
        'observation_policy': 'Read-only physical file comparisons; no solution workflow or repair was performed by the oracle.',
        'independent_example_execution': False,
        'independent_search_query_execution': False,
        'verification_limits': ['Deterministic surface assertions are read-only comparisons of persisted product bytes; they establish what the product left behind, never that a command was executed.',
            'Example stdout and search query behavior require actual lower tool trajectory; artifact claims alone do not establish execution.',
            'Presence of IDs, JSON fields, broker calls or filenames does not earn semantic points.',
            'Final state alone does not prove concurrency/fencing/replay; use the actual raw product action evidence.']}


def observe(case_id, cases_root, initial, observed):
    return compare(case_id, _load_spec(case_id, Path(cases_root)), initial, observed)
