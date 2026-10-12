# WireBatch Protocol v1 — pinned benchmark profile

This synthetic profile is authoritative for the case and is released as CC0-1.0.

1. A frame is `LEN <n>\r\n`, followed by zero or more `Name: value\r\n` lines, a blank `\r\n`, exactly `n` payload bytes, and a final `\r\n`.
2. `n` is one or more ASCII decimal digits, has no sign or whitespace, and is at most the configured payload limit.
3. Header names are nonempty ASCII letters, digits, and hyphens; matching is ASCII case-insensitive. Values are UTF-8 with surrounding spaces/tabs removed. Duplicate names are invalid.
4. `Content-Type` is required and nonempty. `Checksum-SHA256`, when present, is exactly 64 hexadecimal characters and must match the payload.
5. The parser is incremental: every octet boundary is legal, including between CR and LF, within UTF-8, within the length, and between payload and terminator. No incomplete frame is emitted.
6. The complete header block, payload, and total retained buffer must remain within configured limits. Limit failure is deterministic and does not emit a frame.
7. Error codes are `length_line`, `length_value`, `header_syntax`, `header_name`, `header_encoding`, `duplicate_header`, `missing_header`, `payload_limit`, `header_limit`, `buffer_limit`, `checksum`, `frame_terminator`, and `truncated_frame`.
8. `finish()` succeeds only with no pending frame and no retained bytes. A parser in an error state need not resume.
