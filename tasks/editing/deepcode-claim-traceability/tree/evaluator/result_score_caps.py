"""Evaluator-issued, evidence-bound Result ceilings for the hardened hidden cases.

The shared Result judge (``@@AGENTSWE_EDITING_CONTROL@@/result_judge.py``)
accepts an ``agentswe-result-score-caps/v1`` contract and binds it to the exact
rubric / native-evidence / oracle bytes it was given.  Determinate conditions are
decided here, by the task owner, from evaluator-computed evidence only; a
Candidate claim cannot enable one.  Conditions whose evidence is not fully
mechanical are emitted as ``semantic_review`` so the judge decides them from the
actual trajectory and reports the decision in ``ceiling_assessments`` -- which the
shared validator then enforces arithmetically.

Round 2 (0920-fh-001 follow-up) adds the conditions that separate a product which
really implements the Cycle 003 boundaries from one whose happy path merely looks
right: refusal receipts persisted by the product itself, coverage of the case's
reserved adversarial probes, ledger completeness, checkpoint-chain soundness and
promotion binding.  All of them are computed from the product's own stores, never
from files the driver wrote.

Every ceiling restates a published requirement (``input/02``, ``input/03``,
``evaluator/agentloop_result_rubric.md``); none of them is a new criterion.
"""
from __future__ import annotations

import hashlib
import json
from pathlib import Path

SCHEMA_VERSION = "agentswe-result-score-caps/v1"
ISSUER = "deepcode task owner, 0920 hardening round 2"
RUBRIC_REF = "evaluator/agentloop_result_rubric.md#evidence-bound-ceilings"


def _sha256(path: Path | str) -> str:
    digest = hashlib.sha256()
    digest.update(Path(path).read_bytes())
    return digest.hexdigest()


def _read(path: Path | str) -> dict:
    try:
        value = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return value if isinstance(value, dict) else {}


def _entry(cap_id, maximum, status, requirement_ref, reason, evidence_refs):
    return {"cap_id": cap_id, "maximum_score": int(maximum), "status": status,
            "requirement_ref": requirement_ref, "reason": reason,
            "evidence_refs": list(evidence_refs)}


def _determinate(entries, cap_id, maximum, ok, requirement_ref, ok_reason, bad_reason, refs, available=True):
    if not available:
        entries.append(_entry(cap_id, maximum, "unavailable", requirement_ref,
                              "The private oracle does not carry the evidence for this condition.", []))
    else:
        entries.append(_entry(cap_id, maximum, "not_violated" if ok else "violated",
                              requirement_ref, ok_reason if ok else bad_reason, refs))


def build_entries(*, oracle_summary, native_evidence) -> list[dict]:
    oracle = _read(oracle_summary)
    native = _read(native_evidence)
    checks = oracle.get("surface_assertion_comparisons")
    checks = checks if isinstance(checks, dict) else {}
    r2 = oracle.get("round_two_assertion_comparisons")
    r2 = r2 if isinstance(r2, dict) else {}
    graph_ids = oracle.get("capsule_graph_claim_ids")
    graph_ids = graph_ids if isinstance(graph_ids, dict) else {}
    findings = (native.get("artifact_validation") or {}).get("quality_schema_findings")
    findings = findings if isinstance(findings, list) else None
    oracle_ref = str(oracle_summary)
    native_ref = str(native_evidence)
    entries: list[dict] = []

    # C1 -- no revision-store and no execution-store behaviour at all.
    _determinate(
        entries, "c1_new_surface_state_absent", 20,
        bool(checks.get("revision_store_observed")) or bool(checks.get("execution_store_observed")),
        "input/03 requirements 3-12; " + RUBRIC_REF,
        "Persisted product state carries revision-store and/or execution-store records.",
        "No revision-store and no execution-store record appears in the product's own stores, so "
        "neither Cycle 003 surface produced observable behaviour.",
        [oracle_ref + "#surface_assertion_comparisons.revision_store_observed",
         oracle_ref + "#surface_assertion_comparisons.execution_store_observed"],
        available=bool(checks))

    # C4a -- a capsule graph exists but does not carry the case identifiers.
    _determinate(
        entries, "c4_capsule_graph_missing_case_ids", 45,
        all(bool(value) for value in graph_ids.values()),
        "input/02 (capsule identifiers) and input/03 requirement 18; " + RUBRIC_REF,
        "Every requested case identifier appears in the published capsule's own graph/spec.",
        "The published capsule's own paper_spec.json/traceability_graph.json does not carry these "
        "requested identifiers: " + ", ".join(sorted(k for k, v in graph_ids.items() if not v)) + ".",
        [oracle_ref + "#capsule_graph_claim_ids"], available=bool(graph_ids))

    # C5 -- the artifact is not in the product-documented agent-loop contract.
    _determinate(
        entries, "c5_artifact_contract_violation", 55, not findings,
        "input/02 (agent-loop result contract) and input/03 requirement 19; " + RUBRIC_REF,
        "The result artifact conforms to the documented agent-loop result contract.",
        "The evaluator's artifact validation reports these contract violations: "
        + "; ".join(str(item) for item in (findings or [])) + ".",
        [native_ref + "#artifact_validation.quality_schema_findings"], available=findings is not None)

    # C7 -- the product persists no refusal receipt of its own.
    receipts = oracle.get("persisted_operation_receipts")
    refusals = oracle.get("persisted_refusal_receipts")
    codes = oracle.get("persisted_refusal_error_codes")
    codes = codes if isinstance(codes, list) else []
    _determinate(
        entries, "c7_refusals_not_persisted", 28, bool(r2.get("refused_operations_persisted")),
        "input/02 (common operation envelope: operation-id reuse with a different body is "
        "OPERATION_CONFLICT) and input/03 requirement 14; " + RUBRIC_REF,
        "The product's own stores persist refusal receipts with stable error codes: "
        + ", ".join(str(code) for code in codes) + ".",
        "The product's own stores persist %s operation receipts and not one refusal. A refused "
        "operation that leaves no receipt cannot detect a later reuse of the same operation id with a "
        "different body, so operation-identity conflict detection is incomplete. Responses captured "
        "into files by the driver are its narration, not the product's record."
        % (receipts if isinstance(receipts, int) else "no"),
        [oracle_ref + "#persisted_refusal_receipts", oracle_ref + "#product_store_paths"],
        available=bool(r2))

    # C9 -- promotion is not backed by its own plan/approvals.
    _determinate(
        entries, "c9_promotion_binding_unsound", 25, bool(r2.get("promotion_binding_sound")),
        "input/02 (promote is an atomic compare-and-swap) and input/03 requirement 13; " + RUBRIC_REF,
        "Each recorded promotion agrees with the plan it names and with the standing approvals.",
        "A recorded promotion disagrees with its bound plan's revision digest or review generation, "
        "was recorded ahead of the revision's own review generation, or stands while a required role's "
        "decision was a rejection.",
        [oracle_ref + "#round_two_assertion_comparisons.promotion_binding_sound"],
        # 0921b B2: `None` == the product recorded no promotion anywhere.  That is not
        # evidence of an unsound promotion, so the ceiling is unavailable, not violated.
        available=bool(r2) and r2.get("promotion_binding_sound") is not None)

    # C10 -- the checkpoint chain is not an ordered, identified, digest-backed record.
    _determinate(
        entries, "c10_checkpoint_chain_unsound", 30, bool(r2.get("checkpoint_chain_sound")),
        "input/02 (one manifest command per ordered checkpoint) and input/03 requirements 9-11; "
        + RUBRIC_REF,
        "Checkpoints are uniquely ordered and each carries a command identity, exit code and output "
        "digest, and the completion proof reuses those checkpoint digests in order.",
        "No plan carries an ordered, duplicate-free checkpoint list in which every checkpoint records a "
        "command identity, an exit code and an output digest consistent with the completion proof.",
        [oracle_ref + "#round_two_assertion_comparisons.checkpoint_chain_sound"],
        # 123 G4: `None` == the product recorded no checkpoint chain and no completion proof at
        # all (semantic_oracle._checkpoint_chain_sound).  "Not recorded" is not "unsound", so the
        # ceiling is unavailable, not violated -- the same rule 0921b B2 set for c9 above.  The
        # maximum (30) and every recorded-chain rule are unchanged.
        available=bool(r2) and r2.get("checkpoint_chain_sound") is not None)

    # C11 -- the append-only ledger is incomplete or dishonest.
    ledger_ok = (bool(r2.get("ledger_covers_accepted_mutations"))
                 and bool(r2.get("ledger_excludes_refused_operations"))
                 and bool(r2.get("ledger_hashes_unique")))
    _determinate(
        entries, "c11_ledger_incomplete", 35, ledger_ok,
        "input/03 requirement 16 (append-only hash-chained audit ledger); " + RUBRIC_REF,
        "The ledger covers every accepted lifecycle mutation, logs no refused operation and carries "
        "unique event hashes.",
        "The ledger does not cover every accepted lifecycle mutation (%s events for %s accepted "
        "mutations), or logs a refused operation, or repeats an event hash."
        % (oracle.get("ledger_event_count"), oracle.get("accepted_mutation_estimate")),
        [oracle_ref + "#ledger_event_count", oracle_ref + "#accepted_mutation_estimate",
         oracle_ref + "#round_two_assertion_comparisons.ledger_covers_accepted_mutations"],
        available=bool(r2))

    # C12 -- graded: how many of the case's reserved adversarial probes actually
    # produced a product-persisted refusal receipt.  Full coverage lifts the cap
    # entirely; anything less holds the case near the presence-only band.
    probes = oracle.get("reserved_adversarial_probes")
    probes = probes if isinstance(probes, list) else []
    covered = oracle.get("reserved_probe_receipts_observed")
    covered = covered if isinstance(covered, list) else []
    if not probes:
        entries.append(_entry("c12_adversarial_probe_coverage", 100, "unavailable", RUBRIC_REF,
                              "This case declares no reserved adversarial probes.", []))
    else:
        complete = len(covered) == len(probes)
        entries.append(_entry(
            "c12_adversarial_probe_coverage",
            100 if complete else min(100, 20 + 5 * len(covered)),
            "not_violated" if complete else "violated",
            "test_cases/<case>/input.md adversarial probes; " + RUBRIC_REF,
            ("Every reserved adversarial probe produced a product-persisted refusal receipt: "
             + ", ".join(sorted(covered)) + "."
             if complete else
             "Only %d of the %d reserved adversarial probes produced a product-persisted refusal "
             "receipt (%s); missing: %s. A rollout that exercises the happy path and narrates the rest "
             "has not shown the boundaries hold."
             % (len(covered), len(probes), ", ".join(sorted(covered)) or "none",
                ", ".join(sorted(set(probes) - set(covered))))),
            [oracle_ref + "#reserved_adversarial_probes",
             oracle_ref + "#reserved_probe_receipts_observed"]))

    # Semantic-review conditions: the evidence is in the rollout, not in persisted state.
    required_ops = oracle.get("required_product_operations")
    required_ops = required_ops if isinstance(required_ops, list) else []
    boundaries = oracle.get("required_fail_closed_boundaries")
    boundaries = boundaries if isinstance(boundaries, list) else []
    entries.append(_entry(
        "c2_required_operation_not_executed", 28, "semantic_review",
        "test_cases/<case>/input.md numbered requirements; " + RUBRIC_REF,
        "Violated when any operation in required_product_operations was never executed as a real "
        "product command with an observable response: " + ", ".join(str(x) for x in required_ops) + ".",
        [oracle_ref + "#required_product_operations"]))
    entries.append(_entry(
        "c3_fail_closed_boundary_not_demonstrated", 30, "semantic_review",
        "test_cases/<case>/input.md numbered requirements; input/03 requirements 6-17; " + RUBRIC_REF,
        "Violated when any of these boundaries was not shown to be refused with a stable error.code "
        "and unchanged state: " + "; ".join(str(x) for x in boundaries) + ".",
        [oracle_ref + "#required_fail_closed_boundaries"]))
    entries.append(_entry(
        "c4b_audit_chain_not_demonstrated", 45, "semantic_review",
        "input/03 requirement 16; " + RUBRIC_REF,
        "Violated when the rollout did not obtain an ordered, linked, chain_valid scoped audit export "
        "from the product, or did not show the export fail closed against a tampered ledger (the "
        "evaluator observed audit_chain_linked=" + str(bool(checks.get("audit_chain_linked"))) + ").",
        [oracle_ref + "#surface_assertion_comparisons.audit_chain_linked"]))
    entries.append(_entry(
        "c6_scope_or_fence_violation_observed", 35, "semantic_review",
        "input/03 safety section; evaluator/rubric.md case-local ceiling; " + RUBRIC_REF,
        "Violated when cross-tenant or cross-project disclosure, a post-cancel or post-invalidation "
        "execution, or a publication by a fenced owner was observed.",
        [oracle_ref + "#expected_product_invariants"]))
    return entries


def build_contract(*, case_id, rubric, native_evidence, oracle_summary) -> dict:
    return {"schema_version": SCHEMA_VERSION, "case_id": case_id, "issued_by": ISSUER,
            "rubric_sha256": _sha256(rubric),
            "native_evidence_sha256": _sha256(native_evidence),
            "oracle_summary_sha256": _sha256(oracle_summary),
            "entries": build_entries(oracle_summary=oracle_summary, native_evidence=native_evidence)}


def degraded_contract(*, case_id, rubric, native_evidence, oracle_summary, reason) -> dict:
    """Fail open, never fail the run: no determinate ceiling, no semantic conditions."""
    return {"schema_version": SCHEMA_VERSION, "case_id": case_id, "issued_by": ISSUER,
            "rubric_sha256": _sha256(rubric),
            "native_evidence_sha256": _sha256(native_evidence),
            "oracle_summary_sha256": _sha256(oracle_summary),
            "entries": [_entry("c0_ceilings_unavailable", 100, "unavailable", RUBRIC_REF,
                               "Ceiling evidence could not be assembled: " + str(reason), [])]}


def write_contract(destination, *, case_id, rubric, native_evidence, oracle_summary) -> Path:
    destination = Path(destination)
    destination.parent.mkdir(parents=True, exist_ok=True)
    try:
        value = build_contract(case_id=case_id, rubric=rubric,
                               native_evidence=native_evidence, oracle_summary=oracle_summary)
    except Exception as exc:  # noqa: BLE001 - never fail the scoring run over a ceiling
        value = degraded_contract(case_id=case_id, rubric=rubric, native_evidence=native_evidence,
                                  oracle_summary=oracle_summary,
                                  reason=f"{type(exc).__name__}: {exc}")
    destination.write_text(json.dumps(value, sort_keys=True, indent=2) + "\n", encoding="utf-8")
    return destination
