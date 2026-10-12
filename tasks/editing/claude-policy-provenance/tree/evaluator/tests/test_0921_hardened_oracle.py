#!/usr/bin/env python3
"""Cycle-12 hardening: durable-state obligations and the ceilings that bind them.

Every assertion here is offline: no provider, no broker, no Docker, no
Candidate.  The bundle-shaped tests are skipped unless ``CLAUDE_HARDENED_BUNDLE``
points at an issued bundle directory.
"""
from __future__ import annotations

import json
import os
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
if str(ROOT / "evaluator") not in sys.path:
    sys.path.insert(0, str(ROOT / "evaluator"))

from agentloop.evaluator import hidden_executor as HX  # noqa: E402
from agentloop.evaluator.case_contract import task_local_rubric  # noqa: E402
import score_caps  # noqa: E402

ZERO = "sha256:" + "0" * 64
DIGEST_A = "sha256:" + "a" * 64
DIGEST_B = "sha256:" + "b" * 64
NEW_CAPS = ("required_action_order_incorrect", "receipt_identity_not_reused",
            "ledger_view_contract_incorrect", "ledger_not_self_consistent")


def _entry(entries, cap_id):
    for item in entries:
        if item["cap_id"] == cap_id:
            return item
    raise AssertionError(f"missing cap {cap_id}")


def _view(action_id, stdout, *, total=None):
    observation = {"action_id": action_id, "action_type": "inspector_command",
                   "operation": "--audit", "stdout_excerpt": stdout, "exit_code": 0}
    event = {"action_id": action_id, "stdout_bytes": len(stdout.encode("utf-8"))
             if total is None else total}
    return {action_id: observation}, {action_id: event}


def _record(sequence, previous, digest, **extra):
    base = {"schema_version": 1, "kind": "pre", "sequence": sequence,
            "previous_event_digest": previous, "event_digest": digest}
    base.update(extra)
    return base


class ShapeTests(unittest.TestCase):
    def test_every_supported_shape_accepts_and_rejects(self):
        cases = [
            ("null", None, "x"), ("non_null", 0, None),
            ("non_empty_string", "a", ""), ("positive_int", 1, 0),
            ("non_negative_int", 0, -1), ("sha256_digest", DIGEST_A, "sha256:zz"),
            ("prefixed_id:pol_", "pol_x", "res_x"),
        ]
        for shape, good, bad in cases:
            with self.subTest(shape=shape):
                self.assertTrue(HX._shape_ok(good, shape))
                self.assertFalse(HX._shape_ok(bad, shape))

    def test_booleans_are_never_integers(self):
        self.assertFalse(HX._shape_ok(True, "positive_int"))
        self.assertFalse(HX._shape_ok(False, "non_negative_int"))

    def test_unknown_shape_never_passes(self):
        self.assertFalse(HX._shape_ok("anything", "not_a_shape"))


class ViewRecordTests(unittest.TestCase):
    def test_truncated_view_drops_only_the_partial_first_line(self):
        text = 'quence": 4}\n{"a": 1}\n{"b": 2}\n'
        records, malformed = HX._view_records({"stdout_excerpt": text}, True)
        self.assertEqual(records, [{"a": 1}, {"b": 2}])
        self.assertEqual(malformed, 0)

    def test_untruncated_view_counts_a_malformed_line(self):
        records, malformed = HX._view_records({"stdout_excerpt": 'oops\n{"a": 1}\n'}, False)
        self.assertEqual(records, [{"a": 1}])
        self.assertEqual(malformed, 1)

    def test_truncation_is_detected_from_the_launcher_byte_count(self):
        self.assertTrue(HX._view_truncated({"stdout_excerpt": "ab"}, {"stdout_bytes": 99}))
        self.assertFalse(HX._view_truncated({"stdout_excerpt": "ab"}, {"stdout_bytes": 2}))


class AuditChainTests(unittest.TestCase):
    GOOD = "\n".join(json.dumps(item) for item in [
        _record(1, ZERO, DIGEST_A), _record(2, DIGEST_A, DIGEST_B)])

    def test_valid_chain_from_the_documented_anchor(self):
        obs, ev = _view("v", self.GOOD)
        checks, unavailable = HX._view_contract(
            {"v": {"audit_chain": True, "chain_from_anchor": True}}, obs, ev)
        self.assertTrue(checks["v.audit_chain"])
        self.assertEqual(unavailable, [])

    def test_broken_link_fails(self):
        text = "\n".join(json.dumps(item) for item in [
            _record(1, ZERO, DIGEST_A), _record(2, ZERO, DIGEST_B)])
        obs, ev = _view("v", text)
        checks, _ = HX._view_contract({"v": {"audit_chain": True}}, obs, ev)
        self.assertFalse(checks["v.audit_chain"])

    def test_non_monotonic_sequence_fails(self):
        text = "\n".join(json.dumps(item) for item in [
            _record(2, ZERO, DIGEST_A), _record(1, DIGEST_A, DIGEST_B)])
        obs, ev = _view("v", text)
        checks, _ = HX._view_contract({"v": {"audit_chain": True}}, obs, ev)
        self.assertFalse(checks["v.audit_chain"])

    def test_anchor_is_only_required_when_the_view_starts_at_the_beginning(self):
        text = json.dumps(_record(5, DIGEST_A, DIGEST_B))
        obs, ev = _view("v", text)
        without = HX._view_contract({"v": {"audit_chain": True}}, obs, ev)[0]
        self.assertTrue(without["v.audit_chain"])
        withanchor = HX._view_contract(
            {"v": {"audit_chain": True, "chain_from_anchor": True}}, obs, ev)[0]
        self.assertFalse(withanchor["v.audit_chain"])

    def test_a_truncated_view_never_fails_the_anchor(self):
        # The first visible line of a truncated view is a partial record and is
        # dropped, so the chain is checked over what remains.
        text = "\n".join(["ence\": 4}", json.dumps(_record(5, DIGEST_A, DIGEST_B)),
                          json.dumps(_record(6, DIGEST_B, DIGEST_A))])
        obs, ev = _view("v", text, total=99999)
        checks = HX._view_contract(
            {"v": {"audit_chain": True, "chain_from_anchor": True}}, obs, ev)[0]
        self.assertTrue(checks["v.audit_chain"])


class ViewContractTests(unittest.TestCase):
    TEXT = "\n".join(json.dumps(item) for item in [
        _record(1, ZERO, DIGEST_A, event_id="e1", receipt_id="pol_1", decision="allow"),
        _record(2, DIGEST_A, DIGEST_B, kind="post", event_id="e1",
                receipt_id="pol_1", decision="allow")])

    def test_counts_fields_and_required_records(self):
        obs, ev = _view("v", self.TEXT)
        contract = {"v": {"records": {"exact": 2},
                          "required_fields": ["schema_version", "sequence"],
                          "require_records": [
                              {"label": "post", "match": {"kind": "post"},
                               "fields": {"event_id": "e1"},
                               "shapes": {"sequence": "positive_int"}}]}}
        checks, unavailable = HX._view_contract(contract, obs, ev)
        self.assertTrue(all(checks.values()), checks)
        self.assertEqual(unavailable, [])

    def test_missing_required_record_is_a_violation(self):
        obs, ev = _view("v", self.TEXT)
        checks, _ = HX._view_contract(
            {"v": {"require_records": [{"label": "abort", "match": {"kind": "abort"}}]}}, obs, ev)
        self.assertFalse(checks["v.abort"])

    def test_truncation_downgrades_count_and_presence_to_unavailable(self):
        obs, ev = _view("v", self.TEXT, total=99999)
        checks, unavailable = HX._view_contract(
            {"v": {"records": {"exact": 9},
                   "require_records": [{"label": "abort", "match": {"kind": "abort"}}]}}, obs, ev)
        self.assertNotIn("v.record_count", checks)
        self.assertNotIn("v.abort", checks)
        self.assertEqual(sorted(unavailable), ["v.abort", "v.record_count"])

    def test_empty_is_an_assertable_count(self):
        obs, ev = _view("v", "")
        checks, _ = HX._view_contract({"v": {"records": "empty"}}, obs, ev)
        self.assertTrue(checks["v.record_count"])
        obs, ev = _view("v", self.TEXT)
        checks, _ = HX._view_contract({"v": {"records": "empty"}}, obs, ev)
        self.assertFalse(checks["v.record_count"])

    def test_exact_field_set_and_forbidden_material(self):
        text = json.dumps({"a": 1, "b": "PPL-MARKER-1"})
        obs, ev = _view("v", text)
        checks, _ = HX._view_contract(
            {"v": {"exact_fields": ["a"], "forbidden_substrings": ["PPL-MARKER-1"]}}, obs, ev)
        self.assertFalse(checks["v.exact_record_fields"])
        self.assertFalse(checks["v.no_forbidden_material"])

    def test_a_view_the_agent_never_ran_is_unavailable(self):
        checks, unavailable = HX._view_contract({"v": {"records": "empty"}}, {}, {})
        self.assertEqual(checks, {})
        self.assertEqual(unavailable, ["v.view"])


class LedgerCrossCheckTests(unittest.TestCase):
    OBS = {
        "view": {"action_id": "view", "action_type": "inspector_command", "operation": "--audit",
                 "stdout_excerpt": json.dumps(_record(1, ZERO, DIGEST_A, event_id="e1",
                                                      receipt_id="pol_1", decision="allow")),
                 "exit_code": 0},
        "hook": {"action_id": "hook", "action_type": "hook_event",
                 "receipt": {"receipt_id": "pol_1", "decision": "allow"}},
        "other": {"action_id": "other", "action_type": "hook_event",
                  "receipt": {"receipt_id": "pol_9", "decision": "allow"}},
    }
    EV = {"view": {"stdout_bytes": len(OBS["view"]["stdout_excerpt"].encode())}}

    def test_journal_agreeing_with_the_receipt_passes(self):
        checks, unavailable = HX._ledger_cross_check(
            {"view_action_id": "view",
             "records": [{"label": "pre", "action_id": "hook",
                          "match": {"event_id": "e1", "kind": "pre"},
                          "fields": ["receipt_id", "decision"]}]}, self.OBS, self.EV)
        self.assertTrue(checks["pre"])
        self.assertEqual(unavailable, [])

    def test_journal_disagreeing_with_the_receipt_fails(self):
        checks, _ = HX._ledger_cross_check(
            {"view_action_id": "view",
             "records": [{"label": "pre", "action_id": "other",
                          "match": {"event_id": "e1", "kind": "pre"},
                          "fields": ["receipt_id"]}]}, self.OBS, self.EV)
        self.assertFalse(checks["pre"])

    def test_an_event_the_product_never_committed_fails(self):
        checks, _ = HX._ledger_cross_check(
            {"view_action_id": "view",
             "records": [{"label": "post", "action_id": "hook",
                          "match": {"event_id": "e1", "kind": "post"},
                          "fields": ["receipt_id"]}]}, self.OBS, self.EV)
        self.assertFalse(checks["post"])

    def test_a_truncated_journal_downgrades_a_missing_event(self):
        events = {"view": {"stdout_bytes": 999999}}
        checks, unavailable = HX._ledger_cross_check(
            {"view_action_id": "view",
             "records": [{"label": "post", "action_id": "hook",
                          "match": {"event_id": "e1", "kind": "post"},
                          "fields": ["receipt_id"]}]}, self.OBS, events)
        self.assertEqual(checks, {})
        self.assertEqual(unavailable, ["post"])


class ReceiptEqualityTests(unittest.TestCase):
    OBS = {"a": {"receipt": {"receipt_id": "pol_1", "ledger_sequence": 3}},
           "b": {"receipt": {"receipt_id": "pol_1", "ledger_sequence": 4}},
           "c": {"receipt": {"receipt_id": None}}}

    def test_equal_values_pass_and_unequal_fail(self):
        checks, _ = HX._receipt_equalities(
            [{"label": "id", "fields": ["a.receipt_id", "b.receipt_id"]},
             {"label": "seq", "fields": ["a.ledger_sequence", "b.ledger_sequence"]}], self.OBS)
        self.assertTrue(checks["id"])
        self.assertFalse(checks["seq"])

    def test_an_all_null_group_is_not_reuse(self):
        checks, _ = HX._receipt_equalities(
            [{"label": "null", "fields": ["c.receipt_id", "c.receipt_id"]}], self.OBS)
        self.assertFalse(checks["null"])

    def test_an_unobservable_action_is_unavailable(self):
        checks, unavailable = HX._receipt_equalities(
            [{"label": "gone", "fields": ["a.receipt_id", "missing.receipt_id"]}], self.OBS)
        self.assertEqual(checks, {})
        self.assertEqual(unavailable, ["gone"])


class ShapesFoldIntoTheReceiptContractTests(unittest.TestCase):
    def test_a_wrong_shape_counts_as_a_wrong_documented_field(self):
        oracle = {"expected_receipt_field_shapes": {"a": {"receipt_id": "prefixed_id:pol_"}}}
        trajectory = {"binding": {}, "product_events": [{"action_id": "a"}],
                      "observations": [{"action_id": "a", "receipt": {"receipt_id": "nope"}}]}
        comparison = HX._oracle_comparison("test_001", oracle, trajectory, {"binding": {}})
        contract = comparison["contract_comparison"]
        self.assertEqual(contract["documented_receipt_fields_incorrect"], ["a.receipt_id"])
        self.assertEqual(contract["documented_receipt_fields_total"], 1)


class NewCeilingTests(unittest.TestCase):
    def test_every_new_ceiling_has_all_three_states(self):
        expected = {"required_action_order_incorrect": ("required_action_order_matches", True, False),
                    "receipt_identity_not_reused": None,
                    "ledger_view_contract_incorrect": None,
                    "ledger_not_self_consistent": None}
        triples = {
            "receipt_identity_not_reused": ("receipt_identity_incorrect",
                                            "receipt_identity_checks_total"),
            "ledger_view_contract_incorrect": ("view_contract_incorrect",
                                               "view_contract_checks_total"),
            "ledger_not_self_consistent": ("ledger_cross_check_incorrect",
                                           "ledger_cross_check_total"),
        }
        self.assertEqual(_entry(score_caps.conditions({}), "required_action_order_incorrect")["status"],
                         "unavailable")
        for cap_id, (wrong_key, total_key) in triples.items():
            with self.subTest(cap=cap_id):
                self.assertEqual(_entry(score_caps.conditions({}), cap_id)["status"], "unavailable")
                clean = {"contract_comparison": {wrong_key: [], total_key: 2}}
                self.assertEqual(_entry(score_caps.conditions(clean), cap_id)["status"],
                                 "not_violated")
                dirty = {"contract_comparison": {wrong_key: ["x"], total_key: 2}}
                self.assertEqual(_entry(score_caps.conditions(dirty), cap_id)["status"], "violated")
        del expected

    def test_order_ceiling_reads_only_the_oracle_summary(self):
        for value, status in ((True, "not_violated"), (False, "violated"), (None, "unavailable")):
            oracle = {"contract_comparison": {"required_action_order_matches": value}}
            self.assertEqual(
                _entry(score_caps.conditions(oracle), "required_action_order_incorrect")["status"],
                status)

    def test_new_ceilings_read_only_the_sanitized_oracle_summary(self):
        entries = {item["cap_id"]: item for item in score_caps.conditions({})}
        for cap_id in NEW_CAPS:
            with self.subTest(cap=cap_id):
                self.assertIn(cap_id, entries)
                for ref in entries[cap_id]["evidence_refs"]:
                    self.assertTrue(ref.startswith("oracle_comparison.json#"))
                    self.assertNotIn("agent_result", ref)
                    self.assertNotIn("trajectory", ref)

    def test_maxima_and_evidence_references(self):
        entries = {item["cap_id"]: item for item in score_caps.conditions({})}
        self.assertEqual(entries["required_action_order_incorrect"]["maximum_score"], 25)
        self.assertEqual(entries["receipt_identity_not_reused"]["maximum_score"], 12)
        self.assertEqual(entries["ledger_view_contract_incorrect"]["maximum_score"], 15)
        self.assertEqual(entries["ledger_not_self_consistent"]["maximum_score"], 10)
        for cap_id in NEW_CAPS:
            refs = entries[cap_id]["evidence_refs"]
            self.assertTrue(refs and all(ref.startswith("oracle_comparison.json#") for ref in refs))

    def test_cap_ids_are_unique_and_shared_judge_compatible(self):
        import re
        entries = score_caps.conditions({})
        ids = [item["cap_id"] for item in entries]
        self.assertEqual(len(ids), len(set(ids)))
        for item in entries:
            self.assertTrue(re.fullmatch(r"[a-z][a-z0-9_]*", item["cap_id"]))
            self.assertTrue(0 <= item["maximum_score"] <= 100)
            self.assertTrue(item["requirement_ref"].strip() and item["reason"].strip())


class RubricSurfaceTests(unittest.TestCase):
    def test_judge_rubric_names_every_new_major_error(self):
        text = task_local_rubric("test_001")
        for phrase in ("bounded view is the product's own durable evidence",
                       "The catalog is a sequence",
                       "did not reuse",
                       "never appended at all"):
            self.assertIn(phrase, text)

    def test_task_owner_rubric_lists_every_new_ceiling(self):
        text = Path(ROOT / "evaluator/rubric.md").read_text(encoding="utf-8")
        for cap_id in NEW_CAPS:
            self.assertIn(cap_id, text)


class PublishedContractTests(unittest.TestCase):
    def test_cycle_12_clauses_are_published(self):
        text = Path(ROOT / "input/03_requirements_and_constraints.md").read_text(encoding="utf-8")
        self.assertIn("Cycle-12", text)
        for clause in ("10.", "11.", "12."):
            self.assertIn(clause, text)
        self.assertIn("canonical_action_digest", text)
        self.assertIn('kind="post"', text)
        self.assertIn("not_created", text)

    def test_interface_publishes_the_same_three_rules(self):
        text = Path(ROOT / "input/02_interface_and_delivery.md").read_text(encoding="utf-8")
        self.assertIn("Cycle-12", text)
        self.assertIn("ledger_sequence", text)


class DevWorldTeachesTheMechanismsTests(unittest.TestCase):
    def setUp(self):
        self.case = json.loads((ROOT / "agentloop/cases/dev_001.json").read_text(encoding="utf-8"))
        self.oracle = json.loads(
            (ROOT / "agentloop/cases/dev_001.oracle.json").read_text(encoding="utf-8"))

    def test_dev_world_carries_a_conflict_a_duplicate_and_a_matching_post(self):
        ids = {item["id"] for item in self.case["allowed_actions"]}
        self.assertIn("duplicate_read_source", ids)
        self.assertIn("conflicting_content_same_identity", ids)
        self.assertIn("settle_audit_command", ids)
        post = [item for item in self.case["allowed_actions"]
                if item["id"] == "settle_audit_command"][0]
        self.assertEqual(post["event"]["hook_event_name"], "PostToolUse")

    def test_dev_world_checks_the_same_durable_obligations(self):
        self.assertTrue(self.oracle.get("expected_view_contract"))
        self.assertTrue(self.oracle.get("expected_ledger_cross_check"))
        self.assertTrue(self.oracle.get("expected_receipt_field_equalities"))
        self.assertEqual(self.oracle["expected_permissions"]["conflicting_content_same_identity"],
                         "deny")


@unittest.skipUnless(os.environ.get("CLAUDE_HARDENED_BUNDLE"),
                     "set CLAUDE_HARDENED_BUNDLE to the issued bundle directory")
class IssuedBundleTests(unittest.TestCase):
    BUNDLE = Path(os.environ.get("CLAUDE_HARDENED_BUNDLE", "."))
    OPAQUE = {"receipt_id", "reservation_id", "approval_ticket_id", "effect_id", "handoff_id",
              "owner_instance_id", "upgrade_id", "repair_id", "delegation_id", "key_id",
              "policy_snapshot_digest", "canonical_action_digest", "integrity", "ledger_sequence",
              "continuation_id", "session_authority_id"}

    def oracles(self):
        for case_id in HX.CASE_IDS:
            yield case_id, json.loads(
                (self.BUNDLE / f"{case_id}.oracle.json").read_text(encoding="utf-8"))

    def test_every_world_declares_a_durable_state_obligation(self):
        for case_id, oracle in self.oracles():
            with self.subTest(case=case_id):
                self.assertTrue(oracle.get("expected_view_contract")
                                or oracle.get("expected_ledger_cross_check")
                                or oracle.get("expected_receipt_field_equalities"),
                                f"{case_id} has no cross-cutting obligation")

    def test_no_oracle_pins_an_opaque_value_or_repair_generation(self):
        for case_id, oracle in self.oracles():
            for action_id, fields in (oracle.get("expected_receipt_fields") or {}).items():
                for name, value in fields.items():
                    with self.subTest(case=case_id, field=f"{action_id}.{name}"):
                        self.assertNotEqual(name, "repair_generation")
                        if name in self.OPAQUE:
                            self.assertIsNone(value)

    def test_a_field_is_pinned_by_value_or_by_shape_but_never_both(self):
        for case_id, oracle in self.oracles():
            values = oracle.get("expected_receipt_fields") or {}
            shapes = oracle.get("expected_receipt_field_shapes") or {}
            for action_id, fields in shapes.items():
                overlap = set(fields) & set(values.get(action_id, {}))
                self.assertFalse(overlap, f"{case_id}.{action_id}: {sorted(overlap)}")

    def test_record_counts_are_only_pinned_on_unfiltered_views(self):
        for case_id, oracle in self.oracles():
            visible = json.loads((self.BUNDLE / f"{case_id}.json").read_text(encoding="utf-8"))
            by_id = {item["id"]: item for item in visible["allowed_actions"]}
            for action_id, contract in (oracle.get("expected_view_contract") or {}).items():
                argv = by_id.get(action_id, {}).get("argv") or []
                if contract.get("chain_from_anchor"):
                    self.assertNotIn("--after-sequence", argv, f"{case_id}.{action_id}")

    def test_every_case_still_runs_two_lower_model_turns(self):
        for case_id, _oracle in self.oracles():
            visible = json.loads((self.BUNDLE / f"{case_id}.json").read_text(encoding="utf-8"))
            self.assertLessEqual(len(visible["allowed_actions"]), 32)
            self.assertLessEqual(len(visible.get("initial_events") or []), 4)


if __name__ == "__main__":
    unittest.main()
