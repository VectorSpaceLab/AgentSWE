from __future__ import annotations
import contextlib, hashlib, json, shutil, sys, tempfile
from pathlib import Path

PROFILE = 'single-dev-two-round-hidden-smoke-v1'
FILES = ('solution.patch','edit_report.json','run_report.json')
SHARED = Path('@@AGENTSWE_EDITING_CONTROL@@')

def shared_modules():
    if str(SHARED) not in sys.path: sys.path.insert(0,str(SHARED))
    import presubmit_validator
    return presubmit_validator

def delivery_digest(root):
    shared_modules()
    from v2_readiness import tree_digest
    return tree_digest(Path(root))

def _present_for_shared_validator(candidate):
    """Show the delivery to the shared validator under the key it reads.

    input/02_interface_and_delivery.md publishes the command evidence as
    `commands_and_results`; the shared presubmit_validator reads `commands`.
    A Candidate that followed the published spec is aliased here, inside a
    throwaway staging copy, so the Builder's own bytes -- and therefore its
    candidate digest -- are never touched.

    Only the missing alias is added. When `commands` is already present the
    delivery is passed through untouched, and the entries themselves are
    still judged by the shared validator exactly as before, so this relaxes
    no obligation: it only stops a spec-following Candidate from being
    refused for a key name it was never told to use.
    """
    try:
        report=json.loads((candidate/'edit_report.json').read_text())
    except (OSError,ValueError):
        return contextlib.nullcontext(candidate)
    if not isinstance(report,dict) or 'commands' in report or 'commands_and_results' not in report:
        return contextlib.nullcontext(candidate)
    return _staged_alias(candidate,report)


@contextlib.contextmanager
def _staged_alias(candidate,report):
    aliased=dict(report)
    aliased['commands']=aliased['commands_and_results']
    with tempfile.TemporaryDirectory(prefix='presubmit-alias-') as tmp:
        staged=Path(tmp)/'candidate'
        staged.mkdir()
        for name in FILES:
            if name=='edit_report.json':
                (staged/name).write_text(json.dumps(aliased,indent=2,sort_keys=True)+'\n')
            else:
                shutil.copyfile(candidate/name,staged/name)
        yield staged


def validate_metadata(candidate, expected):
    candidate=Path(candidate)
    if set(p.name for p in candidate.iterdir())!=set(FILES): raise ValueError('exactly three delivery files required')
    for name in FILES: sha(candidate/name)
    parser=shared_modules()
    with _present_for_shared_validator(candidate) as presented:
        parser.report_ok(presented,parser.paths(presented/'solution.patch'),expected)

def sha(path):
    p=Path(path)
    if p.is_symlink() or not p.is_file(): raise ValueError('missing readiness file')
    return hashlib.sha256(p.read_bytes()).hexdigest()

def canonical_feedback(value):
    return hashlib.sha256((json.dumps(value,sort_keys=True,separators=(',',':'),ensure_ascii=False)+'\n').encode()).hexdigest()

def validate_binding(value):
    if not isinstance(value,dict) or set(value)!={'task','source_digest','contract_digest','registry_digest'} or value.get('task')!='ai-scientist': raise ValueError('readiness task mismatch')
    for k in ('source_digest','contract_digest','registry_digest'):
        if not isinstance(value.get(k),str) or len(value[k])!=64 or any(c not in '0123456789abcdef' for c in value[k]): raise ValueError('invalid readiness binding')
    return dict(value)

def execution_valid(record):
    b=record.get('broker') or {}
    return (record.get('schema_version')=='agentswe-ai-scientist-launcher-result-v4'
        and record.get('real_execution') is True and record.get('classification_axis')=='candidate'
        and record.get('controller_finished_at') is not None and b.get('usage_complete') is True
        and int(b.get('successful_calls',0) or 0)>0 and not b.get('transport_error')
        and not b.get('budget_exceeded')
        and not int(b.get('in_flight_calls',0) or 0)
        # D13: a recovered upstream transport failure is counted, not disqualifying.
        and not any(int(b.get(k,0) or 0) > int(b.get('recovered_transport_calls_delta',0) or 0)
                    for k in ('failures_delta','usage_unknown_calls_delta')))

def verify_rounds(records, session, binding, exit_evidence):
    validate_binding(binding)
    if len(records)!=2 or not isinstance(exit_evidence,dict) or exit_evidence.get('builder_exit_code')!=0 or exit_evidence.get('valid') is not True: raise RuntimeError('readiness requires two rounds and verified successful exit')
    for i,r in enumerate(records,1):
        if r.get('submission_number')!=i or r.get('builder_session_id')!=session or r.get('round_consumed') is not True: raise RuntimeError('round identity mismatch')
        if set(r.get('dev',{}))!={'dev_001'} or not execution_valid(r['dev']['dev_001']): raise RuntimeError('invalid public execution')
        from .protocol import tree_digest
        from .stable_product import product_source_digest
        repo=Path(r['attempt_paths']['repository']); delivery=Path(r['attempt_paths']['candidate'])
        if delivery_digest(delivery)!=r['candidate_digest'] or tree_digest(repo)!=r['build']['candidate_repo_digest']: raise RuntimeError('accepted source changed')
        if r['build'].get('valid') is not True or product_source_digest(repo)!=r['build'].get('product_source_digest'): raise RuntimeError('controlled build source changed')
        previous=records[i-2] if i>1 else None
        validate_metadata(delivery,{'builder_session_id':session,'submission_number':i,'revision_of_candidate_digest':previous['candidate_digest'] if previous else None,'feedback_digest':previous['feedback_digest'] if previous else None})
        if sha(r['feedback_path'])!=r['feedback_digest']: raise RuntimeError('feedback changed')
    if records[0].get('candidate_digest')==records[1].get('candidate_digest'): raise RuntimeError('candidate did not change')
    if records[0]['build']['product_source_digest']==records[1]['build']['product_source_digest'] or records[0]['patch_sha256']==records[1]['patch_sha256']: raise RuntimeError('product or patch did not change')
    if records[1].get('revision_of_candidate_digest') != records[0]['candidate_digest']: raise RuntimeError('revision binding mismatch')
    if records[1].get('feedback_digest_ack') != records[0]['feedback_digest']: raise RuntimeError('feedback not consumed')
