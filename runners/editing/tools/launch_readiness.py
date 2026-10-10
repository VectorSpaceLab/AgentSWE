#!/usr/bin/env python3
"""Launch one v2 readiness canary for any wired task, on the corrected transport.

Optional flags differ per task, so the launcher reads the task's own
formal_one_stop.py and passes only the flags that file actually declares.
Binding is verified under the coordinator lock before dispatch; a fresh run and
evidence directory are required, and nothing historical is reused.
"""
from __future__ import annotations
import argparse, os, hashlib, json, os, re, subprocess, sys
from pathlib import Path

TOOLS = Path(__file__).resolve().parent
CONTROL = Path('@@AGENTSWE_EDITING_CONTROL@@')
PROFILE = 'single-dev-two-round-hidden-smoke-v1'
CREDENTIAL = '@@AGENTSWE_CREDENTIAL_FILE@@'
HARBOR = '@@AGENTSWE_HARBOR_BIN@@'
BUILDER_IMAGE = 'agentswe/create-agent-builder-codex:0.144.1-node24.6.0-0812'
LOWER_IMAGE = 'agentswe/edit-candidate-python311:0826'
# Environment a task requires before it will dispatch at all. Values are paths
# to evaluator-owned files plus their digests, never credentials.
TASK_ENVIRONMENT = {
    'deeptutor': {
        'AGENTSWE_DEEPTUTOR_PRIOR_PRODUCT_GUARD':
            '@@AGENTSWE_EDITING_STATE@@/deeptutor-prior-product-guard.json',
    },
    # Release: readiness uses the same pinned runtime venv as the formal command
    # (formal_commands.DEEPCODE_PYTHON). Without it formal_one_stop prepares a
    # run-local venv with an unpinned pip install from the network, as the
    # paper-era readiness runs did.
    'deepcode': {
        'DEEPCODE_PYTHON': '@@AGENTSWE_ENVS@@/deepcode-runtime-venv/bin/python',
    },
}
# Variables whose value is the SHA256 of the file named by another variable.
TASK_ENVIRONMENT_DIGESTS = {
    'deeptutor': {'AGENTSWE_DEEPTUTOR_PRIOR_PRODUCT_GUARD_SHA256':
                  'AGENTSWE_DEEPTUTOR_PRIOR_PRODUCT_GUARD'},
}

HIDDEN_DIRS = {
    'dyad': '@@AGENTSWE_EDITING_RUNS@@/formal/evaluator-issued/dyad/0905-edit-codex-xhigh-0920-hardened-001-dyad',
    'claude': '@@AGENTSWE_EDITING_RUNS@@/formal/evaluator-issued/claude/0905-edit-codex-xhigh-0920-hardened-003-claude',
}


def serialize(value) -> str:
    return json.dumps(value, indent=2, ensure_ascii=False) + '\n'


def write_json(path: Path, value) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + '.tmp')
    tmp.write_text(serialize(value))
    os.replace(tmp, path)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument('--task', required=True)
    ap.add_argument('--tag', default='0915-p1-canary-001')
    ap.add_argument('--proxy', default='http://127.0.0.1:7890')
    ap.add_argument('--dry-run', action='store_true')
    args = ap.parse_args()

    sys.path.insert(0, str(CONTROL))
    import formal_config as cfg
    from audit_readiness import tree_digest
    from readiness_binding import (verify_binding, registry_digest, REQUEST_MAX_RETRIES,
                                   STREAM_MAX_RETRIES, STREAM_IDLE_TIMEOUT_MS)

    task = args.task
    source = Path(cfg.TASKS[task])
    one_stop = source / 'harbor/formal_one_stop.py'
    declared = one_stop.read_text(errors='replace')

    # Asking the source text whether a flag exists finds it in places argparse
    # never sees: ai-scientist embeds a Codex wrapper whose own option table
    # contains "--image", so a text match passed --image to a parser that only
    # declares --builder-image, and the run died on argv before it started.
    # Ask the parser instead.
    help_text = subprocess.run(
        ['/usr/bin/python3', '-E', '-s', '-B', str(one_stop), '--help'],
        capture_output=True, text=True, timeout=180).stdout

    def has(flag):
        return re.search(r'(?<![\w-])' + re.escape(flag) + r'(?![\w-])', help_text) is not None

    run = Path(cfg.SMOKE_ROOT) / task / args.tag
    evidence = Path('@@AGENTSWE_LEGACY_DATA@@/0915-edit') / (task + '-' + args.tag)
    unit = 'agentswe-edit-%s-%s' % (task, args.tag)
    if run.exists() or evidence.exists():
        raise SystemExit('fresh run or evidence path already exists: %s / %s' % (run, evidence))

    contract = source / 'meta/0905_case_contract.json'
    binding = {'task': task, 'source_digest': tree_digest(source),
               'contract_digest': hashlib.sha256(contract.read_bytes()).hexdigest(),
               'registry_digest': registry_digest(CONTROL / 'configuration_delta_registry.json', task)}
    if verify_binding(source, binding, control_root=CONTROL) != binding:
        raise SystemExit('binding verification failed')

    binding_path = evidence / 'readiness_current_binding.json'
    if args.dry_run:
        # Plan only: the same bytes write_json would produce, written nowhere,
        # so the tag stays free for the launch this is a rehearsal for.
        binding_sha = hashlib.sha256(serialize(binding).encode()).hexdigest()
    else:
        evidence.mkdir(parents=True)
        write_json(binding_path, binding)
        binding_sha = hashlib.sha256(binding_path.read_bytes()).hexdigest()
        if verify_binding(source, json.loads(binding_path.read_text()), control_root=CONTROL) != binding:
            raise SystemExit('persisted binding verification failed')

    argv = ['/usr/bin/python3', '-E', '-s', '-B', str(one_stop), '--pilot']
    if has('--benchmark'):
        # Some tasks require the benchmark root explicitly rather than deriving it.
        argv += ['--benchmark', str(source)]
    if has('--readiness-profile'):
        argv += ['--readiness-profile', PROFILE]
    if has('--readiness-binding-file'):
        argv += ['--readiness-binding-file', str(binding_path)]
    if has('--readiness-binding-sha256'):
        argv += ['--readiness-binding-sha256', binding_sha]
    argv += ['--run-dir', str(run), '--harbor', HARBOR, '--credential-file', CREDENTIAL]
    if has('--builder-transport'):
        argv += ['--builder-transport', 'direct']
    if has('--builder-base-url'):
        argv += ['--builder-base-url', os.environ.get('AGENTSWE_BUILDER_BASE_URL', 'https://api.deepseek.com/v1')]
    if has('--builder-proxy'):
        # Every task defaults this to the mihomo loopback proxy, so omitting it
        # selects mihomo rather than direct egress. Say the empty value out loud.
        argv += ['--builder-proxy', args.proxy]
    if has('--image'):
        argv += ['--image', BUILDER_IMAGE]
    elif has('--builder-image'):
        argv += ['--builder-image', BUILDER_IMAGE]
    if has('--lower-image'):
        argv += ['--lower-image', LOWER_IMAGE]
    # ai-scientist refuses a budget that does not cover its generated task
    # timeout plus the cleanup margin, and says the required number out loud.
    timeout = '28920' if task == 'ai-scientist' else '28800'
    argv += ['--builder-timeout', timeout, '--max-dev-rounds', '2', '--n-concurrent', '1']
    if has('--hidden-cases-dir') and task in HIDDEN_DIRS:
        argv += ['--hidden-cases-dir', HIDDEN_DIRS[task]]

    preflight = {'schema_version': 'agentswe-edit-v2-binding-preflight-v1', 'task': task,
                 'unit': unit + '.service', 'run_dir': str(run), 'profile': PROFILE,
                 'binding_file': str(binding_path), 'binding_sha256': binding_sha, **binding,
                 'builder_model': 'deepseek-flash', 'builder_reasoning_effort': 'max',
                 'lower_model': 'deepseek-flash', 'lower_reasoning_effort': 'high',
                 'public_cases': ['dev_001'], 'hidden_cases': ['test_001'],
                 'required_valid_rounds': 2,
                 'native_request_max_retries': REQUEST_MAX_RETRIES,
                 'native_stream_max_retries': STREAM_MAX_RETRIES,
                 'native_stream_idle_timeout_ms': STREAM_IDLE_TIMEOUT_MS,
                 'builder_proxy_url': args.proxy,
                 'builder_egress': 'direct' if not args.proxy else 'evaluator-loopback-proxy',
                 'phase': 'phase1+phase3-corrected-transport-and-admission',
                 'reuses_historical_candidate_or_request': False,
                 'provider_calls': 0, 'registry_written': False, 'gate_written': False}
    print('unit  =', unit)
    print('run   =', run)
    print('argv  =', ' '.join(argv))
    if args.dry_run:
        print('dry run; nothing written')
        return 0
    write_json(evidence / 'binding-preflight.json', preflight)
    write_json(evidence / 'launch-argv.json', {'argv': argv, **preflight})
    environment = dict(TASK_ENVIRONMENT.get(task, {}))
    for name, source in TASK_ENVIRONMENT_DIGESTS.get(task, {}).items():
        referenced = Path(environment[source])
        if referenced.is_symlink() or not referenced.is_file():
            raise SystemExit('required task environment file is missing: %s' % referenced)
        environment[name] = hashlib.sha256(referenced.read_bytes()).hexdigest()
    setenv = ['--setenv=%s=%s' % item for item in sorted(environment.items())]
    if setenv:
        print('env   =', ' '.join(sorted(environment)))
    subprocess.run(['systemd-run', '--unit=' + unit, *setenv,
                    '--description=AgentSWE edit %s readiness canary %s' % (task, args.tag),
                    '--property=Type=simple', '--property=KillMode=mixed',
                    '--property=TimeoutStopSec=60',
                    '--property=StandardOutput=append:%s' % (evidence / 'orchestrator.log'),
                    '--property=StandardError=append:%s' % (evidence / 'orchestrator.log'),
                    '--setenv=PYTHONUNBUFFERED=1',
                    # systemd clears a successful unit's ExecMainPID and exit
                    # timestamp, so a coordinator arriving later finds no
                    # terminal evidence at all. ExecStopPost still has the facts
                    # in its environment; record them beside the run. Purely
                    # additive: no check reads this yet.
                    '--property=ExecStopPost=/usr/bin/python3 -E -s -B %s %s %s'
                    % (TOOLS / 'unit_exit_receipt.py', run, unit),
                    '--setenv=PYTHONDONTWRITEBYTECODE=1', *argv],
                   check=True)
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
