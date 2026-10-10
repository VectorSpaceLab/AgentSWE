"""Public case setup, native lower and private observation share one budget."""
from __future__ import annotations
import argparse,json,shutil,subprocess,sys,time
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parent))
from owned_resources import run_owned
from candidate_adapter import tree_digest
from semantic_oracle import observe

def execute(request):
    repository,case,output=(Path(request[k]) for k in ('repository','case','output'))
    runtime=output/'runtime_repository';shutil.copytree(repository,runtime,symlinks=True)
    home=output/'deepcode-home';workspace=output/'workspace'
    command=[sys.executable,'-I',request['launcher'],'--repository',str(runtime),'--task-file',str(case/'input.md'),
        '--workspace',str(workspace),'--project-source',str(case/'assets/project'),'--deepcode-home',str(home),
        '--broker-endpoint',request['broker_endpoint'],'--output',str(output),'--case-id',request['case_id'],
        '--case-deadline-monotonic',str(request['deadline'])]
    if request.get('runtime_python'):command+=['--python',request['runtime_python']]
    try:
        process=subprocess.run(command,text=True,capture_output=True,timeout=max(.001,request['deadline']-time.monotonic()))
    except subprocess.TimeoutExpired as exc:
        process=subprocess.CompletedProcess(command,124,exc.stdout or '',exc.stderr or '')
    (output/'launcher.stdout.log').write_text(process.stdout or '')
    (output/'launcher.stderr.log').write_text(process.stderr or '')
    try:result=json.loads((output/'result.json').read_text())
    except (OSError,ValueError):result={'classification':'launcher_infrastructure_error','infra_valid':False,'stderr':(process.stderr or '')[-1000:]}
    result.update(controller_process_exit_code=process.returncode,launcher_stdout=str(output/'launcher.stdout.log'),
        launcher_stderr=str(output/'launcher.stderr.log'),candidate_repository=str(repository),runtime_repository=str(runtime),
        candidate_digest_after=tree_digest(repository))
    oracle=observe(request['case_id'],project=case/'assets/project',workspace=workspace,home=home)
    (output/'private-semantic-oracle.json').write_text(json.dumps(oracle,indent=2)+'\n')
    (output/'controller_execution_result.json').write_text(json.dumps(result,indent=2)+'\n')
    return result

def main():
    parser=argparse.ArgumentParser();parser.add_argument('--request',type=Path,required=True);parser.add_argument('--inside',action='store_true')
    args=parser.parse_args();request=json.loads(args.request.read_text())
    if args.inside:execute(request);return 0
    # run_owned arms the scope with RuntimeMaxSec=590 from a LATER instant than
    # this one, so a +590 inner deadline leaves the case driver no time to write
    # its timed-out record before SIGTERM.  Reserve 30 s inside the scope.
    output=Path(request['output']);deadline=time.monotonic()+590-30
    request['deadline']=deadline;args.request.write_text(json.dumps(request,indent=2)+'\n')
    process,att=run_owned([sys.executable,'-I',str(Path(__file__).resolve()),'--inside','--request',str(args.request)],
        cwd=Path('/'),env={'PATH':'/usr/bin:/bin','LANG':'C.UTF-8'},output=output.with_name(output.name+'-case-resources'),timeout=600)
    (output.parent/(output.name+'-case.stdout.log')).write_text(process.stdout or '')
    (output.parent/(output.name+'-case.stderr.log')).write_text(process.stderr or '')
    return process.returncode
if __name__=='__main__':raise SystemExit(main())
