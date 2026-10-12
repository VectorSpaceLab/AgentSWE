"""One owned 6900s lower suite: fresh build, probes, six cases, final reserve.

Semantic Result/Code judges are evaluator work with their separate fixed judge
budgets, not extra time granted to the lower Agent. No model API is called here.
"""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import json
import math
from pathlib import Path
import subprocess
import sys
import time

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from lower_agent.owned_resources import run_owned, stop_owned, MEMORY_BYTES
from evaluator.case_budget import CASE_SECONDS

# Six full 930s case envelopes (300s evaluator setup allowance + 600s agent
# clock + 30s reserve) plus the cold build and the final assembly.  The 0919
# formal suite used 3133.9s of the former 4800s with six 600s envelopes; the
# worst case here is 250s of preparation plus 6*930 = 5830s, so 6900 keeps the
# same kind of headroom the 4800 envelope had.
SUITE_SECONDS = 6900
FINAL_RESERVE_SECONDS = 30


class SuiteBudgetExpired(TimeoutError):
    """Missing execution from an exhausted evaluator envelope is not a zero."""


class SuiteClock:
    def __init__(self, *, started, deadline, case_ids, clock=time.monotonic):
        if (not math.isfinite(started) or not math.isfinite(deadline)
                or not 0 < deadline-started <= SUITE_SECONDS):
            raise ValueError('suite wall clock must be finite and at most6900s')
        if not case_ids or len(set(case_ids)) != len(case_ids):
            raise ValueError('suite case inventory must be nonempty and distinct')
        self.started, self.deadline, self.case_ids, self.clock = started, deadline, tuple(case_ids), clock
        self.events = []
        self.next_case = 0

    @property
    def work_deadline(self):
        return self.deadline - FINAL_RESERVE_SECONDS

    def checkpoint(self, phase):
        current = self.clock()
        self.events.append({'phase': phase, 'elapsed_seconds': current-self.started})
        if current >= self.work_deadline:
            raise SuiteBudgetExpired('suite work budget exhausted before ' + phase)
        return current

    def build_deadline(self):
        current = self.checkpoint('build_admission')
        # Preserve the full930s per new case. The build's existing1800s cap
        # is also bounded by the published6900s envelope, not added to it.
        end = min(current + 1800, self.work_deadline-len(self.case_ids)*CASE_SECONDS)
        if end <= current:
            raise SuiteBudgetExpired('insufficient suite budget for build plus full case envelopes')
        return end

    def begin_case(self, case_id):
        if self.next_case >= len(self.case_ids) or self.case_ids[self.next_case] != case_id:
            raise ValueError('case order changed, repeated, or outside suite inventory')
        current = self.checkpoint(case_id + ':preparation')
        if current + (len(self.case_ids)-self.next_case)*CASE_SECONDS > self.work_deadline:
            raise SuiteBudgetExpired('remaining suite budget cannot preserve full case envelopes')
        self.next_case += 1
        return current, current + CASE_SECONDS

    def snapshot(self):
        elapsed = self.clock()-self.started
        return {'schema_version': 'openclaw-suite-clock-v1', 'maximum_seconds': SUITE_SECONDS,
            'started_monotonic': self.started, 'deadline_monotonic': self.deadline,
            'work_deadline_monotonic': self.work_deadline,
            'final_reserve_seconds': FINAL_RESERVE_SECONDS, 'case_seconds': CASE_SECONDS,
            'case_inventory': list(self.case_ids), 'admitted_cases': self.next_case,
            'elapsed_seconds': elapsed, 'within_total_budget': elapsed <= self.deadline-self.started,
            'events': list(self.events),
            'scope': 'lower suite setup/source validation/cold build/probes/cases/cleanup/result assembly',
            'semantic_judges': 'separate evaluator-owned budgets, no lower execution during judging'}


def write(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name+'.tmp')
    temporary.write_text(json.dumps(value,indent=2)+'\n')
    temporary.replace(path)


def cleanup_nested(control, output, cases):
    """Only predeclared evaluator-owned scope paths, never scan Candidate files."""
    paths = [control/'runtime-product-build-evidence/owned-resources/scope-ownership.json']
    paths += [output/case/'case_resources/scope-ownership.json' for case in cases]
    cleanup=[]
    for path in paths:
        if not path.exists():
            cleanup.append({'ownership_file': str(path), 'started': False})
            continue
        try:
            expected=json.loads(path.read_text())
            cleanup.append({'ownership_file': str(path), 'started': True, **stop_owned(expected)})
        except Exception as exc:
            cleanup.append({'ownership_file': str(path), 'started': True, 'complete': False,
                            'error': type(exc).__name__+': '+str(exc)})
    return cleanup


def run_hidden_owned(**arguments):
    started=time.monotonic()
    output=Path(arguments['output']).resolve()
    if output.exists() and any(output.iterdir()):
        raise FileExistsError('prior hidden suite exists; preserve it rather than resampling')
    control=output.parent/(output.name+'-suite-control')
    control.mkdir(parents=True,exist_ok=False)
    deadline=started+SUITE_SECONDS
    request={'schema_version':'openclaw-owned-suite-request/v1','started':started,
        'deadline':deadline,'arguments':{k:str(v.resolve()) if isinstance(v,Path) else v for k,v in arguments.items()}}
    request_path=control/'request.json';write(request_path,request)
    resource=None;process=None;error=None
    try:
        process,resource=run_owned(['/usr/bin/python3','-B',str(Path(__file__).resolve()),
            '--request',str(request_path)],cwd=ROOT,env={'PATH':'/usr/bin:/bin','LANG':'C.UTF-8'},
            output=control/'suite-resources',timeout=deadline-time.monotonic(),
            memory_bytes=MEMORY_BYTES,purpose='suite')
        (control/'worker.stdout').write_text(process.stdout or '')
        (control/'worker.stderr').write_text(process.stderr or '')
    except Exception as exc:
        error=type(exc).__name__+': '+str(exc)
    finally:
        cleanup=cleanup_nested(control,output,arguments['case_ids'])
    complete=all(not row['started'] or row.get('complete') is True for row in cleanup)
    result_path=control/'worker-result.json'
    result=json.loads(result_path.read_text()) if result_path.is_file() else {}
    valid=bool(resource and resource.get('valid') and not resource.get('timed_out')
        and resource.get('cleanup',{}).get('complete') and complete and process
        and process.returncode==0 and result_path.is_file() and time.monotonic()<=deadline)
    attestation={'schema_version':'openclaw-owned-suite-attestation/v1',
        'created_at':datetime.now(timezone.utc).isoformat(),'resources':resource,
        'nested_cleanup':cleanup,'cleanup_complete':complete,'scope_valid':valid,
        'elapsed_seconds':time.monotonic()-started,'maximum_seconds':SUITE_SECONDS,
        'error':error,'worker_result':str(result_path),'semantic_judges_run_inside_suite':False}
    write(control/'attestation.json',attestation)
    if not valid:
        result.update(formal_evidence_valid=False,pilot_evidence_complete=False,smoke_evidence_complete=False,
            classification='launcher_infrastructure_error',formal_result_claimed=False,
            classification_reason='owned lower suite did not complete within its verified resource envelope',
            result_axis='N/A')
    result['suite_resource_attestation']=str(control/'attestation.json')
    result['suite_resource_valid']=valid
    write(output/'summary.json',result)
    return result


def worker(request_path):
    from evaluator.hidden_executor import run_suite
    request=json.loads(request_path.read_text())
    assert request['schema_version']=='openclaw-owned-suite-request/v1'
    arguments=request['arguments']
    for key in ('freeze_manifest_path','hidden_root','output','runtime','builder_attestation_path','runtime_product_manifest_path'):
        if arguments.get(key) is not None:arguments[key]=Path(arguments[key])
    arguments['case_ids']=tuple(arguments['case_ids'])
    clock=SuiteClock(started=request['started'],deadline=request['deadline'],case_ids=arguments['case_ids'])
    context={'clock':clock,'control':request_path.parent}
    try:
        result=run_suite(**arguments,_suite_context=context)
    except Exception as exc:
        result={'formal_evidence_valid':False,'pilot_evidence_complete':False,
            'classification':'launcher_infrastructure_error','formal_result_claimed':False,
            'classification_reason':type(exc).__name__+': '+str(exc),'result_axis':'N/A'}
    finally:
        write(request_path.parent/'suite-clock.json',clock.snapshot())
    result['suite_clock']=clock.snapshot()
    write(request_path.parent/'worker-result.json',result)
    return 0


if __name__=='__main__':
    parser=argparse.ArgumentParser();parser.add_argument('--request',type=Path,required=True)
    raise SystemExit(worker(parser.parse_args().request.resolve()))
