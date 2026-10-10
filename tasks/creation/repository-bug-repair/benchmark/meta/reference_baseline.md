# Reference and Fixture Baseline

## Upstream reference

SWE-agent and mini-swe-agent were not executed for v4. Their full provider/environment stack would not establish a unique answer for this implementation-independent synthetic benchmark. The immutable repository facts and licenses were inherited from the completed v3 source analysis; no credentials were accessed.

## Pristine v4 baseline

Environment: CPython 3.10 on Linux, standard library only. On August 24, 2026, `python3 -B evaluator/harness/validate_benchmark.py --run-tests` established:

| Case | Pristine public | Pristine evaluator checks | Production lines | Intended defect class |
|---|---:|---:|---:|---|
| `dev_001` | pass | fail | 174 | normalized cache identity and policy revision |
| `dev_002` | pass | fail | 169 | JSONL version compatibility and atomic migration |
| `test_001` | pass | fail | 269 | linearizable state/journal idempotency |
| `test_002` | pass | fail | 344 | transactional SQLite schema migration/recovery |
| `test_003` | pass | fail | 264 | exact streaming merge and asymptotic performance |
| `test_004` | pass | fail | 267 | incremental framed-protocol state across chunks |
| `test_005` | pass | fail | 295 | tenant-scoped positive/negative cache and batch atomicity |
| `test_006` | pass | fail | 298 | torn binary tail and pending-manifest recovery |

The size gate applies to the six hidden cases and is 250–1200 physical Python lines beneath `src/`.

## Construction-only repair feasibility

Temporary repaired copies under the v4-owned `.construction/` directory were exercised, then removed before handoff. Every repaired public and evaluator suite passed. Notable observations:

- four repeated 24-thread duplicate-delivery probes produced exactly one successful record each; 80 distinct deliveries preserved contiguous sequence/state/audit order;
- four concurrent SQLite migration callers converged without error and `PRAGMA integrity_check` returned `ok`;
- eight one-pass generators totaling 80,000 unique events completed in approximately 0.78 seconds with the hidden memory gate enabled;
- byte-by-byte WireBatch delivery, split headers/payloads, multiple frames, duplicate headers, checksum, and `finish()` behavior passed;
- warm positive/negative tenant cache and atomic failed/successful batch probes passed;
- torn segment tails, append-after-recovery, checksum corruption, pending-manifest commit, rollback, and repeated restart passed.

The JSONL, SQLite, and segment/manifest recovery artifacts were run directly and through `evaluate_case.py`. Each emitted the exact recovery format and true pre-state, post-state, interruption, retry, compatibility, and rollback checks. Harness results also confirmed patch applicability, path scope, public/hidden passes, report/file inventory equality, and recovery execution.

No reference patch is retained or designated as the only correct answer.

## Limitations

Timing remains hardware-sensitive, mitigated by a measured margin below the 2.0-second threshold. Concurrency uses synchronized starts but cannot prove fairness for every possible scheduler. The evaluator constrains processes and patch scope but is not a complete hostile-code sandbox. Recovery self-tests prove the named fixture states, not every filesystem or SQLite failure mode. Repositories are hundreds, not millions, of lines to remain normally solvable in 600 seconds and 4 GiB.

