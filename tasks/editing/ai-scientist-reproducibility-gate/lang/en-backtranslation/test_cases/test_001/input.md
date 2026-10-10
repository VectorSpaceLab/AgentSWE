# Release request: restart and lost-response recovery

Bind the committed release to one durable attestation and one project-local notification event; a lost-response retry must recover both without a duplicate event.

Audit the patient generalization claim, keep the valid negative control, and publish only the blocked, evidence-backed release capsule. Prepare, commit, and the later lost-response retry happen in separate processes. The retry must reproduce the original capsule and settlement exactly while project state stays internally consistent.

Use the supplied capsule member policy, usage statement, and versioned project budget. Do not include any workspace file not explicitly selected by the capsule policy.

Validate `run_manifest.json` as the execution provenance contract: check the safe relative replay command, the timeout, the script digest, and each worker's experiment/seed/result digest or failure classification. The ordinary worker failure in this case may be kept under the manifest's partial policy, but the failed worker must be listed explicitly in the journal; a fatal class or a digest mismatch must fail closed before reservation, attestation, notification, and publication.

Publish the deterministic `reproducibility_run.json`, binding the project, run ID, manifest SHA-256, per-worker decisions, and the cleanup scope specified by the manifest; cleanup must record `global_process_scan: false` and `unrelated_processes_preserved: true`. The journal must enter the capsule and the attestation's release digest. The durable copy after commit, the lost-response retry, and the recovered output must keep the same journal ID and exactly identical bytes, and must not repeat the replay, settlement, attestation, or notification event.

## Governance outcome sequence required by the evaluator (evaluator-private, not entered into the agent context)

This case needs 2 steps in total. Each step is decided only by the evaluator's own receipt projections, durable store enumeration, and published byte snapshots;
self-reported product flags are not trusted. The number of achieved steps enters input/03 §30: `p = 10 + floor(50 × established / required)`, the cap of this case's `case_recovery` dimension is `p%` of that dimension's full score (rounded down), and the other four dimensions are not constrained by that cap;
a receipt phase not corroborated by a durable record triggers the 40-point cap of §29; a non-publishing operation publishing bytes, or any operation rewriting an existing publication, triggers the 30-point cap of §29.

| # | transition | operations allowed to trigger it | achievement condition (reference behavior) |
|---:|---|---|---|
| 1 | `committed_response_lost` | `verify` | product exit 0; receipt phase=committed, budget settled, all five release artifacts present, and a committed durable record with the same fingerprint in the session store. After achievement the evaluator deletes the two response copies `reproducibility_capsule.zip` and `transaction_receipt.json`. |
| 2 | `response_reconciled` | `verify` | after the retry: committed/settled/all five present, the durable committed record still there, the recovered artifact hashes and the commit/capsule/settlement/event identities byte-identical to those before the loss, and the project's settled total the same as at the time of loss (no second settlement). |
