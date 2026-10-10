"""Provider-free admission to fresh readiness dispatch, separate from formal READY.

This checks current bytes under the coordinator lock. Readiness runs are allowed
while registry rows remain REPAIR, but never on stale effective source bindings.
The coordinator must finish deployments before starting a Builder campaign.
"""
from __future__ import annotations

import fcntl
import hashlib
import importlib.util
import json
import re
from pathlib import Path

from audit_readiness import tree_digest
from readiness_admission import registry_digest

ROOT = Path(__file__).resolve().parent


BUILDER_MODEL = 'deepseek-flash'  # upper Builder provider-TOML model
BUILDER_EFFORT = 'max'  # upper Builder provider-TOML reasoning effort
REQUEST_MAX_RETRIES = 10
STREAM_MAX_RETRIES = 10
STREAM_IDLE_TIMEOUT_MS = 300000


def configure_native_transport(provider_path):
    """Pin bounded native HTTP/stream retries for a newly created readiness config.

    The exact generated provider format is intentional. Refuse other sections
    rather than accidentally change a different provider. Codex 0.144.1 was
    verified offline to recognize all three integer keys as per-provider fields
    of ``ModelProviderInfo`` under ``[model_providers.<id>]``:

    * ``request_max_retries`` bounds the inner HTTP attempt loop (a 503 upstream
      answer is retried in place with exponential backoff).
    * ``stream_max_retries`` bounds the outer reconnect loop and is the ``N`` the
      CLI prints in ``Reconnecting... n/N``; it covers a stream that dies after
      the response was accepted.
    * ``stream_idle_timeout_ms`` is how long the client waits for the next SSE
      byte before it declares ``idle timeout waiting for SSE`` and reconnects.
      gateway's gpt-5.5 at xhigh takes 30-160 s to first byte, so a short idle
      timeout turns healthy slow thinking into a transport failure.

    The fake-upstream replay in ``probe/`` counted upstream POSTs for each key:
    10/10 survived 7 consecutive mid-stream cuts, and the idle timeout fired at
    exactly the configured 5 s and 15 s.  Earlier package 97 claimed 0.144.1 had
    no TOML key for stream retries and always used 5; the binary carries the key
    and honoured 10, so that claim is withdrawn.

    Retrying a stream that disconnected before completion replays nothing: no
    Candidate, feedback, hidden result or usage is reused, and the native client
    announces every attempt as ``Reconnecting... n/N`` in the rollout. Pinning
    these to zero was measured to end 13 of 13 readiness runs at the first
    provider disconnect while leaving provider usage exactly as unknown as
    before, so no-replay is enforced where it belongs -- at the Candidate,
    feedback, hidden and usage layers -- not by forbidding transport recovery.

    Re-running against an already configured file is not an error as long as the
    recorded values still match; two callers configuring one config previously
    failed closed after the binding was already correct.
    """
    path = Path(provider_path)
    before = sha(path)
    original = path.read_text()
    header = '[model_providers.gateway_direct]'
    if original.count(header) != 1 or re.search(r'^\s*\[', original.split(header)[1], re.M):
        raise ValueError('unexpected native provider config layout')
    if ('model_provider="gateway_direct"' not in original or 'model="' + BUILDER_MODEL + '"' not in original or
            'model_reasoning_effort="' + BUILDER_EFFORT + '"' not in original):
        raise ValueError('native provider model/effort configuration differs')
    existing = dict(re.findall(
        r'^\s*(request_max_retries|stream_max_retries|stream_idle_timeout_ms)\s*=\s*(\d+)\s*$',
        original, re.M))
    expected = {'request_max_retries': str(REQUEST_MAX_RETRIES),
                'stream_max_retries': str(STREAM_MAX_RETRIES),
                'stream_idle_timeout_ms': str(STREAM_IDLE_TIMEOUT_MS)}
    if existing and existing != expected:
        raise ValueError('native provider retry configuration differs')
    if not existing:
        path.write_text(original.rstrip() + '\nrequest_max_retries=' + str(REQUEST_MAX_RETRIES) +
                        '\nstream_max_retries=' + str(STREAM_MAX_RETRIES) +
                        '\nstream_idle_timeout_ms=' + str(STREAM_IDLE_TIMEOUT_MS) + '\n')
    return {'provider_config_before_sha256': before, 'provider_config_sha256': sha(path),
            'request_max_retries': REQUEST_MAX_RETRIES, 'stream_max_retries': STREAM_MAX_RETRIES,
            'stream_idle_timeout_ms': STREAM_IDLE_TIMEOUT_MS,
            'already_configured': bool(existing),
            'reason': 'bounded transport recovery raised for gateway gpt-5.5 (0921b); '
                      'no-replay is enforced at the Candidate/feedback/hidden/usage '
                      'layers, not by forbidding reconnects'}


# Callers created before the 2026-09-15 transport correction use the old name.
configure_native_no_replay = configure_native_transport


def sha(path):
    path = Path(path)
    if path.is_symlink() or not path.is_file():
        raise ValueError('missing or symlinked binding file: ' + str(path))
    return hashlib.sha256(path.read_bytes()).hexdigest()


def verify_binding(task_source, binding, *, control_root=None):
    """Return measured binding; fail before dispatch on any source/proof drift."""
    root = Path(control_root) if control_root is not None else ROOT
    source = Path(task_source)
    if not source.is_absolute() or source.is_symlink() or not source.is_dir():
        raise ValueError('readiness source must be the configured absolute directory')
    if not isinstance(binding, dict) or set(binding) != {'task', 'source_digest', 'contract_digest', 'registry_digest'}:
        raise ValueError('readiness requires exact task/source/contract/registry binding')
    spec = importlib.util.spec_from_file_location('readiness_dispatch_config', root / 'formal_config.py')
    cfg = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(cfg)
    task = binding['task']
    if task not in cfg.TASKS or source != Path(cfg.TASKS[task]):
        raise ValueError('readiness task does not match its configured source')
    # Use the existing coordinator lock; no registry/gate writes occur here.
    with (root / 'configuration_delta_registry.lock').open('a') as lock:
        fcntl.flock(lock.fileno(), fcntl.LOCK_SH)
        registry_path = root / 'configuration_delta_registry.json'
        contract_path = source / 'meta/0905_case_contract.json'
        actual = {'task': task, 'source_digest': tree_digest(source),
                  'contract_digest': sha(contract_path),
                  'registry_digest': registry_digest(registry_path, task)}
        if actual != binding:
            raise ValueError('readiness current source/contract/registry drift')
        contract = json.loads(contract_path.read_bytes())
        if contract.get('task') != task or contract.get('sibling') != str(source):
            raise ValueError('contract task/source identity mismatch')
        registry = json.loads(registry_path.read_bytes())
        if registry.get('schema_version') != 'agentswe-edit-configuration-registry/v1' or set(registry.get('tasks', {})) != set(cfg.TASKS):
            raise ValueError('configuration registry inventory/schema mismatch')
        # This task's own references only. Walking all ten made one task's
        # configuration drift fail a different task's dispatch, and the
        # registry inventory check above still requires all ten rows to
        # exist. Coverage was measured before narrowing: the only files
        # shared between tasks are control-plane files that appear in every
        # task's own row.
        row = registry['tasks'][task]
        refs = row.get('effective_source_files')
        if row.get('sibling') != str(cfg.TASKS[task]) or not isinstance(refs, list) or not refs:
            raise ValueError('missing current configuration references: ' + task)
        for ref in refs:
            path = Path(ref['path'])
            if not path.is_absolute() or sha(path) != ref['sha256']:
                raise ValueError('configuration effective source drift: ' + task + ': ' + str(path))
        snapshot = json.loads((root / 'post_repair_tree_snapshot.json').read_bytes())
        if snapshot['tasks'][task]['sibling']['digest'] != actual['source_digest']:
            raise ValueError('readiness requires refreshed source snapshot')
        # Catch replacement while file references were being measured.
        if (registry_digest(registry_path, task) != actual['registry_digest']
                or sha(contract_path) != actual['contract_digest']
                or tree_digest(source) != actual['source_digest']):
            raise ValueError('binding changed during dispatch preflight')
        return actual
