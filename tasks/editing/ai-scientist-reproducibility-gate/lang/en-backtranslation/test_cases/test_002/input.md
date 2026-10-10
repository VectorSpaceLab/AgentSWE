# Release request: concurrent writers under a shared limit

Only the established winner may create an attestation or notification; the refused loser must leave both durable stores unchanged.

Audit the selective seed report and the ignored negative results. Two authorized workers concurrently prepare different release sessions against one project budget whose remaining limit covers only one charge.

Exactly one established owner may reserve and carry out the deterministic capsule and the committed settlement. The other must receive a structured budget or ownership refusal without a capsule, a settlement, or corrupted shared state. Two failures do not satisfy this requirement.

Every attempt must first validate the supplied `run_manifest.json`: the project/run identity, relative replay argv, timeout, script digest, unique worker identities, seeds, and result digests cannot be bypassed by caller self-reports. The `reproducibility_run.json` published by the winner should record the `complete` decision corresponding to the manifest, the exact worker journal, and run-scoped cleanup, which must have `global_process_scan: false` and `unrelated_processes_preserved: true`.

The loser must not leave a staged/committed journal, capsule, attestation, notification, or budget receipt. The winning journal must enter the capsule and be included in the attestation digest, and the durable commit and subsequent status/recovery reads must keep the same journal ID and bytes; the concurrent failure must not rewrite the winner's provenance, the shared budget, or the project-local auxiliary stores.

## Governance outcome sequence required by the evaluator (evaluator-private, not entered into the agent context)

This case needs 3 steps in total. Each step is decided only by the evaluator's own receipt projections, durable store enumeration, and published byte snapshots;
self-reported product flags are not trusted. The number of achieved steps enters input/03 §30: `p = 10 + floor(50 × established / required)`, the cap of this case's `case_recovery` dimension is `p%` of that dimension's full score (rounded down), and the other four dimensions are not constrained by that cap;
a receipt phase not corroborated by a durable record triggers the 40-point cap of §29; a non-publishing operation publishing bytes, or any operation rewriting an existing publication, triggers the 30-point cap of §29.

| # | transition | operations allowed to trigger it | achievement condition (reference behavior) |
|---:|---|---|---|
| 1 | `concurrent_reservation_race` | `prepare` | the process intervals of the main operation and the evaluator's concurrent probe genuinely overlap; exactly one side is prepared+reserved and the other is structurally refused with `budget_exhausted`; the winner has a prepared durable record. |
| 2 | `winner_commit_response_lost` | `commit` | the winner's commit succeeds, settled, all five present, with a committed durable record; the response copies are then deleted. |
| 3 | `winner_reconciled` | `verify` | same as `response_reconciled`, and the winner's provenance or the shared budget must not be rewritten. |
