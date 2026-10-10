"""Prepare exact immutable Code input and run the real network-none container.

No Builder, lower model, Result judge, Code judge or provider API is called.
The Code runner preflight removes its secret mount and disables networking.
"""
from __future__ import annotations
import argparse
import hashlib
import json
from pathlib import Path
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[1]
SHARED = Path('@@AGENTSWE_EDITING_CONTROL@@')


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--run-dir', type=Path, required=True)
    parser.add_argument('--runtime-python', type=Path, required=True)
    parser.add_argument('--credential-file', type=Path, required=True)
    args = parser.parse_args()
    run = args.run_dir.resolve()
    stage = json.loads((run / 'stage_input_hashes.json').read_text())
    for path, expected in stage['sha256'].items():
        if hashlib.sha256(Path(path).read_bytes()).hexdigest() != expected:
            raise ValueError('stage input changed before Code preflight: ' + path)
    output = run / 'preflight'
    output.mkdir(exist_ok=False)
    code = run / 'formal_scoring/code_axis'
    candidate = run / 'lifecycle/frozen_candidate'
    requirements = code / 'public_requirements'
    rubric = ROOT / 'evaluator/code_rubric.md'
    tokens = [str(args.runtime_python), str(ROOT / 'evaluator/tests/verify_0909_code_tokens.py'),
        '--scope', str(code / 'code_evidence_scope.json'), '--candidate', str(candidate),
        '--requirements', str(requirements), '--rubric', str(rubric), '--output', str(output / 'prompt_tokens.json'),
        '--prepare-task-root', str(ROOT), '--candidate-digest', stage['candidate_digest']]
    count = subprocess.run(tokens, capture_output=True, text=True, check=False)
    (output / 'tokens.stdout.log').write_text(count.stdout)
    (output / 'tokens.stderr.log').write_text(count.stderr)
    if count.returncode:
        raise RuntimeError('offline exact Code prompt preparation/count failed')
    scope = json.loads((code / 'code_evidence_scope.json').read_text())
    command = [sys.executable, str(SHARED / 'code_judge_runner.py'), '--candidate-source', str(candidate),
        '--public-requirements', str(requirements), '--code-rubric', str(rubric),
        '--credential-file', str(args.credential_file), '--output-dir', str(output / 'actual_code_container'),
        '--expected-candidate-digest', stage['candidate_digest'], '--preflight-only']
    for path in scope['evidence_paths']:
        command += ['--evidence-path', path]
    result = subprocess.run(command, capture_output=True, text=True, check=False)
    (output / 'runner.stdout.log').write_text(result.stdout)
    (output / 'runner.stderr.log').write_text(result.stderr)
    container_root = output / 'actual_code_container'
    receipt = json.loads((container_root / 'code_preflight_lifecycle.json').read_text())
    actual = json.loads((container_root / 'code_preflight.stdout.log').read_text().strip().splitlines()[-1])
    inputs = [code / 'code_evidence_scope.json', rubric, run / 'stage_input_hashes.json', run / 'lifecycle/freeze_manifest.json',
        output / 'prompt_tokens.json', container_root / 'code_preflight_lifecycle.json', container_root / 'code_preflight.stdout.log']
    inputs += [path for path in requirements.rglob('*') if path.is_file()]
    report = {'schema_version': 'agentswe-staged-acceptance-code-preflight/v1',
        'valid': result.returncode == 0 and actual.get('preflight_valid') is True and receipt['cleanup']['complete'] is True
            and actual.get('candidate_digest') == stage['candidate_digest'],
        'provider_calls': 0, 'actual_container_preflight': actual, 'cleanup': receipt['cleanup'],
        'scope': {'valid': scope.get('valid'), 'complete_change_coverage': scope['complete_change_coverage'],
                  'candidate_digest': scope['candidate_digest'], 'baseline_expected_digest': scope['baseline_expected_digest'],
                  'baseline_observed_digest': scope['baseline_observed_digest'], 'evidence_path_count': len(scope['evidence_paths'])},
        'token_estimate': json.loads((output / 'prompt_tokens.json').read_text()),
        'input_sha256': {str(path): hashlib.sha256(path.read_bytes()).hexdigest() for path in sorted(set(inputs))},
        'formal_scoring_directory_not_preflight_lifecycle_directory': True,
        'no_semantic_dev_feedback_claimed': True}
    with (run / 'stage_code_preflight.json').open('x') as handle:
        json.dump(report, handle, indent=2)
        handle.write('\n')
    print(json.dumps(report, indent=2))
    return 0 if report['valid'] else 2


if __name__ == '__main__':
    raise SystemExit(main())
