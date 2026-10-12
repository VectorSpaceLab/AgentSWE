"""Evaluator-private durable no-replay ledger keyed by materialized product."""
from __future__ import annotations
import hashlib
import json
import os
import re
from pathlib import Path


class ProductReplayBlocked(RuntimeError):
    def __init__(self, digest):
        super().__init__('this materialized product already has a public execution intent; inspect its retained outcome, do not execute it again')
        self.candidate_digest = digest


def durable_json(path: Path, value):
    """Exclusive immutable write, flushed before an external action can start."""
    data = (json.dumps(value, sort_keys=True, ensure_ascii=False, indent=2) + '\n').encode()
    with path.open('xb') as handle:
        handle.write(data); handle.flush(); os.fsync(handle.fileno())
    fd = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
    try: os.fsync(fd)
    finally: os.close(fd)


class ProductAttempts:
    def __init__(self, root: Path):
        self.root = root
        root.mkdir(parents=True, exist_ok=True)
        if root.is_symlink():
            raise RuntimeError('product attempt directory must not be a symlink')

    def directory(self, digest):
        if not isinstance(digest, str) or not re.fullmatch('[0-9a-f]{64}', digest):
            raise ValueError('invalid materialized product identity')
        return self.root / digest

    def lookup(self, digest):
        directory = self.directory(digest)
        if not directory.exists() and not directory.is_symlink():
            return None
        # Even a crash between mkdir and the intent fsync remains blocking.
        if directory.is_symlink() or not directory.is_dir():
            return {'state': 'unknown', 'candidate_digest': digest, 'case_results': []}
        value = {'state': 'unknown_or_in_progress', 'candidate_digest': digest, 'case_results': []}
        try:
            outcome = directory / 'outcome.json'
            if outcome.is_file() and not outcome.is_symlink():
                saved = json.loads(outcome.read_text())
                if saved['candidate_digest'] != digest:
                    raise ValueError('identity mismatch')
                value['state'] = saved['state']
                value['case_results'] = saved['record']['dev']
            else:
                for path in sorted(directory.glob('case_*.json')):
                    if path.is_symlink():
                        raise ValueError('case receipt symlink')
                    saved = json.loads(path.read_text())
                    if saved['candidate_digest'] != digest:
                        raise ValueError('case identity mismatch')
                    value['case_results'].append(saved['result'])
        except (ValueError, OSError, KeyError, TypeError):
            value.update(state='unknown_or_invalid_evidence', case_results=[])
        return value

    def claim(self, digest, identity):
        directory = self.directory(digest)
        try: directory.mkdir()
        except FileExistsError: raise ProductReplayBlocked(digest)
        fd = os.open(self.root, os.O_RDONLY | os.O_DIRECTORY)
        try: os.fsync(fd)
        finally: os.close(fd)
        durable_json(directory / 'intent.json', {'schema_version': 'agentswe-product-execution-intent/v1',
            'candidate_digest': digest, 'state': 'unknown_until_outcome', **identity})

    def record_case(self, digest, case_id, result, output):
        if not re.fullmatch(r'dev_00[12]', case_id):
            raise ValueError('invalid public case')
        references = []
        for path in sorted(output.rglob('*')):
            if path.is_file() and not path.is_symlink():
                references.append({'path': str(path), 'sha256': hashlib.sha256(path.read_bytes()).hexdigest()})
        durable_json(self.directory(digest) / ('case_' + case_id + '.json'), {
            'schema_version': 'agentswe-product-case-retained/v1', 'candidate_digest': digest,
            'case_id': case_id, 'result': result, 'source_files': references,
            'provider_total_usage': None, 'usage_source': 'unmodified original per-request evidence'})

    def finish(self, digest, record, evidence_root):
        durable_json(self.directory(digest) / 'outcome.json', {
            'schema_version': 'agentswe-product-execution-outcome/v1', 'candidate_digest': digest,
            'state': 'completed' if record.get('accepted') is True else 'infrastructure_invalid',
            'record': record, 'retained_evidence_root': str(evidence_root),
            'automatic_replay_allowed': False})
