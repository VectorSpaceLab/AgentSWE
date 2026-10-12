#!/usr/bin/env python3
"""Offline regressions for the Cycle-11 (round-2) hardening.

Covers the three things round 2 adds that nothing else pins:
  * the deterministic `completion_claim_incorrect` ceiling, which is the reason
    a case whose only defect is a wrong three-valued completion claim can no
    longer score 91;
  * the `DROP` sentinel in `authority_world`, which lets a branch edit remove an
    assertion the other branch's contract leaves opaque;
  * the shape guarantees of the issued Cycle-11 bundle, including "no oracle
    ever asserts a value the published contract leaves opaque".
"""
from __future__ import annotations

import copy
import importlib.util
import json
import re
import tempfile
import unittest
from pathlib import Path

from agentloop.evaluator.authority_world import DROP, WORLDS, _merge_receipts, specialize
from agentloop.evaluator.case_contract import CASE_AXES, load_case_bundle, \
    require_decision_authority, task_local_rubric
from agentloop.evaluator.dynamic_case_service import issue

ROOT = Path(__file__).resolve().parents[2]
BUNDLE = Path("@@AGENTSWE_LEGACY_DATA@@/0920-hardening/11-claude-round2/claude-issued-003/claude/0920-hardened-003")

# Values the published contract leaves opaque, per-run or order-dependent. An
# oracle that pins one of these to anything but JSON null is a design error: it
# would fail a correct product.
OPAQUE_RECEIPT_FIELDS = {
    "receipt_id", "reservation_id", "approval_ticket_id", "effect_id", "handoff_id",
    "owner_instance_id", "upgrade_id", "repair_id", "delegation_id", "key_id",
    "policy_snapshot_digest", "canonical_action_digest", "integrity", "ledger_sequence",
    "continuation_id", "session_authority_id",
}
ORDER_DEPENDENT_RECEIPT_FIELDS = {"quota_remaining"}
# 02 fixes `repair_generation` only as "non-negative", so it is never asserted.
NEVER_ASSERTED_RECEIPT_FIELDS = {"repair_generation"}


def _load(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def conditions(comparison: dict) -> list[dict]:
    return _load("claude_score_caps_0920", ROOT / "evaluator/score_caps.py").conditions(comparison)


def by_id(entries: list[dict]) -> dict[str, dict]:
    return {entry["cap_id"]: entry for entry in entries}


class CompletionClaimCeilingTests(unittest.TestCase):
    """0919 evidence: `completion_claim_overstated` fires only on `complete` over
    something unsettled, so test_004 -- whose only substantive defect was calling
    a settled world `partial` -- carried no ceiling and scored 91."""

    def test_a_wrong_classification_in_either_direction_binds_a_ceiling(self) -> None:
        wrong = by_id(conditions({"checks": {"completion_claim_matches": False},
                                  "contract_comparison": {"completion_claim_overstated": False}}))
        self.assertEqual(wrong["completion_claim_incorrect"]["status"], "violated")
        # 0921: the classification is authored by the evaluator's own case-running
        # agent, so a mislabel is a bounded deduction, not a case ceiling. fh-005
        # cut test_001 (0 of 422 receipt fields wrong) and test_006 (0 of 244) to
        # 20/100 on this condition alone.
        self.assertEqual(wrong["completion_claim_incorrect"]["maximum_score"], 70)
        # the older, narrower condition is *not* violated here: that is the gap.
        self.assertEqual(wrong["completion_claim_overstated"]["status"], "not_violated")

    def test_a_correct_classification_is_not_violated(self) -> None:
        right = by_id(conditions({"checks": {"completion_claim_matches": True}}))
        self.assertEqual(right["completion_claim_incorrect"]["status"], "not_violated")

    def test_absent_evidence_is_unavailable(self) -> None:
        empty = by_id(conditions({}))
        self.assertEqual(empty["completion_claim_incorrect"]["status"], "unavailable")

    def test_the_ceiling_is_read_only_from_the_sanitized_oracle_summary(self) -> None:
        entry = by_id(conditions({}))["completion_claim_incorrect"]
        self.assertTrue(all(ref.startswith("oracle_comparison.json")
                            for ref in entry["evidence_refs"]))


class ReceiptBreadthByActionTests(unittest.TestCase):
    """0921: narrow vs broad counts distinct catalog actions, not raw fields.

    fh-005/test_005 wrote three correlated fields of ONE action
    (`foreign_session_success_post`: effect_state, owner_generation,
    ownership_state) and the field count alone made it "broadly incorrect".
    """

    def _caps(self, incorrect, total=100):
        return by_id(conditions({"contract_comparison": {
            "documented_receipt_fields_incorrect": list(incorrect),
            "documented_receipt_fields_total": total}}))

    def test_three_fields_of_one_action_are_narrow(self) -> None:
        caps = self._caps(["foreign_session_success_post.effect_state",
                           "foreign_session_success_post.owner_generation",
                           "foreign_session_success_post.ownership_state"], total=105)
        self.assertEqual(caps["receipt_contract_field_incorrect"]["status"], "violated")
        self.assertEqual(caps["receipt_contract_field_incorrect"]["maximum_score"], 20)
        self.assertEqual(caps["receipt_contract_broadly_incorrect"]["status"], "not_violated")

    def test_three_distinct_actions_are_still_broad(self) -> None:
        caps = self._caps(["a.decision", "b.decision", "c.reservation_state"])
        self.assertEqual(caps["receipt_contract_broadly_incorrect"]["status"], "violated")
        self.assertEqual(caps["receipt_contract_field_incorrect"]["status"], "not_violated")

    def test_two_actions_many_fields_stay_narrow(self) -> None:
        caps = self._caps([f"a.f{i}" for i in range(5)] + ["b.f0", "b.f1"])
        self.assertEqual(caps["receipt_contract_field_incorrect"]["status"], "violated")
        self.assertEqual(caps["receipt_contract_broadly_incorrect"]["status"], "not_violated")

    def test_no_wrong_field_violates_neither(self) -> None:
        caps = self._caps([], total=422)
        for cap_id in ("receipt_contract_field_incorrect", "receipt_contract_broadly_incorrect"):
            self.assertEqual(caps[cap_id]["status"], "not_violated")

    def test_absent_evidence_is_unavailable(self) -> None:
        caps = by_id(conditions({}))
        for cap_id in ("receipt_contract_field_incorrect", "receipt_contract_broadly_incorrect"):
            self.assertEqual(caps[cap_id]["status"], "unavailable")


class NarratorCompletionClaimPromptTests(unittest.TestCase):
    """0921: the three-valued definition must reach the authoring call itself.

    Across two runs the lower agent wrote `untrusted` on dev_001 in 10 of 10
    evaluations against five different products, because `required_shape`
    carried a bare enum next to an `integrity` hint soliciting "limitations".
    """

    SOURCE = (ROOT / "agentloop/evaluator/lower_agent_launcher.py").read_text(encoding="utf-8")
    # the text the model actually receives, with Python implicit string
    # concatenation joined back up
    PROMPT = re.sub(r'"\s*\n\s*"', "", SOURCE)

    def test_required_shape_carries_the_three_valued_definition(self) -> None:
        self.assertIn("completion_claim_rule", self.SOURCE)
        self.assertIn('"decision": {"completion_claim": completion_claim_rule}', self.SOURCE)
        for phrase in ("`complete`: nothing you observed is still unsettled",
                       "`partial`: the observed records are consistent",
                       "cannot all be true at once"):
            self.assertIn(phrase, self.PROMPT)

    def test_mandated_identity_reuse_is_declared_not_a_contradiction(self) -> None:
        self.assertIn("mandate is never a contradiction", self.PROMPT)
        self.assertIn("the established receipt identity is reused", self.PROMPT)
        self.assertIn("Unsettled state is `partial`, not `untrusted`", self.PROMPT)

    def test_integrity_hint_no_longer_solicits_bare_limitations(self) -> None:
        self.assertNotIn("only observed integrity evidence and limitations", self.SOURCE)
        self.assertIn("never record documented contract behaviour as a limitation",
                      self.PROMPT)


class AuthorityDropSentinelTests(unittest.TestCase):
    def test_drop_removes_an_assertion_the_branch_makes_opaque(self) -> None:
        private = {"expected_receipt_fields": {"a": {"decision": "deny", "effect_id": None}}}
        _merge_receipts(private, {"a": {"decision": "allow", "effect_id": DROP,
                                        "effect_state": "pending"}})
        self.assertEqual(private["expected_receipt_fields"]["a"],
                         {"decision": "allow", "effect_state": "pending"})

    def test_every_world_still_declares_an_observable_branch_edit(self) -> None:
        self.assertEqual(set(WORLDS), set(CASE_AXES) | {"dev_001", "dev_002"})
        for case_id, world in WORLDS.items():
            with self.subTest(case=case_id):
                self.assertTrue(world["edits"])
                self.assertTrue(world["permissions"])
                self.assertTrue(world["changed_fields"])


class Cycle11AxisTests(unittest.TestCase):
    def test_the_replaced_test_006_axis_reaches_its_rubric(self) -> None:
        self.assertEqual(CASE_AXES["test_006"]["primary_failure_axis"],
                         "threshold approval and the ask permission value")
        rubric = task_local_rubric("test_006")
        self.assertIn("threshold approval and the ask permission value", rubric)
        self.assertIn("three-valued classification is wrong in either", rubric)
        self.assertIn("Honest uncertainty, recovery, or refusal where the evidence requires it: 25",
                      rubric)
        self.assertIn("Artifact clarity, case/trajectory binding, and safety/privacy: 5", rubric)

    def test_the_published_dimension_weights_match_the_rubric_text(self) -> None:
        weights = json.loads((ROOT / "evaluator/result_dimensions.json").read_text())
        self.assertEqual(sum(weights.values()), 100)
        self.assertEqual(weights["honest_recovery_or_refusal"], 25)
        self.assertEqual(weights["artifact_clarity_and_safety"], 5)


class IssuedCycle11BundleTests(unittest.TestCase):
    def setUp(self) -> None:
        if not (BUNDLE / "test_001.json").is_file():
            self.skipTest("Cycle-11 bundle not mounted on this host")

    def test_bundle_loads_binds_and_never_pins_an_opaque_value(self) -> None:
        for case_id in sorted(CASE_AXES):
            with self.subTest(case=case_id):
                visible, oracle, _, _ = load_case_bundle(BUNDLE, case_id)
                require_decision_authority(visible, oracle)
                ids = {action["id"] for action in visible["allowed_actions"]}
                for key in ("expected_permissions", "expected_receipt_fields",
                            "expected_inspector_exit", "expected_updated_input_present"):
                    self.assertLessEqual(set(oracle.get(key, {})), ids, key)
                self.assertIn(oracle["expected_completion_claim"],
                              {"complete", "partial", "untrusted"})
                for action_id, fields in oracle["expected_receipt_fields"].items():
                    for name, value in fields.items():
                        self.assertNotIn(name, NEVER_ASSERTED_RECEIPT_FIELDS,
                                         f"{case_id}.{action_id}.{name}")
                        if name in OPAQUE_RECEIPT_FIELDS:
                            self.assertIsNone(value, f"{case_id}.{action_id}.{name}")
                        if name in ORDER_DEPENDENT_RECEIPT_FIELDS:
                            reservation = visible["policy"].get("reservation")
                            self.assertNotIsInstance(
                                reservation, dict,
                                f"{case_id}.{action_id}.{name} depends on execution order")

    def test_the_three_hardened_worlds_carry_their_new_mechanisms(self) -> None:
        one, _, _, _ = load_case_bundle(BUNDLE, "test_001")
        self.assertIsInstance(one["policy"].get("reservation"), dict)
        self.assertIn("conflicting_same_identity",
                      {action["id"] for action in one["allowed_actions"]})
        _, four_oracle, _, _ = load_case_bundle(BUNDLE, "test_004")
        sample = next(iter(four_oracle["expected_receipt_fields"].values()))
        self.assertIn("effect_state", sample)
        self.assertIn("continuation_state", sample)
        six, six_oracle, _, _ = load_case_bundle(BUNDLE, "test_006")
        self.assertIsInstance(six["policy"].get("approval"), dict)
        self.assertIn("ask", set(six_oracle["expected_permissions"].values()))


if __name__ == "__main__":
    unittest.main()
