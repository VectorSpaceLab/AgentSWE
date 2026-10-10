"""Durable no-replay admission before the unchanged modern lower transport."""
import copy
import fcntl
import hashlib
import json
import os
import re
from pathlib import Path
try:
    from agentloop.lower_request_identity import canonical, digest, exclusive_private, private_file, require, verify_request
except ModuleNotFoundError:
    from lower_request_identity import canonical, digest, exclusive_private, private_file, require, verify_request


class ReplayForbidden(ValueError):
    pass


EMPTY_HISTORY = {'schema_version': 'agentswe-ai-lower-history-seed/v1', 'attempts': [], 'blocked_products': []}


def valid_hex(value, count):
    return isinstance(value, str) and re.fullmatch('[0-9a-f]{%d}' % count, value) is not None


def validate_history(value):
    require(isinstance(value, dict) and value.get('schema_version') == EMPTY_HISTORY['schema_version'], 'invalid lower history seed')
    attempts = value.get('attempts')
    products = value.get('blocked_products')
    require(isinstance(attempts, list) and all(isinstance(e, dict) for e in attempts), 'invalid historical attempts')
    require(isinstance(products, list) and all(valid_hex(v, 64) for v in products) and len(products) == len(set(products)), 'invalid historical product block list')
    for event in attempts:
        require(valid_hex(event.get('request_id'), 32) and valid_hex(event.get('payload_sha256'), 64), 'invalid historical identity')
        require(event.get('state') == 'terminal', 'historical in-flight request needs terminal audit before migration')
    require(len({e['request_id'] for e in attempts}) == len(attempts), 'duplicate historical request identity')
    if not attempts:
        require(value == EMPTY_HISTORY, 'empty history cannot carry product blocks or a forged receipt')
        return value
    require(set(value) == {'schema_version', 'attempts', 'blocked_products', 'source_receipt', 'source_receipt_sha256'}, 'invalid seeded history fields')
    receipt = value['source_receipt']
    require(isinstance(receipt, dict) and set(receipt) == {'schema_version', 'production_source_sha256', 'source_artifacts', 'mapping'}, 'invalid historical source receipt')
    require(receipt['schema_version'] == 'agentswe-ai-lower-history-sources/v1' and valid_hex(receipt['production_source_sha256'], 64), 'invalid source receipt identity')
    require(valid_hex(value['source_receipt_sha256'], 64) and digest(canonical(receipt)) == value['source_receipt_sha256'], 'historical source receipt hash mismatch')
    artifacts = receipt['source_artifacts']
    require(isinstance(artifacts, list) and artifacts, 'historical source artifacts missing')
    for row in artifacts:
        require(isinstance(row, dict) and set(row) == {'path', 'sha256'}, 'invalid historical source artifact')
        require(isinstance(row['path'], str) and Path(row['path']).is_absolute() and '..' not in Path(row['path']).parts and valid_hex(row['sha256'], 64), 'invalid historical source artifact identity')
    require(len({row['path'] for row in artifacts}) == len(artifacts), 'duplicate historical source artifact')
    artifact_hashes = {row['sha256'] for row in artifacts}
    mapping = receipt['mapping']
    require(isinstance(mapping, list) and len(mapping) == len(attempts), 'historical request mapping length mismatch')
    for event, row in zip(attempts, mapping):
        require(isinstance(row, dict) and set(row) == {'request_id', 'payload_sha256', 'product_source_digest', 'case_id', 'before_sha256', 'after_sha256'}, 'invalid historical request mapping')
        require(row['request_id'] == event['request_id'] and row['payload_sha256'] == event['payload_sha256'], 'historical mapping order or identity mismatch')
        require(valid_hex(row['product_source_digest'], 64) and row['product_source_digest'] in products, 'historical mapping product mismatch')
        require(isinstance(row['case_id'], str) and re.fullmatch('(?:dev_00[12]|test_00[1-6])', row['case_id']), 'invalid historical mapping case')
        require(all(valid_hex(row[k], 64) and row[k] in artifact_hashes for k in ('before_sha256', 'after_sha256')), 'historical before/after source binding missing')
    require({row['product_source_digest'] for row in mapping} == set(products), 'historical product coverage mismatch')
    return value


def load_history(path, expected_sha256=None, *, verify_sources=False):
    if path is None:
        require(expected_sha256 is None, 'seed digest without history seed')
        return copy.deepcopy(EMPTY_HISTORY)
    require(valid_hex(expected_sha256, 64), 'trusted launcher must supply the bound history seed digest')
    raw = Path(path).read_bytes()
    require(digest(raw) == expected_sha256, 'history seed differs from trusted launcher binding')
    value = validate_history(json.loads(raw))
    if verify_sources:
        for artifact in value.get('source_receipt', {}).get('source_artifacts', []):
            require(digest(Path(artifact['path']).read_bytes()) == artifact['sha256'], 'historical source artifact changed')
    return value


class RequestReserve:
    def __init__(self, root, key, history=None):
        require(isinstance(key, bytes) and len(key) == 32, 'invalid evaluator context signing key')
        self.root = Path(root)
        require(self.root.is_absolute() and self.root.absolute() == self.root.resolve(), 'symlinked or relative request reserve ancestry')
        require(not self.root.is_symlink(), 'symlinked request reserve')
        self.root.mkdir(mode=0o700, parents=True, exist_ok=True)
        require(self.root.stat().st_uid == os.geteuid() and self.root.stat().st_mode & 0o077 == 0, 'request reserve must be private evaluator directory')
        self.key = key
        self.history = copy.deepcopy(validate_history(history if history is not None else load_history(None)))
        self.old_ids = {e['request_id'] for e in self.history['attempts']}
        self.old_payloads = {e['payload_sha256'] for e in self.history['attempts']}
        self.old_products = set(self.history['blocked_products'])
        binding = {'schema_version': 'agentswe-ai-lower-request-reserve/v1', 'key_sha256': digest(key),
            'history_seed_sha256': digest(canonical(self.history)), 'historical_context_invented': False,
            'reserve_source_sha256': digest(Path(__file__).read_bytes()),
            'identity_source_sha256': digest(Path(verify_request.__code__.co_filename).read_bytes())}
        # A crash before binding publication remains closed; callers may not
        # replace a key/seed under an established reservation directory.
        lock_path = self.root / '.lock'
        fd = os.open(lock_path, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)
        with os.fdopen(fd, 'r+') as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            path = self.root / 'binding.json'
            if path.exists():
                require(json.loads(private_file(path)) == binding, 'reserve binding key/history/source changed')
            else:
                require(set(v.name for v in self.root.iterdir()) == {'.lock'}, 'unbound nonempty reserve cannot be reset')
                exclusive_private(path, canonical(binding) + b'\n')
            for name in ('contexts', 'request_ids'):
                directory = self.root / name
                require(not directory.is_symlink(), 'symlinked request registry namespace')
                directory.mkdir(mode=0o700, exist_ok=True)

    @staticmethod
    def context_digest(identity):
        return digest(canonical({key: identity[key] for key in ('product_source_digest', 'case_id', 'case_input_sha256', 'request_fingerprint')}))

    def reserve(self, header, wire_bytes, *, legacy_bytes=None):
        identity = verify_request(header, self.key, wire_bytes)
        if identity['request_id'] in self.old_ids:
            raise ReplayForbidden('historical_request_replay_forbidden')
        if identity['product_source_digest'] in self.old_products:
            raise ReplayForbidden('historical_product_replay_forbidden')
        if ({digest(wire_bytes)} | ({digest(legacy_bytes)} if legacy_bytes is not None else set())) & self.old_payloads:
            raise ReplayForbidden('historical_payload_replay_forbidden')
        context = self.context_digest(identity)
        with (self.root / '.lock').open('r+') as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            cp = self.root / 'contexts' / (context + '.json')
            rp = self.root / 'request_ids' / (identity['request_id'] + '.json')
            if cp.exists() or cp.is_symlink() or rp.exists() or rp.is_symlink():
                raise ReplayForbidden('logical_request_execution_already_reserved')
            record = {'schema_version': 'agentswe-ai-lower-request-intent/v1', 'state': 'intent',
                      'identity': identity, 'logical_context_digest': context, 'replay_allowed': False}
            # Either durable file alone blocks repetition after a crash.
            exclusive_private(cp, canonical(record) + b'\n')
            exclusive_private(rp, canonical(record) + b'\n')
        return {**identity, 'logical_context_digest': context, 'signature_verified': True}
