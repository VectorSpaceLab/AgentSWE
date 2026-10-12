"""2026-09-20 Result hardening: durable-state discovery, publication boundary, ceilings.

These are evaluator-owned self-tests. They construct store layouts and case
worlds directly; no Candidate, Builder, judge or provider call is made.
"""
from __future__ import annotations

import hashlib
import json
from pathlib import Path
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[2]
for extra in (str(ROOT), str(ROOT / "agentloop"), str(ROOT / "evaluator")):
    if extra not in sys.path:
        sys.path.insert(0, extra)

import case_world  # noqa: E402
from scientific_audit import (  # noqa: E402
    DURABLE_RECORD_CAP, INCIDENT_CAP_DIMENSION, INCIDENT_CAP_FLOOR, INCIDENT_CAP_SPAN,
    PUBLICATION_BOUNDARY_CAP, case_world_score_caps, incident_ceiling, uncapped_total,
)

FINGERPRINT = "f" * 64


def write(path: Path, value) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, sort_keys=True) + "\n", encoding="utf-8")


def world_for(output: Path, workspace: Path) -> case_world.CaseWorld:
    return case_world.CaseWorld("test_006", workspace, output, {})


class DurableSessionDiscoveryTests(unittest.TestCase):
    def test_sharded_session_record_is_found_and_identity_is_promoted(self):
        with tempfile.TemporaryDirectory() as tmp:
            output = Path(tmp)
            write(output / "transaction_receipt.json",
                  {"request_fingerprint": FINGERPRINT, "phase": "prepared"})
            # A compliant product may shard the durable session directory.
            write(output / "state/sessions/sessions/aa/bb/state.json",
                  {"schema_version": 1, "phase": "prepared",
                   "request_fingerprint": FINGERPRINT,
                   "identity": {"project_id": "project-x", "owner_id": "worker-a"}})
            # Index and lock files are not session records.
            write(output / "state/sessions/index.json", {"schema_version": 1, "entries": []})
            world = world_for(output, output / "workspace")
            states = world._session_states()
            self.assertEqual(len(states), 1)
            self.assertEqual(states[0]["project_id"], "project-x")
            self.assertTrue(world._has_durable_phase("prepared"))
            self.assertFalse(world._has_durable_phase("committed"))
            self.assertEqual(world._durable_projects(), ["project-x"])

    def test_flat_layout_is_still_supported(self):
        with tempfile.TemporaryDirectory() as tmp:
            output = Path(tmp)
            write(output / "transaction_receipt.json",
                  {"request_fingerprint": FINGERPRINT, "phase": "committed"})
            write(output / f"state/sessions/{FINGERPRINT}.json",
                  {"schema_version": 1, "phase": "committed",
                   "request_fingerprint": FINGERPRINT, "project_id": "project-y"})
            world = world_for(output, output / "workspace")
            self.assertTrue(world._has_durable_phase("committed"))

    def test_a_record_without_a_fingerprint_is_not_a_peer(self):
        with tempfile.TemporaryDirectory() as tmp:
            output = Path(tmp)
            write(output / "transaction_receipt.json",
                  {"request_fingerprint": FINGERPRINT, "phase": "committed"})
            write(output / "state/sessions/current/abc.json",
                  {"schema_version": 1, "phase": "committed"})
            write(output / "state/sessions/sessions/aa/bb/state.json",
                  {"schema_version": 1, "phase": "committed", "request_fingerprint": "a" * 64})
            snapshot = world_for(output, output / "workspace")._protected_state_snapshot()
            self.assertEqual(len(snapshot["peer_session_sha256"]), 1)

    def test_published_snapshot_sees_nested_committed_copies_and_budgets(self):
        with tempfile.TemporaryDirectory() as tmp:
            output = Path(tmp)
            write(output / "state/attestations/one.json", {"status": "valid"})
            write(output / "state/outbox/one.json", {"events": {}})
            write(output / "state/sessions/sessions/aa/bb/committed/attestation.json", {"status": "valid"})
            write(output / "state/budgets/projects/one.json",
                  {"project_id": "project-z", "settled_micros": 42})
            snapshot = world_for(output, output / "workspace")._protected_state_snapshot()
            self.assertEqual(len(snapshot["published_file_sha256"]), 3)
            self.assertEqual(snapshot["project_settled_micros"], {"project-z": 42})


class PublicationBoundaryTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.world = world_for(Path(self.tmp.name), Path(self.tmp.name) / "workspace")

    def tearDown(self):
        self.tmp.cleanup()

    @staticmethod
    def state(published, settled=None):
        return {"published_file_sha256": dict(published),
                "project_settled_micros": dict(settled or {}),
                "peer_session_sha256": {}}

    def test_commit_and_verify_may_add_published_bytes(self):
        for operation in ("commit", "verify"):
            record = self.world._publication_boundary(
                operation=operation, before=self.state({}),
                after=self.state({"attestations/a.json": "1"}, {"p": 5}), probe=None)
            self.assertFalse(record["violation"], record)

    def test_status_may_not_publish(self):
        record = self.world._publication_boundary(
            operation="status", before=self.state({}),
            after=self.state({"attestations/a.json": "1"}), probe=None)
        self.assertTrue(record["violation"])
        self.assertIn("status", record["reason"])

    def test_prepare_may_not_move_settled_totals(self):
        record = self.world._publication_boundary(
            operation="prepare", before=self.state({}, {"p": 0}),
            after=self.state({}, {"p": 7}), probe=None)
        self.assertTrue(record["violation"])

    def test_no_operation_may_rewrite_published_bytes(self):
        record = self.world._publication_boundary(
            operation="commit", before=self.state({"attestations/a.json": "1"}),
            after=self.state({"attestations/a.json": "2"}), probe=None)
        self.assertTrue(record["violation"])
        self.assertEqual(record["modified"], ["attestations/a.json"])

    def test_an_evaluator_probe_makes_the_delta_unattributable(self):
        record = self.world._publication_boundary(
            operation="status", before=self.state({}),
            after=self.state({"attestations/a.json": "1"}),
            probe={"timing": "before", "operation": "verify"})
        self.assertFalse(record["attributable"])
        self.assertFalse(record["violation"])


class CaseWorldCeilingTests(unittest.TestCase):
    @staticmethod
    def build(base: Path, *, sequence, attempts):
        """Write a hash-chained case world the way the launcher does."""
        records = []
        for index, attempt in enumerate(attempts, 1):
            relative = f"case_world_evidence/attempt_{index:03d}.json"
            payload = {"schema_version": "agentswe-ai-scientist-case-world-evidence-v2",
                       "attempt_sequence": index, "operation": attempt["operation"],
                       "transition": attempt.get("transition", sequence[0]),
                       "advanced": attempt.get("advanced", False),
                       "checks": attempt.get("checks", {}),
                       "evidence": attempt.get("evidence", {})}
            path = base / relative
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(json.dumps(payload, sort_keys=True, indent=2) + "\n", encoding="utf-8")
            records.append({"attempt_sequence": index, "evidence_path": relative,
                            "evidence_sha256": hashlib.sha256(path.read_bytes()).hexdigest()})
        world = {"schema_version": "agentswe-ai-scientist-case-world-v2",
                 "case_id": "test_006", "sequence": list(sequence),
                 "events": [record for record, attempt in zip(records, attempts) if attempt.get("advanced")],
                 "attempts": records}
        world["world_digest"] = case_world.canonical_digest(world)
        (base / "case_world.json").write_text(json.dumps(world, sort_keys=True) + "\n", encoding="utf-8")

    def caps(self, base: Path) -> dict:
        return {entry["cap_id"]: entry for entry in case_world_score_caps(base)}

    def test_no_case_world_leaves_every_ceiling_unavailable(self):
        with tempfile.TemporaryDirectory() as tmp:
            caps = self.caps(Path(tmp))
            self.assertEqual({entry["status"] for entry in caps.values()}, {"unavailable"})
            self.assertEqual(caps["ai_durable_release_record_v6"]["maximum_score"], DURABLE_RECORD_CAP)
            self.assertEqual(caps["ai_governed_publication_boundary_v6"]["maximum_score"],
                             PUBLICATION_BOUNDARY_CAP)

    def test_incident_ceiling_is_the_published_formula_on_case_recovery(self):
        """§30 bounds the `case_recovery` dimension, not the case total.

        The published percentage is unchanged; what it bounds is not. See
        scientific_audit.incident_ceiling and input/03 §30.
        """
        for established, required in ((0, 5), (1, 5), (2, 4), (3, 3)):
            with tempfile.TemporaryDirectory() as tmp:
                base = Path(tmp)
                attempts = [{"operation": "prepare", "advanced": index < established}
                            for index in range(required)]
                self.build(base, sequence=[f"phase_{i}" for i in range(required)], attempts=attempts)
                entry = self.caps(base)["ai_case_incident_resolution_v6"]
                if established >= required:
                    self.assertEqual(entry["status"], "not_violated")
                    self.assertEqual(entry["maximum_score"], uncapped_total())
                    continue
                ceiling = incident_ceiling(established, required)
                self.assertEqual(entry["status"], "violated")
                # the published percentage, verbatim
                self.assertEqual(ceiling["percent"],
                                 INCIDENT_CAP_FLOOR + (INCIDENT_CAP_SPAN * established) // required)
                # it bounds the dimension ...
                self.assertEqual(ceiling["dimension"], INCIDENT_CAP_DIMENSION)
                self.assertEqual(ceiling["dimension_ceiling"],
                                 (ceiling["dimension_maximum"] * ceiling["percent"]) // 100)
                # ... and the contract carries the equivalent bound on the sum.
                self.assertEqual(entry["maximum_score"], ceiling["total_ceiling"])
                self.assertEqual(entry["maximum_score"], uncapped_total()
                                 - ceiling["dimension_maximum"] + ceiling["dimension_ceiling"])
                self.assertGreater(entry["maximum_score"],
                                   uncapped_total() - ceiling["dimension_maximum"] - 1)
                self.assertIn(INCIDENT_CAP_DIMENSION, entry["reason"])
                self.assertTrue(entry["evidence_refs"])

    def test_incident_ceiling_never_touches_the_other_four_dimensions(self):
        """A total of 0 established steps still leaves 65 of 100 reachable."""
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            self.build(base, sequence=["a", "b", "c"], attempts=[{"operation": "status"}])
            entry = self.caps(base)["ai_case_incident_resolution_v6"]
            ceiling = incident_ceiling(0, 3)
            self.assertEqual(entry["maximum_score"], ceiling["total_ceiling"])
            self.assertGreaterEqual(entry["maximum_score"],
                                    uncapped_total() - ceiling["dimension_maximum"])
            # §29 is unchanged and still bounds the case total.
            self.assertEqual(DURABLE_RECORD_CAP, 40)
            self.assertEqual(PUBLICATION_BOUNDARY_CAP, 30)

    def test_receipt_without_durable_corroboration_is_a_ceiling(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            self.build(base, sequence=["phase_0"], attempts=[
                {"operation": "prepare", "checks": {"operation_relevant": True,
                                                    "prepared": True, "durable_prepared": False}}])
            entry = self.caps(base)["ai_durable_release_record_v6"]
            self.assertEqual(entry["status"], "violated")
            self.assertEqual(entry["maximum_score"], DURABLE_RECORD_CAP)

    def test_corroborated_receipt_is_not_a_ceiling(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            self.build(base, sequence=["phase_0"], attempts=[
                {"operation": "prepare", "checks": {"operation_relevant": True,
                                                    "prepared": True, "durable_prepared": True}}])
            self.assertEqual(self.caps(base)["ai_durable_release_record_v6"]["status"], "not_violated")

    def test_publication_boundary_violation_is_a_ceiling(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            self.build(base, sequence=["phase_0"], attempts=[
                {"operation": "status", "evidence": {"publication_boundary": {
                    "operation": "status", "attributable": True, "violation": True,
                    "reason": "`status` created published attestation bytes"}}}])
            entry = self.caps(base)["ai_governed_publication_boundary_v6"]
            self.assertEqual(entry["status"], "violated")
            self.assertEqual(entry["maximum_score"], PUBLICATION_BOUNDARY_CAP)

    def test_unattributable_probe_delta_is_not_a_ceiling(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            self.build(base, sequence=["phase_0"], attempts=[
                {"operation": "status", "evidence": {"publication_boundary": {
                    "operation": "status", "attributable": False, "violation": False,
                    "reason": "probe"}}}])
            self.assertEqual(self.caps(base)["ai_governed_publication_boundary_v6"]["status"],
                             "unavailable")

    def test_tampered_case_world_is_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            self.build(base, sequence=["phase_0"], attempts=[{"operation": "prepare"}])
            world = json.loads((base / "case_world.json").read_text())
            world["events"] = world["attempts"]
            (base / "case_world.json").write_text(json.dumps(world, sort_keys=True), encoding="utf-8")
            with self.assertRaises(ValueError):
                case_world_score_caps(base)

    def test_tampered_attempt_evidence_is_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            self.build(base, sequence=["phase_0"], attempts=[{"operation": "prepare"}])
            evidence = base / "case_world_evidence/attempt_001.json"
            evidence.write_text(evidence.read_text() + "\n", encoding="utf-8")
            with self.assertRaises(ValueError):
                case_world_score_caps(base)


class PublishedArithmeticTests(unittest.TestCase):
    def test_result_dimensions_total_one_hundred_and_match_the_rubric(self):
        dimensions = json.loads((ROOT / "evaluator/result_dimensions.json").read_text(encoding="utf-8"))
        self.assertEqual(sum(dimensions.values()), 100)
        rubric = (ROOT / "agentloop/result_rubric.md").read_text(encoding="utf-8")
        for name, maximum in dimensions.items():
            self.assertIn(f"| {name} | {maximum} |", rubric)

    def test_public_assertion_arithmetic_still_totals_one_hundred(self):
        manifest = json.loads((ROOT / "dev_cases/public_assertions.json").read_text(encoding="utf-8"))
        for case_id, entries in manifest["cases"].items():
            self.assertEqual(sum(entries.values()), 100, case_id)
            self.assertIn("STATE.DURABLE_SESSION_RECORD", entries, case_id)
            self.assertIn("CROSS.PUBLICATION_BOUNDARY", entries, case_id)

    def test_new_ceilings_are_published_in_the_builder_visible_spec(self):
        spec = (ROOT / "input/03_requirements_and_constraints.md").read_text(encoding="utf-8")
        self.assertIn("29.", spec)
        self.assertIn("30.", spec)
        self.assertIn("10 + floor(50 × established / required)", spec)
        self.assertIn("§29", (ROOT / "agentloop/result_rubric.md").read_text(encoding="utf-8"))


class ActionBudgetTests(unittest.TestCase):
    """The 2026-09-20 budget change: 10 actions, and the authoring call still fits.

    The pre-dispatch guard is purely time-based, so a larger step cap cannot make
    its arithmetic wrong -- but a longer loop can spend the budget the one
    authoring call needs. The loop therefore yields at twice the single-request
    reserve, and these tests pin that relationship rather than the raw numbers.
    """

    @classmethod
    def setUpClass(cls):
        import lower_agent_launcher
        cls.launcher = lower_agent_launcher

    def test_published_action_budget_is_ten(self):
        self.assertEqual(self.launcher.MAX_ACTION_STEPS, 10)
        resources = (ROOT / "input/04_resources.md").read_text(encoding="utf-8")
        self.assertIn("Each case has at most 10 action decisions", resources)
        verifier = (ROOT / "evaluator/verify_0911_request_reserve.py").read_text(encoding="utf-8")
        self.assertIn("self.assertEqual(launcher.MAX_ACTION_STEPS, 10)", verifier)

    def test_action_loop_reserve_covers_one_more_request(self):
        self.assertEqual(self.launcher.LOWER_ACTION_LOOP_RESERVE_SECONDS,
                         self.launcher.LOWER_DISPATCH_RESERVE_SECONDS * 2)

    def test_the_loop_yields_while_the_authoring_call_still_fits(self):
        """At every budget the loop still accepts, the authoring call also fits."""
        guard = self.launcher.dispatch_guard_decision
        loop_reserve = self.launcher.LOWER_ACTION_LOOP_RESERVE_SECONDS
        single = self.launcher.LOWER_DISPATCH_RESERVE_SECONDS
        deadline = 1000.0
        for remaining in (600.0, loop_reserve + 1, loop_reserve, loop_reserve - 1, single, single - 1):
            now = deadline - remaining
            loop_ok = guard("action", "action:10", reserve=loop_reserve,
                            deadline=deadline, now=now) is None
            authoring_ok = guard("final artifact", "authoring:1",
                                 deadline=deadline, now=now) is None
            if loop_ok:
                self.assertTrue(authoring_ok,
                                "the loop accepted an action at %.1fs but the authoring "
                                "call would be refused" % remaining)
        # and the loop must actually stop before the single-request reserve
        self.assertIsNotNone(guard("action", "action:10", reserve=loop_reserve,
                                   deadline=deadline, now=deadline - single))

    def test_every_action_slot_and_the_authoring_slot_stay_distinct(self):
        slots = [f"action:{index}" for index in range(1, self.launcher.MAX_ACTION_STEPS + 1)]
        slots.append("authoring:1")
        self.assertEqual(len(set(slots)), self.launcher.MAX_ACTION_STEPS + 1)

    def test_guard_evidence_records_the_budget_it_enforced(self):
        with tempfile.TemporaryDirectory() as tmp:
            value = self.launcher.write_dispatch_guard_evidence(Path(tmp), "test_001", 3)
            self.assertEqual(value["max_action_steps"], self.launcher.MAX_ACTION_STEPS)
            self.assertEqual(value["action_loop_reserve_seconds"],
                             self.launcher.LOWER_ACTION_LOOP_RESERVE_SECONDS)
            self.assertEqual(value["reserve_seconds"],
                             self.launcher.LOWER_DISPATCH_RESERVE_SECONDS)
            self.assertEqual(value["turns_completed"], 3)
            self.assertFalse(value["stopped_by_evaluator_budget_guard"])
            self.assertTrue((Path(tmp) / "dispatch_guard.json").is_file())


if __name__ == "__main__":
    unittest.main()
