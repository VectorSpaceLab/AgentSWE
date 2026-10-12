"""2026-09-21: the one finish-accountability re-ask in the lower action loop.

Evaluator-owned self-tests. No provider call, no container, no Candidate: the
extra request is stubbed, and the obligation extraction runs on this tree's own
published case task files.
"""
from __future__ import annotations

from pathlib import Path
import sys
import unittest

ROOT = Path(__file__).resolve().parents[2]
for extra in (str(ROOT), str(ROOT / "agentloop"), str(ROOT / "evaluator")):
    if extra not in sys.path:
        sys.path.insert(0, extra)

import lower_agent_launcher as launcher  # noqa: E402
import lower_request_identity as identity  # noqa: E402

CASES = ("test_001", "test_002", "test_003", "test_004", "test_005", "test_006")
FINISH = {"kind": "finish", "operation": None, "rationale": "observed state is sufficient",
          "response_text_sha256": "a" * 64}


def task_text(case_id: str) -> str:
    return (ROOT / "agentloop" / "cases" / case_id / "task.md").read_text(encoding="utf-8")


class ObligationExtractionTests(unittest.TestCase):
    @staticmethod
    def unwrapped(value: str) -> str:
        """The markdown hard wrap removed, and nothing else.

        A line ending in '-' is one word split across the wrap, so it rejoins
        without a space; every other wrap is one space. The quoted sentence must
        be the task's own bytes under exactly this normalisation and no other.
        """
        return " ".join(value.split()).replace("- ", "-")

    def test_every_quoted_sentence_comes_from_the_case_task(self):
        for case_id in CASES:
            text = self.unwrapped(task_text(case_id))
            sentences = launcher.obligation_sentences(task_text(case_id))
            self.assertGreaterEqual(len(sentences), 6, case_id)
            for sentence in sentences:
                self.assertIn(self.unwrapped(sentence), text, (case_id, sentence))

    def test_no_evaluator_transition_name_can_reach_the_quoted_text(self):
        import case_world  # noqa: F401  (import proves the module is loadable)
        for case_id in CASES:
            world = ROOT / "agentloop" / "cases" / case_id
            self.assertTrue(world.is_dir())
            for sentence in launcher.obligation_sentences(task_text(case_id)):
                self.assertNotIn("advanced", sentence.lower())
                self.assertNotIn("operation_relevant", sentence.lower())
                self.assertNotIn("durable_committed", sentence.lower())

    def test_a_decimal_is_not_a_sentence_boundary(self):
        sentences = launcher.obligation_sentences(
            "Preserve a replay tolerance of 1.2 and a deviation of 0.5 in the report.")
        self.assertEqual(len(sentences), 1)
        self.assertIn("1.2", sentences[0])
        self.assertIn("0.5", sentences[0])

    def test_coverage_marks_an_operation_that_was_run(self):
        rows = launcher.obligation_coverage(
            "Prepare the release and then commit it.\n\nNever rewrite published bytes.",
            ["prepare"])
        prepared = [row for row in rows if "Prepare" in row["sentence"]]
        self.assertTrue(prepared and prepared[0]["attempted"])


class ReAskTests(unittest.TestCase):
    def setUp(self):
        self.original = launcher._request_model_json
        self.addCleanup(setattr, launcher, "_request_model_json", self.original)

    def stub(self, answer, response_hash="b" * 64):
        launcher._request_model_json = lambda *a, **k: (answer, response_hash)

    def account(self, trajectory=None, case_id="test_006"):
        return launcher.account_for_finish(
            "http://broker.invalid/v1/responses", task_text(case_id), {},
            trajectory if trajectory is not None else [{"operation": "prepare"}, {"operation": "verify"}],
            3, dict(FINISH))

    def test_the_re_ask_can_continue_the_loop_with_one_governed_action(self):
        self.stub({"kind": "act", "operation": "cancel", "rationale": "the cancelled run was never released",
                   "obligation_account": [{"obligation": 1, "settled_by": None,
                                           "unresolved": "no cancellation receipt exists"}]})
        decision = self.account()
        self.assertEqual(decision["kind"], "act")
        self.assertEqual(decision["operation"], "cancel")
        record = decision["finish_accountability"]
        self.assertEqual(record["outcome"], "continued_with_action")
        self.assertTrue(record["re_asked"])
        self.assertTrue(record["model_account"])

    def test_a_confirmed_finish_is_always_accepted(self):
        self.stub({"kind": "finish", "operation": None,
                   "rationale": "I could not settle the cancellation branch"})
        decision = self.account()
        self.assertEqual(decision["kind"], "finish")
        self.assertEqual(decision["finish_accountability"]["outcome"], "finish_confirmed")

    def test_a_failing_re_ask_keeps_the_original_finish(self):
        def boom(*a, **k):
            raise launcher.ModelContentError("response is not strict JSON")
        launcher._request_model_json = boom
        decision = self.account()
        self.assertEqual(decision["kind"], "finish")
        self.assertEqual(decision["rationale"], FINISH["rationale"])
        self.assertEqual(decision["response_text_sha256"], FINISH["response_text_sha256"])
        record = decision["finish_accountability"]
        self.assertEqual(record["outcome"], "original_finish_kept")
        self.assertIn("ModelContentError", record["skipped_reason"])

    def test_an_unsupported_operation_falls_back_instead_of_failing_the_case(self):
        self.stub({"kind": "act", "operation": "sudo", "rationale": "x"})
        decision = self.account()
        self.assertEqual(decision["kind"], "finish")
        self.assertEqual(decision["finish_accountability"]["outcome"], "original_finish_kept")

    def test_a_malformed_answer_falls_back(self):
        self.stub({"kind": "act", "operation": "cancel", "rationale": "   "})
        self.assertEqual(self.account()["kind"], "finish")

    def test_the_prompt_quotes_the_task_and_leaks_no_evaluator_state(self):
        captured = {}

        def capture(endpoint, prompt, label, *a, **k):
            captured["prompt"] = prompt
            return {"kind": "finish", "operation": None, "rationale": "stop"}, "c" * 64
        launcher._request_model_json = capture
        self.account()
        prompt = captured["prompt"]
        self.assertIn("quoted", prompt)
        self.assertIn("cancelled run followed by", " ".join(prompt.split()))
        for forbidden in ("operation_relevant", "advanced", "durable_committed",
                          "case_world", "allowed_operations", "transition"):
            self.assertNotIn(forbidden, prompt)

    def test_the_action_budget_is_not_enlarged(self):
        self.assertEqual(launcher.MAX_ACTION_STEPS, 10)

    def test_the_loop_offers_the_re_ask_only_while_an_action_could_follow(self):
        source = (ROOT / "agentloop" / "lower_agent_launcher.py").read_text(encoding="utf-8")
        self.assertIn("accountability_allowed=(finish_accountability is None", source)
        self.assertIn("and sequence < MAX_ACTION_STEPS)", source)

    def test_it_is_dispatched_under_the_existing_action_loop_reserve(self):
        source = (ROOT / "agentloop" / "lower_agent_launcher.py").read_text(encoding="utf-8")
        body = source.split("def account_for_finish", 1)[1].split("\ndef ", 1)[0]
        self.assertEqual(body.count("reserve=LOWER_ACTION_LOOP_RESERVE_SECONDS"), 2)


class AccountabilitySlotIdentityTests(unittest.TestCase):
    """D104 (2026-09-21): the re-ask must be a signable, reserve-guarded slot.

    Until this patch the signed-identity grammar
    (lower_request_identity.py:119) accepted only ``action:[1-8]`` and
    ``authoring:1``.  ``account_for_finish`` signs ``accountability:<step>``
    inside a try whose except tuple contains ValueError, so every re-ask was
    recorded as ``skipped_reason: "ValueError: invalid request slot"`` and the
    feature never ran once (72 such records in this benchmark's own
    ai-scientist evidence).  The same gap on ``action:9``/``action:10`` raises
    outside the action loop's except tuples and kills the case
    (0921b-gateway-r-010 dev_001).
    """

    KEY = b"d104-fixture-signing-key-32bytes"
    PRODUCT = "a" * 64
    CASE_DIGEST = "b" * 64
    PAYLOAD = {"model": "candidate-requested-value-is-overridden", "input": "p", "stream": False}

    def sign(self, slot):
        return identity.sign_request(self.KEY, self.PRODUCT, "dev_001", self.CASE_DIGEST,
                                     slot, self.PAYLOAD)

    def test_the_slot_grammar_mirrors_the_published_action_budget(self):
        self.assertEqual(identity.ACTION_SLOT_STEPS, launcher.MAX_ACTION_STEPS)

    def test_every_slot_the_loop_can_emit_signs_and_verifies(self):
        wire = identity.normalized_payload(self.PAYLOAD)
        slots = [*(f"action:{step}" for step in range(1, launcher.MAX_ACTION_STEPS + 1)),
                 *(f"accountability:{step}" for step in range(1, launcher.MAX_ACTION_STEPS + 1)),
                 "authoring:1"]
        for slot in slots:
            verified = identity.verify_request(self.sign(slot), self.KEY, wire)
            self.assertEqual(verified["request_slot"], slot)
            self.assertEqual(verified["request_fingerprint"],
                             identity.fingerprint("dev_001", self.CASE_DIGEST, slot))
        self.assertEqual(len({identity.fingerprint("dev_001", self.CASE_DIGEST, s) for s in slots}),
                         len(slots))

    def test_the_grammar_stays_closed(self):
        for slot in ("action:0", f"action:{launcher.MAX_ACTION_STEPS + 1}", "accountability:0",
                     f"accountability:{launcher.MAX_ACTION_STEPS + 1}", "accountability",
                     "action:", "authoring:0", "authoring:2", "action:1 ", "/tmp/new-context", ""):
            with self.assertRaises(ValueError):
                self.sign(slot)

    def test_the_re_ask_slot_is_the_one_the_launcher_signs(self):
        source = (ROOT / "agentloop" / "lower_agent_launcher.py").read_text(encoding="utf-8")
        self.assertIn('request_slot=f"accountability:{step}"', source)
        self.assertIn('dispatch_guard_decision("finish accountability", f"accountability:{step}"', source)


if __name__ == "__main__":
    unittest.main()
