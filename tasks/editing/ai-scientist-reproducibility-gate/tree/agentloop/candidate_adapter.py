#!/usr/bin/env python3
"""Compile a patched AI Scientist tree inside one owned, isolated build scope."""
from __future__ import annotations
import argparse,hashlib,json,os,stat,subprocess,time
from pathlib import Path
try:
    from .protocol import write_json
    from .owned_resources import run_owned
except ImportError:
    from protocol import write_json
    from owned_resources import run_owned

BUILD_WALL_SECONDS=1800
BUILD_WORK_SECONDS=1770
PYTHON_EXECUTABLE='/usr/bin/python3'  # Trusted launcher only; never Candidate compilation.
COMPILER_RUNTIME=Path('@@AGENTSWE_ENVS@@/ai-python311-compiler-runtime')
COMPILER_MANIFEST_SHA256='1af083ae9d92502d1d9b304187433b22f7ecb11839160552c13f0d2bfaea0795'
COMPILER_IMAGE_ID='sha256:10b0f65061629ca8edab1b33444f49661f1c47e98109fda74072839287630336'
COMPILER_EXECUTABLE='/compiler/usr/local/bin/python3.11'
GIT_EXECUTABLE='/usr/bin/git'
BWRAP_EXECUTABLE='/usr/bin/bwrap'
# Evaluator-only diagnostic controls; no Candidate/environment override.
BOUNDARY_PROBE_PATHS=()
BOUNDARY_PROBE_PORT=0

def _root(path, *, directory=True, may_create=False):
    path=Path(os.path.abspath(path))
    for part in (path,*path.parents):
        if part.is_symlink():raise ValueError('input/output root has a symlink component')
    if may_create:path.mkdir(parents=True,exist_ok=True)
    if directory and not path.is_dir():raise ValueError('input/output directory missing')
    return path

def build_candidate(source_repo:Path,candidate:Path,build_dir:Path)->dict:
    started=time.monotonic()
    # Output roots are evaluator-owned; refusing an unsafe root must not write
    # through it to an arbitrary host target.
    output=_root(build_dir,may_create=True)
    manifest=output/'build_manifest.json'
    if any((output/name).exists() or (output/name).is_symlink() for name in
           ('build_manifest.json','worker_result.json','repository','build_resources')):
        raise RuntimeError('existing build evidence cannot be overwritten')
    result={'valid':False,'classification':'infrastructure_failure','failure':'build_not_started',
            'causal_candidate_failure':False,'infrastructure_health':{'valid':False}}
    resource=None
    stdout_path,stderr_path=output/'build.stdout.log',output/'build.stderr.log'
    try:
        source=_root(source_repo);delivery=_root(candidate)
        if source==delivery or source==output or delivery==output or source in output.parents or delivery in output.parents:
            raise ValueError('build source/delivery/output roots overlap')
        worker=Path(__file__).with_name('build_worker.py')
        launcher=Path(__file__).with_name('build_launcher.py')
        protocol=Path(__file__).with_name('protocol.py')
        stable_product=Path(__file__).with_name('stable_product.py')
        argv=[BWRAP_EXECUTABLE,'--die-with-parent','--new-session','--unshare-pid','--unshare-net',
              '--unshare-ipc','--unshare-uts','--cap-drop','ALL','--clearenv']
        for path in ('/usr','/bin','/lib','/lib64'):
            if Path(path).exists():argv+=['--ro-bind',path,path]
        argv+=['--proc','/proc','--dev','/dev','--tmpfs','/tmp',
               '--ro-bind',str(source),str(source),'--ro-bind',str(delivery),str(delivery),
               '--bind',str(output),'/output','--dir','/trusted',
               '--ro-bind',str(COMPILER_RUNTIME),'/compiler',
               '--ro-bind',str(worker),'/trusted/build_worker.py',
               '--ro-bind',str(protocol),'/trusted/protocol.py',
               '--ro-bind',str(stable_product),'/trusted/stable_product.py',
               '--setenv','PATH','/usr/bin:/bin','--setenv','LANG','C.UTF-8',
               '--setenv','HOME','/tmp','--setenv','PYTHONDONTWRITEBYTECODE','1','--chdir','/output',
               '/compiler/lib64/ld-linux-x86-64.so.2','--library-path',
               '/compiler/usr/local/lib:/compiler/lib/x86_64-linux-gnu:/compiler/usr/lib/x86_64-linux-gnu',
               COMPILER_EXECUTABLE,'-I','-B','/trusted/build_worker.py',
               '--source',str(source),'--candidate',str(delivery),'--git',GIT_EXECUTABLE,
               '--private-probes',json.dumps(list(BOUNDARY_PROBE_PATHS)),
               '--network-probe-port',str(BOUNDARY_PROBE_PORT)]
        # Full compiler prerequisite validation is inside the same build scope.
        # The host interpreter only checks evaluator-owned bytes and execs bwrap.
        command=[PYTHON_EXECUTABLE,'-I',str(launcher),'--runtime',str(COMPILER_RUNTIME),
                 '--manifest-sha256',COMPILER_MANIFEST_SHA256,'--image-id',COMPILER_IMAGE_ID,'--',*argv]
        proc,resource=run_owned(command,cwd='/',env={'PATH':'/usr/bin:/bin','LANG':'C.UTF-8'},
            output=output/'build_resources',timeout=BUILD_WORK_SECONDS,purpose='build')
        stdout_path.write_text(proc.stdout);stderr_path.write_text(proc.stderr)
        worker_result=output/'worker_result.json'
        if worker_result.is_file() and not worker_result.is_symlink():
            value=json.loads(worker_result.read_text())
            if isinstance(value,dict):result=value
        else:result.update(failure='isolated_build_worker_failed',worker_exit_code=proc.returncode)
        resources_ok=resource.get('valid') is True and not resource.get('timed_out') and resource.get('cleanup',{}).get('complete') is True and resource.get('aggregate_cleanup',{}).get('complete',True) is True
        if not resources_ok or proc.returncode!=0:
            result.update(valid=False,classification='infrastructure_failure',failure='build_resource_or_worker_failure',
                causal_candidate_failure=False)
            result['infrastructure_health']={**result.get('infrastructure_health',{}),'valid':False}
        else:
            result['causal_candidate_failure']=bool(result.pop('candidate_syntax_failure',False)
                and result.get('infrastructure_health',{}).get('valid') is True
                and result.get('failure')=='python_compileall' and result.get('patch_apply',{}).get('exit_code')==0)
    except Exception as exc:
        result.update(valid=False,classification='infrastructure_failure',failure='build_setup_failure',
            causal_candidate_failure=False,setup_error_type=type(exc).__name__,
            infrastructure_health={'valid':False})
        if not stdout_path.exists():stdout_path.write_text('')
        if not stderr_path.exists():stderr_path.write_text(type(exc).__name__+': '+str(exc)[:500]+'\n')
        resource_path=output/'build_resources/resource-attestation.json'
        if resource_path.is_file():resource=json.loads(resource_path.read_text())
    result.pop('candidate_syntax_failure',None)
    elapsed=round(time.monotonic()-started,3)
    if elapsed>BUILD_WALL_SECONDS:
        result.update(valid=False,classification='infrastructure_failure',failure='aggregate_build_wall_budget_exceeded',causal_candidate_failure=False)
        result['infrastructure_health']={**result.get('infrastructure_health',{}),'valid':False}
    result.update(build_manifest_path=str(manifest),build_wall_budget_seconds=BUILD_WALL_SECONDS,
        build_work_deadline_seconds=BUILD_WORK_SECONDS,cleanup_reserve_seconds=BUILD_WALL_SECONDS-BUILD_WORK_SECONDS,
        elapsed_seconds=elapsed,stdout_path=str(stdout_path),stderr_path=str(stderr_path),
        resource_contract=resource,resource_attestation_path=None,resource_attestation_sha256=None,
        candidate_code_executed=False)
    result['compiler_runtime']={'image_id':COMPILER_IMAGE_ID,'root':str(COMPILER_RUNTIME),
        'manifest_sha256':COMPILER_MANIFEST_SHA256,'required_python':'3.11.16',
        'source':'same immutable image as native lower product',
        'host_python_role':'trusted prerequisite validation only; no Candidate compilation'}
    if resource is not None:
        path=output/'build_resources/resource-attestation.json'
        result.update(resource_attestation_path=str(path),
            resource_attestation_sha256=hashlib.sha256(path.read_bytes()).hexdigest())
    health_path=output/'infrastructure_health.json';write_json(health_path,result['infrastructure_health'])
    result.update(infrastructure_health_path=str(health_path),
        infrastructure_health_sha256=hashlib.sha256(health_path.read_bytes()).hexdigest())
    write_json(manifest,result)
    return result

def main():
    p=argparse.ArgumentParser();p.add_argument('--source-repository',type=Path,required=True);p.add_argument('--candidate',type=Path,required=True);p.add_argument('--build-dir',type=Path,required=True);p.add_argument('--result',type=Path,required=True);a=p.parse_args()
    result=build_candidate(a.source_repository,a.candidate,a.build_dir)
    if a.result.absolute()!=Path(result['build_manifest_path']):write_json(a.result,result)
    print(json.dumps(result,indent=2,sort_keys=True));return 0 if result.get('valid') else 1
if __name__=='__main__':raise SystemExit(main())
