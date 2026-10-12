#!/usr/bin/env python3
from __future__ import annotations

import json
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from controller.two_round_controller import TwoRoundController, tree_digest
from evaluator.broker.candidate_broker import EFFORT, MODEL, locked_payload
from evaluator.code_runner import score
from evaluator.harness.evaluate_suite import HIDDEN, validate_freeze


def main() -> int:
    assert locked_payload({"model": "wrong", "reasoning": {"effort": "wrong"}}, "/v1/responses")["model"] == MODEL
    assert locked_payload({}, "/v1/responses")["reasoning"]["effort"] == EFFORT
    assert score({
        "interface_lifecycle": 15, "requirement_mechanism_coverage": 20,
        "analysis_evidence_integrity": 15, "safety_privacy_side_effects": 15,
        "recovery_honest_failure": 10, "testability_observability": 10,
        "maintainability_generalization": 10, "resource_discipline": 5,
    })["total"] == 100
    assert HIDDEN == tuple(f"test_{index:03d}" for index in range(1, 7))
    validate_freeze({"schema_version": "dyad-agentloop-freeze-v1", "candidate_digest": "a" * 64, "candidate_1_digest": "b" * 64, "candidate_2_digest": "c" * 64, "source_submission_id": "candidate-002", "builder_session_id": "session-smoke", "candidate_1_feedback_sha256": "d" * 64, "frozen_at": "2026-09-01T00:00:00+00:00"})
    with tempfile.TemporaryDirectory() as temp:
        root = Path(temp); c1 = root / "c1"; c2 = root / "c2"
        c1.mkdir(); c2.mkdir(); (c1 / "x").write_text("one\n"); (c2 / "x").write_text("two\n")
        calls = []
        def evaluate(number: int, _path: Path) -> dict[str, object]:
            calls.append(number)
            return {
                "dev_001": {
                    "classification": "candidate_failure",
                    "failure_class": "acceptance_surface_missing",
                    "product_entry_observed": True,
                    "broker_calls_delta": 1,
                    "broker_successful_calls_delta": 1,
                    "artifact": {"real_product": True, "success": False},
                },
                "dev_002": {
                    "classification": "candidate_failure",
                    "failure_class": "acceptance_surface_missing",
                    "product_entry_observed": True,
                    "broker_calls_delta": 1,
                    "broker_successful_calls_delta": 1,
                    "artifact": {"real_product": True, "success": False},
                },
            }
        controller = TwoRoundController(root / "run", evaluate)
        first = controller.submit(c1)
        controller.submit(c2, builder_session_id=controller.builder_session_id, feedback_sha256=first.feedback_sha256)
        frozen = controller.freeze_candidate_2()
        assert calls == [1, 2]
        assert frozen["schema_version"] == "dyad-agentloop-freeze-v2"
        assert frozen["source_submission"] == 2
        assert frozen["accepted_submission_count"] == 2
        assert len(frozen["accepted_candidate_digests"]) == 2
        assert len(set(frozen["accepted_candidate_digests"])) == 2
        assert frozen["feedback_consumed"] is True
        assert frozen["feedback_chain_complete"] is True
        try:
            controller.run_hidden(lambda _path: {"case_id": "test_001", "classification": "not-run"})
        except RuntimeError as error:
            assert "stub" in str(error)
        else:
            raise AssertionError("stub hidden result was accepted")
        hidden = controller.run_hidden(lambda _path: {
            "schema_version": "dyad-agentloop-hidden-suite-v1",
            "hidden_after_freeze": True,
            "cases": {
                f"test_{index:03d}": {
                    "classification": "candidate_failure",
                    "product_entry_observed": True,
                    "artifact_present": True,
                    "broker_calls_delta": 1,
                    "broker_successful_calls_delta": 1,
                }
                for index in range(1, 7)
            },
        })
        assert len(hidden["cases"]) == 6
        assert tree_digest(root / "run" / "frozen_candidate") == frozen["candidate_digest"]
    print("SELF_TEST=PASS; lifecycle=accepted-submission-ledger; stub-rejection=PASS; hidden-after-freeze=PASS; code-axis=100; broker-calls=0 (static only)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
