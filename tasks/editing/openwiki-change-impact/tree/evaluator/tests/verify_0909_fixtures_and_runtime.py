"""Provider-free real native addon/build sandbox and all eight initial worlds."""
from __future__ import annotations
import argparse
import json
import os
import sys
from pathlib import Path
ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
from agentloop.candidate_adapter import _run, TASK_NODE
from agentloop.evaluator.lower_agent_launcher import runtime_preflight
from agentloop.evaluator.fixture_service import materialize_runtime_case, _load_spec
from agentloop.evaluator.semantic_oracle import observe
from agentloop.protocol import tree_digest


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    output = args.output.resolve()
    output.mkdir(parents=True, exist_ok=False)
    native_dir = output / 'native'
    native_dir.mkdir()
    native = runtime_preflight(ROOT / '.runtime/candidate-smoke/repository', native_dir)
    work, state = output / 'build-probe/candidate', output / 'build-probe/state'
    work.mkdir(parents=True)
    state.mkdir(parents=True)
    canary = output / 'evaluator-private-canary.txt'
    canary.write_text('synthetic access boundary probe; not a real credential')
    code = "const fs=require('fs'); console.log(JSON.stringify({privateVisible:fs.existsSync(" + json.dumps(str(canary)) + "),inheritedSecret:('AGENTSWE_SYNTHETIC_SECRET' in process.env),node:process.version}));"
    os.environ['AGENTSWE_SYNTHETIC_SECRET'] = 'synthetic-test-only'
    try:
        build = _run([str(TASK_NODE), '-e', code], work, 30, state_dir=state)
    finally:
        os.environ.pop('AGENTSWE_SYNTHETIC_SECRET', None)
    actual = json.loads(build['stdout_tail']) if build['exit_code'] == 0 else {}
    cases = {}
    for case_id in ('dev_001', 'dev_002', *(f'test_{i:03d}' for i in range(1, 7))):
        group = ROOT / ('dev_cases' if case_id.startswith('dev') else 'test_cases')
        runtime = materialize_runtime_case(case_id, group, output / 'fixtures' / case_id)
        before = tree_digest(runtime['repository'])
        comparison = observe(case_id, group, runtime['repository'], runtime['repository'])
        after = tree_digest(runtime['repository'])
        spec = _load_spec(case_id, group)
        tx = spec.get('transaction', {})
        claims = runtime['repository'] / tx.get('state_dir', '.openwiki-impact') / 'tenants' / str(tx.get('tenant_id')) / 'claims'
        seeded = True
        seed_checks = {}
        if spec.get('preseed'):
            claim_path = claims / (str(tx.get('request_id')) + '.json')
            claim = json.loads(claim_path.read_text()) if claim_path.is_file() else {}
            staged = claims.parent / 'staging' / str(tx.get('request_id')) / 'openwiki/incomplete.md'
            seed_checks = {'exact_request_claim_exists': claim_path.is_file(),
                'expired_owner': claim.get('owner_id') == 'terminated-owner' and claim.get('lease_expires_at') == '2000-01-01T00:00:00.000Z',
                'correct_phase': claim.get('phase') == ('prepared' if spec['preseed'] == 'expired_prepared_corrupt_stage' else 'claimed'),
                'tenant_request_generation_payload_binding': all(claim.get(key) == tx.get(key) for key in ('tenant_id', 'request_id', 'generation', 'payload_digest')),
                'actual_truncated_stage_bytes': staged.is_file() and staged.read_text() == 'TRUNCATED-UNTRUSTED-STAGING\n'}
            seeded = all(seed_checks.values())
        result = {'case_id': case_id, 'change_kind': runtime['change_source_kind'],
            'oracle_read_only': before == after, 'semantic_checks': len(comparison['semantic_comparisons']),
            'preseed_mode': spec.get('preseed'), 'real_initial_failure_state_present': seeded,
            'preseed_checks': seed_checks,
            'actual_initial_tree_digest': before}
        cases[case_id] = result
        (output / 'fixtures' / case_id / 'initial_semantic_comparison.json').write_text(json.dumps(comparison, indent=2) + '\n')
    checks = {'real_Node22_native_SQLite_probe_in_sandbox': native.get('valid') is True,
        'build_child_cannot_read_evaluator_private_file': actual.get('privateVisible') is False,
        'build_child_does_not_inherit_secrets': actual.get('inheritedSecret') is False,
        'eight_real_worlds_materialized': len(cases) == 8,
        'all_oracles_read_only': all(item['oracle_read_only'] for item in cases.values()),
        'all_cases_have_semantic_comparisons': all(item['semantic_checks'] >= 10 for item in cases.values()),
        'declared_failure_states_actually_seeded': all(item['real_initial_failure_state_present'] for item in cases.values())}
    result = {'checks': checks, 'passed': all(checks.values()), 'provider_calls': 0,
        'product_workflow_executed_by_evaluator': False, 'native_probe': native, 'build_probe': build, 'cases': cases}
    (output / 'verification.json').write_text(json.dumps(result, indent=2) + '\n')
    print(json.dumps({'passed': result['passed'], 'checks': checks, 'cases': cases}, indent=2))
    return 0 if result['passed'] else 1


if __name__ == '__main__':
    raise SystemExit(main())
