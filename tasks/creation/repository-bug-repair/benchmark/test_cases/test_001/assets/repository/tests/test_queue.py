import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parents[1] / "src"))

from dispatchqueue import DispatchQueue
from dispatchqueue.consistency import audit_consistency


class QueueTests(unittest.TestCase):
    def test_sequential_retry(self):
        queue = DispatchQueue()
        self.assertTrue(queue.record_once("d-1", "w", {"x": 1}))
        self.assertFalse(queue.record_once("d-1", "w", {"x": 1}))
        self.assertEqual(len(queue.snapshot()), 1)

    def test_order_and_defensive_snapshot(self):
        queue = DispatchQueue()
        queue.record_once("a", "w", {})
        queue.record_once("b", "w", {})
        rows = queue.snapshot()
        self.assertEqual([row["delivery_id"] for row in rows], ["a", "b"])
        rows[0]["payload"]["changed"] = True
        self.assertEqual(queue.snapshot()[0]["payload"], {})

    def test_audit_matches_state(self):
        queue = DispatchQueue()
        queue.record_once("a", "w", {})
        self.assertTrue(audit_consistency(queue)["same_order"])


if __name__ == "__main__":
    unittest.main()
