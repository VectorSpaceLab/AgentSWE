#!/usr/bin/env python3
"""Evidence-bound task-local Result ceilings for the Aider transaction task.

Every condition is decided by the evaluator from its own observations -- real
Git refs and ``160000`` entries, admission bytes under each participant's Git
common directory, ``state_dir/ledger.json`` bytes, worktree listings and the
append-only execution record of the evaluator-owned worker/test command.  A
Candidate claim inside ``agent_result.json`` can never enable or clear a
condition; the single condition that reads the artifact (``c9``) only asks
whether the report quotes identities the evaluator independently observed.

The contract is consumed by the registry-pinned shared judge through
``result_judge.load_score_caps``: it binds the exact rubric / native-evidence /
oracle bytes of this scoring run, and every entry carries a public requirement
reference.  Only determinate statuses are issued (``violated``,
``not_violated``, ``unavailable``); no ``semantic_review`` entry is produced, so
the judge's response schema is byte-identical to the uncapped one and the
readiness bundle validator keeps revalidating the same payload shape.
"""
from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path
from typing import Any

SCHEMA_VERSION = "agentswe-result-score-caps/v1"

# Set to False to issue the contract but apply no ceiling (diagnostic only).
ISSUE_SCORE_CAPS = True

# cap_id, maximum_score, oracle check keys (all must be True), requirement ref, reason
CHECK_CONDITIONS: tuple[tuple[str, int, tuple[str, ...], str, str], ...] = (
    ("c1_no_durable_transaction", 15, ("durable_transaction_observed",),
     "02_interface_and_delivery.md#quarantine-objects-and-global-prepare / ledger.json",
     "No invocation produced a durable, case-bound state_dir/ledger.json, so no transaction "
     "state the product could recover or publish ever existed."),
    ("c2_case_terminal_not_evidenced", 25, ("safe_terminal_or_refusal",),
     "01_task_goal.md / 02_interface_and_delivery.md#commit-decision-and-cross-repository-publication",
     "The evaluator did not independently observe the terminal this case's world supports: "
     "either a publication whose refs and 160000 gitlinks match the prepared candidates, or a "
     "fail-closed refusal that left every participant at its base with bounded evidence."),
    ("c3_required_product_sequence_incomplete", 30, ("required_action_sequence",),
     "02_interface_and_delivery.md#workers-and-integration / README.md",
     "The product was never driven through the interface boundary this case requires, so the "
     "case's transactional behaviour was never exercised."),
    ("c4_foreign_state_mutated", 10, ("foreign_state_preserved",),
     "03_requirements_and_constraints.md#8 / 02_interface_and_delivery.md#rollback-corruption-and-ownership",
     "Bytes, admission receipts or refs belonging to an external author, a foreign locked "
     "worktree or a disjoint transaction changed during this rollout."),
    ("c5_read_only_or_retry_effects", 20,
     ("read_only_status_left_no_durable_change", "repeat_requests_repeated_no_effect"),
     "02_interface_and_delivery.md#repository-scoped-admission-and-epoch-fences (status) / 03#9",
     "A strictly read-only status request left a durable change, or an exact retry after a "
     "settled response repeated a durable effect."),
    ("c6_commands_not_exactly_once", 25, ("commands_executed_at_most_once",),
     "02_interface_and_delivery.md#quarantine-objects-and-global-prepare / 03_requirements_and_constraints.md#9",
     "The evaluator-owned worker/test command really ran more than once for a declared "
     "subtask, or its append-only record was truncated: effects are not exactly once."),
    ("c7_predecision_quarantine_leak", 20, ("predecision_quarantine_respected",),
     "02_interface_and_delivery.md#quarantine-objects-and-global-prepare",
     "A candidate object the response reported was already readable from a participant's "
     "ordinary object database before any verified publication."),
    ("c8_owned_resources_not_released", 40, ("owned_resources_released",),
     "02_interface_and_delivery.md#commit-decision-and-cross-repository-publication (cleanup) / 03#9",
     "After the transaction settled, transaction-owned admission receipts, branches or "
     "worktrees were still present in a participant repository."),
    ("c11_no_terminal_receipt_or_followup", 30,
     ("terminal_receipt_observed", "required_followup_observation"),
     "02_interface_and_delivery.md#verifiability-facts-the-evaluator-observes-independently 9",
     "No settled response carried a per-repository receipt covering every participant, or the "
     "transaction was never re-observed through the interface after its terminal action, so the "
     "receipt and ownership evidence the case asks for were never produced."),
    ("c10_object_store_not_content_addressed", 30, ("content_addressed_object_store_valid",),
     "02_interface_and_delivery.md#quarantine-objects-and-global-prepare (state_dir/objects/sha256)",
     "The product built candidates but `state_dir/objects/sha256/<hex>` does not hold bytes that "
     "hash to their own names and cover the sha256 digests its durable ledger references, so the "
     "prepare/quarantine digests are not content-addressed evidence."),
)

REPORT_CAP = ("c9_report_not_evidence_bound", 35,
              "02_interface_and_delivery.md#response-report / 03_requirements_and_constraints.md#11",
              "The delivered agent_result.json quotes fewer than two of the dynamic identities "
              "the evaluator actually observed (transaction id, ledger digest, run nonce, base "
              "or published OIDs), so its claims are not bound to this rollout.")

HEX40 = re.compile(r"[0-9a-f]{40}")

# Caps whose evidence cannot exist when the case's own oracle expects the
# product to refuse *before* a durable transaction is formed.  For
# ``expected_terminal_class == "fail_closed"`` (test_004's foreign locked
# worktree, test_006's corrupted global decision) the correct behaviour is a
# pre-plan refusal, so demanding ``state_dir/ledger.json`` as the price of
# exceeding 15 penalises the right answer.  The ceiling for a hollow or
# unevidenced refusal is still enforced, by ``c2_case_terminal_not_evidenced``
# (25) and ``c11_no_terminal_receipt_or_followup`` (30), which read the
# refusal itself.  No other terminal class is exempt: a publication case must
# publish, and a reverse/bounded-obstruction case must have something durable
# to reverse.
REFUSAL_EXEMPT_CAPS: dict[str, tuple[str, ...]] = {
    "c1_no_durable_transaction": ("fail_closed",),
}


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with Path(path).open("rb") as handle:
        while chunk := handle.read(1024 * 1024):
            digest.update(chunk)
    return digest.hexdigest()


def read_json(path: Path) -> Any:
    try:
        return json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, UnicodeError, ValueError):
        return None


def oracle_checks(oracle_summary: Path) -> dict[str, Any]:
    value = read_json(oracle_summary)
    comparison = (value or {}).get("semantic_comparison") if isinstance(value, dict) else None
    checks = comparison.get("checks") if isinstance(comparison, dict) else None
    return checks if isinstance(checks, dict) else {}


def expected_terminal_class(oracle_summary: Path) -> str | None:
    """The case's own declared terminal class, as the evaluator recorded it."""
    value = read_json(oracle_summary)
    comparison = (value or {}).get("semantic_comparison") if isinstance(value, dict) else None
    terminal = comparison.get("expected_terminal_class") if isinstance(comparison, dict) else None
    return terminal if isinstance(terminal, str) else None


def observed_identities(oracle_summary: Path, native_evidence: Path) -> set[str]:
    """Dynamic identities this rollout actually produced, per the evaluator."""
    identities: set[str] = set()
    oracle = read_json(oracle_summary)
    comparison = (oracle or {}).get("semantic_comparison") if isinstance(oracle, dict) else None
    facts = comparison.get("runtime_facts") if isinstance(comparison, dict) else None
    if isinstance(facts, dict):
        if isinstance(facts.get("run_nonce"), str):
            identities.add(facts["run_nonce"])
        for value in (facts.get("base_oids") or {}).values():
            if isinstance(value, str) and HEX40.fullmatch(value):
                identities.add(value)
    native = read_json(native_evidence)
    state = (native or {}).get("state") if isinstance(native, dict) else None
    if isinstance(state, dict):
        for item in state.get("invocations") or []:
            if isinstance(item, dict) and isinstance(item.get("ledger_digest"), str) and item["ledger_digest"]:
                identities.add(item["ledger_digest"])
        for response in (state.get("responses") or {}).values():
            if not isinstance(response, dict):
                continue
            for key in ("transaction_id", "plan_id"):
                if isinstance(response.get(key), str) and len(response[key]) >= 8:
                    identities.add(response[key])
            ledger = response.get("ledger") if isinstance(response.get("ledger"), dict) else {}
            if isinstance(ledger.get("digest"), str) and ledger["digest"]:
                identities.add(ledger["digest"])
        for observation in state.get("product_observations") or []:
            if not isinstance(observation, dict):
                continue
            for row in (observation.get("repositories") or {}).values():
                if isinstance(row, dict) and isinstance(row.get("head_oid"), str) and HEX40.fullmatch(row["head_oid"]):
                    identities.add(row["head_oid"])
    return {value for value in identities if isinstance(value, str) and len(value) >= 8}


def artifact_identity_matches(artifact: Path | None, identities: set[str]) -> set[str]:
    if artifact is None or not Path(artifact).is_file() or not identities:
        return set()
    try:
        text = Path(artifact).read_text(encoding="utf-8", errors="replace")
    except OSError:
        return set()
    matched = set()
    for value in identities:
        # A sha256: prefixed digest and a bare 40-hex OID both count when the
        # distinguishing body appears verbatim in the report.
        body = value.split(":", 1)[1] if value.startswith("sha256:") else value
        if len(body) >= 8 and body in text:
            matched.add(value)
    return matched


def entry(cap_id: str, maximum: int, status: str, requirement_ref: str, reason: str,
          evidence_refs: list[str]) -> dict[str, Any]:
    return {"cap_id": cap_id, "maximum_score": maximum, "status": status,
            "requirement_ref": requirement_ref, "reason": reason,
            "evidence_refs": sorted(set(evidence_refs))}


def build_contract(case_id: str, *, rubric: Path, native_evidence: Path, oracle_summary: Path,
                   artifact: Path | None) -> dict[str, Any]:
    checks = oracle_checks(oracle_summary)
    oracle_ref = str(oracle_summary)
    terminal_class = expected_terminal_class(oracle_summary)
    entries: list[dict[str, Any]] = []
    for cap_id, maximum, keys, requirement_ref, reason in CHECK_CONDITIONS:
        values = [checks.get(key) for key in keys]
        if any(value is None for value in values):
            status = "unavailable"
        elif all(value is True for value in values):
            status = "not_violated"
        else:
            status = "violated"
        refs = [] if status == "unavailable" else [
            oracle_ref + "#semantic_comparison.checks." + key for key in keys]
        if status == "violated" and terminal_class in REFUSAL_EXEMPT_CAPS.get(cap_id, ()):
            # This case's own oracle expects a refusal before a durable
            # transaction exists, so the missing evidence is not a violation.
            # `unavailable` is neither proof of violation nor of compliance
            # (evaluator/rubric.md#evidence-constraint-cap).
            status = "unavailable"
            reason = (reason + " Not applicable to this case: its oracle expects "
                      "expected_terminal_class=%s, a refusal taken before any durable "
                      "transaction is formed; the refusal itself is still bound by "
                      "c2_case_terminal_not_evidenced and c11_no_terminal_receipt_or_followup."
                      % terminal_class)
            refs = [oracle_ref + "#semantic_comparison.expected_terminal_class"]
        entries.append(entry(cap_id, maximum, status, requirement_ref, reason, refs))
    identities = observed_identities(oracle_summary, native_evidence)
    matched = artifact_identity_matches(artifact, identities)
    cap_id, maximum, requirement_ref, reason = REPORT_CAP
    if not identities:
        entries.append(entry(cap_id, maximum, "unavailable", requirement_ref, reason, []))
    else:
        status = "not_violated" if len(matched) >= 2 else "violated"
        entries.append(entry(
            cap_id, maximum, status, requirement_ref,
            reason + " Observed identities: %d; quoted in the report: %d." % (len(identities), len(matched)),
            [str(artifact) if artifact is not None else oracle_ref,
             str(native_evidence) + "#state.responses", oracle_ref + "#semantic_comparison.runtime_facts"]))
    if not ISSUE_SCORE_CAPS:
        entries = [dict(item, status="unavailable", evidence_refs=[]) for item in entries]
    return {
        "schema_version": SCHEMA_VERSION,
        "case_id": case_id,
        "rubric_sha256": sha256_file(rubric),
        "native_evidence_sha256": sha256_file(native_evidence),
        "oracle_summary_sha256": sha256_file(oracle_summary),
        "issued_by": "evaluator/result_score_caps.py",
        "decided_from": "evaluator-observed Git refs, 160000 gitlinks, admission bytes, ledger "
                        "bytes, worktree listings and the append-only command execution record",
        "candidate_claims_never_enable_a_condition": True,
        "entries": entries,
    }


def effective_ceiling(contract: dict[str, Any]) -> int | None:
    applied = [item["maximum_score"] for item in contract.get("entries", [])
               if item.get("status") == "violated"]
    return min(applied) if applied else None


def write_contract(path: Path, contract: dict[str, Any]) -> Path:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(contract, indent=2, ensure_ascii=False, sort_keys=True) + "\n",
                    encoding="utf-8")
    return path
