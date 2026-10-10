"""Narrow, paired, evaluator-owned attribution of Candidate circular imports."""
from __future__ import annotations
import argparse
import hashlib
import json
import os
import signal
from pathlib import Path
import subprocess
import sys
import tempfile
import time


def sha(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def read(path):
    return json.loads(Path(path).read_text())


def write(path, value):
    Path(path).write_text(json.dumps(value, sort_keys=True, indent=2)+'\n')


def suspected(probe):
    exc = (probe.get('observed') or {}).get('mastery_tool_exception') or {}
    return exc.get('type') == 'ImportError' and 'partially initialized module' in exc.get('message', '') and 'circular import' in exc.get('message', '') and 'MASTERY_TOOL_TYPES' in exc.get('message', '') and 'deeptutor.capabilities.mastery' in exc.get('message', '')


def decide(candidate, baseline, resources):
    """No inference from nonzero exit, tool absence, or text alone."""
    result = {'valid': False, 'classification': 'unresolved_import_failure', 'reason': 'paired causal import evidence is incomplete'}
    if not suspected(candidate): return result
    if not resources.get('valid') or resources.get('timed_out') or not resources.get('inherited_case_scope'): return result
    group = resources.get('observed', {}).get('cgroup')
    for probe in (candidate, baseline):
        if probe.get('exit_code') != 0 or probe.get('infra_valid') is not True: return result
        if probe.get('preflight_resource_contract', {}).get('cgroup') != group: return result
        if probe.get('sandbox', {}).get('network_namespace') != 'isolated': return result
        if probe.get('sandbox', {}).get('host_tcp_network_access') is not False: return result
    c, b = candidate['observed'], baseline['observed']
    if b.get('mastery_tool_error') or not b.get('mastery_tool_names'): return result
    if any(c.get(k) != b.get(k) for k in ('executable', 'python_info', 'python_version', 'pydantic_major')): return result
    if candidate['sandbox'].get('python_dependency_mounts') != baseline['sandbox'].get('python_dependency_mounts'): return result
    for name in ('pydantic', 'pydantic_settings', 'openai', 'yaml', 'jinja2', 'defusedxml', 'aiosqlite'):
        if c.get('imports', {}).get(name) != b.get('imports', {}).get(name) or c['imports'][name].get('ok') is not True: return result
    root, reference = Path(candidate['repository']).resolve(), Path(baseline['repository']).resolve()
    frames = c['mastery_tool_exception'].get('frames', [])
    relative = []
    for frame in frames:
        path = Path(frame.get('filename', '')).resolve()
        if path.is_absolute() and path.is_relative_to(root): relative.append(path.relative_to(root).as_posix())
    relative = sorted(set(relative))
    if not relative or any(not p.startswith(('deeptutor/', 'deeptutor_cli/')) for p in relative): return result
    changed = [p for p in relative if (root/p).is_file() and (not (reference/p).is_file() or sha(root/p) != sha(reference/p))]
    if not changed: return result
    # Only fixed explanatory text and validated relative product paths are public.
    # Raw exception messages, absolute paths, frames and module payloads stay private.
    result.update(valid=True, classification='candidate_build_failure', phase='product_import',
        reason='Candidate product import failed with a circular dependency; the same import succeeded in the baseline under the same runtime, dependencies and resource scope.',
        public_diagnostic={'phase':'product_import','error_type':'ImportError',
            'message':'Circular import prevents the mastery tool registry from loading. Remove the import cycle in the changed product modules.',
            'module':'deeptutor.capabilities.mastery', 'symbol':'MASTERY_TOOL_TYPES', 'product_paths':changed},
        changed_product_files={p:sha(root/p) for p in changed}, baseline_import_succeeded=True,
        same_runtime_dependencies_sandbox_and_scope=True)
    return result


def scope_proof(case_output):
    try:
        from .owned_resources import _show, verify_identity
    except ImportError:
        from owned_resources import _show, verify_identity
    expected = read(case_output/'case_resources/scope-ownership.json')
    group = next(x.split(':',2)[2] for x in Path('/proc/self/cgroup').read_text().splitlines() if x.startswith('0::'))
    actual = _show(expected['unit']); verify_identity(actual, expected)
    if group != expected['cgroup']: raise RuntimeError('probe is not inside its owning case scope')
    controller = Path('/sys/fs/cgroup')/group.lstrip('/')
    if (controller/'memory.max').read_text().strip() != str(expected['memory_bytes']) or (controller/'memory.swap.max').read_text().strip() != '0':
        raise RuntimeError('inherited probe resource controller mismatch')
    return {'valid':True,'inherited_case_scope':True,'timed_out':False,'observed':{'cgroup':group,'pid':os.getpid()},
        'memory_bytes':expected['memory_bytes'],'systemd_properties':actual,
        'cleanup':{'owner':'outer_case_scope','verified_after_case_exit':False}}


def bounded_run(command, *, cwd, env, deadline, start_new_session=True):
    remaining = deadline-time.monotonic()
    if remaining <= 0: raise RuntimeError('existing case deadline exhausted before import probe')
    child = subprocess.Popen(command,cwd=cwd,env=env,text=True,stdout=subprocess.PIPE,stderr=subprocess.PIPE,start_new_session=start_new_session)
    try:
        stdout, stderr = child.communicate(timeout=remaining)
    except BaseException:
        try:
            if start_new_session: os.killpg(child.pid,signal.SIGKILL)
            else: child.kill()
        except ProcessLookupError: pass
        child.communicate(timeout=5)
        raise
    return subprocess.CompletedProcess(command,child.returncode,stdout,stderr)


def inherited_runner(case_output, deadline, *, own_process_group=True):
    proof = scope_proof(case_output)
    def run(command, *, cwd, env, output, timeout):
        limit = min(deadline,time.monotonic()+timeout)
        completed = bounded_run(command,cwd=cwd,env=env,deadline=limit,start_new_session=own_process_group)
        return completed, {**proof,'cgroup':proof['observed']['cgroup'], 'absolute_deadline_monotonic':limit}
    return run


def paired_probe(python, repository, baseline, output, *, deadline):
    proof = scope_proof(output.parent)
    root = Path(tempfile.mkdtemp(prefix='paired-import-', dir=output.parent))
    deadline = min(deadline,time.monotonic()+60)
    command = [python, '-I', str(Path(__file__).resolve()), '--worker', '--python', python,
        '--repository', str(repository), '--baseline', str(baseline), '--output', str(root),
        '--case-output',str(output.parent),'--deadline',str(deadline)]
    env = {'PATH':'/usr/bin:/bin','LANG':'C.UTF-8','PYTHONDONTWRITEBYTECODE':'1'}
    completed = bounded_run(command,cwd='/',env=env,deadline=deadline)
    proof.update(absolute_deadline_monotonic=deadline,process_exit_code=completed.returncode)
    candidate_path, baseline_path = root/'candidate.json', root/'baseline.json'
    if completed.returncode or not candidate_path.is_file() or not baseline_path.is_file():
        value = {'valid':False,'classification':'unresolved_import_failure','reason':'paired import worker did not complete','resource_contract':proof}
        write(root/'attribution.json', value)
        return value
    candidate, reference = read(candidate_path), read(baseline_path)
    try:
        from .protocol import tree_digest
    except ImportError:
        from protocol import tree_digest
    value = {**decide(candidate, reference, proof), 'schema_version':'agentswe-deeptutor-paired-import/v1',
        'candidate_probe':str(candidate_path),'candidate_probe_sha256':sha(candidate_path),
        'baseline_probe':str(baseline_path),'baseline_probe_sha256':sha(baseline_path),
        'resource_contract':proof, 'candidate_repository_sha256':tree_digest(repository),
        'baseline_repository_sha256':tree_digest(baseline)}
    write(root/'attribution.json', value)
    value['evidence_path'] = str(root/'attribution.json')
    value['evidence_sha256'] = sha(root/'attribution.json')
    return value


def verify_failure(launcher, output, candidate_digest):
    try:
        from .protocol import tree_digest, AUTHORITATIVE_SOURCE, AUTHORITATIVE_SOURCE_DIGEST
    except ImportError:
        from protocol import tree_digest, AUTHORITATIVE_SOURCE, AUTHORITATIVE_SOURCE_DIGEST
    attr = launcher.get('import_attribution') or {}
    if not attr.get('valid') or launcher.get('classification') != 'candidate_build_failure': return None
    case_resource = launcher.get('case_resource_contract') or {}
    if not case_resource.get('valid') or case_resource.get('timed_out') or not case_resource.get('cleanup',{}).get('complete'): return None
    try:
        evidence = Path(attr['evidence_path'])
        if not evidence.resolve().is_relative_to(output.resolve()) or sha(evidence) != attr['evidence_sha256']: return None
        saved = read(evidence)
        for role in ('candidate','baseline'):
            if sha(saved[role+'_probe']) != saved[role+'_probe_sha256']: return None
        candidate, baseline = read(saved['candidate_probe']), read(saved['baseline_probe'])
        if Path(baseline['repository']).resolve() != AUTHORITATIVE_SOURCE.resolve(): return None
        if saved['baseline_repository_sha256'] != AUTHORITATIVE_SOURCE_DIGEST or tree_digest(AUTHORITATIVE_SOURCE) != AUTHORITATIVE_SOURCE_DIGEST: return None
        if saved['resource_contract']['observed']['cgroup'] != case_resource.get('observed',{}).get('cgroup'): return None
        if tree_digest(Path(candidate['repository'])) != saved['candidate_repository_sha256']: return None
        if tree_digest(Path(baseline['repository'])) != saved['baseline_repository_sha256']: return None
        checked = decide(candidate, baseline, saved['resource_contract'])
        if not checked['valid']: return None
        origin = read(output/'source-copy-observation.json')
        if Path(origin['destination']).resolve() != Path(candidate['repository']).resolve(): return None
        if tree_digest(Path(origin['source'])) != candidate_digest: return None
        return {**checked,'evidence_path':str(evidence)}
    except (OSError, ValueError, KeyError, TypeError): return None


def worker(args):
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    from runtime_probe import probe_runtime
    inherited = inherited_runner(args.case_output,args.deadline,own_process_group=False)
    for name, repository in [('baseline',args.baseline),('candidate',args.repository)]:
        probe_runtime(args.python,repository,output=args.output/(name+'.json'),_runner=inherited)


if __name__ == '__main__':
    parser=argparse.ArgumentParser();parser.add_argument('--worker',action='store_true');parser.add_argument('--python',required=True)
    parser.add_argument('--repository',type=Path,required=True);parser.add_argument('--baseline',type=Path,required=True);parser.add_argument('--output',type=Path,required=True);parser.add_argument('--case-output',type=Path,required=True);parser.add_argument('--deadline',type=float,required=True)
    worker(parser.parse_args())
