"""Coordinator cleanup for retained readiness Docker resources after unit exit.

Resource declarations are evaluator-owned and must match actual run mounts or
Compose labels. No missing resource is inferred to have been safely cleaned.
"""
from __future__ import annotations

import argparse
import fcntl
import os
import hashlib
import json
from pathlib import Path
import re
import subprocess
import tempfile
import time

from cleanup_compose_residual import require_terminal_unit, inspect_json, terminal_trial_result


def command(argv):
    return subprocess.run(argv, text=True, capture_output=True, check=True, timeout=30).stdout


def ref(path):
    return {'path': str(path), 'sha256': hashlib.sha256(path.read_bytes()).hexdigest()}


def write(path, value):
    # Publish an immutable complete file; a crash cannot leave half an intent.
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(mode='w', dir=path.parent, prefix='.cleanup-', delete=False) as stream:
            temporary = Path(stream.name)
            json.dump(value, stream, indent=2)
            stream.write('\n')
            stream.flush()
            os.fsync(stream.fileno())
        os.link(temporary, path)
        descriptor = os.open(path.parent, os.O_RDONLY)
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)
    return ref(path)


def belongs(detail, run):
    def inside(value):
        return isinstance(value, str) and (value == str(run) or value.startswith(str(run) + '/'))
    labels = detail.get('Config', {}).get('Labels') or {}
    return inside(labels.get('com.docker.compose.project.working_dir')) or any(
        inside(m.get('Source')) for m in detail.get('Mounts', []))


def inventory(attempts=5):
    """One consistent listing of the machine's containers and networks.

    A resource can be removed between the listing and its inspection; the
    original code said so and told the caller to repeat the observation, but
    nothing did, so a single unlucky moment failed the finalize of a run that
    was already terminal. Sibling runs create and destroy compose networks
    continuously, and finalizing one task while others still run is now normal,
    so the retry belongs here.

    Only the observation is retried. No deletion, attestation or ownership
    decision is repeated, and a resource still present on the last attempt is
    still reported present.
    """
    for remaining in range(attempts - 1, -1, -1):
        result = {}
        vanished = None
        for kind, argv in [('container', ['docker', 'ps', '-aq', '--no-trunc']),
                           ('network', ['docker', 'network', 'ls', '-q', '--no-trunc'])]:
            for cid in command(argv).split():
                detail = inspect_json(kind, cid)
                if detail is None:
                    vanished = '%s %s' % (kind, cid)
                    break
                result[detail['Id']] = (kind, detail)
            if vanished:
                break
        if not vanished:
            return result
        if not remaining:
            raise ValueError('resource vanished during inventory; repeat observation: ' + vanished)
        time.sleep(0.5)


def public_inventory(values, owned, run_id, run=None):
    """Project the resources this run is accountable for.

    With ``run`` given, anything the run neither owns nor can be attributed
    to is left out, so an unrelated workload starting or stopping during
    cleanup cannot wedge this run against its own pinned before.json.
    Owned resources are never filtered out, whatever ``belongs`` says, so
    every caller can still look up each owned id in the projection.
    """
    result = {}
    for cid, (kind, detail) in values.items():
        if run is not None and cid not in owned and not belongs(detail, run):
            continue
        row = {'kind': kind, 'name': detail.get('Name')}
        if cid in owned:
            if kind == 'container' and (detail.get('State', {}).get('Running') is not False or
                                        detail.get('State', {}).get('Status') not in {'exited', 'dead'}):
                raise ValueError('owned container is not terminal')
            row.update(run_id=run_id, state='terminal')
        result[cid] = row
    return {'resources': result}


def read(path):
    path = Path(path)
    if path.is_symlink() or not path.is_file():
        raise ValueError('cleanup evidence must be a regular file')
    def pairs(items):
        value = {}
        for key, child in items:
            if key in value:
                raise ValueError('duplicate cleanup JSON key')
            value[key] = child
        return value
    return json.loads(path.read_bytes(), object_pairs_hook=pairs)


def checked_ref(reference, run):
    path = Path(reference['path'])
    if not path.is_absolute() or not path.is_relative_to(run) or any(p.is_symlink() for p in (path, *path.parents)):
        raise ValueError('cleanup reference escaped its run')
    value = read(path)
    if ref(path) != reference:
        raise ValueError('cleanup evidence hash changed')
    return value


def preserve(path, value):
    if path.exists():
        if read(path) != value:
            raise ValueError('immutable cleanup evidence changed: ' + path.name)
        return ref(path)
    return write(path, value)


def arguments(run, declaration, output):
    run = Path(run).resolve(strict=True)
    output = Path(output).absolute()
    if any(p.is_symlink() for p in (output, *output.parents)) or output == run or not output.is_relative_to(run):
        raise ValueError('cleanup output must be inside this run without symlinks')
    declared = read(declaration)
    if declared.get('run_id') != run.name or declared.get('owner') != 'evaluator':
        raise ValueError('foreign resource declaration')
    containers, networks = declared.get('containers'), declared.get('networks', [])
    if not isinstance(containers, list) or not isinstance(networks, list):
        raise ValueError('resource declaration lists required')
    owned = containers + networks
    if (not containers or any(not isinstance(cid, str) or not re.fullmatch('[0-9a-f]{64}', cid) for cid in owned)
            or len(set(owned)) != len(owned)):
        raise ValueError('resource declaration must contain unique full Docker IDs')
    return run, output, declared, containers, networks, owned


def harbor_terminal(run):
    jobs = list((run/'jobs').glob('*/result.json'))
    # A Builder resumed after a provider outage leaves one trial per segment
    # under the same job. The terminal record is the last segment's, named by
    # the segment ledger; terminal_trial_result() keeps every other property.
    if len(jobs) != 1:
        raise ValueError('expected one Harbor job and one Builder trial')
    try:
        trial_path = terminal_trial_result(run)
    except RuntimeError as exc:
        raise ValueError('expected one Harbor job and one Builder trial: ' + str(exc)) from exc
    job, trial = read(jobs[0]), read(trial_path)
    execution = trial.get('agent_execution')
    if not job.get('finished_at') or not trial.get('finished_at'):
        raise ValueError('Harbor or Builder lacks terminal evidence')
    if isinstance(execution, dict) and execution.get('finished_at'):
        builder_state = 'terminal'
    elif execution is None and isinstance(trial.get('exception_info'), dict) and trial['exception_info'].get('exception_type'):
        builder_state = 'not_started'
    else:
        raise ValueError('Harbor or Builder lacks terminal evidence')
    return {'harbor_evidence': ref(jobs[0]), 'builder_evidence': ref(trial_path), 'builder_state': builder_state}


def owned_inventory(values, containers, networks, run):
    actual = {cid for cid, (kind, detail) in values.items() if kind == 'container' and belongs(detail, run)}
    if actual != set(containers):
        raise ValueError('declared containers differ from retained run ownership: ' + str(actual ^ set(containers)))
    projects = {(values[cid][1].get('Config', {}).get('Labels') or {}).get('com.docker.compose.project') for cid in containers}
    projects.discard(None)
    actual_networks = {cid for cid, (kind, detail) in values.items() if kind == 'network' and
                       (detail.get('Labels') or {}).get('com.docker.compose.project') in projects}
    if actual_networks != set(networks):
        raise ValueError('network declaration differs from owned Compose networks')
    for cid in networks:
        if set(values[cid][1].get('Containers', {})) - set(containers):
            raise ValueError('owned network has unrelated attached containers')


def safe_detail(detail):
    # Never persist Config.Env or command strings containing controller tokens.
    return {key: detail.get(key) for key in ('Id', 'Name', 'State', 'Mounts', 'Containers')} | {
        'Labels': detail.get('Labels'), 'Config': {'Labels': (detail.get('Config') or {}).get('Labels')}}


def validate_plan(plan, run, unit, declared, output):
    if (plan.get('run_id') != run.name or plan.get('owner') != 'evaluator' or plan.get('unit') != unit
            or checked_ref(plan['declaration'], run) != declared):
        raise ValueError('cleanup plan identity changed')
    original_unit = checked_ref(plan['unit_evidence'], run)
    current_unit = require_terminal_unit(unit, run)
    # ExecStart embeds runtime fields that any `systemctl daemon-reload` blanks
    # (measured: pid=N status=7 becomes pid=0 status=0/0), which made a retry
    # impossible after a routine reload. InvocationID and ExecMainPID both
    # survive it, and an invocation id is never reused the way a PID is.
    identity = ('InvocationID', 'ExecMainPID', 'ExecMainExitTimestamp')
    if not any(original_unit.get(key) for key in identity):
        identity = ('ExecMainPID', 'ExecMainExitTimestamp', 'ExecStart')
    for key in identity:
        if original_unit.get(key) != current_unit.get(key):
            raise ValueError('owning unit execution identity changed')
    for key in ('harbor_evidence', 'builder_evidence'):
        checked_ref(plan[key], run)
    current_terminal = harbor_terminal(run)
    if any(plan.get(key) != current_terminal[key] for key in current_terminal):
        raise ValueError('Harbor terminal proof changed')
    owned = declared['containers'] + declared.get('networks', [])
    if plan.get('owned_resources') != owned or set(plan.get('stats', {})) != set(owned):
        raise ValueError('cleanup plan resource set changed')
    before = checked_ref(plan['before'], run)
    terminal = checked_ref(plan['terminal_resources'], run)
    values = {cid: (row['kind'], row['detail']) for cid, row in terminal.items()}
    if set(values) != set(owned):
        raise ValueError('cleanup plan terminal resource set changed')
    owned_inventory(values, declared['containers'], declared.get('networks', []), run)
    projection = public_inventory(values, owned, run.name, run)['resources']
    if any(before['resources'].get(cid) != projection[cid] for cid in owned):
        raise ValueError('terminal ownership differs from cleanup before evidence')
    for cid, reference in plan['stats'].items():
        if checked_ref(reference, run).get('resource_id') != cid:
            raise ValueError('cleanup stats resource identity changed')
    return before, values


def verify_completed_cleanup(run, unit, resources, output):
    run, output, declared, containers, networks, owned = arguments(run, resources, output)
    require_terminal_unit(unit, run)
    plan = read(output/'deletion_plan.json')
    before, values = validate_plan(plan, run, unit, declared, output)
    receipt = read(output/'cleanup.json')
    if receipt.get('deletion_plan') != ref(output/'deletion_plan.json'):
        raise ValueError('completed cleanup plan hash changed')
    expected = {key: plan[key] for key in ('run_id', 'owner', 'unit_evidence', 'harbor_evidence',
        'builder_evidence', 'builder_state', 'owned_resources', 'before', 'stats')}
    if any(receipt.get(key) != value for key, value in expected.items()):
        raise ValueError('completed cleanup receipt differs from original plan')
    if receipt.get('unit_state') != 'terminal' or receipt.get('harbor_state') != 'terminal':
        raise ValueError('completed cleanup terminal state invalid')
    after = checked_ref(receipt['after'], run)
    if after['resources'] != {cid: row for cid, row in before['resources'].items() if cid not in owned}:
        raise ValueError('completed cleanup inventory mismatch')
    for cid in owned:
        verify_deletion_records(cid, values[cid][0], output, plan)
        if inspect_json(values[cid][0], cid) is not None:
            raise ValueError('completed cleanup resource is no longer absent')
    current = inventory()
    if any(kind == 'container' and belongs(detail, run) for kind, detail in current.values()):
        raise ValueError('new owned container appeared after completed cleanup')
    return ref(output/'cleanup.json')


def deletion_intent(cid, kind, output, plan):
    return {'resource_id': cid, 'kind': kind, 'run_id': plan['run_id'],
        'deletion_plan': ref(output/'deletion_plan.json')}


def verify_deletion_records(cid, kind, output, plan):
    intent = read(output/(cid+'.delete-intent.json'))
    if intent != deletion_intent(cid, kind, output, plan):
        raise ValueError('deletion intent differs from immutable plan')
    done = read(output/(cid+'.deleted.json'))
    if done != {**intent, 'absent_after_cleanup': True}:
        raise ValueError('deletion completion differs from original intent')


def finalize(run, unit, declaration, output):
    run, output, declared, containers, networks, owned = arguments(run, declaration, output)
    require_terminal_unit(unit, run)
    harbor = harbor_terminal(run)
    # One coordinator may resume a partial cleanup; two may never mutate it.
    lock = run/('.' + output.name + '.lock')
    if lock.is_symlink():
        raise ValueError('cleanup lock must not be a symlink')
    with lock.open('a+') as handle:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        if (output/'cleanup.json').exists():
            return verify_completed_cleanup(run, unit, declaration, output)
        output.mkdir(parents=True, exist_ok=True)
        preserve(output/'declaration.json', declared)
        if not (output/'unit.json').exists():
            write(output/'unit.json', require_terminal_unit(unit, run))
        if not (output/'deletion_plan.json').exists():
            values = inventory()
            owned_inventory(values, containers, networks, run)
            stats = {}
            for cid in containers:
                path = output/(cid+'.json')
                if path.exists():
                    if read(path).get('resource_id') != cid:
                        raise ValueError('preserved stats resource mismatch')
                    stats[cid] = ref(path)
                else:
                    detail = values[cid][1]
                    stats[cid] = write(path, {'resource_id': cid, 'observed_at': time.time(),
                        'stats_stdout': command(['docker', 'stats', '--no-stream', '--format', '{{json .}}', cid]),
                        'state': detail['State'], 'labels': (detail.get('Config') or {}).get('Labels'),
                        'mounts': [{k: m.get(k) for k in ('Type', 'Source', 'Destination', 'RW')} for m in detail.get('Mounts', [])]})
                require_terminal_unit(unit, run)
                current = inspect_json('container', cid)
                if current is None or not belongs(current, run):
                    raise ValueError('container ownership changed before stopping')
                if current['State'].get('Running'):
                    command(['docker', 'stop', '--time', '10', cid])
            before_values = inventory()
            owned_inventory(before_values, containers, networks, run)
            before = public_inventory(before_values, owned, run.name, run)
            for cid in networks:
                path = output/(cid+'.json')
                if not path.exists():
                    detail = before_values[cid][1]
                    write(path, {'resource_id': cid, 'observed_at': time.time(), 'kind': 'network',
                        'labels': detail.get('Labels'), 'members': detail.get('Containers')})
                stats[cid] = ref(path)
            terminal_resources = {cid: {'kind': before_values[cid][0],
                'detail': safe_detail(before_values[cid][1])} for cid in owned}
            plan = {'run_id': run.name, 'owner': 'evaluator', 'unit': unit, **harbor,
                'declaration': ref(output/'declaration.json'), 'unit_evidence': ref(output/'unit.json'),
                'owned_resources': owned, 'stats': stats,
                'before': preserve(output/'before.json', before),
                'terminal_resources': preserve(output/'terminal_resources.json', terminal_resources)}
            write(output/'deletion_plan.json', plan)
        plan = read(output/'deletion_plan.json')
        before, original = validate_plan(plan, run, unit, declared, output)
        for cid in owned:
            kind, saved = original[cid]
            require_terminal_unit(unit, run)
            current = inspect_json(kind, cid)
            intent_path = output/(cid+'.delete-intent.json')
            expected = deletion_intent(cid, kind, output, plan)
            if (output/(cid+'.deleted.json')).exists() and current is not None:
                raise ValueError('previously deleted resource is present again')
            if current is None:
                if not intent_path.exists() or read(intent_path) != expected:
                    raise ValueError('resource absent without prior deletion intent')
            else:
                if kind == 'container':
                    if not belongs(current, run) or current.get('State', {}).get('Running') is not False or current['State'].get('Status') not in {'exited', 'dead'}:
                        raise ValueError('owned terminal resource changed before removal')
                elif current.get('Containers') or current.get('Labels') != saved.get('Labels'):
                    raise ValueError('network ownership changed or members remain')
                preserve(intent_path, expected)
                command(['docker', kind, 'rm', cid])
                if inspect_json(kind, cid) is not None:
                    raise ValueError('resource remains after removal')
            preserve(output/(cid+'.deleted.json'), {**expected, 'absent_after_cleanup': True})
        after = public_inventory(inventory(), (), run.name, run)
        if after['resources'] != {cid: row for cid, row in before['resources'].items() if cid not in owned}:
            raise ValueError('unrelated resource inventory changed or owned resource remains')
        after_ref = preserve(output/'after.json', after)
        return write(output/'cleanup.json', {
            **{key: plan[key] for key in ('run_id', 'owner', 'unit_evidence', 'harbor_evidence',
                'builder_evidence', 'builder_state', 'owned_resources', 'before', 'stats')},
            'unit_state': 'terminal', 'harbor_state': 'terminal', 'after': after_ref,
            'deletion_plan': ref(output/'deletion_plan.json')})


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--run-dir', type=Path, required=True)
    parser.add_argument('--unit', required=True)
    parser.add_argument('--resources', type=Path, required=True)
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    print(json.dumps(finalize(args.run_dir, args.unit, args.resources, args.output)))


if __name__ == '__main__':
    main()
