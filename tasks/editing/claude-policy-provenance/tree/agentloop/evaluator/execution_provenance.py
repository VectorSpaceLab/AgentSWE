"""Normalize trusted Claude execution files for the common attribution gate.

Never repair or rewrite a result artifact. Re-open its evaluator trajectory and
verify actual bytes, case/source identity and broker deltas before adapting the
record. Public and hidden callers use this same boundary.
"""
import copy
import hashlib
import json
from pathlib import Path
from .lower_agent_launcher import valid_agent_result
from .case_contract import digest_object


def normalize(record: dict, *, case_id: str, candidate_digest: str) -> tuple[dict, list[dict]]:
    result = copy.deepcopy(record)
    paths = (record.get('failure_attribution') or {}).get('evidence_paths') or []
    path = record.get('trajectory_path') or next((p for p in paths if Path(p).name == 'trajectory.json'), None)
    if not path:
        return result, []
    trace_path = Path(path)
    if not trace_path.is_file() or trace_path.is_symlink():
        return result, []
    raw = trace_path.read_bytes(); trace_sha = hashlib.sha256(raw).hexdigest()
    if record.get('trajectory_sha256') not in (None, trace_sha):
        return result, []
    trace = json.loads(raw)
    if (trace.get('schema_version') != 'agentswe-claude-policy-agent-case/v1'
            or trace.get('case_id') != case_id or trace.get('candidate_digest') != candidate_digest
            or trace.get('execution_attempted') is not True
            or (trace.get('environment_preflight') or {}).get('valid') is not True
            or trace.get('infrastructure_invalid') is not False
            or not trace.get('product_events')):
        return result, []
    if record.get('case_spec_sha256') not in (None, trace.get('case_spec_sha256')):
        return result, []
    metadata = trace.get('artifact') or {}
    if (metadata.get('path') != 'agent_result.json' or metadata.get('origin') != 'lower_model_final_response'
            or metadata.get('evaluator_synthesized') is not False or metadata.get('binding_verified') is not True):
        return result, []
    artifact_path = trace_path.parent / 'agent_result.json'
    if not artifact_path.is_file() or artifact_path.is_symlink():
        return result, []
    raw = artifact_path.read_bytes(); artifact_sha = hashlib.sha256(raw).hexdigest()
    if metadata.get('sha256') != artifact_sha or record.get('result_sha256') not in (None, artifact_sha):
        return result, []
    artifact = json.loads(raw)
    if trace.get('answer') != artifact or not valid_agent_result(artifact, case_id, expected_binding=trace.get('binding')):
        return result, []
    events, observations, binding = trace['product_events'], trace.get('observations'), trace.get('binding') or {}
    if (not isinstance(observations, list) or digest_object(events) != binding.get('product_trajectory_digest')
            or digest_object(observations) != binding.get('observation_digest')
            or [digest_object(e) for e in events] != binding.get('product_event_digests')
            or [e.get('action_id') for e in events] != binding.get('selected_action_ids')):
        return result, []
    observed = {o.get('action_id'):o for o in observations if isinstance(o,dict) and o.get('action_id')}
    for event in events:
        observation = observed.get(event.get('action_id')) or {}
        if event.get('kind') == 'target_hook_call':
            receipt = observation.get('receipt')
            if (event.get('permission') != observation.get('permission')
                    or event.get('receipt_digest') != (digest_object(receipt) if receipt is not None else None)
                    or event.get('response_digest') != observation.get('response_digest')):
                return result, []
        elif event.get('kind') == 'target_inspector_call':
            if event.get('exit_code') != observation.get('exit_code') or event.get('response_digest') != observation.get('stdout_sha256'):
                return result, []
        else:
            return result, []
    if record.get('native_evidence_path'):
        native_path = Path(record['native_evidence_path'])
        if native_path.is_symlink():return result, []
        native_raw = native_path.read_bytes()
        if hashlib.sha256(native_raw).hexdigest() != record.get('native_evidence_sha256'):
            return result, []
        native = json.loads(native_raw)
        if native.get('product_events') != events or native.get('observations') != observations or native.get('binding') != binding:
            return result, []
    broker = trace.get('broker') or {}
    before, after = broker.get('before') or {}, broker.get('after') or {}
    for snapshot in (before, after):
        protocol = snapshot.get('protocol') or {}
        if protocol.get('model') != 'deepseek-flash' or protocol.get('reasoning_effort') != 'high':
            return result, []
    counts = {}
    for key in ('calls','successful_calls','failures'):
        a, b = (before.get('runtime') or {}).get(key), (after.get('runtime') or {}).get(key)
        if type(a) is not int or type(b) is not int or not 0 <= a <= b:
            return result, []
        counts[key] = b - a
    if counts['calls'] < 1 or counts['successful_calls'] < 1 or counts['failures']:
        return result, []
    # These are evaluator broker observations of typed upstream completions,
    # distinct from the model's claims in the result artifact.
    prior_requests, requests = before.get('request_ledger'), after.get('request_ledger')
    if isinstance(prior_requests, list) and isinstance(requests, list):
        if requests[:len(prior_requests)] != prior_requests:
            return result, []
        new_requests = requests[len(prior_requests):]
        completed = [r for r in new_requests if r.get('completed') is True and r.get('submission_state') == 'completed'
                     and isinstance(r.get('response_id'),str) and r.get('response_id') and r.get('status_code') == 200]
        if len(new_requests) != counts['calls'] or len(completed) != counts['successful_calls']:
            return result, []
    else:
        return result, []
    evidence = [{'path': str(trace_path.resolve()), 'sha256': trace_sha},
                {'path': str(artifact_path.resolve()), 'sha256': artifact_sha}]
    result['lower_broker_delta'] = counts
    result['real_execution'] = True
    result['artifact_validation'] = {'validated_by':'evaluator','valid':True,'sha256':artifact_sha,
                                    'trajectory_sha256':trace_sha,'source':'reopened Claude trajectory and model artifact'}
    return result, evidence
