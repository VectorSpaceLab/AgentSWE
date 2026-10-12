#!/usr/bin/env python3
"""Trusted PPTX renderer: health probe, one normal retry, all-page evidence."""
from __future__ import annotations
import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import tempfile
from PIL import Image, ImageStat

DEFAULT_LO_ROOT = Path('/tools/libreoffice/root')


def sha256(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def command_run(command, environment, timeout):
    try:
        cp = subprocess.run(command, env=environment, capture_output=True, text=True, timeout=timeout)
        return {'command': command, 'exit_code': cp.returncode, 'stdout': cp.stdout[-20000:], 'stderr': cp.stderr[-20000:]}
    except (OSError, subprocess.TimeoutExpired) as exc:
        return {'command': command, 'exit_code': None, 'error': type(exc).__name__ + ': ' + str(exc)[:1000]}


def render_once(pptx, directory, soffice, pdftoppm, lo_root, dpi):
    directory.mkdir(parents=True, exist_ok=False)
    env = {k: v for k, v in os.environ.items() if not re.search(r'KEY|TOKEN|SECRET|PASSWORD|CREDENTIAL', k, re.I)}
    env['LD_LIBRARY_PATH'] = str(lo_root / 'usr/lib/x86_64-linux-gnu') + ':' + env.get('LD_LIBRARY_PATH', '')
    env['HOME'] = str(directory)
    commands = []
    with tempfile.TemporaryDirectory(prefix='lo-profile-', dir=directory) as profile:
        command = [str(soffice), '--headless', '--nologo', '--nodefault', '--nofirststartwizard',
                   '-env:UserInstallation=' + Path(profile).as_uri(), '--convert-to', 'pdf',
                   '--outdir', str(directory), str(pptx)]
        commands.append(command_run(command, env, 180))
    pdf = directory / (pptx.stem + '.pdf')
    if commands[-1]['exit_code'] != 0 or not pdf.is_file():
        return {'valid': False, 'commands': commands, 'slides': [], 'errors': ['LibreOffice did not produce all-page PDF']}
    commands.append(command_run([str(pdftoppm), '-png', '-r', str(dpi), str(pdf), str(directory / 'slide')], env, 180))
    if commands[-1]['exit_code'] != 0:
        return {'valid': False, 'commands': commands, 'slides': [], 'errors': ['Poppler rasterization failed']}
    slides = []
    for index, file in enumerate(sorted(directory.glob('slide-*.png'), key=lambda p: int(re.search(r'(\d+)\.png$', p.name)[1])), 1):
        with Image.open(file) as im:
            rgb = im.convert('RGB')
            slides.append({'slide': index, 'file': file.name, 'sha256': sha256(file),
                           'width': rgb.width, 'height': rgb.height,
                           'variance': round(sum(ImageStat.Stat(rgb).var) / 3, 3)})
    return {'valid': bool(slides), 'commands': commands, 'slides': slides,
            'pdf': pdf.name, 'pdf_sha256': sha256(pdf), 'errors': [] if slides else ['no pages rasterized']}


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('pptx', type=Path)
    parser.add_argument('render_dir', type=Path)
    parser.add_argument('--report', type=Path, required=True)
    parser.add_argument('--libreoffice-root', type=Path, default=DEFAULT_LO_ROOT)
    parser.add_argument('--pdftoppm', type=Path)
    parser.add_argument('--health-pptx', type=Path, default=Path(__file__).with_name('render_health.pptx'))
    parser.add_argument('--expected-slides', type=int)
    parser.add_argument('--dpi', type=int, default=120)
    args = parser.parse_args()
    root, pptx = args.render_dir.resolve(), args.pptx.resolve()
    soffice = args.libreoffice_root.resolve() / 'opt/libreoffice25.8/program/soffice'
    pdftoppm = (args.pdftoppm or Path(os.sys.prefix) / 'bin/pdftoppm').resolve()
    result = {'schema_version': '2.0', 'valid': False, 'evaluation_state': 'infrastructure_error',
              'slides': [], 'errors': [], 'warnings': [], 'commands': [], 'attempts': []}
    try:
        for file in (soffice, pdftoppm, args.health_pptx):
            if not file.is_file():
                raise RuntimeError('missing evaluator dependency: ' + str(file))
        if root.exists() and any(root.iterdir()):
            raise RuntimeError('trusted render directory is not empty')
        root.mkdir(parents=True, exist_ok=True)
        health = render_once(args.health_pptx.resolve(), root / 'health', soffice, pdftoppm, args.libreoffice_root, args.dpi)
        result['health'] = health
        if not health['valid'] or len(health['slides']) != 2 or any(p['variance'] < 5 for p in health['slides']):
            raise RuntimeError('trusted renderer health deck failed before Candidate rendering')
        if not pptx.is_file():
            result['evaluation_state'] = 'fatal_zero'
            result['errors'] = ['missing deck.pptx']
        else:
            result['pptx_sha256'] = sha256(pptx)
            for number in (1, 2):
                attempt = render_once(pptx, root / f'attempt-{number}', soffice, pdftoppm, args.libreoffice_root, args.dpi)
                result['attempts'].append(attempt)
                result['commands'].extend(attempt['commands'])
                if any(c.get('exit_code') == 127 or 'error while loading shared libraries' in c.get('stderr', '') for c in attempt['commands']):
                    raise RuntimeError('trusted renderer loader failure')
                if attempt['valid'] and (args.expected_slides is None or len(attempt['slides']) == args.expected_slides):
                    result.update(valid=True, evaluation_state='scoreable', pdf=f'attempt-{number}/' + attempt['pdf'], pdf_sha256=attempt['pdf_sha256'])
                    result['slides'] = [dict(p, file=f'attempt-{number}/' + p['file']) for p in attempt['slides']]
                    result['warnings'] = ['visually blank slide: ' + str(p['slide']) for p in attempt['slides'] if p['variance'] < 5]
                    break
            if not result['valid']:
                result.update(evaluation_state='fatal_zero', errors=['deck could not render completely after one normal retry'])
    except Exception as exc:
        result.update(valid=False, evaluation_state='infrastructure_error', infrastructure_error=type(exc).__name__ + ': ' + str(exc))
        result['errors'].append(result['infrastructure_error'])
    result['renderer'] = 'pinned evaluator LibreOffice + Poppler'
    result['dpi'] = args.dpi
    args.report.parent.mkdir(parents=True, exist_ok=True)
    args.report.write_text(json.dumps(result, indent=2) + '\n')
    print(json.dumps({'valid': result['valid'], 'evaluation_state': result['evaluation_state'], 'slides': len(result['slides'])}))
    return 70 if result['evaluation_state'] == 'infrastructure_error' else int(not result['valid'])


if __name__ == '__main__':
    raise SystemExit(main())
