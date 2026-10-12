"""Atomically rebind the configuration registry and the source snapshot.

Every readiness dispatch binds ``{task, source_digest, contract_digest,
registry_digest}`` and refuses to run if any of them drifts.  A source change
therefore has to be published through both the registry's per-file references
and the snapshot's per-task tree digest, together, under the coordinator lock,
or the next dispatch fails closed -- which is the point of the binding.

Only the coordinator writes these two files.  This takes the exclusive lock for
the whole measure-and-write window, writes each file by atomic replace, and
then re-verifies every affected task through the real ``verify_binding`` path
rather than trusting its own arithmetic.
"""
from __future__ import annotations

import argparse
import fcntl
import hashlib
import importlib.util
import json
import os
import sys
from pathlib import Path

CONTROL = Path('@@AGENTSWE_EDITING_CONTROL@@')
# The control-plane modules import one another by bare name.
sys.path.insert(0, str(CONTROL))


def load(name):
    spec = importlib.util.spec_from_file_location(name, CONTROL / (name + '.py'))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def sha(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def measure_tree(root, exclusions):
    """Digest/count/size the tree over exactly the files ``tree_digest`` walks."""
    files = total = 0
    for directory, dirnames, filenames in os.walk(root, followlinks=False):
        dirnames[:] = sorted(name for name in dirnames if name not in exclusions)
        base = Path(directory)
        for name in sorted(filenames):
            path = base / name
            parts = path.relative_to(root).parts
            if any(part in exclusions for part in parts) or path.suffix in {'.pyc', '.pyo'}:
                continue
            files += 1
            if path.is_symlink():
                total += len(os.readlink(path).encode('utf-8'))
            elif path.is_file():
                total += path.stat().st_size
    return files, total


def write_atomic(path, payload):
    temporary = path.with_suffix(path.suffix + '.rebind-tmp')
    temporary.write_text(json.dumps(payload, indent=2, sort_keys=True) + '\n', encoding='utf-8')
    os.replace(temporary, path)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--apply', action='store_true', help='write; otherwise report only')
    parser.add_argument('--verify', metavar='TASK', nargs='+', default=None,
                        help='verify these tasks bind cleanly and exit')
    args = parser.parse_args()

    audit = load('audit_readiness')
    binding_module = load('readiness_binding')
    config = load('formal_config')
    exclusions = set(audit.SMOKE_DIGEST_EXCLUSIONS)

    registry_path = CONTROL / 'configuration_delta_registry.json'
    snapshot_path = CONTROL / 'post_repair_tree_snapshot.json'

    if args.verify:
        verify(args.verify, config, audit, binding_module, registry_path)
        return 0

    with (CONTROL / 'configuration_delta_registry.lock').open('a') as lock:
        fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
        registry = json.loads(registry_path.read_bytes())
        snapshot = json.loads(snapshot_path.read_bytes())

        file_drift, tree_drift, missing = [], [], []
        for task, row in sorted(registry['tasks'].items()):
            for ref in row.get('effective_source_files', []):
                path = Path(ref['path'])
                if not path.is_file():
                    missing.append((task, str(path)))
                    continue
                actual = sha(path)
                if actual != ref['sha256']:
                    file_drift.append((task, str(path), ref['sha256'], actual))
                    ref['sha256'] = actual

        for task, source in sorted(config.TASKS.items()):
            source = Path(source)
            digest = audit.tree_digest(source)
            row = snapshot['tasks'][task]['sibling']
            if row['digest'] != digest:
                files, total = measure_tree(source, exclusions)
                tree_drift.append((task, row['digest'], digest, row['files'], files))
                row.update(digest=digest, files=files, bytes=total)

        if missing:
            for task, path in missing:
                print('MISSING  %-12s %s' % (task, path))
            raise SystemExit('refusing to rebind while a referenced source file is absent')

        for task, path, before, after in file_drift:
            print('file  %-12s %s\n        %s -> %s' % (task, path.replace(
                '@@AGENTSWE_EDITING_SOURCES@@/', ''), before[:16], after[:16]))
        for task, before, after, old_files, new_files in tree_drift:
            print('tree  %-12s %s -> %s  (%d -> %d files)' % (task, before[:16], after[:16], old_files, new_files))
        if not file_drift and not tree_drift:
            print('no drift; registry and snapshot already describe the current sources')
            return 0
        print('\n%d file reference(s), %d task tree(s)' % (len(file_drift), len(tree_drift)))
        if not args.apply:
            print('dry run; pass --apply to publish')
            return 0

        write_atomic(registry_path, registry)
        write_atomic(snapshot_path, snapshot)
        print('\npublished registry %s  snapshot %s' % (sha(registry_path)[:16], sha(snapshot_path)[:16]))
        affected = sorted({task for task, *_ in file_drift} | {task for task, *_ in tree_drift})

    # verify_binding opens this same lock file again and asks for a shared lock.
    # flock is held per open file description, so requesting it while this
    # process still holds the exclusive lock deadlocks the process against
    # itself. The write above is already published and atomic, so verification
    # belongs out here, after the exclusive window has closed.
    verify(affected, config, audit, binding_module, registry_path)
    return 0


def verify(tasks, config, audit, binding_module, registry_path):
    """Re-check through the real dispatch path rather than trusting the arithmetic."""
    for task in tasks:
        source = Path(config.TASKS[task])
        measured = {'task': task, 'source_digest': audit.tree_digest(source),
                    'contract_digest': sha(source / 'meta/0905_case_contract.json'),
                    'registry_digest': binding_module.registry_digest(registry_path, task)}
        binding_module.verify_binding(source, measured)
        print('verified %-12s source=%s registry=%s' % (
            task, measured['source_digest'][:16], measured['registry_digest'][:16]))


if __name__ == '__main__':
    raise SystemExit(main())
