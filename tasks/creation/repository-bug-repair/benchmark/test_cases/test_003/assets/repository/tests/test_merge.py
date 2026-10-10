import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parents[1] / "src"))

from eventmerge import merge_events


def event(ts, event_id):
    return {"timestamp_ms": ts, "event_id": event_id, "payload": {"id": event_id}}


class MergeTests(unittest.TestCase):
    def test_order_and_duplicate(self):
        result = merge_events([[event(1, "a"), event(3, "c")], [event(2, "b"), event(4, "a")]])
        self.assertEqual([item["event_id"] for item in result], ["a", "b", "c"])

    def test_stable_ties(self):
        result = merge_events([[event(1, "left")], [event(1, "right")]])
        self.assertEqual([item["event_id"] for item in result], ["left", "right"])

    def test_rejects_unordered_source(self):
        with self.assertRaises(ValueError):
            merge_events([[event(2, "a"), event(1, "b")]])


if __name__ == "__main__":
    unittest.main()
