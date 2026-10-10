# Release request: corrupted staged capsule and recovery

Do not attest or notify a corrupted stage. The recovered higher generation must create one digest-bound attestation and one idempotent event.

Audit the stale and overwritten experimental evidence. Prepare the governed release, then handle an integrity loss in the durable staged capsule.

Commit must detect the corruption before publication or settlement. A valid higher generation must be able to supersede the failed prepare, reconcile its reservation, produce a new verified capsule, and settle exactly one charge without harming the audit evidence of the failed generation.

First validate the normal `run_manifest.json`'s replay/script/result digests, per-worker facts, failure policy, and cleanup scope; the separately supplied fatal manifest contains a policy-listed `pool_broken` failure and, even when partial is allowed, must be refused before reservation, staging, attestation, notification, and external publication, returning a structured error. A fatal worker must not be treated as an ordinary recoverable failure.

The `reproducibility_run.json` produced by the normal prepare must be kept as audit evidence together with the corrupted stage, but the corrupted commit must not publish it. After the higher generation recovers, publish a new journal decided by the normal manifest, binding the manifest SHA-256, worker decisions, and scoped cleanup; cleanup must record `global_process_scan: false` and `unrelated_processes_preserved: true`. The journal enters the capsule and is included in the attestation digest. That journal must be byte-consistent in the final durable commit and in status/response recovery, and must not reuse the fatal journal, global cleanup, or produce a second settlement/event.

## Governance outcome sequence required by the evaluator (evaluator-private, not entered into the agent context)

This case needs 3 steps in total. Each step is decided only by the evaluator's own receipt projections, durable store enumeration, and published byte snapshots;
self-reported product flags are not trusted. The number of achieved steps enters input/03 §30: `p = 10 + floor(50 × established / required)`, the cap of this case's `case_recovery` dimension is `p%` of that dimension's full score (rounded down), and the other four dimensions are not constrained by that cap;
a receipt phase not corroborated by a durable record triggers the 40-point cap of §29; a non-publishing operation publishing bytes, or any operation rewriting an existing publication, triggers the 30-point cap of §29.

| # | transition | operations allowed to trigger it | achievement condition (reference behavior) |
|---:|---|---|---|
| 1 | `prepared_capsule_corrupted` | `prepare` | prepare succeeds, reserved, with a prepared durable record; the evaluator then corrupts the staged capsule bytes according to the receipt's `stage_path`. |
| 2 | `corruption_rejected` | `commit` | the next commit exits non-zero and writes a structured error with `code=stage_integrity_error`; the reservation remains; no complete release set is produced. |
| 3 | `higher_generation_recovered` | `verify` | a higher generation takes over by compare-and-swap (`takeover_generation` equal to the original generation), commit succeeds, settled, all five present, with a committed durable record. |
