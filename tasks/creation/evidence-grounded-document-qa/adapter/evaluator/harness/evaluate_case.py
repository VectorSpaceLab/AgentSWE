#!/usr/bin/env python3
"""Core parse gate, quality diagnostics, and actual source-native interaction.

Core failure is a Candidate zero; broken review is a 20-point ceiling;
missing/crashed trusted validators or browser infrastructure produce no score.
"""
from __future__ import annotations

import argparse
import base64
import hashlib
import json
import os
import subprocess
import sys
from pathlib import Path

def find_script(name):
    root = Path(__file__).resolve().parent
    return next((p.resolve() for p in (
        root / name, root.parent / name, Path('/tests') / name,
        Path('/evaluator') / name,
    ) if p.is_file()), None)


def core_errors(output):
    errors = []
    for name in ('answer.md', 'claims_and_citations.json'):
        path = output / name
        try:
            if path.is_symlink():
                raise ValueError('symlink core artifact')
            content = path.read_text(encoding='utf-8')
            if not content.strip():
                raise ValueError('empty core artifact')
            if name.endswith('.json'):
                value = json.loads(content)
                if not isinstance(value, dict) or not isinstance(value.get('claims'), list):
                    raise ValueError('core claims array is not inspectable')
        except (OSError, UnicodeError, ValueError) as exc:
            errors.append(f'{name}: {exc}')
    return errors


def execute(command):
    try:
        process = subprocess.run(command, text=True, capture_output=True, timeout=90)
        value = json.loads(process.stdout)
        if not isinstance(value, dict) or not isinstance(value.get('valid'), bool):
            raise ValueError('trusted validator output has no boolean valid field')
        value['exit_code'] = process.returncode
        if value.get('skipped') or process.returncode not in (0, 1):
            value['infrastructure_error'] = True
        return value
    except (OSError, ValueError, subprocess.TimeoutExpired) as exc:
        return {'valid': False, 'infrastructure_error': True,
                'errors': [f'trusted validator failed: {type(exc).__name__}: {exc}']}


def source_evidence(case_dir):
    parser = find_script('source_native.py')
    if parser is None:
        raise RuntimeError('trusted native source parser missing')
    sys.path.insert(0, str(parser.parent))
    from source_native import pdf_runs, docx_content, xlsx_content
    records = []
    for path in sorted((case_dir / 'assets').rglob('*')):
        if not path.is_file() or path.is_symlink():
            continue
        raw = path.read_bytes()
        record = {'source_id': path.relative_to(case_dir / 'assets').as_posix(), 'sha256': hashlib.sha256(raw).hexdigest()}
        suffix = path.suffix.lower()
        if suffix in ('.txt', '.md', '.html', '.csv', '.svg'):
            record['native_text'] = raw.decode('utf-8')
        elif suffix == '.pdf':
            record['positioned_pdf_pages'] = pdf_runs(path)
        elif suffix == '.docx':
            record['paragraphs'], record['tables'] = docx_content(path)
        elif suffix == '.xlsx':
            record['sheets'] = xlsx_content(path)
        elif suffix in ('.png', '.jpg', '.jpeg'):
            record['image_data_url'] = 'data:image/' + ('png' if suffix == '.png' else 'jpeg') + ';base64,' + base64.b64encode(raw).decode()
        else:
            raise RuntimeError('unsupported trusted source evidence: ' + path.name)
        records.append(record)
    return records


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--case-dir', type=Path, required=True)
    parser.add_argument('--output-dir', type=Path, required=True)
    parser.add_argument('--result', type=Path, required=True)
    args = parser.parse_args()
    fatal = core_errors(args.output_dir)
    quality, feature, infrastructure = [], [], []
    checks = {}
    sources = []
    if not (args.case_dir / 'input.md').is_file() or not (args.case_dir / 'assets').is_dir():
        infrastructure.append('active case input/assets are missing')
    if not fatal and not infrastructure:
        try:
            sources = source_evidence(args.case_dir)
        except Exception as exc:
            infrastructure.append('trusted source extraction failed: ' + str(exc))
        for name in ('validate_artifacts.py', 'validate_source_native_review.py'):
            script = find_script(name)
            if script is None:
                infrastructure.append(f'{name}: trusted validator not mounted')
                continue
            check = execute([sys.executable, str(script), str(args.case_dir), str(args.output_dir)])
            checks[name] = check
            if check.get('infrastructure_error'):
                infrastructure.append(f'{name}: trusted validator failed')
            elif not check['valid']:
                diagnostics = [str(x) for x in check.get('errors', [])]
                (quality if name == 'validate_artifacts.py' else feature).extend(diagnostics)
                if name == 'validate_artifacts.py':
                    feature.extend(x for x in diagnostics if 'locator/content failure' in x)
        if not feature and not infrastructure:
            script = find_script('validate_viewer_interaction.mjs')
            if script is None:
                infrastructure.append('trusted browser validator not mounted')
            else:
                check = execute([os.environ.get('NODE', 'node'), str(script),
                                 str(args.output_dir / 'review.html'),
                                 str(args.output_dir / 'review_manifest.json')])
                checks['browser'] = check
                if check.get('infrastructure_error'):
                    infrastructure.append('trusted browser validation unavailable')
                elif not check['valid']:
                    feature.extend(str(x) for x in check.get('errors', []))
    state = 'infrastructure_error' if infrastructure else 'fatal_zero' if fatal else 'scoreable'
    result = {
        'schema_version': '2.0', 'case': args.case_dir.name,
        'evaluation_state': state, 'validity_gate': state == 'scoreable',
        'fatal_errors': fatal, 'quality_errors': quality,
        'errors': fatal + infrastructure, 'infrastructure_errors': infrastructure,
        'hard_feature_valid': not fatal and not feature and not infrastructure,
        'hard_feature_errors': feature, 'hard_feature_ceiling': 20,
        'checks': checks,
        'trusted_source_evidence': sources,
    }
    args.result.parent.mkdir(parents=True, exist_ok=True)
    args.result.write_text(json.dumps(result, indent=2) + '\n')
    return 0
if __name__ == '__main__': raise SystemExit(main())
