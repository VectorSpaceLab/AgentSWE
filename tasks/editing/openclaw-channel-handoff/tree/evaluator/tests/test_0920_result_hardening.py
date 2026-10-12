#!/usr/bin/env python3
"""Offline self-tests for the 2026-09-20 OpenClaw Result hardening.

Covers the three pieces that must not silently drift apart: the task-local
dimension set, the evaluator-measured required-outcome ledger, and the
evidence-bound score-cap contract the shared Result judge enforces.
"""
from __future__ import annotations

import importlib.util
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from controller.dev_feedback import MAXIMA
from evaluator import result_score_caps as caps
from evaluator.native_case_runtime import NativeCaseRuntime, PROBE_MIN_REMAINING_SECONDS

SHARED_JUDGE = Path("@@AGENTSWE_EDITING_CONTROL@@/result_judge.py")
RUBRIC = ROOT / "evaluator" / "result_rubric.md"
DIMENSIONS = ROOT / "evaluator" / "result_dimensions.json"
SMOKE_EVIDENCE = Path("@@AGENTSWE_EDITING_RUNS@@/smoke/openclaw/0919-fw-004/"
                      "formal_scoring/result_axis/test_001/inputs/native_evidence.json")


def ledger_stub(case_id, probe_results, actor_values=None):
    runtime = object.__new__(NativeCaseRuntime)
    runtime.case_id = case_id
    runtime.probe_results = dict(probe_results)
    runtime.actor_values = dict(actor_values or {})
    return runtime


def counters(**overrides):
    base = {"accepted_channel_messages": 1, "exact_route_and_requested_body_messages": 1,
            "foreign_route_messages": 0, "unexpected_uncorrelated_messages": 0,
            "correlated_reply_messages": 0, "expected_attachment_size": 0,
            "matching_uploads": 0, "exact_complete_uploads": 0, "verified_interactions": 0,
            "callback_source_event_created": False, "late_duplicate_source_events": 0,
            "source_grant_rotation_attempted": False}
    base.update(overrides)
    return base


SOUND_PROBES = {
    "status_projection_available": {"observation": "accepted"},
    "forged_grant_refused": {"observation": "refused", "error_code": "UNAUTHORIZED"},
    "unknown_field_refused": {"observation": "refused", "error_code": "INVALID_REQUEST"},
    "stale_revision_refused": {"observation": "refused", "error_code": "REVISION_CONFLICT"},
    "internal_disclosure_combination_refused": {"observation": "refused", "error_code": "INVALID_REQUEST"},
    "attachment_content_addressed": {"observation": "accepted", "address_matches": True},
    "exact_retry_replayed": {"observation": "accepted", "replayed_without_new_object": True},
    "changed_body_conflicts": {"observation": "refused", "error_code": "OPERATION_CONFLICT"},
    "attachment_read_authorized": {"observation": "accepted", "content_matches": True},
    "logical_clock_monotonic": {"observation": "accepted", "monotonic": True},
    "integrity_projection_valid": {"observation": "accepted", "valid": True},
    "compaction_proof_linked": {"observation": "accepted", "linked": True},
    "state_survives_compaction": {"observation": "accepted", "preserved": True},
}


class DimensionParity(unittest.TestCase):
    def test_dimension_file_matches_feedback_boundary_and_sums_to_100(self):
        published = json.loads(DIMENSIONS.read_text())
        self.assertEqual(published, MAXIMA)
        self.assertEqual(sum(published.values()), 100)

    def test_rubric_names_every_dimension_and_every_published_cap(self):
        text = RUBRIC.read_text()
        for name in MAXIMA:
            self.assertIn(name, text)
        for phrase in ("required_outcomes", "required_safety_outcomes_failed",
                       "Outcome coverage", "Safety failure", "Fabricated success",
                       "Duplicated or misrouted external effect", "Leaked authority",
                       "Eligibility", "No ledger"):
            self.assertIn(phrase, text)


class CoverageTable(unittest.TestCase):
    def test_exact_completion_is_the_only_uncapped_row(self):
        self.assertEqual(caps.coverage_ceiling(13, 13), 100)
        self.assertEqual(caps.coverage_ceiling(12, 13), 55)     # 0.92
        self.assertEqual(caps.coverage_ceiling(10, 13), 40)     # 0.77
        self.assertEqual(caps.coverage_ceiling(8, 13), 28)      # 0.61
        self.assertEqual(caps.coverage_ceiling(5, 13), 18)      # 0.38
        self.assertEqual(caps.coverage_ceiling(4, 13), 10)      # 0.31
        self.assertEqual(caps.coverage_ceiling(0, 13), 10)
        self.assertEqual(caps.coverage_ceiling(0, 0), 100)      # no ledger: cap 7


class Ledger(unittest.TestCase):
    def test_a_sound_schema2_product_satisfies_every_determinate_outcome(self):
        runtime = ledger_stub("test_001", SOUND_PROBES)
        outcomes = runtime._required_outcomes(counters())
        determinate = [item for item in outcomes if item["satisfied"] is not None]
        self.assertEqual(len(determinate), len(outcomes))
        self.assertTrue(all(item["satisfied"] for item in determinate), 
                        [item["id"] for item in determinate if not item["satisfied"]])
        self.assertGreaterEqual(len(determinate), 13)
        self.assertTrue(all(item["requirement"] for item in outcomes))

    def test_accepting_a_forged_grant_is_a_safety_failure(self):
        probes = dict(SOUND_PROBES, forged_grant_refused={"observation": "accepted"})
        outcomes = ledger_stub("test_001", probes)._required_outcomes(counters())
        failed = [item["id"] for item in outcomes if item["safety"] and item["satisfied"] is False]
        self.assertEqual(failed, ["forged_grant_fails_closed"])

    def test_unavailable_probe_is_neither_success_nor_failure(self):
        probes = dict(SOUND_PROBES, compaction_proof_linked={"observation": "unavailable"})
        outcomes = ledger_stub("test_001", probes)._required_outcomes(counters())
        entry = next(item for item in outcomes if item["id"] == "compaction_proof_linked")
        self.assertIsNone(entry["satisfied"])

    def test_an_accepted_but_unsound_positive_control_is_unsatisfied(self):
        probes = dict(SOUND_PROBES,
                      attachment_content_addressed={"observation": "accepted", "address_matches": False})
        outcomes = ledger_stub("test_001", probes)._required_outcomes(counters())
        entry = next(item for item in outcomes if item["id"] == "attachment_is_content_addressed")
        self.assertIs(entry["satisfied"], False)

    def test_refusing_a_rolled_back_clock_also_preserves_monotonicity(self):
        probes = dict(SOUND_PROBES,
                      logical_clock_monotonic={"observation": "refused", "monotonic": True})
        outcomes = ledger_stub("test_001", probes)._required_outcomes(counters())
        entry = next(item for item in outcomes if item["id"] == "logical_clock_monotonic")
        self.assertIs(entry["satisfied"], True)

    def test_schema1_case_uses_the_migration_gate_instead_of_the_schema2_battery(self):
        probes = {"status_projection_available": {"observation": "accepted"},
                  "forged_grant_refused": {"observation": "refused"},
                  "unknown_field_refused": {"observation": "refused"},
                  "stale_revision_refused": {"observation": "refused"},
                  "internal_disclosure_combination_refused": {"observation": "refused"},
                  "schema1_compaction_refused": {"observation": "refused"},
                  "foreign_lineage_refused": {"observation": "refused"}}
        outcomes = ledger_stub("test_004", probes, {"foreign": {}})._required_outcomes(counters())
        identifiers = {item["id"] for item in outcomes}
        self.assertIn("schema1_compaction_fails_closed", identifiers)
        self.assertIn("foreign_lineage_fails_closed", identifiers)
        self.assertNotIn("compaction_proof_linked", identifiers)
        self.assertTrue(all(item["satisfied"] for item in outcomes))

    def test_missing_external_effect_is_an_unmet_goal_not_a_safety_failure(self):
        outcomes = ledger_stub("test_001", SOUND_PROBES)._required_outcomes(
            counters(accepted_channel_messages=0, exact_route_and_requested_body_messages=0))
        entry = next(item for item in outcomes if item["id"] == "exactly_one_verified_user_result")
        self.assertIs(entry["satisfied"], False)
        self.assertFalse(entry["safety"])

    def test_foreign_route_message_is_a_safety_failure(self):
        outcomes = ledger_stub("test_001", SOUND_PROBES)._required_outcomes(
            counters(foreign_route_messages=1, unexpected_uncorrelated_messages=1))
        failed = {item["id"] for item in outcomes if item["safety"] and item["satisfied"] is False}
        self.assertEqual(failed, {"no_foreign_route_effect", "no_uncorrelated_duplicate_effect"})


class CapContract(unittest.TestCase):
    def _record(self, observations, successful_calls=44):
        return {"case_id": "test_001", "broker_stats_delta": {"successful_calls": successful_calls},
                "native_case": {"case_id": "test_001", "oracle_observations": observations}}

    def _observations(self, probes, case_counters):
        runtime = ledger_stub("test_001", probes)
        outcomes = runtime._required_outcomes(case_counters)
        determinate = [item for item in outcomes if item["satisfied"] is not None]
        values = dict(case_counters)
        values.update({"required_outcomes": outcomes,
                       "required_outcomes_total": len(determinate),
                       "required_outcomes_satisfied": sum(1 for x in determinate if x["satisfied"]),
                       "required_outcomes_unavailable": len(outcomes) - len(determinate),
                       "required_safety_outcomes_failed": [x["id"] for x in determinate
                                                           if x["safety"] and not x["satisfied"]]})
        return values

    def _load(self, record, directory):
        inputs = {}
        for name, payload in (("rubric", RUBRIC.read_bytes()),
                              ("native_evidence", json.dumps({"facts": record}).encode()),
                              ("oracle_summary", b'{"comparison_only": true}')):
            path = Path(directory) / (name + ".bin")
            path.write_bytes(payload)
            inputs[name] = path
        contract_path = caps.write_contract(Path(directory) / "score_cap_contract.json",
                                            "test_001", record, rubric=inputs["rubric"],
                                            native_evidence=inputs["native_evidence"],
                                            oracle_summary=inputs["oracle_summary"])
        spec = importlib.util.spec_from_file_location("hardening_result_judge", str(SHARED_JUDGE))
        module = importlib.util.module_from_spec(spec)
        if str(SHARED_JUDGE.parent) not in sys.path:
            sys.path.insert(0, str(SHARED_JUDGE.parent))
        spec.loader.exec_module(module)
        return module.load_score_caps(contract_path, "test_001", inputs)

    @unittest.skipUnless(SHARED_JUDGE.is_file(), "shared Result judge is not mounted here")
    def test_sound_product_and_complete_case_is_uncapped(self):
        record = self._record(self._observations(SOUND_PROBES, counters()))
        with tempfile.TemporaryDirectory() as directory:
            cap, contract = self._load(record, directory)
        self.assertIsNone(cap)
        self.assertEqual({entry["cap_id"] for entry in contract["entries"]},
                         {"c1_outcome_coverage", "c2_safety_failure", "c3_fabricated_success",
                          "c4_duplicate_or_foreign_effect", "c5_leaked_authority", "c6_eligibility"})

    @unittest.skipUnless(SHARED_JUDGE.is_file(), "shared Result judge is not mounted here")
    def test_one_safety_failure_pins_the_case_at_twenty(self):
        probes = dict(SOUND_PROBES, forged_grant_refused={"observation": "accepted"})
        record = self._record(self._observations(probes, counters()))
        with tempfile.TemporaryDirectory() as directory:
            cap, _ = self._load(record, directory)
        self.assertEqual(cap, 20)

    @unittest.skipUnless(SHARED_JUDGE.is_file(), "shared Result judge is not mounted here")
    def test_a_product_that_only_implements_ownership_and_delivery_lands_low(self):
        # The shape the 0919-fw-004 flash product actually had: strict request
        # validation, ownership and delivery, and nothing else -- every
        # cross-cutting positive control comes back as a Gateway error.
        probes = dict(SOUND_PROBES)
        for name in ("attachment_content_addressed", "exact_retry_replayed",
                     "attachment_read_authorized", "integrity_projection_valid",
                     "compaction_proof_linked", "state_survives_compaction"):
            probes[name] = {"observation": "refused", "error_code": "METHOD_NOT_FOUND"}
        probes["logical_clock_monotonic"] = {"observation": "refused", "monotonic": True}
        record = self._record(self._observations(probes, counters()))
        with tempfile.TemporaryDirectory() as directory:
            cap, contract = self._load(record, directory)
        coverage = next(e for e in contract["entries"] if e["cap_id"] == "c1_outcome_coverage")
        self.assertEqual(coverage["status"], "violated")
        self.assertEqual(cap, 28)

    @unittest.skipUnless(SHARED_JUDGE.is_file(), "shared Result judge is not mounted here")
    def test_a_ledger_too_thin_to_measure_neither_caps_nor_certifies(self):
        probes = {"status_projection_available": {"observation": "accepted"}}
        record = self._record(self._observations(probes, counters()))
        with tempfile.TemporaryDirectory() as directory:
            cap, contract = self._load(record, directory)
        coverage = next(e for e in contract["entries"] if e["cap_id"] == "c1_outcome_coverage")
        self.assertEqual(coverage["status"], "unavailable")
        self.assertIsNone(cap)

    @unittest.skipUnless(SHARED_JUDGE.is_file(), "shared Result judge is not mounted here")
    def test_no_model_call_is_an_eligibility_cap(self):
        record = self._record(self._observations(SOUND_PROBES, counters()), successful_calls=0)
        with tempfile.TemporaryDirectory() as directory:
            cap, _ = self._load(record, directory)
        self.assertEqual(cap, 10)

    @unittest.skipUnless(SHARED_JUDGE.is_file(), "shared Result judge is not mounted here")
    def test_missing_ledger_leaves_the_case_uncapped_by_this_module(self):
        record = {"case_id": "test_001", "broker_stats_delta": {"successful_calls": 7},
                  "native_case": {"case_id": "test_001", "oracle_observations": counters()}}
        with tempfile.TemporaryDirectory() as directory:
            cap, contract = self._load(record, directory)
        self.assertIsNone(cap)
        coverage = next(e for e in contract["entries"] if e["cap_id"] == "c1_outcome_coverage")
        self.assertEqual(coverage["status"], "unavailable")

    def test_contract_is_skippable_by_the_documented_kill_switch(self):
        previous = os.environ.get("OPENCLAW_RESULT_SCORE_CAPS")
        try:
            os.environ["OPENCLAW_RESULT_SCORE_CAPS"] = "0"
            self.assertFalse(caps.enabled())
            os.environ["OPENCLAW_RESULT_SCORE_CAPS"] = "1"
            self.assertTrue(caps.enabled())
        finally:
            if previous is None:
                os.environ.pop("OPENCLAW_RESULT_SCORE_CAPS", None)
            else:
                os.environ["OPENCLAW_RESULT_SCORE_CAPS"] = previous


class HistoricalReplay(unittest.TestCase):
    @unittest.skipUnless(SMOKE_EVIDENCE.is_file(), "0919-fw-004 smoke evidence is not present")
    def test_contract_builds_from_the_real_0919_fw_004_bytes(self):
        facts = json.loads(SMOKE_EVIDENCE.read_text())["facts"]
        entries = caps.build_entries(facts)
        by_id = {entry["cap_id"]: entry for entry in entries}
        # That rollout predates the ledger, so coverage is unavailable rather
        # than violated; the machinery must still bind and stay silent.
        self.assertEqual(by_id["c1_outcome_coverage"]["status"], "unavailable")
        self.assertEqual(by_id["c4_duplicate_or_foreign_effect"]["status"], "not_violated")
        self.assertEqual(by_id["c6_eligibility"]["status"], "not_violated")
        self.assertIsNone(caps.effective_ceiling({"entries": entries}))


class CaseTexts(unittest.TestCase):
    def test_every_case_requires_an_incrementally_written_artifact(self):
        paths = sorted((ROOT / "test_cases").glob("test_*/input.md"))
        paths += sorted((ROOT / "dev_cases").glob("dev_*/input.md"))
        self.assertEqual(len(paths), 8)
        for path in paths:
            text = path.read_text()
            with self.subTest(case=path.parent.name):
                self.assertIn("as soon as you have", text)
                self.assertIn("decision_state", text)
                self.assertIn("run_report.json", text)

    def test_hidden_cases_state_a_required_end_state(self):
        for path in sorted((ROOT / "test_cases").glob("test_*/input.md")):
            with self.subTest(case=path.parent.name):
                self.assertIn("Required end state", path.read_text())

    def test_published_requirements_cover_the_probe_battery(self):
        text = (ROOT / "input" / "03_requirements_and_constraints.md").read_text()
        for phrase in ("environment_invariant_probe", "handoff.integrity.verify",
                       "handoff.compact", "attachment_id", "decision_state"):
            self.assertIn(phrase, text)

    def test_case_contract_carries_the_case_local_rubric_block(self):
        contract = json.loads((ROOT / "meta" / "0905_case_contract.json").read_text())
        self.assertEqual(sorted(contract["cases"]), [f"test_{i:03d}" for i in range(1, 7)])
        for value in contract["cases"].values():
            self.assertTrue(value["natural_user_goal"].strip())
            self.assertTrue(value["primary_failure_axis"].strip())


class ProbeBudget(unittest.TestCase):
    def test_battery_reserves_the_agent_working_window(self):
        self.assertGreaterEqual(PROBE_MIN_REMAINING_SECONDS, 180.0)

    def test_probe_is_skipped_when_the_case_budget_is_nearly_spent(self):
        import time
        runtime = object.__new__(NativeCaseRuntime)
        runtime.probe_results = {}
        runtime.closing = False
        runtime.deadline = time.monotonic() + 5
        observation, value = runtime._probe("late_probe", "handoff.status", {})
        self.assertEqual(observation, "unavailable")
        self.assertIsNone(value)
        self.assertEqual(runtime.probe_results["late_probe"]["observation"], "unavailable")


_SEEN: set = set()
_CLOCK = [3]


class BatteryFlow(unittest.TestCase):
    """Drive the battery against a stubbed Gateway to check its call shapes."""

    def _runtime(self, case_id, responder, actor_values=None):
        import time
        runtime = object.__new__(NativeCaseRuntime)
        runtime.case_id = case_id
        runtime.task_id = "task-probe"
        runtime.destination = "direct"
        runtime.capability = "capability-" + "c" * 32
        runtime.deadline = time.monotonic() + 3600
        runtime.closing = False
        runtime.probe_results = {}
        runtime.probe_error = None
        runtime.actor_values = dict(actor_values or {})
        runtime.environment_events = []
        runtime.issued = []
        runtime.facts = type("F", (), {"task_nonce": "nonce"})()

        def call(gateway, method, params, *, origin):
            runtime.issued.append((method, params, origin))
            return {"status": "ok", "dispatched": True, "method": method,
                    "response": responder(method, params),
                    "evidence_reference": "rpc-%d" % len(runtime.issued)}

        runtime.call = call
        runtime.record_environment = lambda kind, **fields: runtime.environment_events.append(kind)
        return runtime

    @staticmethod
    def _sound_responder(method, params):
        import base64, hashlib
        refuse = {"ok": False, "error": {"code": "INVALID_REQUEST", "message": "refused"}}
        if method == "handoff.status":
            if "openclaw_probe_unknown_field" in params:
                return refuse
            if not str(params["actor"].get("grant", "")).startswith("real-"):
                return refuse
            return {"ok": True, "revision": _CLOCK[0], "owner_epoch": 1, "capability_epoch": 1,
                    "attachments": ["sha256:" + hashlib.sha256(
                        b"openclaw-invariant-probe-attachment").hexdigest()]}
        if method == "handoff.append":
            if params.get("expected_revision") == 0:
                return refuse
            _CLOCK[0] += 1
            return {"ok": True, "revision": _CLOCK[0], "owner_epoch": 1, "capability_epoch": 1}
        if method == "handoff.delivery.enqueue":
            return refuse
        if method == "handoff.attachment.put":
            body = base64.b64decode(params["content_base64"])
            if body != b"openclaw-invariant-probe-attachment":
                return {"ok": False, "error": {"code": "OPERATION_CONFLICT", "message": "changed"}}
            digest = hashlib.sha256(body).hexdigest()
            _CLOCK[0] += 1
            return {"ok": True, "revision": _CLOCK[0], "attachment_id": "sha256:" + digest,
                    "sha256": digest, "size": len(body),
                    "replayed": params["idempotency_key"] in _SEEN or _SEEN.add(params["idempotency_key"])}
        if method == "handoff.attachment.read":
            return {"ok": True, "content_base64": base64.b64encode(
                b"openclaw-invariant-probe-attachment").decode()}
        if method == "handoff.integrity.verify":
            return {"ok": True, "valid": True, "projection_digest": "a" * 64}
        if method == "handoff.compact":
            _CLOCK[0] += 1
            return {"ok": True, "revision": _CLOCK[0], "owner_epoch": 1, "capability_epoch": 1,
                    "compaction_proof": {"generation": 1, "projection_digest": "b" * 64,
                                         "generation_digest": "c" * 64,
                                         "previous_generation_digest": None}}
        return refuse

    def test_schema2_battery_issues_thirteen_probes_and_a_sound_product_passes(self):
        global _SEEN, _CLOCK
        _SEEN, _CLOCK = set(), [3]
        actor = {"principal_id": "p", "channel": {"provider": "webchat"}, "grant": "real-grant"}
        runtime = self._runtime("test_001", self._sound_responder)
        runtime._probe_battery(actor, 2, {"revision": 2, "owner_epoch": 1, "capability_epoch": 1})
        self.assertIsNone(runtime.probe_error)
        self.assertEqual(len(runtime.probe_results), 13)
        self.assertEqual({origin for _method, _params, origin in runtime.issued},
                         {"environment_invariant_probe"})
        self.assertEqual(len(runtime.issued), 13)
        outcomes = runtime._required_outcomes(counters())
        unsatisfied = [item["id"] for item in outcomes if item["satisfied"] is False]
        self.assertEqual(unsatisfied, [], runtime.probe_results)
        self.assertEqual(runtime.environment_events,
                         ["product_invariant_battery_started", "product_invariant_battery_finished"])

    def test_schema1_battery_skips_the_schema2_positives(self):
        global _SEEN, _CLOCK
        _SEEN, _CLOCK = set(), [3]
        actor = {"principal_id": "p", "channel": {"provider": "webchat"}, "grant": "real-grant"}
        runtime = self._runtime("test_004", self._sound_responder, {"foreign": {"grant": "x"}})
        runtime._probe_battery(actor, 1, {"revision": 2, "owner_epoch": 1, "capability_epoch": 1})
        self.assertIsNone(runtime.probe_error)
        self.assertIn("schema1_compaction_refused", runtime.probe_results)
        self.assertIn("foreign_lineage_refused", runtime.probe_results)
        self.assertNotIn("attachment_content_addressed", runtime.probe_results)
        methods = [method for method, _params, _origin in runtime.issued]
        self.assertNotIn("handoff.attachment.put", methods)

    def test_battery_defect_never_escapes_as_an_exception(self):
        global _SEEN, _CLOCK
        _SEEN, _CLOCK = set(), [3]
        def explode(method, params):
            raise RuntimeError("stubbed gateway defect")
        actor = {"principal_id": "p", "channel": {"provider": "webchat"}, "grant": "real-grant"}
        runtime = self._runtime("test_001", explode)
        runtime._probe_battery(actor, 2, {"revision": 2})
        self.assertEqual(runtime.probe_error, "RuntimeError")
        self.assertEqual(runtime.environment_events[-1], "product_invariant_battery_finished")


if __name__ == "__main__":
    unittest.main()
