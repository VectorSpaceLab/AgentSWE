#!/usr/bin/env python3
"""Record a unit's own terminal facts at the moment it exits.

systemd clears ExecMainPID and ExecMainExitTimestamp when a unit exits
successfully -- measured here across transient and non-transient units, Type
simple and oneshot, KillMode process and mixed -- so by the time a coordinator
looks, a successful run's terminal evidence is gone. A failed run keeps it,
which is why only failed runs can be finalized today.

systemd hands ExecStopPost the facts directly, in the environment, while they
still exist: $SERVICE_RESULT, $EXIT_CODE, $EXIT_STATUS, $MAINPID. This writes
them next to the run, once, immutably.

This adds evidence; it does not relax any check. Nothing reads this file yet --
whether require_terminal_unit should is the operator's call.
"""
from __future__ import annotations

import json
import os
import socket
import subprocess
import sys
import time
from pathlib import Path

# What validate_plan compares when a finalize is retried. systemd clears the
# runtime fields inside ExecStart on any daemon-reload -- measured: pid and
# status go to 0 while ExecMainPID stays -- so a snapshot taken here, inside the
# exiting unit, is the only durable copy.
UNIT_PROPERTIES = ('LoadState', 'ActiveState', 'SubState', 'MainPID', 'ExecMainPID',
                   'ExecMainStatus', 'ExecMainStartTimestamp', 'ExecMainExitTimestamp',
                   'ExecStart', 'InvocationID', 'Result')


def observe(unit):
    try:
        done = subprocess.run(['systemctl', 'show', unit, *sum((('-p', k) for k in UNIT_PROPERTIES), ())],
                              capture_output=True, text=True, timeout=20)
    except (OSError, subprocess.SubprocessError):
        return None
    if done.returncode:
        return None
    return dict(line.split('=', 1) for line in done.stdout.splitlines() if '=' in line)


def main() -> int:
    if len(sys.argv) != 3:
        print('usage: unit_exit_receipt.py <run-dir> <unit>', file=sys.stderr)
        return 2
    run, unit = Path(sys.argv[1]), sys.argv[2]
    if not run.is_dir():
        return 0
    target = run / 'unit_exit_receipt.json'
    if target.exists():
        # A run exits once; never overwrite the first account of how.
        return 0
    value = {
        'schema_version': 'agentswe-unit-exit-receipt/v1',
        'unit': unit,
        'run_id': run.name,
        'owner': 'evaluator',
        'service_result': os.environ.get('SERVICE_RESULT'),
        'exit_code': os.environ.get('EXIT_CODE'),
        'exit_status': os.environ.get('EXIT_STATUS'),
        'main_pid': os.environ.get('MAINPID'),
        'invocation_id': os.environ.get('INVOCATION_ID'),
        'recorded_at': time.strftime('%Y-%m-%dT%H:%M:%S%z'),
        'recorded_by': socket.gethostname(),
        'recorded_from': 'systemd ExecStopPost, in the unit that owned the run',
        # Snapshot of the properties a retry compares, taken while they exist.
        'unit_properties_at_exit': observe(unit),
    }
    tmp = target.with_name(target.name + '.tmp')
    tmp.write_text(json.dumps(value, indent=2, ensure_ascii=False) + '\n')
    os.replace(tmp, target)
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
