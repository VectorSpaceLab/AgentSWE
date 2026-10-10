"""One task-owned case envelope covering fixture, model, product, observer and cleanup."""
from __future__ import annotations
import argparse,json,os,sys,time
from pathlib import Path
from evaluator.harness.owned_resources import run_owned,_ambient_aggregate,MEMORY_BYTES


def run(script: Path) -> int:
    parser=argparse.ArgumentParser(add_help=False)
    parser.add_argument('--output-dir',type=Path,required=True)
    parser.add_argument('--timeout',type=int,default=600)
    options,_=parser.parse_known_args()
    if not 0 < options.timeout <= 600:
        raise ValueError('Codex case total budget is at most 600 seconds')
    if os.environ.get('AGENTSWE_CODEX_CASE_OWNED')=='1':
        if _ambient_aggregate() is None:
            raise RuntimeError('case child is outside verified aggregate scope')
        return None
    output=options.output_dir.resolve()
    if output.exists() and any(output.iterdir()):
        raise ValueError('case output must be new; preserve previous evidence')
    envelope=output.parent/(output.name+'-owned-resources')
    reserve=min(90,max(1,options.timeout//3))
    work=options.timeout-reserve
    if work<=0:raise ValueError('case budget cannot cover work and cleanup')
    started=time.monotonic();done=None;error=None
    try:
        done,attestation=run_owned([sys.executable,str(script),*sys.argv[1:],'--timeout',str(work)],
            cwd=script.parents[2],env={**os.environ,'AGENTSWE_CODEX_CASE_OWNED':'1'},
            output=envelope,timeout=work,memory_bytes=MEMORY_BYTES,purpose='case')
    except Exception as exc:
        error=type(exc).__name__+': '+str(exc)
        attestation={}
    elapsed=time.monotonic()-started
    valid=bool(done is not None and done.returncode == 0 and (output/'result.json').is_file() and attestation.get('valid') and not attestation.get('timed_out') and
        attestation.get('cleanup',{}).get('complete') and attestation.get('aggregate_cleanup',{}).get('complete') and elapsed<=options.timeout)
    output.mkdir(parents=True,exist_ok=True)
    proof={'schema_version':'agentswe-codex-whole-case-envelope/v1','valid':valid,'total_budget_seconds':options.timeout,
        'work_budget_seconds':work,'cleanup_and_finalization_reserve_seconds':reserve,'elapsed_seconds':elapsed,
        'aggregate_memory_bytes':MEMORY_BYTES,'aggregate_swap_bytes':0,'resource_attestation':str(envelope/'resource-attestation.json'),
        'scope':'fixture initialization, model/Product processes and Docker children, durable observer, artifact capture, cleanup',
        'source':'actual systemd properties and cgroup files; environment marker alone is not evidence','error':error}
    (output/'case_envelope.json').write_text(json.dumps(proof,indent=2)+'\n')
    if done is not None:
        (output/'case_envelope.stdout.log').write_text(done.stdout or '')
        (output/'case_envelope.stderr.log').write_text(done.stderr or '')
    result_path=output/'result.json'
    result=json.loads(result_path.read_text()) if result_path.is_file() else {}
    result['whole_case_resources']=proof
    if not valid:
        result.update(classification='evaluator_infrastructure_failure',infrastructure_invalid=True,score=None,
            error='whole-case resource envelope failed or exceeded total budget; inspect immutable owned-resource evidence')
    result_path.write_text(json.dumps(result,indent=2)+'\n')
    return (done.returncode if done is not None else 78) if valid else 78
