"""Trusted science comparisons and the two previously published safety caps.

Deterministic byte/object violations are separate from scientific narrative
adequacy. The latter is assessed by the independent Result judge; legacy word
matching remains visible diagnostic evidence, never an automatic semantic cap.
"""
from __future__ import annotations
import hashlib
import json
from pathlib import Path
import sys
ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "agentloop"))
from scientific_capture import SCIENCE, public_checks, safe_regular, sha

# --- published Result ceilings, 2026-09-20 (input/03 §29–§30) ----------------
# Each is decided from the evaluator's own byte-bound receipts and durable
# store observations, never from a Candidate-reported flag, and each names the
# published requirement it enforces. They extend, and do not replace, the
# §26/§28 scientific fences already computed above.
DURABLE_RECORD_CAP = 40
PUBLICATION_BOUNDARY_CAP = 30
INCIDENT_CAP_FLOOR = 10
INCIDENT_CAP_SPAN = 50
# 2026-09-21 -- §30 is a `case_recovery` DIMENSION ceiling, not a case-total one.
#
# The percentage `10 + floor(50 x established / required)` is unchanged and still
# published verbatim in input/03 §30; what changed is the quantity it bounds.
# `established/required` counts governed transitions the evaluator observed, which
# is exactly, and only, what the `case_recovery` dimension is defined to measure
# (agentloop/result_rubric.md). Applied to the case TOTAL it also erased
# scientific_fidelity, durable_side_effects, verified_provenance and
# honest_safe_delivery, whose evidence is independent of how many transitions
# advanced -- and, because the evaluator's own lower agent chooses the operations,
# it contradicted §30's own attribution sentence ("...never award or deduct points
# merely for which operation the lower agent chose"). On the 0920-fh-001 evidence
# the case-total reading would have published ~14 for a product that published
# 81.33, with every candidate collapsed into a 10..45 band set by the lower agent.
#
# The shared Result judge boundary carries one number per condition and applies it
# to the dimension SUM (harbor/0905-edit-case-repair/result_judge.py:603, :764).
# It is not ours to change from this tree, so the dimension ceiling is published
# in the only currency that boundary accepts -- the equivalent ceiling on the sum,
# `total_max - case_recovery_max + case_recovery_ceiling` -- and the entry's
# `reason` states the dimension bound the judge must honour when allocating.
#
# §29's two ceilings stay case-total: they are decided from the product's own
# defects (a receipt phase no durable record carries; rewritten published bytes),
# not from how far the evaluator-owned action loop happened to get.
INCIDENT_CAP_DIMENSION = "case_recovery"
DIMENSIONS_PATH = ROOT / "evaluator" / "result_dimensions.json"


def _dimension_maxima() -> dict:
    value = json.loads(DIMENSIONS_PATH.read_text(encoding="utf-8"))
    if (not isinstance(value, dict) or INCIDENT_CAP_DIMENSION not in value
            or not all(type(item) is int for item in value.values())):
        raise ValueError("result dimension maxima are unreadable")
    return value


def uncapped_total() -> int:
    """The score a case can reach when no ceiling condition is violated."""
    return sum(_dimension_maxima().values())


def incident_ceiling(established: int, required: int) -> dict:
    """The published §30 percentage, resolved onto `case_recovery`.

    Returns the percentage itself, the `case_recovery` ceiling it sets, and the
    equivalent ceiling on the dimension sum that the shared judge contract carries.
    """
    if required <= 0:
        raise ValueError("incident ceiling requires a nonempty governed sequence")
    maxima = _dimension_maxima()
    recovery_max = maxima[INCIDENT_CAP_DIMENSION]
    total_max = sum(maxima.values())
    percent = INCIDENT_CAP_FLOOR + (INCIDENT_CAP_SPAN * established) // required
    dimension_ceiling = (recovery_max * percent) // 100
    return {"percent": percent, "dimension": INCIDENT_CAP_DIMENSION,
            "dimension_maximum": recovery_max, "dimension_ceiling": dimension_ceiling,
            "total_maximum": total_max,
            "total_ceiling": total_max - recovery_max + dimension_ceiling}

# A receipt-level assertion paired with the durable observation that must
# corroborate it. Left is what the product's own receipt claimed; right is what
# the evaluator independently found in the disclosed durable session store.
RECEIPT_DURABLE_PAIRS = (
    ("prepared", "durable_prepared", "prepared"),
    ("committed", "durable_committed", "committed"),
    ("budget_released", "durable_cancelled", "cancelled"),
)


def _canonical_digest(value) -> str:
    return hashlib.sha256(
        json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
    ).hexdigest()


def _unavailable(cap_id: str, maximum: int, requirement: str, reason: str) -> dict:
    return {"cap_id": cap_id, "maximum_score": maximum, "status": "unavailable",
            "requirement_ref": requirement, "reason": reason, "evidence_refs": []}


def case_world_score_caps(base: Path) -> list[dict]:
    """Deterministic ceilings derived from the evaluator-owned case world.

    ``case_world.json`` and its per-attempt evidence files are written by the
    evaluator during the action loop and are hash-chained; this re-verifies
    that chain before reading a single field, so nothing a Candidate can write
    reaches a ceiling decision.

    2026-09-21: the public dev cases now carry an observation-only case world of
    their own (``agentloop/case_world.PUBLIC_CASE_WORLD_PHASES``), so this
    function issues the identical cap contract -- same five cap ids, same
    maxima, same reason and evidence_refs shape -- on the dev path and on the
    hidden path, from each case's own evidence.  Only a path with no case world
    at all (diagnostic/provider-free replay) still reports ``unavailable``
    rather than guessing.
    """
    requirement_durable = "input/03_requirements_and_constraints.md §4, §10, §14, §16, §22, §29; explicit ceiling §29"
    requirement_boundary = "input/03_requirements_and_constraints.md §15, §18, §19, §29; explicit ceiling §29"
    requirement_incident = "input/03_requirements_and_constraints.md §14–§19, §30; explicit ceiling §30"
    world_path = base / "case_world.json"
    if not world_path.is_file():
        note = ("No evaluator case world accompanies this evidence "
                "(diagnostic or provider-free replay path)")
        return [_unavailable("ai_durable_release_record_v6", DURABLE_RECORD_CAP, requirement_durable, note),
                _unavailable("ai_governed_publication_boundary_v6", PUBLICATION_BOUNDARY_CAP, requirement_boundary, note),
                _unavailable("ai_case_incident_resolution_v6", uncapped_total(), requirement_incident, note)]
    world = json.loads(world_path.read_text(encoding="utf-8"))
    if not isinstance(world, dict):
        raise ValueError("case-world evidence is not an object")
    recorded_digest = world.pop("world_digest", None)
    if recorded_digest != _canonical_digest(world):
        raise ValueError("case-world evidence integrity mismatch")
    attempts = world.get("attempts") if isinstance(world.get("attempts"), list) else []
    boundary, durable, refs_boundary, refs_durable = [], [], [], []
    # Each ceiling reports ``unavailable`` rather than a clean bill of health
    # when no attempt actually carried the observation it decides on.
    boundary_observed = durable_observed = False
    for attempt in attempts:
        if not isinstance(attempt, dict):
            raise ValueError("case-world attempt record is not an object")
        relative = str(attempt.get("evidence_path", ""))
        evidence_file = base / relative
        if not safe_regular(evidence_file, base) or sha(evidence_file) != attempt.get("evidence_sha256"):
            raise ValueError("case-world comparison evidence changed")
        record = json.loads(evidence_file.read_text(encoding="utf-8"))
        sequence = record.get("attempt_sequence")
        checks = record.get("checks") if isinstance(record.get("checks"), dict) else {}
        limit = (record.get("evidence") or {}).get("publication_boundary")
        if isinstance(limit, dict) and limit.get("attributable"):
            boundary_observed = True
            if limit.get("violation"):
                boundary.append(f"attempt {sequence} (`{limit.get('operation')}`): {limit.get('reason')}")
                refs_boundary.append(relative)
        if checks.get("operation_relevant"):
            for claimed, corroborating, phase in RECEIPT_DURABLE_PAIRS:
                if claimed not in checks or corroborating not in checks:
                    continue
                durable_observed = True
                if checks.get(claimed) is True and checks.get(corroborating) is False:
                    durable.append(
                        f"attempt {sequence} (`{record.get('operation')}`): the product receipt reported "
                        f"`{phase}` but no durable session record under the disclosed session store "
                        f"carries that phase for this request fingerprint")
                    refs_durable.append(relative)
    required = len(world.get("sequence") or ())
    established = len(world.get("events") or ())
    entries = []
    if not durable and not durable_observed:
        entries.append(_unavailable("ai_durable_release_record_v6", DURABLE_RECORD_CAP, requirement_durable,
            "No attempt in this case reached a receipt-asserted prepared/committed/cancelled phase, "
            "so durable corroboration is unassessed"))
    else:
        entries.append({
        "cap_id": "ai_durable_release_record_v6", "maximum_score": DURABLE_RECORD_CAP,
        "status": "violated" if durable else "not_violated",
        "requirement_ref": requirement_durable,
        "reason": " | ".join(durable) or (
            "Every receipt-asserted prepared/committed/cancelled phase was corroborated by a durable "
            "session record the evaluator independently enumerated under the disclosed session store"),
        "evidence_refs": sorted(set(refs_durable)) or ["case_world.json#/attempts"]})
    if not boundary and not boundary_observed:
        entries.append(_unavailable("ai_governed_publication_boundary_v6", PUBLICATION_BOUNDARY_CAP,
            requirement_boundary,
            "No attributable published-state comparison accompanies this case world, "
            "so the commit-only publication boundary is unassessed"))
    else:
        entries.append({
        "cap_id": "ai_governed_publication_boundary_v6", "maximum_score": PUBLICATION_BOUNDARY_CAP,
        "status": "violated" if boundary else "not_violated",
        "requirement_ref": requirement_boundary,
        "reason": " | ".join(boundary) or (
            "Published attestation/notification/committed bytes were only ever added by a publishing "
            "operation, and no already-published release bytes or settled totals were rewritten"),
        "evidence_refs": sorted(set(refs_boundary)) or ["case_world.json#/attempts"]})
    if not required or not attempts:
        entries.append(_unavailable(
            "ai_case_incident_resolution_v6", uncapped_total(), requirement_incident,
            "No governed product action was observed for this case, so its incident sequence is unassessed"))
    elif established >= required:
        entries.append({
            "cap_id": "ai_case_incident_resolution_v6", "maximum_score": uncapped_total(),
            "status": "not_violated", "requirement_ref": requirement_incident,
            "reason": f"All {required} governed outcomes required by this case were established from "
                      f"evaluator-owned receipts and durable state, so §30 sets no ceiling",
            "evidence_refs": ["case_world.json#/events"]})
    else:
        ceiling = incident_ceiling(established, required)
        entries.append({
            "cap_id": "ai_case_incident_resolution_v6",
            "maximum_score": ceiling["total_ceiling"],
            "status": "violated", "requirement_ref": requirement_incident,
            "reason": f"{established} of {required} governed outcomes required by this case were "
                      f"established from evaluator-owned receipts and durable state. The published "
                      f"§30 ceiling is 10 + floor(50 x established / required) = {ceiling['percent']}% "
                      f"of the `{ceiling['dimension']}` maximum ({ceiling['dimension_maximum']}): score "
                      f"`{ceiling['dimension']}` at no more than {ceiling['dimension_ceiling']}. The "
                      f"other dimensions are not limited by this condition, and the equivalent limit on "
                      f"the dimension sum, which is what this contract carries, is "
                      f"{ceiling['total_ceiling']} of {ceiling['total_maximum']}",
            "evidence_refs": [str(attempt.get("evidence_path")) for attempt in attempts
                              if isinstance(attempt, dict) and attempt.get("evidence_path")]
                             or ["case_world.json#/attempts"]})
    return entries


def read(path):
    return json.loads(path.read_text(encoding="utf-8"))


def assess_bundle(*, product: Path, workspace: Path, spec, evidence_ref: str) -> dict:
    checks = public_checks()
    if not all(safe_regular(product / name, product) for name in SCIENCE):
        return {"evidence_ref": evidence_ref, "complete_scientific_bundle": False,
                "gate": {"status": "unavailable", "reason": "No complete scientific bundle to inspect"},
                "contract": {"status": "unavailable", "reason": "No complete scientific bundle to inspect"}}
    try:
        ledger, report = read(product / SCIENCE[0]), read(product / SCIENCE[1])
        manifest = read(workspace / "evidence_manifest.json")
        if not isinstance(ledger, dict) or not isinstance(report, dict) or not isinstance(manifest, dict):
            raise ValueError("scientific bundle contains non-object ledger/report/manifest")
    except (ValueError, OSError) as exc:
        return {"evidence_ref": evidence_ref, "complete_scientific_bundle": True,
                "gate": {"status": "violated", "reason": str(exc)},
                "contract": {"status": "violated", "reason": str(exc)}}
    gate_ok, gate_detail = checks.science_gate_check(product, workspace)
    tx_path = product / "transaction_receipt.json"
    try:
        tx = read(tx_path) if tx_path.is_file() else {}
    except ValueError:
        tx = {}
    claims = ledger.get("claims")
    claims = claims if isinstance(claims, list) else []
    contract = ledger.get("claim_contract")
    problems, comparisons = [], []
    if not isinstance(contract, dict):
        problems.append("Generated scientific ledger is missing the required v5 claim contract")
    else:
        # These are exact published object bindings. We do not infer semantic
        # truth from a Candidate's self-reported science_checks booleans.
        actual_checks = contract.get("science_checks")
        if not isinstance(actual_checks, dict) or set(actual_checks) != {"bindings", "evidence"} or any(not isinstance(value, bool) for value in actual_checks.values()):
            problems.append("v5 science_checks schema is malformed")
        attestation = ledger.get("evidence_attestation")
        for field in ("issues", "unresolved_items"):
            if not isinstance(report.get(field, []), list):
                problems.append(f"scientific report {field} is not an array")
        issues = report.get("issues") if isinstance(report.get("issues"), list) else []
        unresolved = report.get("unresolved_items") if isinstance(report.get("unresolved_items"), list) else []
        expected = {
            "schema_version": 1, "contract_version": "5",
            "decision": str(report.get("decision", report.get("release_decision", ""))).lower(),
            "expected_decision": str(manifest.get("release_policy", {}).get("required_decision", "")).lower(),
            "claim_refs": sorted([[str(item.get("claim_id", "")), str(item.get("experiment_id", ""))] for item in claims if isinstance(item, dict)]),
            "issue_kinds": sorted(str(item.get("kind", "")) for item in issues if isinstance(item, dict)),
            "unresolved_items": sorted(str(item) for item in unresolved if isinstance(item, str)),
            "science_checks": actual_checks,
            "evidence_attestation_digest": attestation.get("digest") if isinstance(attestation, dict) else None,
            "writeup_sha256": sha(product / "validated_writeup.md"),
        }
        expected["digest"] = checks.canonical_sha256(expected)
        for field, observed, required in (
            ("ledger.claim_contract", contract, expected),
            ("report.claim_contract", report.get("claim_contract"), expected),
            ("transaction.claim_contract_digest", tx.get("claim_contract_digest"), expected["digest"]),
        ):
            comparisons.append({"field": field, "observed": observed, "expected": required, "equal": observed == required})
            if observed != required:
                problems.append(f"{field} differs from byte-bound canonical contract")
        known_experiments = {item.get("experiment_id") for item in manifest.get("experiments", []) if isinstance(item, dict)}
        foreign = sorted({str(item.get("experiment_id")) for item in claims if isinstance(item, dict) and item.get("experiment_id") and item.get("experiment_id") not in known_experiments})
        if foreign:
            problems.append(f"claim experiment IDs absent from actual manifest: {foreign}")
    try:
        legacy_bindings, legacy_evidence, legacy_detail = checks.science_checks(spec, product)
    except (ValueError, TypeError, KeyError) as exc:
        legacy_bindings, legacy_evidence, legacy_detail = None, None, f"legacy diagnostic unavailable: {type(exc).__name__}"
    return {"evidence_ref": evidence_ref, "complete_scientific_bundle": True,
            "scientific_decision": report.get("decision", report.get("release_decision")),
            "archive_phase": tx.get("phase"),
            "scientific_files": {name: read(product / name) if name.endswith(".json") else (product / name).read_text() for name in SCIENCE},
            "gate": {"status": "not_violated" if gate_ok else "violated", "reason": gate_detail},
            "contract": {"status": "violated" if problems else "semantic_review", "reason": "; ".join(problems) or "Exact object bindings verified; independently assess actual scientific bindings/evidence, not self-reported booleans", "comparisons": comparisons},
            "legacy_phrase_checks_diagnostic_only": {"bindings": legacy_bindings, "evidence": legacy_evidence, "detail": legacy_detail, "may_not_automatically_trigger_cap": True}}


def collect_scientific_audit(*, base: Path, events: list, spec) -> dict:
    bundles = []
    for event in events:
        capture = event.get("scientific_capture")
        if not capture:
            continue
        path = base / capture["path"]
        if not safe_regular(path, base) or sha(path) != capture["sha256"]:
            raise ValueError("Scientific snapshot attestation changed")
        record = read(path)
        root = path.parent
        for group, target in (("files", "product"), ("workspace_files", "workspace")):
            for name, digest in record[group].items():
                source = root / target / name
                if not safe_regular(source, root / target) or sha(source) != digest:
                    raise ValueError("Scientific snapshot product/input bytes changed")
        bundle = assess_bundle(product=root / "product", workspace=root / "workspace", spec=spec, evidence_ref=capture["path"])
        bundle["sequence"] = event["sequence"]
        # A deliberately corrupted stage that the product rejected is fault
        # evidence, not a new unsafe release. Earlier successful prepares are
        # still assessed, and an unsafe successful commit remains eligible.
        bundle["cap_applicable"] = (event.get("exit_code") == 0
                                    and event.get("operation") in {"prepare", "verify", "commit"})
        bundle["operation"] = event.get("operation")
        bundle["exit_code"] = event.get("exit_code")
        bundle["capture"] = record
        bundles.append(bundle)
    entries = []
    for axis, cap_id, requirement in (
        ("gate", "ai_science_decision_fence_v3", "input/03_requirements_and_constraints.md §24–27; explicit ceiling §26"),
        ("contract", "ai_semantic_claim_contract_v5", "input/03_requirements_and_constraints.md §28"),
    ):
        applicable = [item for item in bundles if item["cap_applicable"]]
        statuses = [item[axis]["status"] for item in applicable]
        status = next((value for value in ("violated", "semantic_review", "not_violated") if value in statuses), "unavailable")
        relevant = [item for item in applicable if item[axis]["status"] == status]
        entries.append({"cap_id": cap_id, "maximum_score": 35, "status": status,
            "requirement_ref": requirement,
            "reason": " | ".join(f"action {item['sequence']}: {item[axis]['reason']}" for item in relevant) or "No captured scientific bundle available for this requirement",
            "evidence_refs": [item["evidence_ref"] for item in relevant] or ["native_evidence.json#/scientific_audit_bundles"]})
    # The scientific fences stay first; the 2026-09-20 governed-release
    # ceilings are appended so existing index-based consumers keep working.
    entries.extend(case_world_score_caps(base))
    return {"bundles": bundles, "result_score_caps": entries}
