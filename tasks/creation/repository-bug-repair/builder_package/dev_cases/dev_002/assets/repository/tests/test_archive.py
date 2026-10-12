import tempfile
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parents[1] / "src"))

from sessionarchive.codec import encode_record
from sessionarchive.store import SessionArchive


class ArchiveTests(unittest.TestCase):
    def test_unmarked_legacy_record_loads(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "sessions.jsonl"
            path.write_bytes(encode_record({"session_id": "s", "user_id": "u", "expires": 2}))
            self.assertEqual(SessionArchive(path).load_all()[0]["expires_at_ms"], 2000)

    def test_v2_round_trip(self):
        with tempfile.TemporaryDirectory() as tmp:
            archive = SessionArchive(Path(tmp) / "sessions.jsonl")
            archive.append_v2({"version": 2, "session_id": "s", "user_id": "u", "expires_at_ms": 5})
            self.assertEqual(archive.load_all()[0]["expires_at_ms"], 5)

    def test_unsupported_version(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "sessions.jsonl"
            path.write_bytes(encode_record({"version": 8}))
            with self.assertRaises(ValueError):
                SessionArchive(path).load_all()


if __name__ == "__main__":
    unittest.main()
