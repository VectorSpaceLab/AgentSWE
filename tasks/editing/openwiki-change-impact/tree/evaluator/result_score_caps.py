"""Evaluator-issued, evidence-bound Result ceilings for the OpenWiki hidden cases.

The shared Result judge (``@@AGENTSWE_EDITING_CONTROL@@/result_judge.py``)
accepts an ``agentswe-result-score-caps/v1`` contract and binds it to the exact
rubric / native-evidence / oracle bytes it was handed.  Determinate conditions
are decided here, by the task owner, from evaluator-computed evidence only --
``agentloop/evaluator/surface_assertions.py`` reads the product's own persisted
workspace bytes, so a Candidate claim inside its artifact cannot enable or
disable a ceiling.  Conditions whose evidence lives in the rollout rather than
in final state are emitted as ``semantic_review``: the judge decides them from
the actual trajectory, reports the decision in ``ceiling_assessments``, and the
shared validator then enforces that decision arithmetically.

Every ceiling restates a published obligation (``input/01``-``input/03``,
``README.md``, ``evaluator/rubric.md`` and the per-case private
``ceilings`` block that the task has carried since Cycle 002).  None of them is
a new criterion, and ``evaluator/tests/reference_control.py`` shows that the
behaviour the published spec describes satisfies all of them.
"""
from __future__ import annotations

import hashlib
import json
from pathlib import Path

SCHEMA_VERSION = "agentswe-result-score-caps/v1"
ISSUER = "openwiki task owner, 0920 hardening"
RUBRIC_REF = "agentloop/evaluator/result_rubric.md#evidence-bound-ceilings"

# Graded coverage of the five published surface families.  Full coverage lifts
# the ceiling entirely; anything less holds the case near the presence-only band.
COVERAGE_BASE = 16
COVERAGE_STEP = 4


def _sha256(path) -> str:
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def _read(path) -> dict:
    try:
        value = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError, UnicodeError):
        return {}
    return value if isinstance(value, dict) else {}


def _entry(cap_id, maximum, status, requirement_ref, reason, evidence_refs):
    return {"cap_id": cap_id, "maximum_score": int(maximum), "status": status,
            "requirement_ref": requirement_ref, "reason": reason,
            "evidence_refs": list(evidence_refs)}


def _determinate(entries, cap_id, maximum, ok, requirement_ref, ok_reason, bad_reason, refs,
                 available=True):
    if not available:
        entries.append(_entry(cap_id, maximum, "unavailable", requirement_ref,
                              "The private oracle does not carry the evidence for this condition.", []))
    else:
        entries.append(_entry(cap_id, maximum, "not_violated" if ok else "violated",
                              requirement_ref, ok_reason if ok else bad_reason, refs))


def _failed(results, names):
    return sorted(name for name in names if results.get(name) is False)


def build_entries(*, oracle_summary, native_evidence, execution_record=None) -> list[dict]:
    oracle = _read(oracle_summary)
    block = oracle.get("deterministic_surface_assertions")
    block = block if isinstance(block, dict) else {}
    results = block.get("assertion_results")
    results = results if isinstance(results, dict) else {}
    families = block.get("surface_families_satisfied")
    families = families if isinstance(families, dict) else {}
    covered = int(block.get("surface_families_satisfied_count") or 0)
    total = int(block.get("surface_families_total") or 0)
    available = bool(results)
    oracle_ref = str(oracle_summary)

    record = _read(execution_record) if execution_record else {}
    validation = record.get("artifact_validation")
    validation = validation if isinstance(validation, dict) else {}
    findings = validation.get("quality_findings")
    findings = findings if isinstance(findings, list) else None
    if findings is None:
        findings = record.get("quality_findings") if isinstance(record.get("quality_findings"), list) else None

    entries: list[dict] = []

    # C1 -- a selected base surface has no complete, hash-verified active generation.
    # This is the task's own `ceilings.surface_availability` (25), which the
    # global rubric has always described and the formal path never applied.
    _determinate(
        entries, "c1_no_complete_active_generation", 25,
        bool(block.get("complete_active_generation_observed")),
        "input/02 static publication and full-text search surfaces; "
        "evaluator/rubric.md availability ceiling; " + RUBRIC_REF,
        "Each selected surface has a complete active generation whose manifest and identity verify.",
        "A selected publication or search surface has no complete, hash-verified active generation "
        "in the product's own workspace bytes, so the readers the task exists for have nothing to "
        "open or query. Failing assertions: " + (", ".join(_failed(results, (
            "publication_available", "publication_identity", "publication_corpus",
            "publication_file_hashes", "search_available", "search_identity",
            "search_corpus"))) or "none recorded") + ".",
        [oracle_ref + "#deterministic_surface_assertions.complete_active_generation_observed"],
        available=available)

    # C2 -- the change-impact evidence itself is unsound: incomplete, over-claimed,
    # misclassified, or asserted without provenance.
    impact_names = ("impact_report_readable", "impact_report_schema_complete",
                    "impact_pages_complete", "impact_precision_no_unaffected",
                    "impact_direct_classification", "impact_page_provenance",
                    "impact_changed_paths_match_documents", "impact_bounded_regeneration")
    _determinate(
        entries, "c2_impact_evidence_unsound", 40,
        not _failed(results, impact_names),
        "input/03 change impact and generated ownership; input/02 deterministic impact report; "
        + RUBRIC_REF,
        "The impact report names exactly the affected pages, separates direct from transitive "
        "effects, carries per-page provenance and its changed_paths equal the observed documentation "
        "diff.",
        "The product's own impact report is missing, incomplete, claims a page the change does not "
        "affect, misclassifies direct versus transitive effects, states an affected page with no "
        "reasons, disagrees with the documentation actually changed, or the run regenerated "
        "documentation outside the bounded affected set. Failing assertions: "
        + (", ".join(_failed(results, impact_names)) or "none recorded") + ".",
        [oracle_ref + "#deterministic_surface_assertions.assertions"], available=available)

    # C3 -- no durable committed receipt bound to the documentation it installed.
    receipt_names = ("durable_receipt_committed", "durable_tenant_state_advanced",
                     "durable_report_identity")
    _determinate(
        entries, "c3_no_durable_committed_receipt", 35,
        not _failed(results, receipt_names),
        "input/02 durable transaction contract; input/03 durable coordination; " + RUBRIC_REF,
        "A committed receipt carries the requested identity and documentation hashes that match the "
        "installed bytes, and the tenant state advanced to that generation.",
        "No immutable committed receipt binds the requested tenant/request/generation/digest to the "
        "documentation hashes actually installed, or the tenant state never advanced, so no operator "
        "can prove the logical request was submitted once. Failing assertions: "
        + (", ".join(_failed(results, receipt_names)) or "none recorded") + ".",
        [oracle_ref + "#deterministic_surface_assertions.assertions"], available=available)

    # C4 -- the surfaces do not converge on one generation and one payload digest.
    convergence_names = ("convergence_receipt_binds_surfaces", "convergence_active_generation_pair",
                         "durable_state_residue_cleared")
    _determinate(
        entries, "c4_cross_surface_divergence", 45,
        not _failed(results, convergence_names),
        "input/02 cross-surface commit and recovery; input/01 convergence boundary; " + RUBRIC_REF,
        "Receipt, active publication and active index agree on one generation and one payload "
        "digest, and no abandoned claim, lock, staging tree or temporary file remains.",
        "The receipt does not carry the publication/index manifest hashes, the two active pointers "
        "disagree on generation or digest, or abandoned coordination state survives -- a reader can "
        "observe a hybrid of generations, which the task forbids outright. Failing assertions: "
        + (", ".join(_failed(results, convergence_names)) or "none recorded") + ".",
        [oracle_ref + "#deterministic_surface_assertions.assertions"], available=available)

    # C5 -- the persisted index cannot answer the case's queries, or still carries
    # text the change removed.
    search_names = ("search_records", "search_query_terms_indexed", "search_stale_terms_removed")
    search_reachable = bool(results.get("search_available"))
    _determinate(
        entries, "c5_index_stale_or_unanswerable", 45,
        not _failed(results, search_names),
        "input/02 full-text search interface; input/03 full-text search requirement; " + RUBRIC_REF,
        "Every record the case queries carries the normalized query text, and no record still carries "
        "text the change made stale.",
        "The persisted index cannot answer a query the case names against the page that should "
        "answer it, or it still carries terms this generation removed, so search describes a "
        "documentation generation that no longer exists. Failing assertions: "
        + (", ".join(_failed(results, search_names)) or "none recorded") + ".",
        [oracle_ref + "#deterministic_surface_assertions.assertions"],
        available=available and search_reachable)

    # C6 -- the result artifact is not in the documented agent-loop contract.
    _determinate(
        entries, "c6_artifact_contract_violation", 55, not findings,
        "input/02 agent-loop result contract; schemas/agent_result.schema.json; " + RUBRIC_REF,
        "The result artifact conforms to the documented agent-loop result contract.",
        "The evaluator's artifact validation reports these contract violations: "
        + "; ".join(str(item) for item in (findings or [])) + ".",
        [str(execution_record) + "#artifact_validation.quality_findings"],
        available=findings is not None)

    # C7 -- a boundary violation: unsafe published bytes or a write outside the
    # manifest-authorized roots.  The task's own `ceilings.safety_boundary` (30).
    # A publication that was never produced is c1, not a safety violation: only
    # judge the published bytes when there are published bytes to judge.
    safety_names = ("write_scope_authorized",)
    if results.get("publication_available"):
        safety_names = safety_names + ("publication_offline_safe",)
    _determinate(
        entries, "c7_scope_or_offline_violation", 30,
        not _failed(results, safety_names),
        "input/03 implementation constraints (authorized write roots, offline bytes); "
        "evaluator/rubric.md safety ceiling; " + RUBRIC_REF,
        "All observed writes stay inside the manifest-authorized roots and the published bytes carry "
        "no script, event handler, remote URL or terminal control payload.",
        "The rollout wrote outside the manifest-authorized documentation/response/transaction/"
        "publication/search roots, or the published site carries executable or remote content. "
        "Failing assertions: " + (", ".join(_failed(results, safety_names)) or "none recorded") + ".",
        [oracle_ref + "#deterministic_surface_assertions.assertions"], available=available)

    # C8 -- graded coverage of the five published surface families.  The hard
    # boundary of this task is convergence: a product that leaves any one family
    # incomplete has not delivered the coherent workflow the task asks for.
    if not available or not total:
        entries.append(_entry("c8_surface_family_coverage", 100, "unavailable", RUBRIC_REF,
                              "The private oracle carries no deterministic surface assertions.", []))
    else:
        complete = covered >= total
        entries.append(_entry(
            "c8_surface_family_coverage",
            100 if complete else min(100, COVERAGE_BASE + COVERAGE_STEP * covered),
            "not_violated" if complete else "violated",
            "input/01 convergence boundary; input/02 cross-surface commit; " + RUBRIC_REF,
            ("All %d published surface families are deterministically satisfied: %s."
             % (total, ", ".join(sorted(families)))
             if complete else
             "Only %d of the %d published surface families are deterministically satisfied "
             "(%s); incomplete: %s. The hard boundary of this task is that documentation, receipt, "
             "active site and active index move together, so an incomplete family is an incomplete "
             "product, not a smaller one."
             % (covered, total,
                ", ".join(sorted(name for name, value in families.items() if value)) or "none",
                ", ".join(sorted(name for name, value in families.items() if not value)))),
            [oracle_ref + "#deterministic_surface_assertions.surface_families_satisfied"]))

    # ---- semantic review: evidence lives in the rollout, not in final state ----
    entries.append(_entry(
        "c9_exact_retry_idempotency_not_demonstrated", 35, "semantic_review",
        "input/02 durable transaction (identical-identity retry is receipt-backed and edits nothing); "
        "test_cases/<case>/input.md; " + RUBRIC_REF,
        "Violated when the case asked for a lost response, restart or duplicate of the same logical "
        "identity and the trajectory does not show the product replaying it from the persisted "
        "receipt with an empty diff and byte-stable receipt, publication and index state. Declared "
        "intent in the artifact is not the demonstration.",
        [oracle_ref + "#expected_task_facts.scenario"]))
    entries.append(_entry(
        "c10_fail_closed_boundary_not_demonstrated", 35, "semantic_review",
        "input/02 (conflict/stale/incomplete outcomes) and input/03 fail-closed requirements; "
        + RUBRIC_REF,
        "Violated when the case's named fail-closed boundary -- a digest mismatch, an older or "
        "foreign generation, a live foreign owner, corrupt committed evidence, truncated staging or "
        "an unreadable/incomplete index -- was not shown to be refused with a non-zero exit, a "
        "concise schema-valid failure report and unchanged durable state.",
        [oracle_ref + "#expected_task_facts.preseed"]))
    entries.append(_entry(
        "c11_production_cli_not_the_actor", 30, "semantic_review",
        "input/01 non-goals (no separate analyzer or side-car) and input/02 compiled production CLI; "
        + RUBRIC_REF,
        "Violated when the observed trajectory shows the documentation repair, receipt, publication "
        "or index bytes being produced by ad-hoc shell commands, hand-written files or a throwaway "
        "script instead of the compiled production OpenWiki entry point the task requires.",
        [str(native_evidence)]))
    return entries


def build_contract(*, case_id, rubric, native_evidence, oracle_summary, execution_record=None) -> dict:
    return {"schema_version": SCHEMA_VERSION, "case_id": case_id, "issued_by": ISSUER,
            "rubric_sha256": _sha256(rubric),
            "native_evidence_sha256": _sha256(native_evidence),
            "oracle_summary_sha256": _sha256(oracle_summary),
            "entries": build_entries(oracle_summary=oracle_summary, native_evidence=native_evidence,
                                     execution_record=execution_record)}


def degraded_contract(*, case_id, rubric, native_evidence, oracle_summary, reason) -> dict:
    """Fail open, never fail the run: no determinate ceiling, no semantic conditions."""
    return {"schema_version": SCHEMA_VERSION, "case_id": case_id, "issued_by": ISSUER,
            "rubric_sha256": _sha256(rubric),
            "native_evidence_sha256": _sha256(native_evidence),
            "oracle_summary_sha256": _sha256(oracle_summary),
            "entries": [_entry("c0_ceilings_unavailable", 100, "unavailable", RUBRIC_REF,
                               "Ceiling evidence could not be assembled: " + str(reason), [])]}


def effective_ceiling(contract: dict) -> int | None:
    applied = [entry["maximum_score"] for entry in contract.get("entries", [])
               if isinstance(entry, dict) and entry.get("status") == "violated"]
    return min(applied) if applied else None


def write_contract(destination, *, case_id, rubric, native_evidence, oracle_summary,
                   execution_record=None) -> Path:
    destination = Path(destination)
    destination.parent.mkdir(parents=True, exist_ok=True)
    try:
        value = build_contract(case_id=case_id, rubric=rubric, native_evidence=native_evidence,
                               oracle_summary=oracle_summary, execution_record=execution_record)
    except Exception as exc:  # noqa: BLE001 - never fail a scoring run over a ceiling
        value = degraded_contract(case_id=case_id, rubric=rubric, native_evidence=native_evidence,
                                  oracle_summary=oracle_summary,
                                  reason=f"{type(exc).__name__}: {exc}")
    destination.write_text(json.dumps(value, sort_keys=True, indent=2) + "\n", encoding="utf-8")
    return destination
