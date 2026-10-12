"""Public build, both dev cases and observers share the verified aggregate parent."""
from pathlib import Path
import argparse,json,sys,uuid
from types import SimpleNamespace
from typing import Any
ROOT=Path(__file__).resolve().parents[1]
sys.path[:0]=[str(ROOT),str(ROOT/'harbor')]
from lower_agent.owned_resources import run_owned,MEMORY_BYTES,_ambient_aggregate,verify_aggregate
from formal_one_stop import (tree_digest,read_json,write_json,allocate_runtime_attempt,
    prepare_candidate_runtime,write_runtime_manifest,unavailable_runtime_results,
    validate_runtime_manifest,write_private_case,launch_case,CASE_TIMEOUT_SECONDS,score_public_case)


def rotate_lower_ledger(endpoint: str, *, timeout: float = 10) -> dict:
    """Ask the evaluator-owned lower broker to start a fresh ledger."""
    import urllib.request
    from lower_agent.launcher import STATS_TOKEN, broker_stats_url
    url = broker_stats_url(endpoint).rsplit("/stats", 1)[0] + "/rotate"
    request = urllib.request.Request(url, data=b"", method="POST",
                                     headers={"Authorization": "Bearer " + STATS_TOKEN})
    with urllib.request.urlopen(request, timeout=timeout) as response:
        return json.loads(response.read())


def evaluate_local(*,run_dir,candidate,number,runtime,public_case_ids,public_lower_endpoint,judge_endpoint):
    unit=_ambient_aggregate()
    if not unit:raise RuntimeError('public evaluator worker must run inside its owned aggregate parent')
    verify_aggregate({'unit':unit,'cgroup':'/'+unit,'memory_bytes':MEMORY_BYTES})
    args=SimpleNamespace(runtime=runtime)
    candidate_digest = tree_digest(candidate)
    public_cache = run_dir / 'public' / 'by_candidate' / candidate_digest
    public_cache.mkdir(parents=True, exist_ok=True)
    runtime_ref = public_cache / 'runtime_reference.json'
    if runtime_ref.is_file():
        reference = read_json(runtime_ref)
        if reference.get('candidate_digest') != candidate_digest:
            raise ValueError('public runtime reference belongs to another Candidate')
        manifest_path = Path(reference['manifest_path'])
        manifest = read_json(manifest_path)
        runtime_output = Path(manifest['runtime_product'])
    else:
        runtime_output, manifest_path = allocate_runtime_attempt(run_dir / 'candidate_runtimes', number)
        manifest = prepare_candidate_runtime(candidate=candidate, output=runtime_output, runtime=args.runtime)
        write_runtime_manifest(manifest_path, manifest)
        if manifest.get('candidate_runtime_ready'):
            write_json(runtime_ref, {'candidate_digest': candidate_digest, 'manifest_path': str(manifest_path)})
    if not manifest.get("candidate_runtime_ready"):
        return {'results': unavailable_runtime_results(manifest, manifest_path, public_case_ids), 'runtime_manifest': str(manifest_path)}
    # One attempt, one ledger: the shared usage normalizer validates a lower
    # ledger as a whole, so an attempt that shares its file with a voided one
    # can never be attributed. Rotating here also keeps every voided attempt's
    # bytes, under the instance id that produced them.
    rotate_lower_ledger(public_lower_endpoint)
    results: dict[str, Any] = {}
    for case_id in public_case_ids:
        runtime_errors=validate_runtime_manifest(manifest,candidate)
        if runtime_errors:
            return {'results': unavailable_runtime_results({'classification':'build_infrastructure_error',
                'classification_reason':'; '.join(runtime_errors)},manifest_path,public_case_ids), 'runtime_manifest': str(manifest_path)}
        case_dir = ROOT / "dev_cases" / case_id
        case_output = public_cache / case_id
        captured = case_output / 'hidden_case_attestation.json'
        if captured.is_file():
            # Preserve completed lower execution if only judging failed.
            result = read_json(captured)
            if (result.get('case_id') != case_id
                    or result.get('frozen_candidate_digest_before') != manifest['runtime_source_digest']):
                raise ValueError('cached public execution does not match case/runtime identity')
        elif case_output.exists():
            result = {'case_id': case_id, 'classification': 'infrastructure-invalid',
                'classification_reason': 'prior lower attempt is unresolved; preserve it for recovery, do not resample'}
        else:
            private_dir = run_dir / '.public-evaluator-private' / candidate_digest / case_id
            private_file, view = write_private_case(private_dir, case_id)
            result = launch_case(
                case_id=case_id, hidden_case=case_dir, frozen_candidate=runtime_output,
                output=case_output, broker_endpoint=public_lower_endpoint, runtime=args.runtime,
                private_file=private_file, view=view, timeout_seconds=CASE_TIMEOUT_SECONDS,
                run_mode="formal", probe_reason=None,
                candidate_runtime_manifest_path=manifest_path,
            )
        result.update({"candidate_runtime_ready": True, "candidate_runtime_manifest": str(manifest_path)})
        results[case_id] = score_public_case(root=ROOT, candidate_digest=candidate_digest,
            record=result, case_output=case_output, broker_endpoint=judge_endpoint)
    return {'results': results, 'runtime_manifest': str(manifest_path)}


def evaluate_owned(*,run_dir,candidate,number,runtime,public_case_ids,public_lower_endpoint,judge_endpoint):
    output=Path(run_dir)/'public/evaluation_scopes'/uuid.uuid4().hex
    output.mkdir(parents=True,exist_ok=False)
    payload={'run_dir':str(run_dir),'candidate':str(candidate),'number':number,
        'runtime':str(runtime) if runtime else None,'public_case_ids':list(public_case_ids),
        'public_lower_endpoint':public_lower_endpoint,'judge_endpoint':judge_endpoint}
    config=output/'request.json';write_json(config,payload)
    command=['/usr/bin/python3','-E','-s','-B',str(Path(__file__).resolve()),'--config',str(config),'--result',str(output/'result.json')]
    process,resources=run_owned(command,cwd=ROOT,env={'PATH':'/usr/bin:/bin','LANG':'C.UTF-8'},
        output=output/'owned_resources',timeout=4770,memory_bytes=MEMORY_BYTES,purpose='suite')
    (output/'worker.stdout').write_text(process.stdout or '')
    (output/'worker.stderr').write_text(process.stderr or '')
    if process.returncode or not resources.get('valid') or resources.get('timed_out') or resources.get('elapsed_seconds',float('inf'))>4800 or not resources.get('cleanup',{}).get('complete') or (resources.get('aggregate_parent',{}).get('created_here') and not resources.get('aggregate_cleanup',{}).get('complete')):
        raise RuntimeError('public build/dev evaluator did not finish inside its owned aggregate budget; preserve '+str(output))
    return read_json(output/'result.json')


def main():
    parser=argparse.ArgumentParser();parser.add_argument('--config',type=Path,required=True);parser.add_argument('--result',type=Path,required=True);a=parser.parse_args()
    data=read_json(a.config)
    for key in ('run_dir','candidate','runtime'):
        if data[key] is not None:data[key]=Path(data[key])
    result=evaluate_local(**data)
    result['actual_parent_cgroup']=Path('/proc/self/cgroup').read_text()
    write_json(a.result,result)
if __name__=='__main__':main()
