import tempfile, unittest
from pathlib import Path
from sessionarchive.codec import encode_record
from sessionarchive.migrate import migrate_record
from sessionarchive.store import SessionArchive

class Tests(unittest.TestCase):
    def test_versions_decimal_extensions_and_v2_idempotence(self):
        for version in (None, 1, "1"):
            raw={"session_id":"s","user_id":"u","expires":"2.5005","metadata":{"r":1},"x":[3]}
            if version is not None: raw["version"]=version
            migrated=migrate_record(raw)
            self.assertEqual(migrated["expires_at_ms"],2501); self.assertEqual(migrated["x"],[3])
            self.assertEqual(migrate_record(migrated),migrated)
        v2={"version":2,"session_id":"v2","user_id":"u","expires_at_ms":7,"extra":{"k":[1]}}
        self.assertEqual(migrate_record(v2),v2)
    def test_invalid_expiry_and_versions_fail(self):
        for value in (True,-1,"-0.1","NaN","Infinity","bad"):
            with self.subTest(value=value),self.assertRaises(ValueError):
                migrate_record({"version":1,"session_id":"s","user_id":"u","expires":value})
        with self.assertRaises(ValueError): migrate_record({"version":"2","session_id":"s","user_id":"u","expires_at_ms":1})
    def test_mixed_file_retry_preserves_original_rollback_copy(self):
        with tempfile.TemporaryDirectory() as tmp:
            path=Path(tmp)/"s.jsonl"
            rows=[{"version":"1","session_id":"a","user_id":"u","expires":"1.25"},{"version":2,"session_id":"b","user_id":"u","expires_at_ms":7,"extra":True}]
            original=b"".join(encode_record(row) for row in rows); path.write_bytes(original)
            path.with_name(path.name+".tmp").write_bytes(b"incomplete")
            archive=SessionArchive(path); archive.migrate_file(); first=path.read_bytes(); archive.migrate_file()
            self.assertEqual(path.read_bytes(),first); self.assertEqual([row["expires_at_ms"] for row in archive.load_all()],[1250,7])
            backups=list(path.parent.glob(path.name+".bak*")); self.assertTrue(backups)
            self.assertIn(original,[backup.read_bytes() for backup in backups])
            self.assertFalse(path.with_name(path.name+".tmp").exists())
    def test_complete_temporary_state_is_retryable(self):
        with tempfile.TemporaryDirectory() as tmp:
            path=Path(tmp)/"s.jsonl"; path.write_bytes(encode_record({"version":1,"session_id":"a","user_id":"u","expires":1}))
            path.with_name(path.name+".tmp").write_bytes(encode_record({"version":2,"session_id":"a","user_id":"u","expires_at_ms":1000}))
            SessionArchive(path).migrate_file()
            self.assertEqual(SessionArchive(path).load_all()[0]["expires_at_ms"],1000)
            self.assertFalse(path.with_name(path.name+".tmp").exists())
if __name__ == "__main__": unittest.main(verbosity=2)
