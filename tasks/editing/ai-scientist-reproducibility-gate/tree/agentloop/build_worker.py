"""Trusted build worker. Compiles syntax only; never imports Candidate code."""
from __future__ import annotations
import argparse,hashlib,json,os,shutil,socket,stat,subprocess,sys
from pathlib import Path
sys.path.insert(0,'/trusted')
from protocol import tree_digest,write_json,changed_paths,safe_patch_paths
from stable_product import product_source_digest
OUT=Path('/output')
MAX_DIAGNOSTICS=20

class UnsafeTree(ValueError):pass

def validate_tree(root):
    for directory,dirs,names in os.walk(root,followlinks=False):
        for name in dirs+names:
            path=Path(directory)/name;mode=path.lstat().st_mode
            if stat.S_ISLNK(mode):
                try:target=path.resolve(strict=True);target.relative_to(root)
                except (ValueError,OSError,RuntimeError):raise UnsafeTree('external or unresolved symlink: '+path.relative_to(root).as_posix())
            elif not (stat.S_ISDIR(mode) or stat.S_ISREG(mode)):
                raise UnsafeTree('special source file: '+path.relative_to(root).as_posix())

def compile_tree(root):
    diagnostics=[];count=0;syntax=True
    for directory,dirs,names in os.walk(root,followlinks=False):
        dirs[:]=sorted(d for d in dirs if d not in {'.git','__pycache__'} and not (Path(directory)/d).is_symlink())
        for name in sorted(names):
            path=Path(directory)/name
            if path.suffix!='.py' or path.is_symlink():continue
            count+=1
            try:compile(path.read_bytes(),path.relative_to(root).as_posix(),'exec',dont_inherit=True)
            except SyntaxError as exc:
                if len(diagnostics)<MAX_DIAGNOSTICS:diagnostics.append({'path':path.relative_to(root).as_posix(),
                    'line':exc.lineno,'offset':exc.offset,'exception':type(exc).__name__,'message':str(exc.msg)[:300]})
            except (OSError,MemoryError,ValueError) as exc:
                syntax=False
                if len(diagnostics)<MAX_DIAGNOSTICS:diagnostics.append({'path':path.relative_to(root).as_posix(),
                    'exception':type(exc).__name__,'message':'compiler could not read or compile this input'})
    return {'valid':not diagnostics,'python_files':count,'diagnostics':diagnostics,
        'only_syntax_errors':bool(diagnostics) and syntax,'candidate_code_executed':False,
        'compiler':sys.version.split()[0],'mechanism':'native compile(bytes, relative_filename, exec), no exec/import/bytecode writes'}

def command(argv,cwd):
    env={'PATH':'/usr/bin:/bin','LANG':'C.UTF-8','HOME':'/tmp','GIT_CONFIG_NOSYSTEM':'1','GIT_CONFIG_GLOBAL':'/dev/null','GIT_TERMINAL_PROMPT':'0'}
    p=subprocess.run(argv,cwd=cwd,env=env,capture_output=True,text=True,timeout=60)
    return {'exit_code':p.returncode,'stdout':p.stdout[-2000:],'stderr':p.stderr[-2000:]}

def run(a):
    health={'valid':False,'filesystem_isolated':True,'network_namespace':os.readlink('/proc/self/ns/net'),
        'compiler_version':sys.version.split()[0],'compiler_executable':sys.executable,
        'required_compiler_version':'3.11.16',
        'candidate_code_executed':False,'synthetic_ambient_secret_absent':'AGENTSWE_BUILD_SYNTHETIC_SECRET' not in os.environ,
        'private_probe_visible':{p:Path(p).exists() for p in json.loads(a.private_probes)}}
    if a.network_probe_port:
        try:
            connection=socket.create_connection(('127.0.0.1',a.network_probe_port),timeout=.2);connection.close();health['host_loopback_reachable']=True
        except OSError:health['host_loopback_reachable']=False
    result={'valid':False,'classification':'infrastructure_failure','failure':'build_worker_initialization',
        'infrastructure_health':health,'compile_diagnostics':[]}
    source=Path(a.source);candidate=Path(a.candidate);repo=OUT/'repository'
    try:
        # The public contract and actual lower image are Python 3.11. A host
        # 3.10 parser must never turn valid except* syntax into Candidate zero.
        if sys.version_info[:3]!=(3,11,16):
            result.update(failure='python_version_mismatch');return result
        if not source.is_dir() or not candidate.is_dir():raise UnsafeTree('source or delivery missing')
        validate_tree(source)
        for name in ('solution.patch','edit_report.json','run_report.json'):
            p=candidate/name
            if p.is_symlink() or not p.is_file():raise UnsafeTree('delivery file is not a regular non-symlink file: '+name)
        names={p.name for p in candidate.iterdir()}
        if names!={'solution.patch','edit_report.json','run_report.json'}:
            result.update(classification='candidate_delivery_failure',failure='delivery_inventory');return result
        for name in ('edit_report.json','run_report.json'):
            if not isinstance(json.loads((candidate/name).read_text()),dict):
                result.update(classification='candidate_delivery_failure',failure='delivery_report_schema');return result
        result['source_digest']=tree_digest(source);result['candidate_digest']=tree_digest(candidate)
        baseline=compile_tree(source);write_json(OUT/'baseline_compile.json',baseline)
        health['baseline_compile']=baseline;health['baseline_source_digest']=result['source_digest']
        if not baseline['valid'] or baseline['python_files']==0:
            result.update(failure='baseline_python_compileall');return result
        health['git_probe']=command([a.git,'--version'],OUT)
        if health['git_probe']['exit_code']!=0:result.update(failure='git_tool_preflight');return result
        shutil.copytree(source,repo,symlinks=True,ignore=shutil.ignore_patterns('.git','__pycache__'))
        # Absolute internal source links become internal links in the copy.
        for path in repo.rglob('*'):
            if path.is_symlink() and Path(os.readlink(path)).is_absolute():
                original=source/path.relative_to(repo);relative=original.resolve().relative_to(source)
                path.unlink();path.symlink_to(os.path.relpath(repo/relative,path.parent))
        validate_tree(repo)
        git=[a.git,'-c','core.hooksPath=/dev/null','-c','commit.gpgSign=false']
        for label,argv in [('git_init',git+['init','-q']),('git_add',git+['add','-A']),
            ('git_commit',git+['-c','user.name=AgentSWE baseline','-c','user.email=baseline@example.invalid','commit','--allow-empty','-qm','baseline'])]:
            detail=command(argv,repo);result[label]=detail
            if detail['exit_code']!=0:result.update(failure=label);return result
        health['valid']=True
        patch=candidate/'solution.patch';paths=changed_paths(patch);result['changed_paths']=paths
        if not safe_patch_paths(paths) or any('.git' in Path(p).parts for p in paths):
            result.update(classification='candidate_delivery_failure',failure='unsafe_or_empty_patch_paths');return result
        for label,argv in [('patch_check',git+['apply','--check',str(patch)]),('patch_apply',git+['apply',str(patch)])]:
            detail=command(argv,repo);result[label]=detail
            if detail['exit_code']!=0:
                result.update(classification='candidate_build_failure' if label=='patch_check' else 'infrastructure_failure',failure=label)
                if label=='patch_apply':health['valid']=False
                return result
        try:validate_tree(repo)
        except UnsafeTree:
            result.update(classification='candidate_delivery_failure',failure='patched_tree_external_or_invalid_symlink');return result
        result['product_source_digest']=product_source_digest(repo)
        result['product_source_digest_stage']='after_patch_apply_before_compile'
        compiled=compile_tree(repo);write_json(OUT/'candidate_compile.json',compiled)
        result.update(python_compileall=compiled['valid'],compile_diagnostics=compiled['diagnostics'],
            candidate_syntax_failure=compiled['only_syntax_errors'],
            target_entry_candidates=[str(p.relative_to(repo)) for p in (repo/'ai_scientist/claim_verification.py',repo/'launch_scientist_bfts.py') if p.is_file()])
        if compiled['valid']:result.update(valid=True,classification='candidate_ready',failure=None)
        elif compiled['only_syntax_errors']:result.update(classification='candidate_build_failure',failure='python_compileall')
        else:result.update(failure='candidate_compile_infrastructure');health['valid']=False
        return result
    except (OSError,ValueError,RuntimeError,subprocess.SubprocessError) as exc:
        health['valid']=False;result.update(valid=False,classification='infrastructure_failure',
            failure='unsafe_source_or_delivery' if isinstance(exc,UnsafeTree) else 'build_tool_or_input_failure',
            error_type=type(exc).__name__,error_message=str(exc)[:500]);return result
    finally:
        if repo.is_dir():
            # Digest hashes symlink targets, never follows external file links.
            result['candidate_repo_digest']=tree_digest(repo)
        write_json(OUT/'worker_health.json',health)

def main():
    p=argparse.ArgumentParser();p.add_argument('--source',required=True);p.add_argument('--candidate',required=True);p.add_argument('--git',required=True);p.add_argument('--private-probes',default='[]');p.add_argument('--network-probe-port',type=int,default=0);a=p.parse_args()
    result=run(a);write_json(OUT/'worker_result.json',result)
    print(json.dumps({'valid':result['valid'],'classification':result['classification'],'failure':result.get('failure'),'compile_diagnostics':result.get('compile_diagnostics',[])}))
    return 0
if __name__=='__main__':raise SystemExit(main())
