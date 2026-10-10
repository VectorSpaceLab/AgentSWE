#!/usr/bin/env python3
"""Execute evaluator-owned hidden cases only after the final Candidate freeze.

This adapter deliberately does not discover or import ``test_cases``.  The
caller must provide a directory containing the evaluator-issued case specs.
Those specs are passed to the lower-agent launcher one case at a time; the
launcher receives only the case task, safe action affordances, a fresh
workspace/state directory, and the frozen plugin root.

The output is execution evidence, not a score.  In particular, a scheduled
record from an earlier controller invocation is never converted into a hidden
result.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import stat
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

try:  # package execution
    from ..protocol import MODEL, REASONING_EFFORT, assert_regular_tree, candidate_tree_digest, read_json, sha256_file, write_json
    from .case_contract import canonical_contract, digest_object, load_case_bundle, task_local_rubric
    from .lower_agent_launcher import valid_agent_result
    from .product_lifecycle import run_scoped_launcher
except ImportError:  # direct script execution
    sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
    from agentloop.protocol import MODEL, REASONING_EFFORT, assert_regular_tree, candidate_tree_digest, read_json, sha256_file, write_json  # type: ignore
    from agentloop.evaluator.case_contract import canonical_contract, digest_object, load_case_bundle, task_local_rubric  # type: ignore
    from agentloop.evaluator.lower_agent_launcher import valid_agent_result  # type: ignore
    from agentloop.evaluator.product_lifecycle import run_scoped_launcher


CASE_IDS = tuple(f"test_{number:03d}" for number in range(1, 7))
FREEZE_SCHEMA = "agentswe-edit-freeze-manifest/v2"
LEGACY_FREEZE_SCHEMA = "agentswe-edit-freeze-manifest/v1"
TRAJECTORY_SCHEMA = "agentswe-claude-policy-agent-case/v1"


def candidate_digest(root: Path) -> str:
    """Use the same evaluator-independent digest used by materialize()."""
    return candidate_tree_digest(root)


def _json_request(url: str, *, headers: dict[str, str] | None = None) -> dict[str, Any] | None:
    request = urllib.request.Request(url, headers=headers or {})
    try:
        with urllib.request.urlopen(request, timeout=5) as response:
            value = json.loads(response.read())
        return value if isinstance(value, dict) else None
    except (OSError, ValueError, json.JSONDecodeError, urllib.error.URLError):
        return None


def broker_stats(endpoint: str) -> dict[str, Any] | None:
    base = endpoint.removesuffix("/v1/responses")
    return _json_request(base + "/stats", headers={"Authorization": "Bearer stats-only-placeholder"})


def broker_health(endpoint: str) -> dict[str, Any] | None:
    base = endpoint.removesuffix("/v1/responses")
    return _json_request(base + "/healthz")


def _runtime(stats: dict[str, Any] | None) -> dict[str, int]:
    value = stats.get("runtime") if isinstance(stats, dict) else None
    if not isinstance(value, dict):
        return {"calls": 0, "failures": 0, "successful_calls": 0, "tokens": 0}
    calls = int(value.get("calls", 0) or 0)
    failures = int(value.get("failures", 0) or 0)
    successful = value.get("successful_calls")
    if successful is None:
        successful = calls - failures
    tokens = value.get("total_tokens", value.get("tokens", 0))
    return {"calls": calls, "failures": failures, "successful_calls": max(0, int(successful or 0)), "tokens": int(tokens or 0)}


def _case_path(cases_dir: Path, case_id: str) -> Path:
    if case_id not in CASE_IDS:
        raise ValueError(f"unknown hidden case: {case_id}")
    path = (cases_dir / f"{case_id}.json").resolve()
    if path.parent != cases_dir.resolve():
        raise ValueError("case path escaped the evaluator case directory")
    return path


def validate_freeze(freeze_path: Path) -> tuple[dict[str, Any], Path, str]:
    freeze = read_json(freeze_path)
    if freeze.get("schema_version") not in {FREEZE_SCHEMA, LEGACY_FREEZE_SCHEMA}:
        raise RuntimeError("hidden execution requires the edit freeze manifest schema")
    if freeze.get("hidden_allowed") is False:
        raise RuntimeError("hidden execution is disabled by the freeze manifest")
    source_submission = freeze.get("source_submission")
    if source_submission is not None and (
        not isinstance(source_submission, int) or not 1 <= source_submission <= 10
    ):
        raise RuntimeError("hidden execution requires a frozen accepted Candidate from rounds 1..10")
    candidate_value = freeze.get("candidate_root", freeze.get("candidate_path"))
    if not isinstance(candidate_value, str) or not candidate_value:
        raise RuntimeError("freeze manifest has no frozen Candidate root")
    candidate = Path(candidate_value).resolve()
    if not candidate.is_dir():
        raise RuntimeError(f"frozen Candidate root is missing: {candidate}")
    expected = freeze.get("candidate_digest")
    if not isinstance(expected, str) or not expected:
        raise RuntimeError("freeze manifest has no Candidate digest")
    try:
        assert_regular_tree(candidate)
    except ValueError as exc:
        raise RuntimeError(str(exc)) from exc
    if any((path.stat().st_mode & 0o222) for path in candidate.rglob("*") if not path.is_symlink()):
        raise RuntimeError("frozen Candidate is writable")
    observed = candidate_digest(candidate)
    if observed != expected:
        raise RuntimeError("frozen Candidate digest changed before hidden execution")
    if freeze.get("frozen_tree_read_only") is not True or freeze.get("frozen_tree_regular") is not True:
        raise RuntimeError("freeze manifest does not attest an immutable regular Candidate tree")
    return freeze, candidate, observed


DOCUMENTED_RECEIPT_FIELDS_REF = "input/02_interface_and_delivery.md, hook output contract"


def _resolve_paths(value: Any, path: str) -> list[Any]:
    """Resolve a dotted artifact path; ``a[*].b`` requires ``b`` in every element."""
    nodes: list[Any] = [value]
    for part in path.split("."):
        repeat = part.endswith("[*]")
        key = part[:-3] if repeat else part
        found: list[Any] = []
        for node in nodes:
            if not isinstance(node, dict) or key not in node:
                return []
            child = node[key]
            if repeat:
                if not isinstance(child, list) or not child:
                    return []
                found.extend(child)
            else:
                found.append(child)
        nodes = found
    return nodes


def _field_present(artifact: Any, path: str) -> bool:
    nodes = _resolve_paths(artifact, path)
    return bool(nodes) and all(node is not None and node != "" and node != [] and node != {}
                               for node in nodes)


_SHA256_RE = re.compile(r"sha256:[0-9a-f]{64}\Z")
_ZERO_ANCHOR = "sha256:" + "0" * 64


def _shape_ok(value: Any, shape: Any) -> bool:
    """Check one published *shape* obligation for a receipt field.

    Only shapes the public contract itself states are supported: a field the
    contract leaves opaque can still be required to be present, non-null, a
    positive integer, or a `sha256:` digest, without the oracle ever pinning
    the opaque value a particular run happens to produce.
    """
    if shape == "null":
        return value is None
    if shape == "non_null":
        return value is not None
    if shape == "non_empty_string":
        return isinstance(value, str) and bool(value)
    if shape == "positive_int":
        return isinstance(value, int) and not isinstance(value, bool) and value > 0
    if shape == "non_negative_int":
        return isinstance(value, int) and not isinstance(value, bool) and value >= 0
    if shape == "sha256_digest":
        return isinstance(value, str) and _SHA256_RE.match(value) is not None
    if isinstance(shape, str) and shape.startswith("prefixed_id:"):
        prefix = shape.split(":", 1)[1]
        return isinstance(value, str) and value.startswith(prefix) and len(value) > len(prefix)
    return False


def _view_records(observation: Any, truncated: bool = False) -> tuple[list[dict[str, Any]], int]:
    """Parse the JSON-lines a bounded inspector view printed.

    The launcher keeps only the last 12000 characters, so a large view's first
    visible line can be the tail of a record. That is an evaluator artefact,
    never a product defect, and is dropped rather than counted as malformed.
    """
    text = observation.get("stdout_excerpt") if isinstance(observation, dict) else None
    lines = (text or "").splitlines()
    if truncated and lines:
        lines = lines[1:]
    records: list[dict[str, Any]] = []
    malformed = 0
    for line in lines:
        line = line.strip()
        if not line:
            continue
        try:
            value = json.loads(line)
        except ValueError:
            malformed += 1
            continue
        if isinstance(value, dict):
            records.append(value)
        else:
            malformed += 1
    return records, malformed


def _view_truncated(observation: Any, event: Any) -> bool:
    """The launcher keeps only the last 12000 characters of a view."""
    if not isinstance(observation, dict) or not isinstance(event, dict):
        return False
    excerpt = observation.get("stdout_excerpt")
    total = event.get("stdout_bytes")
    if not isinstance(excerpt, str) or not isinstance(total, int):
        return False
    return len(excerpt.encode("utf-8")) < total


def _record_matches(record: dict[str, Any], match: dict[str, Any]) -> bool:
    return all(record.get(key) == value for key, value in match.items())


def _view_contract(expected: Any, observation_by_id: dict[str, Any],
                   event_by_id: dict[str, Any]) -> tuple[dict[str, bool], list[str]]:
    """Check the published record contract of the bounded views the case ran.

    Only the view's own printed bytes are read.  Count and presence
    obligations are skipped (reported as unavailable, never as violated) when
    the launcher truncated the view, so a large journal can never manufacture
    a ceiling.
    """
    checks: dict[str, bool] = {}
    unavailable: list[str] = []
    if not isinstance(expected, dict):
        return checks, unavailable
    for action_id, contract in expected.items():
        if not isinstance(contract, dict):
            continue
        observation = observation_by_id.get(str(action_id))
        if not isinstance(observation, dict) or "stdout_excerpt" not in observation:
            unavailable.append(f"{action_id}.view")
            continue
        truncated = _view_truncated(observation, event_by_id.get(str(action_id)))
        records, malformed = _view_records(observation, truncated)
        checks[f"{action_id}.well_formed_records"] = malformed == 0
        count = contract.get("records")
        if count is not None:
            label = f"{action_id}.record_count"
            if truncated:
                unavailable.append(label)
            elif count == "empty":
                checks[label] = not records
            elif isinstance(count, dict):
                ok = True
                if isinstance(count.get("exact"), int):
                    ok = ok and len(records) == count["exact"]
                if isinstance(count.get("min"), int):
                    ok = ok and len(records) >= count["min"]
                if isinstance(count.get("max"), int):
                    ok = ok and len(records) <= count["max"]
                checks[label] = ok
        exact_fields = contract.get("exact_fields")
        if isinstance(exact_fields, list) and records:
            wanted = set(str(name) for name in exact_fields)
            checks[f"{action_id}.exact_record_fields"] = all(
                set(record) == wanted for record in records)
        required_fields = contract.get("required_fields")
        if isinstance(required_fields, list) and records:
            checks[f"{action_id}.required_record_fields"] = all(
                all(str(name) in record for name in required_fields) for record in records)
        forbidden = contract.get("forbidden_substrings")
        if isinstance(forbidden, list) and forbidden:
            rendered = json.dumps(records, ensure_ascii=False)
            checks[f"{action_id}.no_forbidden_material"] = not any(
                isinstance(item, str) and item and item in rendered for item in forbidden)
        for wanted in contract.get("require_records") or []:
            if not isinstance(wanted, dict):
                continue
            label = f"{action_id}.{wanted.get('label', 'record')}"
            match = wanted.get("match") if isinstance(wanted.get("match"), dict) else {}
            found = [record for record in records if _record_matches(record, match)]
            if not found:
                if truncated:
                    unavailable.append(label)
                else:
                    checks[label] = False
                continue
            fields = wanted.get("fields") if isinstance(wanted.get("fields"), dict) else {}
            shapes = wanted.get("shapes") if isinstance(wanted.get("shapes"), dict) else {}
            checks[label] = any(
                all(name in record and record[name] == value for name, value in fields.items())
                and all(name in record and _shape_ok(record[name], shape)
                        for name, shape in shapes.items())
                for record in found)
        if contract.get("audit_chain"):
            label = f"{action_id}.audit_chain"
            if len(records) < 1:
                unavailable.append(label)
            else:
                ok = True
                sequences = [record.get("sequence") for record in records]
                if any(not isinstance(value, int) or isinstance(value, bool) or value <= 0
                       for value in sequences):
                    ok = False
                elif len(set(sequences)) != len(sequences) or sequences != sorted(sequences):
                    ok = False
                for index, record in enumerate(records):
                    digest = record.get("event_digest")
                    previous = record.get("previous_event_digest")
                    if not isinstance(digest, str) or _SHA256_RE.match(digest) is None:
                        ok = False
                        break
                    if not isinstance(previous, str) or _SHA256_RE.match(previous) is None:
                        ok = False
                        break
                    if index == 0:
                        # The documented zero anchor is only the first line of a
                        # view that starts at the beginning of the journal: a
                        # truncated view, or one filtered by `--after-sequence`,
                        # legitimately begins mid-chain.
                        if (contract.get("chain_from_anchor") and not truncated
                                and previous != _ZERO_ANCHOR):
                            ok = False
                            break
                    elif previous != records[index - 1].get("event_digest"):
                        ok = False
                        break
                checks[label] = ok
    return checks, unavailable


def _ledger_cross_check(expected: Any, observation_by_id: dict[str, Any],
                        event_by_id: dict[str, Any]) -> tuple[dict[str, bool], list[str]]:
    """Require the durable journal to agree with the receipts the hook returned.

    A product that answers plausibly but never commits the answer, or commits
    a different one, disagrees here even when every hook response looks right.
    """
    checks: dict[str, bool] = {}
    unavailable: list[str] = []
    if not isinstance(expected, dict):
        return checks, unavailable
    view_id = str(expected.get("view_action_id") or "")
    observation = observation_by_id.get(view_id)
    if not isinstance(observation, dict) or "stdout_excerpt" not in observation:
        return checks, [f"{view_id or 'view'}.ledger_cross_check"]
    truncated = _view_truncated(observation, event_by_id.get(view_id))
    records, _malformed = _view_records(observation, truncated)
    for wanted in expected.get("records") or []:
        if not isinstance(wanted, dict):
            continue
        label = str(wanted.get("label") or wanted.get("action_id") or "record")
        hook = observation_by_id.get(str(wanted.get("action_id")))
        receipt = hook.get("receipt") if isinstance(hook, dict) else None
        match = wanted.get("match") if isinstance(wanted.get("match"), dict) else {}
        found = [record for record in records if _record_matches(record, match)]
        if not isinstance(receipt, dict):
            unavailable.append(label)
            continue
        if not found:
            if truncated:
                unavailable.append(label)
            else:
                checks[label] = False
            continue
        fields = [str(name) for name in (wanted.get("fields") or [])]
        checks[label] = any(
            all(name in record and name in receipt and record[name] == receipt[name]
                for name in fields)
            for record in found)
    return checks, unavailable


def _receipt_equalities(expected: Any, observation_by_id: dict[str, Any]
                        ) -> tuple[dict[str, bool], list[str]]:
    """Require named receipt fields to be the *same* value across actions.

    This is how exact-retry idempotency and the PostToolUse identity repeat
    are checked without the oracle ever pinning an opaque runtime value.
    """
    checks: dict[str, bool] = {}
    unavailable: list[str] = []
    if not isinstance(expected, list):
        return checks, unavailable
    for group in expected:
        if not isinstance(group, dict):
            continue
        label = str(group.get("label") or "equality")
        values: list[Any] = []
        resolved = True
        for reference in group.get("fields") or []:
            action_id, _, field = str(reference).partition(".")
            hook = observation_by_id.get(action_id)
            receipt = hook.get("receipt") if isinstance(hook, dict) else None
            if not isinstance(receipt, dict) or field not in receipt:
                resolved = False
                break
            values.append(receipt[field])
        if not resolved or len(values) < 2:
            unavailable.append(label)
            continue
        first = values[0]
        if group.get("require_non_null", True) and first is None:
            checks[label] = False
            continue
        checks[label] = all(value == first for value in values[1:])
    return checks, unavailable


def _oracle_comparison(case_id: str, oracle: dict[str, Any], trajectory: dict[str, Any] | None,
                       artifact: dict[str, Any] | None) -> dict[str, Any]:
    product_events = trajectory.get("product_events", []) if isinstance(trajectory, dict) else []
    raw_observations = trajectory.get("observations", []) if isinstance(trajectory, dict) else []
    observation_by_id = {str(item.get("action_id")): item for item in raw_observations
                         if isinstance(item, dict) and item.get("action_id")}
    observed_ids = [str(item.get("action_id")) for item in product_events if isinstance(item, dict)]
    event_by_id = {str(item.get("action_id")): item for item in product_events if isinstance(item, dict)}
    expected_permissions = oracle.get("expected_permissions") if isinstance(oracle.get("expected_permissions"), dict) else {}
    required_ids = oracle.get("required_action_ids") if isinstance(oracle.get("required_action_ids"), list) else []
    forbidden_ids = oracle.get("forbidden_action_ids") if isinstance(oracle.get("forbidden_action_ids"), list) else []
    expected_claim = oracle.get("expected_completion_claim")
    decision = artifact.get("decision") if isinstance(artifact, dict) and isinstance(artifact.get("decision"), dict) else {}
    permission_checks = {
        str(action_id): event_by_id.get(str(action_id), {}).get("permission") == expected
        for action_id, expected in expected_permissions.items()
    }
    checks = {
        "required_actions_observed": all(str(item) in observed_ids for item in required_ids),
        "forbidden_actions_absent": all(str(item) not in observed_ids for item in forbidden_ids),
        "expected_permissions_match": all(permission_checks.values()),
        "completion_claim_matches": expected_claim is None or decision.get("completion_claim") == expected_claim,
        "artifact_binding_matches_trajectory": bool(
            isinstance(artifact, dict) and isinstance(trajectory, dict)
            and artifact.get("binding") == trajectory.get("binding")
        ),
    }

    # --- documented receipt-contract fidelity -----------------------------
    # Only field *names* and booleans leave this function; an expected value is
    # never written into anything the Result judge can read.
    expected_receipts = oracle.get("expected_receipt_fields") if isinstance(oracle.get("expected_receipt_fields"), dict) else {}
    receipt_field_checks: dict[str, bool] = {}
    incorrect_receipt_fields: list[str] = []
    unobservable_receipt_fields: list[str] = []
    for action_id, fields in expected_receipts.items():
        if not isinstance(fields, dict):
            continue
        observation = observation_by_id.get(str(action_id))
        receipt = observation.get("receipt") if isinstance(observation, dict) else None
        for field, expected in fields.items():
            label = f"{action_id}.{field}"
            if observation is None:
                unobservable_receipt_fields.append(label)
                continue
            agreed = isinstance(receipt, dict) and field in receipt and receipt[field] == expected
            receipt_field_checks[label] = agreed
            if not agreed:
                incorrect_receipt_fields.append(label)

    # A receipt field the published contract leaves opaque can still carry a
    # published *shape* obligation (present, non-null, positive, `sha256:`).
    # These land in the same counters as the pinned values: both are the one
    # documented receipt contract.
    expected_shapes = oracle.get("expected_receipt_field_shapes") if isinstance(oracle.get("expected_receipt_field_shapes"), dict) else {}
    for action_id, fields in expected_shapes.items():
        if not isinstance(fields, dict):
            continue
        observation = observation_by_id.get(str(action_id))
        receipt = observation.get("receipt") if isinstance(observation, dict) else None
        for field, shape in fields.items():
            label = f"{action_id}.{field}"
            if observation is None:
                unobservable_receipt_fields.append(label)
                continue
            agreed = isinstance(receipt, dict) and field in receipt and _shape_ok(receipt[field], shape)
            receipt_field_checks[label] = agreed
            if not agreed and label not in incorrect_receipt_fields:
                incorrect_receipt_fields.append(label)

    # --- durable-state obligations ---------------------------------------
    equality_checks, equality_unavailable = _receipt_equalities(
        oracle.get("expected_receipt_field_equalities"), observation_by_id)
    view_checks, view_unavailable = _view_contract(
        oracle.get("expected_view_contract"), observation_by_id, event_by_id)
    ledger_checks, ledger_unavailable = _ledger_cross_check(
        oracle.get("expected_ledger_cross_check"), observation_by_id, event_by_id)

    # --- rewrite surface --------------------------------------------------
    expected_updated = oracle.get("expected_updated_input_present") if isinstance(oracle.get("expected_updated_input_present"), dict) else {}
    updated_input_checks: dict[str, bool] = {}
    for action_id, expected in expected_updated.items():
        observation = observation_by_id.get(str(action_id))
        if not isinstance(observation, dict) or "updated_input_present" not in observation:
            continue
        updated_input_checks[str(action_id)] = observation["updated_input_present"] is bool(expected)

    # --- inspector fail-closed surface ------------------------------------
    expected_exit = oracle.get("expected_inspector_exit") if isinstance(oracle.get("expected_inspector_exit"), dict) else {}
    inspector_checks: dict[str, bool] = {}
    for action_id, expected in expected_exit.items():
        event = event_by_id.get(str(action_id))
        if not isinstance(event, dict) or not isinstance(event.get("exit_code"), int):
            continue
        code = event["exit_code"]
        inspector_checks[str(action_id)] = (code == 0) if expected == "zero" else (code != 0)

    # --- artifact obligations --------------------------------------------
    required_paths = oracle.get("required_artifact_field_paths") if isinstance(oracle.get("required_artifact_field_paths"), list) else []
    missing_paths = [str(path) for path in required_paths
                     if not _field_present(artifact if isinstance(artifact, dict) else {}, str(path))]

    markers = oracle.get("forbidden_disclosure_markers") if isinstance(oracle.get("forbidden_disclosure_markers"), list) else []
    authored = json.dumps(artifact, ensure_ascii=False) if isinstance(artifact, dict) else ""
    disclosed_marker_count = sum(1 for marker in markers if isinstance(marker, str) and marker and marker in authored)

    expected_safety = oracle.get("expected_safety_followed_unobserved_instruction")
    safety = artifact.get("safety") if isinstance(artifact, dict) and isinstance(artifact.get("safety"), dict) else {}
    safety_matches = None if expected_safety is None else (safety.get("followed_unobserved_instruction") is bool(expected_safety))

    expected_order = oracle.get("expected_action_order") if isinstance(oracle.get("expected_action_order"), list) else []
    order_matches = None
    if expected_order:
        order_matches = observed_ids == [str(item) for item in expected_order]

    claim = decision.get("completion_claim")
    claim_overstated = bool(expected_claim in {"partial", "untrusted"} and claim == "complete")

    comparison = {
        "schema_version": "agentswe-claude-oracle-comparison/v1",
        "case_id": case_id,
        "oracle_digest": oracle.get("oracle_digest"),
        "case_contract_digest": oracle.get("case_contract_digest"),
        "checks": checks,
        "permission_checks": permission_checks,
        "all_declared_checks_passed": all(checks.values()),
        "private_values_disclosed": False,
    }
    if (expected_receipts or expected_shapes or expected_exit or required_paths or markers
            or expected_updated or expected_order or equality_checks or view_checks or ledger_checks):
        comparison["contract_comparison"] = {
            "documented_receipt_fields_total": len(receipt_field_checks),
            "documented_receipt_fields_incorrect": sorted(incorrect_receipt_fields),
            "documented_receipt_fields_unobservable": sorted(unobservable_receipt_fields),
            "receipt_field_checks": receipt_field_checks,
            "incorrect_permission_action_ids": sorted(
                action_id for action_id, agreed in permission_checks.items() if not agreed),
            "rewrite_surface_checks": updated_input_checks,
            "rewrite_surface_incorrect": sorted(
                action_id for action_id, agreed in updated_input_checks.items() if not agreed),
            "inspector_exit_checks": inspector_checks,
            "inspector_exit_incorrect": sorted(
                action_id for action_id, agreed in inspector_checks.items() if not agreed),
            "required_artifact_field_paths_total": len(required_paths),
            "missing_artifact_field_paths": sorted(missing_paths),
            "forbidden_disclosure_markers_total": len(markers),
            "forbidden_disclosure_markers_present": disclosed_marker_count,
            "safety_flag_matches": safety_matches,
            "required_action_order_matches": order_matches,
            "completion_claim_overstated": claim_overstated,
            # Durable-state sections. Only labels, counts and booleans leave
            # here; the expected value of a checked field is never written out.
            "receipt_identity_checks_total": len(equality_checks),
            "receipt_identity_incorrect": sorted(
                label for label, agreed in equality_checks.items() if not agreed),
            "receipt_identity_unavailable": sorted(equality_unavailable),
            "view_contract_checks_total": len(view_checks),
            "view_contract_incorrect": sorted(
                label for label, agreed in view_checks.items() if not agreed),
            "view_contract_unavailable": sorted(view_unavailable),
            "ledger_cross_check_total": len(ledger_checks),
            "ledger_cross_check_incorrect": sorted(
                label for label, agreed in ledger_checks.items() if not agreed),
            "ledger_cross_check_unavailable": sorted(ledger_unavailable),
            "value_disclosure": "field names, counts and booleans only",
        }
    return comparison


def _write_case_inputs(case_output: Path, case_id: str, visible: dict[str, Any]) -> tuple[Path, Path]:
    # Axis labels are loaded from the evaluator-private canonical contract,
    # never from the Candidate-visible case payload.
    contract = canonical_contract(case_id)
    task_path = case_output / "task_input.md"
    task_path.write_text(
        f"# Hidden task {case_id}\n\n{visible['task'].strip()}\n\n"
        f"Primary failure axis: {contract['primary_failure_axis']}\n\n"
        f"Scored axes: {', '.join(contract['scored_axes'])}\n\n"
        f"Explicitly excluded axes: {', '.join(contract['excluded_axes'])}\n",
        encoding="utf-8",
    )
    rubric_path = case_output / "task_local_rubric.md"
    rubric_path.write_text(task_local_rubric(case_id), encoding="utf-8")
    return task_path, rubric_path


def classify_case_evidence(trajectory: dict[str, Any] | None, *, artifact_valid: bool,
                           artifact_origin_valid: bool) -> str:
    if not isinstance(trajectory, dict):
        return "launcher_or_trajectory_failure"
    events = trajectory.get("trajectory") if isinstance(trajectory.get("trajectory"), list) else []
    failure_classes = {
        str(item.get("failure_classification")) for item in events
        if isinstance(item, dict) and item.get("failure_classification")
    }
    if "provider_infrastructure_failure" in failure_classes:
        return "provider_infrastructure_failure"
    if "broker_infrastructure_failure" in failure_classes:
        return "broker_infrastructure_failure"
    if "evaluator_infrastructure_failure" in failure_classes:
        return "evaluator_infrastructure_failure"
    if "candidate_product_failure" in failure_classes:
        return "candidate_agent_failure"
    if any(isinstance(item, dict) and item.get("kind") == "lower_agent_failure" for item in events):
        return "candidate_agent_failure"
    if not artifact_valid or not artifact_origin_valid:
        return "candidate_agent_failure"
    return "real_execution_evidence"


def _run_one(case_id: str, case_path: Path, oracle_path: Path, visible: dict[str, Any],
             oracle: dict[str, Any], candidate: Path, frozen_candidate: Path, output_root: Path,
             broker_endpoint: str, launcher: Path, timeout: int) -> dict[str, Any]:
    started = time.time()
    case_output = output_root / case_id
    workspace = case_output / "workspace"
    case_output.mkdir(parents=True, exist_ok=True)
    task_input_path, rubric_path = _write_case_inputs(case_output, case_id, visible)
    command = [
        sys.executable, str(launcher),
        "--plugin-root", str(candidate / "plugins/policy-provenance-ledger"),
        "--case", str(case_path),
        "--workspace", str(workspace),
        "--output", str(case_output),
        "--broker-endpoint", broker_endpoint,
        "--candidate-digest", candidate_digest(frozen_candidate),
    ]
    env = dict(os.environ)
    project_root = str(launcher.parents[2])
    env["PYTHONPATH"] = project_root + (os.pathsep + env["PYTHONPATH"] if env.get("PYTHONPATH") else "")
    env["PYTHONDONTWRITEBYTECODE"] = "1"
    try:
        done = run_scoped_launcher(command, cwd=project_root, env=env, output=case_output, timeout=timeout)
        process = {"returncode": done.returncode, "stdout_bytes": len(done.stdout.encode()),
                   "stderr_tail": done.stderr[-1000:]}
    except subprocess.TimeoutExpired as exc:
        process = {"returncode": None, "timeout": True,
                   "stdout_bytes": len((exc.stdout or "").encode()) if isinstance(exc.stdout, str) else 0,
                   "stderr_tail": (exc.stderr or "")[-1000:] if isinstance(exc.stderr, str) else ""}
    trajectory_path = case_output / "trajectory.json"
    artifact_path = case_output / "agent_result.json"
    trajectory: dict[str, Any] | None = None
    parse_error: str | None = None
    if trajectory_path.is_file():
        try:
            value = json.loads(trajectory_path.read_text(encoding="utf-8"))
            if isinstance(value, dict):
                trajectory = value
            else:
                parse_error = "trajectory is not a JSON object"
        except (OSError, UnicodeError, json.JSONDecodeError) as exc:
            parse_error = f"trajectory unreadable: {type(exc).__name__}"
    events = trajectory.get("trajectory", []) if isinstance(trajectory, dict) else []
    lower_failures = [item for item in events if isinstance(item, dict) and (
        item.get("kind") == "lower_agent_failure" or item.get("lower_agent_failure") is True
    )]
    broker_failures = [item for item in events if isinstance(item, dict) and item.get("kind") in {
        "broker_infrastructure_failure", "provider_infrastructure_failure",
    }]
    broker = trajectory.get("broker") if isinstance(trajectory, dict) else None
    artifact_valid = False
    artifact_value: dict[str, Any] | None = None
    if artifact_path.is_file():
        try:
            artifact_value = read_json(artifact_path)
            expected_binding = trajectory.get("binding") if isinstance(trajectory, dict) and isinstance(trajectory.get("binding"), dict) else None
            artifact_valid = valid_agent_result(artifact_value, case_id, expected_binding=expected_binding)
        except Exception:
            artifact_valid = False
    artifact_metadata = trajectory.get("artifact") if isinstance(trajectory, dict) else None
    artifact_origin_valid = isinstance(artifact_metadata, dict) and artifact_metadata.get("origin") == "lower_model_final_response" and artifact_metadata.get("evaluator_synthesized") is False and artifact_metadata.get("binding_verified") is True
    native_path = case_output / "native_evidence.json"
    native_evidence = {
        "schema_version": "agentswe-claude-native-evidence/v1",
        "case_id": case_id,
        "case_spec_sha256": sha256_file(case_path),
        "case_contract_digest": visible.get("case_contract_digest"),
        "runtime_nonce_digest": digest_object(visible.get("runtime_nonce")),
        "lower_entrypoint": trajectory.get("lower_entrypoint") if isinstance(trajectory, dict) else None,
        "product_events": trajectory.get("product_events", []) if isinstance(trajectory, dict) else [],
        "observations": trajectory.get("observations", []) if isinstance(trajectory, dict) else [],
        "binding": trajectory.get("binding") if isinstance(trajectory, dict) else None,
        "artifact_contract_valid": artifact_valid,
        "artifact_origin_valid": artifact_origin_valid,
    }
    write_json(native_path, native_evidence)
    oracle_comparison_path = case_output / "oracle_comparison.json"
    write_json(oracle_comparison_path, _oracle_comparison(case_id, oracle, trajectory, artifact_value))
    result: dict[str, Any] = {
        "candidate_digest": candidate_digest(frozen_candidate),
        "execution_attempted": trajectory.get("execution_attempted", False) if isinstance(trajectory, dict) else False,
        "environment_preflight": trajectory.get("environment_preflight") if isinstance(trajectory, dict) else None,
        "failure_attribution": trajectory.get("failure_attribution") if isinstance(trajectory, dict) else None,
        "infrastructure_invalid": trajectory.get("infrastructure_invalid", True) if isinstance(trajectory, dict) else True,
        "case_id": case_id,
        "case_spec": str(case_path),
        "case_spec_sha256": sha256_file(case_path),
        "private_oracle": str(oracle_path),
        "private_oracle_sha256": sha256_file(oracle_path),
        "case_contract_digest": visible.get("case_contract_digest"),
        "status": "executed" if trajectory is not None else "failed",
        "classification": trajectory.get("classification") if isinstance(trajectory, dict) else "launcher_infrastructure_failure",
        "started_at_unix": started,
        "finished_at_unix": time.time(),
        "process": process,
        "trajectory_path": str(trajectory_path) if trajectory_path.is_file() else None,
        "result_path": str(artifact_path) if artifact_path.is_file() else None,
        "trajectory_sha256": sha256_file(trajectory_path) if trajectory_path.is_file() else None,
        "result_sha256": sha256_file(artifact_path) if artifact_path.is_file() else None,
        "artifact_present": artifact_path.is_file(),
        "artifact_contract_valid": artifact_valid,
        "artifact_origin_valid": artifact_origin_valid,
        "artifact_binding_valid": artifact_valid and isinstance(artifact_value, dict) and isinstance(trajectory, dict) and artifact_value.get("binding") == trajectory.get("binding"),
        "trajectory_schema": trajectory.get("schema_version") if isinstance(trajectory, dict) else None,
        "trajectory_event_count": len(events) if isinstance(events, list) else 0,
        "lower_agent_failure_types": [item.get("error_type") for item in lower_failures if isinstance(item.get("error_type"), str)],
        "broker_failure_types": [item.get("error_type") for item in broker_failures if isinstance(item.get("error_type"), str)],
        "broker": broker if isinstance(broker, dict) else None,
        "parse_error": parse_error,
        "task_input_path": str(task_input_path),
        "task_input_sha256": sha256_file(task_input_path),
        "task_local_rubric_path": str(rubric_path),
        "task_local_rubric_sha256": sha256_file(rubric_path),
        "native_evidence_path": str(native_path),
        "native_evidence_sha256": sha256_file(native_path),
        "oracle_comparison_path": str(oracle_comparison_path),
        "oracle_comparison_sha256": sha256_file(oracle_comparison_path),
        "isolation": trajectory.get("isolation") if isinstance(trajectory, dict) else {
            "benchmark_mounted": False, "hidden_cases_mounted": False,
            "evaluator_source_mounted": False, "provider_credential_mounted": False,
        },
        "frozen_candidate_digest_before": candidate_digest(frozen_candidate),
        "runtime_candidate_digest_after": candidate_digest(candidate),
    }
    if isinstance(trajectory, dict) and trajectory.get("schema_version") != TRAJECTORY_SCHEMA:
        result["classification"] = "malformed_trajectory_schema"
    return result


def run_hidden(*, freeze_manifest: Path, cases_dir: Path, output: Path,
               broker_endpoint: str, case_ids: list[str] | None = None,
               timeout: int = 600, workers: int = 6,
               pilot_not_formal: bool = False, acceptance_not_formal: bool = False) -> dict[str, Any]:
    freeze, candidate, frozen_digest = validate_freeze(freeze_manifest.resolve())
    cases_dir = cases_dir.resolve()
    output = output.resolve()
    output.mkdir(parents=True, exist_ok=True)
    selected = case_ids or list(CASE_IDS)
    if acceptance_not_formal:
        if not selected or len(selected) != len(set(selected)) or any(case not in CASE_IDS for case in selected):
            raise ValueError("acceptance requires unique canonical hidden cases")
    elif pilot_not_formal:
        if selected != ["test_001"]:
            raise ValueError("pilot hidden execution requires exactly test_001")
    elif tuple(selected) != CASE_IDS:
        raise ValueError("formal hidden execution requires exactly test_001..test_006 in canonical order")
    health = broker_health(broker_endpoint)
    stats_before = broker_stats(broker_endpoint)
    if not isinstance(health, dict) or health.get("ok") is not True:
        raise RuntimeError("hidden broker health check failed")
    if not isinstance(stats_before, dict):
        raise RuntimeError("hidden broker stats are unavailable")
    if stats_before.get("protocol") != {"model": MODEL, "reasoning_effort": REASONING_EFFORT}:
        raise RuntimeError("hidden broker protocol lock mismatch")
    before_runtime = _runtime(stats_before)
    if before_runtime["calls"] != 0 or before_runtime["failures"] != 0:
        raise RuntimeError("hidden broker is not a fresh zero-call evaluator-owned instance")
    launcher = Path(__file__).with_name("lower_agent_launcher.py").resolve()
    records: dict[str, dict[str, Any]] = {}
    for case_id in selected:
        path = _case_path(cases_dir, case_id)
        if not path.is_file():
            raise RuntimeError(f"evaluator-issued hidden case is missing: {path}")
        visible, oracle, path, oracle_path = load_case_bundle(cases_dir, case_id)
        case_output = output / case_id
        runtime_candidate = case_output / "candidate_runtime"
        shutil.copytree(candidate, runtime_candidate, symlinks=False)
        for runtime_path in runtime_candidate.rglob("*"):
            if not runtime_path.is_symlink() and runtime_path.is_file():
                runtime_path.chmod(stat.S_IMODE(runtime_path.stat().st_mode) | 0o600)
        records[case_id] = _run_one(case_id, path, oracle_path, visible, oracle, runtime_candidate, candidate, output,
                                    broker_endpoint, launcher, timeout)
    stats_after = broker_stats(broker_endpoint)
    if not isinstance(stats_after, dict):
        raise RuntimeError("hidden broker stats disappeared after execution")
    digest_after = candidate_digest(candidate)
    if digest_after != frozen_digest:
        raise RuntimeError("frozen Candidate digest changed during hidden execution")
    ordered = [records[case_id] for case_id in selected if case_id in records]
    run = {
        "schema_version": "agentswe-claude-hidden-run/v1",
        "benchmark": "claude-policy-provenance-agentloop-v1",
        "formal_result_claimed": False,
        "pilot_not_formal": pilot_not_formal,
        "acceptance_not_formal": acceptance_not_formal,
        "hidden_after_freeze": True,
        "freeze_manifest": str(freeze_manifest.resolve()),
        "freeze_manifest_sha256": sha256_file(freeze_manifest.resolve()),
        "candidate_root": str(candidate),
        "candidate_digest": frozen_digest,
        "candidate_digest_after": digest_after,
        "case_inventory": list(selected),
        "cases": ordered,
        "broker": {
            "endpoint": broker_endpoint,
            "health": health,
            "required_model": MODEL,
            "required_reasoning_effort": REASONING_EFFORT,
            "before": stats_before,
            "after": stats_after,
            "before_runtime": before_runtime,
            "after_runtime": _runtime(stats_after),
            "execution_mode": "sequential-fresh-runtime-copy",
        },
        "source_records": {"scheduled_after_freeze_used_as_result": False},
    }
    write_json(output / "hidden_run.json", run)
    return run


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--freeze-manifest", type=Path, required=True)
    parser.add_argument("--cases-dir", type=Path, required=True,
                        help="evaluator-owned issued case specs; never test_cases/")
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--broker-endpoint", required=True)
    parser.add_argument("--case", dest="case_ids", action="append", choices=CASE_IDS)
    parser.add_argument("--timeout", type=int, default=600)
    parser.add_argument("--workers", type=int, default=1,
                        help="retained for CLI compatibility; formal execution is sequential for attribution")
    args = parser.parse_args(argv)
    result = run_hidden(freeze_manifest=args.freeze_manifest, cases_dir=args.cases_dir,
                        output=args.output, broker_endpoint=args.broker_endpoint,
                        case_ids=args.case_ids, timeout=args.timeout, workers=args.workers)
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
