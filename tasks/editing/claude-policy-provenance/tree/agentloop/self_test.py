#!/usr/bin/env python3
"""Offline protocol and isolation self-test for the Claude Agent-loop adapter."""
from __future__ import annotations

import hashlib
import json
import os
import stat
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from agentloop.evaluator.broker import BrokerState, EFFORT, MODEL, PLACEHOLDER
from agentloop.evaluator.builder_protocol import (
    BUILDER_EFFORT,
    BUILDER_MODEL,
    load_and_verify_feedback,
    load_and_verify_witness,
    verify_submission_binding,
)
from agentloop.evaluator.code_rubric import DIMENSIONS
from agentloop.evaluator.controller import Controller
from agentloop.evaluator.hidden_executor import CASE_IDS, validate_freeze
from agentloop.evaluator.lower_agent_launcher import candidate_environment, valid_agent_result
from agentloop.evaluator.public_package import stage
from agentloop.protocol import RESULT_SCHEMA, assert_regular_tree, candidate_tree_digest, tree_digest, write_json


def check(condition: bool, message: str) -> None:
    if not condition:
        raise AssertionError(message)


def expect_error(function, fragment: str, message: str) -> None:
    try:
        function()
    except (RuntimeError, ValueError) as exc:
        check(fragment in str(exc), message)
    else:
        raise AssertionError(message)


def witness_value(*, patch_1: Path, patch_2: Path, digest_1: str, digest_2: str,
                  feedback_digest: str, **overrides: object) -> dict[str, object]:
    value: dict[str, object] = {
        "schema_version": "agentswe-builder-session-witness/v1",
        "model": BUILDER_MODEL,
        "reasoning_effort": BUILDER_EFFORT,
        "single_connection": True,
        "transport": "evaluator-owned-single-session",
        "session_id": "session-self-test",
        "connection_id": "connection-self-test",
        "builder_exit_code": 0,
        "feedback_consumed": True,
        "feedback_digest": feedback_digest,
        "latest_feedback_digest": feedback_digest,
        "submissions": [
            {"submission_number": 1, "session_id": "session-self-test", "connection_id": "connection-self-test",
             "patch": str(patch_1), "candidate_digest": digest_1},
            {"submission_number": 2, "session_id": "session-self-test", "connection_id": "connection-self-test",
             "patch": str(patch_2), "candidate_digest": digest_2, "feedback_digest": feedback_digest},
        ],
    }
    value.update(overrides)
    return value


def main() -> int:
    root = ROOT
    layer = root / "agentloop"
    lock = json.loads((layer / "protocol_lock.json").read_text(encoding="utf-8"))
    check(lock["lower_agent"] == {"model": MODEL, "reasoning_effort": EFFORT,
                                  "transport": "evaluator-owned-responses-broker"}, "lower protocol lock")
    check(lock["candidate_credential"] == PLACEHOLDER, "placeholder credential lock")
    check(sum(DIMENSIONS.values()) == 100 and len(DIMENSIONS) == 8, "eight code dimensions")
    check(len(list((layer / "cases").glob("dev_*.json"))) == 2, "two public dev cases")
    check(len([line for line in (root / "meta/hidden_test_inventory.md").read_text(encoding="utf-8").splitlines()
               if line.startswith("| `test_")]) == 6, "six hidden inventory rows")

    state = BrokerState()
    state.record(failed=False, usage={"input_tokens": 4, "output_tokens": 3, "total_tokens": 7})
    state.record(failed=True, failure_classification="provider_failure", status_code=502)
    state.record(failed=True, failure_classification="protocol_failure", status_code=401)
    stats = state.stats()
    runtime = stats["runtime"]
    check(runtime["calls"] == 3 and runtime["failures"] == 2 and runtime["successful_calls"] == 1,
          "broker basic counters")
    check(runtime["broker_failures"] == 0 and runtime["provider_failures"] == 1 and runtime["protocol_failures"] == 1,
          "broker mixed-provider attribution")
    check(stats["upstream"]["status_counts"] == {"401": 1, "502": 1}, "upstream status attribution")
    check(stats["credential"] == {"candidate_visible": PLACEHOLDER, "provider_secret_logged": False,
                                   "credential_value_recorded": False}, "credential redaction")

    with tempfile.TemporaryDirectory() as directory:
        temp = Path(directory)
        patch_1, patch_2, patch_3 = temp / "candidate-1.patch", temp / "candidate-2.patch", temp / "candidate-3.patch"
        patch_1.write_text("candidate one", encoding="utf-8")
        patch_2.write_text("candidate two", encoding="utf-8")
        patch_3.write_text("candidate three", encoding="utf-8")
        digest_1, digest_2, digest_3 = "1" * 64, "2" * 64, "3" * 64
        feedback_path = temp / "feedback.json"
        write_json(feedback_path, {
            "schema_version": "agentswe-edit-feedback/v1",
            "builder_session_id": "session-self-test",
            "connection_id": "connection-self-test",
            "candidate_1_digest": digest_1,
            "public_inventory": ["dev_001", "dev_002"],
            "oracle_included": False,
            "native_suite_used_as_result": False,
        })
        feedback_digest = hashlib.sha256(feedback_path.read_bytes()).hexdigest()
        witness_path = temp / "witness.json"
        write_json(witness_path, witness_value(patch_1=patch_1, patch_2=patch_2, digest_1=digest_1,
                                               digest_2=digest_2, feedback_digest=feedback_digest))
        witness = load_and_verify_witness(witness_path, expected_submissions=2)
        feedback, observed_feedback_digest = load_and_verify_feedback(
            feedback_path, session_id="session-self-test", candidate_1_digest=digest_1,
            connection_id="connection-self-test")
        check(feedback["feedback_digest"] == feedback_digest and observed_feedback_digest == feedback_digest,
              "feedback byte digest")
        verify_submission_binding(witness, number=1, patch=patch_1, candidate_digest=digest_1)
        verify_submission_binding(witness, number=2, patch=patch_2, candidate_digest=digest_2)
        feedback_2_path = temp / "feedback-2.json"
        write_json(feedback_2_path, {
            "schema_version": "agentswe-edit-feedback/v1",
            "builder_session_id": "session-self-test",
            "connection_id": "connection-self-test",
            "candidate_digest": digest_2,
            "candidate_1_digest": digest_2,
            "public_inventory": ["dev_001", "dev_002"],
            "oracle_included": False,
            "native_suite_used_as_result": False,
        })
        feedback_2_digest = hashlib.sha256(feedback_2_path.read_bytes()).hexdigest()
        witness_3 = witness_value(patch_1=patch_1, patch_2=patch_2, digest_1=digest_1,
                                  digest_2=digest_2, feedback_digest=feedback_digest)
        witness_3["feedback_digest"] = feedback_2_digest
        witness_3["latest_feedback_digest"] = feedback_2_digest
        witness_3["submissions"].append({
            "submission_number": 3, "session_id": "session-self-test",
            "connection_id": "connection-self-test", "patch": str(patch_3),
            "candidate_digest": digest_3, "feedback_digest": feedback_2_digest,
        })
        witness_3_path = temp / "witness-3.json"
        write_json(witness_3_path, witness_3)
        verified_3 = load_and_verify_witness(witness_3_path, expected_submissions=3)
        verify_submission_binding(verified_3, number=3, patch=patch_3, candidate_digest=digest_3)
        provisional_path = temp / "provisional-witness.json"
        provisional = witness_value(patch_1=patch_1, patch_2=patch_2, digest_1=digest_1,
                                    digest_2=digest_2, feedback_digest=feedback_digest)
        provisional["feedback_consumed"] = False
        provisional["feedback_digest"] = None
        provisional["latest_feedback_digest"] = None
        provisional["submissions"] = provisional["submissions"][:1]
        write_json(provisional_path, provisional)
        staged_witness = load_and_verify_witness(provisional_path, expected_submissions=1)
        verify_submission_binding(staged_witness, number=1, patch=patch_1, candidate_digest=digest_1)
        expect_error(lambda: load_and_verify_witness(provisional_path, expected_submissions=2), "exactly 2",
                     "provisional witness rejected at final gate")
        expect_error(lambda: load_and_verify_witness(temp / "bad-model.json"), "unreadable", "missing witness rejected")

        bad_model = witness_value(patch_1=patch_1, patch_2=patch_2, digest_1=digest_1,
                                   digest_2=digest_2, feedback_digest=feedback_digest,
                                   model="wrong-model")
        bad_model_path = temp / "bad-model.json"
        write_json(bad_model_path, bad_model)
        expect_error(lambda: load_and_verify_witness(bad_model_path, expected_submissions=2), "model/reasoning", "wrong builder model rejected")

        bad_connection = witness_value(patch_1=patch_1, patch_2=patch_2, digest_1=digest_1,
                                        digest_2=digest_2, feedback_digest=feedback_digest,
                                        single_connection=False)
        bad_connection_path = temp / "bad-connection.json"
        write_json(bad_connection_path, bad_connection)
        expect_error(lambda: load_and_verify_witness(bad_connection_path, expected_submissions=2), "single continuous", "non-continuous builder rejected")

        bad_feedback = witness_value(patch_1=patch_1, patch_2=patch_2, digest_1=digest_1,
                                     digest_2=digest_2, feedback_digest=feedback_digest,
                                     latest_feedback_digest="3" * 64)
        bad_feedback_path = temp / "bad-feedback-witness.json"
        write_json(bad_feedback_path, bad_feedback)
        expect_error(lambda: load_and_verify_witness(bad_feedback_path, expected_submissions=2), "feedback digest", "unbound feedback rejected")

        bad_same_digest = witness_value(patch_1=patch_1, patch_2=patch_2, digest_1=digest_1,
                                        digest_2=digest_1, feedback_digest=feedback_digest)
        bad_same_path = temp / "same-digest-witness.json"
        write_json(bad_same_path, bad_same_digest)
        expect_error(lambda: load_and_verify_witness(bad_same_path, expected_submissions=2), "not distinct", "same Candidate digest rejected")
        bad_connection_feedback = temp / "bad-feedback-connection.json"
        bad_feedback_value = json.loads(feedback_path.read_text(encoding="utf-8"))
        bad_feedback_value["connection_id"] = "other-connection"
        write_json(bad_connection_feedback, bad_feedback_value)
        expect_error(lambda: load_and_verify_feedback(
            bad_connection_feedback, session_id="session-self-test", candidate_1_digest=digest_1,
            connection_id="connection-self-test"), "different Builder connection",
                     "feedback connection binding rejected")

        candidate = temp / "candidate"
        candidate.mkdir()
        (candidate / "product.txt").write_text("product", encoding="utf-8")
        before_digest = candidate_tree_digest(candidate)
        (candidate / ".agentloop_build.json").write_text("evaluator metadata", encoding="utf-8")
        check(candidate_tree_digest(candidate) == before_digest, "build metadata excluded from Candidate digest")
        symlink = candidate / "bad-link"
        symlink.symlink_to(candidate / "product.txt")
        expect_error(lambda: assert_regular_tree(candidate), "regular-file-only", "Candidate symlink rejected")

        os.environ["OPENAI_API_KEY"] = "test-provider-secret"
        try:
            env = candidate_environment(workspace=temp / "workspace", plugin=candidate, state=temp / "state")
        finally:
            os.environ.pop("OPENAI_API_KEY", None)
        check("OPENAI_API_KEY" not in env and "DEEPSEEK_API_KEY" not in env and env["AGENTSWE_CREDENTIAL"] == PLACEHOLDER,
              "Candidate-only credential environment")

        controller = Controller(repository=temp, cases=temp, run_dir=temp / "run",
                                 broker_endpoint="http://127.0.0.1:18080/v1/responses")
        expect_error(controller.hidden, "before Candidate freeze", "hidden pre-freeze rejection")
        controller.frozen = {"schema_version": "agentswe-edit-freeze-manifest/v1", "candidate_digest": "a" * 64,
                             "candidate_root": str(candidate), "freeze_reason": "self-test",
                             "hidden_gate": "issued-after-dev-feedback", "frozen_at_unix": 0}
        expect_error(controller.hidden, "evaluator-issued", "hidden requires evaluator-issued case specs")

        freeze_controller = Controller(repository=temp, cases=temp, run_dir=temp / "freeze-run",
                                       broker_endpoint="http://127.0.0.1:18080/v1/responses",
                                       max_dev_rounds=10)
        final_candidate = temp / "freeze-run" / "candidate_003"
        final_candidate.mkdir(parents=True)
        (final_candidate / "product.txt").write_text("third candidate", encoding="utf-8")
        final_digest = candidate_tree_digest(final_candidate)
        freeze_controller.rounds = [
            {"submission_number": 1, "candidate_digest": digest_1,
             "builder_session_id": "session-self-test", "builder_connection_id": "connection-self-test"},
            {"submission_number": 2, "candidate_digest": digest_2,
             "builder_session_id": "session-self-test", "builder_connection_id": "connection-self-test",
             "feedback": {"feedback_digest": feedback_digest, "feedback_consumed": True}},
            {"submission_number": 3, "candidate_digest": final_digest,
             "builder_session_id": "session-self-test", "builder_connection_id": "connection-self-test",
             "feedback": {"feedback_digest": feedback_2_digest, "feedback_consumed": True}},
        ]
        latest_freeze = freeze_controller.freeze_latest("builder_exit")
        check(latest_freeze["source_submission"] == 3 and latest_freeze["accepted_submission_count"] == 3
              and latest_freeze["accepted_candidate_digests"] == [digest_1, digest_2, final_digest]
              and latest_freeze["candidate_digest"] == final_digest,
              "freeze binds latest accepted Candidate after a variable-length lifecycle")

        frozen = temp / "frozen"
        frozen.mkdir()
        (frozen / "file.txt").write_text("frozen", encoding="utf-8")
        for path in [frozen, frozen / "file.txt"]:
            path.chmod(stat.S_IMODE(path.stat().st_mode) & ~0o222)
        manifest = {
            "schema_version": "agentswe-edit-freeze-manifest/v2",
            "source_submission": 2,
            "source_submission_id": "candidate_002",
            "accepted_submission_count": 2,
            "max_dev_rounds": 10,
            "accepted_candidate_digests": ["1" * 64, "2" * 64],
            "candidate_digest": candidate_tree_digest(frozen),
            "candidate_root": str(frozen),
            "builder_session_id": "session-self-test",
            "builder_connection_id": "connection-self-test",
            "feedback_digest": feedback_digest,
            "feedback_consumed": True,
            "feedback_chain_complete": True,
            "freeze_reason": "builder_exit",
            "hidden_gate": "issued-after-freeze",
            "frozen_at": "2026-09-05T00:00:00Z",
            "hidden_allowed": True,
            "dev_evaluated": True,
            "immutable_candidate": True,
            "frozen_tree_read_only": True,
            "frozen_tree_regular": True,
            "credential_mounted_to_candidate": False,
            "hidden_case_inventory": list(CASE_IDS),
            "pilot_not_formal": False,
        }
        manifest_path = temp / "freeze_manifest.json"
        write_json(manifest_path, manifest)
        loaded, loaded_candidate, loaded_digest = validate_freeze(manifest_path)
        check(loaded["hidden_case_inventory"] == list(CASE_IDS) and loaded_candidate == frozen.resolve()
              and loaded_digest == manifest["candidate_digest"], "freeze manifest guard")

    check(len(tree_digest(layer)) == 64, "deterministic tree digest")
    check(RESULT_SCHEMA.endswith("/v1"), "result schema constant")
    authored_result = {
        "schema_version": RESULT_SCHEMA,
        "case_id": "test_001",
        "observations": [],
        "integrity": {},
        "decision": {"completion_claim": "partial"},
        "safety": {"followed_unobserved_instruction": False},
        "binding": {
            "case_id": "test_001", "runtime_nonce": "case-self-test",
            "case_spec_sha256": "a" * 64, "case_contract_digest": "b" * 64,
            "execution_nonce": "execution-self-test",
            "selected_action_ids": ["inspect"], "product_event_digests": ["c" * 64],
            "observation_digest": "d" * 64, "product_trajectory_digest": "e" * 64,
        },
    }
    check(valid_agent_result(authored_result, "test_001"), "valid lower-agent artifact contract")
    invalid_result = dict(authored_result)
    invalid_result["decision"] = {"completion_claim": "complete"}
    invalid_result["safety"] = {"followed_unobserved_instruction": "false"}
    check(not valid_agent_result(invalid_result, "test_001"), "invalid lower-agent artifact rejected")
    launcher_source = (layer / "evaluator/lower_agent_launcher.py").read_text(encoding="utf-8")
    check("artifact_authored = False" in launcher_source and "return 0 if artifact_authored else 2" in launcher_source,
          "launcher fail-closed artifact gate")
    check('"origin": "lower_model_final_response"' in launcher_source and '"evaluator_synthesized": False' in launcher_source,
          "lower-agent artifact provenance attestation")
    check('write_json(output / "agent_result.json", answer)' not in launcher_source,
          "launcher does not unconditionally synthesize agent_result")
    controller_source = (layer / "evaluator/controller.py").read_text(encoding="utf-8")
    check('output=self.run_dir / "lifecycle/hidden_after_freeze_attestation.json"' in controller_source,
          "formal hidden attestation uses the finalizer canonical path")
    one_stop_source = (root / "harbor/formal_one_stop.py").read_text(encoding="utf-8")
    check('"hidden_attestation": "pilot_hidden_attestation.json"' in one_stop_source,
          "pilot summary points to the root-level attestation")
    with tempfile.TemporaryDirectory() as directory:
        staged = Path(directory) / "public"
        package = stage(root, staged)
        check(package["visible_roots"] == ["input", "dev_cases"], "public package roots")
        check(not (staged / "test_cases").exists() and not (staged / "evaluator").exists(),
              "public package hidden isolation")
    print("SELF_TEST=PASS checks=36 network_calls=0 formal_result_claimed=false")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
