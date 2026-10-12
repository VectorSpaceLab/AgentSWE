"""Case-bound terminal-output admission and causal Candidate failure receipts.

Only an unusable core delivery with verified healthy execution can be a
deterministic zero. Parseable weak output goes to the independent Result judge.
This module never authors or repairs a Candidate artifact or calls a model.
"""
from __future__ import annotations

import hashlib
import json
import math
import os
from pathlib import Path
import stat
import time

from evaluator.case_budget import CASE_SECONDS
from evaluator.durable_state import bound_durable_evidence
from lower_agent.entry_contract import PRODUCTION_ENTRY

ADMISSION='openclaw-core-output-admission/v1'
POLICY='openclaw-evidenced-terminal-output-zero/v1'
MAX_BYTES=1_500_000  # Existing shared Result judge input capacity, not a fatal gate.
# D23 malformed artifact: 'evaluator_ownership_claim' joins the fatal core-output reasons.
# A core the product wrote that declares the evaluator as its author/source is a
# Candidate-authored contract violation for THAT case; it used to escape this admission as
# `semantic_review` and raise out of semantic_finalize.artifact_for, voiding the axis.
FATAL_REASONS={'missing_core','non_regular_core','non_json_core','non_object_core',
               'forbidden_private_value','evaluator_ownership_claim'}


# --- D13 (2026-09-19) recovered upstream transport failures ---------------------------
# An upstream transport failure the lower agent recovered from -- it issued a new logical
# request and a later one in the same ledger succeeded -- is infrastructure noise, not a
# provider failure for this case. Same allowlist and exclusions as the shared admission
# normalizers (harbor/0905-edit-case-repair/v2_usage_normalizers.py, transport_error);
# duplicated here because the control plane is not importable at run time.
_D13_TRANSPORT_TOKENS = ("brokenpipe", "connectionreset", "connectionaborted", "connectionclosed",
                         "remotedisconnected", "serverdisconnected", "incompleteread",
                         "chunkedencoding", "ssleof", "prematureclose")
_D13_PROVIDER_SIDE_TOKENS = ("readtimeout", "readtimedout", "sockettimeout", "timeouterror",
                             "timedout", "connecttimeout", "connectionerror")
_D13_NON_TRANSPORT_TOKENS = ("credential", "apikey", "unauthor", "forbidden", "invalidrequest",
                             "protocolfailure", "schema", "casedeadline", "deadlineexceeded",
                             "maxoutputtokens", "brokerrestart", "notdispatched", "notsent", "cancel")
_D13_NON_TRANSPORT_TEXT = ("client:", "client_", "client failure", "client error", "clientfailure")


def _d13_has_5xx(text):
    groups = "".join(character if character.isdigit() else " " for character in text).split()
    return any(len(group) == 3 and group[0] == "5" for group in groups)


def _d13_transport_error(error):
    if not isinstance(error, str) or not error.strip():
        return False
    text = error.lower()
    squeezed = "".join(character for character in text if character.isalnum())
    if any(token in squeezed for token in _D13_NON_TRANSPORT_TOKENS):
        return False
    if any(token in text for token in _D13_NON_TRANSPORT_TEXT):
        return False
    if any(token in squeezed for token in _D13_TRANSPORT_TOKENS):
        return True
    provider_side = any(marker in squeezed for marker in ("provider", "upstream", "http"))
    if provider_side and any(token in squeezed for token in _D13_PROVIDER_SIDE_TOKENS):
        return True
    return bool(provider_side and _d13_has_5xx(text))


def _d13_rows(stats):
    """Ledger rows of either family: intent rows (`requests`) or attempts (`attempts`)."""
    if not isinstance(stats, dict):
        return []
    for key in ("requests", "attempts"):
        rows = stats.get(key)
        if isinstance(rows, list):
            return [row for row in rows if isinstance(row, dict)]
    return []


def _d13_row_ok(row):
    if "model_response_available" in row or "usage_unknown" in row:
        return (row.get("state") == "terminal" and row.get("model_response_available") is True
                and row.get("usage_unknown") is False)
    return (row.get("ok") is True and row.get("usage_state") == "known"
            and row.get("upstream_completion") == "completed")


def _d13_row_error(row):
    if row.get("failure_kind") == "client" or row.get("provider_outcome") == "not_dispatched":
        return None
    for key in ("error", "transport_abort_reason", "error_type", "failure_reason"):
        value = row.get(key)
        if isinstance(value, str) and value:
            return value
    return None


def _d13_row_attempts(row):
    for key in ("transport_attempts", "upstream_attempts"):
        if key in row:
            return row.get(key)
    return None


def _d13_identity(row):
    return row.get("request_sha256") or row.get("request_id")


def _d13_recovered_transport_calls(before, after):
    """Tolerated rows added between the snapshots; before=None counts the whole ledger."""
    rows = _d13_rows(after)
    seen = {_d13_identity(row) for row in _d13_rows(before)} if before is not None else set()
    count = 0
    for index, row in enumerate(rows):
        if _d13_identity(row) in seen or _d13_row_ok(row):
            continue
        if not _d13_transport_error(_d13_row_error(row)) or _d13_row_attempts(row) != 1:
            continue
        if any(_d13_row_ok(later) for later in rows[index + 1:]):
            count += 1
    return count
# --- end D13 --------------------------------------------------------------------------


def sha(path):
    digest=hashlib.sha256()
    with Path(path).open('rb') as stream:
        while chunk:=stream.read(1024*1024):digest.update(chunk)
    return digest.hexdigest()


def read(path):
    value=json.loads(Path(path).read_text())
    if not isinstance(value,dict):raise ValueError('evaluator evidence must be an object')
    return value


def inspect_core(path,case_id,*,deadline,private_values=()):
    """Observe safely without following links, executing code, or inventing output."""
    path=Path(path)
    value={'schema_version':ADMISSION,'case_id':case_id,'path':str(path),
        'state':'evidence_unavailable','reason':'unobserved','sha256':None,
        'private_value_check_complete':True,'evaluator_authored_artifact':False}
    try:
        if time.monotonic()>=deadline:raise TimeoutError()
        try:info=path.lstat()
        except FileNotFoundError:
            return {**value,'state':'fatal_candidate_output','reason':'missing_core'}
        if not stat.S_ISREG(info.st_mode) or info.st_nlink!=1:
            return {**value,'state':'fatal_candidate_output','reason':'non_regular_core'}
        if info.st_size>MAX_BYTES:
            return {**value,'reason':'judge_input_capacity_exceeded'}
        fd=os.open(path,os.O_RDONLY|os.O_NOFOLLOW|os.O_NONBLOCK)
        try:
            before=os.fstat(fd)
            if (before.st_dev,before.st_ino)!=(info.st_dev,info.st_ino):raise OSError('artifact changed')
            chunks=[];count=0
            while True:
                if time.monotonic()>=deadline:raise TimeoutError()
                chunk=os.read(fd,65536)
                if not chunk:break
                count+=len(chunk)
                if count>MAX_BYTES:return {**value,'reason':'judge_input_capacity_exceeded'}
                chunks.append(chunk)
            after=os.fstat(fd)
            identity=lambda item:(item.st_dev,item.st_ino,item.st_size,item.st_mtime_ns,item.st_ctime_ns)
            if identity(before)!=identity(after):raise OSError('artifact changed')
        finally:os.close(fd)
        if time.monotonic()>=deadline:raise TimeoutError()
        raw=b''.join(chunks);value.update(sha256=hashlib.sha256(raw).hexdigest(),bytes=len(raw))
        try:parsed=json.loads(raw)
        except (ValueError,UnicodeError):
            return {**value,'state':'fatal_candidate_output','reason':'non_json_core'}
        if not isinstance(parsed,dict):
            return {**value,'state':'fatal_candidate_output','reason':'non_object_core'}
        if any(secret and secret in json.dumps(parsed,ensure_ascii=False) for secret in private_values):
            return {**value,'state':'fatal_candidate_output','reason':'forbidden_private_value'}
        # D23 malformed artifact: the product claiming the evaluator authored its own
        # delivery is a Candidate contract violation observed in the Candidate's own bytes.
        # This check is unconditional on purpose: prove_terminal_failure re-runs this
        # function to reconstruct the admission, so a flag it could not pass would make the
        # receipt impossible to revalidate.
        if parsed.get('evaluator_synthesized') is True or parsed.get('source')=='evaluator':
            return {**value,'state':'fatal_candidate_output','reason':'evaluator_ownership_claim'}
        return {**value,'state':'semantic_review','reason':'parseable_core',
            'claimed_case_id_matches':parsed.get('case_id')==case_id}
    except (OSError,TimeoutError) as exc:
        return {**value,'reason':'artifact_observation_'+type(exc).__name__}


TERMINAL_CAUSES = {
    'evaluator_case_budget_refused_further_model_calls':
        'the evaluator\'s own case-budget guard refused the next model request; the loop '
        'was stopped by the case clock, not by the product',
    'agent_process_error_before_core_delivery':
        'the agent process exited with an error before a usable agent_result.json existed',
    'agent_loop_ended_without_core_delivery':
        'the agent loop ended on its own terms without leaving a usable agent_result.json',
}


def terminal_cause(agent, reason):
    """Name why a verified-healthy run left no usable core delivery.

    Derived only from the stored native_agent evidence, so validate_zero_contract
    reconstructs the same answer byte for byte.  It never inspects the hidden case,
    the oracle, or anything the Candidate cannot already see about its own run.
    """
    transport = (agent.get('sandbox') or {}).get('transport') or {}
    guard = transport.get('dispatch_guard') or {}
    refusals = guard.get('refused_requests')
    refusals = refusals if type(refusals) is int and refusals >= 0 else 0
    turns = guard.get('turns_completed')
    turns = turns if type(turns) is int and turns >= 0 else None
    stop = agent.get('observed_stop_reason')
    stop = stop if isinstance(stop, str) else None
    status = agent.get('status')
    if reason != 'missing_core':
        cause = 'core_output_' + str(reason)
    elif refusals > 0 or status == 'timeout':
        cause = 'evaluator_case_budget_refused_further_model_calls'
    elif status == 'candidate_process_error':
        cause = 'agent_process_error_before_core_delivery'
    else:
        cause = 'agent_loop_ended_without_core_delivery'
    detail = {'native_agent_status': status, 'agent_exit_code': agent.get('exit_code'),
              'model_turns_completed': turns, 'budget_guard_refusals': refusals,
              'agent_clock_seconds': agent.get('agent_clock_seconds'),
              'agent_clock_full': agent.get('agent_clock_full'),
              'agent_elapsed_seconds': (round(agent['ended_monotonic']-agent['started_monotonic'], 3)
                  if type(agent.get('ended_monotonic')) in (int, float)
                  and type(agent.get('started_monotonic')) in (int, float) else None),
              'product_reported_stop_reason': stop,
              'stop_reason_is_product_self_report': stop is not None}
    return cause, detail


def terminal_major_errors(cause, detail, reason):
    """Concrete, Candidate-visible failure lines; never oracle or hidden-case facts."""
    turns = detail.get('model_turns_completed')
    turns = turns if type(turns) is int else 'an unrecorded number of'
    errors = ['agent_result.json was not a usable core delivery in the case workspace '
              '(core-output admission: %s)' % reason]
    if cause == 'evaluator_case_budget_refused_further_model_calls':
        errors.append('the evaluator refused %d further model request(s) at the case budget '
                      'reserve after %s completed model turn(s): the case clock ended the loop, '
                      'so write and refresh agent_result.json from the first product response '
                      'instead of only at the end'
                      % (detail.get('budget_guard_refusals') or 0, turns))
    elif cause == 'agent_process_error_before_core_delivery':
        errors.append('the agent process exited with code %r after %s completed model turn(s) '
                      'before any usable agent_result.json existed'
                      % (detail.get('agent_exit_code'), turns))
    elif cause == 'agent_loop_ended_without_core_delivery':
        errors.append('the agent loop ended by itself after %s completed model turn(s) with '
                      'stopReason=%s (the product\'s own log) and never wrote a usable '
                      'agent_result.json' % (turns, detail.get('product_reported_stop_reason')))
    return errors


def semantic_admitted(record):
    admission=record.get('artifact_contract',{}).get('core_admission',{})
    return (admission.get('schema_version')==ADMISSION and admission.get('state')=='semantic_review'
        and admission.get('case_id')==record.get('case_id') and admission.get('private_value_check_complete') is True)


def identities(paths):
    return {name:{'path':str(Path(path).resolve()),'sha256':sha(path)} for name,path in paths.items()}


def prove_terminal_failure(record,case_output,candidate_digest):
    """Reconstruct the cause from fixed evaluator files, not a generic failure label."""
    from lower_agent.launcher import broker_stats_delta
    case_output=Path(case_output).resolve();case_id=record.get('case_id')
    if case_id not in {'dev_001','dev_002',*(f'test_{i:03d}' for i in range(1,7))}:
        raise ValueError('terminal failure has an undeclared case identity')
    paths={'attestation':case_output/'hidden_case_attestation.json',
        'lower_report':case_output/'run_report.json',
        'resources':case_output/'case_resources/resource-attestation.json',
        'broker_before':case_output/'broker_before_suite_case.json',
        'broker_after':case_output/'broker_after_suite_case.json'}
    captured=read(paths['attestation']);lower=read(paths['lower_report'])
    for key in ('case_id','classification','production_entry','artifact_contract','native_case','native_agent',
                'case_resources','durable_state','broker_stats_delta','frozen_candidate_digest_before',
                'frozen_candidate_digest_after','candidate_runtime_manifest','candidate_runtime_manifest_sha256'):
        if key not in captured or captured[key]!=record.get(key):raise ValueError('terminal evidence mismatch: '+key)
    if captured['classification'] not in {'candidate_product_failure','candidate_behavior_failure'}:
        raise ValueError('terminal failure is not attributed to the Candidate')
    if captured.get('frozen_candidate_digest_stable') is not True or not bound_durable_evidence(captured,case_id):
        raise ValueError('stopped runtime identity/state is unresolved')
    if captured['production_entry']!=PRODUCTION_ENTRY or lower.get('command_entry')!=PRODUCTION_ENTRY:
        raise ValueError('terminal failure lacks the native product entry')
    if (captured.get('model')!='deepseek-flash' or captured.get('reasoning_effort')!='high'
            or captured.get('credential_mode')!='placeholder-only'
            or captured.get('candidate_sensitive_environment_keys')!=[]):
        raise ValueError('Candidate model/credential boundary is not attested')
    if lower.get('case_id')!=case_id or lower.get('artifact_contract')!=captured['artifact_contract']:
        raise ValueError('lower output is not bound to the captured case')
    if lower.get('health',{}).get('status')!='ok' or lower.get('status') not in {'completed','partial'}:
        raise ValueError('native product startup was not verified healthy')
    scope=read(paths['resources'])
    if scope!=captured['case_resources'] or scope.get('valid') is not True or scope.get('timed_out') is not False or scope.get('cleanup',{}).get('complete') is not True:
        raise ValueError('owned launcher scope did not finish and clean normally')
    native=captured['native_case'];agent=captured['native_agent']
    if native!=lower.get('native_case') or agent!=lower.get('native_agent'):
        raise ValueError('native evidence differs from the lower report')
    if native.get('infrastructure_errors')!=[] or native.get('world',{}).get('failure') or native.get('world',{}).get('cleanup_complete') is not True:
        raise ValueError('native environment or cleanup is unresolved')
    if (agent.get('status') not in {'completed','candidate_process_error','timeout'}
            or agent.get('cleanup_complete') is not True or agent.get('evaluator_authored_result') is not False):
        raise ValueError('native Agent did not reach an evidenced terminal outcome')
    for key in ('started_monotonic','ended_monotonic','deadline_monotonic'):
        if type(agent.get(key)) not in (int,float) or not math.isfinite(agent[key]):raise ValueError('native clock unavailable')
    if not agent['started_monotonic']<agent['ended_monotonic'] or agent['started_monotonic']>=agent['deadline_monotonic']:
        raise ValueError('native Agent had no execution interval')
    if agent['status']=='completed' and (type(agent.get('exit_code')) is not int or agent['exit_code']!=0):raise ValueError('normal native exit is unproven')
    if agent['status']=='candidate_process_error' and (type(agent.get('exit_code')) is not int or agent['exit_code']==0):raise ValueError('native process error is unproven')
    if agent['status']=='timeout' and agent['ended_monotonic']<agent['deadline_monotonic']:raise ValueError('early evaluator interruption')
    sandboxes=[lower.get('sandbox',{})]+native.get('cluster',{}).get('sandbox_evidence',[])
    if len(sandboxes)<3 or any(s.get('valid') is not True or s.get('gateway_wrapper_reaped') is not True
            or s.get('transport',{}).get('incomplete') is not False for s in sandboxes):
        raise ValueError('product/Gateway isolation or model transport cleanup is unresolved')
    timing=captured.get('timing_contract',{});elapsed=timing.get('elapsed_seconds')
    if type(elapsed) not in (int,float) or not math.isfinite(elapsed) or not 0<elapsed<=CASE_SECONDS or timing.get('record_publication_overrun'):
        raise ValueError('full case budget is not verified')
    before,after=read(paths['broker_before']),read(paths['broker_after'])
    if not before.get('broker_instance_id') or before.get('broker_instance_id')!=after.get('broker_instance_id'):
        raise ValueError('broker ownership changed')
    for stats in (before,after):
        if stats.get('protocol',{}).get('model')!='deepseek-flash' or stats.get('protocol',{}).get('reasoning_effort')!='high':
            raise ValueError('lower model protocol is not locked')
        runtime=stats.get('runtime',{})
        if runtime.get('in_flight_calls')!=0:raise ValueError('broker request remains active')
        for key in ('calls','failures','successful_calls','client_failures','provider_failures','usage_unknown_calls'):
            if type(runtime.get(key)) is not int or runtime[key]<0:raise ValueError('broker counter unavailable')
        if (runtime['calls']!=runtime['successful_calls']+runtime['failures']
                or runtime['failures']!=runtime['client_failures']+runtime['provider_failures']
                or runtime['usage_unknown_calls']>runtime['calls']):
            raise ValueError('broker counters are internally inconsistent')
    if any(after['runtime'][key]<before['runtime'][key] for key in
            ('calls','failures','successful_calls','client_failures','provider_failures','usage_unknown_calls')):
        raise ValueError('broker counters regressed within the same instance')
    delta=broker_stats_delta(before,after)
    # D13: recovered upstream transport failures stay in the counters and are subtracted
    # here; the recomputed delta must still match the launcher's recorded one exactly.
    recovered=_d13_recovered_transport_calls(before,after)
    # Deadline-aborted attempts are evaluator-initiated truncations and a Candidate
    # outcome, so only provider/usage-unknown activity beyond the deadline aborts and the
    # recovered transport rows still blocks the terminal-failure proof. Attestations
    # captured before this contract carry no split counters, fall back to the raw ones and
    # keep refusing exactly as they did. The raw-counter invariants above are untouched.
    if (delta!=captured['broker_stats_delta']
            or delta.get('unattributed_provider_failures',delta['provider_failures'])>recovered
            or delta.get('unattributed_usage_unknown_calls',delta['usage_unknown_calls'])>recovered):
        raise ValueError('case-local provider outcome/health is unresolved')
    paths['runtime_manifest']=Path(captured['candidate_runtime_manifest'])
    if sha(paths['runtime_manifest'])!=captured['candidate_runtime_manifest_sha256']:
        raise ValueError('runtime manifest changed after execution')
    runtime=read(paths['runtime_manifest'])
    if (runtime.get('candidate_runtime_ready') is not True or runtime.get('source_digest_stable') is not True
            or runtime.get('candidate_source_digest')!=candidate_digest
            or runtime.get('candidate_source_digest_after_build')!=candidate_digest
            or runtime.get('runtime_source_digest')!=captured['frozen_candidate_digest_before']
            or runtime.get('runtime_source_digest')!=captured['frozen_candidate_digest_after']):
        raise ValueError('failure does not bind to this frozen Candidate')
    admission=captured['artifact_contract'].get('core_admission',{})
    path=case_output/'workspace/agent_result.json'
    if (admission.get('schema_version')!=ADMISSION or admission.get('case_id')!=case_id
            or admission.get('state')!='fatal_candidate_output' or admission.get('reason') not in FATAL_REASONS
            or admission.get('path')!=str(path) or admission.get('private_value_check_complete') is not True
            or admission.get('evaluator_authored_artifact') is not False):
        raise ValueError('no fatal core-output observation')
    current=inspect_core(path,case_id,deadline=time.monotonic()+5)
    if current.get('sha256')!=admission.get('sha256'):raise ValueError('core output changed after capture')
    if admission['reason']=='forbidden_private_value':
        if current.get('state')!='semantic_review':raise ValueError('private-output bytes are unavailable')
    elif current.get('reason')!=admission['reason']:
        raise ValueError('core-output failure no longer matches the observation')
    cause,detail=terminal_cause(agent,admission['reason'])
    return {'case_id':case_id,'candidate_digest':candidate_digest,'runtime_digest':runtime['runtime_source_digest'],
        'case_bundle_sha256':native['case_bundle_sha256'],'owned_scope_unit':scope['unit'],
        'native_terminal_status':agent['status'],'fatal_output_reason':admission['reason'],
        'terminal_cause':cause,'terminal_cause_explained':TERMINAL_CAUSES.get(cause,''),
        'terminal_cause_detail':detail,
        'core_sha256':admission.get('sha256'),'broker_delta':delta},identities(paths)


def validate_zero_contract(contract,inputs,record=None):
    try:
        if (contract.get('failure_policy')!=POLICY or contract.get('evaluation_state')!='candidate_zero'
                or type(contract.get('result_score')) is not int or contract['result_score']!=0
                or contract.get('judge_invoked') is not False or contract.get('contract_valid') is not True
                or contract.get('result_score_publishable') is not True):
            return False
        for item in inputs.values():
            if sha(item['path'])!=item['sha256']:return False
        record=record if record is not None else read(inputs['attestation']['path'])
        proof,bound=prove_terminal_failure(record,Path(inputs['attestation']['path']).parent,contract['candidate_digest'])
        return (contract.get('case_id')==record['case_id'] and contract.get('causal_proof')==proof
            and all(inputs.get(key)==value for key,value in bound.items()))
    except (OSError,ValueError,KeyError,TypeError,AttributeError):return False


def candidate_zero_receipt(*,root,candidate_digest,record,case_output,output):
    """Return None for semantic cases; raise for unproven fatal claims. Never retry a judge."""
    admission=record.get('artifact_contract',{}).get('core_admission',{})
    if admission.get('state')!='fatal_candidate_output':return None
    proof,inputs=prove_terminal_failure(record,case_output,candidate_digest)
    split='dev_cases' if record['case_id'].startswith('dev_') else 'test_cases'
    inputs.update(identities({'task_input':Path(root)/split/record['case_id']/'input.md',
        'rubric':Path(root)/'evaluator/result_rubric.md','failure_policy_source':Path(__file__)}))
    output=Path(output);receipt_path=output/'receipt.json'
    if receipt_path.is_file():
        receipt=read(receipt_path);contract=read(receipt['contract_path']);binding=read(receipt['binding_path'])
        if (receipt.get('candidate_digest')!=candidate_digest or receipt.get('case_id')!=record['case_id']
                or sha(receipt['contract_path'])!=receipt.get('contract_sha256')
                or sha(receipt['binding_path'])!=receipt.get('binding_sha256')
                or binding.get('case_id')!=record['case_id'] or binding.get('candidate_digest')!=candidate_digest
                or binding.get('contract_sha256')!=receipt.get('contract_sha256')
                or binding.get('inputs')!=inputs or not validate_zero_contract(contract,inputs)):
            raise ValueError('cached terminal zero evidence changed')
        return receipt
    output.mkdir(parents=True,exist_ok=False)
    # The reason and the assessment must be actionable.  Four consecutive 0919 dev
    # rounds received 'missing_core' with empty major_errors and dimensions, and the
    # Builder's closing message says it stopped a round early because that signal was
    # identical across very different implementations.  Everything added here is
    # reconstructed from the same stored evidence, so validate_zero_contract still
    # rebuilds the contract independently.
    contract={'case_id':record['case_id'],'candidate_digest':candidate_digest,'failure_policy':POLICY,
        'evaluation_state':'candidate_zero','contract_valid':True,'result_score_publishable':True,
        'result_score':0,'judge_invoked':False,'causal_proof':proof,'evidence':list(inputs.values()),
        'reason':'Verified healthy native execution ended without a usable core delivery ('
                 +proof['fatal_output_reason']+'): '+proof['terminal_cause_explained'],
        'major_errors':terminal_major_errors(proof['terminal_cause'],proof['terminal_cause_detail'],
                                             proof['fatal_output_reason']),
        'dimensions':{},
        'assessment':'Candidate terminal delivery failure, cause '+proof['terminal_cause']
                     +'. No semantic score is fabricated from RPC counts; the Result judge '
                     'was not invoked because there was no core delivery to judge.'}
    if not validate_zero_contract(contract,inputs):raise ValueError('terminal zero proof failed independent reconstruction')
    def write_new(path,value):
        with path.open('x') as stream:json.dump(value,stream,indent=2);stream.write('\n')
    contract_path=output/'candidate_zero_contract.json';write_new(contract_path,contract)
    binding_path=output/'binding.json';write_new(binding_path,{'case_id':record['case_id'],
        'candidate_digest':candidate_digest,'contract_sha256':sha(contract_path),'inputs':inputs})
    receipt={'case_id':record['case_id'],'candidate_digest':candidate_digest,'classification':'candidate_zero',
        'contract_path':str(contract_path),'contract_sha256':sha(contract_path),
        'binding_path':str(binding_path),'binding_sha256':sha(binding_path)}
    write_new(receipt_path,receipt)
    return receipt
