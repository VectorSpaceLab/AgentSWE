#!/usr/bin/env python3
"""PPTX execution contract: valid core is scoreable, not a semantic 100."""
from __future__ import annotations
import argparse
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import subprocess
import uuid

def put(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, indent=2, ensure_ascii=False) + '\n')

def main():
    p = argparse.ArgumentParser()
    for name in ('cases-root', 'output-dir', 'manifest', 'harness', 'run-evidence', 'verifier-dir'):
        p.add_argument('--' + name, type=Path, required=True)
    a = p.parse_args()
    a.verifier_dir.mkdir(parents=True, exist_ok=True)
    # Preserve earlier diagnostics, while preventing a retry from consuming stale reward.
    owned = ('harness_result.json', 'score_contract.json', 'reward.json', 'harness.stdout.log', 'infrastructure_error.json')
    stale = [a.verifier_dir / name for name in owned if (a.verifier_dir / name).exists()]
    if stale:
        archive = a.verifier_dir / 'previous_attempts' / uuid.uuid4().hex
        archive.mkdir(parents=True, exist_ok=False)
        for path in stale:
            path.rename(archive / path.name)
    try:
        manifest = json.loads(a.manifest.read_text())
        ev = json.loads(a.run_evidence.read_text())
        if not isinstance(ev, dict) or not isinstance(manifest, dict):
            raise RuntimeError('missing trusted execution evidence')
        spec = importlib.util.spec_from_file_location('pptx_evidence', Path('/evaluator/evidence_bundle.py'))
        reader = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(reader)
        if reader.tree_digest(a.cases_root / manifest['case_id']) != manifest.get('case_digest'):
            raise RuntimeError('active case differs from trusted manifest')
        if reader.tree_digest(a.output_dir) != ev.get('output_digest'):
            raise RuntimeError('Candidate output differs from trusted execution evidence')
        result = a.verifier_dir / 'harness_result.json'
        cp = subprocess.run([os.environ.get('PYTHON', os.sys.executable), str(a.harness),
            '--case-dir', str(a.cases_root / manifest['case_id']), '--output-dir', str(a.output_dir),
            '--result', str(result)], capture_output=True, text=True, timeout=1000)
        (a.verifier_dir / 'harness.stdout.log').write_text(cp.stdout + cp.stderr)
        h = json.loads(result.read_text())
        if cp.returncode or h.get('evaluation_state') == 'infrastructure_error' or h.get('infrastructure_error'):
            raise RuntimeError(h.get('infrastructure_error', 'trusted PPTX verifier failed'))
        if h.get('case') != manifest['case_id']:
            raise RuntimeError('trusted harness case mismatch')
        errors = list(h.get('fatal_errors', h.get('errors', [])))
        if ev.get('candidate_exit_code') != 0: errors.append('candidate entrypoint exited nonzero')
        if ev.get('timed_out') is True: errors.append('task_registered_timeout')
        if ev.get('memory_violation') is True: errors.append('registered_memory_violation')
        if ev.get('credential_leak_detected') is True: errors.append('credential_leak')
        if ev.get('output_structure_valid') is not True: errors.append('unsafe_output_structure')
        if ev.get('process_log_paths_valid') is False: errors.append('unsafe_process_log_path')
        if ev.get('candidate_unchanged') is False: errors.append('frozen_candidate_changed')
        if ev.get('case_unchanged') is False: errors.append('active_case_changed')
        valid = h.get('validity_gate') is True and not errors
        contract = {'schema_version': '2.0', 'case_id': manifest['case_id'], 'case_digest': manifest.get('case_digest'),
            'evaluation_mode': manifest['evaluation_mode'], 'validity_gate': valid, 'fatal_gate': not valid,
            'evaluation_state': 'scoreable' if valid else 'fatal_zero', 'fatal_reasons': errors,
            'score': 100 if valid else 0, 'reward': 1.0 if valid else 0.0, 'score_kind': 'execution_gate_only',
            'candidate_exit_code': ev.get('candidate_exit_code'), 'timed_out': ev.get('timed_out'),
            'memory_violation': ev.get('memory_violation'), 'credential_leak_detected': ev.get('credential_leak_detected'),
            'candidate_digest': manifest.get('candidate_digest'), 'output_digest': ev.get('output_digest'),
            'harness_errors': errors, 'candidate_provider_counts': ev.get('provider_counts', {}),
            'trusted_harness_result': h,
            'trusted_harness_result_sha256': hashlib.sha256(json.dumps(h, sort_keys=True, separators=(',', ':')).encode()).hexdigest(),
            'leaderboard_eligible': False}
        put(a.verifier_dir / 'score_contract.json', contract)
        put(a.verifier_dir / 'reward.json', {'reward': contract['reward'], 'score': contract['score'], 'validity_gate': int(valid)})
        return 0
    except Exception as exc:
        put(a.verifier_dir / 'infrastructure_error.json', {'evaluation_state': 'infrastructure_error',
            'score_publishable': False, 'score': None, 'error': type(exc).__name__ + ': ' + str(exc)})
        return 70

if __name__ == '__main__':
    raise SystemExit(main())
