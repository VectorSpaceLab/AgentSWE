"""Decode unmodified current broker ledgers; unsupported schemas fail closed.

Sources inspected: OpenWiki request_ledger.Stats, DeepTutor broker.State,
judge_broker_xhigh.State, and result_judge.call_judge used by Code transport.
Adapters never persist normalized records or claim they are provider output.
"""
from __future__ import annotations

import re

MEASUREMENT = "http-request-start-after-connect-and-tls/v1"
LOWER_SCHEMAS = {"openwiki": "openwiki-request-ledger/v2", "deeptutor": "agentswe-deeptutor-single-attempt/v4"}

# D13 (approved 2026-09-19). An upstream transport failure the lower agent recovered from --
# it issued a new logical request and its round still completed and was accepted -- is
# infrastructure noise, not evidence of unaccounted provider use. Such a row stays a failure
# with unknown usage and zero known tokens; it is named in the role's usage receipt instead of
# being summed. A client, protocol, credential, candidate-budget or broker-restart failure,
# and a transport failure with no later success, keep today's strict refusal.
RECOVERED_TRANSPORT_POLICY_ID = "edit-recovered-transport-2026-09-19"
USAGE_KEYS = ("input_tokens", "output_tokens", "total_tokens")
# D54 (2026-09-21). Budget exhaustion is a Candidate outcome (candidate_timeout, a hard
# zero), never an infrastructure fault, so a readiness smoke whose hidden case spent its
# whole budget still proves the pipeline works. The only failure such a ledger may carry is
# the evaluator's OWN case-deadline kill, and the ledger has to say so itself. Two
# self-identifying shapes exist and no third is guessed:
#   * counter shape (D47a, dyad): the row's error is "deadline:<reason>" and the ledger
#     publishes deadline_failures beside failures, with no other failure family claiming it.
#   * row shape (D52d/D52f, openclaw/ai-scientist): the row carries
#     transport_abort_reason == "absolute_case_deadline". Those brokers publish no deadline
#     counter and keep the kill inside provider_failures on purpose, so the explicit
#     per-row reason is the declaration and only client_failures == 0 is required with it.
# Everything else keeps today's refusal: provider/client/transport/credential/protocol/
# broker failures, pending or in-flight requests, multi-attempt rows, any other unknown
# usage, and every count mismatch.
DEADLINE_POLICY_ID = "edit-case-deadline-candidate-outcome-2026-09-21"
DEADLINE_ERROR_PREFIX = "deadline:"
DEADLINE_ABORT_REASON = "absolute_case_deadline"
# The evaluator-owned exception openhands' broker already books for its own case deadline
# (13-.../evaluator/broker/candidate_broker.py:29-31, :102, :291-293). It is a third
# self-identifying shape, and it is already in the live tree.
DEADLINE_ERROR_NAMES = ("CaseDeadlineExceeded",)
# Counters a broker may publish to declare those kills. Whichever it publishes must name
# exactly the rows found; a broker that publishes none must mark every row instead.
DEADLINE_COUNTERS = ("deadline_failures", "case_deadline_calls")
# Failure families that must claim nothing once a deadline counter is the declaration.
EXCLUSIVE_FAILURE_COUNTERS = ("provider_failures", "credential_failures",
                              "protocol_failures", "broker_failures")


def deadline_error(row, error):
    """The evaluator's own case-deadline kill as the ledger names it, or None.

    The returned string is always `deadline:`-prefixed, so all three shapes reach the usage
    receipt under one name: a counter-shape ledger already writes the prefix itself, a
    row-shape ledger contributes `deadline:absolute_case_deadline` from its own reason, and
    openhands contributes `deadline:CaseDeadlineExceeded` from its own exception name.
    """
    if (isinstance(error, str) and error.startswith(DEADLINE_ERROR_PREFIX)
            and error[len(DEADLINE_ERROR_PREFIX):].strip()):
        return error
    if row.get("transport_abort_reason") == DEADLINE_ABORT_REASON:
        return DEADLINE_ERROR_PREFIX + DEADLINE_ABORT_REASON
    if isinstance(error, str) and error in DEADLINE_ERROR_NAMES:
        return DEADLINE_ERROR_PREFIX + error
    return None


def deadline_killed_ids(rows, *, identity, succeeded, error_of, attempts_of):
    """{request id: error} for rows the evaluator's own case deadline cut.

    Unlike a D13 recovered transport failure this needs no later success in the window:
    the kill is terminal for the case by construction -- the evaluator ended it.
    """
    result = {}
    for row in rows:
        if succeeded(row):
            continue
        error = deadline_error(row, error_of(row))
        if error is None or attempts_of(row) != 1:
            continue
        result[row[identity]] = error
    return result


def deadline_counters_consistent(runtime, rows, killed):
    """The ledger's own aggregates must agree these kills are the only failures it has."""
    if not killed:
        return True
    if not isinstance(runtime, dict):
        return False
    if type(runtime.get("failures")) is not int or runtime["failures"] != len(killed):
        return False
    if runtime.get("client_failures") not in (None, 0):
        return False
    declared = [runtime[k] for k in DEADLINE_COUNTERS if k in runtime]
    if declared:
        return (all(type(v) is int and v == len(killed) for v in declared)
                and all(runtime.get(k) in (None, 0) for k in EXCLUSIVE_FAILURE_COUNTERS))
    # No counter: every kill must carry the explicit per-row abort reason instead.
    return sum(1 for row in rows
               if row.get("transport_abort_reason") == DEADLINE_ABORT_REASON) == len(killed)


def canonical_deadline(request_id, error, attempts=1):
    """Canonical row for an evaluator-armed case-deadline kill.

    It reuses D13's unknown-usage carrier shape so that every task's existing usage-receipt
    exporter reports it as unknown usage and names it by identity with no change, and adds
    `deadline_killed` so the two tolerances stay distinguishable in the admitted evidence:
    D13 requires a later success in the same round, D54 requires the evaluator's own kill
    and never tolerates a provider fault.
    """
    require(isinstance(request_id, str) and bool(request_id), "provider request/response identity absent")
    require(type(attempts) is int and attempts == 1, "case-deadline transport attempt count invalid")
    require(isinstance(error, str) and error.startswith(DEADLINE_ERROR_PREFIX),
            "error is not an evaluator case-deadline kill")
    return dict(request_id=request_id, state="failure", upstream_attempts=attempts, known_tokens=0,
                usage_known=False, recovered_transport=True, deadline_killed=True, error=error)
# Named upstream transport faults. Each token is matched against the error string with all
# non-alphanumerics removed, so "provider:BrokenPipeError" and "provider broken pipe" agree.
TRANSPORT_ERROR_TOKENS = ("brokenpipe", "connectionreset", "connectionaborted", "connectionclosed",
                          "remotedisconnected", "serverdisconnected", "incompleteread",
                          "chunkedencoding", "ssleof", "prematureclose")
# Ambiguous faults that are tolerated only when the error names the provider/upstream side.
PROVIDER_SIDE_TRANSPORT_TOKENS = ("readtimeout", "readtimedout", "sockettimeout", "timeouterror",
                                  "timedout", "connecttimeout", "connectionerror")
# Never tolerated, whatever else the string contains: these are ours or the Candidate's.
NON_TRANSPORT_TOKENS = ("credential", "apikey", "unauthor", "forbidden", "invalidrequest",
                        "protocolfailure", "schema", "casedeadline", "deadlineexceeded",
                        "maxoutputtokens", "brokerrestart", "notdispatched", "notsent", "cancel")
# Matched on the raw text, because a module path such as http.client.RemoteDisconnected is a
# transport fault while the broker's own "client:" classification never is.
NON_TRANSPORT_TEXT = ("client:", "client_", "client failure", "client error", "clientfailure")
TRANSPORT_STATUS = re.compile(r"(?<![0-9])(5[0-9][0-9])(?![0-9])")


def transport_error(error):
    """True only for an upstream transport failure named by the D13 allowlist."""
    if not isinstance(error, str) or not error.strip():
        return False
    text = error.lower()
    squeezed = "".join(c for c in text if c.isalnum())
    if any(token in squeezed for token in NON_TRANSPORT_TOKENS) or any(token in text for token in NON_TRANSPORT_TEXT):
        return False
    if any(token in squeezed for token in TRANSPORT_ERROR_TOKENS):
        return True
    provider_side = any(marker in squeezed for marker in ("provider", "upstream", "http"))
    if provider_side and any(token in squeezed for token in PROVIDER_SIDE_TRANSPORT_TOKENS):
        return True
    return bool(provider_side and TRANSPORT_STATUS.search(text))


def canonical_recovered(request_id, error, attempts=1):
    """Canonical row for a tolerated transport failure: still a failure, usage still unknown."""
    require(isinstance(request_id, str) and bool(request_id), "provider request/response identity absent")
    require(type(attempts) is int and attempts == 1, "recovered transport attempt count invalid")
    require(transport_error(error), "error is not a tolerated upstream transport failure")
    return dict(request_id=request_id, state="failure", upstream_attempts=attempts, known_tokens=0,
                usage_known=False, recovered_transport=True, error=error)


def recovered_transport_ids(rows, *, identity, succeeded, error_of, attempts_of):
    """{request id: error} for allowlisted transport failures a later row recovered from.

    Ledger order is the only order the evidence carries, and it is the order in which the
    lower agent issued its logical requests: a later successful row proves it continued.
    """
    result = {}
    for index, row in enumerate(rows):
        if succeeded(row):
            continue
        error = error_of(row)
        if not transport_error(error) or attempts_of(row) != 1:
            continue
        if not any(succeeded(later) for later in rows[index + 1:]):
            continue
        result[row[identity]] = error
    return result


def known_usage_totals(raw, runtime, recovered, *, deadline_killed=()):
    """The totals to reconcile against: brokers null their runtime totals once a usage is
    unknown and publish `known_usage_subtotal` beside them (in runtime, or at ledger top
    level). With no tolerated row the runtime totals are used exactly as before."""
    if not recovered and not deadline_killed:
        return runtime
    subtotal = runtime.get("known_usage_subtotal")
    if subtotal is None and isinstance(raw, dict):
        subtotal = raw.get("known_usage_subtotal")
    if (subtotal is None and deadline_killed and not recovered
            and all(type(runtime.get(k)) is int for k in USAGE_KEYS)):
        # D54: a broker that never nulls its totals -- it sums only the rows whose usage it
        # knows -- publishes no subtotal (ai-scientist's lower ledger is the case). Its own
        # totals are then the known-usage totals, and the caller reconciles them against the
        # known rows exactly as it would the subtotal, so nothing is taken on trust. A broker
        # that DID null its totals still has to publish one: None is not an int and refuses.
        return runtime
    require(isinstance(subtotal, dict), "known usage subtotal absent while a usage is unknown")
    require(all(runtime.get(k) in (None, subtotal.get(k)) for k in USAGE_KEYS),
            "runtime totals contradict the known usage subtotal")
    return subtotal


def _row_succeeded(row):
    """An intent-ledger row that durably completed upstream with known usage."""
    return (row.get("usage_state") == "known" and row.get("upstream_completion") == "completed"
            and row.get("ok") is not False)


def require(value, message):
    if not value:
        raise ValueError(message)


def tokens(usage):
    require(isinstance(usage, dict) and all(type(usage.get(k)) is int and usage[k] >= 0 for k in
            ("input_tokens", "output_tokens", "total_tokens")), "usage token counters incomplete")
    require(usage["total_tokens"] >= usage["input_tokens"] + usage["output_tokens"], "usage totals inconsistent")
    return usage["total_tokens"]


def canonical(request_id, total, attempts=1):
    require(isinstance(request_id, str) and bool(request_id), "provider request/response identity absent")
    require(type(attempts) is int and attempts >= 1, "upstream attempt count invalid")
    return dict(request_id=request_id, state="success", upstream_attempts=attempts, known_tokens=total, usage_known=True)


def lower_request(raw, request_id, *, task, tolerate_recovered=False):
    require(raw.get("schema_version") == LOWER_SCHEMAS[task] and raw.get("transport_measurement") == MEASUREMENT,
            "unsupported lower ledger/measurement")
    protocol = raw.get("protocol", {}) if task == "openwiki" else raw
    require(protocol.get("model") == "deepseek-flash" and protocol.get("reasoning_effort") == "high", "lower model/effort mismatch")
    rows, intents = raw.get("requests"), raw.get("logical_requests")
    require(isinstance(rows, list) and isinstance(intents, dict) and rows, "lower ledger requests missing")
    require(all(isinstance(r, dict) for r in rows), "lower ledger row malformed")
    ids = [r.get("request_sha256") for r in rows]
    require(all(isinstance(rid, str) and bool(rid) for rid in ids), "lower request identity missing")
    require(len(set(ids)) == len(ids) and set(ids) == set(intents), "lower ledger rows/intent mismatch or truncated snapshot")
    runtime = raw.get("runtime", {}) if task == "openwiki" else raw
    require(all(type(runtime.get(k)) is int for k in ("calls", "successful_calls", "failures", "unknown_usage_calls")), "lower aggregate counters missing")
    # D13: tolerated transport failures are counted apart. Any other failed, pending or
    # unknown request still makes these aggregate counters disagree and refuses below.
    recovered = recovered_transport_ids(rows, identity="request_sha256", succeeded=_row_succeeded,
                                        error_of=lambda row: row.get("error"),
                                        attempts_of=lambda row: row.get("transport_attempts")) if tolerate_recovered else {}
    # D54: the evaluator's own case-deadline kills, which the ledger names itself. They are
    # counted apart for every lower role, because budget exhaustion is a Candidate outcome.
    killed = {rid: error for rid, error in deadline_killed_ids(
        rows, identity="request_sha256", succeeded=_row_succeeded,
        error_of=lambda row: row.get("error"),
        attempts_of=lambda row: row.get("transport_attempts")).items() if rid not in recovered}
    require(deadline_counters_consistent(runtime, rows, killed),
            "case-deadline kill not declared by the ledger's own counters")
    tolerated = len(recovered) + len(killed)
    require(runtime["calls"] == len(rows) and runtime["successful_calls"] == len(rows) - tolerated and
            runtime["failures"] == runtime["unknown_usage_calls"] == tolerated,
            "lower ledger incomplete/failed/unknown")
    total = 0
    for row in rows:
        rid = row["request_sha256"]
        intent = intents[rid]
        if rid in recovered or rid in killed:
            require(row.get("usage_state") == "unknown" and type(row.get("transport_attempts")) is int and
                    row["transport_attempts"] == 1 and row.get("upstream_completion") == "unknown_or_failed" and
                    intent.get("state") == "unknown_or_failed" and intent.get("transport_attempts") == 1 and
                    intent.get("result") == row, "recovered transport request not durably recorded")
            continue
        require(row.get("usage_state") == "known" and type(row.get("transport_attempts")) is int and row["transport_attempts"] == 1 and
                row.get("upstream_completion") == "completed" and intent.get("state") == "completed" and
                intent.get("transport_attempts") == 1 and intent.get("result") == row, "lower request not durably completed")
        if task == "openwiki":
            require(row.get("ok") is True, "lower request failed")
        total += tokens(row)
    require(tokens(known_usage_totals(raw, runtime, recovered, deadline_killed=killed)) == total,
            "lower token aggregate mismatch")
    matches = [row for row in rows if row["request_sha256"] == request_id]
    require(len(matches) == 1, "lower request ID not found")
    if request_id in recovered:
        return canonical_recovered(request_id, recovered[request_id])
    if request_id in killed:
        return canonical_deadline(request_id, killed[request_id])
    return canonical(request_id, tokens(matches[0]))


def judge_attempt(raw, request_id):
    require(raw.get("schema_version") == "agentswe-judge-broker-stats/v1", "unsupported judge broker ledger")
    protocol = raw.get("protocol", {})
    require(protocol.get("model") == "deepseek-flash" and protocol.get("reasoning_effort") == "max", "judge model/effort mismatch")
    rows, runtime = raw.get("attempts"), raw.get("runtime")
    require(isinstance(rows, list) and len(rows) == 1 and isinstance(runtime, dict), "judge smoke must have one attempt")
    require(all(type(runtime.get(k)) is int and runtime[k] == v for k, v in {
        "calls": 1, "completed_calls": 1, "successful_calls": 1, "failures": 0,
        "in_flight_calls": 0, "upstream_attempts": 1, "usage_unknown_calls": 0}.items()), "judge aggregate incomplete")
    row = rows[0]
    require(row.get("request_id") == request_id and row.get("state") == "terminal" and
            row.get("usage_unknown") is False and row.get("completed_response") is True and
            row.get("worker_reaped") is True and type(row.get("upstream_attempts")) is int and row["upstream_attempts"] == 1, "judge attempt incomplete/unknown")
    total = tokens(row.get("usage"))
    require(tokens(runtime) == total, "judge token aggregate mismatch")
    return canonical(request_id, total)


def direct_judge_request(raw, request_id):
    """Code direct HTTP uses provider response ID as the logical request key.

    Accept only one observed HTTP 200 completed response. That observation
    proves one actual request; ambiguous or retry attempts are not guessed.
    """
    require(raw.get("schema_version") == "agentswe-judge-http-attempts/v1" and raw.get("logical_requests") == 1,
            "unsupported direct judge ledger")
    rows = raw.get("attempts")
    require(isinstance(rows, list) and len(rows) == 1, "direct judge attempts incomplete or ambiguous")
    row = rows[0]
    require(row.get("attempt") == 1 and row.get("state") == "terminal" and row.get("http_status") == 200 and
            row.get("response_status") == "completed" and row.get("response_id") == request_id and
            row.get("usage_known") is True and row.get("error_type") is None, "direct judge response incomplete/unknown")
    return canonical(request_id, tokens(row.get("usage")))


def make_broker_record_normalizers(task):
    # D13 is scoped to the public lower ledger: only a public dev round can be accepted with
    # the lower agent having continued past the fault. The hidden smoke and both judges get
    # the strict normalizer, so a transport failure there still refuses.
    def lower_for(tolerate_recovered):
        if task in LOWER_SCHEMAS:
            return lambda raw, request_id: lower_request(raw, request_id, task=task,
                                                         tolerate_recovered=tolerate_recovered)
        if task in ("claude", "aider", "codex"):
            from v2_receipt_usage_normalizers import make_receipt_lower_normalizer
            return make_receipt_lower_normalizer(task)
        from v2_extra_usage_normalizers import make_extra_lower_normalizer
        return make_extra_lower_normalizer(task, tolerate_recovered=tolerate_recovered)
    return dict(public_lower=lower_for(True), hidden_lower=lower_for(False),
                result_judge=judge_attempt, code_judge=direct_judge_request)
