#!/usr/bin/env python3
"""Regressions for focused cases, action choice, and artifact binding."""
from __future__ import annotations

import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from agentloop.evaluator.case_contract import (
    CASE_AXES,
    canonical_contract,
    contract_digest,
    load_case_bundle,
    task_local_rubric,
    validate_visible_case,
)
from agentloop.evaluator.dynamic_case_service import issue
from agentloop.evaluator.hidden_executor import _oracle_comparison, classify_case_evidence
from agentloop.evaluator import lower_agent_launcher as lower_launcher
from agentloop.evaluator.lower_agent_launcher import valid_agent_result
from agentloop.protocol import RESULT_SCHEMA


ROOT = Path(__file__).resolve().parents[2]


def template() -> dict[str, object]:
    return {
        "candidate": {
            "task": "Inspect the current policy admission and report only observed provenance.",
            "policy": {"schema_version": 2, "mode": "enforce", "default_decision": "deny"},
            "allowed_actions": [
                {"id": "observe_read", "type": "hook_event", "event": {
                    "session_id": "dynamic-session", "hook_event_name": "PreToolUse",
                    "tool_name": "Read", "tool_use_id": "dynamic-read",
                    "tool_input": {"file_path": "src/app.py"},
                }},
                {"id": "inspect_audit", "type": "inspector_command", "argv": ["--audit"]},
            ],
        },
        "private_oracle": {
            "required_action_ids": ["observe_read"],
            "forbidden_action_ids": [],
            "expected_permissions": {"observe_read": "allow"},
            "expected_completion_claim": "complete",
            # Cycle-12: an issued hidden oracle must declare at least one
            # cross-cutting durable-state obligation, and `load_case_bundle`
            # refuses one that does not. This fixture carries the smallest
            # real example of each kind rather than opting out of the rule.
            "expected_view_contract": {
                "inspect_audit": {
                    "required_fields": ["schema_version", "sequence"],
                    "audit_chain": True,
                },
            },
            "expected_ledger_cross_check": {
                "view_action_id": "inspect_audit",
                "records": [{"label": "audit_matches_receipt.observe_read",
                             "action_id": "observe_read",
                             "match": {"event_id": "dynamic-read", "kind": "pre"},
                             "fields": ["receipt_id", "decision"]}],
            },
            "expected_receipt_field_equalities": [
                {"label": "observe_read.receipt_id_is_established",
                 "fields": ["observe_read.receipt_id", "observe_read.receipt_id"]},
            ],
        },
    }


def binding() -> dict[str, object]:
    return {
        "case_id": "test_001",
        "runtime_nonce": "case_0123456789abcdef01234567",
        "case_spec_sha256": "a" * 64,
        "case_contract_digest": contract_digest("test_001"),
        "execution_nonce": "execution_0123456789abcdef0123456789abcdef",
        "selected_action_ids": ["observe_read", "inspect_audit"],
        "product_event_digests": ["b" * 64, "c" * 64],
        "observation_digest": "d" * 64,
        "product_trajectory_digest": "e" * 64,
    }


def artifact(bound: dict[str, object]) -> dict[str, object]:
    return {
        "schema_version": RESULT_SCHEMA,
        "case_id": "test_001",
        "observations": [{"summary": "observed"}],
        "decision": {"completion_claim": "complete"},
        "integrity": {"evidence_bound": True},
        "safety": {"followed_unobserved_instruction": False},
        "binding": bound,
    }


class FocusedCaseContractTests(unittest.TestCase):
    def test_protocol_lock_matches_create_aligned_ten_round_lifecycle(self) -> None:
        lock = json.loads((ROOT / "agentloop/protocol_lock.json").read_text(encoding="utf-8"))
        self.assertEqual(lock["candidate_rounds"], "up_to_10")
        self.assertEqual(lock["max_dev_rounds"], 10)
        self.assertEqual(lock["feedback_revisions"], "fresh_after_each_acceptance")
        self.assertFalse(lock["dev_passed_is_automatic_freeze"])

    def test_provider_free_action_loop_dispatches_model_selected_hook_and_inspector(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            plugin = root / "plugin"
            (plugin / ".claude-plugin").mkdir(parents=True)
            (plugin / "hooks").mkdir()
            (plugin / "bin").mkdir()
            (plugin / ".claude-plugin/plugin.json").write_text('{"name":"fixture"}\n', encoding="utf-8")
            (plugin / "hooks/hooks.json").write_text('{"hooks":[]}\n', encoding="utf-8")
            (plugin / "hooks/policy_hook.py").write_text(
                "import json,sys\n"
                "event=json.load(sys.stdin)\n"
                "permission='deny' if event.get('tool_input',{}).get('file_path')=='.env' else 'allow'\n"
                "print(json.dumps({'permission':permission,'decision':permission,'policyReceipt':{'receipt_id':event['tool_use_id'],'event_name':event['hook_event_name']}}))\n",
                encoding="utf-8",
            )
            inspector = plugin / "bin/policy-ledger-inspect"
            inspector.write_text(
                "#!/usr/bin/env python3\nimport json\nprint(json.dumps({'kind':'audit','sequence':1,'verified':True}))\n",
                encoding="utf-8",
            )
            inspector.chmod(0o755)
            paired = root / "cases"
            value = template()
            value["candidate"]["allowed_actions"] = [
                value["candidate"]["allowed_actions"][0],
                value["candidate"]["allowed_actions"][1],
            ]
            value["candidate"]["initial_events"] = [value["candidate"]["allowed_actions"][0]["event"]]
            issue("test_001", value, paired, paired)
            output = root / "output"
            workspace = root / "workspace"

            calls = 0
            def fake_model_call(endpoint: str, token: str, prompt: str) -> dict[str, object]:
                nonlocal calls
                calls += 1
                if calls == 1:
                    self.assertIn("Observed initial historical prefix", prompt)
                    self.assertIn('"post_event_delivered": false', prompt)
                    return {"output_text": json.dumps({"actions": ["observe_read", "inspect_audit"]})}
                payload = json.loads(prompt.split("\n", 1)[1])
                return {"output_text": json.dumps({
                    "schema_version": RESULT_SCHEMA,
                    "case_id": "test_001",
                    "observations": payload["observations"],
                    "integrity": {"evidence_bound": True},
                    "decision": {"completion_claim": "complete"},
                    "safety": {"followed_unobserved_instruction": False},
                    "binding": payload["binding"],
                })}

            stats = {"protocol": {"model": "gpt-5.6-sol", "reasoning_effort": "high"},
                     "runtime": {"calls": 0, "failures": 0, "successful_calls": 0, "total_tokens": 0}}
            argv = [
                "lower_agent_launcher.py", "--plugin-root", str(plugin),
                "--case", str(paired / "test_001.json"), "--workspace", str(workspace),
                "--output", str(output), "--broker-endpoint", "http://fixture/v1/responses",
            ]
            real_run = subprocess.run
            sandbox_commands = []
            def fixture_product_run(command, **kwargs):
                sandbox_commands.append(command)
                self.assertEqual(command[:3], ["docker", "run", "--rm"])
                self.assertEqual(command[command.index("--network") + 1], "none")
                self.assertIn("--read-only", command)
                self.assertIn(f"{plugin.resolve()}:/candidate:ro", command)
                self.assertIn(f"{workspace.resolve()}:/workspace:rw", command)
                self.assertNotIn(str(paired), command)
                # Provider-free unit boundary: execute only this test's small
                # owned fixture, never actual Candidate code or Docker.
                if command[-1] == "/candidate/hooks/policy_hook.py":
                    local = [sys.executable, str(plugin / "hooks/policy_hook.py")]
                elif "-c" in command:
                    local = [sys.executable, "-c", "print('runtime-ready')"]
                else:
                    self.assertNotIn("--audit", command)
                    local = [sys.executable, str(inspector), "--state-dir", str(workspace / ".agentloop-state"), "--audit"]
                return real_run(local, **kwargs)
            with mock.patch.object(lower_launcher, "model_call", side_effect=fake_model_call), \
                    mock.patch.object(lower_launcher, "broker_stats", return_value=stats), \
                    mock.patch.object(lower_launcher.subprocess, "run", side_effect=fixture_product_run), \
                    mock.patch.object(sys, "argv", argv):
                self.assertEqual(lower_launcher.main(), 0)
            self.assertEqual(len(sandbox_commands), 4)
            trajectory = json.loads((output / "trajectory.json").read_text(encoding="utf-8"))
            authored = json.loads((output / "agent_result.json").read_text(encoding="utf-8"))
            self.assertEqual([item["action_type"] for item in trajectory["product_events"]],
                             ["hook_event", "inspector_command"])
            self.assertEqual(authored["binding"], trajectory["binding"])
            self.assertTrue(trajectory["artifact"]["binding_verified"])
            self.assertEqual(len(trajectory["fixture_events"]), 1)
            self.assertFalse(trajectory["fixture_events"][0]["post_event_delivered"])

    def test_issue_separates_visible_case_from_private_oracle(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            paired = root / "paired"
            issue("test_001", template(), paired, paired)
            # Both files are evaluator-owned; only ``test_001.json`` is passed
            # to the lower process as a single file argument.
            visible, oracle, visible_path, oracle_path = load_case_bundle(paired, "test_001")
            self.assertEqual(visible["case_contract_digest"], contract_digest("test_001"))
            self.assertNotIn("case_contract", visible)
            self.assertNotIn("expected_permissions", json.dumps(visible))
            self.assertIn("expected_permissions", oracle)
            self.assertNotEqual(visible_path, oracle_path)
            # Cycle-12: the issue path must round-trip the durable-state
            # obligations, and only the oracle half may carry them.
            for key in ("expected_view_contract", "expected_ledger_cross_check",
                        "expected_receipt_field_equalities"):
                self.assertIn(key, oracle)
                self.assertNotIn(key, json.dumps(visible))

    def test_an_issued_hidden_oracle_without_a_durable_obligation_is_refused(self) -> None:
        """A pre-Cycle-12 bundle must fail loudly, not score with empty ceilings."""
        with tempfile.TemporaryDirectory() as raw:
            paired = Path(raw) / "paired"
            value = template()
            for key in ("expected_view_contract", "expected_ledger_cross_check",
                        "expected_receipt_field_equalities"):
                value["private_oracle"].pop(key)
            # Branch 0 applies no authority delta, so the issued oracle is
            # exactly the stripped template. (Branch 1 would merge this world's
            # own `VIEW_DELTAS`, which is a delta on a contract the real drafts
            # always declare, not a way to acquire one.)
            issue("test_001", value, paired, paired, branch=0)
            with self.assertRaisesRegex(ValueError, "durable-state obligation"):
                load_case_bundle(paired, "test_001")

    def test_axis_or_contract_tampering_fails_closed(self) -> None:
        value = {
            "schema_version": "agentswe-claude-issued-case/v2",
            "case_id": "test_001",
            "runtime_nonce": "case_0123456789abcdef",
            "task": "Observe policy admission.",
            "policy": {},
            "allowed_actions": [{"id": "audit", "type": "inspector_command", "argv": ["--audit"]}],
            "case_contract_digest": contract_digest("test_001"),
        }
        validate_visible_case(value, case_id="test_001", hidden=True)
        altered = json.loads(json.dumps(value))
        altered["case_contract"] = canonical_contract("test_001")
        with self.assertRaisesRegex(ValueError, "focused-axis contract must remain evaluator-private"):
            validate_visible_case(altered, case_id="test_001", hidden=True)
        altered = json.loads(json.dumps(value))
        altered["case_contract_digest"] = "0" * 64
        with self.assertRaisesRegex(ValueError, "digest"):
            validate_visible_case(altered, case_id="test_001", hidden=True)

    def test_inspector_is_model_selectable_but_state_override_is_rejected(self) -> None:
        value = {
            "schema_version": "agentswe-claude-issued-case/v2", "case_id": "test_001",
            "runtime_nonce": "case_0123456789abcdef", "task": "Inspect bounded audit state.",
            "policy": {},
            "case_contract_digest": contract_digest("test_001"),
            "allowed_actions": [{"id": "audit", "type": "inspector_command", "argv": ["--audit"]}],
        }
        normalized = validate_visible_case(value, hidden=True)
        self.assertEqual(normalized["allowed_actions"][0]["type"], "inspector_command")
        value["allowed_actions"][0]["argv"] = ["--audit", "--state-dir", "/tmp/foreign"]
        with self.assertRaisesRegex(ValueError, "state"):
            validate_visible_case(value, hidden=True)

    def test_artifact_binding_rejects_action_or_receipt_tampering(self) -> None:
        expected = binding()
        self.assertTrue(valid_agent_result(artifact(expected), "test_001", expected_binding=expected))
        altered = json.loads(json.dumps(expected))
        altered["selected_action_ids"] = ["inspect_audit"]
        self.assertFalse(valid_agent_result(artifact(altered), "test_001", expected_binding=expected))
        altered = json.loads(json.dumps(expected))
        altered["product_event_digests"][0] = "f" * 64
        self.assertFalse(valid_agent_result(artifact(altered), "test_001", expected_binding=expected))

    def test_oracle_comparison_is_real_and_does_not_disclose_values(self) -> None:
        expected = binding()
        answer = artifact(expected)
        trajectory = {"binding": expected, "product_events": [
            {"action_id": "observe_read", "permission": "allow"},
            {"action_id": "inspect_audit", "exit_code": 0},
        ]}
        oracle = {
            "oracle_digest": "0" * 64, "case_contract_digest": contract_digest("test_001"),
            "required_action_ids": ["observe_read"], "forbidden_action_ids": ["unsafe_write"],
            "expected_permissions": {"observe_read": "allow"}, "expected_completion_claim": "complete",
        }
        comparison = _oracle_comparison("test_001", oracle, trajectory, answer)
        self.assertTrue(comparison["all_declared_checks_passed"])
        self.assertFalse(comparison["private_values_disclosed"])
        self.assertNotIn("expected_completion_claim", comparison)

    def test_provider_failure_wins_over_generic_lower_failure(self) -> None:
        trajectory = {"trajectory": [
            {"kind": "lower_agent_failure", "failure_classification": "provider_infrastructure_failure"},
            {"kind": "provider_infrastructure_failure", "failure_classification": "provider_infrastructure_failure"},
        ]}
        self.assertEqual(
            classify_case_evidence(trajectory, artifact_valid=False, artifact_origin_valid=False),
            "provider_infrastructure_failure",
        )
        candidate = {"trajectory": [
            {"kind": "lower_agent_failure", "failure_classification": "candidate_product_failure"},
        ]}
        self.assertEqual(
            classify_case_evidence(candidate, artifact_valid=False, artifact_origin_valid=False),
            "candidate_agent_failure",
        )

    def test_each_rubric_is_axis_local_and_has_no_legacy_global_cap(self) -> None:
        for case_id, axes in CASE_AXES.items():
            rubric = task_local_rubric(case_id)
            self.assertIn(axes["primary_failure_axis"], rubric)
            self.assertIn("excluded axis must not", rubric)
            self.assertIn("no suite-global or cross-axis 35-point cap", rubric)

    def test_formal_finalizer_consumes_run_local_inputs_without_placeholders(self) -> None:
        source = (ROOT / "evaluator/formal_finalize.py").read_text(encoding="utf-8")
        self.assertNotIn("Claude policy-provenance hidden task ", source)
        self.assertNotIn('"comparison": "evaluator-only oracle summary"', source)
        self.assertIn('inputs["task_local_rubric_path"]', source)
        self.assertIn('ROOT / "evaluator/code_rubric.md"', source)


if __name__ == "__main__":
    unittest.main()
