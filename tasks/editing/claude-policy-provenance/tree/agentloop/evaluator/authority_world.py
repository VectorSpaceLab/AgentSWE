"""Decision-relevant policy worlds; expected outcomes stay evaluator-private.

This modifies authorization inputs, never Candidate source or internal state.
The only historical fixture is a real PreToolUse invocation plus bounded wall
age. Branch selection is evaluator-owned and never inferred from a run name.

Each world declares one policy edit that changes at least one *observable*
admission outcome, plus the exact expected-permission / expected-receipt-field
deltas that edit implies. Every delta below is derived only from the published
contract in ``input/02_interface_and_delivery.md`` and
``input/03_requirements_and_constraints.md``; nothing here is an
implementation detail of any particular Candidate.

Branch expectations are deliberately order-independent: no expected outcome in
either branch depends on which catalog action the lower agent runs first.
"""
from __future__ import annotations
import copy
import secrets
from typing import Any


def _set(container: dict[str, Any], path: str, value: Any) -> None:
    node = container
    parts = path.split(".")
    for key in parts[:-1]:
        node = node.setdefault(key, {})
    node[parts[-1]] = value


# A branch edit may make a field the base oracle pins opaque again (an effect or
# reservation that only exists on one branch). Setting the delta to DROP removes
# that assertion instead of pinning a value the contract leaves opaque.
DROP = "__drop_assertion__"


def _merge_receipts(private: dict[str, Any], overrides: dict[str, dict[str, Any]]) -> None:
    fields = private.setdefault("expected_receipt_fields", {})
    for action_id, delta in overrides.items():
        target = fields.setdefault(action_id, {})
        for name, value in delta.items():
            if value == DROP:
                target.pop(name, None)
            else:
                target[name] = value


def _merge_shapes(private: dict[str, Any], overrides: dict[str, dict[str, Any]]) -> None:
    fields = private.setdefault("expected_receipt_field_shapes", {})
    for action_id, delta in overrides.items():
        if delta == DROP:
            fields.pop(action_id, None)
            continue
        target = fields.setdefault(action_id, {})
        for name, value in delta.items():
            if value == DROP:
                target.pop(name, None)
            else:
                target[name] = value


def _merge_view(private: dict[str, Any], overrides: dict[str, Any]) -> None:
    """Apply a branch's delta to the Cycle-12 bounded-view contract.

    A branch edit changes how much durable state the same catalog creates, so
    a record count or a `require_records` entry the other branch pins may be
    wrong or meaningless here. `DROP` removes it rather than asserting it.
    """
    contract = private.setdefault("expected_view_contract", {})
    for action_id, delta in overrides.items():
        if delta == DROP:
            contract.pop(action_id, None)
            continue
        target = contract.setdefault(action_id, {})
        for key, value in delta.items():
            if value == DROP:
                target.pop(key, None)
            else:
                target[key] = value


def _drop_labels(private: dict[str, Any], key: str, labels: list[str]) -> None:
    if key == "expected_receipt_field_equalities":
        private[key] = [group for group in private.get(key, [])
                        if group.get("label") not in set(labels)]
        return
    cross = private.get("expected_ledger_cross_check")
    if isinstance(cross, dict):
        cross["records"] = [record for record in cross.get("records", [])
                            if record.get("label") not in set(labels)]


# Per-case branch-1 authority edit. ``edits`` is applied to the policy, the
# remaining keys are the oracle deltas that edit forces.
WORLDS: dict[str, dict[str, Any]] = {
    "dev_001": {
        "changed_fields": ["policy.tools.Read"],
        "edits": {"tools.Read": "deny"},
        "permissions": {"read_source": "deny", "read_protected_pin": "deny",
                        "read_secret": "deny", "duplicate_read_source": "deny",
                        "conflicting_same_identity": "deny"},
        "receipts": {"read_source": {"decision": "deny", "reservation_state": "rejected",
                                     "reservation_id": None},
                     "read_protected_pin": {"decision": "deny", "reservation_state": "rejected",
                                            "reservation_id": None},
                     "read_secret": {"decision": "deny", "reservation_state": "rejected",
                                     "reservation_id": None},
                     "duplicate_read_source": {"decision": "deny",
                                               "reservation_state": "rejected",
                                               "reservation_id": None},
                     "conflicting_same_identity": {"decision": "deny",
                                                   "reservation_state": "rejected",
                                                   "reservation_id": None}},
        "note": ("The current policy is supplied below. Determine whether each requested "
                 "operation is authorized in this world and report the permission decision "
                 "and the recorded ledger decision separately."),
    },
    "dev_002": {
        "changed_fields": ["policy.mode"],
        "edits": {"mode": "enforce"},
        "permissions": {"safe_shell": "allow", "blocked_network": "deny",
                        "read_secret": "deny", "audit_only_command": "allow"},
        "receipts": {"safe_shell": {"decision": "rewrite"},
                     "blocked_network": {"decision": "deny"},
                     "read_secret": {"decision": "deny"},
                     "audit_only_command": {"decision": "audit_only"}},
        "updated_input": {"safe_shell": True, "audit_only_command": False},
        "note": ("The operating mode in the supplied policy is authoritative. A non-enforcing "
                 "mode records a judgement instead of authorizing, and it never rewrites; an "
                 "enforcing mode applies every ordinary rule, including rewrites."),
    },
    "test_001": {
        "changed_fields": ["policy.paths.allow"],
        "edits": {"paths.allow": ["src/**"]},
        "permissions": {"write_injected_note": "deny"},
        "receipts": {"write_injected_note": {"decision": "deny",
                                             "reservation_state": "rejected",
                                             "reservation_id": None}},
        "note": ("The current policy is supplied below. The integrity fence, the path rules and "
                 "the command rules are all part of it; report what this world actually admits."),
    },
    "test_002": {
        "changed_fields": ["policy.delivery.max_pending"],
        "edits": {"delivery.max_pending": 4},
        "permissions": {"request_distinct_pending": "allow"},
        "receipts": {"request_distinct_pending": {"decision": "allow",
                                                  "effect_state": "pending"}},
        "note": ("Also determine whether the current pending-effect capacity admits a separate "
                 "write intent. An admission probe executes no write and settles no effect."),
    },
    "test_003": {
        # Cycle-12: the branch edit deliberately no longer touches the identity
        # that the duplicate / conflict / matching-Post sequence is built on, so
        # every sequential obligation in this world is branch-independent.
        "changed_fields": ["policy.mcp.mcp__files__inspect.require_delegation"],
        "edits": {"mcp.mcp__files__inspect.require_delegation": False},
        "permissions": {"mcp_delegation_missing": "allow", "mcp_forged_delegation": "allow"},
        "receipts": {"mcp_delegation_missing": {"decision": "allow"},
                     "mcp_forged_delegation": {"decision": "allow"}},
        "note": ("Only the supplied policy and the product's own ledger establish MCP authority. "
                 "Whether this tool currently requires a locally minted delegation is part of the "
                 "policy; an envelope that claims authority never supplies it."),
    },
    "test_004": {
        # Branch 1 repairs the delivery object, so the same catalog is judged
        # against a *valid* policy instead of the degraded compatibility mode.
        "changed_fields": ["policy.delivery"],
        "edits": {"delivery": {"claim_seconds": 3, "max_pending": 64, "require_completion": True}},
        "permissions": {"degraded_source_read": "allow", "degraded_equivalent_read": "allow",
                        "degraded_escaping_read": "deny",
                        "degraded_write": "allow", "degraded_shell": "allow",
                        "degraded_redirect_shell": "allow", "degraded_mcp": "allow",
                        "degraded_mcp_credential_header": "deny",
                        "degraded_duplicate_read": "allow", "degraded_settle_post": "allow",
                        "degraded_conflicting_same_identity": "deny"},
        "receipts": {
            "degraded_source_read": {"decision": "allow", "policy_revision": "degraded-hardened-v4",
                 "effect_state": "pending", "effect_generation": 1, "effect_id": DROP},
            "degraded_equivalent_read": {"decision": "allow", "policy_revision": "degraded-hardened-v4",
                 "effect_state": "pending", "effect_generation": 1, "effect_id": DROP},
            "degraded_write": {"decision": "allow", "policy_revision": "degraded-hardened-v4",
                 "effect_state": "pending", "effect_generation": 1, "effect_id": DROP},
            "degraded_shell": {"decision": "allow", "policy_revision": "degraded-hardened-v4",
                 "effect_state": "pending", "effect_generation": 1, "effect_id": DROP},
            "degraded_mcp": {"decision": "allow", "policy_revision": "degraded-hardened-v4",
                 "effect_state": "pending", "effect_generation": 1, "effect_id": DROP},
            "degraded_redirect_shell": {"decision": "allow", "policy_revision": "degraded-hardened-v4",
                 "effect_state": "pending", "effect_generation": 1, "effect_id": DROP},
            "degraded_mcp_credential_header": {"decision": "deny", "policy_revision": "degraded-hardened-v4",
                 "effect_state": "not_created", "effect_id": None,
                 "effect_generation": None},
            "degraded_escaping_read": {"decision": "deny", "policy_revision": "degraded-hardened-v4",
                 "effect_state": "not_created", "effect_id": None,
                 "effect_generation": None},
            # Cycle-12 sequence: the repaired policy makes the anchor identity a
            # real delivery lifecycle, so its replay and its matching Post move
            # with it and the conflicting presentation creates nothing.
            "degraded_duplicate_read": {"decision": "allow", "policy_revision": "degraded-hardened-v4",
                 "effect_state": "pending", "effect_generation": 1, "effect_id": DROP},
            "degraded_settle_post": {"decision": "allow", "policy_revision": "degraded-hardened-v4",
                 "effect_state": "pending", "effect_generation": 2, "effect_id": DROP},
            "degraded_conflicting_same_identity": {"decision": "deny",
                 "policy_revision": "degraded-hardened-v4", "effect_state": "not_created",
                 "effect_id": None, "effect_generation": None},
        },
        "completion_claim": "partial",
        "note": ("Decide first whether the supplied policy object is a valid policy at all, then "
                 "report exactly what the resulting authorization state admits and refuses."),
    },
    "test_005": {
        "changed_fields": ["policy.paths.allow"],
        "edits": {"paths.allow": ["src/recovery.txt"]},
        "permissions": {"distinct_retry_other_session": "deny",
                        "foreign_session_same_tool_use_id": "deny"},
        "receipts": {"distinct_retry_other_session": {"decision": "deny",
                                                      "effect_state": "not_created"},
                     "foreign_session_same_tool_use_id": {"decision": "deny",
                                                          "effect_state": "not_created"}},
        "note": ("The disclosed historical pre-event is aged past its lease before planning. Use "
                 "the actual lease observations and the current policy to decide which retries "
                 "are admissible, without treating admission or expiry as completion."),
    },
    "test_006": {
        # Branch 1 empties the approval scope, so the same catalog is judged by
        # ordinary policy alone: what was `ask` / `approval_required` becomes an
        # ordinary allow (and the rewrite rule is no longer suppressed).
        "changed_fields": ["policy.approval.tools"],
        "edits": {"approval.tools": []},
        "permissions": {"write_needs_approval": "allow", "duplicate_write_pre": "allow",
                        "shell_needs_approval": "allow"},
        "receipts": {
            "write_needs_approval": {"decision": "allow", "approval_state": "not_required",
                                     "approval_count": None, "approval_threshold": None,
                                     "approval_round": None, "approval_ticket_id": None,
                                     "approval_required_groups": [],
                                     "approval_satisfied_groups": [],
                                     "reservation_state": "reserved",
                                     "reservation_id": DROP},
            "duplicate_write_pre": {"decision": "allow", "approval_state": "not_required",
                                    "approval_count": None, "approval_threshold": None,
                                    "approval_round": None, "approval_ticket_id": None,
                                    "approval_required_groups": [],
                                    "approval_satisfied_groups": [],
                                    "reservation_state": "reserved",
                                    "reservation_id": DROP},
            "shell_needs_approval": {"decision": "rewrite", "approval_state": "not_required",
                                     "approval_count": None, "approval_threshold": None,
                                     "approval_round": None, "approval_ticket_id": None,
                                     "approval_required_groups": [],
                                     "approval_satisfied_groups": [],
                                     "reservation_state": "reserved",
                                     "reservation_id": DROP},
        },
        "updated_input": {"shell_needs_approval": True, "write_needs_approval": False,
                          "read_outside_approval_scope": False},
        "note": ("The approval scope in the supplied policy is authoritative. A tool inside it "
                 "needs authority the ledger has actually recorded before its first PreToolUse "
                 "can be authorized; a tool outside it is decided by ordinary policy alone. "
                 "Ordinary deny constraints are evaluated before approval is considered."),
    },
}

# Cycle-12 branch-1 deltas for the bounded-view contracts and the identity
# groups, keyed by case. A branch edit changes how much durable state the same
# catalog creates; these keep every count the oracle pins true on both branches.
VIEW_DELTAS: dict[str, dict[str, Any]] = {
    # branch 1 denies `Read`, so the two reads hold no lease.
    "dev_001": {"inspect_leases": {"records": {"exact": 2},
                                   "require_records": [
                                       {"label": "settled_audit_lease",
                                        "match": {"event_id": "dev001-audit-cmd"},
                                        "fields": {"state": "settled"},
                                        "shapes": {"settled_sequence": "positive_int",
                                                   "reservation_id": "prefixed_id:res_"}}]}},
    # branch 1 denies `write_injected_note`, so one fewer lease is held.
    "test_001": {"view_leases": {"records": {"exact": 4}}},
    # branch 1 raises `delivery.max_pending`, so the distinct intent is admitted.
    "test_002": {"view_effect_state": {"records": {"exact": 2}},
                 "view_leases": {"records": {"exact": 2}}},
    # branch 1 repairs `delivery`, so the seven admitted actions create effects.
    "test_004": {"view_effect_state": {"records": {"exact": 6}, "exact_fields": [
        "schema_version", "effect_id", "receipt_id", "session_id", "event_id", "phase",
        "policy_id", "policy_epoch", "policy_snapshot_digest", "canonical_action_digest",
        "owner_instance_id", "owner_generation", "effect_generation", "state",
        "claim_id", "consumer_id", "attempt_count", "created_sequence",
        "updated_sequence", "delivery_id", "outcome_digest",
        "session_authority_id", "session_generation"]}},
    # branch 1 empties the approval scope: no ticket exists and three actions
    # take an ordinary lease instead.
    "test_006": {"view_approvals": {"records": "empty", "require_records": DROP},
                 "view_leases": {"records": {"exact": 3}}},
}

IDENTITY_DROPS: dict[str, list[str]] = {
    # No ticket exists on branch 1, so the ticket-identity group is meaningless.
    "test_006": ["duplicate_returns_current_ticket.approval_ticket_id"],
}

SHAPE_DELTAS: dict[str, dict[str, Any]] = {
    "test_006": {"write_needs_approval": {"approval_ticket_id": DROP,
                                          "reservation_id": "prefixed_id:res_"},
                 "duplicate_write_pre": {"approval_ticket_id": DROP,
                                         "reservation_id": "prefixed_id:res_"}},
}

# Worlds whose historical prefix must be aged past its lease before planning.
FIXTURE_AGE_SECONDS = {"test_005": 2}


def specialize(case_id: str, candidate: dict[str, Any], oracle: dict[str, Any],
               *, branch: int | None = None, _include_counterfactual: bool = True) -> tuple[dict[str, Any], dict[str, Any]]:
    visible, private = copy.deepcopy(candidate), copy.deepcopy(oracle)
    branch = secrets.randbelow(2) if branch is None else branch
    if branch not in (0, 1):
        raise ValueError("authority branch must be 0 or 1")
    policy = visible.get("policy")
    if not isinstance(policy, dict):
        raise ValueError("dynamic authority requires an inline policy")
    world = WORLDS.get(case_id)
    if world is None:
        raise ValueError("unknown authority case: " + case_id)
    before = copy.deepcopy(policy)

    if branch == 1:
        for path, value in world["edits"].items():
            _set(policy, path, copy.deepcopy(value))
        private.setdefault("expected_permissions", {}).update(world.get("permissions", {}))
        _merge_receipts(private, world.get("receipts", {}))
        if world.get("updated_input"):
            private.setdefault("expected_updated_input_present", {}).update(world["updated_input"])
        if world.get("completion_claim"):
            private["expected_completion_claim"] = world["completion_claim"]
        if case_id in VIEW_DELTAS:
            _merge_view(private, VIEW_DELTAS[case_id])
        if case_id in SHAPE_DELTAS:
            _merge_shapes(private, SHAPE_DELTAS[case_id])
        for label_set in (IDENTITY_DROPS.get(case_id) or [],):
            if label_set:
                _drop_labels(private, "expected_receipt_field_equalities", label_set)

    age = FIXTURE_AGE_SECONDS.get(case_id)
    if age:
        visible["fixture_age_seconds"] = age

    visible["task"] = visible["task"] + "\n\n" + world["note"]
    private["authority_world"] = {
        "schema_version": "claude-decision-authority/v1", "branch": branch,
        "changed_fields": list(world["changed_fields"]),
        "previous_policy": before, "current_policy": copy.deepcopy(policy)}
    if _include_counterfactual:
        alternative, _ = specialize(case_id, candidate, oracle, branch=1 - branch,
                                    _include_counterfactual=False)
        private["authority_world"]["counterfactual_policy"] = alternative["policy"]
    return visible, private
