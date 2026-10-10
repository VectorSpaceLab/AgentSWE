"""Strict typed-envelope regressions for merged native Codex JSONL."""
import unittest

from native_builder_evidence import NativeEvidenceError, events


class NativeBuilderEvidenceTests(unittest.TestCase):
    def test_pretty_tool_json_is_skipped_but_compact_native_event_is_retained(self):
        data = (b'{\n  "tool": "output"\n}\n'
                b'{"type":"thread.started","thread_id":"unit"}\n')
        self.assertEqual(events(data), [
            {"type": "thread.started", "thread_id": "unit"},
        ])

    def test_malformed_compact_typed_frame_is_rejected(self):
        with self.assertRaisesRegex(NativeEvidenceError, "malformed native JSON event"):
            events(b'{"type":\n')

    def test_non_string_type_is_rejected(self):
        with self.assertRaisesRegex(NativeEvidenceError, "lacks a string type"):
            events(b'{"type":1}\n')

    def test_indented_typed_json_is_not_promoted_to_native_evidence(self):
        self.assertEqual(events(b'  {"type":"thread.started"}\n'), [])


if __name__ == "__main__":
    unittest.main()
