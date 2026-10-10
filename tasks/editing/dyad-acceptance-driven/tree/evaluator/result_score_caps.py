#!/usr/bin/env python3
"""Evaluator-issued, evidence-bound task-local Result ceilings for Dyad.

The shared Result judge accepts an ``agentswe-result-score-caps/v1`` contract
whose ``rubric``/``native_evidence``/``oracle_summary`` digests must equal the
exact bytes it is about to read.  Every condition here is derived from the
private-oracle comparison and the native evidence that the run already
produced; nothing is inferred from Candidate or agent prose, and a condition
that cannot be determined is published as ``unavailable`` rather than guessed.

This module never dispatches a provider request and never mutates evidence.
"""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
from typing import Any

BINDING_FIELDS = ("app_id", "chat_id", "run_id", "session_id", "revision", "target_fingerprint")

# Absolute prefixes that only the evaluator owns.  A model-authored artifact
# that contains one of them has repeated evaluator-private layout back at us.
# Release adaptation (disclosed): the paper listed its hosts' two private roots
# and /run/secrets; a release install records its own private roots at setup
# (AGENTSWE_HOME, the .env and credential-file directories, the overlay
# directory, /run/secrets).  Without that record, AGENTSWE_HOME and
# /run/secrets still apply.
def _private_prefixes() -> tuple[str, ...]:
    record = Path("@@AGENTSWE_EDITING_STATE@@").parent / "private_roots.json"
    try:
        roots = json.loads(record.read_text(encoding="utf-8"))["roots"]
        if roots and all(isinstance(root, str) and root.startswith("/") for root in roots):
            return tuple(roots)
    except (OSError, ValueError, KeyError, TypeError):
        pass
    return (str(Path("@@AGENTSWE_ENVS@@").parent), "/run/secrets")


PRIVATE_PREFIXES = _private_prefixes()

CAP_DEFINITIONS = (
    (
        "acceptance_chain_incomplete", 20,
        "input/02_interface_and_delivery.md 'Preview session surface' and "
        "input/03_requirements_and_constraints.md items 2-4",
        "every required Acceptance product action must be observed, in order, "
        "with a usable typed product envelope",
    ),
    (
        "case_binding_unverified", 25,
        "input/02_interface_and_delivery.md 'Workspace revision' and hidden case prompts",
        "appId, chatId, runId, sessionId, workspace revision and target "
        "fingerprint must all come from product returns and agree between the "
        "model artifact and the native evidence",
    ),
    (
        "dispatch_integrity_failure", 30,
        "input/03_requirements_and_constraints.md item 2 and the run-local action protocol",
        "every product action must be model-selected and dispatched without "
        "evaluator defaulting, handler failure or generic fallback, and the "
        "final artifact must be authored by the model",
    ),
    (
        "artifact_trajectory_infidelity", 35,
        "input/02_interface_and_delivery.md 'What must be submitted' and the run-local action protocol",
        "the artifact's product_actions must be the exact ordered dispatch "
        "record and the executed-task identity must match",
    ),
    (
        "hard_capability_unproven", 45,
        "evaluator/hidden_test_inventory.md and input/03_requirements_and_constraints.md items 4-11",
        "every scenario check for this case's hard capability must hold in the "
        "observed product behaviour",
    ),
    (
        "product_contract_unmet", 28,
        "input/02_interface_and_delivery.md 'Acceptance target identity', "
        "'Content addressability of the workspace revision', 'Durable "
        "attestation surface' and input/03_requirements_and_constraints.md "
        "items 15-22",
        "the cross-cutting Acceptance contract must hold in observed product "
        "returns: the current target is published so a caller never guesses it, "
        "the accepted target survives into the session, testFingerprint is the "
        "admitted target bytes, the event sequence never decreases, terminal "
        "and attestation reads are byte stable, resultDigest is the published "
        "canonical digest, and a non-passing attestation never carries the run "
        "to passed",
    ),
    (
        "private_evidence_or_path_leak", 10,
        "input/03_requirements_and_constraints.md implementation constraints (disclosure)",
        "the rollout must not expose evaluator-private paths, oracle bytes or credentials",
    ),
)


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with Path(path).open("rb") as handle:
        while chunk := handle.read(1 << 20):
            digest.update(chunk)
    return digest.hexdigest()


def _read_json(path: Path | None) -> dict[str, Any] | None:
    if path is None:
        return None
    try:
        value = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return value if isinstance(value, dict) else None


def _tri(value: object) -> bool | None:
    """True/False for a determinate check, None when the check is absent."""
    return None if value is None else value is True


def _all_determinate(values: list[bool | None]) -> bool | None:
    if not values or any(item is None for item in values):
        return None
    return all(values)


def _assess(oracle: dict[str, Any] | None, native: dict[str, Any] | None,
            artifact_text: str | None) -> dict[str, tuple[bool | None, str, list[str]]]:
    checks = (oracle or {}).get("checks") if isinstance((oracle or {}).get("checks"), dict) else {}
    observations = (oracle or {}).get("required_action_observations")
    observations = observations if isinstance(observations, list) else []
    scenario_keys = sorted(key for key in checks if key.startswith("scenario:"))

    result: dict[str, tuple[bool | None, str, list[str]]] = {}

    # 1. acceptance chain completeness
    if oracle is None:
        result["acceptance_chain_incomplete"] = (
            None, "private-oracle comparison is unavailable", [])
    else:
        unusable = [str(item.get("action")) for item in observations
                    if isinstance(item, dict) and item.get("usable") is not True]
        ordered = _tri(checks.get("required_action_order"))
        usable = _tri(checks.get("required_action_observations_usable"))
        complete = _all_determinate([ordered, usable])
        result["acceptance_chain_incomplete"] = (
            None if complete is None else not complete,
            ("required action order met and every required observation usable"
             if complete else
             "unmet/unusable required product actions: " + (", ".join(unusable) or "order violated")),
            ["oracle_summary#/checks/required_action_order",
             "oracle_summary#/required_action_observations"],
        )

    # 2. case binding
    workspace = (native or {}).get("workspace") if isinstance((native or {}).get("workspace"), dict) else None
    if oracle is None and workspace is None:
        result["case_binding_unverified"] = (None, "no binding evidence is available", [])
    else:
        empty = [field for field in BINDING_FIELDS
                 if not str((workspace or {}).get(field) or "").strip()]
        matched = _tri(checks.get("binding_matches_native_evidence"))
        bound = None if (matched is None and workspace is None) else (
            bool(matched) and not empty and workspace is not None)
        result["case_binding_unverified"] = (
            None if bound is None else not bound,
            ("all six binding fields verified and identical in artifact and native evidence"
             if bound else
             "unverified binding fields: " + (", ".join(empty) or "artifact/native binding mismatch")),
            ["native_evidence#/workspace", "oracle_summary#/checks/binding_matches_native_evidence"],
        )

    # 3. dispatch integrity
    integrity = _all_determinate([
        _tri(checks.get("model_selected_actions_only")),
        _tri(checks.get("no_dispatch_validation_or_handler_failures")),
        _tri(checks.get("no_generic_single_flow_fallback")),
        _tri(checks.get("model_authored_artifact")),
    ])
    result["dispatch_integrity_failure"] = (
        None if integrity is None else not integrity,
        ("all product actions were model-selected and dispatched cleanly"
         if integrity else
         "dispatcher integrity checks failed: " + ", ".join(
             key for key in ("model_selected_actions_only",
                             "no_dispatch_validation_or_handler_failures",
                             "no_generic_single_flow_fallback",
                             "model_authored_artifact")
             if checks.get(key) is not True)),
        ["oracle_summary#/checks", "native_evidence#/product_action_trajectory"],
    )

    # 4. artifact/trajectory fidelity
    fidelity = _all_determinate([
        _tri(checks.get("artifact_binds_action_trajectory")),
        _tri(checks.get("executed_task")),
        _tri(checks.get("case_id")),
        _tri(checks.get("scenario_id")),
    ])
    result["artifact_trajectory_infidelity"] = (
        None if fidelity is None else not fidelity,
        ("artifact product_actions equal the dispatch record and the case/task identity matches"
         if fidelity else
         "artifact does not faithfully record the dispatch trajectory or executed-task identity"),
        ["oracle_summary#/checks/artifact_binds_action_trajectory",
         "oracle_summary#/observed_actions"],
    )

    # 5. hard capability
    if not scenario_keys:
        result["hard_capability_unproven"] = (
            None, "no scenario checks were recorded for this case", [])
    else:
        failed = [key for key in scenario_keys if checks.get(key) is not True]
        result["hard_capability_unproven"] = (
            bool(failed),
            ("every scenario check holds: " + ", ".join(scenario_keys)) if not failed
            else "unproven scenario checks: " + ", ".join(failed),
            ["oracle_summary#/checks", "native_evidence#/scenario_checks"],
        )

    # 6. cross-cutting product contract
    contract_keys = sorted(key for key in checks if key.startswith("contract:"))
    if not contract_keys:
        result["product_contract_unmet"] = (
            None, "no cross-cutting contract checks were recorded for this case", [])
    else:
        unmet = [key.split(":", 1)[1] for key in contract_keys if checks.get(key) is not True]
        result["product_contract_unmet"] = (
            bool(unmet),
            ("every contract obligation holds: "
             + ", ".join(key.split(":", 1)[1] for key in contract_keys)) if not unmet
            else "unmet contract obligations: " + ", ".join(unmet),
            ["oracle_summary#/checks", "native_evidence#/product_action_trajectory",
             "native_evidence#/read_stability_probes"],
        )

    # 7. privacy
    if artifact_text is None and native is None:
        result["private_evidence_or_path_leak"] = (None, "no artifact or native evidence to inspect", [])
    else:
        leaked = sorted({prefix for prefix in PRIVATE_PREFIXES if prefix in (artifact_text or "")})
        oracle_hidden = (native or {}).get("private_oracle_visible")
        visible = oracle_hidden is not False if native is not None else False
        leak = bool(leaked) or visible or checks.get("private_oracle_not_candidate_visible") is False
        result["private_evidence_or_path_leak"] = (
            leak,
            ("no evaluator-private path, oracle byte or credential is present in the rollout"
             if not leak else
             "evaluator-private material present: " + (", ".join(leaked) or "private oracle was visible")),
            ["agent_artifact", "native_evidence#/private_oracle_visible"],
        )
    return result


def cap_entries(oracle_summary: Path | None, native_evidence: Path | None,
                agent_artifact: Path | None) -> list[dict[str, Any]]:
    oracle = _read_json(oracle_summary)
    native = _read_json(native_evidence)
    try:
        artifact_text = Path(agent_artifact).read_text(encoding="utf-8") if agent_artifact else None
    except OSError:
        artifact_text = None
    assessed = _assess(oracle, native, artifact_text)
    entries: list[dict[str, Any]] = []
    for cap_id, maximum, requirement_ref, requirement in CAP_DEFINITIONS:
        violated, reason, refs = assessed.get(cap_id, (None, "condition was not evaluated", []))
        status = "unavailable" if violated is None else ("violated" if violated else "not_violated")
        entries.append({
            "cap_id": cap_id,
            "maximum_score": maximum,
            "status": status,
            "requirement_ref": requirement_ref,
            "reason": f"{requirement}; observed: {reason}",
            "evidence_refs": refs if status != "unavailable" else [],
        })
    return entries


def build_cap_contract(*, case_id: str, rubric: Path, native_evidence: Path,
                       oracle_summary: Path, agent_artifact: Path | None,
                       destination: Path) -> Path | None:
    """Write the ceiling contract bound to the exact judge inputs, or return None.

    The digests must be taken from the same bytes the judge will read, so this
    is called with the very paths that go on the judge command line.  Any
    problem is reported by returning ``None``: an unusable ceiling must never
    fail an otherwise valid scoring round.
    """
    try:
        rubric, native_evidence, oracle_summary = Path(rubric), Path(native_evidence), Path(oracle_summary)
        if not (rubric.is_file() and native_evidence.is_file() and oracle_summary.is_file()):
            return None
        contract = {
            "schema_version": "agentswe-result-score-caps/v1",
            "case_id": case_id,
            "rubric_sha256": sha256_file(rubric),
            "native_evidence_sha256": sha256_file(native_evidence),
            "oracle_summary_sha256": sha256_file(oracle_summary),
            "issued_by": "dyad evaluator/result_score_caps.py",
            "entries": cap_entries(oracle_summary, native_evidence,
                                   Path(agent_artifact) if agent_artifact else None),
        }
        if not contract["entries"]:
            return None
        destination = Path(destination)
        destination.parent.mkdir(parents=True, exist_ok=True)
        temporary = destination.with_suffix(destination.suffix + ".tmp")
        temporary.write_text(
            json.dumps(contract, indent=2, ensure_ascii=False, sort_keys=True) + "\n",
            encoding="utf-8")
        os.replace(temporary, destination)
        return destination
    except (OSError, ValueError, TypeError, KeyError):
        return None


def effective_ceiling(entries: list[dict[str, Any]]) -> int | None:
    applied = [entry["maximum_score"] for entry in entries if entry.get("status") == "violated"]
    return min(applied) if applied else None
