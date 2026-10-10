"""Evaluator-only lower request identities; no secrets enter Candidate or provider requests."""
from contextvars import ContextVar
import hashlib
import hmac
import json
import os
from pathlib import Path
import re
import secrets
import stat
import uuid

CONTEXT_HEADER = 'X-AgentSWE-Lower-Trusted-Context'
ACTIVE_CONTEXT = ContextVar('ai_lower_trusted_context', default=None)
VERSION = 'agentswe-ai-lower-request-context/v1'

# D104 (2026-09-21).  The signed slot is the evaluator's logical-request
# namespace: one reserved execution per (product, case, slot).  It must name
# every slot the lower action loop can emit, which is
# ``action:1..lower_agent_launcher.MAX_ACTION_STEPS``, the one
# ``accountability:<decision turn>`` finish re-ask, and ``authoring:1``.  This
# module is mounted standalone beside the broker inside Docker
# (evaluator/lower_responses_broker.py:13-23), so it cannot import the
# launcher; evaluator/verify_0911_request_reserve.py pins the two constants
# equal instead.  A slot the loop emits but this grammar refuses raises
# ValueError outside every handler in the loop and kills the case
# (0921b-gateway-r-010 dev_001: action:9).
ACTION_SLOT_STEPS = 10
_SLOT_STEP = '(?:%s)' % '|'.join(str(step) for step in range(ACTION_SLOT_STEPS, 0, -1))
REQUEST_SLOT_GRAMMAR = '(?:action:%s|accountability:%s|authoring:1)' % (_SLOT_STEP, _SLOT_STEP)


def require(condition, message):
    if not condition:
        raise ValueError(message)


def digest(value):
    return hashlib.sha256(value).hexdigest()


def canonical(value):
    return json.dumps(value, sort_keys=True, separators=(',', ':'), ensure_ascii=True).encode()


def private_file(path):
    path = Path(path)
    info = path.lstat()
    require(stat.S_ISREG(info.st_mode) and not path.is_symlink(), 'context file is not regular')
    require(info.st_uid == os.geteuid() and stat.S_IMODE(info.st_mode) == 0o600,
            'context file must be evaluator-owned mode 0600')
    return path.read_bytes()


def exclusive_private(path, value):
    path = Path(path)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    with os.fdopen(fd, 'wb') as output:
        output.write(value)
        output.flush()
        os.fsync(output.fileno())
    fd = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def binding_path(brokers, endpoint):
    return Path(brokers) / ('context-binding-' + digest(endpoint.encode()) + '.json')


def create_binding(brokers, evidence, endpoint):
    """Called only by evaluator broker startup, never by request callers."""
    require(Path(brokers).absolute() == Path(brokers).resolve() and Path(evidence).absolute() == Path(evidence).resolve(),
            'symlinked context directory ancestry')
    brokers, evidence = Path(brokers).resolve(), Path(evidence).resolve()
    require(evidence.parent == brokers, 'context evidence must belong to this broker directory')
    require(not Path(brokers).is_symlink() and not Path(evidence).is_symlink(), 'symlinked context directory')
    key_file = evidence / 'logical-context.key'
    exclusive_private(key_file, secrets.token_bytes(32))
    binding = {'schema_version': VERSION, 'endpoint': endpoint, 'key_file': str(key_file),
               'key_sha256': digest(private_file(key_file)), 'broker_evidence': str(evidence)}
    path = binding_path(brokers, endpoint)
    exclusive_private(path, canonical(binding) + b'\n')
    return path


def load_binding(path, endpoint, *, brokers=None):
    path = Path(path)
    require(path.absolute() == path.resolve(), 'symlinked context binding ancestry')
    require(path.name == binding_path(path.parent, endpoint).name, 'context endpoint binding name mismatch')
    if brokers is not None:
        require(path.parent.resolve() == Path(brokers).resolve(), 'context binding outside this run')
    require(not path.parent.is_symlink(), 'symlinked context binding directory')
    binding = json.loads(private_file(path))
    require(binding.get('schema_version') == VERSION and binding.get('endpoint') == endpoint,
            'context binding endpoint mismatch')
    evidence = Path(binding['broker_evidence'])
    key_file = Path(binding['key_file'])
    require(evidence.parent == path.parent and key_file.parent == evidence and key_file.name == 'logical-context.key',
            'context key outside registered broker evidence')
    require(not evidence.is_symlink(), 'symlinked context key directory')
    key = private_file(key_file)
    require(len(key) == 32 and digest(key) == binding['key_sha256'], 'context key binding changed')
    return binding, key


def resolve_binding(run, endpoint):
    brokers = Path(run).resolve() / 'brokers'
    path = binding_path(brokers, endpoint)
    load_binding(path, endpoint, brokers=brokers)
    return path


def normalized_payload(payload, *, stream=True):
    # Preserve the modern broker's exact body policy and insertion order.
    body = dict(payload)
    body['stream'] = stream
    body['model'] = 'deepseek-flash'
    body['reasoning'] = {'effort': 'high'}
    body.pop('max_output_tokens', None)
    return json.dumps(body, ensure_ascii=False).encode()


def validate_identity(identity):
    fields = {'schema_version', 'product_source_digest', 'case_id', 'case_input_sha256',
              'request_slot', 'request_fingerprint', 'request_id', 'payload_sha256'}
    require(isinstance(identity, dict) and set(identity) == fields, 'invalid context fields')
    require(identity['schema_version'] == VERSION, 'invalid context version')
    for key in ('product_source_digest', 'case_input_sha256', 'request_fingerprint', 'payload_sha256'):
        require(isinstance(identity[key], str) and re.fullmatch('[0-9a-f]{64}', identity[key]), 'invalid context digest')
    require(re.fullmatch('(?:dev_00[12]|test_00[1-6])', str(identity['case_id'])), 'invalid context case')
    require(re.fullmatch(REQUEST_SLOT_GRAMMAR, str(identity['request_slot'])), 'invalid request slot')
    require(re.fullmatch('[0-9a-f]{32}', str(identity['request_id'])), 'invalid request identity')
    require(identity['request_fingerprint'] == fingerprint(identity['case_id'], identity['case_input_sha256'], identity['request_slot']),
            'request fingerprint mismatch')


def fingerprint(case_id, case_digest, slot):
    return digest(canonical({'case_id': case_id, 'case_input_sha256': case_digest, 'request_slot': slot}))


def sign_request(key, product_digest, case_id, case_digest, slot, payload, *, request_id=None):
    identity = {'schema_version': VERSION, 'product_source_digest': product_digest, 'case_id': case_id,
        'case_input_sha256': case_digest, 'request_slot': slot, 'request_fingerprint': fingerprint(case_id, case_digest, slot),
        'request_id': request_id or uuid.uuid4().hex, 'payload_sha256': digest(normalized_payload(payload))}
    validate_identity(identity)
    return json.dumps({'identity': identity, 'signature': hmac.new(key, canonical(identity), hashlib.sha256).hexdigest()},
                      sort_keys=True, separators=(',', ':'))


def verify_request(header, key, wire_bytes):
    require(isinstance(header, str) and len(header) <= 2048, 'trusted context missing or oversized')
    value = json.loads(header)
    require(isinstance(value, dict) and set(value) == {'identity', 'signature'}, 'invalid signed context')
    identity = value['identity']
    validate_identity(identity)
    expected = hmac.new(key, canonical(identity), hashlib.sha256).hexdigest()
    require(isinstance(value['signature'], str) and hmac.compare_digest(value['signature'], expected), 'untrusted context signature')
    require(identity['payload_sha256'] == digest(wire_bytes), 'signed request body changed')
    return identity


def request_header(payload, slot):
    context = ACTIVE_CONTEXT.get()
    require(context is not None, 'lower request has no evaluator logical context')
    return sign_request(context['key'], context['product_source_digest'], context['case_id'],
                        context['case_input_sha256'], slot, payload)
