"""Task-native evaluator attestation; no scoring or replacement artifacts."""
from __future__ import annotations

import hashlib
import json
from pathlib import Path


_FENCE = "```"


def _fenced_body(response: str):
    """Body of a terminal response that is exactly one markdown code fence.

    The disclosed contract obliges the *product* to persist the model's own
    object -- input/02: "The product may parse and atomically write the
    response"; input/03: "parse the model's exact final response as one JSON
    object" -- it never obliges the *model* to emit that object unfenced.
    Requiring the raw response string to be JSON therefore fails a terminal
    turn that wrapped the identical object in one ```json fence: formal
    0921b-v4-001 test_005, whose fence body parses byte-equal to
    agent_result.json, was filed evaluator_infrastructure_error with five
    sibling cases judged 73/100/93/79/66.

    Exactly one whole fence is unwrapped -- no prose outside it, no info
    string other than a bare language word, no nested or second fence, no
    substring scan -- so a response that merely *contains* a JSON block
    still does not establish authorship.
    """
    text = response.strip()
    if not (text.startswith(_FENCE) and text.endswith(_FENCE) and len(text) > 2 * len(_FENCE)):
        return None
    info, newline, rest = text[len(_FENCE):].partition("\n")
    if not newline or (info.strip() and not info.strip().isalnum()):
        return None
    body = rest[: -len(_FENCE)]
    return None if _FENCE in body else body


def _terminal_values(response: str):
    """(encoding, parsed value) for each admissible reading of one response."""
    for encoding, payload in (("raw", response), ("fenced_code_block", _fenced_body(response))):
        if payload is None:
            continue
        try:
            yield encoding, json.loads(payload)
        except ValueError:
            continue


def artifact_authorship(output: Path, case_id: str) -> dict:
    artifact = output / "agent_result.json"
    trajectory = output / "trajectory.jsonl"
    result = {"validated_by": "evaluator", "valid": False,
              "sha256": hashlib.sha256(artifact.read_bytes()).hexdigest() if artifact.is_file() else None,
              "validation_method": "deeptutor-terminal-response-equals-product-artifact/v1"}
    try:
        value = json.loads(artifact.read_text())
        if not isinstance(value,dict):return result
        substantive={k:v for k,v in value.items() if k not in {'schema_version','case_id','operation_id'} and v not in (None,'',[],{})}
        if not substantive:return result
        result['quality_schema_findings']=[]
        if value.get('case_id')!=case_id:result['quality_schema_findings'].append('case_id mismatch')
        for key in ('schema_version','status','summary'):
            if not isinstance(value.get(key),str) or not value[key].strip():result['quality_schema_findings'].append('missing '+key)
        if not isinstance(value.get('artifacts'),dict):result['quality_schema_findings'].append('missing artifacts object')
        for line in trajectory.read_text().splitlines():
            try:
                event = json.loads(line)
                response = event.get("metadata", {}).get("response")
                if event.get("type") != "result" or not isinstance(response, str):
                    continue
                for encoding, parsed in _terminal_values(response):
                    if parsed == value:
                        result["valid"] = True
                        result["trajectory_sha256"] = hashlib.sha256(trajectory.read_bytes()).hexdigest()
                        if encoding != "raw":
                            result["terminal_response_encoding"] = encoding
                        break
                if result["valid"]:
                    break
            except (ValueError, TypeError, AttributeError):
                continue
    except (OSError, ValueError, TypeError):
        pass
    return result


def attest_execution(record: dict, launcher: dict, *, output: Path, case_id: str, candidate_digest: str) -> dict:
    """Translate positively observed product evidence to shared contract fields.

    Fixture preparation failure alone is deliberately unresolved: importing a
    Candidate or failing to construct an incident is not a lower-agent run.
    Infrastructure remains dominant even if a malformed artifact also exists.
    """
    try:
        from .import_attribution import verify_failure
    except ImportError:
        from import_attribution import verify_failure
    attribution = verify_failure(launcher, output, candidate_digest)
    if attribution and record.get('infra_valid') is not False:
        return {**record, 'candidate_digest':candidate_digest, 'case_id':case_id,
            'classification':'candidate_build_failure', 'execution_attempted':True,
            'execution_phase':'product_import', 'real_execution':False, 'lower_agent_executed':False,
            'environment_preflight':{'valid':True,'scope':'paired product import, runtime, dependencies and resource scope; model transport was not invoked'},
            'public_diagnostic':attribution['public_diagnostic'],
            'failure_attribution':{'party':'candidate','observed_by':'evaluator','fatal':True,
                'reason':attribution['reason'],'evidence_paths':[attribution['evidence_path']]}}
    # A Candidate fixture precondition failure IS product code running and
    # failing terminally: the evaluator called the product's own claim tool
    # inside the fixture and it returned no persisted delivery, so the case
    # ended with zero model calls. Without the shared fatal-gate shape,
    # classify_candidate_execution falls through to "unresolved", the record
    # comes back infra_valid False, and the submission is answered 503
    # infrastructure_attempt_not_consumed / retry_same_round forever (0919
    # formal run f-003: candidate_002 resubmitted seven times on dev_002).
    # The markers below are the launcher's own, written by
    # lower_agent_entry.py:384-391; a fixture failure without them stays
    # unresolved, exactly as before.
    fixture_failure = launcher.get('fixture_failure') or {}
    if (fixture_failure.get('classification') == 'candidate_fixture_precondition_failure'
            and launcher.get('product_terminal_failure') is True
            and launcher.get('execution_requested') is True
            and launcher.get('runtime_probe', {}).get('infra_valid') is True
            and record.get('infra_valid') is not False):
        fixture_reason = str(fixture_failure.get('reason') or launcher.get('error')
                             or 'Candidate product could not establish the case precondition')
        fixture_evidence = [str(path) for path in (output / 'launcher_result.json',
                                                   output / 'fixture' / 'fixture-failure.json')
                            if path.is_file()]
        return {**record, 'candidate_digest': candidate_digest, 'case_id': case_id,
            'classification': 'candidate_product_failure', 'execution_attempted': True,
            'execution_phase': 'product_case_precondition', 'real_execution': False,
            'lower_agent_executed': False, 'infra_valid': True, 'valid': True,
            'environment_preflight': {'valid': True,
                'scope': 'paired product import, runtime, dependencies and resource scope verified; '
                         'the product tool ran inside the evaluator fixture and refused the precondition'},
            'public_diagnostic': fixture_reason,
            'failure_attribution': {'party': 'candidate', 'observed_by': 'evaluator', 'fatal': True,
                'reason': fixture_reason, 'evidence_paths': fixture_evidence}}
    observed = str(record.get("classification", ""))
    delta = record.get("broker_delta") or record.get("broker", {}).get("delta") or {}
    executed = launcher.get("executed") is True
    # A check that never ran did not fail. The launcher stops at a Candidate
    # precondition failure before the transport preflight exists, and treating
    # that absence as a refusal recorded candidate_fixture_precondition_failure
    # as "evaluator environment preflight failed", which threw the whole run out
    # of judge smoke. Failing requires a signal that ran and said no; at least
    # one must have run, so no evidence at all is still not health.
    health_signals = [launcher.get("runtime_probe", {}).get("infra_valid"),
                      launcher.get("transport_preflight", {}).get("valid")]
    healthy = (any(signal is True for signal in health_signals)
               and all(signal is not False for signal in health_signals))
    validation = artifact_authorship(output, case_id)
    result = dict(record)
    result.update({"candidate_digest": candidate_digest, "case_id": case_id,
                   "broker_delta": delta, "execution_attempted": executed,
                   "real_execution": executed and int(delta.get("successful_calls", 0) or 0) > 0,
                   "environment_preflight": {"valid": healthy}, "artifact_validation": validation})
    stats = record.get('broker_after') or (record.get('broker') or {}).get('after') or launcher.get('broker_after') or {}
    if not isinstance(stats, dict):
        stats = {}
    budget = stats.get('context_budgets', {}).get(launcher.get('logical_context_id'), {}) if stats.get('budget_scope') == 'case-context/v1' else {}
    if budget:
        result['case_model_budget'] = budget
    if budget.get('budget_exceeded') is True:
        # Do not relabel unknown/provider/fixture failures as a resource zero.
        try:
            events = json.loads((output / 'transport_relay_events.json').read_text()).get('events', [])
        except (OSError, ValueError):
            events = []
        transport_errors = [event for event in events if event.get('kind') in {'broker_http_error','relay_error','rejected_endpoint'}]
        only_budget_refusals = bool(transport_errors) and all(event.get('kind') == 'broker_http_error' and event.get('status') == 429 for event in transport_errors)
        known = budget.get('unknown_usage_calls') == 0 and isinstance(budget.get('total_tokens'), int)
        no_infra_delta = all(int(delta.get(key, 0) or 0) == 0 for key in ('failures','provider_failures','delivery_failures','upstream_failures'))
        terminal = isinstance(launcher.get('exit_code'), int) or launcher.get('timed_out') is True
        # Say what was actually spent.  Repeating a bare "exhausted its disclosed
        # budget" five rounds running gave the 0919 Builder nothing to converge on.
        budget_detail = ('observed %s of %s model calls and %s of %s counted tokens'
                         % (budget.get('calls'), budget.get('max_calls'),
                            budget.get('known_total_tokens'), budget.get('max_tokens')))
        # An evaluator_infrastructure_error is never re-attributed to the
        # Candidate.  The former escape hatch (`budget_only_transport`) promoted
        # exactly that, because a 429 budget refusal used to be recorded as a
        # transport failure; that false positive is now fixed at its source in
        # lower_agent_entry.py, so a case that still arrives here infra-invalid
        # has a genuine evaluator-side fault and stays ours.  A real budget
        # exhaustion on a healthy case is unaffected and remains the Candidate's.
        if known and no_infra_delta and executed and healthy and terminal and record.get('infra_valid') is not False:
            result.update(classification='candidate_policy_violation', infra_valid=True, valid=True,
                failure_attribution={'party':'candidate','observed_by':'evaluator','fatal':True,
                    'reason':'Actual case exhausted its disclosed model-call/token budget; final-response overrun is measured, not hidden. ' + budget_detail + '.',
                    'evidence_paths':[str(output / 'launcher_result.json'), str(output / 'logical-context.json')]})
            return result
        result.update(classification='evaluator_infrastructure_error', infra_valid=False)
        unproven = ('the launcher recorded an evaluator-side fault on this case, so the exhaustion is not exclusively the Candidate'
                    if record.get('infra_valid') is False
                    else 'complete usage or exclusive causal attribution is unproven')
        result['failure_attribution']={'party':'evaluator','observed_by':'evaluator','fatal':False,
            'reason':'Case budget exhaustion exists but ' + unproven + '; ' + budget_detail
                     + ('; the only transport refusals were the evaluator broker\'s own 429 budget stops' if only_budget_refusals else '') + '.'}
        return result
    if validation['valid'] and result['real_execution'] and record.get('infra_valid') is not False:
        findings=[str(f) for f in (validation.get('quality_schema_findings') or [])]
        # D23 malformed artifact: the disclosed contract (input/03, and the launcher's
        # artifact_contract) requires non-empty schema_version/case_id/status/summary
        # strings, an object-valued `artifacts` and the case-local case_id, and says a
        # malformed final response is a product/candidate evidence failure.  These
        # findings are exactly what the shared Result axis rejects as provenance errors;
        # handing such a case on as `candidate_partial` voided the whole axis (formal
        # 0921-fx-001 test_002: `artifacts` was a list).  It is this case's Candidate zero.
        if findings:
            evidence=output/'agent_result.json'
            result['classification']='candidate_artifact_failure'
            result['failure_attribution']={'party':'candidate','observed_by':'evaluator','fatal':True,
                'reason':'Product-authored final response violates the disclosed agent_result.json contract: '+'; '.join(findings)+'.',
                'evidence_paths':[str(evidence)] if evidence.is_file() else []}
            return result
        result['classification']='candidate_partial' if observed!='candidate_valid' else 'candidate_valid'
        result.pop('failure_attribution',None)
        return result
    # A parseable substantive output with missing authorship evidence is an
    # evaluator provenance gap, never an automatic Candidate quality zero.
    try:
        payload=json.loads((output/'agent_result.json').read_text())
        substantive=isinstance(payload,dict) and any(k not in {'schema_version','case_id','operation_id'} and v not in (None,'',[],{}) for k,v in payload.items())
    except (OSError,ValueError):substantive=False
    if substantive and not validation['valid'] and record.get('infra_valid') is not False:
        result.update(classification='evaluator_infrastructure_error',infra_valid=False)
        result['failure_attribution']={'party':'evaluator','observed_by':'evaluator','fatal':False,'reason':'Substantive product output exists but terminal native authorship could not be established'}
        return result
    known_failures = {"candidate_agent_failure": "candidate_product_failure",
                      "candidate_artifact_missing": "candidate_artifact_failure",
                      "candidate_contract_failure": "candidate_artifact_failure",
                      "candidate_timeout": "candidate_timeout"}
    failure = known_failures.get(observed)
    if executed and healthy and not validation["valid"] and observed in {"candidate_valid", "candidate_capability_gap"}:
        failure = "candidate_artifact_failure"
    # A lower process must have actually returned (or hit its own wall time)
    # before a zero can be attributed. A merely missing launcher is unknown.
    terminal_observed = isinstance(launcher.get("exit_code"), int) or launcher.get("timed_out") is True
    if failure and executed and healthy and terminal_observed and record.get("infra_valid") is not False:
        result["classification"] = failure
        evidence = output / "launcher_result.json"
        result["failure_attribution"] = {"party": "candidate", "observed_by": "evaluator", "fatal": True,
            "reason": str(record.get("reason") or observed),
            "evidence_paths": [str(evidence)] if evidence.is_file() else []}
    return result
