#!/usr/bin/env python3
"""Generate case requests and the pinned WireBatch protocol profile."""

from pathlib import Path
from textwrap import dedent

ROOT = Path(__file__).resolve().parents[1]

CASES = {
"dev_cases/dev_001/input.md": r'''
# Repair request: normalized route changes reuse a stale quote

Repository: `assets/repository`

ParcelRoute 2.4 is used by a checkout service. In one long-lived process, quoting the same account and parcel for `EU` economy and then `APAC` priority can return the first quote's destination, service level, and amount. Equivalent spelling such as `" eu "` must share the canonical `EU` cache entry, but materially different pricing inputs and policy revisions must never alias. Existing tests cover only repeated identical requests.

Trace request normalization, cache identity, pricing policy, service orchestration, and serialization. Preserve `QuoteService.quote(...)`, `quote_request(...)`, the dictionary keys, arbitrary nonempty destination strings, LRU behavior, and defensive result dictionaries. A policy replacement with a different revision must not reuse an old quote. Run the public suite and focused warm-cache probes.

Only `src/parcelroute/` may change. Do not modify tests or project files and do not add dependencies.

```repair_contract
{"schema_version":"1.0","repository":"assets/repository","allowed_paths":["src/parcelroute/"],"public_test_command":["{python}","-m","unittest","discover","-s","tests","-v"],"network":"local_only","recovery":"none","max_patch_bytes":2000000}
```
''',
"dev_cases/dev_002/input.md": r'''
# Repair request: rolling session migration rejects JSON string versions

Repository: `assets/repository`

SessionArchive stores one JSON object per line. Version 1 records may omit `version` or use integer `1` or JSON string `"1"`; `expires` may be an integer, decimal number, or decimal string in seconds. Version 2 uses nonnegative integer `expires_at_ms`. A rolling upgrade now rejects string-version rows, and the existing whole-file migration can destroy the only usable copy if interrupted.

Repair reads and implement an idempotent migration/recovery artifact grounded in this JSONL format. It must preserve unknown fields and metadata, retain v2 records byte-for-byte as Python dictionaries during repeated record migration, atomically install a complete migrated archive, retain an executable rollback copy, recover deterministically from stale or complete temporary state, and allow safe retry. Unsupported versions and malformed/nonfinite/negative expiry values raise `ValueError`. `SessionArchive.load_all()` keeps its public output shape.

Run the public suite and focused mixed-generation, interruption, retry, and rollback probes. Only `src/sessionarchive/` may change. Deliver `migration_report.json` and a Python artifact beneath that prefix. The command must use the required `{python}`, `{artifact}`, and `{work}` placeholders and print the recovery-probe JSON contract described in the builder interface.

```repair_contract
{"schema_version":"1.0","repository":"assets/repository","allowed_paths":["src/sessionarchive/"],"public_test_command":["{python}","-m","unittest","discover","-s","tests","-v"],"network":"local_only","recovery":"required","recovery_format":"sessionarchive-jsonl-v1-v2","max_patch_bytes":2000000}
```
''',
"test_cases/test_001/input.md": r'''
# Repair request: concurrent delivery retries duplicate state and audit rows

Repository: `assets/repository`

`DispatchQueue.record_once(delivery_id, worker, payload)` is documented as linearizable and idempotent for threads sharing one queue. Under synchronized retries, multiple calls return `True`, state contains duplicate IDs, audit replay contains duplicate rows, and sequence numbers can become inconsistent. Sequential tests pass. Distinct delivery IDs must continue making progress concurrently; do not hold one global lock across validation, serialization, or journal I/O.

Repair the state/service/journal boundary. Exactly one duplicate caller returns `True`; state and audit remain in identical insertion order; committed sequences are contiguous; snapshot payloads are defensive; replay reconstructs an equivalent queue; a failed journal append must release any reservation without publishing partial state; no spin, sleep, probabilistic retry, or dependency is allowed. Run repeated concurrent probes and the public suite.

Only `src/dispatchqueue/` may change.

```repair_contract
{"schema_version":"1.0","repository":"assets/repository","allowed_paths":["src/dispatchqueue/"],"public_test_command":["{python}","-m","unittest","discover","-s","tests","-v"],"network":"local_only","recovery":"none","max_patch_bytes":2000000}
```
''',
"test_cases/test_002/input.md": r'''
# Repair request: interrupted SQLite migration advertises the wrong schema

Repository: `assets/repository`

TenantConfig uses SQLite `PRAGMA user_version=1` with `tenant_settings(tenant_id, timeout_seconds, retries, payload_json)` and version 3 with integer `timeout_ms`, `label`, `extension_json`, `revision`, plus migration state. The current migration advances the version before durable replacement and rejects decimal seconds. An interruption can leave `user_version=3` with only the renamed v1 table.

Implement a transactionally safe, idempotent v1-to-v3 migration and executable recovery/rollback probe using the actual SQLite format. Legacy timeouts accept finite nonnegative integer, float, or decimal string seconds and round half-up to milliseconds; booleans, negative/nonfinite/malformed values fail with `ValueError`. Preserve tenant rows, retries, label, all other payload keys, compatibility reads during rolling deployment, and a rollback path to a readable v1 database. Recover all plausible partially migrated table/version combinations without data loss, support repeated/concurrent invocation, and leave `PRAGMA integrity_check` clean.

Only `src/tenantconfig/` may change. Run the public suite and migration, injected-interruption, retry, concurrent invocation, compatibility, integrity, and rollback probes. Deliver the conditional migration report and executable probe artifact.

```repair_contract
{"schema_version":"1.0","repository":"assets/repository","allowed_paths":["src/tenantconfig/"],"public_test_command":["{python}","-m","unittest","discover","-s","tests","-v"],"network":"local_only","recovery":"required","recovery_format":"sqlite-tenantconfig-v1-v3","max_patch_bytes":2000000}
```
''',
"test_cases/test_003/input.md": r'''
# Repair request: stream merge is slow and folds distinct event IDs

Repository: `assets/repository`

EventMerge combines individually timestamp-ordered finite iterables. Output is globally ordered by `(timestamp_ms, source_index, source_offset)`, retains the first occurrence of each exact case-sensitive `event_id`, and consumes each source once. Correctness tests miss case variants, and a recent large-fan-in workload regressed from interactive latency to tens of seconds with excessive memory use.

Repair planning, identity, and merge orchestration without materializing source iterables or sharing state across calls. On this environment, eight generators totaling 80,000 unique events must finish in under 2.0 seconds and peak below 120 MiB. Preserve stable ties, arbitrary Unicode IDs and payloads, empty source lists, input validation, and detection of an out-of-order source. Do not use probabilistic membership or dependencies. Run the public suite and a measured focused benchmark.

Only `src/eventmerge/` may change.

```repair_contract
{"schema_version":"1.0","repository":"assets/repository","allowed_paths":["src/eventmerge/"],"public_test_command":["{python}","-m","unittest","discover","-s","tests","-v"],"network":"local_only","recovery":"none","max_patch_bytes":2000000}
```
''',
"test_cases/test_004/input.md": r'''
# Repair request: WireBatch loses frames at arbitrary network boundaries

Repository: `assets/repository`
Pinned authoritative profile: `assets/WIREBATCH_PROTOCOL_V1.md`

`FrameParser.feed(bytes)` must parse the supplied streaming protocol across arbitrary chunk boundaries and emit zero or more frames. Complete-frame tests pass, but the same bytes can disappear or be misclassified when delivered in small or adversarially placed chunks. Repair the incremental behavior across buffer, headers, parser state, limits, checksum, and finish behavior.

Conform exactly to the pinned profile: multiple frames per feed; CRLF boundaries split byte-by-byte; exact payload length including binary bytes; required and duplicate headers; case-insensitive names; UTF-8 values; optional SHA-256; header/payload/buffer limits; deterministic `ProtocolError.code`; and no partial frame emission. `finish()` succeeds only at a frame boundary. This is closed corpus: make zero search or page calls. Run public and focused conformance probes.

Only `src/wirebatch/` may change; do not edit the profile.

```repair_contract
{"schema_version":"1.0","repository":"assets/repository","allowed_paths":["src/wirebatch/"],"public_test_command":["{python}","-m","unittest","discover","-s","tests","-v"],"network":"closed","recovery":"none","max_patch_bytes":2000000}
```
''',
"test_cases/test_005/input.md": r'''
# Repair request: tenant-scoped profile writes leave positive and negative cache entries stale

Repository: `assets/repository`

ProfileDirectory combines a revisioned repository, positive/negative cache, event bus, and service. After a warm read, `update_user` returns the new value but `get_user` can return the old profile. A cached miss can also remain `None` after `create_user`, and same-named users in different tenants make the failure inconsistent.

Repair coherence across model, repository, cache, event, service, and batch boundaries. Invalidation is exact to `(tenant_id,user_id)`; same-named users in other tenants remain warm and unchanged; cached misses are invalidated by create; returned nested values are defensive; unchanged reads still hit cache; revisions remain monotonic. `update_many` must be all-or-nothing for one tenant when any requested user or validation fails, and successful batch invalidation must occur only after commit. Do not disable caching or clear the whole cache.

Only `src/profiledirectory/` may change. Run the public suite plus warm positive, warm negative, tenant isolation, defensive copy, rollback, and successful batch probes.

```repair_contract
{"schema_version":"1.0","repository":"assets/repository","allowed_paths":["src/profiledirectory/"],"public_test_command":["{python}","-m","unittest","discover","-s","tests","-v"],"network":"local_only","recovery":"none","max_patch_bytes":2000000}
```
''',
"test_cases/test_006/input.md": r'''
# Repair request: torn segment tails and pending manifests block restart

Repository: `assets/repository`

SegmentStore persists length-prefixed JSON records with CRC32 in binary segments and selects generations through `MANIFEST.json`; compaction stages `MANIFEST.next`. After abrupt termination, a partial final record makes all prior durable records unreadable. A complete pending manifest may also be discarded even when its segment is durable.

Implement deterministic recovery grounded in these formats. Ignore only a physically incomplete final record in the final active segment without rewriting it during `load`; any checksum failure, impossible complete length, malformed JSON, missing non-final segment, root-escaping segment name, or corruption before the tail raises `StoreCorruption` with path and byte offset. Recovery validates pending manifests and referenced segments, atomically commits a complete newer generation, rolls back an incomplete or stale generation, removes only proven stale temporary files, is idempotent, and preserves append/compaction ordering. A later append after recovery must not preserve torn bytes ahead of new data.

This is closed corpus. Only `src/segmentstore/` may change. Run public and binary crash-state, repeated restart, pending commit/rollback, corruption-boundary, append-after-recovery, and compaction probes. Deliver `migration_report.json` and an executable recovery self-test artifact using the required placeholders.

```repair_contract
{"schema_version":"1.0","repository":"assets/repository","allowed_paths":["src/segmentstore/"],"public_test_command":["{python}","-m","unittest","discover","-s","tests","-v"],"network":"closed","recovery":"required","recovery_format":"segmentstore-v1-manifest-segments","max_patch_bytes":2000000}
```
''',
}

PROFILE = r'''
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
'''

def main():
    for relative, text in CASES.items():
        path = ROOT / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(dedent(text).lstrip(), encoding="utf-8")
    profile = ROOT / "test_cases/test_004/assets/WIREBATCH_PROTOCOL_V1.md"
    profile.write_text(dedent(PROFILE).lstrip(), encoding="utf-8")
    old = ROOT / "test_cases/test_004/assets/RFC9110_RETRY_AFTER_PROFILE.md"
    if old.exists():
        old.unlink()
    print("generated 8 case inputs and WireBatch profile")

if __name__ == "__main__":
    main()
