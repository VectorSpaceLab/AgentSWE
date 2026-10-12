#!/usr/bin/env python3
"""Run disclosed contract examples without importing evaluator-owned material."""
from __future__ import annotations

import argparse
import concurrent.futures
import hashlib
import json
import os
import re
import signal
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path


RECEIPT_FIELDS = {
    "schema_version", "receipt_id", "session_id", "event_id", "tool_name",
    "decision", "policy_id", "policy_epoch", "policy_revision",
    "policy_snapshot_digest", "canonical_action_digest", "key_id",
    "ledger_sequence", "integrity", "delegation_id",
    "delegation_parent_id", "delegation_key_id", "delegation_scope_digest",
    "reservation_id", "reservation_state", "quota_remaining",
    "approval_ticket_id", "approval_state", "approval_count",
    "approval_threshold", "approval_round", "approval_required_groups",
    "approval_satisfied_groups", "owner_instance_id", "owner_generation",
    "ownership_state", "handoff_id",
    "state_format_version", "upgrade_id", "upgrade_state",
    "effect_id", "effect_generation", "effect_state",
    "repair_id", "repair_generation", "integrity_state",
    "session_authority_id", "session_generation", "continuation_id",
    "continuation_state",
}
LEASE_FIELDS = {
    "schema_version", "reservation_id", "session_id", "event_id", "receipt_id",
    "policy_id", "policy_epoch", "policy_snapshot_digest", "key_id", "state",
    "created_sequence", "settled_sequence", "quota_remaining",
    "session_authority_id", "session_generation",
}
MUTABLE_SETTLEMENT_FIELDS = {"reservation_state", "quota_remaining", "integrity"}
MUTABLE_SETTLEMENT_FIELDS.update({"approval_ticket_id", "approval_state", "approval_count",
                                  "approval_round", "approval_satisfied_groups",
                                  "owner_instance_id", "owner_generation",
                                  "ownership_state", "handoff_id", "effect_generation",
                                  "effect_state", "continuation_state"})
APPROVAL_FIELDS = {
    "schema_version", "ticket_id", "session_id", "event_id", "receipt_id",
    "tool_name", "policy_id", "policy_epoch", "policy_snapshot_digest",
    "canonical_action_digest", "key_id", "state", "threshold",
    "approval_count", "principal_ids", "created_sequence",
    "consumed_sequence", "expires_at", "approval_round", "required_groups",
    "satisfied_groups", "predecessor_ticket_id", "superseded_by_ticket_id",
    "supersession_id", "session_authority_id", "session_generation",
}
CHECKPOINT_FIELDS = {
    "schema_version", "kind", "transfer_id", "origin_instance_id",
    "export_sequence", "base_checkpoint_digest", "created_at", "policy_id",
    "policy_epoch", "policy_snapshot_digest", "audit_event_count",
    "receipt_count", "lease_count", "approval_ticket_count", "handoff_count",
    "effect_count", "upgrade_count", "state_format_version", "upgrade_lineage_digest",
    "repair_count", "continuation_count", "repair_generation", "repair_lineage_digest",
    "owner_instance_id", "owner_generation", "ownership_state", "sealed_state",
    "checkpoint_digest", "integrity",
}
CHECKPOINT_VIEW_FIELDS = {
    "schema_version", "checkpoint_digest", "origin_instance_id",
    "origin_export_sequence", "base_checkpoint_digest", "imported_at_sequence",
    "policy_id", "policy_epoch", "policy_snapshot_digest", "audit_event_count",
    "receipt_count", "lease_count", "approval_ticket_count", "handoff_count",
    "effect_count", "upgrade_count", "state_format_version", "upgrade_lineage_digest",
    "repair_count", "continuation_count", "repair_generation", "repair_lineage_digest",
    "owner_instance_id", "owner_generation", "ownership_state", "state", "lineage_digest",
}
HANDOFF_OFFER_FIELDS = {
    "schema_version", "kind", "handoff_id", "source_instance_id",
    "target_instance_id", "source_generation", "target_generation", "created_at",
    "expires_at", "policy_id", "policy_epoch", "policy_snapshot_digest",
    "audit_event_count", "receipt_count", "lease_count", "approval_ticket_count",
    "effect_count", "upgrade_count", "state_format_version", "upgrade_lineage_digest",
    "repair_count", "continuation_count", "repair_generation", "repair_lineage_digest",
    "sealed_state", "offer_digest", "integrity",
}
HANDOFF_ACK_FIELDS = {
    "schema_version", "kind", "handoff_id", "source_instance_id",
    "target_instance_id", "source_generation", "target_generation", "offer_digest",
    "accepted_at", "ack_digest", "integrity",
}
HANDOFF_COMMIT_FIELDS = (HANDOFF_ACK_FIELDS - {"accepted_at", "ack_digest"}) | {
    "ack_digest", "finalized_at", "commit_digest",
}
HANDOFF_VIEW_FIELDS = {
    "schema_version", "handoff_id", "source_instance_id", "target_instance_id",
    "source_generation", "target_generation", "state", "offer_digest", "ack_digest",
    "commit_digest", "created_sequence", "transition_sequence", "policy_id",
    "policy_epoch", "policy_snapshot_digest", "expires_at",
    "effect_count", "upgrade_count", "state_format_version", "upgrade_lineage_digest",
    "repair_count", "continuation_count", "repair_generation", "repair_lineage_digest",
}
EFFECT_FIELDS = {
    "schema_version", "effect_id", "receipt_id", "session_id", "event_id", "phase",
    "policy_id", "policy_epoch", "policy_snapshot_digest", "canonical_action_digest",
    "owner_instance_id", "owner_generation", "effect_generation", "state", "claim_id",
    "consumer_id", "attempt_count", "created_sequence", "updated_sequence",
    "delivery_id", "outcome_digest", "session_authority_id", "session_generation",
}
EFFECT_CLAIM_FIELDS = {
    "schema_version", "kind", "consumer_id", "claim_id", "claimed_at", "expires_at",
    "items", "claim_token", "claim_digest", "integrity",
}
UPGRADE_MANIFEST_FIELDS = {
    "schema_version", "kind", "upgrade_id", "source_format_version",
    "target_format_version", "base_sequence", "cursor_sequence", "source_state_digest",
    "expected_upgrade_lineage_digest", "started_at", "upgrade_digest", "integrity",
    "repair_generation", "repair_lineage_digest", "continuation_count",
}
UPGRADE_STATUS_FIELDS = {
    "schema_version", "kind", "upgrade_id", "source_format_version",
    "target_format_version", "base_sequence", "cursor_sequence", "catchup_sequence",
    "remaining_items", "ready_to_commit", "state", "upgrade_digest", "integrity",
    "repair_generation", "repair_lineage_digest", "continuation_count",
}
UPGRADE_COMMIT_FIELDS = {
    "schema_version", "kind", "upgrade_id", "source_format_version",
    "target_format_version", "source_state_digest", "previous_upgrade_lineage_digest",
    "committed_at", "commit_sequence", "upgrade_digest", "upgrade_lineage_digest", "integrity",
    "repair_generation", "repair_lineage_digest", "continuation_count",
}
UPGRADE_VIEW_FIELDS = {
    "schema_version", "upgrade_id", "source_format_version", "target_format_version",
    "state", "base_sequence", "cursor_sequence", "catchup_sequence", "source_state_digest",
    "previous_upgrade_lineage_digest", "upgrade_lineage_digest", "created_sequence",
    "transition_sequence", "repair_generation", "repair_lineage_digest", "continuation_count",
}
REPAIR_MANIFEST_FIELDS = {
    "schema_version", "kind", "repair_id", "repair_generation",
    "source_repair_generation", "source_state_digest", "damage_digest",
    "damaged_high_sequence", "verified_prefix_sequence", "verified_prefix_digest",
    "candidate_cursor", "candidate_item_count", "started_at", "repair_digest", "integrity",
}
REPAIR_STATUS_FIELDS = {
    "schema_version", "kind", "repair_id", "repair_generation", "candidate_cursor",
    "candidate_item_count", "remaining_items", "candidate_state_digest",
    "discarded_event_count", "invalidated_authority_count", "attested_verifiers",
    "ready_for_verification", "state", "repair_digest", "integrity",
}
REPAIR_ATTESTATION_FIELDS = {
    "schema_version", "kind", "repair_id", "repair_generation", "verifier_id",
    "candidate_state_digest", "damage_digest", "verified_prefix_sequence", "attested_at",
    "attestation_digest", "integrity",
}
REPAIR_COMMIT_FIELDS = {
    "schema_version", "kind", "repair_id", "repair_generation", "source_state_digest",
    "candidate_state_digest", "damage_digest", "verified_prefix_sequence",
    "discarded_event_count", "invalidated_authority_count", "verifier_ids", "committed_at",
    "commit_sequence", "previous_repair_lineage_digest", "repair_lineage_digest",
    "repair_digest", "integrity",
}
REPAIR_VIEW_FIELDS = {
    "schema_version", "repair_id", "repair_generation", "source_repair_generation",
    "state", "source_state_digest", "damage_digest", "damaged_high_sequence",
    "verified_prefix_sequence", "candidate_cursor", "candidate_item_count",
    "candidate_state_digest", "discarded_event_count", "invalidated_authority_count",
    "verifier_ids", "created_sequence", "transition_sequence",
    "previous_repair_lineage_digest", "repair_lineage_digest",
}
CONTINUATION_TOKEN_FIELDS = {
    "schema_version", "kind", "continuation_id", "session_authority_id",
    "source_session_id", "target_session_id", "source_generation", "target_generation",
    "issued_at", "expires_at", "policy_id", "policy_epoch", "policy_snapshot_digest",
    "owner_instance_id", "owner_generation", "repair_generation", "repair_lineage_digest",
    "state_format_version", "upgrade_lineage_digest", "last_receipt_id", "last_sequence",
    "predecessor_continuation_id", "token_digest", "token", "integrity",
}
CONTINUATION_VIEW_FIELDS = {
    "schema_version", "continuation_id", "session_authority_id", "source_session_id",
    "target_session_id", "source_generation", "target_generation", "state", "policy_id",
    "policy_epoch", "policy_snapshot_digest", "owner_instance_id", "owner_generation",
    "repair_generation", "repair_lineage_digest", "state_format_version",
    "upgrade_lineage_digest", "last_receipt_id", "last_sequence",
    "predecessor_continuation_id", "token_digest", "issued_sequence",
    "transition_sequence", "expires_at",
}
INTEGRITY_VIEW_FIELDS = {
    "schema_version", "state", "generation", "sealed_file_count",
    "protected_path_count", "manifest_digest", "authority_state",
    "created_sequence", "transition_sequence",
}


def invoke(hook: Path, event: dict, workspace: Path, state: Path, plugin: Path) -> dict:
    payload = {key: value for key, value in event.items() if not key.startswith("expect")}
    payload["cwd"] = str(workspace)
    env = dict(os.environ)
    env.update({
        "CLAUDE_PROJECT_DIR": str(workspace),
        "CLAUDE_PLUGIN_ROOT": str(plugin),
        "POLICY_PROVENANCE_STATE_DIR": str(state),
        "POLICY_PROVENANCE_COMPACT_AFTER": "16",
        "PYTHONDONTWRITEBYTECODE": "1",
    })
    done = subprocess.run(
        [sys.executable, str(hook)], input=json.dumps(payload), text=True,
        capture_output=True, env=env, cwd=workspace, timeout=10, check=False,
    )
    if done.returncode != 0:
        raise RuntimeError(f"hook exited {done.returncode}: {done.stderr[:300]}")
    value = json.loads(done.stdout)
    if not isinstance(value, dict):
        raise RuntimeError("hook response is not one JSON object")
    return value


def inspect(inspector: Path, state: Path, *extra: str) -> tuple[list[dict], str, int]:
    env = {**os.environ, "PYTHONDONTWRITEBYTECODE": "1"}
    done = subprocess.run(
        [str(inspector), "--state-dir", str(state), *extra], text=True,
        capture_output=True, env=env, timeout=10, check=False,
    )
    values: list[dict] = []
    try:
        for line in done.stdout.splitlines():
            if line.strip():
                value = json.loads(line)
                if not isinstance(value, dict):
                    raise ValueError("non-object JSONL")
                values.append(value)
    except (json.JSONDecodeError, ValueError):
        values = []
    return values, done.stdout + done.stderr, done.returncode


def maintain(inspector: Path, state: Path, *extra: str,
             input_value: dict | None = None) -> tuple[dict, str, int]:
    env = {**os.environ, "PYTHONDONTWRITEBYTECODE": "1"}
    done = subprocess.run(
        [str(inspector), "--state-dir", str(state), *extra],
        input=json.dumps(input_value) if input_value is not None else None,
        text=True, capture_output=True, env=env, timeout=10, check=False,
    )
    try:
        value = json.loads(done.stdout) if done.stdout.strip() else {}
    except json.JSONDecodeError:
        value = {}
    return value if isinstance(value, dict) else {}, done.stdout + done.stderr, done.returncode


def maintain_jsonl(inspector: Path, state: Path, *extra: str,
                   input_value: dict | None = None) -> tuple[list[dict], str, int]:
    env = {**os.environ, "PYTHONDONTWRITEBYTECODE": "1"}
    done = subprocess.run(
        [str(inspector), "--state-dir", str(state), *extra],
        input=json.dumps(input_value) if input_value is not None else None,
        text=True, capture_output=True, env=env, timeout=10, check=False,
    )
    values: list[dict] = []
    try:
        for line in done.stdout.splitlines():
            if line.strip():
                value = json.loads(line)
                if not isinstance(value, dict):
                    raise ValueError("non-object JSONL")
                values.append(value)
    except (json.JSONDecodeError, ValueError):
        values = []
    return values, done.stdout + done.stderr, done.returncode


def crash_partial_maintenance(inspector: Path, state: Path, option: str,
                              request: dict) -> tuple[str, int]:
    """Kill an evaluator-owned inspector after a partial request is flushed."""
    env = {**os.environ, "PYTHONDONTWRITEBYTECODE": "1"}
    process = subprocess.Popen(
        [str(inspector), "--state-dir", str(state), option], text=True,
        stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        env=env, start_new_session=True,
    )
    payload = json.dumps(request)
    assert process.stdin is not None
    process.stdin.write(payload[:max(1, len(payload) // 2)])
    process.stdin.flush()
    try:
        os.killpg(process.pid, signal.SIGKILL)
    except ProcessLookupError:
        pass
    stdout, stderr = process.communicate()
    return stdout + stderr, process.returncode


def audit_digest(value: dict) -> str:
    body = {key: item for key, item in value.items() if key != "event_digest"}
    encoded = json.dumps(body, sort_keys=True, separators=(",", ":"), ensure_ascii=True).encode()
    return "sha256:" + hashlib.sha256(encoded).hexdigest()


def valid_chain(values: list[dict]) -> bool:
    previous = "sha256:" + "0" * 64
    sequence = 0
    for value in values:
        current = value.get("sequence")
        if (not isinstance(current, int) or current != sequence + 1
                or value.get("previous_event_digest") != previous
                or value.get("event_digest") != audit_digest(value)):
            return False
        previous = value["event_digest"]
        sequence = current
    return True


def receipt_ok(response: dict, event: dict, expected_policy: bytes) -> bool:
    receipt = response.get("policyReceipt")
    specific = response.get("hookSpecificOutput")
    if not isinstance(receipt, dict) or not isinstance(specific, dict):
        return False
    reservation_id = receipt.get("reservation_id")
    reservation_state = receipt.get("reservation_state")
    quota = receipt.get("quota_remaining")
    reservation_ok = (
        (reservation_state == "unlimited" and reservation_id is None and quota is None)
        or (reservation_state == "rejected" and reservation_id is None
            and (quota is None or isinstance(quota, int) and not isinstance(quota, bool) and quota >= 0))
        or (reservation_state in {"reserved", "settled", "expired"}
            and re.fullmatch(r"res_[A-Za-z0-9_-]{8,128}", str(reservation_id or "")) is not None
            and isinstance(quota, int) and not isinstance(quota, bool) and quota >= 0)
    )
    ticket_id = receipt.get("approval_ticket_id")
    approval_state = receipt.get("approval_state")
    approval_count = receipt.get("approval_count")
    approval_threshold = receipt.get("approval_threshold")
    approval_ok = (
        (approval_state == "not_required" and ticket_id is None
         and approval_count is None and approval_threshold is None)
        or (approval_state in {"pending", "ready", "consumed", "expired", "revoked", "superseded"}
            and re.fullmatch(r"apt_[A-Za-z0-9_-]{8,128}", str(ticket_id or "")) is not None
            and isinstance(approval_count, int) and not isinstance(approval_count, bool)
            and approval_count >= 0
            and isinstance(approval_threshold, int) and not isinstance(approval_threshold, bool)
            and approval_threshold > 0
            and isinstance(receipt.get("approval_round"), int)
            and receipt["approval_round"] > 0
            and isinstance(receipt.get("approval_required_groups"), list)
            and all(isinstance(item, str) for item in receipt["approval_required_groups"])
            and receipt["approval_required_groups"] == sorted(set(receipt["approval_required_groups"]))
            and isinstance(receipt.get("approval_satisfied_groups"), list)
            and all(isinstance(item, str) for item in receipt["approval_satisfied_groups"])
            and receipt["approval_satisfied_groups"] == sorted(set(receipt["approval_satisfied_groups"])))
    )
    if approval_state == "not_required":
        approval_ok = approval_ok and receipt.get("approval_round") is None \
            and receipt.get("approval_required_groups") == [] \
            and receipt.get("approval_satisfied_groups") == []
    ownership_state = receipt.get("ownership_state")
    unmanaged = ownership_state == "unmanaged" and receipt.get("owner_instance_id") is None \
        and receipt.get("owner_generation") is None and receipt.get("handoff_id") is None
    managed = (ownership_state in {"active", "prepared", "accepted", "finalized", "checkpoint_only"}
               and re.fullmatch(r"inst_[A-Za-z0-9_-]{8,128}", str(receipt.get("owner_instance_id") or "")) is not None
               and isinstance(receipt.get("owner_generation"), int)
               and receipt["owner_generation"] > 0
               and (receipt.get("handoff_id") is None
                    or re.fullmatch(r"hof_[A-Za-z0-9_-]{8,128}", str(receipt.get("handoff_id"))) is not None))
    format_ok = (isinstance(receipt.get("state_format_version"), int)
                 and not isinstance(receipt.get("state_format_version"), bool)
                 and receipt["state_format_version"] > 0
                 and receipt.get("upgrade_state") in {"stable", "building"}
                 and (receipt.get("upgrade_id") is None
                      or re.fullmatch(r"upg_[A-Za-z0-9_-]{8,128}", str(receipt.get("upgrade_id"))) is not None))
    effect_state = receipt.get("effect_state")
    effect_ok = ((effect_state in {"not_configured", "not_created"}
                  and receipt.get("effect_id") is None and receipt.get("effect_generation") is None)
                 or (effect_state in {"pending", "claimed", "delivered"}
                     and re.fullmatch(r"eff_[A-Za-z0-9_-]{8,128}", str(receipt.get("effect_id") or "")) is not None
                     and isinstance(receipt.get("effect_generation"), int)
                     and receipt["effect_generation"] > 0))
    integrity_state = receipt.get("integrity_state")
    repair_ok = (isinstance(receipt.get("repair_generation"), int)
                 and not isinstance(receipt.get("repair_generation"), bool)
                 and receipt["repair_generation"] >= 0
                 and integrity_state in {"healthy", "quarantined", "rebuilding", "verifying"}
                 and ((integrity_state in {"healthy", "quarantined"}
                       and receipt.get("repair_id") is None)
                      or (integrity_state in {"rebuilding", "verifying"}
                          and re.fullmatch(r"rpr_[A-Za-z0-9_-]{8,128}",
                                           str(receipt.get("repair_id") or "")) is not None)))
    continuation_state = receipt.get("continuation_state")
    continuation_unset = (continuation_state in {"not_configured", "not_created"}
                          and receipt.get("session_authority_id") is None
                          and receipt.get("session_generation") is None
                          and receipt.get("continuation_id") is None)
    continuation_live = (continuation_state in {"active", "pending", "continued",
                                                 "cancelled", "expired", "invalidated"}
                         and re.fullmatch(r"sca_[A-Za-z0-9_-]{8,128}",
                                          str(receipt.get("session_authority_id") or "")) is not None
                         and isinstance(receipt.get("session_generation"), int)
                         and receipt["session_generation"] > 0
                         and (receipt.get("continuation_id") is None
                              or re.fullmatch(r"ctn_[A-Za-z0-9_-]{8,128}",
                                              str(receipt.get("continuation_id"))) is not None))
    return (
        RECEIPT_FIELDS.issubset(receipt)
        and receipt.get("schema_version") == 1
        and receipt.get("session_id") == event.get("session_id")
        and receipt.get("event_id") == event.get("tool_use_id")
        and receipt.get("tool_name") == event.get("tool_name")
        and receipt.get("policy_snapshot_digest")
        == "sha256:" + hashlib.sha256(expected_policy).hexdigest()
        and receipt.get("policy_id") == json.loads(expected_policy).get("policy_id", "legacy")
        and receipt.get("policy_epoch") == json.loads(expected_policy).get("policy_epoch", 0)
        and re.fullmatch(r"key_[0-9a-f]{16,64}", str(receipt.get("key_id", ""))) is not None
        and isinstance(receipt.get("ledger_sequence"), int)
        and receipt["ledger_sequence"] > 0
        and re.fullmatch(r"sha256:[0-9a-f]{64}", str(receipt.get("canonical_action_digest", ""))) is not None
        and re.fullmatch(r"hmac-sha256:[0-9a-f]{64}", str(receipt.get("integrity", ""))) is not None
        and specific.get("hookEventName") == event.get("hook_event_name")
        and all(name in receipt for name in ("delegation_id", "delegation_parent_id",
                                              "delegation_key_id", "delegation_scope_digest"))
        and reservation_ok and approval_ok and (unmanaged or managed) and format_ok and effect_ok
        and repair_ok and (continuation_unset or continuation_live)
    )


def add_response_checks(failures: list[str], response: dict, event: dict, policy_bytes: bytes) -> None:
    specific = response.get("hookSpecificOutput", {})
    receipt = response.get("policyReceipt", {})
    event_id = str(event.get("tool_use_id"))
    if specific.get("permissionDecision") != event.get("expect"):
        failures.append(event_id + ": permission")
    if event.get("expect_decision") and receipt.get("decision") != event["expect_decision"]:
        failures.append(event_id + ": decision")
    if not receipt_ok(response, event, policy_bytes):
        failures.append(event_id + ": receipt schema/snapshot")


def same_lifecycle(pre: dict, later: dict) -> bool:
    pre_receipt = pre.get("policyReceipt", {})
    later_receipt = later.get("policyReceipt", {})
    if not isinstance(pre_receipt, dict) or not isinstance(later_receipt, dict):
        return False
    stable = RECEIPT_FIELDS - MUTABLE_SETTLEMENT_FIELDS
    return all(pre_receipt.get(name) == later_receipt.get(name) for name in stable)


def post_for(event: dict, result: dict | None = None) -> dict:
    value = {key: item for key, item in event.items() if not key.startswith("expect")}
    value["hook_event_name"] = "PostToolUse"
    value["tool_response"] = result or {"status": "ok"}
    value["expect"] = "allow"
    return value


def lease_record_ok(value: dict) -> bool:
    settled = value.get("settled_sequence")
    return (
        set(value) == LEASE_FIELDS
        and value.get("schema_version") == 1
        and re.fullmatch(r"res_[A-Za-z0-9_-]{8,128}", str(value.get("reservation_id", ""))) is not None
        and all(isinstance(value.get(name), str) and value.get(name) for name in
                ("session_id", "event_id", "receipt_id", "policy_id", "policy_snapshot_digest", "key_id"))
        and isinstance(value.get("policy_epoch"), int) and not isinstance(value.get("policy_epoch"), bool)
        and value.get("state") in {"reserved", "settled", "expired"}
        and isinstance(value.get("created_sequence"), int) and value["created_sequence"] > 0
        and ((value.get("state") == "settled" and isinstance(settled, int)
              and not isinstance(settled, bool) and settled > 0)
             or (value.get("state") in {"reserved", "expired"} and settled is None))
        and isinstance(value.get("quota_remaining"), int) and value["quota_remaining"] >= 0
    )


def approval_record_ok(value: dict) -> bool:
    consumed = value.get("consumed_sequence")
    return (
        set(value) == APPROVAL_FIELDS
        and value.get("schema_version") == 1
        and re.fullmatch(r"apt_[A-Za-z0-9_-]{8,128}", str(value.get("ticket_id", ""))) is not None
        and value.get("state") in {"pending", "ready", "consumed", "expired", "revoked", "superseded"}
        and isinstance(value.get("threshold"), int) and value["threshold"] > 0
        and isinstance(value.get("approval_count"), int) and value["approval_count"] >= 0
        and isinstance(value.get("principal_ids"), list)
        and all(isinstance(item, str) for item in value["principal_ids"])
        and value["principal_ids"] == sorted(set(value["principal_ids"]))
        and isinstance(value.get("created_sequence"), int) and value["created_sequence"] > 0
        and ((value.get("state") == "consumed" and isinstance(consumed, int) and consumed > 0)
             or (value.get("state") != "consumed" and consumed is None))
        and isinstance(value.get("expires_at"), int)
        and isinstance(value.get("approval_round"), int) and value["approval_round"] > 0
        and all(isinstance(value.get(name), list)
                and all(isinstance(item, str) for item in value[name])
                and value[name] == sorted(set(value[name]))
                for name in ("required_groups", "satisfied_groups"))
        and all(value.get(name) is None or isinstance(value.get(name), str)
                for name in ("predecessor_ticket_id", "superseded_by_ticket_id", "supersession_id"))
    )


def checkpoint_ok(value: dict) -> bool:
    return (
        set(value) == CHECKPOINT_FIELDS
        and value.get("schema_version") == 1
        and value.get("kind") == "policy-provenance-checkpoint"
        and re.fullmatch(r"mig_[A-Za-z0-9_-]{8,128}", str(value.get("transfer_id", ""))) is not None
        and re.fullmatch(r"inst_[A-Za-z0-9_-]{8,128}", str(value.get("origin_instance_id", ""))) is not None
        and isinstance(value.get("export_sequence"), int) and value["export_sequence"] > 0
        and (value.get("base_checkpoint_digest") is None
             or re.fullmatch(r"sha256:[0-9a-f]{64}", str(value["base_checkpoint_digest"])) is not None)
        and all(isinstance(value.get(name), int) and not isinstance(value.get(name), bool)
                and value[name] >= 0 for name in ("created_at", "policy_epoch", "audit_event_count",
                                                  "receipt_count", "lease_count", "approval_ticket_count"))
        and isinstance(value.get("sealed_state"), str) and bool(value["sealed_state"])
        and re.fullmatch(r"sha256:[0-9a-f]{64}", str(value.get("checkpoint_digest", ""))) is not None
        and re.fullmatch(r"hmac-sha256:[0-9a-f]{64}", str(value.get("integrity", ""))) is not None
    )


def checkpoint_view_ok(value: dict) -> bool:
    return (
        set(value) == CHECKPOINT_VIEW_FIELDS
        and value.get("schema_version") == 1
        and value.get("state") in {"exported", "imported"}
        and re.fullmatch(r"sha256:[0-9a-f]{64}", str(value.get("checkpoint_digest", ""))) is not None
        and re.fullmatch(r"sha256:[0-9a-f]{64}", str(value.get("lineage_digest", ""))) is not None
    )


def handoff_offer_ok(value: dict) -> bool:
    return (set(value) == HANDOFF_OFFER_FIELDS
            and value.get("schema_version") == 1
            and value.get("kind") == "policy-provenance-handoff-offer"
            and re.fullmatch(r"hof_[A-Za-z0-9_-]{8,128}", str(value.get("handoff_id", ""))) is not None
            and re.fullmatch(r"sha256:[0-9a-f]{64}", str(value.get("offer_digest", ""))) is not None
            and re.fullmatch(r"hmac-sha256:[0-9a-f]{64}", str(value.get("integrity", ""))) is not None)


def handoff_ack_ok(value: dict) -> bool:
    return (set(value) == HANDOFF_ACK_FIELDS and value.get("schema_version") == 1
            and value.get("kind") == "policy-provenance-handoff-ack"
            and re.fullmatch(r"sha256:[0-9a-f]{64}", str(value.get("ack_digest", ""))) is not None)


def handoff_commit_ok(value: dict) -> bool:
    return (set(value) == HANDOFF_COMMIT_FIELDS and value.get("schema_version") == 1
            and value.get("kind") == "policy-provenance-handoff-commit"
            and re.fullmatch(r"sha256:[0-9a-f]{64}", str(value.get("commit_digest", ""))) is not None)


def handoff_view_ok(value: dict) -> bool:
    return (set(value) == HANDOFF_VIEW_FIELDS and value.get("schema_version") == 1
            and value.get("state") in {"prepared", "accepted", "finalized", "activated", "aborted"}
            and re.fullmatch(r"sha256:[0-9a-f]{64}", str(value.get("offer_digest", ""))) is not None)


def effect_record_ok(value: dict) -> bool:
    return (set(value) == EFFECT_FIELDS and value.get("schema_version") == 1
            and re.fullmatch(r"eff_[A-Za-z0-9_-]{8,128}", str(value.get("effect_id", ""))) is not None
            and value.get("phase") in {"authorized", "completed", "expired"}
            and value.get("state") in {"pending", "claimed", "delivered"}
            and isinstance(value.get("effect_generation"), int) and value["effect_generation"] > 0
            and isinstance(value.get("attempt_count"), int) and value["attempt_count"] >= 0)


def effect_claim_ok(value: dict) -> bool:
    return (set(value) == EFFECT_CLAIM_FIELDS and value.get("schema_version") == 1
            and value.get("kind") == "policy-provenance-effect-claim"
            and isinstance(value.get("items"), list) and bool(value["items"])
            and all(effect_record_ok(item) and item.get("state") == "claimed" for item in value["items"])
            and re.fullmatch(r"efc_[A-Za-z0-9_-]{8,128}", str(value.get("claim_token", ""))) is not None)


def upgrade_manifest_ok(value: dict) -> bool:
    return (set(value) == UPGRADE_MANIFEST_FIELDS and value.get("schema_version") == 1
            and value.get("kind") == "policy-provenance-upgrade-manifest"
            and value.get("source_format_version") == 1 and value.get("target_format_version") == 2)


def upgrade_status_ok(value: dict) -> bool:
    return (set(value) == UPGRADE_STATUS_FIELDS and value.get("schema_version") == 1
            and value.get("kind") == "policy-provenance-upgrade-status"
            and value.get("state") == "building" and isinstance(value.get("ready_to_commit"), bool))


def upgrade_commit_ok(value: dict) -> bool:
    return (set(value) == UPGRADE_COMMIT_FIELDS and value.get("schema_version") == 1
            and value.get("kind") == "policy-provenance-upgrade-commit"
            and value.get("source_format_version") == 1 and value.get("target_format_version") == 2)


def upgrade_view_ok(value: dict) -> bool:
    return (set(value) == UPGRADE_VIEW_FIELDS and value.get("schema_version") == 1
            and value.get("state") in {"building", "committed", "aborted"})


def repair_manifest_ok(value: dict) -> bool:
    return (set(value) == REPAIR_MANIFEST_FIELDS and value.get("schema_version") == 1
            and value.get("kind") == "policy-provenance-repair-manifest"
            and re.fullmatch(r"rpr_[A-Za-z0-9_-]{8,128}", str(value.get("repair_id", ""))) is not None
            and isinstance(value.get("repair_generation"), int) and value["repair_generation"] > 0)


def repair_status_ok(value: dict) -> bool:
    return (set(value) == REPAIR_STATUS_FIELDS and value.get("schema_version") == 1
            and value.get("kind") == "policy-provenance-repair-status"
            and value.get("state") in {"rebuilding", "verifying"}
            and isinstance(value.get("candidate_cursor"), int)
            and isinstance(value.get("remaining_items"), int)
            and isinstance(value.get("ready_for_verification"), bool)
            and isinstance(value.get("attested_verifiers"), list))


def repair_attestation_ok(value: dict) -> bool:
    return (set(value) == REPAIR_ATTESTATION_FIELDS and value.get("schema_version") == 1
            and value.get("kind") == "policy-provenance-repair-attestation"
            and re.fullmatch(r"sha256:[0-9a-f]{64}",
                             str(value.get("attestation_digest", ""))) is not None)


def repair_commit_ok(value: dict) -> bool:
    return (set(value) == REPAIR_COMMIT_FIELDS and value.get("schema_version") == 1
            and value.get("kind") == "policy-provenance-repair-commit"
            and isinstance(value.get("verifier_ids"), list)
            and value["verifier_ids"] == sorted(set(value["verifier_ids"])))


def repair_view_ok(value: dict) -> bool:
    return (set(value) == REPAIR_VIEW_FIELDS and value.get("schema_version") == 1
            and value.get("state") in {"quarantined", "rebuilding", "verifying",
                                       "committed", "aborted"})


def continuation_token_ok(value: dict) -> bool:
    return (set(value) == CONTINUATION_TOKEN_FIELDS and value.get("schema_version") == 1
            and value.get("kind") == "policy-provenance-continuation-token"
            and re.fullmatch(r"ctn_[A-Za-z0-9_-]{8,128}",
                             str(value.get("continuation_id", ""))) is not None
            and re.fullmatch(r"ctk_[A-Za-z0-9_-]{8,128}",
                             str(value.get("token", ""))) is not None)


def continuation_view_ok(value: dict) -> bool:
    return (set(value) == CONTINUATION_VIEW_FIELDS and value.get("schema_version") == 1
            and value.get("state") in {"pending", "activated", "cancelled",
                                       "expired", "invalidated"})


def integrity_view_ok(value: dict) -> bool:
    integer_fields = ("generation", "sealed_file_count", "protected_path_count",
                      "created_sequence", "transition_sequence")
    return (set(value) == INTEGRITY_VIEW_FIELDS
            and value.get("schema_version") == 1
            and value.get("state") in {"not_configured", "sealed", "compromised"}
            and all(isinstance(value.get(field), int)
                    and not isinstance(value.get(field), bool)
                    and value[field] >= 0 for field in integer_fields)
            and re.fullmatch(r"sha256:[0-9a-f]{64}",
                             str(value.get("manifest_digest", ""))) is not None
            and value.get("authority_state") in {"unmanaged", "active", "quarantined"})


def exercise_cycle9_integrity(
        workspace: Path, plugin: Path, assets: Path, label: str,
        failures: list[str]) -> tuple[int, str]:
    """Run the disclosed Cycle-9 contract against an isolated plugin copy."""
    root = workspace.parent
    integrity_workspace = root / f"integrity-{label}-workspace"
    integrity_state = root / f"integrity-{label}-state"
    integrity_plugin = root / f"integrity-{label}-plugin"
    (integrity_workspace / ".claude").mkdir(parents=True)
    (integrity_workspace / "src").mkdir()
    (integrity_workspace / "src" / "app.py").write_text(
        "print('fixture')\n", encoding="utf-8")
    shutil.copytree(plugin, integrity_plugin, symlinks=True)
    integrity_hook = integrity_plugin / "hooks" / "policy_hook.py"
    integrity_inspector = integrity_plugin / "bin" / "policy-ledger-inspect"
    policy_bytes = (assets / "policy_integrity.json").read_bytes()
    (integrity_workspace / ".claude" / "policy-provenance.json").write_bytes(
        policy_bytes)

    events = json.loads((assets / "events.json").read_text(encoding="utf-8"))
    base = json.loads(json.dumps(events[0]))
    base.update({
        "session_id": f"integrity-{label}",
        "hook_event_name": "PreToolUse",
        "tool_use_id": f"integrity-{label}-seal",
        "tool_name": "Read",
        "tool_input": {"file_path": "src/app.py"},
    })
    protected = json.loads(json.dumps(base))
    protected.update({
        "tool_use_id": f"integrity-{label}-protected-write",
        "tool_name": "Write",
        "tool_input": {
            "file_path": "./.claude//policy-provenance.json",
            "content": "changed",
        },
    })
    shell = json.loads(json.dumps(base))
    shell.update({
        "tool_use_id": f"integrity-{label}-protected-shell",
        "tool_name": "Bash",
        "tool_input": {
            "command": "printf changed > ./.claude//policy-provenance.json",
        },
    })
    tampered = json.loads(json.dumps(base))
    tampered["tool_use_id"] = f"integrity-{label}-after-tamper"

    original = integrity_hook.read_bytes()
    try:
        sealed = invoke(integrity_hook, base, integrity_workspace, integrity_state,
                        integrity_plugin)
        protected_response = invoke(
            integrity_hook, protected, integrity_workspace, integrity_state,
            integrity_plugin)
        shell_response = invoke(
            integrity_hook, shell, integrity_workspace, integrity_state,
            integrity_plugin)
        before, before_text, before_code = inspect(
            integrity_inspector, integrity_state, "--integrity")
        integrity_hook.write_bytes(
            original + b"\n# public integrity byte mutation\n")
        compromised = invoke(
            integrity_hook, tampered, integrity_workspace, integrity_state,
            integrity_plugin)
        after, after_text, after_code = inspect(
            integrity_inspector, integrity_state, "--integrity")
    finally:
        integrity_hook.write_bytes(original)
    restored, restored_text, restored_code = inspect(
        integrity_inspector, integrity_state, "--integrity")

    before_view = before[0] if len(before) == 1 else {}
    after_view = after[0] if len(after) == 1 else {}
    restored_view = restored[0] if len(restored) == 1 else {}
    permission = lambda response: response.get(
        "hookSpecificOutput", {}).get("permissionDecision")
    protected_receipts = [protected_response.get("policyReceipt", {}),
                          shell_response.get("policyReceipt", {})]

    if permission(sealed) != "allow" or not receipt_ok(
            sealed, base, policy_bytes):
        failures.append("cycle9 first controlled call did not seal and authorize")
    if (permission(protected_response) != "deny"
            or permission(shell_response) != "deny"
            or any(receipt.get("reservation_id") is not None
                   or receipt.get("approval_ticket_id") is not None
                   or receipt.get("effect_id") is not None
                   for receipt in protected_receipts)):
        failures.append("cycle9 protected path normalization/write fence")
    if (before_code != 0 or not integrity_view_ok(before_view)
            or before_view.get("state") != "sealed"
            or before_view.get("generation") != 1
            or before_view.get("sealed_file_count") != 5
            or before_view.get("protected_path_count") != 2
            or before_view.get("authority_state") != "active"):
        failures.append("cycle9 exact sealed integrity view")
    if (permission(compromised) != "deny"
            or compromised.get("policyReceipt", {}).get("integrity_state")
                != "quarantined"
            or after_code != 0 or restored_code != 0
            or not integrity_view_ok(after_view)
            or not integrity_view_ok(restored_view)
            or after_view.get("state") != "compromised"
            or restored_view.get("state") != "compromised"
            or after_view.get("authority_state") != "quarantined"
            or restored_view.get("authority_state") != "quarantined"
            or after_view.get("generation") != before_view.get("generation")
            or restored_view.get("manifest_digest")
                != before_view.get("manifest_digest")
            or int(after_view.get("transition_sequence") or 0)
                < int(before_view.get("transition_sequence") or 0)):
        failures.append(
            "cycle9 persistent tamper quarantine and restart fail-closed")

    observed = json.dumps(
        [sealed, protected_response, shell_response, compromised,
         before_view, after_view, restored_view], sort_keys=True)
    observed += before_text + after_text + restored_text
    return 4, observed


def exercise_handoff(hook: Path, inspector: Path, workspace: Path, source: Path,
                     plugin: Path, event: dict, policy_bytes: bytes,
                     target_id: str, target: Path, failures: list[str]) -> tuple[int, str]:
    active = invoke(hook, event, workspace, source, plugin)
    count = 1
    add_response_checks(failures, active, event, policy_bytes)
    secret = "public-handoff-secret-000000000000001"
    prepare = {"handoff_id": "hof_public_success_01", "target_instance_id": target_id,
               "handoff_secret": secret}
    offer, offer_text, offer_code = maintain(inspector, source, "--prepare-handoff", input_value=prepare)
    offer_replay, replay_text, replay_code = maintain(inspector, source, "--prepare-handoff", input_value=prepare)
    fenced_event = json.loads(json.dumps(event)); fenced_event["tool_use_id"] += "-source-fenced"
    fenced = invoke(hook, fenced_event, workspace, source, plugin); count += 1
    accept_request = {"offer": offer, "handoff_secret": secret}
    ack, ack_text, ack_code = maintain(inspector, target, "--accept-handoff", input_value=accept_request)
    ack_replay, ack_replay_text, ack_replay_code = maintain(
        inspector, target, "--accept-handoff", input_value=accept_request)
    standby_post = invoke(hook, post_for(event), workspace, target, plugin); count += 1
    finalize_request = {"ack": ack, "handoff_secret": secret}
    commit, commit_text, commit_code = maintain(
        inspector, source, "--finalize-handoff", input_value=finalize_request)
    commit_replay, commit_replay_text, commit_replay_code = maintain(
        inspector, source, "--finalize-handoff", input_value=finalize_request)
    activate_request = {"commit": commit, "handoff_secret": secret}
    _activated, activate_text, activate_code = maintain_jsonl(
        inspector, target, "--activate-handoff", input_value=activate_request)
    _activate_replay, activate_replay_text, activate_replay_code = maintain_jsonl(
        inspector, target, "--activate-handoff", input_value=activate_request)
    target_post = invoke(hook, post_for(event), workspace, target, plugin)
    source_post = invoke(hook, post_for(event), workspace, source, plugin)
    count += 2
    source_view, source_view_text, source_view_code = inspect(
        inspector, source, "--handoffs", "--limit", "10")
    target_view, target_view_text, target_view_code = inspect(
        inspector, target, "--handoffs", "--limit", "10")
    wrong_target = target.parent / "wrong-handoff-target"
    _wrong, wrong_text, wrong_code = maintain(
        inspector, wrong_target, "--accept-handoff",
        input_value={"offer": offer, "handoff_secret": "wrong-handoff-secret-000000000000"})
    if (permission := fenced.get("hookSpecificOutput", {}).get("permissionDecision")) != "deny":
        failures.append("prepared source was not fenced")
    if (offer_code != 0 or replay_code != 0 or not handoff_offer_ok(offer)
            or offer_replay != offer or ack_code != 0 or ack_replay_code != 0
            or not handoff_ack_ok(ack) or ack_replay != ack
            or standby_post.get("hookSpecificOutput", {}).get("permissionDecision") != "deny"):
        failures.append("handoff prepare/accept/replay/standby fence")
    if (commit_code != 0 or commit_replay_code != 0 or not handoff_commit_ok(commit)
            or commit_replay != commit or activate_code != 0 or activate_replay_code != 0
            or target_post.get("hookSpecificOutput", {}).get("permissionDecision") != "allow"
            or target_post.get("policyReceipt", {}).get("reservation_state") != "settled"
            or source_post.get("hookSpecificOutput", {}).get("permissionDecision") != "deny"):
        failures.append("handoff finalize/activate/settle fencing")
    if (source_view_code != 0 or target_view_code != 0
            or not source_view or not target_view
            or not all(handoff_view_ok(item) for item in [*source_view, *target_view])
            or wrong_code == 0):
        failures.append("handoff redacted views/wrong secret")
    observed = "".join((offer_text, replay_text, ack_text, ack_replay_text, commit_text,
                        commit_replay_text, activate_text, activate_replay_text,
                        source_view_text, target_view_text, wrong_text))
    observed += json.dumps([active, fenced, standby_post, target_post, source_post], sort_keys=True)
    if secret in observed:
        failures.append("handoff secret disclosure")
    return count, observed


def exercise_abort_and_crash_handoff(hook: Path, inspector: Path, workspace: Path,
                                     source: Path, plugin: Path, event: dict,
                                     policy_bytes: bytes, failures: list[str]) -> tuple[int, str]:
    count = 0
    abort_event = json.loads(json.dumps(event)); abort_event["session_id"] = "public-handoff-abort"
    abort_event["tool_use_id"] = "public-handoff-abort-active"
    abort_pre = invoke(hook, abort_event, workspace, source, plugin); count += 1
    secret_a = "public-abort-handoff-secret-0000000001"
    prepare_a = {"handoff_id": "hof_public_abort_01", "target_instance_id": "public-branch-a",
                 "handoff_secret": secret_a}
    offer_a, text_offer_a, code_offer_a = maintain(
        inspector, source, "--prepare-handoff", input_value=prepare_a)
    abort_target = source.parent / "public-abort-target"
    ack_a, text_ack_a, code_ack_a = maintain(
        inspector, abort_target, "--accept-handoff",
        input_value={"offer": offer_a, "handoff_secret": secret_a})
    abort_certificate, text_abort_a, code_abort_a = maintain(
        inspector, source, "--abort-handoff",
        input_value={"handoff_id": prepare_a["handoff_id"], "handoff_secret": secret_a})
    _target_abort, text_target_abort, code_target_abort = maintain_jsonl(
        inspector, abort_target, "--abort-handoff",
        input_value={"abort": abort_certificate, "handoff_secret": secret_a})
    abort_post = invoke(hook, post_for(abort_event), workspace, source, plugin); count += 1
    target_probe = json.loads(json.dumps(event)); target_probe["session_id"] = "public-aborted-target"
    target_probe["tool_use_id"] = "public-aborted-target-probe"
    aborted_target_response = invoke(hook, target_probe, workspace, abort_target, plugin); count += 1

    active_event = json.loads(json.dumps(event)); active_event["session_id"] = "public-handoff-crash"
    active_event["tool_use_id"] = "public-handoff-crash-active"
    active = invoke(hook, active_event, workspace, source, plugin); count += 1
    secret_b = "public-crash-handoff-secret-000000002"
    prepare_b = {"handoff_id": "hof_public_crash_01", "target_instance_id": "public-recovery-two",
                 "handoff_secret": secret_b}
    offer_b, text_offer_b, code_offer_b = maintain(
        inspector, source, "--prepare-handoff", input_value=prepare_b)
    crash_target = source.parent / "public-crash-target"
    accept_b = {"offer": offer_b, "handoff_secret": secret_b}
    crash_text, crash_code = crash_partial_maintenance(
        inspector, crash_target, "--accept-handoff", accept_b)
    before, before_text, before_code = inspect(inspector, crash_target, "--handoffs", "--limit", "10")
    ack_b, text_ack_b, code_ack_b = maintain(
        inspector, crash_target, "--accept-handoff", input_value=accept_b)
    standby = invoke(hook, post_for(active_event), workspace, crash_target, plugin); count += 1
    finalize_b = {"ack": ack_b, "handoff_secret": secret_b}
    with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
        finalize_results = [future.result(timeout=12) for future in [
            pool.submit(maintain, inspector, source, "--finalize-handoff",
                        input_value=finalize_b) for _ in range(2)]]
    commit = finalize_results[0][0] if finalize_results else {}
    activate_b = {"commit": commit, "handoff_secret": secret_b}
    with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
        activate_results = [future.result(timeout=12) for future in [
            pool.submit(maintain_jsonl, inspector, crash_target, "--activate-handoff",
                        input_value=activate_b) for _ in range(2)]]
    target_post = invoke(hook, post_for(active_event), workspace, crash_target, plugin)
    source_post = invoke(hook, post_for(active_event), workspace, source, plugin)
    count += 2
    views, view_text, view_code = inspect(inspector, crash_target, "--handoffs", "--limit", "10")
    if (code_offer_a != 0 or code_ack_a != 0 or not handoff_ack_ok(ack_a)
            or code_abort_a != 0 or code_target_abort != 0
            or abort_post.get("hookSpecificOutput", {}).get("permissionDecision") != "allow"
            or aborted_target_response.get("hookSpecificOutput", {}).get("permissionDecision") != "deny"):
        failures.append("handoff abort/source recovery/standby discard")
    if (code_offer_b != 0 or not handoff_offer_ok(offer_b) or crash_code == 0
            or before_code != 0 or before != [] or code_ack_b != 0
            or standby.get("hookSpecificOutput", {}).get("permissionDecision") != "deny"
            or not finalize_results or not all(item[2] == 0 and item[0] == commit for item in finalize_results)
            or not handoff_commit_ok(commit) or not all(item[2] == 0 for item in activate_results)
            or target_post.get("hookSpecificOutput", {}).get("permissionDecision") != "allow"
            or source_post.get("hookSpecificOutput", {}).get("permissionDecision") != "deny"
            or view_code != 0 or not views or not all(handoff_view_ok(item) for item in views)):
        failures.append("handoff partial-accept/finalize/activate replay")
    observed = "".join((text_offer_a, text_ack_a, text_abort_a, text_target_abort,
                        text_offer_b, crash_text, before_text, text_ack_b, view_text,
                        *(item[1] for item in finalize_results),
                        *(item[1] for item in activate_results)))
    observed += json.dumps([abort_pre, abort_post, aborted_target_response, active,
                            standby, target_post, source_post], sort_keys=True)
    if secret_a in observed or secret_b in observed:
        failures.append("abort/crash handoff secret disclosure")
    return count, observed


def exercise_cycle7_delivery_upgrade(hook: Path, inspector: Path, workspace: Path,
                                     state: Path, plugin: Path, template: dict,
                                     policy_bytes: bytes, label: str,
                                     failures: list[str]) -> tuple[int, str]:
    count = 0
    event = json.loads(json.dumps(template)); event["session_id"] = f"public-{label}-effect"
    event["tool_use_id"] = f"public-{label}-effect"
    pre = invoke(hook, event, workspace, state, plugin); count += 1
    effect_id = str(pre.get("policyReceipt", {}).get("effect_id", ""))
    generation = pre.get("policyReceipt", {}).get("effect_generation")
    claim1_request = {"consumer_id": f"public-{label}-consumer-a",
                      "claim_id": f"clm_public_{label}_auth", "max_items": 1}
    claim1, claim1_text, claim1_code = maintain(
        inspector, state, "--claim-effects", input_value=claim1_request)
    post = invoke(hook, post_for(event, {"status": "ok", "authorization": "Bearer public-effect-secret-12345"}),
                  workspace, state, plugin); count += 1
    stale_request = {"claim_id": claim1.get("claim_id"), "claim_token": claim1.get("claim_token"),
                     "expected_effect_generation": generation,
                     "delivery_id": f"dlv_public_{label}_stale"}
    _stale, stale_text, stale_code = maintain_jsonl(
        inspector, state, "--ack-effect", effect_id, input_value=stale_request)
    claim2, claim2_text, claim2_code = maintain(
        inspector, state, "--claim-effects",
        input_value={"consumer_id": f"public-{label}-consumer-b",
                     "claim_id": f"clm_public_{label}_complete", "max_items": 1})
    nack_request = {"claim_id": claim2.get("claim_id"), "claim_token": claim2.get("claim_token"),
                    "expected_effect_generation": 2, "retry_id": f"rty_public_{label}_one"}
    _nack, nack_text, nack_code = maintain_jsonl(
        inspector, state, "--nack-effect", effect_id, input_value=nack_request)
    claim3, claim3_text, claim3_code = maintain(
        inspector, state, "--claim-effects",
        input_value={"consumer_id": f"public-{label}-consumer-a",
                     "claim_id": f"clm_public_{label}_retry", "max_items": 1})
    ack_request = {"claim_id": claim3.get("claim_id"), "claim_token": claim3.get("claim_token"),
                   "expected_effect_generation": 2, "delivery_id": f"dlv_public_{label}_complete"}
    _ack, ack_text, ack_code = maintain_jsonl(
        inspector, state, "--ack-effect", effect_id, input_value=ack_request)
    _ack_replay, ack_replay_text, ack_replay_code = maintain_jsonl(
        inspector, state, "--ack-effect", effect_id, input_value=ack_request)

    held = json.loads(json.dumps(template)); held["session_id"] = f"public-{label}-held"
    held["tool_use_id"] = f"public-{label}-held"
    held_pre = invoke(hook, held, workspace, state, plugin); count += 1
    secret = f"public-upgrade-secret-{label}-000000000000"
    begin_request = {"upgrade_id": f"upg_public_{label}_01", "target_format_version": 2,
                     "expected_upgrade_lineage_digest": "sha256:" + "0" * 64,
                     "upgrade_secret": secret}
    manifest, begin_text, begin_code = maintain(
        inspector, state, "--begin-upgrade", input_value=begin_request)
    statuses: list[dict] = []; cursor = 0; advance_texts: list[str] = []
    for _ in range(30):
        status, status_text, status_code = maintain(
            inspector, state, "--advance-upgrade", begin_request["upgrade_id"],
            input_value={"manifest": manifest, "upgrade_secret": secret,
                         "expected_cursor_sequence": cursor})
        advance_texts.append(status_text)
        if status_code != 0 or not status:
            break
        statuses.append(status); cursor = int(status.get("cursor_sequence", cursor))
        if status.get("ready_to_commit"):
            break
    held_post = invoke(hook, post_for(held), workspace, state, plugin); count += 1
    stale_commit, stale_commit_text, stale_commit_code = maintain(
        inspector, state, "--commit-upgrade",
        input_value={"status": statuses[-1] if statuses else {}, "upgrade_secret": secret})
    for _ in range(30):
        status, status_text, status_code = maintain(
            inspector, state, "--advance-upgrade", begin_request["upgrade_id"],
            input_value={"manifest": manifest, "upgrade_secret": secret,
                         "expected_cursor_sequence": cursor})
        advance_texts.append(status_text)
        if status_code != 0 or not status:
            break
        statuses.append(status); cursor = int(status.get("cursor_sequence", cursor))
        if status.get("ready_to_commit"):
            break
    commit_request = {"status": statuses[-1] if statuses else {}, "upgrade_secret": secret}
    commit, commit_text, commit_code = maintain(
        inspector, state, "--commit-upgrade", input_value=commit_request)
    replay, replay_text, replay_code = maintain(
        inspector, state, "--commit-upgrade", input_value=commit_request)
    probe = json.loads(json.dumps(template)); probe["session_id"] = f"public-{label}-format2"
    probe["tool_use_id"] = f"public-{label}-format2"
    format2 = invoke(hook, probe, workspace, state, plugin); count += 1
    format2_post = invoke(hook, post_for(probe), workspace, state, plugin); count += 1
    downgrade = {"upgrade_id": f"upg_public_{label}_down", "target_format_version": 1,
                 "expected_upgrade_lineage_digest": commit.get("upgrade_lineage_digest"),
                 "upgrade_secret": secret}
    _down, down_text, down_code = maintain(inspector, state, "--begin-upgrade", input_value=downgrade)
    effects, effect_text, effect_code = inspect(inspector, state, "--effects", "--limit", "20")
    upgrades, upgrade_text, upgrade_code = inspect(inspector, state, "--upgrades", "--limit", "20")
    if (pre.get("hookSpecificOutput", {}).get("permissionDecision") != "allow"
            or generation != 1 or claim1_code != 0 or not effect_claim_ok(claim1)
            or post.get("policyReceipt", {}).get("effect_id") != effect_id
            or post.get("policyReceipt", {}).get("effect_generation") != 2
            or stale_code == 0 or claim2_code != 0 or nack_code != 0
            or claim3_code != 0 or ack_code != 0 or ack_replay_code != 0):
        failures.append("cycle7 durable effect claim/generation/retry")
    if (begin_code != 0 or not upgrade_manifest_ok(manifest) or len(statuses) < 2
            or not all(upgrade_status_ok(item) for item in statuses)
            or stale_commit_code == 0 or commit_code != 0 or replay_code != 0
            or not upgrade_commit_ok(commit) or replay != commit or down_code == 0
            or format2.get("policyReceipt", {}).get("state_format_version") != 2
            or not same_lifecycle(format2, format2_post)):
        failures.append("cycle7 bounded upgrade/catchup/commit/replay")
    if (effect_code != 0 or not effects or not all(effect_record_ok(item) for item in effects)
            or upgrade_code != 0 or not upgrades or not all(upgrade_view_ok(item) for item in upgrades)):
        failures.append("cycle7 exact effect/upgrade views")
    observed = json.dumps([pre, post, held_pre, held_post, stale_commit, format2, format2_post], sort_keys=True)
    observed += "".join((stale_text, nack_text, ack_text, ack_replay_text, begin_text,
                         *advance_texts, stale_commit_text, commit_text, replay_text,
                         down_text, effect_text, upgrade_text))
    for claim in (claim1, claim2, claim3):
        token = str(claim.get("claim_token", ""))
        if token and token in observed:
            failures.append("cycle7 claim token disclosure")
    if secret in observed or "public-effect-secret-12345" in observed:
        failures.append("cycle7 upgrade/effect secret disclosure")
    return count, observed


def damage_interior_journal(state: Path) -> bool:
    candidates = [state / "audit.snapshot.jsonl", state / "audit.jsonl"]
    selected: tuple[Path, list[bytes]] | None = None
    for path in candidates:
        if not path.is_file():
            continue
        lines = path.read_bytes().splitlines(keepends=True)
        if len(lines) >= 3 and (selected is None or len(lines) > len(selected[1])):
            selected = (path, lines)
    if selected is None:
        return False
    path, lines = selected
    lines[-2] = b'{"schema_version":1,"kind":"damaged-interior"\n'
    path.write_bytes(b"".join(lines))
    return True


def crash_partial_repair(inspector: Path, state: Path, repair_id: str,
                         request: dict) -> tuple[str, int]:
    env = {**os.environ, "PYTHONDONTWRITEBYTECODE": "1"}
    process = subprocess.Popen(
        [str(inspector), "--state-dir", str(state), "--advance-repair", repair_id,
         "--limit", "3"], text=True, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
        stderr=subprocess.PIPE, env=env, start_new_session=True,
    )
    payload = json.dumps(request)
    assert process.stdin is not None
    process.stdin.write(payload[:max(1, len(payload) // 2)])
    process.stdin.flush()
    try:
        os.killpg(process.pid, signal.SIGKILL)
    except ProcessLookupError:
        pass
    stdout, stderr = process.communicate()
    return stdout + stderr, process.returncode


def exercise_cycle8_repair_continuation(
        hook: Path, inspector: Path, workspace: Path, state: Path, plugin: Path,
        template: dict, policy_bytes: bytes, label: str, failures: list[str]) -> tuple[int, str]:
    count = 0
    source = json.loads(json.dumps(template)); source["session_id"] = f"public-{label}-source"
    source["tool_use_id"] = f"public-{label}-source-base"
    source_pre = invoke(hook, source, workspace, state, plugin); count += 1
    authority_id = source_pre.get("policyReceipt", {}).get("session_authority_id")
    generation = source_pre.get("policyReceipt", {}).get("session_generation")
    expected_generation = generation if isinstance(generation, int) else 0
    secret = f"public-continuation-secret-{label}-000000000"
    issue_request = {
        "continuation_id": f"ctn_public_{label}_success",
        "source_session_id": source["session_id"],
        "target_session_id": f"public-{label}-target",
        "expected_session_generation": expected_generation,
        "continuation_secret": secret,
    }
    token, _token_text, token_code = maintain(
        inspector, state, "--issue-continuation", input_value=issue_request)
    token_replay, _token_replay_text, token_replay_code = maintain(
        inspector, state, "--issue-continuation", input_value=issue_request)
    source_post = invoke(hook, post_for(source), workspace, state, plugin); count += 1
    source_blocked = json.loads(json.dumps(template)); source_blocked["session_id"] = source["session_id"]
    source_blocked["tool_use_id"] = f"public-{label}-source-blocked"
    blocked = invoke(hook, source_blocked, workspace, state, plugin); count += 1
    wrong = json.loads(json.dumps(template)); wrong["session_id"] = f"public-{label}-wrong"
    wrong["tool_use_id"] = f"public-{label}-wrong-target"
    wrong.setdefault("tool_input", {})["_policy_continuation"] = token
    wrong_response = invoke(hook, wrong, workspace, state, plugin); count += 1
    target = json.loads(json.dumps(template)); target["session_id"] = f"public-{label}-target"
    target["tool_use_id"] = f"public-{label}-target-activate"
    target.setdefault("tool_input", {})["_policy_continuation"] = token
    with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
        activated = [future.result(timeout=12) for future in [
            pool.submit(invoke, hook, target, workspace, state, plugin) for _ in range(2)]]
    count += 2
    target_follow = json.loads(json.dumps(template)); target_follow["session_id"] = target["session_id"]
    target_follow["tool_use_id"] = f"public-{label}-target-follow"
    follow = invoke(hook, target_follow, workspace, state, plugin); count += 1
    reused = json.loads(json.dumps(target)); reused["tool_use_id"] = f"public-{label}-token-reuse"
    reused_response = invoke(hook, reused, workspace, state, plugin); count += 1

    cancel_secret = f"public-cancel-secret-{label}-0000000000000"
    cancel_request = {
        "continuation_id": f"ctn_public_{label}_cancel",
        "source_session_id": target["session_id"],
        "target_session_id": f"public-{label}-cancel-target",
        "expected_session_generation": (activated[0].get("policyReceipt", {})
                                        .get("session_generation")),
        "continuation_secret": cancel_secret,
    }
    cancel_token, _cancel_token_text, cancel_issue_code = maintain(
        inspector, state, "--issue-continuation", input_value=cancel_request)
    cancel_input = {"expected_session_generation": cancel_request["expected_session_generation"],
                    "continuation_secret": cancel_secret}
    _cancelled, cancel_text, cancel_code = maintain_jsonl(
        inspector, state, "--cancel-continuation", cancel_request["continuation_id"],
        input_value=cancel_input)
    _cancel_replay, cancel_replay_text, cancel_replay_code = maintain_jsonl(
        inspector, state, "--cancel-continuation", cancel_request["continuation_id"],
        input_value=cancel_input)
    resumed = json.loads(json.dumps(template)); resumed["session_id"] = target["session_id"]
    resumed["tool_use_id"] = f"public-{label}-cancel-resumed"
    resumed_response = invoke(hook, resumed, workspace, state, plugin); count += 1

    stale_secret = f"public-stale-secret-{label}-00000000000000"
    stale_request = {
        "continuation_id": f"ctn_public_{label}_repair_stale",
        "source_session_id": target["session_id"],
        "target_session_id": f"public-{label}-stale-target",
        "expected_session_generation": resumed_response.get("policyReceipt", {}).get("session_generation"),
        "continuation_secret": stale_secret,
    }
    stale_token, _stale_token_text, stale_issue_code = maintain(
        inspector, state, "--issue-continuation", input_value=stale_request)
    damaged = damage_interior_journal(state)
    quarantine_probe = json.loads(json.dumps(template)); quarantine_probe["session_id"] = target["session_id"]
    quarantine_probe["tool_use_id"] = f"public-{label}-quarantine"
    quarantined = invoke(hook, quarantine_probe, workspace, state, plugin); count += 1

    repair_secret = f"public-repair-secret-{label}-0000000000000"
    begin_request = {"repair_id": f"rpr_public_{label}_01",
                     "expected_repair_generation": 0, "repair_secret": repair_secret}
    manifest, begin_text, begin_code = maintain(
        inspector, state, "--begin-repair", input_value=begin_request)
    initial_advance = {"manifest": manifest, "repair_secret": repair_secret,
                       "expected_candidate_cursor": 0}
    crash_text = ""; crash_code = -1
    if label == "two" and manifest:
        crash_text, crash_code = crash_partial_repair(
            inspector, state, begin_request["repair_id"], initial_advance)
    statuses: list[dict] = []; cursor = 0; advance_texts: list[str] = []
    for _ in range(40):
        status, status_text, status_code = maintain(
            inspector, state, "--advance-repair", begin_request["repair_id"],
            "--limit", "3", input_value={"manifest": manifest,
                "repair_secret": repair_secret, "expected_candidate_cursor": cursor})
        advance_texts.append(status_text)
        if status_code != 0 or not status:
            break
        statuses.append(status); cursor = int(status.get("candidate_cursor", cursor))
        if status.get("ready_for_verification"):
            break
    ready = statuses[-1] if statuses else {}
    attestations: list[dict] = []; attestation_texts: list[str] = []
    for verifier in ("public-repair-a", "public-repair-b"):
        request = {"status": ready, "repair_secret": repair_secret,
                   "verifier_id": verifier,
                   "verifier_secret": f"public-verifier-secret-{verifier}-000000"}
        attestation, attestation_text, code = maintain(
            inspector, state, "--verify-repair", begin_request["repair_id"],
            input_value=request)
        attestation_texts.append(attestation_text)
        if code == 0:
            attestations.append(attestation)
    replay_request = {"status": ready, "repair_secret": repair_secret,
                      "verifier_id": "public-repair-a",
                      "verifier_secret": "public-verifier-secret-public-repair-a-000000"}
    attestation_replay, attestation_replay_text, attestation_replay_code = maintain(
        inspector, state, "--verify-repair", begin_request["repair_id"],
        input_value=replay_request)
    _unknown, unknown_text, unknown_code = maintain(
        inspector, state, "--verify-repair", begin_request["repair_id"],
        input_value={"status": ready, "repair_secret": repair_secret,
                     "verifier_id": "public-unknown",
                     "verifier_secret": "public-unknown-secret-0000000000000"})
    commit_request = {"status": ready, "repair_secret": repair_secret,
                      "attestations": attestations}
    commit, commit_text, commit_code = maintain(
        inspector, state, "--commit-repair", input_value=commit_request)
    commit_replay, commit_replay_text, commit_replay_code = maintain(
        inspector, state, "--commit-repair", input_value=commit_request)
    stale_target = json.loads(json.dumps(template)); stale_target["session_id"] = stale_request["target_session_id"]
    stale_target["tool_use_id"] = f"public-{label}-stale-after-repair"
    stale_target.setdefault("tool_input", {})["_policy_continuation"] = stale_token
    stale_response = invoke(hook, stale_target, workspace, state, plugin); count += 1
    fresh = json.loads(json.dumps(template)); fresh["session_id"] = target["session_id"]
    fresh["tool_use_id"] = f"public-{label}-fresh-after-repair"
    fresh_response = invoke(hook, fresh, workspace, state, plugin); count += 1
    repairs, repair_view_text, repair_view_code = inspect(inspector, state, "--repairs", "--limit", "10")
    continuations, continuation_view_text, continuation_view_code = inspect(
        inspector, state, "--continuations", "--limit", "20")

    if (not same_lifecycle(source_pre, source_post) or not authority_id
            or not continuation_token_ok(token) or token_code != 0
            or token_replay_code != 0 or token_replay != token
            or blocked.get("hookSpecificOutput", {}).get("permissionDecision") != "deny"
            or wrong_response.get("hookSpecificOutput", {}).get("permissionDecision") != "deny"
            or len(activated) != 2 or activated[0] != activated[1]
            or activated[0].get("hookSpecificOutput", {}).get("permissionDecision") != "allow"
            or activated[0].get("policyReceipt", {}).get("session_authority_id") != authority_id
            or activated[0].get("policyReceipt", {}).get("session_generation") != expected_generation + 1
            or follow.get("hookSpecificOutput", {}).get("permissionDecision") != "allow"
            or reused_response.get("hookSpecificOutput", {}).get("permissionDecision") != "deny"):
        failures.append("cycle8 continuation issue/fence/activate/replay")
    if (cancel_issue_code != 0 or not continuation_token_ok(cancel_token)
            or cancel_code != 0 or cancel_replay_code != 0
            or resumed_response.get("hookSpecificOutput", {}).get("permissionDecision") != "allow"):
        failures.append("cycle8 continuation cancel/source recovery")
    if (not damaged or stale_issue_code != 0
            or quarantined.get("hookSpecificOutput", {}).get("permissionDecision") != "deny"
            or quarantined.get("policyReceipt", {}).get("integrity_state") != "quarantined"
            or begin_code != 0 or not repair_manifest_ok(manifest)
            or not statuses or not all(repair_status_ok(item) for item in statuses)
            or not ready.get("ready_for_verification") or len(attestations) != 2
            or not all(repair_attestation_ok(item) for item in attestations)
            or attestation_replay_code != 0 or attestation_replay != attestations[0]
            or unknown_code == 0 or commit_code != 0 or commit_replay_code != 0
            or not repair_commit_ok(commit) or commit_replay != commit
            or stale_response.get("hookSpecificOutput", {}).get("permissionDecision") != "deny"
            or fresh_response.get("hookSpecificOutput", {}).get("permissionDecision") != "allow"
            or fresh_response.get("policyReceipt", {}).get("repair_generation") != 1
            or fresh_response.get("policyReceipt", {}).get("integrity_state") != "healthy"):
        failures.append("cycle8 quarantine/rebuild/attest/commit/recovery")
    if label == "two" and crash_code == 0:
        failures.append("cycle8 partial repair process did not die")
    if (repair_view_code != 0 or not repairs or not all(repair_view_ok(item) for item in repairs)
            or continuation_view_code != 0 or not continuations
            or not all(continuation_view_ok(item) for item in continuations)):
        failures.append("cycle8 exact repair/continuation views")
    observed = json.dumps([source_pre, source_post, blocked, wrong_response, *activated,
                           follow, reused_response, resumed_response, quarantined,
                           stale_response, fresh_response], sort_keys=True)
    observed += "".join((cancel_text, cancel_replay_text, begin_text, crash_text,
                          *advance_texts, *attestation_texts, attestation_replay_text,
                          unknown_text, commit_text, commit_replay_text,
                          repair_view_text, continuation_view_text))
    bearer_values = [str(item.get("token", "")) for item in (token, cancel_token, stale_token)
                     if item.get("token")]
    secret_values = [secret, cancel_secret, stale_secret, repair_secret,
                     "public-verifier-secret-public-repair-a-000000",
                     "public-verifier-secret-public-repair-b-000000"]
    if any(item in observed for item in [*bearer_values, *secret_values]):
        failures.append("cycle8 repair/continuation bearer disclosure")
    return count, observed


def run_dev_001(hook: Path, inspector: Path, workspace: Path, state: Path,
                plugin: Path, assets: Path, failures: list[str]) -> tuple[int, str]:
    policy_bytes = (assets / "policy.json").read_bytes()
    (workspace / ".claude" / "policy-provenance.json").write_bytes(policy_bytes)
    events = json.loads((assets / "events.json").read_text(encoding="utf-8"))
    response_count = 0
    # Use two bounded duplicate cohorts. This still exercises independent
    # processes and exactly-once identity, without making public success depend
    # on an eight-writer lock convoy.
    with concurrent.futures.ThreadPoolExecutor(max_workers=4) as pool:
        first = [pool.submit(invoke, hook, events[0], workspace, state, plugin) for _ in range(4)]
        concurrent_responses = [future.result(timeout=12) for future in first]
    with concurrent.futures.ThreadPoolExecutor(max_workers=4) as pool:
        second = [pool.submit(invoke, hook, events[0], workspace, state, plugin) for _ in range(4)]
        concurrent_responses += [future.result(timeout=12) for future in second]
    response_count += len(concurrent_responses)
    for response in concurrent_responses:
        add_response_checks(failures, response, events[0], policy_bytes)
        if response.get("policyReceipt", {}).get("reservation_state") != "reserved":
            failures.append("concurrent duplicate did not reserve")
    identities = {json.dumps(response.get("policyReceipt", {}), sort_keys=True) for response in concurrent_responses}
    if len(identities) != 1:
        failures.append("concurrent duplicate receipt mismatch")

    capacity_event = dict(events[0])
    capacity_event["tool_use_id"] = "same-session-capacity"
    capacity_event["tool_input"] = {"file_path": "src/capacity.py", "content": "held"}
    capacity_event["expect"] = "deny"
    capacity = invoke(hook, capacity_event, workspace, state, plugin)
    response_count += 1
    add_response_checks(failures, capacity, capacity_event, policy_bytes)
    if capacity.get("policyReceipt", {}).get("reservation_state") != "rejected":
        failures.append("same-session quota did not reject")

    conflict = dict(events[0])
    conflict["tool_input"] = {"file_path": "tmp/conflict.py", "content": "different"}
    conflict_response = invoke(hook, conflict, workspace, state, plugin)
    response_count += 1
    if conflict_response.get("hookSpecificOutput", {}).get("permissionDecision") != "deny":
        failures.append("same-identity conflict not denied")
    if conflict_response.get("policyReceipt") != concurrent_responses[0].get("policyReceipt"):
        failures.append("same-identity conflict replaced receipt")

    held_post_event = post_for(events[0])
    held_post = invoke(hook, held_post_event, workspace, state, plugin)
    duplicate_post = invoke(hook, held_post_event, workspace, state, plugin)
    response_count += 2
    if (held_post.get("hookSpecificOutput", {}).get("permissionDecision") != "allow"
            or held_post.get("policyReceipt", {}).get("reservation_state") != "settled"
            or not same_lifecycle(concurrent_responses[0], held_post)
            or duplicate_post != held_post):
        failures.append("held reservation settlement/idempotency")

    baseline: dict[str, dict] = {}
    for event in events[1:]:
        response = invoke(hook, event, workspace, state, plugin)
        response_count += 1
        baseline[event["tool_use_id"]] = response
        add_response_checks(failures, response, event, policy_bytes)
        state_value = response.get("policyReceipt", {}).get("reservation_state")
        if event.get("expect") == "allow":
            if state_value != "reserved":
                failures.append(event["tool_use_id"] + ": missing reservation")
            post_event = post_for(event)
            post_response = invoke(hook, post_event, workspace, state, plugin)
            response_count += 1
            if (post_response.get("hookSpecificOutput", {}).get("permissionDecision") != "allow"
                    or post_response.get("policyReceipt", {}).get("reservation_state") != "settled"
                    or not same_lifecycle(response, post_response)):
                failures.append(event["tool_use_id"] + ": settlement")
        elif state_value != "rejected":
            failures.append(event["tool_use_id"] + ": denied reservation state")
    if (baseline["shell-authority-a"].get("policyReceipt", {}).get("canonical_action_digest")
            != baseline["shell-authority-b"].get("policyReceipt", {}).get("canonical_action_digest")):
        failures.append("nested redirect canonical equivalence")

    stress_pre: list[dict] = []
    for index in range(4):
        pre = dict(events[0]); pre["session_id"] = f"public-stress-{index}"
        pre["tool_use_id"] = f"stress-{index}"
        pre["tool_input"] = {"file_path": f"src/stress-{index}.py", "content": "ok"}
        stress_pre.append(pre)
    # Four writers and one bounded maintenance pair overlap, while writer
    # concurrency itself remains capped at four.
    with concurrent.futures.ThreadPoolExecutor(max_workers=6) as pool:
        pre_futures = [pool.submit(invoke, hook, event, workspace, state, plugin) for event in stress_pre]
        leases_future = pool.submit(inspect, inspector, state, "--leases", "--limit", "20")
        reconcile_future = pool.submit(inspect, inspector, state, "--reconcile", "--limit", "20")
        stress_pre_responses = [future.result(timeout=12) for future in pre_futures]
        concurrent_leases = leases_future.result(timeout=12)
        concurrent_reconcile = reconcile_future.result(timeout=12)
    response_count += len(stress_pre_responses)
    allowed_pairs = [(event, response) for event, response in zip(stress_pre, stress_pre_responses)
                     if response.get("hookSpecificOutput", {}).get("permissionDecision") == "allow"]
    denied_pairs = [(event, response) for event, response in zip(stress_pre, stress_pre_responses)
                    if response.get("hookSpecificOutput", {}).get("permissionDecision") == "deny"]
    if (len(allowed_pairs) != 2 or len(denied_pairs) != 2
            or any(response.get("policyReceipt", {}).get("reservation_state") != "reserved" for _, response in allowed_pairs)
            or any(response.get("policyReceipt", {}).get("reservation_state") != "rejected" for _, response in denied_pairs)):
        failures.append("global reservation contention")
    if (concurrent_leases[2] != 0 or not all(lease_record_ok(item) for item in concurrent_leases[0])
            or concurrent_reconcile[2] != 0):
        failures.append("concurrent lease/reconcile inspection")
    for event, pre_response in allowed_pairs:
        post_event = post_for(event)
        post_response = invoke(hook, post_event, workspace, state, plugin)
        duplicate = invoke(hook, post_event, workspace, state, plugin)
        response_count += 2
        if (post_response.get("policyReceipt", {}).get("reservation_state") != "settled"
                or not same_lifecycle(pre_response, post_response) or duplicate != post_response):
            failures.append(event["tool_use_id"] + ": stress settlement")

    lines, output, code = inspect(inspector, state)
    leases, lease_text, lease_code = inspect(inspector, state, "--leases", "--limit", "20")
    write_lines = [line for line in lines if line.get("event_id") == "write-1" and line.get("kind") == "pre"]
    if code != 0 or len(lines) != 18 or len(write_lines) != 1 or not valid_chain(lines):
        failures.append(f"concurrent audit lifecycle count={len(lines)} write={len(write_lines)}")
    if (lease_code != 0 or len(leases) != 6 or not all(lease_record_ok(item) for item in leases)
            or any(item.get("state") != "settled" for item in leases)
            or [item.get("reservation_id") for item in leases] != sorted(item.get("reservation_id") for item in leases)):
        failures.append(f"redacted settled lease inventory={len(leases)}")
    window, window_text, window_code = inspect(inspector, state, "--after-sequence", "6", "--limit", "5")
    if window_code != 0 or [item.get("sequence") for item in window] != [7, 8, 9, 10, 11]:
        failures.append("bounded ordered inspection window")

    approval_event = {
        "session_id": "public-approval", "hook_event_name": "PreToolUse",
        "tool_name": "WebFetch", "tool_use_id": "approval-release",
        "tool_input": {"url": "https://example.test/release"},
        "expect": "ask", "expect_decision": "approval_required",
    }
    pending = invoke(hook, approval_event, workspace, state, plugin)
    response_count += 1
    add_response_checks(failures, pending, approval_event, policy_bytes)
    pending_receipt = pending.get("policyReceipt", {})
    ticket_id = pending_receipt.get("approval_ticket_id")
    if (pending_receipt.get("approval_state") != "pending"
            or pending_receipt.get("reservation_state") != "rejected"):
        failures.append("approval ticket establishment")

    def enroll(principal: str, hosts: list[str]) -> tuple[dict, str, int]:
        claims = {"principal_id": principal, "policy_id": "public-workspace",
                  "policy_epoch": 1, "tools": ["WebFetch"],
                  "path_prefixes": [], "hosts": hosts}
        return maintain(inspector, state, "--enroll-approver", input_value=claims)

    release_token, release_text, release_code = enroll("public-release", ["example.test"])
    security_token, security_text, security_code = enroll("public-security", ["example.test"])
    owner_token, owner_text, owner_code = enroll("public-owner", ["blocked.test"])
    owner_vote = {"ticket_id": ticket_id, "vote_id": "public-owner-vote",
                  "principal_token": owner_token}
    _owner_view, owner_vote_text, owner_vote_code = maintain_jsonl(
        inspector, state, "--cast-approval", input_value=owner_vote)
    first_vote = {"ticket_id": ticket_id, "vote_id": "public-release-vote",
                  "principal_token": release_token}
    _first_view, first_vote_text, first_vote_code = maintain_jsonl(
        inspector, state, "--cast-approval", input_value=first_vote)
    _replay_view, replay_text, replay_code = maintain_jsonl(
        inspector, state, "--cast-approval", input_value=first_vote)
    second_vote = {"ticket_id": ticket_id, "vote_id": "public-security-vote",
                   "principal_token": security_token}
    _second_view, second_vote_text, second_vote_code = maintain_jsonl(
        inspector, state, "--cast-approval", input_value=second_vote)
    if (release_code != 0 or security_code != 0 or owner_code != 0
            or owner_vote_code == 0 or first_vote_code != 0
            or replay_code != 0 or second_vote_code != 0):
        failures.append("scoped threshold approval votes")

    consume_event = dict(approval_event)
    consume_event["expect"] = "allow"; consume_event["expect_decision"] = "allow"
    with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
        consumed = [future.result(timeout=12) for future in
                    [pool.submit(invoke, hook, consume_event, workspace, state, plugin)
                     for _ in range(2)]]
    response_count += 2
    for response in consumed:
        add_response_checks(failures, response, consume_event, policy_bytes)
    if (consumed[0] != consumed[1]
            or consumed[0].get("policyReceipt", {}).get("approval_state") != "consumed"
            or consumed[0].get("policyReceipt", {}).get("reservation_state") != "reserved"):
        failures.append("concurrent approval consume")
    approval_post = invoke(hook, post_for(consume_event), workspace, state, plugin)
    response_count += 1
    if (not same_lifecycle(consumed[0], approval_post)
            or approval_post.get("policyReceipt", {}).get("reservation_state") != "settled"):
        failures.append("approved action settlement")
    approvals, approval_text, approval_code = inspect(inspector, state, "--approvals", "--limit", "10")
    if (approval_code != 0 or len(approvals) != 1
            or not approval_record_ok(approvals[0])
            or approvals[0].get("state") != "consumed"
            or approvals[0].get("principal_ids") != ["public-release", "public-security"]):
        failures.append("bounded approval view")

    supersede_event = json.loads(json.dumps(approval_event))
    supersede_event["tool_use_id"] = "approval-supersede"
    pending_round = invoke(hook, supersede_event, workspace, state, plugin)
    response_count += 1
    round_ticket = pending_round.get("policyReceipt", {}).get("approval_ticket_id")
    _rotated_owner, _rotate_owner_text, rotate_owner_code = inspect(
        inspector, state, "--rotate-approver", "public-owner")
    owner_scoped_token, owner_scoped_text, owner_scoped_code = enroll(
        "public-owner", ["example.test"])
    for vote_id, token in (("public-round-release", release_token),
                           ("public-round-owner", owner_scoped_token)):
        _view, _text, code = maintain_jsonl(
            inspector, state, "--cast-approval",
            input_value={"ticket_id": round_ticket, "vote_id": vote_id,
                         "principal_token": token})
        if code != 0:
            failures.append("same-group pre-supersession vote")
    before_supersede, _, _ = inspect(inspector, state, "--approvals", "--limit", "10")
    requests = [
        {"supersession_id": f"sup_public_round_{index}", "expected_approval_round": 1}
        for index in range(2)
    ]
    with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
        supersede_results = [future.result(timeout=12) for future in [
            pool.submit(maintain_jsonl, inspector, state, "--supersede-approval",
                        str(round_ticket), input_value=request) for request in requests]]
    winners = [index for index, item in enumerate(supersede_results) if item[2] == 0]
    if len(winners) == 1:
        winner_request = requests[winners[0]]
        _sup_replay, sup_replay_text, sup_replay_code = maintain_jsonl(
            inspector, state, "--supersede-approval", str(round_ticket),
            input_value=winner_request)
    else:
        sup_replay_text = ""; sup_replay_code = -1
    after_supersede, supersede_view_text, supersede_view_code = inspect(
        inspector, state, "--approvals", "--limit", "10")
    successor_records = [item for item in after_supersede
                         if item.get("predecessor_ticket_id") == round_ticket]
    successor_ticket = successor_records[0].get("ticket_id") if len(successor_records) == 1 else None
    stale_vote = {"ticket_id": round_ticket, "vote_id": "public-stale-security",
                  "principal_token": security_token}
    _stale_view, stale_vote_text, stale_vote_code = maintain_jsonl(
        inspector, state, "--cast-approval", input_value=stale_vote)
    for vote_id, token in (("public-successor-release", release_token),
                           ("public-successor-security", security_token)):
        _view, _text, code = maintain_jsonl(
            inspector, state, "--cast-approval",
            input_value={"ticket_id": successor_ticket, "vote_id": vote_id,
                         "principal_token": token})
        if code != 0:
            failures.append("successor duty vote")
    superseded_consumed = invoke(hook, supersede_event, workspace, state, plugin)
    superseded_post = invoke(hook, post_for(supersede_event), workspace, state, plugin)
    response_count += 2
    if (not before_supersede or before_supersede[-1].get("state") != "pending"
            or rotate_owner_code != 0 or owner_scoped_code != 0 or len(winners) != 1
            or sup_replay_code != 0 or supersede_view_code != 0
            or len(successor_records) != 1 or successor_records[0].get("approval_count") != 0
            or successor_records[0].get("satisfied_groups") != [] or stale_vote_code == 0
            or superseded_consumed.get("hookSpecificOutput", {}).get("permissionDecision") != "allow"
            or superseded_consumed.get("policyReceipt", {}).get("approval_round") != 2
            or not same_lifecycle(superseded_consumed, superseded_post)):
        failures.append("duty groups/supersession/replay")

    transfer = {"transfer_id": "mig_public_dev_one", "transfer_secret": "public-transfer-secret-000000000001"}
    checkpoint, checkpoint_text, checkpoint_code = maintain(
        inspector, state, "--export-checkpoint", input_value=transfer)
    recovery_state = state.parent / "recovery-state"
    import_request = {"checkpoint": checkpoint, "transfer_secret": transfer["transfer_secret"],
                      "mode": "replace", "expected_lineage_digest": None}
    imported, import_text, import_code = maintain_jsonl(
        inspector, recovery_state, "--import-checkpoint", input_value=import_request)
    before_replay, _, _ = inspect(inspector, recovery_state)
    _replayed, replay_import_text, replay_import_code = maintain_jsonl(
        inspector, recovery_state, "--import-checkpoint", input_value=import_request)
    after_replay, _, _ = inspect(inspector, recovery_state)
    tampered = json.loads(json.dumps(checkpoint)) if checkpoint else {}
    if tampered:
        tampered["receipt_count"] = int(tampered.get("receipt_count", 0)) + 1
    tamper_request = {"checkpoint": tampered, "transfer_secret": transfer["transfer_secret"],
                      "mode": "replace", "expected_lineage_digest": None}
    _tamper, tamper_text, tamper_code = maintain_jsonl(
        inspector, recovery_state, "--import-checkpoint", input_value=tamper_request)
    recovered_approvals, recovered_approval_text, recovered_approval_code = inspect(
        inspector, recovery_state, "--approvals", "--limit", "10")
    recovered_leases, recovered_lease_text, recovered_lease_code = inspect(
        inspector, recovery_state, "--leases", "--limit", "20")
    checkpoint_view, checkpoint_view_text, checkpoint_view_code = inspect(
        inspector, recovery_state, "--checkpoints", "--limit", "10")
    if (checkpoint_code != 0 or not checkpoint_ok(checkpoint) or import_code != 0
            or replay_import_code != 0 or len(before_replay) != len(after_replay)
            or tamper_code == 0 or recovered_approval_code != 0
            or len(recovered_approvals) != 1 or recovered_approvals[0].get("state") != "consumed"
            or recovered_lease_code != 0 or not any(item.get("receipt_id") == consumed[0].get("policyReceipt", {}).get("receipt_id") for item in recovered_leases)
            or checkpoint_view_code != 0 or not checkpoint_view
            or not all(checkpoint_view_ok(item) for item in checkpoint_view)):
        failures.append("checkpoint replace/replay/tamper recovery")
    cycle8_count, cycle8_observed = exercise_cycle8_repair_continuation(
        hook, inspector, workspace, state, plugin, events[0], policy_bytes,
        "one", failures)
    response_count += cycle8_count
    cycle7_count, cycle7_observed = exercise_cycle7_delivery_upgrade(
        hook, inspector, workspace, state, plugin, events[0], policy_bytes,
        "one", failures)
    response_count += cycle7_count
    handoff_event = json.loads(json.dumps(events[0]))
    handoff_event["session_id"] = "public-handoff"
    handoff_event["tool_use_id"] = "public-handoff-active"
    handoff_event["tool_input"] = {"file_path": "src/handoff.py", "content": "handoff"}
    handoff_count, handoff_observed = exercise_handoff(
        hook, inspector, workspace, state, plugin, handoff_event, policy_bytes,
        "public-recovery", state.parent / "handoff-target", failures)
    response_count += handoff_count
    observed = (output + lease_text + window_text
                + owner_vote_text + first_vote_text + replay_text + second_vote_text
                + approval_text + checkpoint_text + import_text + replay_import_text
                + tamper_text + recovered_approval_text + recovered_lease_text
                + checkpoint_view_text + owner_scoped_text + sup_replay_text
                + supersede_view_text + stale_vote_text + cycle8_observed
                + cycle7_observed + handoff_observed
                + json.dumps([pending, *consumed, approval_post, pending_round,
                              superseded_consumed, superseded_post]))
    if any(token in observed for token in (str(release_token.get("token", "")),
                                           str(security_token.get("token", "")),
                                           str(owner_token.get("token", "")),
                                           str(owner_scoped_token.get("token", ""))) if token):
        failures.append("approver token disclosure")
    if transfer["transfer_secret"] in observed:
        failures.append("checkpoint transfer secret disclosure")
    return response_count, observed


def run_dev_002(hook: Path, inspector: Path, workspace: Path, state: Path,
                plugin: Path, assets: Path, failures: list[str]) -> tuple[int, str]:
    v1 = (assets / "policy_v1.json").read_bytes()
    v2 = (assets / "policy_v2.json").read_bytes()
    policy_path = workspace / ".claude" / "policy-provenance.json"
    policy_path.write_bytes(v1)
    events = json.loads((assets / "events.json").read_text(encoding="utf-8"))
    response_count = 0

    pre = invoke(hook, events[0], workspace, state, plugin)
    response_count += 1
    add_response_checks(failures, pre, events[0], v1)
    if pre.get("hookSpecificOutput", {}).get("updatedInput") != {"command": "printf offline-status"}:
        failures.append("rewrite updatedInput")
    if pre.get("policyReceipt", {}).get("reservation_state") != "reserved":
        failures.append("rewrite did not reserve")

    replacement = policy_path.with_suffix(".replacement")
    replacement.write_bytes(v2)
    os.replace(replacement, policy_path)
    post = invoke(hook, events[1], workspace, state, plugin)
    response_count += 1
    post_receipt = post.get("policyReceipt", {})
    pre_receipt = pre.get("policyReceipt", {})
    if post.get("hookSpecificOutput", {}).get("hookEventName") != "PostToolUse":
        failures.append("post hook event name")
    if post.get("hookSpecificOutput", {}).get("permissionDecision") != events[1]["expect"]:
        failures.append("post permission")
    if (not same_lifecycle(pre, post)
            or post_receipt.get("reservation_state") != "settled"
            or post_receipt.get("policy_epoch") != 10):
        failures.append("post did not preserve and settle the epoch-10 lease")

    audit = invoke(hook, events[2], workspace, state, plugin)
    response_count += 1
    add_response_checks(failures, audit, events[2], v2)
    if audit.get("policyReceipt", {}).get("policy_revision") != "public-reload-v2":
        failures.append("audit event did not use revision 2")
    audit_post_event = post_for(events[2])
    audit_post = invoke(hook, audit_post_event, workspace, state, plugin)
    response_count += 1
    if audit_post.get("policyReceipt", {}).get("reservation_state") != "settled" or not same_lifecycle(audit, audit_post):
        failures.append("audit-only reservation settlement")
    reload_event = dict(events[0])
    reload_event["tool_use_id"] = "rewrite-2"
    reload_event["expect"] = "deny"
    reload_event["expect_decision"] = "deny"
    reload_response = invoke(hook, reload_event, workspace, state, plugin)
    response_count += 1
    add_response_checks(failures, reload_response, reload_event, v2)
    if reload_response.get("policyReceipt", {}).get("policy_revision") != "public-reload-v2":
        failures.append("policy reload revision")

    policy_path.write_bytes(v1)
    rollback_event = dict(reload_event); rollback_event["tool_use_id"] = "rollback-probe"
    rollback_response = invoke(hook, rollback_event, workspace, state, plugin)
    response_count += 1
    add_response_checks(failures, rollback_response, rollback_event, v2)
    if rollback_response.get("policyReceipt", {}).get("policy_epoch") != 11:
        failures.append("policy epoch rollback was not rejected")
    policy_path.write_bytes(v2)

    root_claims = {"delegation_id": "public-root", "subject": "public-coordinator",
                   "policy_id": "public-reload", "policy_epoch": 11,
                   "tools": ["mcp__files__inspect"], "path_prefixes": ["src/"],
                   "hosts": ["example.test"]}
    root_token, root_text, root_code = maintain(inspector, state, "--mint-delegation", input_value=root_claims)
    child_claims = {"delegation_id": "public-child", "subject": "public-worker",
                    "policy_id": "public-reload", "policy_epoch": 11,
                    "tools": ["mcp__files__inspect"], "path_prefixes": ["src/public/"],
                    "hosts": ["example.test"], "parent_token": root_token}
    child_token, child_text, child_code = maintain(inspector, state, "--mint-delegation", input_value=child_claims)
    wider_claims = dict(child_claims); wider_claims["delegation_id"] = "public-wider"
    wider_claims["path_prefixes"] = ["tmp/"]
    _wider, wider_text, wider_code = maintain(inspector, state, "--mint-delegation", input_value=wider_claims)
    if root_code != 0 or child_code != 0 or not root_token or not child_token or wider_code == 0:
        failures.append("delegation mint/attenuation")
    delegated_event = json.loads(json.dumps(events[4]))
    delegated_event["tool_input"]["_policy_delegation"] = child_token
    delegated = invoke(hook, delegated_event, workspace, state, plugin)
    response_count += 1
    add_response_checks(failures, delegated, delegated_event, v2)
    delegated_receipt = delegated.get("policyReceipt", {})
    if delegated_receipt.get("delegation_id") != "public-child" or delegated_receipt.get("delegation_parent_id") != "public-root":
        failures.append("delegation receipt provenance")
    delegated_post_event = post_for(delegated_event)
    delegated_post = invoke(hook, delegated_post_event, workspace, state, plugin)
    response_count += 1
    if delegated_post.get("policyReceipt", {}).get("reservation_state") != "settled" or not same_lifecycle(delegated, delegated_post):
        failures.append("delegated reservation settlement")

    compacted, compact_text, compact_code = inspect(inspector, state, "--compact")
    if compact_code != 0 or len(compacted) != 8:
        failures.append(f"compaction evidence count={len(compacted)}")
    rotated, rotate_text, rotate_code = inspect(inspector, state, "--rotate-key")
    if rotate_code != 0 or len(rotated) != 8:
        failures.append(f"rotation evidence count={len(rotated)}")

    rotated_delegated_event = json.loads(json.dumps(delegated_event)); rotated_delegated_event["tool_use_id"] = "delegated-after-rotation"
    rotated_delegated = invoke(hook, rotated_delegated_event, workspace, state, plugin)
    response_count += 1
    add_response_checks(failures, rotated_delegated, rotated_delegated_event, v2)
    if rotated_delegated.get("hookSpecificOutput", {}).get("permissionDecision") != "allow":
        failures.append("old delegation did not survive key rotation")
    rotated_delegated_post = invoke(hook, post_for(rotated_delegated_event), workspace, state, plugin)
    response_count += 1
    if not same_lifecycle(rotated_delegated, rotated_delegated_post) or rotated_delegated_post.get("policyReceipt", {}).get("reservation_state") != "settled":
        failures.append("post-rotation delegated settlement")
    revoked_view, revoke_text, revoke_code = inspect(inspector, state, "--revoke-delegation", "public-child")
    revoked_event = json.loads(json.dumps(delegated_event)); revoked_event["tool_use_id"] = "delegated-after-revoke"
    revoked = invoke(hook, revoked_event, workspace, state, plugin)
    response_count += 1
    if revoke_code != 0 or len(revoked_view) != 10 or revoked.get("hookSpecificOutput", {}).get("permissionDecision") != "deny":
        failures.append("durable delegation revocation")
    rotated_read = invoke(hook, events[3], workspace, state, plugin)
    response_count += 1
    add_response_checks(failures, rotated_read, events[3], v2)
    if rotated_read.get("policyReceipt", {}).get("key_id") == pre_receipt.get("key_id"):
        failures.append("key rotation did not change key_id")
    rotated_read_post = invoke(hook, post_for(events[3]), workspace, state, plugin)
    response_count += 1
    if not same_lifecycle(rotated_read, rotated_read_post) or rotated_read_post.get("policyReceipt", {}).get("reservation_state") != "settled":
        failures.append("rotated Read settlement")

    race_events: list[dict] = []
    for index in range(2):
        event = json.loads(json.dumps(events[3]))
        event["session_id"] = f"public-lease-race-{index}"
        event["tool_use_id"] = f"lease-race-{index}"
        race_events.append(event)
    # The two-writer race overlaps one two-process maintenance pair.
    with concurrent.futures.ThreadPoolExecutor(max_workers=4) as pool:
        race_futures = [pool.submit(invoke, hook, event, workspace, state, plugin) for event in race_events]
        leases_future = pool.submit(inspect, inspector, state, "--leases", "--limit", "20")
        reconcile_future = pool.submit(inspect, inspector, state, "--reconcile", "--limit", "20")
        race_responses = [future.result(timeout=12) for future in race_futures]
        concurrent_leases = leases_future.result(timeout=12)
        concurrent_reconcile = reconcile_future.result(timeout=12)
    response_count += 2
    if (not all(permission.get("hookSpecificOutput", {}).get("permissionDecision") == "allow" for permission in race_responses)
            or not all(response.get("policyReceipt", {}).get("reservation_state") == "reserved" for response in race_responses)
            or concurrent_leases[2] != 0 or concurrent_reconcile[2] != 0):
        failures.append("epoch-11 reservation race/maintenance")
    race_settled = invoke(hook, post_for(race_events[0]), workspace, state, plugin)
    response_count += 1
    if not same_lifecycle(race_responses[0], race_settled):
        failures.append("race settlement")

    time.sleep(1.25)
    filler_event = json.loads(json.dumps(events[3])); filler_event["session_id"] = "public-lease-filler"
    filler_event["tool_use_id"] = "lease-filler"
    filler = invoke(hook, filler_event, workspace, state, plugin)
    response_count += 1
    time.sleep(1.05)
    reclaimed_event = json.loads(json.dumps(events[3])); reclaimed_event["session_id"] = "public-lease-reclaimed"
    reclaimed_event["tool_use_id"] = "lease-reclaimed"
    reclaimed = invoke(hook, reclaimed_event, workspace, state, plugin)
    response_count += 1
    stale_post = invoke(hook, post_for(race_events[1]), workspace, state, plugin)
    response_count += 1
    filler_post = invoke(hook, post_for(filler_event), workspace, state, plugin)
    reclaimed_post = invoke(hook, post_for(reclaimed_event), workspace, state, plugin)
    response_count += 2
    if (filler.get("policyReceipt", {}).get("reservation_state") != "reserved"
            or reclaimed.get("policyReceipt", {}).get("reservation_state") != "reserved"
            or stale_post.get("hookSpecificOutput", {}).get("permissionDecision") != "deny"
            or stale_post.get("policyReceipt", {}).get("reservation_state") != "expired"
            or not same_lifecycle(race_responses[1], stale_post)
            or not same_lifecycle(filler, filler_post) or not same_lifecycle(reclaimed, reclaimed_post)):
        failures.append("selective expiry/reclaim/stale Post")

    final, final_text, final_code = inspect(inspector, state)
    leases, lease_text, lease_code = inspect(inspector, state, "--leases", "--limit", "20")
    bounded, bounded_text, bounded_code = inspect(inspector, state, "--after-sequence", "5", "--limit", "3")
    if final_code != 0 or len(final) != 20 or not valid_chain(final):
        failures.append(f"post-rotation evidence count={len(final)}")
    if bounded_code != 0 or [item.get("sequence") for item in bounded] != [6, 7, 8]:
        failures.append("bounded post-rotation inspection")
    if (lease_code != 0 or len(leases) != 9 or not all(lease_record_ok(item) for item in leases)
            or sum(item.get("state") == "expired" for item in leases) != 1
            or any(item.get("state") == "reserved" for item in leases)):
        failures.append(f"final lease inventory={len(leases)}")

    approval_event = {
        "session_id": "public-approval-two", "hook_event_name": "PreToolUse",
        "tool_name": "Write", "tool_use_id": "approval-rotate",
        "tool_input": {"file_path": "src/approved.py", "content": "approved"},
        "expect": "ask", "expect_decision": "approval_required",
    }
    pending = invoke(hook, approval_event, workspace, state, plugin)
    response_count += 1
    add_response_checks(failures, pending, approval_event, v2)
    ticket_id = pending.get("policyReceipt", {}).get("approval_ticket_id")
    claims_a = {"principal_id": "public-a", "policy_id": "public-reload",
                "policy_epoch": 11, "tools": ["Write"], "path_prefixes": ["src/"], "hosts": []}
    claims_b = {"principal_id": "public-b", "policy_id": "public-reload",
                "policy_epoch": 11, "tools": ["Write"], "path_prefixes": ["src/"], "hosts": []}
    token_a1, token_a1_text, token_a1_code = maintain(
        inspector, state, "--enroll-approver", input_value=claims_a)
    token_b, token_b_text, token_b_code = maintain(
        inspector, state, "--enroll-approver", input_value=claims_b)
    vote_a1 = {"ticket_id": ticket_id, "vote_id": "public-a-generation-one",
               "principal_token": token_a1}
    _vote_a1_view, vote_a1_text, vote_a1_code = maintain_jsonl(
        inspector, state, "--cast-approval", input_value=vote_a1)
    _rotated_principal_view, rotate_principal_text, rotate_principal_code = inspect(
        inspector, state, "--rotate-approver", "public-a")
    _old_vote_view, old_vote_text, old_vote_code = maintain_jsonl(
        inspector, state, "--cast-approval", input_value=vote_a1)
    token_a2, token_a2_text, token_a2_code = maintain(
        inspector, state, "--enroll-approver", input_value=claims_a)
    vote_a2 = {"ticket_id": ticket_id, "vote_id": "public-a-generation-two",
               "principal_token": token_a2}
    vote_b = {"ticket_id": ticket_id, "vote_id": "public-b-generation-one",
              "principal_token": token_b}
    _vote_a2_view, vote_a2_text, vote_a2_code = maintain_jsonl(
        inspector, state, "--cast-approval", input_value=vote_a2)
    _vote_b_view, vote_b_text, vote_b_code = maintain_jsonl(
        inspector, state, "--cast-approval", input_value=vote_b)
    consume_event = dict(approval_event); consume_event["expect"] = "allow"
    consume_event["expect_decision"] = "allow"
    consumed = invoke(hook, consume_event, workspace, state, plugin)
    consumed_post = invoke(hook, post_for(consume_event), workspace, state, plugin)
    response_count += 2
    if (token_a1_code != 0 or token_b_code != 0 or vote_a1_code != 0
            or rotate_principal_code != 0 or old_vote_code == 0 or token_a2_code != 0
            or vote_a2_code != 0 or vote_b_code != 0
            or consumed.get("policyReceipt", {}).get("approval_state") != "consumed"
            or not same_lifecycle(consumed, consumed_post)):
        failures.append("approver generation rotation and consume")

    revoked_event = json.loads(json.dumps(approval_event))
    revoked_event["tool_use_id"] = "approval-revoked"
    revoked_event["tool_input"] = {"file_path": "src/revoked.py", "content": "blocked"}
    revoked_pending = invoke(hook, revoked_event, workspace, state, plugin)
    response_count += 1
    revoked_ticket = revoked_pending.get("policyReceipt", {}).get("approval_ticket_id")
    for request in (
        {"ticket_id": revoked_ticket, "vote_id": "public-revoke-a", "principal_token": token_a2},
        {"ticket_id": revoked_ticket, "vote_id": "public-revoke-b", "principal_token": token_b},
    ):
        _view, _text, code = maintain_jsonl(inspector, state, "--cast-approval", input_value=request)
        if code != 0:
            failures.append("ready ticket votes before revocation")
    _revoke_approval_view, revoke_approval_text, revoke_approval_code = inspect(
        inspector, state, "--revoke-approval", str(revoked_ticket))
    revoked_retry = invoke(hook, revoked_event, workspace, state, plugin)
    response_count += 1
    approvals, approval_text, approval_code = inspect(inspector, state, "--approvals", "--limit", "10")
    if (revoke_approval_code != 0 or revoked_retry.get("hookSpecificOutput", {}).get("permissionDecision") != "deny"
            or revoked_retry.get("policyReceipt", {}).get("approval_state") != "revoked"
            or approval_code != 0 or len(approvals) != 2
            or not all(approval_record_ok(item) for item in approvals)
            or {item.get("state") for item in approvals} != {"consumed", "revoked"}):
        failures.append("ready ticket revocation")

    base_transfer = {"transfer_id": "mig_public_base_two",
                     "transfer_secret": "public-base-transfer-secret-0000002"}
    base_checkpoint, base_checkpoint_text, base_checkpoint_code = maintain(
        inspector, state, "--export-checkpoint", input_value=base_transfer)
    archive_state = state.parent / "checkpoint-archive-base"
    archive_replace = {"checkpoint": base_checkpoint,
                       "transfer_secret": base_transfer["transfer_secret"],
                       "mode": "replace", "expected_lineage_digest": None}
    _archive_view, archive_text, archive_code = maintain_jsonl(
        inspector, archive_state, "--import-checkpoint", input_value=archive_replace)
    branch_states = [state.parent / "branch-state-a", state.parent / "branch-state-b"]
    child_checkpoints: list[dict] = []
    child_secrets: list[str] = []
    for index, branch_state in enumerate(branch_states):
        replace_request = {"checkpoint": base_checkpoint,
                           "transfer_secret": base_transfer["transfer_secret"],
                           "mode": "replace", "expected_lineage_digest": None}
        _replace_view, _replace_text, replace_code = maintain_jsonl(
            inspector, branch_state, "--import-checkpoint", input_value=replace_request)
        branch_event = json.loads(json.dumps(events[3]))
        branch_event["session_id"] = f"public-checkpoint-branch-{index}"
        branch_event["tool_use_id"] = f"checkpoint-branch-{index}"
        branch_pre = invoke(hook, branch_event, workspace, branch_state, plugin)
        response_count += 1
        child_secret = f"public-child-transfer-secret-00000{index}"
        child_request = {"transfer_id": f"mig_public_child_{index:02d}",
                         "transfer_secret": child_secret}
        child_checkpoint, _child_text, child_code = maintain(
            inspector, branch_state, "--export-checkpoint", input_value=child_request)
        if (replace_code != 0
                or branch_pre.get("hookSpecificOutput", {}).get("permissionDecision") != "deny"
                or branch_pre.get("policyReceipt", {}).get("ownership_state") != "checkpoint_only"
                or child_code != 0 or not checkpoint_ok(child_checkpoint)
                or child_checkpoint.get("base_checkpoint_digest") != base_checkpoint.get("checkpoint_digest")):
            failures.append(f"checkpoint direct child {index}")
        child_checkpoints.append(child_checkpoint); child_secrets.append(child_secret)
    merge_requests = [
        {"checkpoint": checkpoint, "transfer_secret": secret, "mode": "merge",
         "expected_lineage_digest": base_checkpoint.get("checkpoint_digest")}
        for checkpoint, secret in zip(child_checkpoints, child_secrets)
    ]
    with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
        merge_futures = [pool.submit(maintain_jsonl, inspector, archive_state, "--import-checkpoint",
                                     input_value=request) for request in merge_requests]
        merge_results = [future.result(timeout=12) for future in merge_futures]
    winners = [index for index, result in enumerate(merge_results) if result[2] == 0]
    if len(winners) == 1:
        winner = winners[0]
        _replay_merge, replay_merge_text, replay_merge_code = maintain_jsonl(
            inspector, archive_state, "--import-checkpoint", input_value=merge_requests[winner])
    else:
        replay_merge_text = ""; replay_merge_code = -1
    checkpoint_view, checkpoint_view_text, checkpoint_view_code = inspect(
        inspector, archive_state, "--checkpoints", "--limit", "10")
    if (base_checkpoint_code != 0 or not checkpoint_ok(base_checkpoint)
            or archive_code != 0 or len(winners) != 1 or replay_merge_code != 0
            or checkpoint_view_code != 0 or not checkpoint_view
            or not all(checkpoint_view_ok(item) for item in checkpoint_view)):
        failures.append("checkpoint merge lineage fence")

    cycle8_count, cycle8_observed = exercise_cycle8_repair_continuation(
        hook, inspector, workspace, state, plugin, events[3], v2,
        "two", failures)
    response_count += cycle8_count
    cycle7_count, cycle7_observed = exercise_cycle7_delivery_upgrade(
        hook, inspector, workspace, state, plugin, events[3], v2,
        "two", failures)
    response_count += cycle7_count
    handoff_count, handoff_observed = exercise_abort_and_crash_handoff(
        hook, inspector, workspace, state, plugin, events[3], v2, failures)
    response_count += handoff_count

    observed = json.dumps([pre, post, audit, audit_post, reload_response, rollback_response,
                           delegated, delegated_post, rotated_delegated, rotated_delegated_post,
                           revoked, rotated_read, rotated_read_post, *race_responses, race_settled,
                           filler, reclaimed, stale_post, filler_post, reclaimed_post,
                           pending, consumed, consumed_post, revoked_pending, revoked_retry], sort_keys=True)
    observed += (wider_text + compact_text + rotate_text + revoke_text + final_text + lease_text
                 + bounded_text + vote_a1_text
                 + rotate_principal_text + old_vote_text + vote_a2_text
                 + vote_b_text + revoke_approval_text + approval_text + base_checkpoint_text + archive_text
                 + replay_merge_text + checkpoint_view_text + cycle8_observed
                 + cycle7_observed + handoff_observed)
    if json.dumps(child_token, sort_keys=True) in observed:
        failures.append("delegation token disclosure")
    for token in (token_a1, token_a2, token_b):
        if token.get("token") and token["token"] in observed:
            failures.append("approver token disclosure")
    if any(secret in observed for secret in [base_transfer["transfer_secret"], *child_secrets]):
        failures.append("checkpoint transfer secret disclosure")
    return response_count, observed


def main() -> int:
    parser = argparse.ArgumentParser(description="Run a public Policy Provenance Ledger development case")
    parser.add_argument("--case", choices=("dev_001", "dev_002"), required=True)
    parser.add_argument("--plugin-root", type=Path, required=True)
    args = parser.parse_args()
    plugin = args.plugin_root.resolve()
    hook = plugin / "hooks" / "policy_hook.py"
    inspector = plugin / "bin" / "policy-ledger-inspect"
    assets = Path(__file__).resolve().parent / args.case / "assets"
    failures: list[str] = []
    with tempfile.TemporaryDirectory(prefix=f"ppl-{args.case}-") as raw:
        root = Path(raw)
        workspace = root / "workspace"
        state = root / "state"
        (workspace / ".claude").mkdir(parents=True)
        (workspace / "src").mkdir()
        (workspace / "src" / "public").mkdir()
        (workspace / "tmp").mkdir()
        response_count = 0
        observed = ""
        baseline_events = 18 if args.case == "dev_001" else 20
        try:
            if args.case == "dev_001":
                case_count, case_observed = run_dev_001(
                    hook, inspector, workspace, state, plugin, assets, failures)
            else:
                case_count, case_observed = run_dev_002(
                    hook, inspector, workspace, state, plugin, assets, failures)
            response_count += case_count
            observed += case_observed
        except Exception as exc:
            failures.append(f"public execution failure: {type(exc).__name__}: {str(exc)[:240]}")
        try:
            integrity_count, integrity_observed = exercise_cycle9_integrity(
                workspace, plugin, assets, args.case, failures)
            response_count += integrity_count
            observed += integrity_observed
        except Exception as exc:
            failures.append(
                f"cycle9 public execution failure: {type(exc).__name__}: {str(exc)[:240]}")
        if "public_dev_token_123456789" in observed:
            failures.append("secret disclosure")
        print(json.dumps({
            "case": args.case, "passed": not failures, "failures": failures,
            "responses": response_count, "baseline_audit_events": baseline_events,
        }, indent=2))
    return 0 if not failures else 1


if __name__ == "__main__":
    raise SystemExit(main())
