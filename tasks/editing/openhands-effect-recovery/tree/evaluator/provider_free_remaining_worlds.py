"""Bounded real preserved-product probes, never lower-agent acceptance."""
import argparse
import json
from pathlib import Path
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[1]


def action(name, **arguments): return {'kind': 'action', 'action': name, 'arguments': arguments}


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--candidate', type=Path, required=True)
    parser.add_argument('--case', choices=['test_005', 'test_006'], required=True)
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    args.output.mkdir(parents=True, exist_ok=False)
    scope = {'backendId': '$ref.scope.backendId', 'conversationId': '$ref.scope.conversationId'}
    if args.case == 'test_005':
        choices = [action('recover_conversation', **scope, ownerId='offline-migration-owner',
            expectedRevision=1, workspace='$ref.state.workspace', ttlMs=5000),
            action('restart_product'), action('inspect_recovery', **scope),
            action('open_production_dispatch_session'), action('dispatch_production_event'),
            action('inspect_recovery', **scope)]
    else:
        choices = [action('create_checkpoint', **scope, eventCursor=0,
                    workspace={'digest': 'sha256:' + 'a' * 64, 'version': 'git:provider-free'}),
            action('acquire_recovery_lease', **scope, ownerId='offline-sync-owner', expectedRevision='$ref.state.revision', ttlMs=5000),
            *[action(name) for name in ('start_production_sync', 'sync_production', 'sync_production',
              'freeze_production_sync', 'resume_production_sync', 'sync_production',
              'reconcile_production_workspace', 'reconcile_production_workspace',
              'inspect_production_workspace', 'inspect_production_sync')]]
    with (args.output / 'diagnostic_actions.json').open('x') as handle: json.dump(choices, handle, indent=2)
    plan = {'role': 'evaluator-only real product/world probe, not a model trajectory',
        'case_id': args.case, 'operations': len(choices), 'provider_calls': 0, 'score': None,
        'max_wall_seconds': 600, 'formal_result_publishable': False, 'acceptance_result_publishable': False}
    (args.output / 'diagnostic_plan.json').write_text(json.dumps(plan, indent=2) + '\n')
    command = [sys.executable, str(ROOT / 'lower_agent/openhands_lower_agent.py'),
        '--repository', str(args.candidate), '--case', str(ROOT / 'test_cases' / args.case),
        '--broker-endpoint', 'http://unreachable.invalid/v1/responses', '--output', str(args.output),
        '--node-modules', '@@AGENTSWE_ENVS@@/openhands-effect-recovery-ledger-edit-v1/baseline-install/node_modules',
        '--timeout', '600', '--provider-free-actions', str(args.output / 'diagnostic_actions.json')]
    return subprocess.run(command, check=False).returncode


if __name__ == '__main__': raise SystemExit(main())
