# Repair request: WireBatch loses frames at arbitrary network boundaries

Repository: `assets/repository`
Pinned authoritative profile: `assets/WIREBATCH_PROTOCOL_V1.md`

`FrameParser.feed(bytes)` must parse the supplied streaming protocol across arbitrary chunk boundaries and emit zero or more frames. Complete-frame tests pass, but the same bytes can disappear or be misclassified when delivered in small or adversarially placed chunks. Repair the incremental behavior across buffer, headers, parser state, limits, checksum, and finish behavior.

Conform exactly to the pinned profile: multiple frames per feed; CRLF boundaries split byte-by-byte; exact payload length including binary bytes; required and duplicate headers; case-insensitive names; UTF-8 values; optional SHA-256; header/payload/buffer limits; deterministic `ProtocolError.code`; and no partial frame emission. `finish()` succeeds only at a frame boundary. This is closed corpus: make zero search or page calls. Run public and focused conformance probes.

Only `src/wirebatch/` may change; do not edit the profile.

```repair_contract
{"schema_version":"1.0","repository":"assets/repository","allowed_paths":["src/wirebatch/"],"public_test_command":["{python}","-m","unittest","discover","-s","tests","-v"],"network":"closed","recovery":"none","max_patch_bytes":2000000}
```
