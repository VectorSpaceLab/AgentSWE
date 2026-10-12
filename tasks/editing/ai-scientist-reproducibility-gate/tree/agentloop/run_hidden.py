#!/usr/bin/env python3
"""Gate hidden execution on the latest accepted Candidate freeze.

The accepted-submission ledger is evaluator-owned state in
``dev_lifecycle.json``.  Hidden execution may use only the immutable freeze
that is explicitly bound to the ledger's latest accepted entry.  The gate is
deliberately independent of how many accepted submissions the Builder made:
the permitted range is one through ten.
"""

from __future__ import annotations

import argparse
import json
import stat
from pathlib import Path
from typing import Any

try:
    from .protocol import read_json, sha256_file, tree_digest, write_json
except ImportError:  # direct execution from the agentloop directory
    from protocol import read_json, sha256_file, tree_digest, write_json


PUBLIC_CASES = ("dev_001", "dev_002")
HIDDEN_CASES = tuple(f"test_{index:03d}" for index in range(1, 7))
MAX_ACCEPTED_SUBMISSIONS = 10
FREEZE_REASONS = {"max_dev_rounds", "builder_exit"}


def _failure(error: str, *, details: list[str] | None = None) -> dict[str, Any]:
    value: dict[str, Any] = {
        "schema_version": 1,
        "valid": False,
        "classification": "protocol_error",
        "error": error,
    }
    if details:
        value["details"] = details
    return value


def _hash(value: Any) -> bool:
    return isinstance(value, str) and len(value) == 64 and all(
        character in "0123456789abcdef" for character in value
    )


def _accepted_ledger(run_dir: Path, public_case_ids=PUBLIC_CASES) -> tuple[list[dict[str, Any]] | None, list[str]]:
    path = run_dir / "dev_lifecycle.json"
    if not path.is_file():
        return None, ["accepted_submission_ledger_missing"]
    try:
        lifecycle = read_json(path)
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        return None, [f"accepted_submission_ledger_invalid:{type(exc).__name__}"]
    records = lifecycle.get("records")
    if not isinstance(records, list) or not 1 <= len(records) <= MAX_ACCEPTED_SUBMISSIONS:
        return None, ["accepted_submission_count_not_in_1..10"]

    errors: list[str] = []
    if not all(isinstance(record, dict) for record in records):
        return None, ["accepted_submission_record_is_not_an_object"]
    if lifecycle.get("dev_passed_is_automatic_freeze") is not False:
        errors.append("dev_passed_must_not_automatically_freeze")
    for index, record in enumerate(records, 1):
        if not isinstance(record, dict):
            errors.append(f"accepted_submission_{index}_is_not_an_object")
            continue
        if record.get("submission_number") != index:
            errors.append(f"accepted_submission_{index}_out_of_order")
        if not _hash(record.get("candidate_digest")):
            errors.append(f"accepted_submission_{index}_candidate_digest_invalid")
        if not isinstance(record.get("feedback_digest"), str) or not _hash(record.get("feedback_digest")):
            errors.append(f"accepted_submission_{index}_fresh_feedback_missing")
        if index > 1 and record.get("feedback_digest_ack") != records[index - 2].get("feedback_digest"):
            errors.append(f"accepted_submission_{index}_feedback_chain_broken")
        dev = record.get("dev")
        if not isinstance(dev, dict) or set(dev) != set(public_case_ids):
            errors.append(f"accepted_submission_{index}_public_inventory_incomplete")
        if not record.get("builder_session_id") or record.get("builder_session_id") != records[0].get("builder_session_id"):
            errors.append(f"accepted_submission_{index}_builder_session_mismatch")
        feedback_path = Path(str(record.get("feedback_path", "")))
        expected_feedback = run_dir / "feedback" / f"candidate_{index:03d}.json"
        if (feedback_path.resolve() != expected_feedback.resolve() or feedback_path.is_symlink()
                or not feedback_path.is_file() or sha256_file(feedback_path) != record.get("feedback_digest")):
            errors.append(f"accepted_submission_{index}_feedback_evidence_missing_or_changed")
    digests = [record.get("candidate_digest") for record in records if isinstance(record, dict)]
    if len(digests) != len(set(digests)):
        errors.append("accepted_candidate_digests_not_unique")
    return records, errors


def _validate_freeze(run_dir: Path, manifest: dict[str, Any], *, public_case_ids=PUBLIC_CASES) -> tuple[dict[str, Any] | None, list[str]]:
    records, errors = _accepted_ledger(run_dir, public_case_ids)
    if records is None:
        return None, errors
    try:
        if read_json(run_dir / "freeze_manifest.json") != manifest:
            errors.append("persisted_freeze_manifest_differs_from_controller")
    except (OSError, ValueError, json.JSONDecodeError):
        errors.append("persisted_freeze_manifest_missing_or_invalid")
    accepted_count = len(records)
    source_submission = manifest.get("source_submission")
    if not isinstance(source_submission, int) or not 1 <= source_submission <= MAX_ACCEPTED_SUBMISSIONS:
        errors.append("freeze_source_submission_not_in_1..10")
    if source_submission != accepted_count:
        errors.append("freeze_is_not_bound_to_latest_accepted_submission")
    if manifest.get("accepted_submission_count") != accepted_count:
        errors.append("freeze_accepted_submission_count_mismatch")

    ledger_digests = [record.get("candidate_digest") for record in records]
    manifest_digests = manifest.get("accepted_candidate_digests")
    if manifest_digests != ledger_digests:
        errors.append("freeze_accepted_candidate_ledger_mismatch")
    latest_delivery_digest = ledger_digests[-1]
    if manifest.get("candidate_delivery_digest") != latest_delivery_digest:
        errors.append("freeze_delivery_digest_is_not_latest_accepted_digest")
    if manifest.get("source_submission_id") != f"candidate-{accepted_count:03d}":
        errors.append("freeze_source_submission_id_mismatch")
    if manifest.get("freeze_reason") not in FREEZE_REASONS:
        errors.append("freeze_reason_invalid")
    maximum = manifest.get("max_dev_rounds")
    if not isinstance(maximum, int) or not 1 <= maximum <= MAX_ACCEPTED_SUBMISSIONS:
        errors.append("max_dev_rounds_not_in_1..10")
    elif accepted_count > maximum:
        errors.append("accepted_submissions_exceed_max_dev_rounds")
    elif manifest.get("freeze_reason") == "max_dev_rounds" and accepted_count != maximum:
        errors.append("max_dev_rounds_freeze_before_submission_limit")
    if manifest.get("public_case_inventory") != list(public_case_ids):
        errors.append("freeze_public_case_inventory_mismatch")
    if manifest.get("n_concurrent") != 1:
        errors.append("n_concurrent_must_equal_1")
    if manifest.get("dev_passed_is_automatic_freeze") is not False:
        errors.append("dev_passed_must_not_automatically_freeze")
    if manifest.get("feedback_consumed") is not True or manifest.get("feedback_chain_complete") is not True:
        errors.append("accepted_feedback_chain_is_not_complete")
    revision = manifest.get("revision_contract") if isinstance(manifest.get("revision_contract"), dict) else {}
    requires_revision = accepted_count > 1
    if revision.get("requires_feedback_bound_later_distinct_candidate") is not requires_revision:
        errors.append("revision_contract_does_not_match_actual_submissions")
    if manifest.get("feedback_digest") != records[-1].get("feedback_digest"):
        errors.append("freeze_feedback_digest_is_not_latest")
    if manifest.get("feedback_digest_ack") != records[-1].get("feedback_digest_ack"):
        errors.append("freeze_feedback_ack_is_not_latest")
    if requires_revision:
        if revision.get("later_distinct_candidate_present") is not True:
            errors.append("feedback_bound_later_distinct_candidate_missing")
        product_key = ('product_source_digest' if 'product_source_digest' in manifest else 'candidate_repo_digest')
        if (records[-1].get("role") != "feedback_revision"
                or not isinstance(records[-1].get("build", {}).get(product_key), str)
                or not isinstance(records[-2].get("build", {}).get(product_key), str)
                or records[-1].get("build", {}).get(product_key) == records[-2].get("build", {}).get(product_key)):
            errors.append("later_distinct_product_revision_missing")
        if revision.get("feedback_consumed") is not True:
            errors.append("revision_feedback_consumption_not_attested")
    elif revision.get("later_distinct_candidate_present") is not False:
        errors.append("single_submission_claims_nonexistent_revision")
    if manifest.get("dev_evaluated") is not True:
        errors.append("accepted_submissions_were_not_evaluated")
    if manifest.get("hidden_allowed") is not True:
        errors.append("hidden_not_allowed_before_verified_freeze")
    if manifest.get("frozen_tree_read_only") is not True or manifest.get("frozen_tree_regular") is not True:
        errors.append("frozen_tree_is_not_immutable_regular_tree")

    frozen_value = manifest.get("frozen_candidate_path", manifest.get("candidate_path"))
    frozen = Path(str(frozen_value or "")).resolve()
    if not frozen.is_dir():
        errors.append("frozen_candidate_repository_missing")
    expected_digest = manifest.get("candidate_digest", manifest.get("candidate_materialized_digest"))
    if not _hash(expected_digest):
        errors.append("frozen_candidate_digest_invalid")
    elif frozen.is_dir() and tree_digest(frozen) != expected_digest:
        errors.append("frozen_candidate_digest_changed")
    if frozen.is_dir() and any(item.is_symlink() for item in frozen.rglob("*")):
        errors.append("frozen_candidate_contains_symlink")
    if frozen.is_dir() and any(
        (item.stat().st_mode & 0o222) != 0 for item in (frozen, *frozen.rglob("*"))
    ):
        errors.append("frozen_candidate_is_not_read_only")

    if errors:
        return None, errors
    return {
        "manifest": manifest,
        "frozen_candidate": frozen,
        "accepted_count": accepted_count,
        "frozen_digest": expected_digest,
        "freeze_manifest_sha256": sha256_file(run_dir / "freeze_manifest.json"),
    }, []


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run-dir", type=Path, required=True)
    parser.add_argument("--result", type=Path, required=True)
    args = parser.parse_args()
    run_dir = args.run_dir.resolve()
    freeze_path = run_dir / "freeze_manifest.json"
    if not freeze_path.is_file():
        value = _failure("hidden_before_freeze")
        write_json(args.result.resolve(), value)
        print(json.dumps(value, indent=2))
        return 1

    try:
        manifest = read_json(freeze_path)
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        value = _failure("freeze_manifest_invalid", details=[f"{type(exc).__name__}"])
        write_json(args.result.resolve(), value)
        print(json.dumps(value, indent=2))
        return 1

    verified, errors = _validate_freeze(run_dir, manifest)
    if verified is None:
        value = _failure("freeze_manifest_not_latest_accepted_candidate", details=errors)
        write_json(args.result.resolve(), value)
        print(json.dumps(value, indent=2))
        return 1

    value = {
        "schema_version": 1,
        "valid": True,
        "classification": "hidden_after_freeze",
        "accepted_submission_count": verified["accepted_count"],
        "frozen_candidate_digest": verified["frozen_digest"],
        "frozen_digest": verified["frozen_digest"],
        "freeze_manifest_sha256": verified["freeze_manifest_sha256"],
        "case_ids": list(HIDDEN_CASES),
    }
    write_json(args.result.resolve(), value)
    print(json.dumps(value, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
