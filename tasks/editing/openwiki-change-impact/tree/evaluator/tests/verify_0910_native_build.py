"""Real offline baseline build through the complete owned Candidate build entry.

A diagnostic patch adds only a labelled text note; no repaired Candidate or
benchmark capability result is manufactured. All compile/import scripts are
actual source scripts inside the same sandbox used for submitted Candidates.
"""
import argparse,json,os,sys
from pathlib import Path
ROOT=Path(__file__).resolve().parents[2];sys.path.insert(0,str(ROOT))
from agentloop.candidate_adapter import build_candidate,_run,TASK_NODE
from agentloop.protocol import tree_digest

def main():
 p=argparse.ArgumentParser();p.add_argument('--output',type=Path,required=True);a=p.parse_args();a.output.mkdir(parents=True,exist_ok=False)
 source=(ROOT/'.runtime/candidate-smoke/repository').resolve();before=tree_digest(source)
 delivery=a.output/'scripted-delivery';delivery.mkdir()
 (delivery/'solution.patch').write_text('diff --git a/agentswe-native-build-probe.txt b/agentswe-native-build-probe.txt\nnew file mode 100644\n--- /dev/null\n+++ b/agentswe-native-build-probe.txt\n@@ -0,0 +1 @@\n+Scripted native build boundary probe only; no Candidate capability claim.\n')
 for name in ('edit_report.json','run_report.json'):(delivery/name).write_text('{"scripted_diagnostic_only":true,"benchmark_candidate":false}\n')
 result=build_candidate(source,delivery,a.output/'build',True)
 canary=a.output/'evaluator-private-canary.txt';canary.write_text('synthetic boundary value, not a credential')
 code="const fs=require('fs');console.log(JSON.stringify({privateVisible:fs.existsSync("+json.dumps(str(canary))+"),inheritedSecret:'AGENTSWE_SYNTHETIC_SECRET' in process.env,node:process.version}));"
 state=a.output/'boundary-state';state.mkdir();os.environ['AGENTSWE_SYNTHETIC_SECRET']='scripted-test'
 try:boundary=_run([str(TASK_NODE),'-e',code],a.output/'build/repository',30,state_dir=state)
 finally:os.environ.pop('AGENTSWE_SYNTHETIC_SECRET',None)
 observed=json.loads(boundary['stdout_tail']) if boundary['exit_code']==0 else {};resources=result.get('build_resource_contract',{});native=result.get('native_runtime',{})
 checks={'actual_baseline_compilation_completed':result.get('valid') is True and (a.output/'build/repository/dist/cli.js').is_file(),
  'actual_native_rebuild_and_SQLite_roundtrip':native.get('valid') is True and native.get('rebuild',{}).get('exit_code')==0,
  'copy_install_rebuild_compile_in_one_600s_4GiB_scope':resources.get('memory_bytes')==4294967296 and resources.get('timeout_seconds')==600 and resources.get('elapsed_seconds',601)<=600,
  'native_rebuild_inherits_build_scope':native.get('rebuild',{}).get('resource_contract',{}).get('cgroup')==resources.get('cgroup'),
  'compile_inherits_build_scope':result.get('pnpm_build',{}).get('resource_contract',{}).get('cgroup')==resources.get('cgroup'),
  'build_subtree_cleaned':resources.get('cleanup',{}).get('complete') is True,
  'actual_build_child_private_file_inaccessible':observed.get('privateVisible') is False,
  'actual_build_child_does_not_inherit_secret':observed.get('inheritedSecret') is False,
  'original_baseline_source_unchanged':tree_digest(source)==before}
 report={'passed':all(checks.values()),'checks':checks,'provider_calls':0,'benchmark_candidate_created':False,'source_digest':before,'build':result,'boundary_probe':boundary}
 (a.output/'verification.json').write_text(json.dumps(report,indent=2)+'\n');print(json.dumps({'passed':report['passed'],'checks':checks,'classification':result.get('classification'),'failure':result.get('failure')}),flush=True);return 0 if report['passed'] else 2
if __name__=='__main__':raise SystemExit(main())
