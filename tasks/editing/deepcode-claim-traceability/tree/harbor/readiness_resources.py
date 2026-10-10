"""Keep stopped evaluator resources until the terminal coordinator records cleanup."""
from __future__ import annotations

import json
from pathlib import Path
import re
import subprocess

from evaluator.harness.builder_lifecycle import write_json


def inspect_container(identifier):
    result = subprocess.run(['docker', 'container', 'inspect', identifier],
        capture_output=True, text=True, check=True, timeout=15)
    values = json.loads(result.stdout)
    if len(values) != 1 or not re.fullmatch(r'[0-9a-f]{64}', values[0].get('Id', '')):
        raise ValueError('invalid retained container identity')
    return values[0]


def retain_container(identifier, output, *, expected_name=None, expected_mount=None,
                     expected_project=None, expected_working_dir=None):
    """Save live statistics, stop the exact owned container, retain terminal bytes."""
    before = inspect_container(identifier)
    labels = before.get('Config', {}).get('Labels') or {}
    if expected_name and before.get('Name', '').lstrip('/') != expected_name.lstrip('/'):
        raise ValueError('retained container name mismatch')
    if expected_mount and not any((m.get('Source'), m.get('Destination')) == expected_mount
            for m in before.get('Mounts', [])):
        raise ValueError('retained container mount ownership mismatch')
    if expected_project and (labels.get('com.docker.compose.project') != expected_project or
            labels.get('com.docker.compose.project.working_dir') != expected_working_dir):
        raise ValueError('retained compose ownership mismatch')
    if not expected_mount and not expected_project:
        raise ValueError('retention requires run-specific ownership')
    cid = before['Id']
    stats = subprocess.run(['docker', 'stats', '--no-stream', '--format', '{{json .}}', cid],
        capture_output=True, text=True, check=True, timeout=20)
    directory = Path(output) / 'readiness_resource_retention'
    directory.mkdir(parents=True, exist_ok=True)
    # Exclude Config.Env and command strings: they can carry controller tokens.
    def safe(value):
        return {key: value.get(key) for key in ('Id', 'Name', 'Image', 'State', 'Mounts')} | {
            'labels': value.get('Config', {}).get('Labels') or {}}
    path = directory / (cid + '.json')
    receipt = {'container_id': cid, 'owner': 'evaluator', 'removal_deferred': True,
        'stats_stdout': stats.stdout, 'before': safe(before), 'retained_terminal': False}
    write_json(path, receipt)
    subprocess.run(['docker', 'stop', '--time', '20', cid], capture_output=True,
        text=True, check=True, timeout=30)
    after = inspect_container(cid)
    if after['Id'] != cid or after.get('State', {}).get('Status') not in {'exited', 'dead'}:
        raise RuntimeError('container did not reach retained terminal state')
    receipt.update(after=safe(after), retained_terminal=True)
    write_json(path, receipt)
    return {'container_id': cid, 'retained_terminal': True, 'removal_deferred': True,
        'absent_after_cleanup': False, 'evidence_path': str(path)}


def retained_manifest(run_dir, brokers):
    run = Path(run_dir)
    builder = json.loads((run / 'builder_container_cleanup.json').read_bytes())
    if not builder.get('retained_terminal') and not builder.get('nothing_started'):
        raise ValueError('Builder terminal retention is unproven')
    # A container Harbor already removed cannot be handed to the coordinator for
    # ownership-verified deletion; declaring it would make the coordinator's own
    # inventory check fail on a resource that is provably gone. Keep it out of the
    # cleanup list and record it, with its attribution, as separate evidence.
    absent = [row for row in builder.get('containers', [])
              if row.get('absent_after_cleanup') is True]
    absent_ids = {row['container_id'] for row in absent}
    containers = [row['container_id'] for row in builder.get('containers', [])
                  if row['container_id'] not in absent_ids]
    for broker in brokers:
        if broker is None:
            continue
        cid = getattr(broker, 'container_id', None)
        if not cid:
            raise ValueError('started broker container ownership missing')
        containers.append(cid)
    networks = [row['network_id'] for row in builder.get('networks', [])]
    if any(not re.fullmatch(r'[0-9a-f]{64}', value) for value in containers + networks):
        raise ValueError('retained resources require full immutable Docker identifiers')
    if any(not re.fullmatch(r'[0-9a-f]{64}', value) for value in absent_ids):
        raise ValueError('absent resources require full immutable Docker identifiers')
    value = {'run_id': run.name, 'owner': 'evaluator', 'containers': sorted(set(containers)),
        'networks': sorted(set(networks)), 'removal_deferred': True,
        'coordinator_cleanup_required': True,
        'absent_before_declaration': sorted(absent, key=lambda row: row['container_id']),
        'absent_attribution': 'harbor environment teardown removed these owned containers '
                              'before evaluator retention; ownership was proven while they ran'}
    write_json(run / 'readiness_retained_resources.json', value)
    return value
