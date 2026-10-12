#!/usr/bin/env python3
"""Take one terminal readiness run from run artifacts to a gate admission.

Runs the three coordinator steps in order and stops at the first that refuses,
printing exactly what it refused on. Nothing here promotes a task by hand: the
admission is written by readiness_coordinator under its own lock and is rolled
back by that module if the recomputed audit does not make the row READY.
"""
from __future__ import annotations
import argparse, hashlib, importlib.util, json, subprocess, sys
from pathlib import Path

CONTROL = Path('@@AGENTSWE_EDITING_CONTROL@@')
TOOLS = Path('@@AGENTSWE_EDITING_TOOLS@@')


def sha(p): return hashlib.sha256(Path(p).read_bytes()).hexdigest()


def run(argv, **kw):
    print('  $', ' '.join(str(a) for a in argv))
    p = subprocess.run([str(a) for a in argv], capture_output=True, text=True, **kw)
    out = (p.stdout or '').strip(); err = (p.stderr or '').strip()
    if out: print('   ', out[-1200:].replace('\n', '\n    '))
    if p.returncode and err: print('    ! ', err[-1200:].replace('\n', '\n      '))
    return p


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--task', required=True)
    ap.add_argument('--run-dir', required=True)
    ap.add_argument('--unit', required=True)
    ap.add_argument('--binding-file', required=True)
    ap.add_argument('--apply', action='store_true')
    args = ap.parse_args()
    # readiness_finalize binds the unit exit receipt to the bare unit name; accept the systemd form too
    args.unit = args.unit.removesuffix('.service')
    sys.path.insert(0, str(CONTROL))
    import formal_config as cfg
    source = Path(cfg.TASKS[args.task])
    binding_sha = sha(args.binding_file)

    print('== step 1: readiness_finalize (cleanup + bundle export + validation)')
    p = run([sys.executable, CONTROL / 'readiness_finalize.py', '--task', args.task,
             '--run-dir', args.run_dir, '--unit', args.unit,
             '--binding-file', args.binding_file, '--binding-sha256', binding_sha],
            cwd=str(CONTROL))
    if p.returncode:
        return 2
    receipt = json.loads(p.stdout.strip().splitlines()[-1])
    bundle_root = receipt['bundle_root']
    print('   bundle_root =', bundle_root, ' validation_passed =', receipt.get('validation_passed'))

    print('== step 2: mechanical independent review')
    review_path = Path(args.run_dir) / 'readiness_independent_review.json'
    p = run([sys.executable, TOOLS / 'readiness_review.py', '--task', args.task,
             '--bundle-root', bundle_root, '--source', source, '--out', review_path])
    if p.returncode:
        return 3

    print('== step 3: coordinator admission (locked, atomic, self-rolling-back)')
    if not args.apply:
        print('   dry run: not writing an admission')
        return 0
    p = run([sys.executable, CONTROL / 'readiness_coordinator.py', '--task', args.task,
             '--bundle-root', bundle_root, '--review-report', review_path,
             '--review-sha256', sha(review_path)], cwd=str(CONTROL))
    return 0 if p.returncode == 0 else 4


raise SystemExit(main())
