#!/usr/bin/env python3
"""Per-task formal launcher.

Launches ONE task's formal branch (`formal_one_stop.py --run-formal`, 6 hidden
cases, Result axis only) as a systemd transient unit, gated only by THIS task's
readiness (patched `readiness_admission.require_formal_readiness`) and its own
input prerequisites (`formal_commands.branch_input_evidence`).  Does not require
the other nine tasks to be READY.  Usage:

  python3 launch_formal_task.py --task aider --label f-001 [--dry-run]
  python3 launch_formal_task.py --task claude --label f-001 --issue-hidden-from repair-001

--issue-hidden-from copies the evaluator-issued hidden bundle of an earlier label
(claude / dyad only need it; their hidden bundle path derives from the run id).

--integrity-only (what `agentswe run` passes for a formal run): the readiness admission is a benchmark-construction
gate, so a release install launches without one. The launcher still refuses a task tree that differs from the
snapshot setup recorded, or a release template that changed (the same digest checks as the readiness gate), and sets
AGENTSWE_EDITING_FORMAL_GATE=release-integrity on the formal unit so that the one-stops which re-check the gate
themselves (readiness_admission.require_formal_readiness) apply the same source checks.

The Builder provider URL comes from AGENTSWE_BUILDER_BASE_URL (set by `agentswe run` from the configured BUILDER
role) as --builder-base-url, for every one-stop whose parser declares it, as the readiness launcher does.
"""
import argparse, hashlib, importlib.util, json, os, re, subprocess, sys, shutil
from pathlib import Path
CONTROL = Path('@@AGENTSWE_EDITING_CONTROL@@')
sys.path.insert(0, str(CONTROL))
import formal_config as cfg  # noqa: E402
import formal_commands as fc  # noqa: E402
from control_runtime import control_command  # noqa: E402
TOOLS = Path(__file__).resolve().parent
sys.path.insert(0, str(TOOLS))
from formal_unit_env import FORMAL_GATE_RELEASE, TaskEnvironmentMissing, unit_environment  # noqa: E402

def sha(p): return hashlib.sha256(Path(p).read_bytes()).hexdigest()


def declares(help_text, flag):
    """Whether a one-stop's --help names the option (its parser, not its source text: an embedded tool's option
    table would match a text search; see launch_readiness.py)."""
    return re.search(r'(?<![\w-])' + re.escape(flag) + r'(?![\w-])', help_text) is not None


def builder_provider_args(help_text, environ):
    """--builder-base-url <configured Builder URL> when the one-stop declares it. Without it a formal one-stop used its
    default provider URL whatever the BUILDER role named."""
    url = (environ.get('AGENTSWE_BUILDER_BASE_URL') or '').strip()
    return ['--builder-base-url', url] if url and declares(help_text, '--builder-base-url') else []


def one_stop_help(one_stop):
    return subprocess.run([*control_command(cfg.CONTROL_PYTHON, one_stop), '--help'],
                          capture_output=True, text=True, timeout=180).stdout


def load_admission():
    spec = importlib.util.spec_from_file_location('readiness_admission', CONTROL / 'readiness_admission.py')
    adm = importlib.util.module_from_spec(spec); spec.loader.exec_module(adm)
    return adm


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--task', required=True, choices=sorted(cfg.TASKS))
    ap.add_argument('--label', required=True, help='attempt label, e.g. f-001')
    ap.add_argument('--credential-file', type=Path, default=Path('@@AGENTSWE_CREDENTIAL_FILE@@'))
    ap.add_argument('--issue-hidden-from', default=None, help='copy claude/dyad hidden bundle from this earlier label')
    ap.add_argument('--skip-audit', action='store_true', help='do not re-run audit_readiness before the per-task gate')
    ap.add_argument('--integrity-only', action='store_true',
                    help='release install: check the task tree against the setup snapshot, no readiness admission')
    ap.add_argument('--dry-run', action='store_true')
    a = ap.parse_args()
    task, sibling = a.task, Path(cfg.TASKS[a.task])
    launch_id = f'0905-edit-codex-xhigh-{a.label}'
    run_id = f'{launch_id}-{task}'
    run_dir = cfg.FORMAL_ROOT / 'codex_xhigh' / task / run_id
    if run_dir.exists():
        raise SystemExit(f'run directory already exists: {run_dir}')
    if task in {'claude', 'dyad'} and a.issue_hidden_from:
        src = fc.ISSUED_ROOT / task / f'0905-edit-codex-xhigh-{a.issue_hidden_from}-{task}'
        dst = fc.hidden_bundle_path(task, run_dir)
        if not dst.exists():
            if a.dry_run: print('would copy hidden bundle', src, '->', dst)
            else:
                shutil.copytree(src, dst); print('issued hidden bundle', dst)
    if a.integrity_only:
        # release install: source integrity only (no readiness admission, so no gate-file refresh either). The
        # same mode reaches the formal unit, whose one-stop re-checks the gate in-tree (DeepCode, DeepTutor, Dyad,
        # OpenWiki call readiness_admission.require_formal_readiness under --run-formal).
        os.environ['AGENTSWE_EDITING_FORMAL_GATE'] = FORMAL_GATE_RELEASE
        gate = load_admission().require_formal_readiness(sibling)
        print('release integrity OK:', json.dumps({k: gate[k] for k in ('task', 'sibling_digest', 'readiness')}))
    else:
        os.environ.pop('AGENTSWE_EDITING_FORMAL_GATE', None)  # the full gate: no release mode leaks in from outside
        # 1. refresh the gate file (all-task audit; its exit code is NOT a gate here)
        if not a.skip_audit:
            audit = subprocess.run([sys.executable, '-E', '-s', '-B', str(CONTROL / 'audit_readiness.py')], capture_output=True, text=True)
            print('audit_readiness exit', audit.returncode, '(informational; per-task gate follows)')
        # 2. per-task gate: this task READY + current source == reviewed snapshot + admission valid
        gate = load_admission().require_formal_readiness(sibling)
        print('per-task gate OK:', json.dumps({k: gate[k] for k in ('task', 'sibling_digest', 'readiness')}))
    # 3. command + prerequisites
    command = fc.build_formal_command(task, sibling, run_dir, a.credential_file.resolve(), cfg.CONTROL_PYTHON)
    if os.environ.get('AGENTSWE_BUILDER_BASE_URL'):
        command += builder_provider_args(one_stop_help(sibling / 'harbor/formal_one_stop.py'), os.environ)
    inputs = fc.branch_input_evidence(task, sibling, run_dir)
    unit = f'agentswe-formal-{task}-{a.label}'
    control = cfg.FORMAL_ROOT / 'launch_control' / launch_id / task
    record = {'schema_version': 'agentswe-edit-per-task-formal-launch/v1', 'task': task, 'label': a.label, 'run_id': run_id,
              'run_dir': str(run_dir), 'unit': unit, 'command': command, 'gate': gate, 'input_evidence': inputs,
              'result_judge_sha256': sha(cfg.RESULT_JUDGE), 'sibling_digest': gate['sibling_digest'],
              'max_dev_rounds': 5, 'builder_timeout_seconds': 18000, 'code_axis': 'skipped_by_policy',
              'early_stop_resample': 'enabled', 'early_stop_resample_scope': 'formal_only',
              'early_stop_resample_max_extra_logical_requests_per_case': 1}
    print('argv  =', ' '.join(command))
    print('run   =', run_dir)
    if a.dry_run:
        print('dry run; not launched'); return 0
    # The unit's environment (formal_unit_env.py; never a credential), recorded so that a later budget freeze of this
    # run (budget_freeze.py) runs its held-out cases and finalizer with the same variables.
    try:
        env = unit_environment(task, cfg, integrity_only=a.integrity_only)
    except TaskEnvironmentMissing as exc:
        print('task environment file missing:', exc); return 1
    record['unit_environment'] = env
    control.mkdir(parents=True, exist_ok=True)  # after the dry-run return: a dry run leaves nothing behind
    (control / 'launch_record.json').write_text(json.dumps(record, indent=2) + '\n')
    run_dir.parent.mkdir(parents=True, exist_ok=True)
    subprocess.run(['systemd-run', '--unit=' + unit, *['--setenv=%s=%s' % kv for kv in env.items()],
                    '--description=AgentSWE edit %s FORMAL %s' % (task, a.label),
                    '--property=Type=simple', '--property=KillMode=mixed', '--property=TimeoutStopSec=60',
                    '--property=StandardOutput=append:%s' % (control / 'orchestrator.log'),
                    '--property=StandardError=append:%s' % (control / 'orchestrator.log'),
                    '--property=WorkingDirectory=%s' % sibling, *command], check=True)
    print('launched', unit, '->', run_dir); return 0

if __name__ == '__main__':
    sys.exit(main())
