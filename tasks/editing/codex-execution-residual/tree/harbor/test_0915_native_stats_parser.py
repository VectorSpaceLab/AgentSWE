"""Provider-free regression tests for native Builder stats parsing."""
import json
import tempfile
import importlib.util
import unittest
from pathlib import Path

_SPEC = importlib.util.spec_from_file_location(
    "direct_harbor_builder", Path(__file__).with_name("direct_harbor_builder.py"))
direct = importlib.util.module_from_spec(_SPEC)
assert _SPEC.loader is not None
_SPEC.loader.exec_module(direct)


class NativeStatsParserTests(unittest.TestCase):
    def test_scalar_json_events_are_ignored_without_breaking_object_events(self):
        with tempfile.TemporaryDirectory() as temp:
            run = Path(temp)
            stream = run / "jobs" / "native" / "agent"
            stream.mkdir(parents=True)
            (stream / "codex.txt").write_text(
                "1\n"
                + json.dumps({"type": "thread.started", "thread_id": "thread-1"}) + "\n"
                + "null\n"
                + "[]\n"
                + json.dumps({"type": "turn.completed", "usage": {"input_tokens": 3}}) + "\n"
            )
            stats = direct.native_stats(run)
        self.assertEqual(stats["native_sessions"], ["thread-1"])
        self.assertEqual(stats["native_completed_turns"], 1)
        self.assertEqual(stats["native_reported_usage"], [{"input_tokens": 3}])

    def test_malformed_json_remains_ignored(self):
        with tempfile.TemporaryDirectory() as temp:
            run = Path(temp)
            stream = run / "jobs" / "native" / "agent"
            stream.mkdir(parents=True)
            (stream / "codex.txt").write_text("not-json\n")
            stats = direct.native_stats(run)
        self.assertEqual(stats["native_sessions"], [])
        self.assertEqual(stats["native_completed_turns"], 0)


if __name__ == "__main__":
    unittest.main()
