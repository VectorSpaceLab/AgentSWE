#!/usr/bin/env python3
"""Provider-free Stage A contract checks for the DeepTutor sibling."""

from __future__ import annotations

import json
from pathlib import Path

try:
    from .protocol import (
        BUILDER_EFFORT,
        BUILDER_MODEL,
        DEV_CASES,
        HIDDEN_CASES,
        LOWER_EFFORT,
        LOWER_MODEL,
    )
except ImportError:  # pragma: no cover
    from protocol import (  # type: ignore
        BUILDER_EFFORT,
        BUILDER_MODEL,
        DEV_CASES,
        HIDDEN_CASES,
        LOWER_EFFORT,
        LOWER_MODEL,
    )


ROOT = Path(__file__).resolve().parents[1]


def main() -> int:
    errors: list[str] = []
    lock = json.loads((ROOT / "agentloop/protocol_lock.json").read_text(encoding="utf-8"))
    inventory = json.loads((ROOT / "agentloop/case_inventory.json").read_text(encoding="utf-8"))
    if lock.get("lower_model") != LOWER_MODEL or lock.get("lower_reasoning_effort") != LOWER_EFFORT:
        errors.append("model lock drift")
    if lock.get("builder_model") != BUILDER_MODEL or lock.get("builder_reasoning_effort") != BUILDER_EFFORT:
        errors.append("Builder model lock drift")
    lifecycle = lock.get("lifecycle")
    expected_lifecycle = [
        "accepted_candidate_1..10",
        "dev_001+dev_002_after_each_acceptance",
        "feedback_after_each_acceptance",
        "freeze_latest_after_builder_exit_or_max_rounds",
        "hidden_after_freeze",
    ]
    if lifecycle != expected_lifecycle:
        errors.append("lifecycle lock drift")
    if tuple(lock.get("public_cases", [])) != DEV_CASES or tuple(lock.get("hidden_cases", [])) != HIDDEN_CASES:
        errors.append("lock inventory drift")
    if len(inventory.get("public_dev", [])) != 2 or len(inventory.get("hidden", [])) != 6:
        errors.append("inventory is not 2+6")
    for case in DEV_CASES:
        if not (ROOT / "dev_cases" / case / "input.md").is_file():
            errors.append(f"missing public input {case}")
    for case in HIDDEN_CASES:
        if not (ROOT / "test_cases" / case / "input.md").is_file():
            errors.append(f"missing hidden input {case}")
        if not (ROOT / "evaluator/cases" / f"{case}.json").is_file():
            errors.append(f"missing hidden evaluator case {case}")
    for required in (
        "agentloop/broker.py",
        "agentloop/candidate_adapter.py",
        "agentloop/lower_agent_launcher.py",
        "agentloop/two_round_controller.py",
        "agentloop/run_hidden.py",
        "agentloop/result_evaluator.py",
        "agentloop/code_score_runner.py",
        "harbor/formal_one_stop.py",
        "evaluator/result_rubric.md",
        "evaluator/code_rubric.md",
        "schemas/freeze_manifest.schema.json",
        "schemas/broker_stats.schema.json",
        "schemas/infra_classification.schema.json",
        "schemas/result.schema.json",
    ):
        if not (ROOT / required).is_file():
            errors.append(f"missing {required}")
    hidden_runner = (ROOT / "agentloop/run_hidden.py").read_text(encoding="utf-8")
    for required in ("evidence-manifest", "artifact_owner", "lower_agent_product", "trajectory"):
        if required not in hidden_runner:
            errors.append(f"hidden artifact provenance missing {required}")
    lower_entry = (ROOT / "agentloop/lower_agent_entry.py").read_text(encoding="utf-8")
    for required in (
        "DEEPTUTOR_AGENT_RESULT",
        "Required terminal artifact protocol",
        "evaluator_synthesized",
        "lower_agent_product",
    ):
        if required not in lower_entry:
            errors.append(f"lower product artifact boundary missing {required}")
    freeze_schema = json.loads(
        (ROOT / "schemas/freeze_manifest.schema.json").read_text(encoding="utf-8")
    )
    required_freeze_fields = {
        "schema_version",
        "source_submission",
        "session_id",
        "builder_witness_digest",
        "candidate_digest",
        "feedback_received",
        "same_session_verified",
        "hidden_allowed",
        "frozen_tree_read_only",
        "frozen_tree_regular",
        "frozen_at",
    }
    if not required_freeze_fields.issubset(set(freeze_schema.get("required", []))):
        errors.append("freeze schema does not require immutable same-session fields")
    code_weights = [15, 20, 15, 15, 10, 10, 10, 5]
    if sum(code_weights) != 100:
        errors.append("code weights drift")
    if errors:
        for error in errors:
            print(f"ERROR: {error}")
        return 1
    print("SELF_TEST=PASS inventory=2+6 lifecycle=accepted-submissions-1..10 hidden-after-freeze=guarded code_total=100 provider_calls=0 status=PARTIAL")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
