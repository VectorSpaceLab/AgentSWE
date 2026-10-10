#!/usr/bin/env python3
"""Offline GUI execution gate and hash-bound evidence, separate from quality."""
import argparse
import base64
import hashlib
import importlib.util
import json
import os
import uuid
from pathlib import Path


def load(path):
    value = json.loads(path.read_text())
    if not isinstance(value, dict):
        raise ValueError(f'trusted evidence is not an object: {path.name}')
    return value


def write(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, indent=2, sort_keys=True) + '\n')


def tree_digest(root):
    digest = hashlib.sha256()
    for path in sorted(root.rglob('*'), key=lambda p: p.relative_to(root).as_posix()):
        name = path.relative_to(root).as_posix().encode()
        if path.is_symlink():
            kind, data = b'L', os.readlink(path).encode()
        elif path.is_file():
            kind, data = b'F', path.read_bytes()
        elif path.is_dir():
            continue
        else:
            kind, data = b'O', b''
        digest.update(kind + len(name).to_bytes(8, 'big') + name + len(data).to_bytes(8, 'big') + data)
    return digest.hexdigest()


def main():
    parser = argparse.ArgumentParser()
    for name in ('manifest', 'run-evidence', 'harness-evidence', 'output-dir', 'verifier-dir'):
        parser.add_argument('--' + name, type=Path, required=True)
    parser.add_argument('--artifact-validator', type=Path, default=Path('/tests/run_case.py'))
    args = parser.parse_args()
    args.verifier_dir.mkdir(parents=True, exist_ok=True)
    stale = [args.verifier_dir / name for name in ('harness_result.json', 'score_contract.json',
             'reward.json', 'infrastructure_error.json') if (args.verifier_dir / name).exists()]
    if stale:
        archive = args.verifier_dir / 'previous_attempts' / uuid.uuid4().hex
        archive.mkdir(parents=True, exist_ok=False)
        for path in stale:
            path.rename(archive / path.name)
    try:
        manifest = load(args.manifest)
        event = load(args.run_evidence)
        evidence = args.harness_evidence
        execution = load(evidence / 'execution.json')
        artifacts = load(evidence / 'artifact_validation.json')
        if args.artifact_validator.is_file():
            spec = importlib.util.spec_from_file_location('gui_artifact_reader', args.artifact_validator)
            reader = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(reader)
            artifacts = reader.inspect_artifacts(args.output_dir, manifest['case_id'], artifacts['expected_viewport'])
        audit = load(evidence / 'audit/request_summary.json')
        initial = load(evidence / 'audit/initial_state.json')
        final = load(evidence / 'audit/final_state.json')
        for layer in (event, execution, artifacts):
            if layer.get('infrastructure_error') or layer.get('evaluation_state') == 'infrastructure_error':
                raise RuntimeError(str(layer.get('infrastructure_error', 'trusted GUI execution failed')))
        if execution.get('case_id') != manifest['case_id'] or final.get('case_id') != manifest['case_id']:
            raise RuntimeError('GUI execution/bridge identity mismatch')
        if tree_digest(args.output_dir) != event.get('output_digest'):
            raise RuntimeError('GUI output changed since trusted execution')
        if execution.get('state_provenance') != 'evaluator-controlled-browser-v1':
            raise RuntimeError('GUI bridge records are client-controlled; authoritative provenance is unavailable')
        browser_receipts = load(evidence / 'browser/browser_receipts.json')
        browser_events = load(evidence / 'browser/browser_events.json')
        if browser_receipts.get('protocol') != 'evaluator-controlled-browser-v1' or browser_events.get('protocol') != 'evaluator-controlled-browser-v1':
            raise RuntimeError('GUI browser receipt protocol mismatch')
        receipts = browser_receipts.get('receipts')
        if not isinstance(receipts, list) or not receipts:
            raise RuntimeError('GUI evaluator browser capture receipts missing')
        screenshot_bindings = {}
        for filename, phase in (('initial_state.png', 'initial'), ('decisive_step.png', 'decisive'), ('final_state.png', 'final')):
            path = args.output_dir / filename
            if path.is_file() and not path.is_symlink():
                digest = hashlib.sha256(path.read_bytes()).hexdigest()
                matching = [r for r in receipts if r.get('sha256') == digest and r.get('phase') == phase]
                screenshot_bindings[filename] = {'sha256': digest, 'phase': phase, 'capture_matched': bool(matching),
                    'observations': [r['observation_id'] for r in matching]}
        errors, quality = [], list(artifacts.get('errors', []))
        for filename, binding in screenshot_bindings.items():
            if not binding['capture_matched']:
                if filename == 'final_state.png':
                    errors.append('final screenshot was not captured in this evaluator-owned browser run')
                else:
                    quality.append(filename + ' does not match its evaluator-owned phase capture')
        if execution.get('exit_code') is None:
            raise RuntimeError('GUI harness did not record the Candidate exit code')
        if execution.get('exit_code') != 0:
            errors.append('candidate entrypoint exited nonzero')
        for field, message in (('timeout', 'task_registered_timeout'), ('memory_violation', 'registered_memory_violation')):
            if execution.get(field) is True:
                errors.append(message)
        if event.get('credential_leak_detected') is True:
            errors.append('credential_leak')
        for field in ('process_log_paths_valid', 'evidence_path_valid', 'candidate_unchanged', 'case_unchanged'):
            if event.get(field) is not True:
                errors.append(field + ' violated')
        if audit.get('protected_bridge_attempts', 0) or audit.get('evaluator_header_attempts', 0):
            errors.append('protected evaluator bridge attempted')
        required = artifacts.get('required', {})
        for name in ('automation_result.json', 'final_state.png'):
            if required.get(name, {}).get('exists') is not True:
                errors.append('missing core artifact: ' + name)
        core = artifacts.get('json', {}).get('automation_result.json', {})
        if core.get('parseable') is not True:
            errors.append('automation_result.json is not parseable JSON')
        if core.get('case_id') is not None and core['case_id'] != manifest['case_id']:
            errors.append('automation_result.json belongs to a different case')
        if artifacts.get('png', {}).get('final_state.png', {}).get('readable') is not True:
            errors.append('final screenshot is not a readable PNG')
        for name in ('run_report.json', 'initial_state.png', 'decisive_step.png'):
            if required.get(name, {}).get('exists') is not True:
                quality.append('missing non-core artifact: ' + name)
        try:
            report = load(args.output_dir / 'run_report.json')
        except (OSError, ValueError):
            report = {}
            quality.append('run_report.json is absent or malformed')
        counts = report.get('usage', report.get('provider_counts', {}))
        if not isinstance(counts, dict):
            counts = {}
        valid_counts = {}
        for name in ('deepseek', 'gateway', 'gateway_image', 'serper', 'web_retrieval'):
            value = counts.get(name)
            if isinstance(value, bool) or not isinstance(value, int) or value < 0:
                quality.append('invalid provider count: ' + name)
            else:
                valid_counts[name] = value
        if valid_counts.get('serper', 0) or valid_counts.get('web_retrieval', 0):
            errors.append('closed-corpus retrieval count is nonzero')
        if valid_counts.get('gateway', 0) > 300 or valid_counts.get('gateway_image', 0) > 100:
            errors.append('candidate provider budget exceeded')
        valid = not errors
        harness = {'case': manifest['case_id'], 'validity_gate': valid, 'fatal_gate': not valid,
                   'evaluation_state': 'scoreable' if valid else 'fatal_zero', 'fatal_reasons': errors,
                   'quality_errors': quality, 'execution': execution, 'artifact_validation': artifacts,
                   'audit': audit, 'initial_state': initial, 'final_state': final,
                   'browser_receipts': browser_receipts, 'browser_events': browser_events,
                   'screenshot_bindings': screenshot_bindings}
        images = []
        for name in ('initial_state.png', 'decisive_step.png', 'final_state.png'):
            metadata = artifacts.get('png', {}).get(name, {})
            if metadata.get('readable') is True:
                data = (args.output_dir / name).read_bytes()
                if hashlib.sha256(data).hexdigest() != required[name].get('sha256'):
                    raise RuntimeError('screenshot changed after trusted inspection: ' + name)
                images.append({'file': name, 'sha256': hashlib.sha256(data).hexdigest(),
                               'width': metadata['width'], 'height': metadata['height'],
                               'base64': base64.b64encode(data).decode('ascii')})
        harness['visual_images'] = images
        write(args.verifier_dir / 'harness_result.json', harness)
        contract = {'schema_version': '2.0', 'case_id': manifest['case_id'],
                    'case_digest': manifest.get('case_digest'), 'evaluation_mode': manifest.get('evaluation_mode'),
                    'validity_gate': valid, 'fatal_gate': not valid, 'evaluation_state': harness['evaluation_state'],
                    'fatal_reasons': errors, 'quality_errors': quality, 'score': 100 if valid else 0,
                    'reward': 1.0 if valid else 0.0, 'score_kind': 'execution_gate_only', 'leaderboard_eligible': False,
                    'candidate_exit_code': execution['exit_code'], 'candidate_digest': manifest.get('candidate_digest'),
                    'output_digest': event.get('output_digest'), 'trusted_harness_result': harness,
                    'trusted_harness_result_sha256': hashlib.sha256(json.dumps(harness, sort_keys=True,
                                                    separators=(',', ':')).encode()).hexdigest()}
        write(args.verifier_dir / 'score_contract.json', contract)
        write(args.verifier_dir / 'reward.json', {'reward': contract['reward'], 'score': contract['score']})
        return 0
    except (OSError, ValueError, RuntimeError, KeyError, TypeError) as exc:
        write(args.verifier_dir / 'infrastructure_error.json', {'evaluation_state': 'infrastructure_error',
              'score_publishable': False, 'score': None, 'error': type(exc).__name__ + ': ' + str(exc)})
        return 70


if __name__ == '__main__':
    raise SystemExit(main())
