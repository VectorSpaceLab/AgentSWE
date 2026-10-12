# Repair request: torn segment tails and pending manifests block restart

Repository: `assets/repository`

SegmentStore persists length-prefixed JSON records with CRC32 in binary segments and selects generations through `MANIFEST.json`; compaction stages `MANIFEST.next`. After abrupt termination, a partial final record makes all prior durable records unreadable. A complete pending manifest may also be discarded even when its segment is durable.

Implement deterministic recovery grounded in these formats. Ignore only a physically incomplete final record in the final active segment without rewriting it during `load`; any checksum failure, impossible complete length, malformed JSON, missing non-final segment, root-escaping segment name, or corruption before the tail raises `StoreCorruption` with path and byte offset. Recovery validates pending manifests and referenced segments, atomically commits a complete newer generation, rolls back an incomplete or stale generation, removes only proven stale temporary files, is idempotent, and preserves append/compaction ordering. A later append after recovery must not preserve torn bytes ahead of new data.

This is closed corpus. Only `src/segmentstore/` may change. Run public and binary crash-state, repeated restart, pending commit/rollback, corruption-boundary, append-after-recovery, and compaction probes. Deliver `migration_report.json` and an executable recovery self-test artifact using the required placeholders.

```repair_contract
{"schema_version":"1.0","repository":"assets/repository","allowed_paths":["src/segmentstore/"],"public_test_command":["{python}","-m","unittest","discover","-s","tests","-v"],"network":"closed","recovery":"required","recovery_format":"segmentstore-v1-manifest-segments","max_patch_bytes":2000000}
```
