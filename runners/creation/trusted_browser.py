#!/usr/bin/env python3
"""Create/verify a copied evaluator-owned browser bundle; never a Candidate base."""
from __future__ import annotations
import argparse
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess

DATA = Path(os.environ.get('AGENTSWE_HOME', '.agentswe')) / 'deps'
DEFAULT = DATA / 'trusted-browser-v1'
CHROME = Path(os.environ.get('AGENTSWE_TRUSTED_CHROME', str(Path(os.environ.get('AGENTSWE_HOME', '.agentswe')) / 'envs' / 'desktop-gui-automation-agent-v2' / 'browsers' / 'chromium-1228' / 'chrome-linux64')))


def entries(root):
    result = {}
    for p in sorted(root.rglob('*')):
        if p.is_symlink():
            result[str(p.relative_to(root))] = {'link': os.readlink(p)}
        elif p.is_file() and p != root / 'bundle-manifest.json':
            result[str(p.relative_to(root))] = {'sha256': hashlib.sha256(p.read_bytes()).hexdigest(),
                                              'mode': p.stat().st_mode & 0o777}
    return result


def checked_bundle():
    root = Path(os.environ.get('AGENTSWE_TRUSTED_BROWSER_BUNDLE', str(DEFAULT))).resolve()
    manifest = root / 'bundle-manifest.json'
    if not manifest.is_file():
        raise RuntimeError('trusted browser bundle is not prepared: ' + str(root))
    value = json.loads(manifest.read_text())
    if entries(root) != value['files']:
        raise RuntimeError('trusted browser dependency manifest mismatch')
    for required in ('node', 'chrome/chrome', 'fonts/etc/fonts.conf'):
        if not (root / required).is_file():
            raise RuntimeError('trusted browser dependency missing: ' + required)
    return root


def service_fragment():
    root = checked_bundle()
    return {
        'volumes': [{'type': 'bind', 'source': str(root), 'target': '/opt/agentswe-trusted-browser', 'read_only': True}],
        'environment': {
            'NODE': '/opt/agentswe-trusted-browser/node',
            'CHROMIUM': '/opt/agentswe-trusted-browser/chrome/chrome',
            'LD_LIBRARY_PATH': '/opt/agentswe-trusted-browser/lib',
            'FONTCONFIG_PATH': '/opt/agentswe-trusted-browser/fonts/etc',
            'FONTCONFIG_FILE': '/opt/agentswe-trusted-browser/fonts/etc/fonts.conf',
        },
        'cap_drop': ['ALL'], 'security_opt': ['no-new-privileges:true'],
    }


def prepare(output):
    output = output.resolve()
    output.relative_to(DATA)
    output.mkdir(parents=True, exist_ok=False)
    source_libs = DATA / 'runtime-dependencies-v1/browser/lib'
    source_fonts = DATA / 'runtime-dependencies-v1/fonts'
    for source, name in ((CHROME, 'chrome'), (source_libs, 'lib'), (source_fonts, 'fonts')):
        shutil.copytree(source, output / name)
    shutil.copyfile('/usr/bin/node', output / 'node')
    (output / 'node').chmod(0o555)
    # Copied fontconfig paths must resolve inside this bundle, not the host.
    config = output / 'fonts/etc/fonts.conf'
    text = config.read_text().replace('/usr/share/fonts', '/opt/agentswe-trusted-browser/fonts/share')
    config.write_text(text)
    manifest = {'scope': 'evaluator-only Chromium/Node/fonts/shared-library copy',
                'node_version': subprocess.check_output(['/usr/bin/node', '--version'], text=True).strip(),
                'sources': {'chrome': str(CHROME), 'node': '/usr/bin/node',
                            'fonts': str(source_fonts), 'libraries': str(source_libs)},
                'files': entries(output)}
    (output / 'bundle-manifest.json').write_text(json.dumps(manifest, indent=2) + '\n')
    print(json.dumps({'output': str(output), 'files': len(manifest['files']), 'node_version': manifest['node_version']}))


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--prepare', type=Path)
    args = parser.parse_args()
    if args.prepare:
        prepare(args.prepare)
    else:
        print(json.dumps({'verified': str(checked_bundle())}))
