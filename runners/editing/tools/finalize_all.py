#!/usr/bin/env python3
"""Finalize every terminal run of one tag, and say plainly what each one did.

readiness_finalize does cleanup, then export, then validation, and raises on the
first thing that is not right. Run per task it is a one-line answer; run over a
tag it is the picture of where the batch actually stands.

Refuses while a run it is about to walk is still live -- finalize on a running
unit is exactly what require_terminal_unit exists to prevent. With --only, a
sibling task of the same tag that is still running does not block, because
cleanup is scoped to a run's own resources and each task binds to its own
configuration row.

Nothing here is retried and nothing is forced. A task that fails is reported
with the reason and left alone -- a failed export can be retried later in a
fresh directory once the cause is fixed, which is what readiness_finalize's own
attempt directories are for.
"""
from __future__ import annotations

import argparse
import hashlib
import subprocess
import sys
from pathlib import Path

TOOLS = Path(__file__).resolve().parent
CONTROL = Path('@@AGENTSWE_EDITING_CONTROL@@')
EVIDENCE = Path('@@AGENTSWE_LEGACY_DATA@@/0915-edit')
sys.path.insert(0, str(CONTROL))
import formal_config as cfg


def live(tag, tasks=None):
    out = subprocess.run(['systemctl', 'list-units', 'agentswe-edit-*.service', '--state=active',
                          '--no-pager', '--no-legend'], capture_output=True, text=True)
    units = [l.split()[0] for l in out.stdout.splitlines() if tag in l]
    if tasks is None:
        return units
    wanted = {'agentswe-edit-%s-%s.service' % (t, tag) for t in tasks}
    return [u for u in units if u in wanted]


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument('--tag', required=True)
    parser.add_argument('--only', nargs='*')
    args = parser.parse_args()

    tasks = sorted(cfg.TASKS)
    if args.only:
        tasks = [t for t in tasks if t in set(args.only)]

    # Only the runs about to be walked have to be terminal. A sibling task of the
    # same tag still running used to block this too, because cleanup compared the
    # whole machine and the binding covered all ten tasks at once; both are scoped
    # per run now, so a task that finished at minute 20 no longer waits for one
    # that needs three hours.
    running = live(args.tag, tasks if args.only else None)
    if running:
        print('refusing: %d selected run(s) of this tag are still live' % len(running))
        for unit in running:
            print('  %s' % unit)
        return 1
    if args.only:
        others = [u for u in live(args.tag) if u not in running]
        if others:
            print('note: %d other run(s) of this tag are live and do not block this finalize'
                  % len(others))

    ok = 0
    for task in tasks:
        run = Path(cfg.SMOKE_ROOT) / task / args.tag
        binding = EVIDENCE / ('%s-%s' % (task, args.tag)) / 'readiness_current_binding.json'
        if not run.is_dir():
            print('  %-13s no run for this tag' % task)
            continue
        if not binding.is_file():
            print('  %-13s no binding file; cannot finalize' % task)
            continue
        done = subprocess.run(
            ['/usr/bin/python3', '-E', '-s', '-B', str(CONTROL / 'readiness_finalize.py'),
             '--task', task, '--run-dir', str(run),
             '--unit', 'agentswe-edit-%s-%s' % (task, args.tag),
             '--binding-file', str(binding),
             '--binding-sha256', hashlib.sha256(binding.read_bytes()).hexdigest()],
            capture_output=True, text=True, cwd=str(CONTROL))
        if done.returncode == 0:
            ok += 1
            print('  %-13s FINALIZED' % task)
        else:
            tail = [l for l in (done.stderr or '').strip().splitlines() if l.strip()]
            why = tail[-1] if tail else (done.stdout or '').strip()[-120:]
            print('  %-13s %s' % (task, why[:118]))
    print('\n%d of %d finalized' % (ok, len(tasks)))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
