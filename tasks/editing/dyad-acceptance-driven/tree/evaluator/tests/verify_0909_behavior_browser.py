"""Zero-provider actual Chromium controls; never a benchmark acceptance run."""
from __future__ import annotations
import argparse
import hashlib
import importlib.util
import json
from pathlib import Path
import shutil
import sys

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / 'evaluator'))
from behavior_worlds import materialize

NODE = Path('@@AGENTSWE_EDITING_TASKS@@/openwiki-change-impact/tree/.runtime/node-v22.12.0-linux-x64/bin/node')
MODULES = Path('@@AGENTSWE_ENVS@@/dyad-task-env-cycle-006/node_modules')
BROWSER = Path('@@AGENTSWE_ENVS@@/runtime-deps-0909/dyad/ms-playwright/chromium_headless_shell-1208/chrome-headless-shell-linux64/chrome-headless-shell')
WIKI = Path('@@AGENTSWE_EDITING_TASKS@@/openwiki-change-impact/tree')


def module(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    value = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(value)
    return value


def sha(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    args.output.mkdir(parents=True, exist_ok=False)
    sandbox = module('dyad_browser_sandbox', WIKI / 'agentloop/evaluator/transport_sandbox.py')
    resources = module('dyad_browser_resources', WIKI / 'agentloop/owned_resources.py')
    dependencies = {'playwright_version': json.loads((MODULES / 'playwright-core/package.json').read_text())['version'],
        'browser_revision': '1208', 'browser_product_version': '145.0.7632.6',
        'node': str(NODE), 'browser_executable': str(BROWSER),
        'sha256': {str(path): sha(path) for path in [NODE, BROWSER,
            MODULES / 'playwright-core/package.json', MODULES / 'playwright-core/browsers.json']},
        'system_packages_modified': False, 'provider_calls': 0}
    (args.output / 'runtime-dependencies.json').write_text(json.dumps(dependencies, indent=2) + '\n')
    records = []
    for name, case_id, positive, expected in [
        ('registration-broken', 'test_001', False, 'failed'),
        ('registration-control', 'test_001', True, 'passed'),
        ('inventory-control', 'test_002', False, 'passed'),
    ]:
        case = args.output / name
        case.mkdir()
        fixture = materialize(case_id, case / 'app', positive_control=positive)
        target = case / 'app/e2e/behavior.spec.ts'
        target.parent.mkdir()
        shutil.copyfile(fixture['private_independent_test'], target)
        runtime = case / 'browser-runtime'
        runtime.mkdir()
        options = {'appId': 1, 'appDir': str(case / 'app'), 'testFile': 'e2e/behavior.spec.ts',
            'grep': case_id + ' actual', 'runtimeRoot': str(runtime), 'nodeModules': str(MODULES),
            'browserExecutable': str(BROWSER), 'timeoutMs': 60000}
        option_path = case / 'options.json'
        option_path.write_text(json.dumps(options) + '\n')
        output = case / 'actual-result.json'
        command = sandbox.sandbox_command([str(NODE), str(ROOT / 'environment/behavior_runner.mjs'),
            str(option_path), str(output)], writable=[case], readonly=[NODE.parent.parent,
            MODULES, BROWSER.parent, ROOT / 'environment/behavior_runner.mjs'], cwd=case)
        completed, attestation = resources.run_owned(command, cwd=case,
            env={'PATH': '/usr/bin:/bin', 'LANG': 'C.UTF-8', 'HOME': str(case), 'TMPDIR': str(runtime)},
            output=case / 'resources', timeout=90)
        (case / 'stdout.log').write_text(completed.stdout)
        (case / 'stderr.log').write_text(completed.stderr)
        result = json.loads(output.read_text()) if output.exists() else {}
        status = (result.get('results') or [{}])[0].get('status')
        native = result.get('native_behavior', {})
        records.append({'name': name, 'expected': expected, 'actual': status,
            'process_returncode': completed.returncode, 'actual_test_count': native.get('actual_test_count'),
            'source_sha256': fixture['source_sha256'], 'private_test_sha256': fixture['private_test_sha256'],
            'copied_target_sha256': native.get('copied_target_sha256'),
            'resource_valid': attestation.get('valid'), 'cleanup': attestation.get('cleanup'),
            'network_namespace': 'isolated', 'private_world_module_mounted': False,
            'valid': completed.returncode == 0 and status == expected and native.get('actual_test_count', 0) > 0
                and native.get('infrastructure_invalid') is False
                and attestation.get('valid') is True and attestation.get('cleanup', {}).get('complete') is True})
    result = {'all_passed': all(record['valid'] for record in records), 'records': records,
        'provider_calls': 0, 'model_or_benchmark_acceptance_claimed': False,
        'positive_controls_are_evaluator_only': True}
    (args.output / 'verification.json').write_text(json.dumps(result, indent=2) + '\n')
    print(json.dumps(result), flush=True)
    return 0 if result['all_passed'] else 2


if __name__ == '__main__':
    raise SystemExit(main())
