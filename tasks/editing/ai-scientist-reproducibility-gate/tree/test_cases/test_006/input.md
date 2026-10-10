# Release request: cancel, re-entry, and artifact reconciliation

Cancel creates no attestation/event. Re-entry and response repair must produce the final pair, while ordinary mode leaves both stores unchanged.

Audit the config/log/checksum/figure inconsistencies. Prepare and then cancel an uncommitted release, proving that its reservation was released and that a stale commit is refused.

A new generation enters through the production launcher, commits one capsule and one settlement, and then recovers deleted response artifacts from the committed durable bytes without paying again. Ordinary one-shot verification must remain backward compatible and must not touch the budget store.

The governed lifecycle must validate the relative replay command, timeout, script SHA-256, worker seed/result digests, failure policy, and cleanup scope of `run_manifest.json`. The initial `prepare` may durably stage `reproducibility_run.json`, but after cancel it must not publish the journal, attestation, or notification; a stale commit likewise must not turn the cancelled journal back into a publishable state.

When the new generation commits successfully, generate and persist the final journal, binding the project, run ID, manifest SHA-256, per-worker decisions, and run-scoped cleanup, explicitly recording `global_process_scan: false` and `unrelated_processes_preserved: true`. The journal must enter the capsule and be included in the attestation digest. After the committed artifacts in the response are deleted, the retry may only recover the same journal ID, capsule, attestation, notification, and settlement from the durable committed bytes, and must not re-run the replay or charge again. Ordinary one-shot mode only produces compatible scientific output and must not create a provenance journal or touch the session/budget/attestation/notification stores.

## Governance outcome sequence required by the evaluator (evaluator-private, not entered into the agent context)

This case needs 5 steps in total. Each step is decided only by the evaluator's own receipt projections, durable store enumeration, and published byte snapshots;
self-reported product flags are not trusted. The number of achieved steps enters input/03 §30: `p = 10 + floor(50 × established / required)`, the cap of this case's `case_recovery` dimension is `p%` of that dimension's full score (rounded down), and the other four dimensions are not constrained by that cap;
a receipt phase not corroborated by a durable record triggers the 40-point cap of §29; a non-publishing operation publishing bytes, or any operation rewriting an existing publication, triggers the 30-point cap of §29.

| # | transition | operations allowed to trigger it | achievement condition (reference behavior) |
|---:|---|---|---|
| 1 | `initial_prepared` | `prepare` | prepare succeeds, reserved, with a prepared durable record, and no attestation/notification/committed bytes are published. |
| 2 | `run_cancelled` | `cancel` | cancel succeeds, budget status cancelled, with a cancelled durable record, and the published bytes and settled totals completely unchanged. |
| 3 | `stale_commit_rejected` | `commit` | the delayed commit of the cancelled request exits non-zero and writes a structured error; the cancelled record remains; no complete release set; published bytes unchanged. |
| 4 | `generation_reentry_response_lost` | `verify` | the new generation enters by compare-and-swap and commits successfully, settled, all five present, with a committed durable record; the response copies are then deleted. |
| 5 | `response_artifact_repaired` | `verify` | recovered only from the durable committed bytes, with identities and bytes consistent with those before the loss, and no second settlement produced. |
