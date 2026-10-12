#!/usr/bin/env python3
"""Offline PPTX verifier. Parse first; isolate quality, fatal and infrastructure."""
from __future__ import annotations
import argparse
import importlib.util
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile

def main():
    p = argparse.ArgumentParser()
    p.add_argument('--case-dir', type=Path, required=True)
    p.add_argument('--output-dir', type=Path, required=True)
    p.add_argument('--result', type=Path, required=True)
    p.add_argument('--evaluator-dir', type=Path, default=Path('/evaluator'))
    p.add_argument('--libreoffice-root', type=Path, default=Path('/tools/libreoffice/root'))
    p.add_argument('--pdftoppm', type=Path, default=Path('/opt/agentswe-trusted-runtime/bin/pdftoppm'))
    a = p.parse_args()
    a.result.parent.mkdir(parents=True, exist_ok=True)
    value = {'case': a.case_dir.name, 'validity_gate': False}
    def save():
        a.result.write_text(json.dumps(value, indent=2, ensure_ascii=False) + '\n')
    try:
        evaluator = a.evaluator_dir.resolve()
        spec = importlib.util.spec_from_file_location('pptx_evidence', evaluator / 'evidence_bundle.py')
        evidence = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(evidence)
        unsafe = [x for x in a.output_dir.rglob('*') if x.is_symlink() or not (x.is_file() or x.is_dir())]
        if unsafe:
            value.update(evaluation_state='fatal_zero', fatal_errors=['unsafe output structure'], errors=['unsafe output structure'])
            save()
            return 0
        root = Path(tempfile.mkdtemp(prefix='evidence-', dir=a.result.parent))
        parsed = root / 'parsed.json'
        command = [sys.executable, str(evaluator / 'validate_pptx.py'), '--case-id', a.case_dir.name,
                   '--output-dir', str(a.output_dir), '--expectations', str(evaluator / 'case_expectations.json'),
                   '--report', str(parsed)]
        cp = subprocess.run(command, capture_output=True, text=True, timeout=180)
        if cp.returncode not in (0, 1) or not parsed.is_file():
            raise RuntimeError('PPTX OOXML validator failed to produce a valid report')
        value = json.loads(parsed.read_text())
        value['case'] = a.case_dir.name
        if value.get('evaluation_state') == 'fatal_zero':
            save()
            return 0
        ooxml = value['evidence']['ooxml']
        render_dir = root / 'render'
        report = root / 'render_report.json'
        cp = subprocess.run([sys.executable, str(evaluator / 'render_pptx.py'),
            str(a.output_dir / 'deck.pptx'), str(render_dir), '--report', str(report),
            '--libreoffice-root', str(a.libreoffice_root), '--pdftoppm', str(a.pdftoppm),
            '--expected-slides', str(ooxml['slide_count'])], capture_output=True, text=True, timeout=780)
        if not report.is_file():
            raise RuntimeError('PPTX renderer did not write its report')
        rendered = json.loads(report.read_text())
        value['evidence']['render'] = rendered
        if cp.returncode == 70 or rendered.get('evaluation_state') == 'infrastructure_error':
            raise RuntimeError(rendered.get('infrastructure_error', 'PPTX renderer infrastructure failed'))
        if rendered.get('evaluation_state') == 'fatal_zero' and cp.returncode == 1:
            value.update(validity_gate=False, evaluation_state='fatal_zero',
                         fatal_errors=rendered['errors'], errors=rendered['errors'])
            save()
            return 0
        if cp.returncode or not rendered.get('valid'):
            raise RuntimeError('unexpected PPTX renderer result')
        images = [dict(item, role='candidate_slide', file='render/' + item['file']) for item in rendered['slides']]
        sources, source_images = evidence.collect_sources(a.case_dir, root, a.pdftoppm)
        source_marks = evidence.compare_source_marks(a.output_dir / 'deck.pptx', root, source_images)
        descriptor = evidence.seal(root, {'case': a.case_dir.name, 'case_digest': evidence.tree_digest(a.case_dir),
            'output_digest': evidence.tree_digest(a.output_dir), 'slides': ooxml['slides'],
            'sources': sources, 'source_mark_comparison': source_marks, 'images': images + source_images})
        # Re-read exactly what the credentialed judge will receive.
        evidence.load_bundle(root, descriptor)
        value.update(validity_gate=True, evaluation_state='scoreable', visual_evidence=descriptor)
        value.setdefault('warnings', []).extend(rendered.get('warnings', []))
        save()
        return 0
    except Exception as exc:
        value.update(validity_gate=False, contract_valid=False, evaluation_state='infrastructure_error',
                     infrastructure_error=type(exc).__name__ + ': ' + str(exc))
        save()
        return 70

if __name__ == '__main__':
    raise SystemExit(main())
