"""Read current task-specific Lower ledgers without rewriting their raw schema.

The evaluator passes these callbacks only with source-bound, hashed raw artifacts.
Aider/Codex need their directory of immutable request receipts, so aggregated
stats alone remain insufficient. Claude also needs an explicit final in-flight
observation; its current stats list only completed requests.
"""
from v2_usage_normalizers import (require, tokens, canonical, MEASUREMENT, canonical_recovered,
                                  known_usage_totals, recovered_transport_ids)
# D54 (2026-09-21): the evaluator's own case-deadline kill is a Candidate outcome, so a
# ledger whose only failures are those kills is admissible. The policy, the two recognised
# self-identifying shapes and the canonical row live in v2_usage_normalizers.
from v2_usage_normalizers import (canonical_deadline, deadline_counters_consistent,
                                  deadline_killed_ids)

SCHEMAS = {
    'deepcode': 'deepcode-request-ledger/v2',
    'dyad': 'dyad-request-ledger/v2',
    'openhands': 'agentswe-broker-stats/v1',
    'openclaw': 'agentswe-broker-stats-v2',
    'ai-scientist': 'agentswe-broker-stats-v2',
}
TOKEN_KEYS = ('input_tokens', 'output_tokens', 'total_tokens')


def _counters(raw, expected):
    require(isinstance(raw, dict), 'aggregate counters missing')
    require(all(type(raw.get(k)) is int and raw[k] == v for k, v in expected.items()),
            'aggregate count mismatch, failure, pending or unknown request')


def _identity(rows, key, request_id):
    require(isinstance(rows, list) and bool(rows) and all(isinstance(r, dict) for r in rows),
            'complete raw request list required')
    ids = [r.get(key) for r in rows]
    require(all(isinstance(i, str) and i for i in ids) and len(set(ids)) == len(ids),
            'missing or duplicate request identity')
    require(request_id in ids, 'requested identity absent from raw ledger')
    return ids


def _usage_matches(runtime, usages, raw=None, recovered=(), killed=()):
    for usage in usages:
        tokens(usage)
    # D13: with a tolerated row present the runtime totals are null by construction, so the
    # broker's own known-usage subtotal is reconciled against the rows that are known.
    # D54: a case-deadline kill is reconciled the same way.
    totals = known_usage_totals(raw, runtime, recovered, deadline_killed=killed)
    tokens(totals)
    require(all(totals[k] == sum(u[k] for u in usages) for k in TOKEN_KEYS),
            'input/output/total token aggregates mismatch')


def _row_ok(row):
    return (row.get('ok') is True and row.get('usage_state') == 'known'
            and row.get('upstream_completion') == 'completed')


def _attempt_ok(row):
    return (row.get('state') == 'terminal' and row.get('model_response_available') is True
            and row.get('usage_unknown') is False)


def _attempt_error(row):
    """The attempt ledger's own name for the upstream fault; a client or undispatched
    rejection has no upstream fault to tolerate and never returns one."""
    if row.get('failure_kind') == 'client' or row.get('provider_outcome') == 'not_dispatched':
        return None
    for key in ('transport_abort_reason', 'error', 'error_type', 'failure_reason'):
        value = row.get(key)
        if isinstance(value, str) and value:
            return value
    return None


def normalize_extra_lower(raw, request_id, *, task, tolerate_recovered=False):
    require(task in SCHEMAS, 'unsupported task lower schema: ' + str(task))
    require(isinstance(raw, dict) and raw.get('schema_version') == SCHEMAS[task],
            'wrong task lower schema')
    protocol = raw.get('protocol', {})
    require(protocol.get('model') == 'deepseek-flash' and protocol.get('reasoning_effort') == 'high',
            'lower model/effort mismatch')
    if task in ('openclaw', 'ai-scientist'):
        return _attempts(raw, request_id, task, tolerate_recovered)
    return _intents(raw, request_id, task, tolerate_recovered)


def _intents(raw, request_id, task, tolerate_recovered=False):
    rows, intents, runtime = raw.get('requests'), raw.get('logical_requests'), raw.get('runtime')
    ids = _identity(rows, 'request_sha256', request_id)
    require(raw.get('role') == 'lower' and isinstance(intents, dict) and set(ids) == set(intents),
            'wrong role or truncated/unresolved intent inventory')
    # D13: allowlisted upstream transport failures the lower agent recovered from are counted
    # apart; anything else failed, pending or unknown still fails the counters below.
    recovered = recovered_transport_ids(rows, identity='request_sha256', succeeded=_row_ok,
                                        error_of=lambda row: row.get('error'),
                                        attempts_of=lambda row: row.get('transport_attempts')) if tolerate_recovered else {}
    # D54: evaluator case-deadline kills are counted apart for every lower role.
    killed = {rid: error for rid, error in deadline_killed_ids(
        rows, identity='request_sha256', succeeded=_row_ok,
        error_of=lambda row: row.get('error'),
        attempts_of=lambda row: row.get('transport_attempts')).items() if rid not in recovered}
    require(deadline_counters_consistent(runtime, rows, killed),
            "case-deadline kill not declared by the ledger's own counters")
    tolerated = len(recovered) + len(killed)
    _counters(runtime, dict(calls=len(rows), successful_calls=len(rows) - tolerated,
                            failures=tolerated, unknown_usage_calls=tolerated))
    if task == 'openhands':
        _counters(runtime, dict(in_flight_calls=0))
    else:
        require(raw.get('transport_measurement') == MEASUREMENT, 'unmeasured lower transport')
        # Disk snapshots omit the derived pending_calls field; intent inventory
        # and all terminal states above/below prove no unresolved request.
        require(runtime.get('pending_calls', 0) == 0, 'pending lower request')
    known_rows = []
    for row in rows:
        intent = intents[row['request_sha256']]
        if row['request_sha256'] in recovered or row['request_sha256'] in killed:
            require(isinstance(intent, dict) and intent.get('state') == 'unknown_or_failed' and
                    intent.get('result') == row and row.get('usage_state') == 'unknown' and
                    row.get('upstream_completion') == 'unknown_or_failed' and
                    type(row.get('transport_attempts')) is int and row['transport_attempts'] == 1 and
                    row.get('model') == 'deepseek-flash' and row.get('reasoning_effort') == 'high',
                    'recovered transport request not durably recorded')
            if task != 'openhands':
                require(type(intent.get('transport_attempts')) is int and intent['transport_attempts'] == 1,
                        'HTTP start was not durably observed')
            continue
        require(isinstance(intent, dict) and intent.get('state') == 'completed' and intent.get('result') == row,
                'request completion differs from durable intent')
        require(row.get('ok') is True and row.get('usage_state') == 'known' and
                row.get('upstream_completion') == 'completed' and
                type(row.get('transport_attempts')) is int and row['transport_attempts'] == 1 and
                row.get('model') == 'deepseek-flash' and row.get('reasoning_effort') == 'high',
                'lower request failed, incomplete or unknown')
        if task != 'openhands':
            require(type(intent.get('transport_attempts')) is int and intent['transport_attempts'] == 1,
                    'HTTP start was not durably observed')
        known_rows.append(row)
    _usage_matches(runtime, known_rows, raw, recovered, killed)
    if request_id in recovered:
        return canonical_recovered(request_id, recovered[request_id])
    if request_id in killed:
        return canonical_deadline(request_id, killed[request_id])
    return canonical(request_id, tokens(rows[ids.index(request_id)]))


def _terminal_response_acceptable(row):
    """A completed response, or one finished inside the Candidate's own budget.

    The brokers deliver the latter because a real API client receives it; the
    ledger row it produces has to be admissible here for the same reason.
    """
    status = row.get('response_status')
    if status == 'completed':
        return True
    details = row.get('incomplete_details')
    return (status == 'incomplete' and isinstance(details, dict)
            and details.get('reason') == 'max_output_tokens'
            and row.get('response_error_present') is False)


def _attempts(raw, request_id, task, tolerate_recovered=False):
    rows, runtime, protocol = raw.get('attempts'), raw.get('runtime'), raw['protocol']
    ids = _identity(rows, 'request_id', request_id)
    require(isinstance(raw.get('broker_instance_id'), str) and bool(raw['broker_instance_id']),
            'broker instance identity absent')
    require(protocol.get('inner_retries') == 0 and protocol.get('max_upstream_attempts_per_transport') == 1 and
            protocol.get('redirects_allowed') is False, 'unbounded transport policy')
    # D13: the same tolerance as the intent ledgers, read off the attempt ledger's own fields.
    recovered = recovered_transport_ids(rows, identity='request_id', succeeded=_attempt_ok,
                                        error_of=_attempt_error,
                                        attempts_of=lambda row: row.get('upstream_attempts')) if tolerate_recovered else {}
    # D54: these two brokers publish no deadline counter, so the kill is recognised from the
    # per-row transport_abort_reason their D52d/D52f patches write. _attempt_error returns
    # None for a client failure, so a client rejection can never enter here.
    killed = {rid: error for rid, error in deadline_killed_ids(
        rows, identity='request_id', succeeded=_attempt_ok, error_of=_attempt_error,
        attempts_of=lambda row: row.get('upstream_attempts')).items() if rid not in recovered}
    require(deadline_counters_consistent(runtime, rows, killed),
            "case-deadline kill not declared by the ledger's own counters")
    tolerated = len(recovered) + len(killed)
    _counters(runtime, dict(calls=len(rows), completed_calls=len(rows), successful_calls=len(rows) - tolerated,
                           failures=tolerated, in_flight_calls=0, upstream_attempts=len(rows),
                           usage_unknown_calls=tolerated))
    if task == 'openclaw':
        _counters(raw, dict(cached_replays=0, blocked_duplicate_requests=0))
    else:
        _counters(raw, dict(historical_calls=0, current_invocation_calls=len(rows)))
    response_ids, usages = [], []
    for row in rows:
        if row.get('request_id') in recovered or row.get('request_id') in killed:
            require(row.get('state') == 'terminal' and row.get('model_response_available') is False and
                    row.get('usage_unknown') is True and row.get('worker_reaped') is True and
                    row.get('failure_kind') != 'client' and row.get('usage') is None and
                    type(row.get('upstream_attempts')) is int and row['upstream_attempts'] == 1,
                    'recovered transport attempt not durably recorded')
            continue
        require(row.get('state') == 'terminal' and row.get('model_response_available') is True and
                row.get('usage_unknown') is False and row.get('worker_reaped') is True and
                type(row.get('upstream_attempts')) is int and row['upstream_attempts'] == 1 and
                row.get('upstream_http_status') == 200 and row.get('transport_complete') is True and
                row.get('terminal_observed') is True and _terminal_response_acceptable(row) and
                row.get('model_identity_valid') is True and row.get('response_error_present') is False,
                'lower HTTP response incomplete, unknown, or worker not reaped')
        response_ids.append(row.get('response_id'))
        usages.append(row.get('usage'))
    require(all(isinstance(i, str) and i for i in response_ids) and len(set(response_ids)) == len(response_ids),
            'provider response identity missing or repeated')
    _usage_matches(runtime, usages, raw, recovered, killed)
    if request_id in recovered:
        return canonical_recovered(request_id, recovered[request_id])
    if request_id in killed:
        return canonical_deadline(request_id, killed[request_id])
    return canonical(request_id, tokens(rows[ids.index(request_id)]['usage']))


def make_extra_lower_normalizer(task, tolerate_recovered=False):
    require(task in SCHEMAS, 'unsupported task lower schema: ' + str(task))
    return lambda raw, request_id: normalize_extra_lower(raw, request_id, task=task,
                                                         tolerate_recovered=tolerate_recovered)
