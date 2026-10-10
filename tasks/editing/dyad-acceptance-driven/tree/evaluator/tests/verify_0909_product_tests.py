"""Run actual product Tests owner/Stop against a local browser, with no model."""
from __future__ import annotations
import argparse
import json
from pathlib import Path
import subprocess
import sys
import time

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / 'evaluator'))
from scenario_contract import prepare_scenario
PRODUCT = Path('@@AGENTSWE_ENVS@@/dyad-task-env-cycle-006')
TASK = Path('@@AGENTSWE_EDITING_TASKS@@/dyad-acceptance-driven/tree')


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--cases', nargs='+', default=['dev_001', 'test_002', 'test_005', 'dev_002'])
    args = parser.parse_args()
    args.output.mkdir(parents=True, exist_ok=False)
    records = []
    for case_id in args.cases:
        case = args.output / case_id
        case.mkdir()
        deadline = time.monotonic() + 600
        source = TASK / ('dev_cases' if case_id.startswith('dev_') else 'test_cases') / case_id / 'input.md'
        fixture = prepare_scenario(case_id, source, case / 'fixture')
        command = [sys.executable, '-I', str(ROOT / 'environment/headless_chat_flow.py'),
            '--repository', str(PRODUCT), '--case', fixture['executed_task_path'], '--case-id', case_id,
            '--scenario-public', fixture['public_fixture_path'], '--output', str(case / 'launch.json'),
            '--artifact', str(case / 'agent-result.json'), '--native-evidence', str(case / 'native.json'),
            '--timeout', '600', '--case-deadline-monotonic', str(deadline), '--provider-free-browser-probe']
        proc = subprocess.run(command, text=True, capture_output=True, timeout=630, cwd='/',
            env={'PATH': '/usr/bin:/bin', 'LANG': 'C.UTF-8'})
        (case / 'controller.stdout.log').write_text(proc.stdout)
        (case / 'controller.stderr.log').write_text(proc.stderr)
        native = json.loads((case / 'native.json').read_text()) if (case / 'native.json').exists() else {}
        launch = json.loads((case / 'launch.json').read_text()) if (case / 'launch.json').exists() else {}
        resource_path = case / 'launch-resources/resource-attestation.json'
        resource = json.loads(resource_path.read_text()) if resource_path.exists() else {}
        probe = native.get('provider_free_probe') or {}
        browser = native.get('runner', {}).get('native_browser_executions', [])
        result = probe.get('real_product_Tests_result') or {}
        assertions = {
            'process_completed': proc.returncode == 0 and launch.get('exit_code') == 0,
            'actual_product_loaded': native.get('real_product') is True,
            'private_inputs_inaccessible': probe.get('private_fixture_module_visible') is False and probe.get('credential_file_visible') is False,
            'no_agent_artifact_manufactured': not (case / 'agent-result.json').exists(),
            'actual_browser_executed': bool(browser) and all(item.get('actual_test_count', 0) > 0 and item.get('infrastructure_invalid') is False for item in browser),
            'independent_actual_browser_recheck': (native.get('independent_behavior_recheck') or {}).get('native_behavior', {}).get('actual_test_count', 0) > 0 and (native.get('independent_behavior_recheck') or {}).get('native_behavior', {}).get('app_server_private_spec_isolated') is True,
            'resources_enforced_and_empty': resource.get('valid') is True and resource.get('memory_bytes') == 4294967296 and resource.get('cleanup', {}).get('complete') is True,
        }
        if case_id in {'dev_001', 'test_002'}:
            expected = 'failed' if case_id == 'dev_001' else 'passed'
            assertions['real_product_report_parser_observed_expected_behavior'] = bool(result.get('results')) and all(item.get('status') == expected for item in result['results'])
        if case_id in {'test_005', 'dev_002'}:
            assertions['actual_Stop_after_computed_report'] = probe.get('actual_report_before_stop') is True and bool(probe.get('actual_product_stop_receipt'))
            assertions['product_owned_signal_reached_browser_boundary'] = bool(browser) and all(item.get('signal_aborted_at_delivery') is True for item in browser)
        if case_id == 'test_006':
            cold = native.get('cold_restart') or {}
            assertions['actual_cold_restart_same_disk_fresh_namespace'] = cold.get('actual_fresh_process') is True and cold.get('previous_namespace') != cold.get('fresh_namespace') and cold.get('evaluator_rewrote_product_state') is False and cold.get('automatic_test_run_count_at_start') == 0
        if case_id == 'dev_002':
            assertions['product_prior_run_drained_before_successor'] = len(browser) == 2 and browser[1]['adapter_started_at_ms'] >= browser[0]['result_delivered_at_ms']
            assertions['product_Stop_result'] = 'stopped' in json.dumps(result).lower() and 'stopped' in json.dumps(probe.get('prior_run_result')).lower()
        record = {'case_id': case_id, 'checks': assertions, 'all_passed': all(assertions.values()),
            'resource': resource, 'probe': probe, 'browser': browser, 'setup_error': native.get('setup_error')}
        records.append(record)
        (case / 'verification.json').write_text(json.dumps(record, indent=2) + '\n')
        print(json.dumps({'case_id': case_id, 'checks': assertions, 'all_passed': record['all_passed']}), flush=True)
    value = {'all_passed': all(item['all_passed'] for item in records), 'provider_calls': 0,
        'not_a_repaired_Candidate_or_acceptance': True, 'records': records}
    (args.output / 'verification.json').write_text(json.dumps(value, indent=2) + '\n')
    return 0 if value['all_passed'] else 2


if __name__ == '__main__':
    raise SystemExit(main())
