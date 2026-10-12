"""The Optimization responses/BrowseComp brokers read effort settings the same way as the shared
agentswe_broker.config.normalize_effort (they run in sidecars without that package, so they carry a copy)."""
import importlib.util
import sys
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "broker"))
from agentswe_broker.config import normalize_effort  # noqa: E402


def load(rel):
    spec = importlib.util.spec_from_file_location(rel.replace("/", "_"), REPO / rel)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


SPELLINGS = [None, "", " ", "none", "NONE", "off", "unset", "explicit-none", " Explicit-None ", "low", "medium",
             "high", "max", "minimal"]


class EffortMapping(unittest.TestCase):
    def test_brokers_match_shared_reader(self):
        for rel in ("runners/optimization/optimization_native/responses_broker.py",
                    "runners/optimization/optimization_native/browsecomp_broker.py"):
            module = load(rel)
            for value in SPELLINGS:
                with self.subTest(broker=rel, value=value):
                    self.assertEqual(module._effort_field(value), normalize_effort(value))

    def test_explicit_none_is_sent_as_none_and_none_is_omitted(self):
        module = load("runners/optimization/optimization_native/responses_broker.py")
        self.assertEqual(module._effort_field("explicit-none"), "none")
        self.assertIsNone(module._effort_field("none"))


if __name__ == "__main__":
    unittest.main()
