"""Case-owned launcher process groups and exact-label product cleanup."""
from __future__ import annotations

import json
import os
import re
import secrets
import signal
import subprocess
import time
from pathlib import Path

OWNER_KEY = 'AGENTSWE_CLAUDE_PRODUCT_OWNER'
OWNER_LABEL = 'agentswe.claude.case-owner'
# The product runs with --rm. Docker 29 removes an exited --rm container asynchronously: for several
# seconds `docker inspect` still shows it and `docker rm -f` answers "removal of container ... is already
# in progress". Cleanup waits for that removal, inside the reserved cleanup budget, before judging absence.
REMOVAL_WAIT_SECONDS = 30
REMOVAL_POLL_SECONDS = 0.5


def cleanup_owned(owner: str, *, deadline: float | None = None) -> dict:
    if not re.fullmatch(r'claude-[0-9a-f]{32}', owner):
        raise ValueError('invalid task-owned container namespace')
    def remaining():
        if deadline is None:
            return 20
        left = deadline - time.monotonic()
        if left <= 0:
            raise TimeoutError('case cleanup deadline exhausted')
        return min(5, left)
    listed = subprocess.run(['docker', 'ps', '-aq', '--no-trunc', '--filter',
        f'label={OWNER_LABEL}={owner}'], text=True, capture_output=True, timeout=remaining(), check=False)
    if listed.returncode:
        raise OSError('cannot inspect owned product containers')
    records = []
    for cid in listed.stdout.split():
        if not re.fullmatch(r'[0-9a-f]{64}', cid):
            raise OSError('invalid Docker container identity in cleanup inventory')
        inspected = subprocess.run(['docker', 'inspect', '--format', '{{json .Config.Labels}}', cid],
            text=True, capture_output=True, timeout=remaining(), check=False)
        if inspected.returncode:  # --rm can win this race.
            records.append({'container_id': cid, 'already_absent': True})
            continue
        if json.loads(inspected.stdout).get(OWNER_LABEL) != owner:
            raise OSError('container ownership changed; refusing removal')
        removed = subprocess.run(['docker', 'rm', '-f', cid], text=True, capture_output=True, timeout=remaining(), check=False)
        in_progress = removed.returncode != 0 and 'already in progress' in (removed.stdout + removed.stderr).lower()
        checked = subprocess.run(['docker', 'inspect', cid], text=True, capture_output=True, timeout=remaining(), check=False)
        if in_progress:
            # Only this refusal is awaited; any other rm failure is judged from the one inspect, as before.
            # The wait ends early enough to leave the final inventory call its 5 s inside the case deadline.
            wait_until = time.monotonic() + REMOVAL_WAIT_SECONDS
            if deadline is not None:
                wait_until = min(wait_until, deadline - 5)
            while checked.returncode == 0 and time.monotonic() < wait_until:
                time.sleep(REMOVAL_POLL_SECONDS)
                checked = subprocess.run(['docker', 'inspect', cid], text=True, capture_output=True,
                                         timeout=remaining(), check=False)
        record = {'container_id': cid, 'ownership_proven': True, 'remove_exit_code': removed.returncode,
                  'absent_after_cleanup': checked.returncode != 0}
        if removed.returncode:
            record.update(remove_stderr=removed.stderr[-500:], removal_in_progress_at_rm=in_progress)
        records.append(record)
    remaining = subprocess.run(['docker', 'ps', '-aq', '--filter', f'label={OWNER_LABEL}={owner}'],
        text=True, capture_output=True, timeout=remaining(), check=False)
    return {'owner': owner, 'records': records, 'complete': remaining.returncode == 0 and not remaining.stdout.strip()}


def run_scoped_launcher(command, *, output: Path, timeout: int = 600, env=None, cwd=None):
    """One verified aggregate for launcher/product; all cleanup inside case budget."""
    from agentloop.evaluator.owned_resources import run_owned
    if not 100 <= timeout <= 600:
        raise ValueError('case wall budget must include 90 seconds of bounded resource cleanup')
    output.mkdir(parents=True, exist_ok=True)
    path = output / 'product_container_lifecycle.json'
    if path.exists():
        raise ValueError('case launcher lifecycle already exists; choose a new execution directory')
    owner = 'claude-' + secrets.token_hex(16)
    child_env = dict(os.environ if env is None else env)
    child_env[OWNER_KEY] = owner
    child_env['AGENTSWE_CLAUDE_REQUIRE_AGGREGATE'] = '1'
    started = time.monotonic()
    deadline = started + timeout
    # The reused scope helper bounds identity checks/stop/collect calls. Reserve
    # 90s for those calls plus exact-label Docker metadata cleanup and evidence.
    work_seconds = timeout - 90
    # D52: tell the launcher the evaluator's own case deadline.  Same CLOCK_MONOTONIC
    # epoch, one host, one process group; read for attribution only (see
    # lower_agent_launcher._d52_case_deadline_passed) and never to extend a budget.
    # This is the single place both the dev path (controller.py) and the hidden path
    # (hidden_executor.py) reach the product.
    child_env['AGENTSWE_CLAUDE_CASE_DEADLINE_MONOTONIC'] = repr(started + work_seconds)
    record = {'schema_version': 'agentswe-claude-product-lifecycle/v2', 'owner': owner,
              'case_timeout_seconds': timeout, 'work_timeout_seconds': work_seconds,
              'cleanup_reserved_seconds': 90, 'aggregate_memory_bytes': 4 * 1024 ** 3,
              'network_mode': 'none', 'state': 'starting', 'started_at': time.time()}
    path.write_text(json.dumps(record, indent=2) + '\n')
    try:
        done, attestation = run_owned(command, cwd=cwd or '/', env=child_env,
                                     output=output / 'owned_resources', timeout=work_seconds)
        record['resource_attestation'] = attestation
        record['state'] = 'timeout' if done.returncode == 124 else 'exited'
        record['exit_code'] = done.returncode
        if done.returncode == 124:
            raise subprocess.TimeoutExpired(command, work_seconds, output=done.stdout, stderr=done.stderr)
        return done
    finally:
        try:
            record['cleanup'] = cleanup_owned(owner, deadline=deadline - 1)
        except Exception as exc:
            record['cleanup'] = {'complete': False, 'error': type(exc).__name__ + ': ' + str(exc)}
        record['finished_at'] = time.time()
        record['total_elapsed_seconds'] = time.monotonic() - started
        record['within_total_budget'] = record['total_elapsed_seconds'] <= timeout
        path.write_text(json.dumps(record, indent=2) + '\n')
        if record['cleanup'].get('complete') is not True or not record['within_total_budget']:
            raise OSError('case aggregate cleanup or total wall budget could not be verified')
