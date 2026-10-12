"""Actual isolated Python 3.11 compiler controls; zero external API calls."""
import argparse,contextlib,hashlib,json,sys
from pathlib import Path
from unittest.mock import patch
ROOT=Path(__file__).resolve().parents[2]
sys.path.insert(0,str(ROOT))
from agentloop import candidate_adapter as adapter
from agentloop.protocol import tree_digest

def main():
    p=argparse.ArgumentParser();p.add_argument('--source',type=Path,required=True);p.add_argument('--output',type=Path,required=True);a=p.parse_args()
    a.output.mkdir(parents=True,exist_ok=False)
    before=tree_digest(a.source);rows=[]
    def control(name,content,expected,overrides=None):
        delivery=a.output/'deliveries'/name;delivery.mkdir(parents=True)
        lines=content.splitlines()
        text='diff --git a/ai_scientist/_version_probe.py b/ai_scientist/_version_probe.py\nnew file mode 100644\n--- /dev/null\n+++ b/ai_scientist/_version_probe.py\n@@ -0,0 +1,'+str(len(lines))+' @@\n'+''.join('+'+line+'\n' for line in lines)
        (delivery/'solution.patch').write_text(text)
        for filename in ('edit_report.json','run_report.json'):(delivery/filename).write_text('{}\n')
        out=a.output/'builds'/name
        with (patch.multiple(adapter,**overrides) if overrides else contextlib.nullcontext()):
            result=adapter.build_candidate(a.source,delivery,out)
        health=result['infrastructure_health'];resource=result.get('resource_contract') or {}
        checks={
            'expected_classification':result['classification']==expected,
            'manifest_equals_return':json.loads((out/'build_manifest.json').read_text())==result,
            'actual_owned_build_4GiB':resource.get('valid') is True and resource.get('purpose')=='build' and resource.get('memory_bytes')==4294967296,
            'deadline_including_cleanup':result['elapsed_seconds']<=1800 and resource.get('timeout_seconds')==1770 and not resource.get('timed_out'),
            'complete_cleanup':resource.get('cleanup',{}).get('complete') is True and resource.get('aggregate_cleanup',{}).get('complete',True) is True,
            'candidate_code_never_executed':result.get('candidate_code_executed') is False and not (out/'CANDIDATE_EXECUTED').exists(),
            'resource_hash_bound':hashlib.sha256(Path(result['resource_attestation_path']).read_bytes()).hexdigest()==result['resource_attestation_sha256'],
            'actual_post_compile_digest':not (out/'repository').exists() or tree_digest(out/'repository')==result.get('candidate_repo_digest'),
        }
        if expected!='infrastructure_failure':
            checks['same_product_python_31116']=health.get('compiler_version')=='3.11.16' and health.get('baseline_compile',{}).get('compiler')=='3.11.16'
            checks['healthy_real_baseline']=health.get('valid') is True and health.get('baseline_compile',{}).get('valid') is True and health['baseline_compile']['python_files']>0
        if name=='except_star_valid':
            checks['valid_python311_not_candidate_zero']=result['valid'] is True and result['python_compileall'] is True and result['causal_candidate_failure'] is False
        elif name=='genuine_syntax_error':
            checks['causal_syntax_failure']=result['causal_candidate_failure'] is True and result['failure']=='python_compileall'
            checks['native_bounded_syntax_diagnostics']=0<len(result.get('compile_diagnostics',[]))<=20 and all(v.get('exception')=='SyntaxError' and 'text' not in v and not v['path'].startswith('/') for v in result['compile_diagnostics'])
        else:
            checks['infrastructure_never_candidate_zero']=result['causal_candidate_failure'] is False and result['valid'] is False
        if name=='host_python310_rejected':checks['explicit_version_mismatch']=result['failure']=='python_version_mismatch' and health.get('compiler_version','').startswith('3.10.')
        row={'name':name,'passed':all(checks.values()),'checks':checks,'manifest':str(out/'build_manifest.json'),'manifest_sha256':hashlib.sha256((out/'build_manifest.json').read_bytes()).hexdigest(),'failure':result.get('failure'),'compiler':health.get('compiler_version'),'elapsed_seconds':result['elapsed_seconds']}
        rows.append(row);print(json.dumps(row),flush=True)
    marker='from pathlib import Path\nPath("/output/CANDIDATE_EXECUTED").write_text("bad")\n'
    valid=marker+'try:\n    raise ExceptionGroup("probe", [ValueError("x")])\nexcept* ValueError:\n    pass\n'
    control('except_star_valid',valid,'candidate_ready')
    control('genuine_syntax_error',marker+'def genuinely_broken(:\n','candidate_build_failure')
    control('missing_runtime',valid,'infrastructure_failure',{'COMPILER_RUNTIME':a.output/'missing-runtime'})
    control('manifest_digest_mismatch',valid,'infrastructure_failure',{'COMPILER_MANIFEST_SHA256':'0'*64})
    control('missing_python_version',valid,'infrastructure_failure',{'COMPILER_EXECUTABLE':'/compiler/usr/local/bin/python3.10'})
    control('host_python310_rejected',valid,'infrastructure_failure',{'COMPILER_EXECUTABLE':'/usr/bin/python3'})
    report={'passed':all(r['passed'] for r in rows) and tree_digest(a.source)==before,'cases':rows,'external_provider_calls':0,'candidate_code_executed':False,'source_digest_before':before,'source_digest_after':tree_digest(a.source),'compiler_cache':str(adapter.COMPILER_RUNTIME),'compiler_manifest_sha256':adapter.COMPILER_MANIFEST_SHA256,'fixed_product_image':adapter.COMPILER_IMAGE_ID}
    (a.output/'verification.json').write_text(json.dumps(report,indent=2)+'\n')
    return 0 if report['passed'] else 2

if __name__=='__main__':raise SystemExit(main())
