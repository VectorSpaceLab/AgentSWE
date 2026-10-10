import tempfile
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parents[1] / "src"))

from tenantconfig import ConfigRepository, create_v1_database, create_v3_database


class RepositoryTests(unittest.TestCase):
    def test_v1_integer_timeout_is_readable(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "config.db"
            create_v1_database(path, [{"tenant_id": "t", "timeout": 2}])
            self.assertEqual(ConfigRepository(path).load("t")["timeout_ms"], 2000)

    def test_empty_v3_schema(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "config.db"
            create_v3_database(path)
            self.assertIn("timeout_ms", ConfigRepository(path).schema_columns())

    def test_missing_tenant(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "config.db"
            create_v1_database(path, [])
            with self.assertRaises(KeyError):
                ConfigRepository(path).load("missing")


if __name__ == "__main__":
    unittest.main()
