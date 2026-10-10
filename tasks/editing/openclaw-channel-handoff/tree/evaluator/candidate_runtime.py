#!/usr/bin/env python3
"""Build one disposable OpenClaw Candidate runtime from its source snapshot."""
from __future__ import annotations

import argparse
import hashlib
import math
import json
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from lower_agent.owned_resources import run_owned, MEMORY_BYTES
from lower_agent.build_sandbox import run_build_command
from controller.two_round_controller import tree_digest
from lower_agent.launcher import make_materialized_tree_writable
from lower_agent.runtime_resolver import resolve


def file_digest(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def compiled_digest(root: Path) -> str:
    digest = hashlib.sha256()
    files: list[Path] = []
    for pattern in ("dist/**/*", "dist-runtime/**/*", "packages/*/dist/**/*", "extensions/*/dist/**/*"):
        files.extend(path for path in root.glob(pattern) if path.is_file() or path.is_symlink())
    for path in sorted(set(files), key=lambda item: item.relative_to(root).as_posix()):
        relative = path.relative_to(root).as_posix().encode()
        payload = os.readlink(path).encode() if path.is_symlink() else path.read_bytes()
        digest.update(b'L' if path.is_symlink() else b'F')
        digest.update(len(relative).to_bytes(8, "big")); digest.update(relative)
        digest.update(len(payload).to_bytes(8, "big")); digest.update(payload)
    return digest.hexdigest() if files else ""


def source_tree_digest(root: Path) -> str:
    """Source view separate from compiled outputs and installed dependencies."""
    root = root.resolve()
    digest = hashlib.sha256()
    excluded = {".git", "node_modules", "dist", "dist-runtime", ".artifacts", "__pycache__"}
    entries: list[Path] = []
    for current, directories, files in os.walk(root, topdown=True, followlinks=False):
        current_path = Path(current)
        kept: list[str] = []
        for name in sorted(directories):
            path = current_path / name
            if name in excluded:
                continue
            entries.append(path)
            if not path.is_symlink():
                kept.append(name)
        directories[:] = kept
        entries.extend(current_path / name for name in sorted(files) if name not in excluded)
    for path in sorted(entries, key=lambda item: item.relative_to(root).as_posix()):
        relative = path.relative_to(root).as_posix().encode()
        if path.is_symlink():
            kind, payload = b"L", os.readlink(path).encode()
        elif path.is_file():
            kind, payload = b"F", path.read_bytes()
        else:
            kind, payload = b"D", b""
        digest.update(len(relative).to_bytes(8, "big")); digest.update(relative)
        digest.update(kind); digest.update(len(payload).to_bytes(8, "big")); digest.update(payload)
    return digest.hexdigest()


BUILD_SECONDS = 1800
FOCUSED_TESTS = ('src/gateway/server-methods/server-methods.test.ts',
                 'packages/gateway-protocol/src/frame-guards.test.ts')


def protected_source_inventory(root: Path) -> dict[str, str]:
    """Exact core/protocol source inputs, not generated plugin declarations."""
    result = {}
    for prefix in ('src', 'packages/gateway-protocol/src'):
        base = root / prefix
        if not base.is_dir():
            continue
        for current, dirs, files in os.walk(base, followlinks=False):
            directory = Path(current)
            dirs[:] = sorted(name for name in dirs if name not in {'node_modules', 'dist', '__pycache__'})
            for name in [*dirs, *sorted(files)]:
                path = directory / name
                relative = path.relative_to(root).as_posix()
                if path.is_symlink():
                    if not path.resolve().is_relative_to(root.resolve()):
                        raise ValueError('source link escapes frozen Candidate: ' + relative)
                    result[relative] = 'link:' + os.readlink(path)
                elif path.is_file():
                    result[relative] = 'file:' + file_digest(path)
    return result


def runtime_binding(root: Path) -> dict[str, str]:
    """Bind compiled outputs and source view before the runtime is consumed."""
    for path in root.rglob('*'):
        if path.is_symlink() and not path.resolve().is_relative_to(root.resolve()):
            raise ValueError('runtime link escapes disposable product: ' + path.relative_to(root).as_posix())
    return {'compiled_digest': compiled_digest(root),
            'runtime_source_digest': tree_digest(root),
            'runtime_source_view_digest': source_tree_digest(root),
            'protected_sources_digest': hashlib.sha256(json.dumps(
                protected_source_inventory(root), sort_keys=True).encode()).hexdigest()}


def validate_runtime_manifest(manifest: dict, frozen_candidate: Path) -> list[str]:
    """Check evaluator build provenance and actual bytes before any case runs."""
    errors=[]
    expected=tree_digest(frozen_candidate)
    if manifest.get('schema_version')!='openclaw-candidate-runtime-v2':
        errors.append('unverified runtime build protocol')
    if manifest.get('candidate_runtime_ready') is not True:
        errors.append('Candidate runtime is not ready')
    for key in ('candidate_source_digest','candidate_source_digest_after_build'):
        if manifest.get(key)!=expected:errors.append('frozen source binding mismatch: '+key)
    if manifest.get('source_digest_stable') is not True or manifest.get('protected_source_drift')!=[]:
        errors.append('source immutability not proven')
    resources=manifest.get('build_resources') or {}
    if not resources.get('valid') or not (resources.get('cleanup') or {}).get('complete') or resources.get('timed_out'):
        errors.append('build ownership/resources/cleanup not verified')
    phases=manifest.get('build_phases') or []
    if [item.get('phase') for item in phases]!=['git-init','offline-install','strict-build','focused-tests','entry-probe']:
        errors.append('native build phases incomplete')
    elif any(not item.get('valid') or item.get('timed_out') or item.get('exit_code')!=0 for item in phases):
        errors.append('native build phase failed or unverified')
    product=Path(str(manifest.get('runtime_product','')))
    if not product.is_dir() or not (product/'dist/entry.js').is_file():
        errors.append('compiled runtime product missing')
    else:
        try:
            for key,value in runtime_binding(product).items():
                if not value or manifest.get(key)!=value:errors.append('runtime bytes changed: '+key)
            protected=hashlib.sha256(json.dumps(protected_source_inventory(frozen_candidate),sort_keys=True).encode()).hexdigest()
            if manifest.get('protected_sources_digest')!=protected:
                errors.append('runtime core/protocol sources do not match frozen Candidate')
        except (OSError,ValueError) as exc:errors.append('runtime byte verification failed: '+str(exc))
    return errors


def _build_worker(*, candidate: Path, output: Path, runtime: Path | None,
                  evidence: Path, deadline: float) -> dict[str, Any]:
    candidate, output = candidate.resolve(), output.resolve()
    if output.exists() or output.is_relative_to(candidate) or candidate.is_relative_to(output):
        raise ValueError('build output must be a fresh disjoint disposable path')
    source_digest = tree_digest(candidate)
    protected_before = protected_source_inventory(candidate)
    pinned = resolve(runtime, candidate)
    # pnpm's lockfile policy verifier reads registry metadata separately from
    # the content-addressed package store. Preserve the policy and use only
    # the prewarmed public metadata, in a private writable copy.
    metadata_source=Path(str(pinned['root']))/'xdg-cache/pnpm'
    if not metadata_source.is_dir():
        raise FileNotFoundError('prewarmed pnpm metadata cache is missing')
    metadata_cache=evidence/'metadata-cache'
    shutil.copytree(metadata_source,metadata_cache/'pnpm',symlinks=True)
    for path in metadata_cache.rglob('*'):
        if path.is_symlink():
            raise ValueError('prewarmed metadata cache must not contain links')
    # No baseline compiled outputs are preseeded. Dependency extraction is
    # performed once through the published offline frozen install.
    shutil.copytree(candidate, output, symlinks=True,
        ignore=shutil.ignore_patterns('.git','node_modules','dist','dist-runtime','.artifacts','__pycache__'))
    make_materialized_tree_writable(output)
    if protected_source_inventory(output) != protected_before:
        raise RuntimeError('disposable source copy differs from frozen Candidate')
    plan = [
        ('git-init', ['/usr/bin/git','init','-q'], 'build_infrastructure_error'),
        ('offline-install', [str(pinned['pnpm']),'install','--offline','--frozen-lockfile',
                             '--frozen-store','--package-import-method','copy',
                             '--fetch-retries','0','--fetch-timeout','1000',
                             '--store-dir',str(Path(str(pinned['root']))/'pnpm-store')], 'build_infrastructure_error'),
        ('strict-build', [str(pinned['pnpm']),'build:strict-smoke'], 'candidate_build_failure'),
        ('focused-tests', [str(pinned['pnpm']),'test',*FOCUSED_TESTS], 'candidate_build_failure'),
        ('entry-probe', [str(pinned['node']),str(output/'openclaw.mjs'),'--version'], 'candidate_build_failure'),
    ]
    phases = []
    classification = 'candidate_runtime_built'
    reason = 'offline install, native build, focused upstream tests and entry completed'
    for name, command, failure in plan:
        if time.monotonic() >= deadline:
            classification, reason = 'build_infrastructure_error', 'build deadline exhausted before ' + name
            break
        record = run_build_command(command, product=output, pinned=pinned,
                                   evidence=evidence/name, deadline=deadline,cache=metadata_cache)
        phases.append({'phase':name, **record})
        if not record.get('valid'):
            classification, reason = 'build_infrastructure_error', 'build sandbox not verified: ' + name
            break
        if record.get('timed_out'):
            classification, reason = 'build_infrastructure_error', 'build outcome incomplete at deadline: ' + name
            break
        if record.get('exit_code') != 0:
            classification, reason = failure, 'native command failed: ' + name
            break
    protected_after = protected_source_inventory(output)
    drift = sorted(key for key in set(protected_before)|set(protected_after)
                   if protected_before.get(key) != protected_after.get(key))
    source_after = tree_digest(candidate)
    if source_after != source_digest or drift:
        classification, reason = 'build_infrastructure_error', 'source integrity unverified after build; no Candidate attribution'
    binding = runtime_binding(output)
    ready = (classification == 'candidate_runtime_built' and len(phases) == len(plan)
             and (output/'dist/entry.js').is_file() and bool(binding['compiled_digest']))
    if classification == 'candidate_runtime_built' and not ready:
        classification, reason = 'candidate_build_failure', 'native build did not produce required entry'
    return {
        'schema_version':'openclaw-candidate-runtime-v2','candidate_source':str(candidate),
        'candidate_source_digest':source_digest,'candidate_source_digest_after_build':source_after,
        'source_digest_stable':source_after == source_digest,'runtime_product':str(output),
        **binding,
        'protected_source_paths':len(protected_before),'protected_source_drift':drift,
        'candidate_runtime_ready':ready,'classification':classification,'classification_reason':reason,
        'build_phases':phases,'dist_entry':str(output/'dist/entry.js'),
        'dist_entry_sha256':file_digest(output/'dist/entry.js') if (output/'dist/entry.js').is_file() else None,
        'baseline_compiled_outputs_seeded':False,'credential_mode':'no-model-credential-during-build',
        'dependency_store_mode':'pnpm native frozen-store with private copied imports; no shared store writes',
        'build_protocol':{'upper_bound_seconds':BUILD_SECONDS,'absolute_deadline_monotonic':deadline,
                          'case_agent_clock_unchanged_seconds':600,'suite_accounting_still_requires_caller':True},
    }


def prepare_candidate_runtime(*, candidate: Path, output: Path, runtime: Path | None,
                              timeout_seconds: int = BUILD_SECONDS,
                              deadline_monotonic: float | None = None) -> dict[str, Any]:
    """A fresh owned build scope, distinct from the case scope and its agent clock."""
    started = time.monotonic()
    if not math.isfinite(timeout_seconds) or not 0 < timeout_seconds <= BUILD_SECONDS:
        raise ValueError('invalid build time bound')
    deadline = started + timeout_seconds
    if deadline_monotonic is not None:
        if not math.isfinite(deadline_monotonic):
            raise ValueError('nonfinite caller build deadline')
        deadline = min(deadline, deadline_monotonic)
    if deadline <= started:
        raise ValueError('caller build budget already exhausted')
    candidate, output = candidate.resolve(), output.resolve()
    evidence = output.parent / (output.name + '-build-evidence')
    if output.is_relative_to(candidate) or candidate.is_relative_to(output) or evidence.is_relative_to(candidate):
        raise ValueError('build outputs must be disjoint from the frozen Candidate')
    if output.exists() or evidence.exists():
        raise FileExistsError('Candidate build output/evidence already exists')
    evidence.mkdir(parents=True)
    command = ['/usr/bin/python3','-B',str(Path(__file__).resolve()),'--worker',
               '--candidate',str(candidate),'--output',str(output),'--evidence',str(evidence),
               '--deadline',str(deadline-5)]
    if runtime is not None:
        command += ['--runtime',str(runtime.resolve())]
    value = {'schema_version':'openclaw-candidate-runtime-v2','candidate_runtime_ready':False,
             'candidate_source':str(candidate),'runtime_product':str(output),
             'classification':'build_infrastructure_error','classification_reason':'build worker not completed'}
    try:
        process, resource = run_owned(command,cwd=ROOT,env={'PATH':'/usr/bin:/bin','LANG':'C.UTF-8'},
            output=evidence/'owned-resources',timeout=max(.001,deadline-time.monotonic()),
            memory_bytes=MEMORY_BYTES,purpose='build')
        (evidence/'worker.stdout').write_text(process.stdout or '')
        (evidence/'worker.stderr').write_text(process.stderr or '')
        worker_result = evidence/'worker-result.json'
        if worker_result.is_file():
            value.update(json.loads(worker_result.read_text()))
        value['build_resources'] = resource
        if not resource.get('valid') or not (resource.get('cleanup') or {}).get('complete') or resource.get('timed_out'):
            value.update(candidate_runtime_ready=False,classification='build_infrastructure_error',
                         classification_reason='owned build resources, outcome or cleanup unverified')
        elif process.returncode not in (0,1) or not worker_result.is_file():
            value.update(candidate_runtime_ready=False,classification='build_infrastructure_error',
                         classification_reason='build worker exited without a complete result')
    except Exception as exc:
        value.update(candidate_runtime_ready=False,classification='build_infrastructure_error',
                     classification_reason=type(exc).__name__ + ': ' + str(exc))
    value['build_evidence'] = str(evidence)
    value['duration_seconds'] = round(time.monotonic()-started,3)
    write_runtime_manifest(evidence/'manifest.json',value)
    return value


def write_runtime_manifest(path: Path, value: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--worker',action='store_true')
    parser.add_argument('--candidate',type=Path,required=True)
    parser.add_argument('--output',type=Path,required=True)
    parser.add_argument('--runtime',type=Path)
    parser.add_argument('--evidence',type=Path)
    parser.add_argument('--deadline',type=float)
    args = parser.parse_args()
    if args.worker:
        if args.evidence is None or args.deadline is None:
            parser.error('worker requires private evidence and deadline')
        value = _build_worker(candidate=args.candidate,output=args.output,runtime=args.runtime,
                              evidence=args.evidence,deadline=args.deadline)
        write_runtime_manifest(args.evidence/'worker-result.json',value)
    else:
        value = prepare_candidate_runtime(candidate=args.candidate,output=args.output,runtime=args.runtime)
        print(json.dumps(value,indent=2))
    return 0 if value.get('candidate_runtime_ready') else 1


if __name__ == '__main__':
    raise SystemExit(main())
