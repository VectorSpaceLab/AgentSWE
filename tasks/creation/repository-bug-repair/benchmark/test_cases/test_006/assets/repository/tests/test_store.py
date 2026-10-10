import tempfile
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parents[1] / "src"))

from segmentstore import SegmentStore


class StoreTests(unittest.TestCase):
    def test_append_and_load(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = SegmentStore(tmp)
            store.append({"seq": 1})
            store.append({"seq": 2})
            self.assertEqual(store.load(), [{"seq": 1}, {"seq": 2}])

    def test_compaction_preserves_records(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = SegmentStore(tmp)
            store.append({"seq": 1})
            store.compact()
            self.assertEqual(store.load(), [{"seq": 1}])

    def test_clean_recovery(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = SegmentStore(tmp)
            store.initialize()
            self.assertEqual(store.recover(), "clean")


if __name__ == "__main__":
    unittest.main()
