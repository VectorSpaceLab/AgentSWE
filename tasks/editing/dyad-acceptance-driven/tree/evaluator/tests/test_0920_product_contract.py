#!/usr/bin/env python3
"""Provider-free tests for the 0920 cross-cutting Acceptance product contract.

Every obligation checked here is published in
``input/02_interface_and_delivery.md`` ("independently verifiable product contract") and
``input/03_requirements_and_constraints.md`` items 23-26, is decided from
product returns alone, and costs the lower agent no extra turn.  The reference
rollout below is the documented conforming behaviour: it satisfies all seven,
and each single documented defect flips exactly one of them and lowers the
published ceiling to 28.
"""
from __future__ import annotations

import hashlib
import importlib.util
import json
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))


def load_module(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


scenario = load_module("dyad_contract_scenario", ROOT / "evaluator/scenario_contract.py")
caps = load_module("dyad_contract_caps", ROOT / "evaluator/result_score_caps.py")
rubric = load_module("dyad_contract_rubric", ROOT / "evaluator/result_rubric.py")
semantic = load_module("dyad_contract_semantic", ROOT / "evaluator/semantic_execution.py")

TARGET = {"testFile": "e2e-tests/agentswe-test_001-abc.spec.ts",
          "grep": "test_001 actual registration behavior"}
ADMITTED = "b" * 64
REVISION = "a" * 64


def terminal_session(**overrides: object) -> dict[str, object]:
    value: dict[str, object] = {
        "sessionId": "session-1", "runId": "run-1", "status": "passed",
        "target": dict(TARGET), "testFingerprint": ADMITTED,
        "startedRevision": REVISION, "currentRevision": REVISION,
        "lastEventSequence": 3, "result": {"passed": 1, "failed": 0, "skipped": 0},
    }
    value.update(overrides)
    return value


def record(sequence: int, action: str, result: object,
           command: object = None) -> dict[str, object]:
    request: dict[str, object] = {"action": action}
    if command is not None:
        request["command"] = command
    return {"sequence": sequence, "action": action, "request": request,
            "dispatch_status": "completed", "requested_by_model": True,
            "executor": "generic_model_action_dispatch", "evaluator_defaulted": False,
            "source_message_id": sequence, "source_message_sha256": f"{sequence:064x}",
            "result": result}


def reference_rollout(outcome: str = "passed") -> tuple[dict[str, object], list[dict[str, object]]]:
    """The conforming rollout: seven obligations, all met."""
    session = terminal_session(status=outcome)
    attestation = {
        "attestationId": "attestation-1", "sessionId": "session-1",
        "outcome": outcome, "testFingerprint": ADMITTED,
        "startedRevision": REVISION, "finishedRevision": REVISION,
        "resultDigest": scenario.canonical_result_digest(session),
    }
    actions = [
        record(1, "inspect_latest",
               {"ok": True, "value": {"runId": "run-1", "currentTarget": dict(TARGET)}}),
        record(2, "start_preview",
               {"ok": True, "value": terminal_session(status="queued", lastEventSequence=0)},
               command={"runId": "run-1", "target": dict(TARGET)}),
        record(3, "get_preview", {"ok": True, "value": session}),
        record(4, "get_attestation", {"ok": True, "value": attestation}),
        record(5, "finish", {"captured": True, "artifactSha256": "f" * 64}),
    ]
    evidence = {
        "target_bytes_sha256_at_admission": ADMITTED,
        "read_stability_probes": [
            {"kind": "terminal_preview", "sequence": 3, "status": outcome, "stable": True},
            {"kind": "attestation", "sequence": 4, "status": outcome, "stable": True},
        ],
        "acceptance_gate_probes": [
            {"sequence": 4, "attestation_outcome": outcome,
             "attestation_identity_present": True, "run_snapshot_readable": True,
             "run_status": "passed" if outcome == "passed" else "unresolved",
             "passing_evidence_cites_attestation": outcome == "passed"},
        ],
        "product_action_trajectory": actions,
    }
    return evidence, actions


ORACLE = {"case_id": "test_001", "target": dict(TARGET),
          "required_contract_checks": list(scenario.UNIVERSAL_CONTRACT_CHECKS)
          + [scenario.GATE_CONTRACT_CHECK]}


class ProductContractTests(unittest.TestCase):
    def test_reference_rollout_meets_every_published_obligation(self) -> None:
        evidence, actions = reference_rollout()
        results = scenario.contract_check_results(ORACLE, evidence, actions)
        self.assertEqual(sorted(results), sorted(
            list(scenario.UNIVERSAL_CONTRACT_CHECKS) + [scenario.GATE_CONTRACT_CHECK]))
        for name, value in results.items():
            self.assertTrue(value, name)

    def test_result_digest_follows_the_published_formula(self) -> None:
        session = terminal_session()
        document = {"result": session["result"], "status": session["status"],
                    "target": session["target"]}
        text = json.dumps(document, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
        self.assertEqual(scenario.canonical_result_digest(session),
                         hashlib.sha256(text.encode("utf-8")).hexdigest())
        # The published text carries no insignificant whitespace and is
        # key-sorted (the grep value's own spaces are part of the data).
        self.assertNotIn('", "', text)
        self.assertNotIn('": ', text)
        self.assertNotIn("{ ", text)
        self.assertLess(text.index('"result"'), text.index('"status"'))
        self.assertLess(text.index('"status"'), text.index('"target"'))
        self.assertLess(text.index('"grep"'), text.index('"testFile"'))

    def test_each_documented_defect_flips_exactly_its_own_check(self) -> None:
        defects = {
            # The run publishes no usable current target: the caller must guess.
            "current_target_binds_started_target": lambda e, a: a[0]["result"]["value"].update(
                {"currentTarget": {"testFile": TARGET["testFile"], "grep": None}}),
            # The session normalises the accepted grep away.
            "session_target_preserved": lambda e, a: a[1]["result"]["value"].update(
                {"target": {"testFile": TARGET["testFile"], "grep": None}}),
            # A constant stands in for the admitted target bytes.
            "fingerprint_is_admitted_target_bytes": lambda e, a: a[2]["result"]["value"].update(
                {"testFingerprint": "c" * 64}),
            # The event sequence goes backwards between two reads.
            "event_sequence_never_decreases": lambda e, a: a[1]["result"]["value"].update(
                {"lastEventSequence": 9}),
            # A repeat read of the immutable attestation returned other bytes.
            "terminal_and_attestation_reads_byte_stable": lambda e, a: e[
                "read_stability_probes"][1].update({"stable": False}),
            # resultDigest is the product's own invention.
            "result_digest_matches_terminal_facts": lambda e, a: a[3]["result"]["value"].update(
                {"resultDigest": "d" * 64}),
        }
        for name, break_it in defects.items():
            with self.subTest(defect=name):
                evidence, actions = reference_rollout()
                break_it(evidence, actions)
                results = scenario.contract_check_results(ORACLE, evidence, actions)
                self.assertFalse(results[name], name)
                for other, value in results.items():
                    if other != name:
                        self.assertTrue(value, f"{name} also broke {other}")

    def test_gate_fails_closed_only_for_a_non_passing_attestation(self) -> None:
        # A cancelled attestation that left the run at `passed` is the exact
        # anti-pattern input/03 item 26 forbids.
        evidence, actions = reference_rollout(outcome="cancelled")
        self.assertTrue(scenario.contract_check_results(
            ORACLE, evidence, actions)[scenario.GATE_CONTRACT_CHECK])
        evidence["acceptance_gate_probes"][0]["run_status"] = "passed"
        self.assertFalse(scenario.contract_check_results(
            ORACLE, evidence, actions)[scenario.GATE_CONTRACT_CHECK])
        evidence["acceptance_gate_probes"][0]["run_status"] = "unresolved"
        evidence["acceptance_gate_probes"][0]["passing_evidence_cites_attestation"] = True
        self.assertFalse(scenario.contract_check_results(
            ORACLE, evidence, actions)[scenario.GATE_CONTRACT_CHECK])

    def test_an_unobservable_obligation_is_unmet_not_absent(self) -> None:
        results = scenario.contract_check_results(ORACLE, {}, [])
        self.assertTrue(results)
        for name, value in results.items():
            self.assertFalse(value, name)

    def _comparison(self, directory: Path, checks: dict[str, object]) -> Path:
        path = directory / "comparison.json"
        scenario.write_json(path, {"case_id": "test_001", "checks": checks,
                                   "required_action_observations": []})
        return path

    def test_ceiling_is_28_when_any_contract_obligation_is_unmet(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            directory = Path(raw)
            met = {f"contract:{name}": True for name in ORACLE["required_contract_checks"]}
            entries = caps.cap_entries(self._comparison(directory, met), None, None)
            entry = next(item for item in entries if item["cap_id"] == "product_contract_unmet")
            self.assertEqual(entry["status"], "not_violated")
            self.assertEqual(entry["maximum_score"], 28)

            unmet = dict(met)
            unmet["contract:result_digest_matches_terminal_facts"] = False
            entries = caps.cap_entries(self._comparison(directory, unmet), None, None)
            entry = next(item for item in entries if item["cap_id"] == "product_contract_unmet")
            self.assertEqual(entry["status"], "violated")
            self.assertIn("result_digest_matches_terminal_facts", entry["reason"])
            self.assertEqual(caps.effective_ceiling(entries), 28)

    def test_ceiling_is_unavailable_when_no_contract_check_was_recorded(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            entries = caps.cap_entries(
                self._comparison(Path(raw), {"required_action_order": True}), None, None)
            entry = next(item for item in entries if item["cap_id"] == "product_contract_unmet")
            self.assertEqual(entry["status"], "unavailable")

    def test_every_hidden_and_public_case_carries_the_universal_contract(self) -> None:
        for case_id, case in scenario.SCENARIOS.items():
            with self.subTest(case=case_id):
                for name in scenario.UNIVERSAL_CONTRACT_CHECKS:
                    self.assertIn(name, case.contract_checks)
        for case_id in ("test_004", "test_005", "test_006"):
            self.assertIn(scenario.GATE_CONTRACT_CHECK,
                          scenario.SCENARIOS[case_id].contract_checks)
        for case_id in ("test_001", "test_002", "test_003", "dev_001", "dev_002"):
            self.assertNotIn(scenario.GATE_CONTRACT_CHECK,
                             scenario.SCENARIOS[case_id].contract_checks)

    def test_rubric_dimensions_are_consistent_and_document_every_check(self) -> None:
        dimensions = json.loads((ROOT / "evaluator/result_dimensions.json").read_text())
        self.assertEqual(sum(dimensions.values()), 100)
        self.assertEqual(dimensions, rubric.WEIGHTS)
        text = (ROOT / "evaluator/result_rubric.md").read_text(encoding="utf-8")
        for name in dimensions:
            self.assertIn(f"`{name}`", text)
            self.assertIn(f"| `{name}` | {dimensions[name]} |", text)
        for name in list(scenario.UNIVERSAL_CONTRACT_CHECKS) + [scenario.GATE_CONTRACT_CHECK]:
            self.assertIn(name, text)
        self.assertIn("| any `contract:*` cross-cutting Acceptance obligation false | **28** |", text)

    def test_mechanical_contract_dimension_follows_the_published_bands(self) -> None:
        names = list(scenario.UNIVERSAL_CONTRACT_CHECKS)
        self.assertEqual(rubric.score_contract({name: True for name in names}), 20)
        one_ordinary = {name: True for name in names}
        one_ordinary["event_sequence_never_decreases"] = False
        self.assertEqual(rubric.score_contract(one_ordinary), 13)
        one_severe = {name: True for name in names}
        one_severe["fingerprint_is_admitted_target_bytes"] = False
        self.assertEqual(rubric.score_contract(one_severe), 6)
        self.assertEqual(rubric.score_contract({name: False for name in names}), 0)
        self.assertEqual(rubric.score_contract({}), 0)

    def test_probe_fields_are_never_projected_away_from_the_judge(self) -> None:
        for name in ("read_stability_probes", "acceptance_gate_probes",
                     "target_bytes_sha256_at_admission"):
            self.assertIn(name, semantic.JUDGE_NATIVE_PRESERVED_FIELDS)

    def test_trajectory_gets_the_same_bounded_view_as_the_native_evidence(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            directory = Path(raw)
            small = directory / "small.json"
            small.write_text(json.dumps({"schema_version": "v1", "case_id": "test_001",
                                         "events": [{"text": "short"}]}), encoding="utf-8")
            self.assertEqual(
                semantic.judge_trajectory_projection(small, directory / "small.projection.json"),
                small)

            big = directory / "big.json"
            big.write_text(json.dumps({
                "schema_version": "v1", "case_id": "test_001",
                "model_selected_actions_only": True,
                "events": [{"text": "x" * 20_000} for _ in range(200)],
            }), encoding="utf-8")
            self.assertGreater(big.stat().st_size, semantic.JUDGE_NATIVE_EVIDENCE_MAX_BYTES)
            projected = semantic.judge_trajectory_projection(
                big, directory / "big.projection.json")
            self.assertNotEqual(projected, big)
            self.assertLessEqual(projected.stat().st_size,
                                 semantic.JUDGE_NATIVE_EVIDENCE_MAX_BYTES)
            document = json.loads(projected.read_text(encoding="utf-8"))
            self.assertEqual(document["case_id"], "test_001")
            self.assertTrue(document["model_selected_actions_only"])
            provenance = document["evaluator_judge_projection"]
            self.assertFalse(provenance["candidate_authored"])
            self.assertEqual(provenance["source_trajectory_path"], str(big))
            self.assertEqual(provenance["source_trajectory_sha256"],
                             hashlib.sha256(big.read_bytes()).hexdigest())
            self.assertEqual(provenance["source_bytes"], big.stat().st_size)


if __name__ == "__main__":
    unittest.main(verbosity=1)
