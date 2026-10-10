"""Provider-free strict typed-frame regressions for merged OpenHands Codex JSONL."""
import unittest
from native_builder_evidence import NativeEvidenceError, events

class NativeBuilderEvidenceTests(unittest.TestCase):
    def test_pretty_multiline_tool_json_is_skipped_and_compact_native_is_retained(self):
        data = (b'{\n  "tool": "output"\n}\n'
                b'{"type":"thread.started","thread_id":"unit"}\n')
        self.assertEqual(events(data), [{"type":"thread.started", "thread_id":"unit"}])
    def test_malformed_compact_typed_frame_fails_closed(self):
        with self.assertRaisesRegex(NativeEvidenceError, "malformed native JSON event"): events(b'{"type":\n')
    def test_non_string_type_fails_closed(self):
        with self.assertRaisesRegex(NativeEvidenceError, "lacks a string type"): events(b'{"type":1}\n')
    def test_null_type_fails_closed(self):
        with self.assertRaisesRegex(NativeEvidenceError, "lacks a string type"): events(b'{"type":null}\n')
    def test_indented_typed_and_compact_untyped_json_are_not_promoted(self):
        data = b'  {"type":"thread.started"}\n{"status":200,"payload":{"type":"turn.completed"}}\n'
        self.assertEqual(events(data), [])
    def test_live_incomplete_native_frame_is_deferred(self):
        self.assertEqual(events(b'{"type":"thread.started"', live=True), [])

if __name__ == "__main__": unittest.main()
