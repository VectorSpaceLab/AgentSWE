import tempfile, threading, unittest
from pathlib import Path
from tenantconfig import ConfigRepository, create_v1_database, create_v3_database
from tenantconfig.db import connect
from tenantconfig.migrate import migrate, rollback

class Tests(unittest.TestCase):
    def test_decimal_preservation_idempotence_compatibility_and_rollback(self):
        with tempfile.TemporaryDirectory() as tmp:
            path=Path(tmp)/"c.db"; create_v1_database(path,[{"tenant_id":"t","timeout":"2.3455","retries":4,"payload":{"label":"blue","x":[1]}}])
            migrate(path); self.assertEqual(ConfigRepository(path).load("t"),{"tenant_id":"t","timeout_ms":2346,"retries":4,"label":"blue","extension":{"x":[1]},"revision":1})
            self.assertEqual(migrate(path),0); rollback(path)
            self.assertEqual(ConfigRepository(path).load("t"),{"tenant_id":"t","timeout_ms":2346,"retries":4,"label":"blue","extension":{"x":[1]},"revision":0})
        with tempfile.TemporaryDirectory() as tmp:
            path=Path(tmp)/"v3.db"; create_v3_database(path); connection=connect(path)
            connection.execute("INSERT INTO tenant_settings VALUES (?,?,?,?,?,?)",("n",9,3,"v3",'{"z":2}',7)); connection.close()
            self.assertEqual(ConfigRepository(path).load("n")["revision"],7)
    def test_interrupted_table_version_combinations_recover(self):
        for version in (1,3):
            with self.subTest(version=version),tempfile.TemporaryDirectory() as tmp:
                path=Path(tmp)/"c.db"; create_v1_database(path,[{"tenant_id":"t","timeout":"1.5"}])
                connection=connect(path); connection.execute("ALTER TABLE tenant_settings RENAME TO tenant_settings_v1"); connection.execute(f"PRAGMA user_version={version}"); connection.close()
                migrate(path); self.assertEqual(ConfigRepository(path).load("t")["timeout_ms"],1500)
    def test_invalid_row_rolls_back_without_partial_schema(self):
        for value in ("NaN","Infinity","-1","bad"):
            with self.subTest(value=value),tempfile.TemporaryDirectory() as tmp:
                path=Path(tmp)/"c.db"; create_v1_database(path,[{"tenant_id":"ok","timeout":"1"},{"tenant_id":"bad","timeout":value}])
                with self.assertRaises(ValueError): migrate(path)
                connection=connect(path); tables={row[0] for row in connection.execute("SELECT name FROM sqlite_master WHERE type='table'")}
                self.assertEqual(connection.execute("PRAGMA user_version").fetchone()[0],1); self.assertIn("tenant_settings",tables); self.assertNotIn("migration_state",tables); connection.close()
                self.assertEqual(ConfigRepository(path).load("ok")["timeout_ms"],1000)
    def test_concurrent_migration_and_integrity(self):
        with tempfile.TemporaryDirectory() as tmp:
            path=Path(tmp)/"c.db"; create_v1_database(path,[{"tenant_id":str(i),"timeout":"0.0015"} for i in range(20)])
            barrier=threading.Barrier(4); errors=[]
            def run():
                try: barrier.wait(); migrate(path)
                except Exception as exc: errors.append(exc)
            threads=[threading.Thread(target=run) for _ in range(4)]; [thread.start() for thread in threads]; [thread.join(8) for thread in threads]
            self.assertTrue(all(not thread.is_alive() for thread in threads)); self.assertEqual(errors,[])
            connection=connect(path); self.assertEqual(connection.execute("PRAGMA integrity_check").fetchone()[0],"ok"); self.assertEqual(connection.execute("SELECT COUNT(*) FROM tenant_settings").fetchone()[0],20); connection.close()
if __name__ == "__main__": unittest.main(verbosity=2)
