"""Independent Candidates must not share one logical-request identity.

Provider-free. Reproduces the 2026-09-15 failure directly: two Candidates each run
dev_001 from the same pristine repository, so the lower agent's first request is
byte-identical. Before scoping, one transient upstream failure in Candidate 1 left
that identity permanently unknown and Candidate 2 got 409 forever, which killed the
run's dev case (evaluator_infrastructure_failure / exit 137).
"""
from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from lower_request_ledger import RequestLedger

BODY = {'model': 'deepseek-flash', 'input': [{'role': 'user', 'content': 'inspect the repository'}]}
ROUTE = '/v1/chat/completions'
URL = 'https://api.deepseek.com/v1'
SCOPE_A = 'a' * 64
SCOPE_B = 'b' * 64


class EvaluationScopeTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(); self.addCleanup(self.tmp.cleanup)
        self.ledger = RequestLedger(Path(self.tmp.name) / 'requests')
        self.addCleanup(self.ledger.close)

    def test_poisoned_candidate_does_not_block_the_next_candidate(self):
        # Candidate 1: identical first request, upstream fails with an unknown outcome.
        path_a, cached, error = self.ledger.claim(BODY, ROUTE, URL, evaluation_scope=SCOPE_A)
        self.assertIsNone(error)
        self.ledger.sent(path_a)
        self.ledger.fail(path_a, error='HTTPError', status=502, sent=True)
        # Same scope, same bytes: still exactly one upstream attempt, still poisoned.
        _, _, same_scope_error = self.ledger.claim(BODY, ROUTE, URL, evaluation_scope=SCOPE_A)
        self.assertEqual(same_scope_error, 'existing_request_pending_or_unknown')
        # Candidate 2 is an independent evaluation and must start clean.
        path_b, cached_b, error_b = self.ledger.claim(BODY, ROUTE, URL, evaluation_scope=SCOPE_B)
        self.assertIsNone(error_b, 'an independent Candidate must not inherit the poisoning')
        self.assertIsNone(cached_b)
        self.assertNotEqual(path_a, path_b)

    def test_single_upstream_still_holds_inside_one_scope(self):
        path, _, _ = self.ledger.claim(BODY, ROUTE, URL, evaluation_scope=SCOPE_A)
        self.ledger.sent(path)
        self.ledger.complete(path, payload=b'{"ok":1}', status=200,
                             content_type='application/json',
                             usage={'input_tokens': 1, 'output_tokens': 1, 'total_tokens': 2})
        again, cached, error = self.ledger.claim(BODY, ROUTE, URL, evaluation_scope=SCOPE_A)
        self.assertIsNone(error)
        self.assertEqual(again, path)
        self.assertEqual(cached['payload'], b'{"ok":1}', 'a completed response is served from cache')

    def test_scope_is_part_of_the_identity_not_a_side_channel(self):
        a, _, _ = self.ledger.claim(BODY, ROUTE, URL, evaluation_scope=SCOPE_A)
        b, _, _ = self.ledger.claim(BODY, ROUTE, URL, evaluation_scope=SCOPE_B)
        unscoped, _, _ = self.ledger.claim(BODY, ROUTE, URL)
        self.assertEqual(len({a.name, b.name, unscoped.name}), 3)
        self.assertTrue(all(len(p.name) == 64 for p in (a, b, unscoped)))

    def test_unscoped_behaviour_is_unchanged(self):
        path, _, error = self.ledger.claim(BODY, ROUTE, URL)
        self.assertIsNone(error)
        self.ledger.sent(path)
        self.ledger.fail(path, error='HTTPError', status=524, sent=True)
        _, _, again = self.ledger.claim(BODY, ROUTE, URL)
        self.assertEqual(again, 'existing_request_pending_or_unknown')

    def test_malformed_scope_fails_closed(self):
        for bad in ('', 'ZZZZ', 'abc', 'a' * 65, '../../etc', 'A' * 32):
            with self.assertRaises(ValueError):
                self.ledger.claim(BODY, ROUTE, URL, evaluation_scope=bad)


if __name__ == '__main__':
    unittest.main()
