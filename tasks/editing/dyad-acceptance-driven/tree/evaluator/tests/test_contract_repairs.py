#!/usr/bin/env python3
"""Provider-free regressions for the Dyad audit contract repairs."""
from __future__ import annotations

import importlib.util
import io
import json
import sys
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest import mock

from jsonschema import Draft7Validator


ROOT = Path(__file__).resolve().parents[2]


def load_module(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


lower = load_module("dyad_contract_lower", ROOT / "evaluator/harness/run_lower_agent_case.py")
headless = load_module("dyad_contract_headless", ROOT / "environment/headless_chat_flow.py")


def successful_artifact(workspace: dict[str, object]) -> dict[str, object]:
    return {
        "schema_version": "dyad-lower-agent-artifact-v3",
        "case_id": "test_001",
        "scenario_id": "test_001",
        "task_sha256": "c" * 64,
        "workspace": workspace,
    }


def successful_native_evidence(workspace: dict[str, object]) -> dict[str, object]:
    return {
        "real_product": True,
        "acceptance_surface_observed": True,
        "action_protocol_complete": True,
        "generic_fallback_used": False,
        "agent_artifact_origin": "model_finish_action",
        "case_id": "test_001",
        "scenario_id": "test_001",
        "executed_task_sha256": "c" * 64,
        "workspace": workspace,
    }


def lower_stats(*, calls: int, successful: int) -> dict[str, object]:
    return {
        "protocol": {"model": lower.MODEL, "reasoning_effort": lower.EFFORT},
        "runtime": {
            "calls": calls,
            "successful_calls": successful,
            "provider_failures": 0,
            "broker_failures": 0,
            "failures": 0,
            "client_failures": 0,
        },
    }


class ContractRepairTests(unittest.TestCase):
    def test_protocol_lock_matches_create_aligned_ten_round_lifecycle(self) -> None:
        lock = json.loads((ROOT / "protocol_lock.json").read_text(encoding="utf-8"))
        self.assertEqual(lock["max_candidate_rounds"], 10)
        self.assertEqual(lock["candidate_rounds"], "up_to_10")
        self.assertEqual(lock["feedback_revisions"], "fresh_after_each_acceptance")
        self.assertFalse(lock["dev_passed_is_automatic_freeze"])

    def test_emitted_result_envelope_matches_local_schema_v2(self) -> None:
        schema = json.loads((ROOT / "schemas/result.schema.json").read_text(encoding="utf-8"))
        self.assertEqual(
            schema["properties"]["schema_version"]["const"],
            lower.RESULT_SCHEMA_VERSION,
        )
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            case = root / "test_001" / "input.md"
            case.parent.mkdir()
            case.write_text("provider-free schema regression\n", encoding="utf-8")
            output = root / "result.json"
            argv = [
                "run_lower_agent_case.py",
                "--repository", str(root),
                "--case", str(case),
                "--broker-endpoint", "http://127.0.0.1:1/v1/responses",
                "--output", str(output),
            ]
            with mock.patch.object(lower, "broker_stats", side_effect=OSError("offline")), \
                    mock.patch.object(sys, "argv", argv), redirect_stdout(io.StringIO()):
                self.assertEqual(lower.main(), 0)
            result = json.loads(output.read_text(encoding="utf-8"))
        errors = sorted(Draft7Validator(schema).iter_errors(result), key=lambda item: list(item.path))
        self.assertEqual([error.message for error in errors], [])
        self.assertFalse(result["artifact_present"])
        self.assertEqual(result["broker"], {"before": {}, "after": {}})

    def test_numeric_app_chat_ids_and_session_are_valid_binding(self) -> None:
        workspace = {
            "app_id": 7,
            "chat_id": 11,
            "run_id": "run-1",
            "session_id": "session-1",
            "revision": "a" * 64,
            "target_fingerprint": "b" * 64,
        }
        self.assertTrue(lower.case_binding_complete(workspace))
        classification = lower.classify(
            mode="headless",
            launcher_exit=0,
            artifact=successful_artifact(workspace),
            native_evidence=successful_native_evidence(workspace),
            oracle_comparison={"passed": True},
            before=lower_stats(calls=0, successful=0),
            after=lower_stats(calls=1, successful=1),
        )
        self.assertEqual(classification, ("valid", None))

    def test_binding_fails_closed_without_session_or_with_invalid_numeric_ids(self) -> None:
        workspace = {
            "app_id": 7,
            "chat_id": 11,
            "run_id": "run-1",
            "session_id": "session-1",
            "revision": "a" * 64,
            "target_fingerprint": "b" * 64,
        }
        for key, invalid in (("session_id", ""), ("app_id", 0), ("chat_id", False)):
            with self.subTest(key=key, invalid=invalid):
                candidate = dict(workspace)
                candidate[key] = invalid
                self.assertFalse(lower.case_binding_complete(candidate))
                classification = lower.classify(
                    mode="headless",
                    launcher_exit=0,
                    artifact=successful_artifact(candidate),
                    native_evidence=successful_native_evidence(candidate),
                    oracle_comparison={"passed": True},
                    before=lower_stats(calls=0, successful=0),
                    after=lower_stats(calls=1, successful=1),
                )
                self.assertEqual(
                    classification,
                    ("candidate_failure", "acceptance_case_binding_incomplete"),
                )

    def test_headless_binding_has_no_synthetic_target_fingerprint_fallback(self) -> None:
        source = (ROOT / "environment/scenario_chat_flow.test.ts").read_text(encoding="utf-8")
        self.assertNotIn("sha256(JSON.stringify(target))", source)
        self.assertIn("session_id", source)
        self.assertIn("testFingerprint", source)
        self.assertIn("targetFingerprint", source)

    def test_formal_code_rubric_lookup_target_exists_and_matches_locked_dimensions(self) -> None:
        rubric_path = ROOT / "evaluator/code_rubric.md"
        self.assertTrue(rubric_path.is_file())
        rubric = rubric_path.read_text(encoding="utf-8")
        locked = json.loads((ROOT / "evaluator/code_rubric.json").read_text(encoding="utf-8"))
        for dimension, points in locked["dimensions"]:
            self.assertIn(f"`{dimension}`", rubric)
            self.assertIn(f"| {points} |", rubric)


if __name__ == "__main__":
    unittest.main()
