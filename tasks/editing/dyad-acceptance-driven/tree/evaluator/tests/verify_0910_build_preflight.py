"""Run actual offline compiler/resource controls against a supplied task stage."""
import argparse
import hashlib
import json
from pathlib import Path
import shutil
import sys

p = argparse.ArgumentParser(); p.add_argument('--source', type=Path, required=True); p.add_argument('--run', type=Path, required=True)
a = p.parse_args(); a.run.mkdir(parents=True, exist_ok=False)
sys.path[:0] = [str(a.source), str(a.source/'harbor')]
from harbor.build_preflight import run_typecheck
repo = a.source/'input/repository'
source_files=['harbor/build_preflight.py','harbor/formal_one_stop.py','input/04_resources.md','evaluator/tests/test_0910_build_preflight.py','evaluator/tests/verify_0910_build_preflight.py']
source_hashes = {name:hashlib.sha256((a.source/name).read_bytes()).hexdigest() for name in source_files}
checks = []
results = {}

def record(name, value, expected):
    results[name] = value
    checks.append({'name': name, 'expected': expected, 'actual': value['classification'],
                   'passed': value['classification'] == expected,
                   'cleanup': value['resource_attestation']['cleanup']})
    print(name, value['classification'], value.get('exit_code'), flush=True)
    (a.run/'progress.json').write_text(json.dumps({'checks':checks},indent=2)+'\n')

def fixture(name, source=repo, text=None):
    path = a.run/'fixtures'/name
    shutil.copytree(source, path, symlinks=True, ignore=shutil.ignore_patterns('.git', 'node_modules', 'out'))
    if text is not None: (path/'src/__dyad_build_probe.ts').write_text(text)
    return path

baseline = run_typecheck(repo, dependency_root=repo, output=a.run/'baseline')
record('current_baseline', baseline, 'baseline_typecheck_healthy')
if not checks[-1]['passed']: raise SystemExit('Current real baseline was not healthy; no Candidate fault controls may be claimed.')
syntax = fixture('syntax', text='export const dyadBuildProbe: number = ;\n')
record('new_syntax_error', run_typecheck(syntax,dependency_root=repo,output=a.run/'syntax',baseline=baseline), 'candidate_build_failure')
type_error = fixture('type-error', text='export const dyadBuildProbe: number = "incorrect";\n')
record('new_type_error', run_typecheck(type_error,dependency_root=repo,output=a.run/'type-error',baseline=baseline), 'candidate_build_failure')
missing = fixture('missing-module', text='import missing from "__dyad_build_dependency_absent__";\nexport default missing;\n')
record('missing_dependency', run_typecheck(missing,dependency_root=repo,output=a.run/'missing-module',baseline=baseline), 'infrastructure_invalid')
record('missing_prepared_tree', run_typecheck(repo,dependency_root=a.run/'absent',output=a.run/'missing-prepared',baseline=baseline), 'infrastructure_invalid')
dirty = run_typecheck(type_error,dependency_root=repo,output=a.run/'dirty-baseline')
record('known_baseline_error',dirty,'baseline_existing_type_errors')
shifted = fixture('shifted',source=type_error,text='\n\nexport const dyadBuildProbe: number = "incorrect";\n')
record('same_error_after_line_shift',run_typecheck(shifted,dependency_root=repo,output=a.run/'shifted',baseline=dirty),'ready_for_lower')
extra = fixture('extra',source=type_error,text='export const dyadBuildProbe: number = "incorrect";\nexport const additionalError: boolean = 17;\n')
record('new_error_on_dirty_baseline',run_typecheck(extra,dependency_root=repo,output=a.run/'extra',baseline=dirty),'candidate_build_failure')
record('bounded_setup_timeout',run_typecheck(repo,dependency_root=repo,output=a.run/'timeout',baseline=baseline,timeout=21),'infrastructure_invalid')
checks.append({'name':'timeout_observed','passed':results['bounded_setup_timeout'].get('timed_out') is True})
checks.append({'name':'all_owned_cleanup_complete','passed':all(value['resource_attestation']['cleanup']['complete'] for value in results.values())})
checks.append({'name':'source_unchanged_during_controls','passed':source_hashes == {name:hashlib.sha256((a.source/name).read_bytes()).hexdigest() for name in source_files}})
checks.append({'name':'actual_cgroup_limits_and_offline_namespace','passed':all(value.get('runtime_resources',{}).get('memory.max') == str(4*1024**3) and value['runtime_resources']['memory.swap.max']=='0' and value['runtime_resources']['network_offline'] is True for name,value in results.items() if name not in ('missing_prepared_tree','bounded_setup_timeout'))})
report={'schema_version':'dyad-build-controls-v1','source':str(a.source),'source_hashes':source_hashes,'checks':checks,'external_api_calls':0,'model_capability_acceptance':False,'passed':all(row['passed'] for row in checks)}
(a.run/'verification.json').write_text(json.dumps(report,indent=2)+'\n')
print(json.dumps(report,indent=2))
raise SystemExit(0 if report['passed'] else 1)
