"""The shared formal finalizer reconciles the Result judge broker with the judged cases, counting the single
early-stop resample that valid_result_contract accepts (stdlib unittest).

Before this, a resampled case (logical_requests 2 with early_stop_resample recorded) was left out of the transport
count and the successful-call count expected one call per case, so the finalizer refused the run ("successful-call
delta 6 does not match judged case count 5", "request delta 6 does not match transport request count 4")."""
from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "runners" / "editing" / "control"))

import formal_axes_shared as fas  # noqa: E402


def contract(case_id, *, logical=1, transport=1, resample=False):
    value = {"schema_version": "agentswe-edit-result-score-contract/v1", "case_id": case_id,
             "contract_valid": True, "result_score_publishable": True, "result_score": 20,
             "judge": {"model": "deepseek-flash", "reasoning_effort": "max"},
             "provider_usage": {"logical_requests": logical, "transport_attempts": transport,
                                "completed_responses": logical}}
    if resample:
        value["early_stop_resample"] = {"attempt": 1, "reason": "unparseable answer"}
    return value


# OpenWiki r3's five freshly judged cases: test_001 resampled once; test_004 was a Candidate-zero (never judged).
R3 = [contract("test_001", logical=2, transport=2, resample=True)] + [
    contract(case) for case in ("test_002", "test_003", "test_005", "test_006")]


def reconcile(contracts, successful_delta, logical_delta):
    footprints = [fas.judge_call_footprint(c) for c in contracts]
    attempted_transport = sum(f[0] for f in footprints if f is not None)
    expected_successful = sum(fas._expected_requests(c) for c in contracts)
    return fas.broker_reconciliation_reasons(
        successful_delta, logical_delta, judged_cases=len(contracts),
        expected_successful_calls=expected_successful, attempted_transport_requests=attempted_transport)


class ResampleAccounting(unittest.TestCase):
    def test_footprints(self):
        self.assertEqual(fas.judge_call_footprint(contract("t")), (1, 1))
        self.assertEqual(fas.judge_call_footprint(contract("t", logical=2, transport=2, resample=True)), (2, 2))
        self.assertEqual(fas.judge_call_footprint(contract("t", transport=2)), (2, 1))  # outer transport retry
        self.assertIsNone(fas.judge_call_footprint(contract("t", logical=2, transport=2)))  # extra call, no resample
        self.assertIsNone(fas.judge_call_footprint({"case_id": "t"}))

    def test_openwiki_r3_reconciles(self):
        # broker before 10 / after 16 calls, all successful: delta 6 and 6
        self.assertEqual(reconcile(R3, successful_delta=6, logical_delta=6), [])

    def test_unaccounted_call_still_refuses(self):
        reasons = reconcile(R3, successful_delta=7, logical_delta=7)
        self.assertEqual(len(reasons), 2)
        self.assertIn("successful-call delta 7", reasons[0])
        self.assertIn("request delta 7 does not match transport request count 6", reasons[1])

    def test_no_resample_path_unchanged(self):
        plain = [contract(case) for case in ("test_001", "test_002", "test_003")]
        self.assertEqual(reconcile(plain, successful_delta=3, logical_delta=3), [])
        self.assertEqual(len(reconcile(plain, successful_delta=4, logical_delta=4)), 2)

    def test_two_requests_without_recorded_resample_are_refused(self):
        bad = contract("test_001", logical=2, transport=2)
        reasons = reconcile([bad], successful_delta=2, logical_delta=2)
        self.assertTrue(any("request delta 2 does not match transport request count 0" in r for r in reasons))
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "result_score_contract.json"
            path.write_text(json.dumps(bad))
            _, errors = fas.valid_result_contract(path, "test_001")
        self.assertIn("Result judge logical_requests must equal 1, or 2 with a recorded early_stop_resample", errors)

    def test_earlier_reasons_suppress_only_the_successful_check(self):
        reasons = fas.broker_reconciliation_reasons(9, 6, judged_cases=5, expected_successful_calls=6,
                                                    attempted_transport_requests=6, earlier_reasons=True)
        self.assertEqual(reasons, [])


if __name__ == "__main__":
    unittest.main()
