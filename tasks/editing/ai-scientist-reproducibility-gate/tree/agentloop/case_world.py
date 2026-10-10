"""Evidence-gated evaluator-owned adversarial worlds for the lower action loop.

The world never chooses the lower model's primary operation and never creates a
release on its behalf.  It may inject a fault, change evaluator-owned request
context, or run an explicitly recorded adversarial sidecar probe through the
same Candidate launcher.  A transition advances only when operation-specific
product receipts, durable state, mutation proof, and (where required) sidecar
evidence establish its condition.
"""

from __future__ import annotations

import copy
import hashlib
import json
from pathlib import Path
from typing import Any

try:
    from .protocol import sha256_file, write_json
except ImportError:  # direct execution from the agentloop directory
    from protocol import sha256_file, write_json


RELEASE_ARTIFACTS = {
    "claim_ledger.json", "verification_report.json", "validated_writeup.md",
    "reproducibility_capsule.zip", "reproducibility_run.json",
    "transaction_receipt.json", "budget_receipt.json", "attestation.json",
    "notification_receipt.json",
}

CASE_WORLD_PHASES: dict[str, tuple[tuple[str, tuple[str, ...]], ...]] = {
    "test_001": (
        ("committed_response_lost", ("verify",)),
        ("response_reconciled", ("verify",)),
    ),
    "test_002": (
        ("concurrent_reservation_race", ("prepare",)),
        ("winner_commit_response_lost", ("commit",)),
        ("winner_reconciled", ("verify",)),
    ),
    "test_003": (
        ("prepared_capsule_corrupted", ("prepare",)),
        ("corruption_rejected", ("commit",)),
        ("higher_generation_recovered", ("verify",)),
    ),
    "test_004": (
        ("v1_prepared", ("prepare",)),
        ("policy_drift_rejected", ("commit",)),
        ("v1_cancelled", ("cancel",)),
        ("stale_request_fenced", ("commit",)),
        ("v2_migrated", ("verify",)),
    ),
    "test_005": (
        ("alpha_committed", ("verify",)),
        ("peer_project_isolated", ("status",)),
        ("unauthorized_peer_denied", ("status",)),
        ("notification_retry_reconciled", ("verify",)),
    ),
    "test_006": (
        ("initial_prepared", ("prepare",)),
        ("run_cancelled", ("cancel",)),
        ("stale_commit_rejected", ("commit",)),
        ("generation_reentry_response_lost", ("verify",)),
        ("response_artifact_repaired", ("verify",)),
    ),
}
# Only these two governed operations may publish. ``verify`` is the published
# atomic prepare-then-commit convenience form (input/02 lifecycle).
PUBLISHING_OPERATIONS = {"commit", "verify"}

CASE_WORLD_SEQUENCES = {
    case_id: tuple(name for name, _operations in phases)
    for case_id, phases in CASE_WORLD_PHASES.items()
}
# --- public dev case worlds, 2026-09-21 --------------------------------------
# Until now only the hidden cases had a case world, so every 29/30 ceiling in
# ``evaluator/scientific_audit.case_world_score_caps`` reported ``unavailable``
# on the public dev path and the Builder's only feedback channel was blind to
# the ceilings that decide the hidden score (0921-v4-001: dev mean 93.5 against
# hidden mean 61.6, with `case_recovery` pinned exactly at the published 30 cap
# in four of five judged hidden cases).
#
# These worlds are OBSERVATION-ONLY.  They name the one governed outcome both
# published dev tasks already require -- completing the governed audit archive
# -- and reuse the existing ``alpha_committed`` transition, which injects no
# peer, corrupts nothing, removes nothing and rewrites no product context
# (``prepare_action`` has no branch for these case ids and ``_evaluate_transition``
# performs no mutation for it).  What they add is exactly the evidence the caps
# are computed from: per-attempt receipt/durable corroboration checks and the
# publication-boundary snapshot, the same ones the hidden path produces.
#
# ``CASE_WORLD_PHASES`` and ``CASE_WORLD_SEQUENCES`` deliberately keep their
# hidden-only contents so every existing consumer (semantic_finalize.py:226,
# evaluator/tests) reads exactly what it read before.
PUBLIC_CASE_WORLD_PHASES: dict[str, tuple[tuple[str, tuple[str, ...]], ...]] = {
    "dev_001": (("alpha_committed", ("commit",)),),
    "dev_002": (("alpha_committed", ("commit",)),),
}
PUBLIC_CASE_WORLD_SEQUENCES = {
    case_id: tuple(name for name, _operations in phases)
    for case_id, phases in PUBLIC_CASE_WORLD_PHASES.items()
}


def case_world_phases(case_id: str):
    """The phase script for a case -- hidden first, then public dev -- or None."""
    if case_id in CASE_WORLD_PHASES:
        return CASE_WORLD_PHASES[case_id]
    return PUBLIC_CASE_WORLD_PHASES.get(case_id)


def case_world_sequence(case_id: str) -> tuple[str, ...]:
    if case_id in CASE_WORLD_SEQUENCES:
        return CASE_WORLD_SEQUENCES[case_id]
    return PUBLIC_CASE_WORLD_SEQUENCES[case_id]


def has_case_world(case_id: str) -> bool:
    return case_world_phases(case_id) is not None


def equivalent_operations(operations: tuple[str, ...]) -> tuple[str, ...]:
    """Widen a phase's scripted verbs by the equivalence the task publishes.

    ``input/02_interface_and_delivery.md`` line 48 states that ``verify`` is the atomic
    prepare-then-commit form, and ``PUBLISHING_OPERATIONS`` above repeats it.  The model
    is never told which verb this phase expects (``next_projection`` withholds it and the
    launcher offers the whole set), so a phase that asks for ``prepare`` or ``commit``
    must also admit the published superset ``verify``.  The converse is not true: a phase
    that asks for ``verify`` wants both halves, so a bare ``prepare``/``commit`` is not
    accepted for it.  This only opens the gate; ``_evaluate_transition`` still decides
    whether the phase actually advanced.
    """
    widened = list(operations)
    if any(name in {"prepare", "commit"} for name in operations) and "verify" not in widened:
        widened.append("verify")
    return tuple(widened)


def phase_operations(case_id: str, transition: str) -> tuple[str, ...]:
    for name, operations in (case_world_phases(case_id) or ()):
        if name == transition:
            return equivalent_operations(operations)
    return ()


SESSION_SCAN_MAX_DEPTH = 6
SESSION_SCAN_MAX_FILES = 512
# ``committed`` holds published release copies and ``stages`` holds staged
# capsule bytes; neither is a durable session record, so the session scan does
# not descend into them.
NON_SESSION_DIRECTORIES = {"committed", "stages"}


def bounded_json_files(root: Path, *, max_depth: int = SESSION_SCAN_MAX_DEPTH,
                       max_files: int = SESSION_SCAN_MAX_FILES,
                       skip_directories: frozenset[str] = frozenset()) -> list[Path]:
    """Discover durable JSON records under a disclosed store directory.

    The published CLI contract names a durable *directory* (``--session-store``,
    ``--budget-store``, ``--attestation-store``, ``--notification-store``); it
    does not prescribe one internal layout.  A product may legitimately shard a
    store (``sessions/<verification>/<request>/state.json``) instead of writing
    flat ``<fingerprint>.json`` files, so the evaluator enumerates the store
    rather than one hardcoded depth.  The walk is bounded, never follows a
    symlink, and is an observation of actual store bytes, not a score proxy.
    """
    if not root.is_dir():
        return []
    found: list[Path] = []
    stack: list[tuple[Path, int]] = [(root, 0)]
    while stack and len(found) < max_files:
        directory, depth = stack.pop()
        try:
            entries = sorted(directory.iterdir())
        except OSError:
            continue
        for entry in entries:
            if entry.is_symlink():
                continue
            if entry.is_dir():
                if depth < max_depth and entry.name not in skip_directories:
                    stack.append((entry, depth + 1))
            elif entry.is_file() and entry.suffix == ".json":
                found.append(entry)
                if len(found) >= max_files:
                    break
    return sorted(found)


def canonical_digest(value: Any) -> str:
    return hashlib.sha256(
        json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
    ).hexdigest()


class CaseWorld:
    """Track attempts separately from evidence-established transitions."""

    def __init__(self, case_id: str, workspace: Path, output: Path, context: dict[str, Any]) -> None:
        if not has_case_world(case_id):
            raise ValueError(f"unsupported evaluator case world: {case_id}")
        self.case_id = case_id
        self.workspace = workspace
        self.output = output
        self.context = context
        self.original_context = copy.deepcopy(context)
        self.phases = case_world_phases(case_id)
        self.applied: list[dict[str, Any]] = []
        self.attempts: list[dict[str, Any]] = []
        self.baseline: dict[str, Any] = {}
        self.initial_history: dict[str, Any] | None = None

    @property
    def complete(self) -> bool:
        return len(self.applied) == len(self.phases)

    @property
    def transition(self) -> str | None:
        return None if self.complete else self.phases[len(self.applied)][0]

    @property
    def sequence(self) -> tuple[str, ...]:
        """Return the immutable canonical transition sequence for this case."""
        return case_world_sequence(self.case_id)

    @property
    def relevant_operations(self) -> tuple[str, ...]:
        return () if self.complete else equivalent_operations(self.phases[len(self.applied)][1])

    def next_projection(self) -> dict[str, Any]:
        # Describe the current user-visible incident, not the evaluator's
        # expected operation sequence or pass/fail checks. The model must
        # decide its next operation from this context and product receipts.
        incidents = {
            "committed_response_lost": "Deliver the requested scientific release. Its delivery channel may lose a response.",
            "response_reconciled": "The client is missing release response artifacts; durable state may already exist.",
            "concurrent_reservation_race": "Another worker is requesting capacity from the same limited project budget.",
            "winner_commit_response_lost": "Continue the worker whose reservation succeeded; delivery can be interrupted.",
            "winner_reconciled": "The winning worker's delivery acknowledgement is missing.",
            "prepared_capsule_corrupted": "Stage the release against the current scientific evidence and budget.",
            "corruption_rejected": "The staged capsule's storage integrity is suspect. Do not publish unverified bytes.",
            "higher_generation_recovered": "A successor worker has taken responsibility for the damaged release.",
            "v1_prepared": "The release is awaiting publication under the currently active policy.",
            "policy_drift_rejected": "The project owner changed policy after staging; the pending release still has its old identity.",
            "v1_cancelled": "The project owner withdrew the old-policy publication request.",
            "stale_request_fenced": "A delayed old-policy publication request has arrived after withdrawal.",
            "v2_migrated": "A new release request is authorized under the replacement policy.",
            "alpha_committed": "Publish this project's requested scientific release.",
            "peer_project_isolated": "A peer project has activity in the shared service; inspect only the current project's release.",
            "unauthorized_peer_denied": "An untrusted actor is attempting to inspect the release.",
            "notification_retry_reconciled": "The authorized owner resumed a release whose notification may already have been delivered.",
            "initial_prepared": "Prepare the requested release without prematurely publishing it.",
            "run_cancelled": "The owner withdrew the pending release request.",
            "stale_commit_rejected": "A delayed publication attempt from the withdrawn request is pending.",
            "generation_reentry_response_lost": "A successor has a new authorized request for the same scientific evidence.",
            "response_artifact_repaired": "Delivery is missing response artifacts after the successor's publication attempt.",
        }
        return {
            "case_id": self.case_id,
            "current_incident": incidents.get(self.transition, "No further incident is pending; report the observed outcome honestly."),
            "notification_sequence": len(self.applied) + 1,
            **({"existing_history": "A prior committed release in this project and a peer project already exist; preserve their published bytes and previously settled usage."} if self.initial_history else {}),
        }

    def prepare_action(self, operation: str) -> dict[str, Any]:
        """Apply only evaluator-owned setup for a relevant attempted phase."""
        transition = self.transition
        plan: dict[str, Any] = {
            "transition": transition,
            "operation": operation,
            "operation_relevant": operation in self.relevant_operations,
            "context_before": self._context_projection(self.context),
            "probe": None,
            "protected_state_before": self._protected_state_snapshot(),
        }
        if not plan["operation_relevant"] or transition is None:
            plan["context_after_setup"] = self._context_projection(self.context)
            return plan

        if self.case_id == "test_002" and transition == "concurrent_reservation_race":
            peer = copy.deepcopy(self.original_context)
            peer.update({
                "verification_id": f"{self.original_context['verification_id']}-peer",
                "request_id": "race-two",
                "owner_id": "worker-two",
            })
            plan["probe"] = {"timing": "concurrent", "operation": "prepare", "context": peer}
        elif self.case_id == "test_003" and transition == "higher_generation_recovered":
            old_generation = int(self.original_context.get("generation", 0))
            self.context.update({
                "request_id": "request-t2",
                "owner_id": "worker-new",
                "generation": old_generation + 1,
                "takeover_generation": old_generation,
            })
        elif self.case_id == "test_004":
            if transition == "policy_drift_rejected":
                self.context.update({
                    "run_manifest": "run_manifest_v2.json",
                    "capsule_policy": "capsule_policy_v2.json",
                    "budget_policy": "budget_policy_v2.json",
                })
            elif transition in {"v1_cancelled", "stale_request_fenced"}:
                self.context.clear(); self.context.update(copy.deepcopy(self.original_context))
            elif transition == "v2_migrated":
                self.context.clear(); self.context.update(copy.deepcopy(self.original_context))
                self.context.update({
                    "verification_id": "release-u4-new",
                    "request_id": "policy-new",
                    "run_manifest": "run_manifest_v2.json",
                    "capsule_policy": "capsule_policy_v2.json",
                    "budget_policy": "budget_policy_v2.json",
                })
        elif self.case_id == "test_005":
            if transition == "peer_project_isolated":
                beta = copy.deepcopy(self.original_context)
                beta.update({
                    "tenant_id": "tenant-beta",
                    "project_id": "project-beta",
                    "run_manifest": "run_manifest_beta.json",
                    "capsule_policy": "capsule_policy_beta.json",
                    "usage_statement": "usage_beta.json",
                    "budget_policy": "budget_policy_beta.json",
                    "attestation_policy": "attestation_policy_beta.json",
                    "notification_policy": "notification_policy_beta.json",
                })
                plan["probe"] = {"timing": "before", "operation": "verify", "context": beta}
                self.context.clear(); self.context.update(copy.deepcopy(self.original_context))
            elif transition == "unauthorized_peer_denied":
                self.context.clear(); self.context.update(copy.deepcopy(self.original_context))
                self.context["owner_id"] = "unauthorized-actor"
            elif transition == "notification_retry_reconciled":
                self.context.clear(); self.context.update(copy.deepcopy(self.original_context))
        elif self.case_id == "test_006" and transition == "generation_reentry_response_lost":
            old_generation = int(self.original_context.get("generation", 0))
            self.context.clear(); self.context.update(copy.deepcopy(self.original_context))
            self.context.update({
                "request_id": "release-second",
                "owner_id": "worker-release",
                "generation": old_generation + 1,
                "takeover_generation": old_generation,
            })
        plan["context_after_setup"] = self._context_projection(self.context)
        return plan

    def observe_action(
        self,
        *,
        action_sequence: int,
        operation: str,
        product_event: dict[str, Any],
        plan: dict[str, Any],
        probe_event: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        transition = str(plan.get("transition") or "complete")
        checks: dict[str, bool] = {"operation_relevant": bool(plan.get("operation_relevant"))}
        mutation: dict[str, Any] = {"kind": "none", "applied": False}
        evidence: dict[str, Any] = {
            "product_observation_digest": product_event.get("observation_digest"),
            "product_exit_code": product_event.get("exit_code"),
            "receipt_projection": product_event.get("observation", {}).get("receipt_projections", {}),
            "release_artifact_hashes": product_event.get("observation", {}).get("release_artifact_hashes", {}),
            "durable_state": self._durable_state_projection(),
            "probe_observation_digest": probe_event.get("observation_digest") if isinstance(probe_event, dict) else None,
            "probe_exit_code": probe_event.get("exit_code") if isinstance(probe_event, dict) else None,
            "protected_state_timeline": {"before_operation": plan.get("protected_state_before"), "after_operation": self._protected_state_snapshot(), "operation": operation},
        }
        evidence["publication_boundary"] = self._publication_boundary(
            operation=operation,
            before=plan.get("protected_state_before"),
            after=evidence["protected_state_timeline"]["after_operation"],
            probe=plan.get("probe") if isinstance(plan, dict) else None,
        )
        if checks["operation_relevant"]:
            mutation, transition_checks, extra = self._evaluate_transition(
                transition, product_event, probe_event, plan
            )
            checks.update(transition_checks)
            evidence.update(extra)
        advanced = bool(checks and all(checks.values()))
        evidence_payload = {
            "schema_version": "agentswe-ai-scientist-case-world-evidence-v2",
            "case_id": self.case_id,
            "attempt_sequence": len(self.attempts) + 1,
            "action_sequence": action_sequence,
            "transition": transition,
            "operation": operation,
            "allowed_operations": list(self.relevant_operations),
            "advanced": advanced,
            "checks": checks,
            "setup": plan,
            "mutation": mutation,
            "evidence": evidence,
        }
        evidence_rel = Path("case_world_evidence") / f"attempt_{len(self.attempts) + 1:03d}.json"
        evidence_path = self.output / evidence_rel
        write_json(evidence_path, evidence_payload)
        attempt = {
            "attempt_sequence": len(self.attempts) + 1,
            "action_sequence": action_sequence,
            "transition": transition,
            "triggered_by_model_operation": operation,
            "allowed_operations": list(self.relevant_operations),
            "evaluator_owned": True,
            "advanced": advanced,
            "checks": checks,
            "evidence_path": evidence_rel.as_posix(),
            "evidence_sha256": sha256_file(evidence_path),
        }
        self.attempts.append(attempt)
        if advanced:
            self.applied.append(attempt)
        return attempt

    def persist(self) -> str:
        payload = {
            "schema_version": "agentswe-ai-scientist-case-world-v2",
            "case_id": self.case_id,
            "sequence": list(self.sequence),
            "events": self.applied,
            "attempts": self.attempts,
            "event_count": len(self.applied),
            "attempt_count": len(self.attempts),
            "complete": self.complete,
            "initial_history": self.initial_history,
        }
        digest = canonical_digest(payload)
        payload["world_digest"] = digest
        write_json(self.output / "case_world.json", payload)
        return digest

    def _evaluate_transition(
        self,
        transition: str,
        event: dict[str, Any],
        probe: dict[str, Any] | None,
        plan: dict[str, Any],
    ) -> tuple[dict[str, Any], dict[str, bool], dict[str, Any]]:
        phase = self._phase(event)
        budget = self._budget_status(event)
        error = self._error_code(event)
        hashes = event.get("observation", {}).get("release_artifact_hashes", {})
        complete_release = RELEASE_ARTIFACTS.issubset(set(hashes))
        checks: dict[str, bool] = {}
        extra: dict[str, Any] = {}
        mutation: dict[str, Any] = {"kind": "none", "applied": False}

        if transition == "committed_response_lost":
            checks.update(product_succeeded=event.get("exit_code") == 0, committed=phase == "committed", settled=budget == "settled", complete_release=complete_release, durable_committed=self._has_durable_phase("committed"))
            if all(checks.values()):
                mutation = self._remove_response_artifacts(event)
                checks["response_artifacts_removed"] = mutation.get("applied") is True
        elif transition in {"response_reconciled", "winner_reconciled", "response_artifact_repaired"}:
            # An exact retry restores the committed bytes from durable state.
            # It must not add a second settlement, so the project's settled
            # total is compared with the one observed when the response copies
            # were removed (input/02 exact-retry recovery, input/03 §16).
            baseline_settled = (self.baseline.get("response_loss") or {}).get("settled_micros")
            observed_settled = self._protected_state_snapshot()["project_settled_micros"]
            checks.update(product_succeeded=event.get("exit_code") == 0, committed=phase == "committed", settled=budget == "settled", complete_release=complete_release, durable_committed=self._has_durable_phase("committed"), stable_repair=self._matches_baseline(event), no_additional_settlement=baseline_settled is not None and observed_settled == baseline_settled)
            extra["settlement_idempotency"] = {"settled_micros_when_response_lost": baseline_settled, "settled_micros_after_recovery": observed_settled}
        elif transition == "concurrent_reservation_race":
            primary_ok = event.get("exit_code") == 0 and phase == "prepared" and budget == "reserved"
            probe_ok = isinstance(probe, dict) and probe.get("exit_code") == 0 and self._phase(probe) == "prepared" and self._budget_status(probe) == "reserved"
            primary_rejected = event.get("exit_code") not in {None, 0} and self._has_structured_error(event)
            probe_rejected = isinstance(probe, dict) and probe.get("exit_code") not in {None, 0} and self._has_structured_error(probe)
            primary_times = event.get("observation", {})
            peer_times = (probe or {}).get("observation", {})
            overlap = max(primary_times.get("started_ns", 0), peer_times.get("started_ns", 0)) < min(primary_times.get("ended_ns", 0), peer_times.get("ended_ns", 0))
            checks.update(probe_executed=isinstance(probe, dict), concurrent_process_intervals_overlap=overlap, exactly_one_winner=bool(primary_ok ^ probe_ok), exactly_one_budget_rejection=bool(primary_rejected ^ probe_rejected), durable_prepared=self._has_durable_phase("prepared"))
            if all(checks.values()):
                # Snapshot before clearing: ``winner_context = self.context``
                # aliases the mutable primary context, so clearing it first
                # would erase the winner and leave later product arguments
                # empty.  The winner is evaluator-selected only after the
                # two substantive product observations establish the race.
                winner_context = copy.deepcopy(
                    self.context if primary_ok else (plan.get("probe") or {}).get("context")
                )
                self.context.clear(); self.context.update(copy.deepcopy(winner_context))
                extra["winner"] = "primary" if primary_ok else "evaluator_concurrent_probe"
        elif transition == "winner_commit_response_lost":
            checks.update(product_succeeded=event.get("exit_code") == 0, committed=phase == "committed", settled=budget == "settled", complete_release=complete_release, durable_committed=self._has_durable_phase("committed"))
            if all(checks.values()):
                mutation = self._remove_response_artifacts(event)
                checks["response_artifacts_removed"] = mutation.get("applied") is True
        elif transition == "prepared_capsule_corrupted":
            checks.update(product_succeeded=event.get("exit_code") == 0, prepared=phase == "prepared", reserved=budget == "reserved", durable_prepared=self._has_durable_phase("prepared"))
            if all(checks.values()):
                mutation = self._corrupt_staged_capsule()
                checks["staged_capsule_digest_changed"] = mutation.get("applied") is True
        elif transition == "corruption_rejected":
            checks.update(product_rejected=event.get("exit_code") not in {None, 0}, structured_integrity_error=self._has_structured_error(event), durable_reservation_preserved=self._has_durable_phase("prepared"), no_complete_release=not complete_release)
        elif transition == "higher_generation_recovered":
            checks.update(product_succeeded=event.get("exit_code") == 0, committed=phase == "committed", settled=budget == "settled", complete_release=complete_release, durable_committed=self._has_durable_phase("committed"), takeover_passed=self.context.get("takeover_generation") == self.original_context.get("generation"))
        elif transition == "v1_prepared":
            checks.update(product_succeeded=event.get("exit_code") == 0, prepared=phase == "prepared", reserved=budget == "reserved", durable_prepared=self._has_durable_phase("prepared"), policy_v1=self.context.get("budget_policy") == "budget_policy_v1.json")
        elif transition == "policy_drift_rejected":
            checks.update(product_rejected=event.get("exit_code") not in {None, 0}, structured_policy_conflict=self._has_structured_error(event), durable_v1_preserved=self._has_durable_phase("prepared"), policy_v2_attempted=self.context.get("budget_policy") == "budget_policy_v2.json")
        elif transition == "v1_cancelled":
            checks.update(product_succeeded=event.get("exit_code") == 0, budget_released=budget == "cancelled", durable_cancelled=self._has_durable_phase("cancelled"))
            extra["transaction_phase_comparison"] = {"expected_semantics": "current request cancelled without reversing a committed release", "observed_receipt_phase": phase, "observed_durable_cancelled": self._has_durable_phase("cancelled"), "receipt_phase_is_not_a_published_cancel_field_requirement": True}
        elif transition == "stale_request_fenced":
            checks.update(product_rejected=event.get("exit_code") not in {None, 0}, structured_stale_error=self._has_structured_error(event), durable_cancelled=self._has_durable_phase("cancelled"), no_complete_release=not complete_release)
        elif transition == "v2_migrated":
            expected_policy = self._policy_version("budget_policy_v2.json")
            receipt = self._receipt(event, "budget_receipt.json")
            checks.update(product_succeeded=event.get("exit_code") == 0, committed=phase == "committed", settled=budget == "settled", complete_release=complete_release, durable_committed=self._has_durable_phase("committed"), policy_v2_bound=bool(expected_policy and receipt.get("policy_version") == expected_policy))
        elif transition == "alpha_committed":
            checks.update(product_succeeded=event.get("exit_code") == 0, committed=phase == "committed", settled=budget == "settled", complete_release=complete_release, durable_committed=self._has_durable_phase("committed"))
            if all(checks.values()):
                self.baseline["alpha"] = self._identity_baseline(event)
        elif transition == "peer_project_isolated":
            primary_corpus = json.dumps(event.get("observation", {}), sort_keys=True).lower()
            durable_projects = set(self._durable_projects())
            checks.update(peer_probe_committed=isinstance(probe, dict) and probe.get("exit_code") == 0 and self._phase(probe) == "committed", alpha_status_succeeded=event.get("exit_code") == 0 and phase == "committed", both_projects_durable={"project-alpha", "project-beta"}.issubset(durable_projects), peer_not_disclosed="project-beta" not in primary_corpus and "tenant-beta" not in primary_corpus)
        elif transition == "unauthorized_peer_denied":
            corpus = json.dumps(event.get("observation", {}), sort_keys=True).lower()
            checks.update(product_rejected=event.get("exit_code") not in {None, 0}, structured_unauthorized=error == "unauthorized", peer_not_disclosed="project-beta" not in corpus and "tenant-beta" not in corpus and "operator-secret" not in corpus)
        elif transition == "notification_retry_reconciled":
            checks.update(product_succeeded=event.get("exit_code") == 0, committed=phase == "committed", complete_release=complete_release, durable_committed=self._has_durable_phase("committed"), stable_alpha_identity=self._matches_named_baseline(event, "alpha"), single_alpha_notification=self._notification_event_count("project-alpha") == 1)
        elif transition == "initial_prepared":
            checks.update(product_succeeded=event.get("exit_code") == 0, prepared=phase == "prepared", reserved=budget == "reserved", durable_prepared=self._has_durable_phase("prepared"))
        elif transition == "run_cancelled":
            checks.update(product_succeeded=event.get("exit_code") == 0, budget_released=budget == "cancelled", durable_cancelled=self._has_durable_phase("cancelled"))
            extra["transaction_phase_comparison"] = {"expected_semantics": "current request cancelled without reversing a committed release", "observed_receipt_phase": phase, "observed_durable_cancelled": self._has_durable_phase("cancelled"), "receipt_phase_is_not_a_published_cancel_field_requirement": True}
        elif transition == "stale_commit_rejected":
            checks.update(product_rejected=event.get("exit_code") not in {None, 0}, structured_stale_error=self._has_structured_error(event), durable_cancelled=self._has_durable_phase("cancelled"), no_complete_release=not complete_release)
        elif transition == "generation_reentry_response_lost":
            checks.update(product_succeeded=event.get("exit_code") == 0, committed=phase == "committed", settled=budget == "settled", complete_release=complete_release, durable_committed=self._has_durable_phase("committed"), takeover_argument_active=self.context.get("takeover_generation") == self.original_context.get("generation"))
            if all(checks.values()):
                mutation = self._remove_response_artifacts(event)
                checks["response_artifacts_removed"] = mutation.get("applied") is True
        else:
            checks["known_transition"] = False
        if transition in {"v1_cancelled", "run_cancelled", "stale_request_fenced", "stale_commit_rejected"}:
            before = plan.get("protected_state_before")
            after = self._protected_state_snapshot()
            checks["no_new_publication_or_settlement_and_peer_preserved"] = isinstance(before, dict) and before == after
            extra["protected_state_comparison"] = {"expected": before, "observed": after, "equal": isinstance(before, dict) and before == after}
        return mutation, checks, extra

    def _publication_boundary(self, *, operation: str, before: Any, after: Any,
                              probe: Any) -> dict[str, Any]:
        """Compare the published bytes this operation was permitted to touch.

        ``commit`` -- and ``verify``, its published atomic prepare-then-commit
        convenience form (input/02 "Controlled release command" / lifecycle) -- may ADD
        published attestation, notification and committed-session bytes for the
        release they complete, and may move that project's settled total.
        ``status``, ``prepare``, ``cancel`` and the ordinary one-shot mode may
        not (input/03 §18).  No operation at all may modify or remove bytes that
        were already published, including a historical release or a peer
        project's (input/03 §15, §19).

        When the evaluator ran its own adversarial sidecar probe in the same
        attempt, that probe also writes through the product, so the delta is
        not attributable to the model's primary operation and is recorded as
        ``attributable: False``.  Such an attempt is never Candidate evidence.
        """
        record: dict[str, Any] = {
            "operation": operation,
            "attributable": probe is None,
            "publishing_operation": operation in PUBLISHING_OPERATIONS,
            "added": [], "modified": [], "removed": [],
            "settled_totals_changed": False,
            "violation": False,
            "reason": "",
        }
        if not isinstance(before, dict) or not isinstance(after, dict):
            record["reason"] = "protected-state snapshot unavailable for this attempt"
            return record
        published_before = before.get("published_file_sha256") or {}
        published_after = after.get("published_file_sha256") or {}
        record["added"] = sorted(set(published_after) - set(published_before))
        record["removed"] = sorted(set(published_before) - set(published_after))
        record["modified"] = sorted(
            name for name in set(published_before) & set(published_after)
            if published_before[name] != published_after[name]
        )
        record["settled_totals_changed"] = (
            before.get("project_settled_micros") != after.get("project_settled_micros")
        )
        if not record["attributable"]:
            record["reason"] = ("an evaluator-owned probe also wrote through the product "
                                "in this attempt; the delta is not Candidate evidence")
            return record
        reasons = []
        if record["modified"] or record["removed"]:
            reasons.append("bytes that were already published were modified or removed")
        if record["added"] and not record["publishing_operation"]:
            reasons.append(f"`{operation}` created published attestation/notification/committed bytes")
        if record["settled_totals_changed"] and not record["publishing_operation"]:
            reasons.append(f"`{operation}` changed a durable settled project total")
        record["violation"] = bool(reasons)
        record["reason"] = "; ".join(reasons) or "published release bytes respected the commit-only boundary"
        return record

    def _protected_state_snapshot(self) -> dict[str, Any]:
        """Capture published bytes, settled totals and unrelated request state.

        Cancellation may change the current reservation and its session, but
        may not settle, publish, or alter a peer/previous committed release.
        Hashes are observations of actual store bytes, not a score proxy.
        """
        state_root = self.output / "state"
        published = {}
        for root in self._published_roots():
            for path in sorted(root.rglob("*")):
                if path.is_file() and not path.is_symlink():
                    published[path.relative_to(state_root).as_posix()] = sha256_file(path)
        settled = {}
        for path in bounded_json_files(state_root / "budgets"):
            try:
                value = json.loads(path.read_text())
            except (OSError, json.JSONDecodeError):
                continue
            if isinstance(value, dict):
                settled[str(value.get("project_id", path.name))] = value.get("settled_micros")
        current = {}
        try:
            current = json.loads((self.output / "transaction_receipt.json").read_text())
        except (OSError, json.JSONDecodeError):
            pass
        fingerprint = current.get("request_fingerprint")
        peers = {}
        for value in self._session_states():
            # A peer is established positively, by a *different* recorded
            # request fingerprint.  A record that simply does not carry one is
            # not evidence of a peer, and treating it as such would make the
            # current request's own lifecycle look like peer damage.
            peer_fingerprint = value.get("request_fingerprint")
            if fingerprint and isinstance(peer_fingerprint, str) and peer_fingerprint != fingerprint:
                peers[value["_evaluator_state_path"]] = value["_evaluator_state_sha256"]
        return {"published_file_sha256": published, "project_settled_micros": settled, "peer_session_sha256": peers}

    def _published_roots(self) -> list[Path]:
        """Published stores, including committed copies at any session depth."""
        state_root = self.output / "state"
        roots = [state_root / "attestations", state_root / "outbox",
                 state_root / "sessions" / "committed"]
        sessions = state_root / "sessions"
        if sessions.is_dir():
            stack: list[tuple[Path, int]] = [(sessions, 0)]
            while stack:
                directory, depth = stack.pop()
                try:
                    entries = sorted(directory.iterdir())
                except OSError:
                    continue
                for entry in entries:
                    if entry.is_symlink() or not entry.is_dir():
                        continue
                    if entry.name == "committed":
                        roots.append(entry)
                    elif depth < SESSION_SCAN_MAX_DEPTH and entry.name != "stages":
                        stack.append((entry, depth + 1))
        unique, seen = [], set()
        for root in roots:
            key = root.as_posix()
            if key not in seen:
                seen.add(key)
                unique.append(root)
        return unique

    def _remove_response_artifacts(self, event: dict[str, Any]) -> dict[str, Any]:
        names = ("reproducibility_capsule.zip", "transaction_receipt.json")
        before = {name: sha256_file(self.output / name) for name in names if (self.output / name).is_file()}
        if set(before) != set(names):
            return {"kind": "response_loss", "applied": False, "before": before, "after": {}}
        self.baseline["response_loss"] = {"hashes": before, "identity": self._identity_baseline(event),
                                          "settled_micros": self._protected_state_snapshot()["project_settled_micros"]}
        for name in names:
            (self.output / name).unlink()
        after = {name: (self.output / name).exists() for name in names}
        return {"kind": "response_loss", "applied": not any(after.values()), "before": before, "exists_after": after}

    def _corrupt_staged_capsule(self) -> dict[str, Any]:
        for state in self._session_states():
            stage = state.get("stage_path")
            if not isinstance(stage, str):
                continue
            target = self.output / "state" / "sessions" / stage / "reproducibility_capsule.zip"
            if target.is_file():
                before = sha256_file(target)
                with target.open("ab") as stream:
                    stream.write(b"evaluator-owned-corruption-fault")
                after = sha256_file(target)
                return {"kind": "staged_capsule_corruption", "applied": before != after, "path": target.relative_to(self.output).as_posix(), "before_sha256": before, "after_sha256": after}
        return {"kind": "staged_capsule_corruption", "applied": False, "path": None}

    def _matches_baseline(self, event: dict[str, Any]) -> bool:
        baseline = self.baseline.get("response_loss") if isinstance(self.baseline.get("response_loss"), dict) else {}
        hashes = event.get("observation", {}).get("release_artifact_hashes", {})
        return bool(
            baseline
            and all(hashes.get(name) == digest for name, digest in baseline.get("hashes", {}).items())
            and self._identity_baseline(event) == baseline.get("identity")
        )

    def _matches_named_baseline(self, event: dict[str, Any], name: str) -> bool:
        return bool(self.baseline.get(name) and self._identity_baseline(event) == self.baseline[name])

    def _identity_baseline(self, event: dict[str, Any]) -> dict[str, Any]:
        transaction = self._receipt(event, "transaction_receipt.json")
        budget = self._receipt(event, "budget_receipt.json")
        notification = self._receipt(event, "notification_receipt.json")
        return {
            "commit_id": transaction.get("commit_id"),
            "capsule_id": transaction.get("capsule_id"),
            "settlement_id": budget.get("settlement_id"),
            "event_id": notification.get("event_id"),
        }

    @staticmethod
    def _receipt(event: dict[str, Any], name: str) -> dict[str, Any]:
        value = event.get("observation", {}).get("receipt_projections", {}).get(name, {})
        return value if isinstance(value, dict) else {}

    def _phase(self, event: dict[str, Any]) -> str | None:
        value = self._receipt(event, "transaction_receipt.json")
        phase = value.get("phase", value.get("state", value.get("status")))
        return str(phase) if isinstance(phase, str) else None

    def _budget_status(self, event: dict[str, Any]) -> str | None:
        value = self._receipt(event, "budget_receipt.json")
        # Public API requires the semantic request-budget state, not one
        # private spelling. Both published Candidate formats are observed.
        status = value.get("budget_status", value.get("request_status", value.get("status")))
        return str(status) if isinstance(status, str) else None

    def _error_code(self, event: dict[str, Any]) -> str | None:
        value = self._receipt(event, "error.json")
        code = value.get("code")
        return str(code) if isinstance(code, str) else None

    def _has_structured_error(self, event: dict[str, Any]) -> bool:
        """The public contract requires structured errors, not a private enum.

        Error wording is retained in native evidence for semantic assessment.
        A failure exit alone or an empty error object is not sufficient.
        """
        value = self._receipt(event, "error.json")
        return bool(
            isinstance(value.get("code"), str) and value["code"].strip()
            or isinstance(value.get("errors"), list)
            and any(isinstance(item, str) and item.strip() or isinstance(item, dict) and item for item in value["errors"])
        )

    def _session_states(self) -> list[dict[str, Any]]:
        result = []
        root = self.output / "state" / "sessions"
        # The CLI contract names a durable directory, not a flat private
        # storage layout.  Enumerate the whole disclosed store instead of two
        # hardcoded depths: a sharded ``sessions/<verification>/<request>/
        # state.json`` layout is as compliant as a flat ``<fingerprint>.json``
        # one, and an evaluator that only understood the flat spelling would
        # report a correctly durable product as having no durable state at all.
        paths = bounded_json_files(root, skip_directories=NON_SESSION_DIRECTORIES)
        current_receipt = {}
        try:
            current_receipt = json.loads((self.output / "transaction_receipt.json").read_text())
        except (OSError, json.JSONDecodeError):
            pass
        for path in sorted(paths):
            try:
                value = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                continue
            if isinstance(value, dict):
                value = dict(value)
                # Identity is commonly nested under the receipt-shaped
                # ``identity`` object; promote it without overwriting a
                # top-level spelling.
                identity = value.get("identity")
                if isinstance(identity, dict):
                    for key, item in identity.items():
                        value.setdefault(key, item)
                if not isinstance(value.get("phase"), str):
                    # Locks, stage payloads and index files are not session
                    # records; only a record carrying a release phase is.
                    continue
                fingerprint = value.get("request_fingerprint")
                receipt = current_receipt if fingerprint and current_receipt.get("request_fingerprint") == fingerprint else {}
                committed_receipt = root / "committed" / path.stem / "transaction_receipt.json"
                if committed_receipt.is_file():
                    try:
                        stored = json.loads(committed_receipt.read_text())
                        if fingerprint and stored.get("request_fingerprint") == fingerprint:
                            receipt = stored
                    except (OSError, json.JSONDecodeError):
                        pass
                # Enrich only from a product receipt with the exact same
                # request fingerprint; never borrow another project's ID.
                for key in ("tenant_id", "verification_id", "request_id", "owner_id", "generation", "project_id", "stage_path"):
                    if key not in value and key in receipt:
                        value[key] = receipt[key]
                value["_evaluator_state_path"] = str(path.relative_to(self.output))
                value["_evaluator_state_sha256"] = sha256_file(path)
                result.append(value)
        return result

    def _has_durable_phase(self, phase: str) -> bool:
        try:
            receipt = json.loads((self.output / "transaction_receipt.json").read_text())
        except (OSError, json.JSONDecodeError):
            receipt = {}
        fingerprint = receipt.get("request_fingerprint")
        return any(state.get("phase") == phase and (not fingerprint or state.get("request_fingerprint") == fingerprint)
                   for state in self._session_states())

    def _durable_projects(self) -> list[str]:
        return [str(state.get("project_id")) for state in self._session_states() if state.get("project_id")]

    def _durable_state_projection(self) -> dict[str, Any]:
        sessions = self._session_states()
        budgets = []
        for path in sorted((self.output / "state" / "budgets").glob("*.json")):
            try:
                value = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                continue
            if isinstance(value, dict):
                budgets.append({key: value.get(key) for key in ("project_id", "policy_version", "reserved_micros", "settled_micros")})
        return {
            "sessions": [{key: value.get(key) for key in ("tenant_id", "verification_id", "request_id", "owner_id", "generation", "project_id", "phase", "stage_path", "committed_dir")} for value in sessions],
            "budgets": budgets,
        }

    def _notification_event_count(self, project_id: str) -> int:
        count = 0
        for path in bounded_json_files(self.output / "state" / "outbox"):
            try:
                value = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                continue
            events = value.get("events") if isinstance(value, dict) else None
            if isinstance(events, dict):
                count += sum(1 for event in events.values() if isinstance(event, dict) and event.get("project_id") == project_id)
        return count

    def _policy_version(self, relative: str) -> str | None:
        try:
            value = json.loads((self.workspace / relative).read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return None
        version = value.get("policy_version") if isinstance(value, dict) else None
        return str(version) if isinstance(version, str) else None

    @staticmethod
    def _context_projection(context: dict[str, Any]) -> dict[str, Any]:
        return {key: context.get(key) for key in (
            "tenant_id", "verification_id", "request_id", "owner_id", "generation",
            "takeover_generation", "project_id", "capsule_policy", "usage_statement",
            "budget_policy", "run_manifest", "attestation_policy", "notification_policy",
        )}
