"""Evaluator-only scientific/durable observations; never executes a solution."""
from __future__ import annotations
import csv
import hashlib
import json
import math
import re
from pathlib import Path
try:
    from .case_specs import CASE_SPECS
except ImportError:
    from case_specs import CASE_SPECS

INVARIANTS = {
    "dev_001": ["one scientifically verified affine capsule", "claims linked to real source, tests, commands and artifact"],
    "dev_002": ["tie-breaking remains an explicit implementation assumption", "pause/resume uses persisted receipts"],
    "test_001": ["eight concurrent same-body requests create one publication", "exact retries preserve terminal receipt", "changed body conflicts without mutation", "generation/audit ordering is product evidence"],
    "test_002": ["expired owner cannot heartbeat/fail/commit after takeover", "partial stage is not executed", "corrupt replay rejected before retryable recovery", "exact commit retry preserves terminal receipt"],
    "test_003": ["body/output conflicts do not mutate accepted state", "foreign tenant is not disclosed or mutated", "escaping output rejected before side effects", "only red capsule committed"],
    "test_004": ["truncated staging reconciled without executing untrusted bytes", "reconciliation is idempotent", "checksum failure precedes side effects", "terminal corruption reported without rerun"],
    "test_005": ["stale generation cannot commit checksum-valid capsule", "readers see no mixed publication", "one terminal event and publication digest", "full and ablated evidence distinct"],
    "test_006": ["missing citation is not guessed or fetched", "incomplete capsule not published", "blocked capsule stays blocked under retry/corruption", "untracked project unaffected"],
}

def read(path):
    return json.loads(path.read_text())

def expected_science(case_id, project):
    result = {"seeds": CASE_SPECS[case_id]["seeds"], "claim_ids": CASE_SPECS[case_id]["ids"], "allowed_capsule_statuses": CASE_SPECS[case_id]["statuses"]}
    if case_id == "test_001":
        dt, tau = read(project / "data/times.json"), read(project / "config.json")["tau_seconds"]
        result["artifact"] = {"seed": 3, "weights": [[math.exp(-v/(1000*tau[c])) for c,v in enumerate(row)] for row in dt]}
    elif case_id == "test_003":
        result["artifact"] = {"seed": 5, "learning_rate": 0.01, "steps": 10, "final": 0.99**10}
        result["unresolved_alternative"] = "Paper 0.1 and implemented/configured 0.01 must remain distinguished."
    elif case_id == "test_004":
        with (project / "data/values.csv").open() as handle:
            values = [float(row["value"]) for row in csv.DictReader(handle)]
        mean = sum(values[:6])/6; std = math.sqrt(sum((x-mean)**2 for x in values[:6])/6)
        result["artifact"] = {"seed": 13, "train_indices": list(range(6)), "test_indices": [6,7], "train_mean": mean, "train_std": std,
            "train": [(x-mean)/std for x in values[:6]], "test": [(x-mean)/std for x in values[6:]]}
    elif case_id == "test_005":
        result["artifact"] = {"seed": 23, "full": {"outputs": [3,5,7], "mean": 5}, "without_gate": {"outputs": [2,4,6], "mean": 4}, "improvement": 1}
    elif case_id == "test_002":
        result.update({"draws": 200, "probabilities": "finite, nonnegative, normalized within 1e-12", "scientific_gap_must_remain_explicit": "Conversion from weights to probabilities unspecified by paper."})
    elif case_id == "test_006":
        result.update({"missing_definition": "kappa in unavailable SYN-CITE-17", "numeric_artifact_must_not_be_fabricated": True})
    elif case_id == "dev_002":
        result["artifact"] = {"seed": 11, "selected": "alpha", "tie": True, "input_order": ["alpha","beta","gamma"]}
    elif case_id == "dev_001":
        values, config = read(project / "data/input.json"), read(project / "config.json")
        mean = sum(values)/len(values); var = sum((x-mean)**2 for x in values)/len(values)
        result["artifact"] = {"seed": 0, "output": [config["gamma"]*(x-mean)/(math.sqrt(var)+config["epsilon"])+config["beta"] for x in values]}
    return result

def compare_values(expected, observed, path="artifact"):
    checks = {}
    if isinstance(expected, dict):
        for key, value in expected.items(): checks.update(compare_values(value, observed.get(key) if isinstance(observed, dict) else None, path+"."+key))
    elif isinstance(expected, list):
        checks[path+".length"] = isinstance(observed, list) and len(observed) == len(expected)
        for i,value in enumerate(expected): checks.update(compare_values(value, observed[i] if isinstance(observed,list) and i<len(observed) else None, f"{path}[{i}]"))
    elif isinstance(expected,(int,float)) and not isinstance(expected,bool):
        checks[path] = isinstance(observed,(int,float)) and not isinstance(observed,bool) and math.isfinite(observed) and abs(observed-expected)<=1e-12
    else: checks[path] = observed == expected
    return checks


# --- 0920 hardening: evaluator-computed product-surface observations ---------
# Deterministic, layout-agnostic evidence about the Cycle 003 surfaces, derived
# only from persisted product state that the evaluator already collects. These
# checks never execute the product and never prescribe a storage layout: they
# look for the *semantic* records the public contract requires. A ``False``
# value means "the evaluator could not observe this in persisted state", not
# "the Candidate is wrong"; Result resolves it against the actual trajectory.

REQUIRED_PRODUCT_OPERATIONS = {
    "dev_001": ["traceability.capsule", "runs.submit", "revisions.register", "revisions.review",
                "revisions.promote", "revisions.quarantine", "revisions.restore", "revisions.audit",
                "execution.start", "execution.advance"],
    "dev_002": ["traceability.capsule", "revisions.register", "revisions.review", "revisions.audit",
                "execution.start", "execution.advance", "execution.pause", "execution.resume"],
    "test_001": ["traceability.capsule", "runs.submit", "runs.claim", "runs.commit",
                 "revisions.register", "revisions.review", "revisions.promote", "revisions.audit",
                 "execution.start", "execution.advance"],
    "test_002": ["traceability.capsule", "runs.claim", "runs.commit", "revisions.register",
                 "execution.start", "execution.advance", "execution.pause", "execution.resume"],
    "test_003": ["traceability.capsule", "runs.submit", "revisions.register", "revisions.compare",
                 "revisions.inspect", "revisions.review", "revisions.audit"],
    "test_004": ["traceability.capsule", "runs.reconcile", "revisions.register", "revisions.reconcile",
                 "revisions.audit", "execution.start", "execution.advance", "execution.reconcile"],
    "test_005": ["traceability.capsule", "runs.commit", "revisions.register", "revisions.review",
                 "revisions.promote", "revisions.audit", "execution.start", "execution.advance",
                 "execution.cancel"],
    "test_006": ["traceability.capsule", "runs.commit", "runs.reconcile", "revisions.register",
                 "revisions.review", "revisions.promote", "revisions.inspect", "revisions.quarantine",
                 "revisions.restore", "revisions.audit"],
}

REQUIRED_FAIL_CLOSED_BOUNDARIES = {
    "dev_001": ["promotion without complete execution proof"],
    "dev_002": ["stale generation or claim token cannot advance"],
    "test_001": ["promote before review", "exact review retry does not advance the generation",
                 "stale expected_head_revision_id cannot swap the head",
                 "unauthorized audit export is refused without disclosure"],
    "test_002": ["stale generation heartbeat/fail/commit refused",
                 "old generation and rotated-away claim token cannot advance",
                 "corrupted replay refused before recovery"],
    "test_003": ["changed payload under the same key conflicts without mutation",
                 "unassigned actor is refused a review decision",
                 "foreign tenant inspect/compare/review/promote/audit refused without disclosure",
                 "traversal or escaping-symlink output refused before side effects"],
    "test_004": ["corrupted checksum refused before any sentinel side effect",
                 "reconcile executes no manifest command",
                 "plan bound to an invalidated revision cannot advance"],
    "test_005": ["stale generation cannot commit a checksum-valid capsule",
                 "cancelled generation cannot advance, pause or attest",
                 "promotion carrying the cancelled plan generation fails closed",
                 "stale expected_head_revision_id cannot overwrite a newer head"],
    "test_006": ["incomplete submission refused without publication",
                 "blocked revision cannot be promoted",
                 "stale policy version or quarantine generation refused",
                 "non-security actor refused quarantine/restore",
                 "tampered audit ledger fails closed"],
}

_HEX64 = re.compile(r"^[0-9a-f]{64}$")


def _objects(value):
    if isinstance(value, dict):
        yield value
        for item in value.values():
            yield from _objects(item)
    elif isinstance(value, list):
        for item in value:
            yield from _objects(item)


def _lists_of_objects(value):
    if isinstance(value, dict):
        for item in value.values():
            yield from _lists_of_objects(item)
    elif isinstance(value, list):
        if len(value) >= 2 and all(isinstance(item, dict) for item in value):
            yield value
        for item in value:
            yield from _lists_of_objects(item)


def _hex_keys(record):
    return {key for key, item in record.items() if isinstance(item, str) and _HEX64.match(item)}


def _linked_chain(sequence):
    """One append-only hash chain: some (hash key, previous key) links every pair."""
    head, previous = _hex_keys(sequence[0]), _hex_keys(sequence[1])
    for hash_key in head:
        for previous_key in previous:
            if hash_key == previous_key:
                continue
            if all(isinstance(item.get(previous_key), str) and isinstance(prior.get(hash_key), str)
                   and item[previous_key] == prior[hash_key]
                   for prior, item in zip(sequence, sequence[1:])):
                return True
    return False


def _ordered_checkpoints(records):
    for sequence in records:
        numbers = []
        for item in sequence:
            number = next((item[key] for key in ("sequence", "sequence_number", "checkpoint", "index", "step")
                           if type(item.get(key)) is int), None)
            if number is None or not any(key in item for key in ("exit_code", "command", "command_identity")):
                numbers = []
                break
            numbers.append(number)
        if numbers and len(set(numbers)) == len(numbers) and numbers == sorted(numbers):
            return True
    return False


def surface_observations(case_id, persisted, project):
    records = [item for entry in persisted for item in _objects(entry.get("value"))]
    sequences = [item for entry in persisted for item in _lists_of_objects(entry.get("value"))]
    graph_text = "\n".join(
        json.dumps(entry.get("value"), sort_keys=True) for entry in persisted
        if Path(entry.get("relative_path", "")).name in {"paper_spec.json", "traceability_graph.json"})
    ids = CASE_SPECS[case_id]["ids"]
    digests = {item["revision_digest"] for item in records
               if isinstance(item.get("revision_digest"), str) and _HEX64.match(item["revision_digest"])}
    checks = {
        "revision_registered": any(
            isinstance(item.get("revision_id"), str) and isinstance(item.get("revision_digest"), str)
            and _HEX64.match(item["revision_digest"]) for item in records),
        "revision_snapshot_immutable_reuse": any(
            sum(1 for item in records if item.get("revision_digest") == digest) >= 2 for digest in digests),
        "review_decisions_recorded": any(
            isinstance(item.get("decisions"), dict) and len(item["decisions"]) >= 1 for item in records),
        "review_generation_present": any(type(item.get("review_generation")) is int for item in records),
        "review_generation_advanced": any(
            type(item.get("review_generation")) is int and item["review_generation"] >= 1 for item in records),
        "promotion_head_recorded": any(
            isinstance(item.get("head_revision_id"), str) and item["head_revision_id"] for item in records)
            or any(isinstance(item.get("promotion"), dict) and item["promotion"] for item in records),
        "execution_plan_bound_to_digest": any(
            isinstance(item.get("plan_id"), str) and isinstance(item.get("revision_digest"), str)
            and _HEX64.match(item["revision_digest"]) for item in records),
        "execution_checkpoints_ordered": _ordered_checkpoints(sequences),
        "execution_generation_recorded": any(
            isinstance(item.get("plan_id"), str) and type(item.get("generation")) is int for item in records),
        "execution_attestation_bound": any(
            isinstance(item.get("attestation"), dict)
            and isinstance(item["attestation"].get("revision_digest"), str) for item in records),
        "audit_chain_linked": any(_linked_chain(sequence) for sequence in sequences),
        "quarantine_boundary_recorded": any(
            isinstance(item.get("quarantine"), dict) and item["quarantine"] for item in records)
            or any("quarantine" in str(item.get("kind", "")) for item in records),
        "capsule_graph_carries_case_ids": bool(graph_text) and all(identifier in graph_text for identifier in ids),
    }
    checks["revision_store_observed"] = checks["revision_registered"]
    checks["execution_store_observed"] = checks["execution_plan_bound_to_digest"] or checks["execution_generation_recorded"]
    return {
        "schema_version": "deepcode-surface-observation/v1",
        "required_product_operations": REQUIRED_PRODUCT_OPERATIONS[case_id],
        "required_fail_closed_boundaries": REQUIRED_FAIL_CLOSED_BOUNDARIES[case_id],
        "surface_assertion_comparisons": checks,
        "surface_checks_unobserved": sorted(key for key, value in checks.items() if not value),
        "capsule_graph_claim_ids": {identifier: (identifier in graph_text) for identifier in ids},
        "surface_check_semantics": (
            "Evaluator-computed from persisted product state only; the evaluator executed no product "
            "command. A false check means the evaluator could not observe that evidence in persisted "
            "state. Result must then look for the same behaviour in actual product responses in the "
            "trajectory before applying a published ceiling. These checks prescribe no storage layout, "
            "identifier format, or file naming."),
    }



# --- 0920 hardening round 2: product-owned state vs agent-captured evidence ----
# Round 1 treated every JSON under the workspace ``.deepcode`` tree as product
# state.  The 0920-fh-001 rollout showed why that is too generous: the driver
# also writes its own captured stdout under ``.deepcode/evidence`` and its own
# request bodies under ``.deepcode/operations``.  Those are the Candidate's
# narration of what happened, not the product's record of it.  Only a store --
# an object the product itself maintains, recognisable by an ``operations`` map
# or an ``events`` ledger -- counts for the deterministic checks below.

# 2026-09-21: the two PUBLIC dev cases declared reserved probes that their own task
# text never asks for.  test_cases/test_00N/input.md carries an "Adversarial probes"
# section and says, verbatim, "Run each probe with exactly the operation_id given"
# (test_cases/test_002/input.md:38), then lists every identifier -- so on the hidden
# axis the identifier IS a published obligation and c12 is fair.  dev_cases/dev_001/
# input.md and dev_cases/dev_002/input.md have no probe section, name no identifier and
# give no such instruction; measured on 0905-edit-codex-xhigh-0921-v4-001-deepcode round
# 4, the strings "adv-stale-token"/"adv-stale-generation" appear ZERO times in the dev
# rollout trajectory and ZERO times in the artifact, and only once each in the
# evaluator's own declaration below.  c12 was therefore unsatisfiable on dev by
# construction: it capped every dev case at 20 for a criterion no dev task publishes,
# which evaluator/result_score_caps.py's own contract forbids ("Every ceiling restates a
# published requirement ...; none of them is a new criterion").  A case that declares no
# reserved probe gets the explicit "unavailable" c12 entry build_entries already emits.
RESERVED_ADVERSARIAL_PROBES = {
    "test_001": ["adv-digest-stale", "adv-forged-head", "adv-race-review-b",
                 "adv-reject-promote", "adv-audit-tamper"],
    "test_002": ["adv-digest-stale", "adv-stale-token", "adv-stale-generation",
                 "adv-corrupt-replay", "adv-opid-reuse"],
    "test_003": ["adv-cross-tenant-inspect", "adv-cross-tenant-promote",
                 "adv-unassigned-review", "adv-traversal-output", "adv-digest-stale"],
    "test_004": ["adv-untrusted-stage", "adv-corrupt-checksum", "adv-advance-invalidated",
                 "adv-reconcile-execute", "adv-opid-reuse"],
    "test_005": ["adv-stale-commit", "adv-cancelled-advance", "adv-cancelled-promote",
                 "adv-stale-head-promote", "adv-opid-reuse"],
    "test_006": ["adv-blocked-promote", "adv-stale-policy-restore",
                 "adv-nonsecurity-quarantine", "adv-quarantined-start", "adv-audit-tamper"],
}


def _is_store(value):
    """A product-maintained store, not a single operation response.

    The published envelope says a response echoes ``operation_id``/``action`` and
    carries ``accepted``; an ``audit`` response therefore also carries an event
    list.  A store is the thing behind those responses: it keeps the receipt map
    and/or the ledger together with the objects they describe.
    """
    if not isinstance(value, dict):
        return False
    if "accepted" in value or "operation_id" in value:
        return False
    if isinstance(value.get("operations"), dict):
        return True
    return any(isinstance(value.get(key), list) for key in LEDGER_KEYS) and any(
        isinstance(value.get(key), dict) for key in ("revisions", "plans", "runs", "objects"))


# --- 0921b B1: the sharded-store gate is shape-and-provenance, not naming -----
# Round 1 of the sharded-store package (patch 60) admitted a shard only when some path
# component was underscore-prefixed (``_receipts``, ``_runs.json``).  That is an
# UNPUBLISHED naming convention: `input/02_interface_and_delivery.md:40` says an
# identifier is an opaque string and forbids assuming "a numeric suffix, a fixed prefix
# or a fixed length", `input/03` requirement 14 requires one persisted receipt per
# handled operation and says nothing about where it lives, and this file's own
# `surface_check_semantics` tells Result "These checks prescribe no storage layout,
# identifier format, or file naming" (:233-236).
#
# Measured on 0905-edit-codex-xhigh-0921b-v4-001-deepcode: the product files its stores
# as `.deepcode/traceability_{revisions,execution,runs}/receipts/receipt-<hash>.json`
# plus `revision_state.json` / `execution_state.json` / `runs_state.json` /
# `audit_ledger.json` -- no underscore anywhere -- so five of six cases read 0 stores,
# 0 receipts, 0 refusals and 0 ledger events while 22-26 receipts, 6-13 refusals with
# stable error codes and 3-9 ledger events sat in the recorded state.  The one case that
# read anything did so because the product happened to name a BACKUP directory
# `ops/_backup_revisions_pre_race/`.
#
# The 0920 boundary it was protecting stays intact and is now carried by the two things
# that actually distinguish the driver's narration from the product's record:
#   (a) SHAPE -- `_is_receipt_shard` needs `operation_id` + a `response` object + an
#       `exit_code`; `_is_index_shard` needs a non-empty registry/ledger table and
#       rejects anything carrying `accepted`/`operation_id`.  The driver's request
#       bodies (`ops/<step>.json`) carry neither a `response` nor an `exit_code`, and its
#       captured responses (`ops/out-*.json`) carry `accepted`/`operation_id` and are
#       rejected by `_is_index_shard`.
#   (b) PROVENANCE -- a path the case driver reserves for its own narration is excluded
#       outright, whatever shape it has.  These are enumerated from the driver's own
#       recorded output on this task (`.deepcode/**/evidence/`, `.deepcode/**/logs/`,
#       captured-response basenames `out-*`); the evaluator harness itself writes nothing
#       under `.deepcode` (`deepcode_lower_agent.py` writes only into the run's `output/`).
# Direction of the change: (b) can only REMOVE candidate files from the product side, so
# no obligation is weakened by it.

DRIVER_NARRATION_COMPONENTS = ("evidence", "logs", "log", "stdout", "stderr",
                               "narration", "report", "reports", "transcript",
                               "transcripts", "captured")
DRIVER_NARRATION_PREFIXES = ("out-", "out_", "output-", "output_", "stdout", "stderr",
                             "response-", "response_")


def _private_store_path(entry) -> bool:
    """A path component the product explicitly reserves for itself (``_receipts``).

    No longer a gate -- kept because such a name is still unambiguous product-private
    state and overrides the narration heuristic below.
    """
    parts = str(entry.get("relative_path") or "").split("/")
    return any(part.startswith("_") for part in parts if part)


def _driver_narration_path(entry) -> bool:
    """A path the case driver reserves for its narration (captured stdout, responses)."""
    parts = [part for part in str(entry.get("relative_path") or "").split("/") if part]
    if not parts:
        return True
    if any(part.lower() in DRIVER_NARRATION_COMPONENTS for part in parts[:-1]):
        return True
    name = parts[-1].lower()
    return any(name.startswith(prefix) for prefix in DRIVER_NARRATION_PREFIXES)


# --- 123 G1: a receipt's "exit semantics" is not the literal key `exit_code` ------------
# `input/03` requirement 20 asks for one persisted receipt per handled operation "operation subject,
# response, exit semantics, and a stable `error.code`" -- body, response, EXIT SEMANTICS and a stable error.code.
# It names no key.  `input/02_interface_and_delivery.md:66-69` defines those semantics as
# "accepted -> exit 0; well-formed conflict/refusal -> exit 2 with `accepted: false` and a stable
# `error.code`", so a receipt that records `accepted` as a boolean records the exit semantics
# exactly: exit 0 <=> accepted true, exit 2 <=> accepted false.  0921b B1 fixed the path-naming
# half of this gate; the key-naming half stayed.  Measured on
# 0905-edit-codex-xhigh-0922-di-v1-003-deepcode: the product (`core/traceability/receipts.py`)
# wrote 10/30/37/22/18 receipt shards per hidden case as
# `{accepted, error_code, operation_id, recorded_at, request{..}, response{accepted, error{code}}}`
# and the old predicate read every case as "0 receipts, 0 refusals" (c7 @28 and c12 @20 on 5/5).
#
# What still distinguishes the product's record from the driver's narration is unchanged:
#   * `response` must be an object -- the driver's request bodies (`operations/<step>.json`)
#     carry none, its captured responses (`out-*.json`, `evidence/**`) are excluded by path
#     (`_driver_narration_path`) and carry `accepted` at top level WITHOUT a nested response;
#   * the top-level `accepted` must be a real boolean and must AGREE with the nested
#     response's own `accepted` when that is recorded -- a record that contradicts itself is
#     not a receipt.
# `exit_code` alone still qualifies exactly as before (strictly additive).
def _receipt_exit_semantics(value) -> bool:
    """The receipt records its outcome as an exit code, or as the published accepted flag."""
    if "exit_code" in value:
        return True
    accepted = value.get("accepted")
    if type(accepted) is not bool:
        return False
    inner = value["response"].get("accepted")
    return type(inner) is not bool or inner is accepted


def _is_receipt_shard(value) -> bool:
    """One persisted operation receipt written as its own file."""
    return (isinstance(value, dict) and isinstance(value.get("operation_id"), str)
            and isinstance(value.get("response"), dict) and _receipt_exit_semantics(value))


# --- 123 G1: per-object table shards (`revisions/<revision_id>.json`, `plans/<plan_id>.json`) ---
# `_is_store` / `_is_index_shard` recognise a revision or plan table only as a dict held under a
# key named `revisions` / `plans` inside one file.  Nothing published fixes that layout:
# `input/02:40` forbids assuming an identifier's shape, requirement 8/21 state what a plan and
# its checkpoints must carry and never where it is filed, and this oracle's own
# `surface_check_semantics` says "These checks prescribe no storage layout ... or file naming".
# The di-v1-003 product files one object per file, named by the object's own id
# (`traceability_execution/plans/<plan_id>.json`, `traceability_revisions/revisions/<id>.json`),
# so `_checkpoint_chain_sound` saw no plan and `_accepted_mutation_count` no revision.
#
# The gate is structural, not a naming convention: a file is one row of a table exactly when its
# basename (minus `.json`) EQUALS the object's own `plan_id` / `revision_id` -- i.e. the product
# keyed the file by the id, as a dict table keys its rows.  Same provenance exclusion as the
# receipt shards; anything carrying `accepted` / `operation_id` (a response or request) is not an
# object row.  Rows are regrouped into one evaluator-assembled `{table: {id: row}}` store per
# directory, so every check below runs its round-1 rules on them unchanged.
OBJECT_SHARD_TABLES = (("plans", "plan_id"), ("revisions", "revision_id"))


def _object_shard_table(entry):
    """(`plans`|`revisions`, id) when this file is one row of a per-object table, else None."""
    if _driver_narration_path(entry) and not _private_store_path(entry):
        return None
    value = entry.get("value")
    if not isinstance(value, dict) or "accepted" in value or "operation_id" in value:
        return None
    name = str(entry.get("relative_path") or "").rsplit("/", 1)[-1]
    if not name.endswith(".json"):
        return None
    stem = name[:-len(".json")]
    for table, key in OBJECT_SHARD_TABLES:
        identity = value.get(key)
        if not (isinstance(identity, str) and identity and identity == stem):
            continue
        if table == "revisions" and not (isinstance(value.get("revision_digest"), str)
                                         and _HEX64.match(value["revision_digest"])):
            continue
        return table, identity
    return None


def _is_index_shard(value) -> bool:
    """A registry or ledger the product keeps beside its receipts."""
    if not isinstance(value, dict) or "accepted" in value or "operation_id" in value:
        return False
    if any(isinstance(value.get(key), dict) and value[key]
           for key in ("revisions", "plans", "runs", "objects")):
        return True
    return any(isinstance(value.get(key), list) and value[key]
               and all(isinstance(item, dict) for item in value[key]) for key in LEDGER_KEYS)


def _sharded_product_state(entry):
    """('store'|'receipt'|None) for a file that is not a monolithic store.

    0921b: gated on SHAPE (the two predicates above) and PROVENANCE (not a driver
    narration path), never on naming.  An explicitly product-private `_`-prefixed name
    still qualifies outright, as it did before.
    """
    if _driver_narration_path(entry) and not _private_store_path(entry):
        return None
    value = entry.get("value")
    if _is_index_shard(value):
        return "store"
    if _is_receipt_shard(value):
        return "receipt"
    return None


def _split_state(persisted):
    product, captured, receipts = [], [], {}
    objects = {}  # 123 G1: (directory, table) -> {id: row} for per-object table shards
    for entry in persisted:
        if _is_store(entry.get("value")):
            product.append(entry)
            continue
        kind = _sharded_product_state(entry)
        table = _object_shard_table(entry) if kind is None else None
        if kind == "store":
            product.append(entry)
        elif table is not None:
            relative = str(entry.get("relative_path") or "")
            directory = relative.rsplit("/", 1)[0] if "/" in relative else ""
            objects.setdefault((directory, table[0]), {})[table[1]] = entry.get("value")
        elif kind == "receipt":
            relative = str(entry.get("relative_path") or "")
            # Group by the surface that owns the shard: the segment before the product's
            # own `_`-reserved name when there is one, else the containing directory.
            # (Before 0921b every non-underscore path became its own synthetic store,
            # which inflated `product_store_count` without changing any check.)
            if "/_" in relative:
                surface = relative.split("/_", 1)[0] or relative
            else:
                surface = relative.rsplit("/", 1)[0] if "/" in relative else relative
            receipts.setdefault(surface, {})[relative] = entry.get("value")
        else:
            captured.append(entry)
    for surface, operations in sorted(receipts.items()):
        # One synthetic store per surface so the deterministic checks below see the
        # product's own receipt map; the keys stay the real file paths and the receipt
        # rows carry their own operation_id (see _receipts).
        product.append({"root": "workspace", "relative_path": surface + "/_receipts",
                        "sha256": None,
                        "provenance": "evaluator-assembled from the product's own per-operation "
                                      "receipt files; no Candidate narration is included",
                        "value": {"operations": operations}})
    for (directory, table), rows in sorted(objects.items()):
        # 123 G1: one synthetic `{table: {id: row}}` store per directory, rows byte-for-byte as
        # the product persisted them (see _object_shard_table).
        product.append({"root": "workspace", "relative_path": (directory + "/" if directory else "")
                        + "_" + table, "sha256": None,
                        "provenance": "evaluator-assembled from the product's own per-object "
                                      "files, each named by its own id; no Candidate narration "
                                      "is included",
                        "value": {table: rows}})
    return product, captured


# --- reserved-probe identity surfaces (2026-09-21, c12 false negative) --------
# `reserved_probe_receipts_observed` used to accept a probe only when the receipt's
# OWN operation_id equalled or started with the reserved probe name.  Nothing public
# requires that of a dev case: `dev_cases/dev_001/input.md` and
# `dev_cases/dev_002/input.md` have no adversarial-probe section at all and never name
# an `adv-*` identifier, while `input/02_interface_and_delivery.md:58` calls
# `operation_id` a "caller-unique-id", line 40 forbids assuming "a numeric suffix, a
# fixed prefix or a fixed length" of an identifier, and lines 70/74 require only that
# the response ECHO the operation_id and that the product persist one receipt per
# handled operation.  On 0905-edit-codex-xhigh-0921-v4-001-deepcode round 4 dev_002 the
# product persisted ten refusal receipts with stable error codes -- including
# `op-fence_pause_old_token-001 / UNAUTHORIZED` and
# `op-fence_advance_old_generation-001 / STALE_GENERATION`, which ARE the two reserved
# probes -- and c12 still capped the case at 20 because it had keyed them its own way.
#
# The identifier requirement is not dropped: the probe identifier must still appear in
# the PRODUCT's OWN persisted REFUSAL receipt, with a stable error.code.  Only the place
# it may appear is widened -- the receipt's own id, any value the product stored under a
# key literally named `operation_id` (which input/02:70 makes it echo), or the path/key
# it persisted the receipt under.  Free prose is never read, so narration cannot match.
ECHOED_ID_KEYS = ("operation_id", "operationId", "op_id")
ECHOED_ID_MAX_DEPTH = 6
ECHOED_ID_MAX = 64


def _echoed_operation_ids(value, depth=0, found=None):
    """Every value stored under a key literally named `operation_id`, bounded."""
    if found is None:
        found = set()
    if len(found) >= ECHOED_ID_MAX or depth > ECHOED_ID_MAX_DEPTH:
        return found
    if isinstance(value, dict):
        for key, item in value.items():
            if key in ECHOED_ID_KEYS and isinstance(item, str) and item:
                found.add(item)
            else:
                _echoed_operation_ids(item, depth + 1, found)
    elif isinstance(value, list):
        for item in value[:ECHOED_ID_MAX]:
            _echoed_operation_ids(item, depth + 1, found)
    return found


def _probe_observed(probe, refused_rows):
    """Did the product's own stores persist a REFUSAL receipt for this reserved probe?

    Unchanged obligations: the receipt is the product's own persisted state, the
    operation was refused (`accepted is False`) and it carries a stable `error.code`.
    """
    for _operation_id, _accepted, code, surfaces in refused_rows:
        if not code:
            continue
        if any(value == probe or value.startswith(probe) for value in surfaces["operation_ids"]):
            return True
        if any(probe in value for value in surfaces["paths"]):
            return True
    return False


# --- the identity-reuse probe: an identity, not a new receipt (0921b, c12 false negative) -
# `adv-opid-reuse` is defined by the case text itself -- `test_cases/test_002/input.md:54-55`,
# identically at `test_004:59-60` and `test_005:54-55` -- as "reuse the `operation_id` of one of
# the refused probes above with a different body; it must return `OPERATION_CONFLICT` and mutate
# nothing."  Its refusal is therefore keyed by the OTHER probe's id, and "mutate nothing" tells the
# product not to write a new receipt under this probe's own name, so `_probe_observed` above -- which
# looks for the probe NAME in a persisted refusal -- can never see it.  The same page's "Run each
# probe with exactly the `operation_id` given" cannot be satisfied at the same time; for this one
# probe the bullet governs.  Empirically the probe was covered 0 times in 20 slots across every
# deepcode run on disk, while `0905-edit-codex-xhigh-0921b-v4-002-deepcode` test_005 executed it
# exactly as written (`operations/adv-opid-reuse.json` carrying `operation_id: adv-stale-commit`)
# and was correctly refused `OPERATION_CONFLICT` -- and was still scored as a miss, which alone
# holds that case at a 40-point ceiling.
#
# Recognition is added for THIS probe only.  Nothing else is widened:
#   (A) the product's OWN stores hold two or more persisted refusal receipts under one and the same
#       operation id, that id is another reserved probe of this case, and at least one of those
#       receipts carries an operation-identity conflict code -- i.e. the reuse itself left a second,
#       conflicting refusal receipt; or
#   (B) the rollout persisted the probe in its own slot (a path naming `adv-opid-reuse`) and that
#       record shows a refusal (`accepted is False`) carrying an operation-identity conflict code
#       whose echoed `operation_id` is another reserved probe of this case that the PRODUCT'S OWN
#       stores independently persisted as refused.
# (B) is the only place in this oracle where a driver-captured file is read, and it is admissible
# only because the case text makes a product-side receipt impossible for this one probe and because
# every identifier in it is cross-checked against the product's own persisted refusals.  Narration
# about any other probe, a slot whose reused id the product never refused, and a slot that records
# no conflict refusal all remain worth nothing.
REUSE_IDENTITY_PROBES = ("adv-opid-reuse",)
REUSE_CODE_KINDS = ("CONFLICT", "DUPLICATE", "ALREADY_USED", "REPLAY")
REUSE_CODE_SUBJECTS = ("OPERATION", "OPID", "OP_ID", "IDEMPOT", "REUSE")


def _is_operation_reuse_code(code) -> bool:
    """A stable error code that names an operation-identity conflict, not any conflict."""
    if not isinstance(code, str) or not code:
        return False
    upper = code.upper()
    return (any(kind in upper for kind in REUSE_CODE_KINDS)
            and any(subject in upper for subject in REUSE_CODE_SUBJECTS))


def _reuse_refusal_recorded(value, depth=0) -> bool:
    """Some record inside `value` is a refusal carrying an operation-identity conflict code."""
    if depth > ECHOED_ID_MAX_DEPTH:
        return False
    if isinstance(value, dict):
        if value.get("accepted") is False:
            error = value.get("error") if isinstance(value.get("error"), dict) else {}
            if (_is_operation_reuse_code(error.get("code"))
                    or _is_operation_reuse_code(value.get("error_code"))):
                return True
        return any(_reuse_refusal_recorded(item, depth + 1) for item in value.values())
    if isinstance(value, list):
        return any(_reuse_refusal_recorded(item, depth + 1) for item in value[:ECHOED_ID_MAX])
    return False


def _reuse_probe_observed(probe, probes, refused_rows, captured):
    """Did the rollout run the identity-reuse probe and get it refused?  See the note above."""
    if probe not in REUSE_IDENTITY_PROBES:
        return False
    others = {name for name in probes if name != probe}
    # (A) a second, conflicting refusal receipt in the product's own stores under a reused probe id.
    for name in sorted(others):
        rows = [row for row in refused_rows if row[0] == name]
        if len(rows) >= 2 and any(_is_operation_reuse_code(row[2]) for row in rows):
            return True
    # (B) the probe's own slot, every identifier cross-checked against the product's own refusals.
    reused = {name for name in others if any(row[0] == name for row in refused_rows)}
    if not reused:
        return False
    variants = _reuse_slot_variants(probe)
    for entry in captured:
        path = str(entry.get("relative_path") or "")
        if not any(variant in path for variant in variants):
            continue
        value = entry.get("value")
        if not _reuse_refusal_recorded(value):
            continue
        if _echoed_operation_ids(value) & reused:
            return True
    # (B2) 123, 111-residual: the slot names the probe but holds only the REQUEST (the reused id
    # and the new body), while the refusal was captured into a file that does not name it (e.g.
    # one shared responses file, one record per line or list element).  Admissible under
    # exactly (B)'s cross-checks, applied per RECORD: the probe-named slot echoes a reused id X
    # that the product's own stores refused, and some captured file holds, AT ITS TOP LEVEL (the
    # file itself, or one element of a top-level list), a published response envelope
    # (`input/02:66-71`: `accepted: false`, `error.code`, echoed `operation_id` and `action`),
    # bare or as `{.., "response": envelope}`, whose code is an operation-identity conflict and
    # whose own `operation_id` is that same X.  Records nested deeper -- e.g. copies inside the
    # agent's own result/summary document -- are NOT read: those are the Candidate's report of
    # what happened, the boundary 0920 round 2 drew.  Prose and `results{..}` summaries without
    # a response envelope remain worth nothing.
    slot_ids = set()
    for entry in captured:
        path = str(entry.get("relative_path") or "")
        if any(variant in path for variant in variants):
            slot_ids |= _echoed_operation_ids(entry.get("value")) & reused
    if slot_ids:
        for entry in captured:
            if _reuse_refusal_ids(entry.get("value")) & slot_ids:
                return True
    return False


# 123, 111-residual: the slot-name test.  (B) matched only `adv-opid-reuse` / `adv_opid_reuse`
# in the path; 0922-tc-v1-002 test_004 (machine 27) filed the same structured record as
# `evidence/probe_opid_reuse.json` (accepted:false, OPERATION_CONFLICT, echoing
# adv-untrusted-stage, which the product itself refused CAPSULE_INVALID).  The distinctive stem
# of the probe name (without the `adv-` family prefix, either separator) names the same slot;
# every substantive condition of (B) is unchanged.  `_probe_observed`'s id match is NOT widened.
def _reuse_slot_variants(probe):
    stem = probe[len("adv-"):] if probe.startswith("adv-") else probe
    return tuple(dict.fromkeys((probe, probe.replace("-", "_"), stem, stem.replace("-", "_"))))


def _reuse_refusal_envelope_id(record):
    """The echoed operation_id of a top-level identity-conflict refusal envelope, else None."""
    if not isinstance(record, dict):
        return None
    for envelope in (record, record.get("response")):
        if not (isinstance(envelope, dict) and envelope.get("accepted") is False
                and isinstance(envelope.get("operation_id"), str) and envelope["operation_id"]
                and isinstance(envelope.get("action"), str)):
            continue
        error = envelope.get("error") if isinstance(envelope.get("error"), dict) else {}
        if _is_operation_reuse_code(error.get("code")):
            return envelope["operation_id"]
    return None


def _reuse_refusal_ids(value):
    """Echoed ids of identity-conflict refusal envelopes at the TOP LEVEL of one captured file."""
    records = value[:ECHOED_ID_MAX] if isinstance(value, list) else [value]
    return {found for found in map(_reuse_refusal_envelope_id, records) if found}


def _receipt_rows(stores):
    """Persisted receipts as (operation_id, accepted, error_code, identity_surfaces)."""
    rows = []
    for entry in stores:
        operations = entry["value"].get("operations")
        if not isinstance(operations, dict):
            continue
        store_path = str(entry.get("relative_path") or "")
        for key, record in operations.items():
            if not isinstance(record, dict):
                continue
            response = record.get("response") if isinstance(record.get("response"), dict) else record
            accepted = response.get("accepted")
            error = response.get("error") if isinstance(response.get("error"), dict) else {}
            code = error.get("code") if isinstance(error.get("code"), str) else None
            # 123 G1: a receipt that records its outcome at top level (`accepted`, `error_code`)
            # beside the nested response is read from there when the response omits it.  Only
            # fills a missing value; a response that records its own outcome still decides.
            if type(accepted) is not bool and type(record.get("accepted")) is bool:
                accepted = record["accepted"]
            if code is None and isinstance(record.get("error_code"), str) and record["error_code"]:
                code = record["error_code"]
            # A sharded store keys its receipts by file path; the receipt itself names the
            # operation, and the probe-coverage check matches on that identifier.
            identity = record.get("operation_id")
            operation_id = str(identity if isinstance(identity, str) else key)
            surfaces = {"operation_ids": _echoed_operation_ids(record) | {operation_id},
                        "paths": {store_path, str(key)}}
            rows.append((operation_id, accepted, code, surfaces))
    return rows


def _receipts(stores):
    """Every persisted operation receipt, as (operation_id, accepted, error_code)."""
    return [row[:3] for row in _receipt_rows(stores)]


LEDGER_KEYS = ("events", "audit", "ledger", "audit_log", "audit_events", "event_log")


def _ledger_events(stores):
    """The append-only ledger, under whichever of the usual names the product picked."""
    events = []
    for entry in stores:
        for key in LEDGER_KEYS:
            value = entry["value"].get(key)
            if isinstance(value, list) and value and all(isinstance(item, dict) for item in value):
                events.extend(value)
                break
    return events


def _kind(event):
    for key in ("event_type", "kind", "type", "action"):
        if isinstance(event.get(key), str):
            return event[key]
    return ""


def _canonical(stores, *names):
    """The objects a store itself keys, never a copy nested inside a receipt."""
    for entry in stores:
        for name in names:
            table = entry["value"].get(name)
            if isinstance(table, dict):
                for record in table.values():
                    if isinstance(record, dict):
                        yield record


def _accepted_mutation_count(stores):
    """Registrations, recorded review decisions, promotions and quarantine moves."""
    total = 0
    for record in _canonical(stores, "revisions"):
        total += 1
        decisions = record.get("decisions")
        if isinstance(decisions, dict):
            total += len(decisions)
        if record.get("promotion"):
            total += 1
        quarantine = record.get("quarantine")
        if isinstance(quarantine, dict) and quarantine.get("status") not in (None, "active"):
            total += 1
    return total


# --- 123 G4: "no checkpoint chain recorded" is unobserved, not unsound --------------------
# Before 123 this returned `seen_plan`, i.e. False when the evaluator found no plan carrying a
# checkpoint list at all, and c10 then issued "checkpoint chain unsound @30".  That contradicts
# the 0921b design this oracle already applies to c9 (`_promotion_binding_sound` returns None ->
# c9 `unavailable`) and its own note that a False surface check "means the evaluator could not
# observe this", and it multiplied G1: on di-v1-003 the plans existed but were filed per object.
# Obligation kept: every recorded checkpoint list is checked by exactly the round-1 rules below
# (`input/03` requirement 21: unique, ordered, command identity, exit code, output digest,
# proof reuses the checkpoint digests), and a plan that claims completion or carries a
# proof/attestation with NO checkpoint behind it is still False -- that is a recorded, unbacked
# proof, not an unobserved chain.  Only "nothing recorded anywhere" becomes None, which
# result_score_caps renders as c10 `unavailable` (site S8), exactly like c9.
def _checkpoint_chain_sound(stores):
    """Ordered, unique, command-identified checkpoints whose digests back the proof.

    None when no plan recorded a checkpoint list and none claims a completion proof.
    """
    seen_plan = False
    unbacked_proof = False
    if True:
        for record in _canonical(stores, "plans"):
            checkpoints = record.get("checkpoints")
            if not isinstance(checkpoints, list) or not checkpoints:
                if record.get("status") in ("complete", "completed") or any(
                        isinstance(record.get(key), dict) and record[key]
                        for key in ("proof", "attestation")):
                    unbacked_proof = True
                continue
            if not all(isinstance(item, dict) for item in checkpoints):
                return False
            seen_plan = True
            order = [next((item[key] for key in ("index", "sequence", "sequence_number", "step")
                           if type(item.get(key)) is int), None) for item in checkpoints]
            if any(value is None for value in order) or order != sorted(order) or len(set(order)) != len(order):
                return False
            for item in checkpoints:
                if "exit_code" not in item:
                    return False
                if not any(isinstance(item.get(key), (str, list)) for key in
                           ("command_id", "command", "argv", "command_identity")):
                    return False
                if not any(isinstance(item.get(key), str) for key in
                           ("output_digest", "output_sha256", "output_hash")):
                    return False
            digests = [item.get("checkpoint_digest") for item in checkpoints
                       if isinstance(item.get("checkpoint_digest"), str)]
            proof = record.get("proof") if isinstance(record.get("proof"), dict) else \
                record.get("attestation") if isinstance(record.get("attestation"), dict) else None
            if digests and isinstance(proof, dict):
                claimed = proof.get("checkpoint_digests")
                if isinstance(claimed, list) and claimed != digests:
                    return False
    if seen_plan:
        return True
    return False if unbacked_proof else None


# --- 0921b B2: find the promotion wherever the product filed it --------------
# Round 1 read a promotion only at `revisions[<id>].promotion`.  Nothing published fixes
# that key: `input/02_interface_and_delivery.md:133` fixes only the `promote` REQUEST
# fields (`revision_id`, `revision_digest`, ...), :157 requires only that a failed
# review/promotion leave no partial revision or head update, and :40 forbids assuming a
# fixed identifier shape; `input/03` requirement 13 states the compare-and-swap
# obligation and never names a storage key.  On
# 0905-edit-codex-xhigh-0921b-v4-001-deepcode the product records a TOP-LEVEL
# `promotions: [...]` list on `revision_state.json`, each element carrying a complete
# `binding {plan_id, plan_generation, review_generation, revision_digest, proof_digest,
# head_before}`, and leaves `revisions[<id>].promotion` null -- so `seen` was never set
# and the old `return seen` reported "no promotion found" to the judge as
# "a recorded promotion disagrees with its bound plan" on all six cases.
#
# Two changes, neither of which weakens the obligation:
#   1. A promotion is collected from `revisions[<id>].promotion`, from a
#      `revisions[<id>].promotions` list, and from a top-level `promotions` list/map on
#      any store; its fields may sit directly on the record or inside `binding`.
#   2. The verdict is tri-state.  None == the product recorded no promotion at all, which
#      `result_score_caps` renders as c9 `unavailable` instead of `violated`.  Every
#      recorded promotion is still checked by exactly the round-1 rules, plus the
#      promotion's OWN `revision_digest` is now compared against the plan as well as the
#      revision's -- strictly stricter, so a mismatched digest still violates.

PROMOTION_RECORD_KEYS = ("promotion", "promotions")


def _promotion_fields(value):
    """A promotion's fields, whether written flat or inside a `binding` block."""
    if not isinstance(value, dict) or not value:
        return None
    merged = dict(value)
    for key in ("binding", "bound_to", "cas", "compare_and_swap"):
        nested = value.get(key)
        if isinstance(nested, dict):
            merged.update(nested)
    return merged


def _promotion_records(stores):
    """[(promotion_fields, owning_revision_record)] over every recorded promotion."""
    index, revisions = {}, []
    for record in _canonical(stores, "revisions"):
        revisions.append(record)
        for key in ("revision_id", "revision_key", "id", "key"):
            if isinstance(record.get(key), str):
                index.setdefault(record[key], record)
        if isinstance(record.get("revision_digest"), str):
            index.setdefault("digest:" + record["revision_digest"], record)

    def owner(fields):
        for key in ("revision_id", "revision_key", "id", "key"):
            if isinstance(fields.get(key), str) and fields[key] in index:
                return index[fields[key]]
        digest = fields.get("revision_digest")
        if isinstance(digest, str):
            return index.get("digest:" + digest, {})
        return {}

    found = []
    for record in revisions:
        for key in PROMOTION_RECORD_KEYS:
            value = record.get(key)
            items = value.values() if isinstance(value, dict) and key == "promotions" \
                else [value] if isinstance(value, dict) else value if isinstance(value, list) else []
            for item in items:
                fields = _promotion_fields(item)
                if fields:
                    found.append((fields, record))
    for entry in stores:
        value = entry.get("value")
        if not isinstance(value, dict):
            continue
        listed = value.get("promotions")
        items = listed if isinstance(listed, list) else \
            list(listed.values()) if isinstance(listed, dict) else []
        for item in items:
            fields = _promotion_fields(item)
            if fields:
                found.append((fields, owner(fields)))
    return found


def _promotion_binding_sound(stores):
    """A recorded promotion must be backed by its own plan and by approvals.

    A later review legitimately advances the revision's review generation past a
    recorded promotion (that is what makes the promotion stale), so the rule is
    not equality with the current generation; it is that the promotion agrees
    with the plan it names and that no rejection was standing when it happened.

    Returns None when the product recorded no promotion anywhere -- "not recorded" is
    not "unsound", and c9 is then reported as unavailable rather than violated.
    """
    plans = {record["plan_id"]: record for record in _canonical(stores, "plans")
             if isinstance(record.get("plan_id"), str)}
    promotions = _promotion_records(stores)
    if not promotions:
        return None
    for promotion, record in promotions:
        promoted_at = promotion.get("review_generation")
        current = record.get("review_generation")
        if type(promoted_at) is int and type(current) is int and promoted_at > current:
            return False
        plan = plans.get(promotion.get("plan_id"))
        if isinstance(plan, dict):
            for digest in (record.get("revision_digest"), promotion.get("revision_digest")):
                if isinstance(digest, str) and plan.get("revision_digest") != digest:
                    return False
            if type(plan.get("review_generation")) is int and type(promoted_at) is int \
                    and plan["review_generation"] != promoted_at:
                return False
        decisions = record.get("decisions")
        if isinstance(decisions, dict):
            for item in decisions.values():
                if not isinstance(item, dict) or item.get("decision") != "reject":
                    continue
                at = item.get("generation")
                if type(at) is not int:
                    at = current if type(current) is int else None
                if at is None or type(promoted_at) is not int or at <= promoted_at:
                    return False
    return True


def round_two_observations(case_id, persisted):
    stores, captured = _split_state(persisted)
    rows = _receipt_rows(stores)
    receipts = [row[:3] for row in rows]
    refused_rows = [row for row in rows if row[1] is False]
    refused = [row[:3] for row in refused_rows]
    codes = sorted({row[2] for row in refused if row[2]})
    probes = RESERVED_ADVERSARIAL_PROBES.get(case_id, [])
    covered = sorted(name for name in probes
                     if _probe_observed(name, refused_rows)
                     or _reuse_probe_observed(name, probes, refused_rows, captured))
    events = _ledger_events(stores)
    refused_ids = {row[0] for row in refused}
    logged_refusals = sorted({str(event.get("operation_id")) for event in events
                              if isinstance(event.get("operation_id"), str)} & refused_ids)
    mutations = _accepted_mutation_count(stores)
    checks = {
        "refused_operations_persisted": bool(refused),
        "reserved_probe_receipts_complete": bool(probes) and len(covered) == len(probes),
        "ledger_covers_accepted_mutations": bool(events) and len(events) >= mutations,
        "ledger_excludes_refused_operations": not logged_refusals,
        "ledger_hashes_unique": len({event.get("hash") for event in events
                                     if isinstance(event.get("hash"), str)}) ==
                                len([event for event in events if isinstance(event.get("hash"), str)]),
        "checkpoint_chain_sound": _checkpoint_chain_sound(stores),
        "promotion_binding_sound": _promotion_binding_sound(stores),
    }
    return {
        "product_store_count": len(stores),
        "agent_captured_file_count": len(captured),
        "product_store_paths": sorted(entry["relative_path"] for entry in stores),
        "agent_captured_evidence_is_not_product_state": (
            "Files the driver wrote under the workspace (captured stdout, request bodies) are the "
            "Candidate's narration. Only the product's own stores are used for the checks below, and "
            "Result must not treat a captured response file as proof that the product behaved that way."),
        "persisted_operation_receipts": len(receipts),
        "persisted_refusal_receipts": len(refused),
        "persisted_refusal_error_codes": codes,
        "reserved_adversarial_probes": probes,
        "reserved_probe_receipts_observed": covered,
        "accepted_mutation_estimate": mutations,
        "ledger_event_count": len(events),
        "ledger_event_kinds": [_kind(event) for event in events],
        "round_two_assertion_comparisons": checks,
        # 0921b: a check whose evidence the product never recorded at all is `None`, not
        # False, and is listed separately so Result does not read "not recorded" as
        # "recorded and wrong".  result_score_caps renders those as `unavailable`.
        "round_two_checks_unobserved": sorted(key for key, value in checks.items()
                                              if value is not None and not value),
        "round_two_checks_unavailable": sorted(key for key, value in checks.items()
                                               if value is None),
    }


def observe(case_id: str, *, project: Path, workspace: Path, home: Path) -> dict:
    expected = expected_science(case_id, project)
    name = CASE_SPECS[case_id]["artifact"]
    artifact = workspace / "artifacts" / name if name else None
    # Product execution may persist its scientific output beneath its native runtime
    # artifact directory. Preserve every observed output and its comparisons; do not
    # infer scientific failure from the absence of one preferred filename.
    candidates=[artifact] if artifact else []
    native=workspace / ".deepcode/traceability_execution/artifacts"
    if native.is_dir() and not native.is_symlink():candidates.extend(sorted(native.glob("*.json")))
    scientific_outputs=[]
    for path in candidates:
        if path is None or not path.is_file() or path.is_symlink():continue
        try:
            relative=path.relative_to(workspace)
            if any((workspace / Path(*relative.parts[:i])).is_symlink() for i in range(1,len(relative.parts)+1)):continue
            if path.stat().st_size>512000:continue
            value=read(path)
        except (OSError,ValueError):value={"invalid_json":True}
        scientific_outputs.append({"path":str(path),"relative_path":str(relative),
            "sha256":hashlib.sha256(path.read_bytes()).hexdigest(),"value":value,
            "assertion_comparisons":compare_values(expected["artifact"],value) if "artifact" in expected else {},
            "provenance":"observed product output bytes; native trajectory and checkpoint receipts remain separate evidence"})
    # A primary filename, or a single unambiguous native output, gives a singular
    # observation. Multiple alternate files remain explicitly ambiguous to Result.
    primary=next((r for r in scientific_outputs if artifact and r["path"]==str(artifact)),None)
    selected=primary or (scientific_outputs[0] if len(scientific_outputs)==1 else None)
    payload=selected["value"] if selected else None
    comparisons = compare_values(expected["artifact"], payload) if "artifact" in expected else {}
    if case_id == "test_002":
        runs = payload.get("runs",[]) if isinstance(payload,dict) else []
        comparisons["exact_seeds"] = [r.get("seed") for r in runs] == [7,19,31]
        comparisons["draws"] = isinstance(payload,dict) and payload.get("draws")==200
        for i,run in enumerate(runs):
            p,counts = run.get("probabilities",[]),run.get("counts",[])
            comparisons[f"run_{i}_probabilities"] = bool(p) and all(isinstance(x,(int,float)) and math.isfinite(x) and x>=0 for x in p) and abs(sum(p)-1)<=1e-12
            comparisons[f"run_{i}_counts"] = bool(counts) and all(type(x) is int and x>=0 for x in counts) and sum(counts)==200
    observed = []
    for root in (workspace / ".deepcode",home):
        if not root.is_dir(): continue
        for path in sorted(root.rglob("*.json")):
            if path.is_symlink() or path.name=="deepcode_config.json" or path.stat().st_size>512000: continue
            try: value=read(path)
            except (OSError,ValueError): value={"invalid_json":True}
            observed.append({"root":"workspace" if root.parent==workspace else "product_home", "relative_path":str(path.relative_to(root)),
                "sha256":hashlib.sha256(path.read_bytes()).hexdigest(), "value":value})
    return {"schema_version":"deepcode-semantic-oracle/v1", "case_id":case_id, "candidate_visible":False,
        "expected_scientific_facts":expected, "expected_product_invariants":INVARIANTS[case_id], "actual_scientific_artifact":payload,
        "scientific_assertion_comparisons":comparisons, "observed_scientific_outputs":scientific_outputs,
        "scientific_output_selection": "preferred_path" if primary else "single_native_output" if selected else "absent_or_ambiguous", "actual_persisted_product_state":observed,
        **surface_observations(case_id, observed, project),
        **round_two_observations(case_id, observed),
        "evaluator_executed_product_actions":False, "native_evidence_is_not_a_replacement_for_agent_artifact":True,
        "id_fields_and_model_calls_earn_no_semantic_points":True}
