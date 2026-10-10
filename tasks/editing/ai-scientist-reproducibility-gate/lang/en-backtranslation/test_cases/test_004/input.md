# Release request: policy migration

Attestation and notification policy/version drift must fail closed; the migrated commit binds both artifacts to the new policy version.

Audit the unequal training budgets and the invalid ablation. The release is prepared under one exact pricing policy but commit is attempted with a newer policy version and a different capsule policy binding.

The original request must fail closed rather than silently reprice. After cancel reconciles its reservation, a new request may use the newer policy, compute its integer-rounded charge, publish the corresponding deterministic capsule, and settle under the new version while keeping the old policy evidence auditable.

Run provenance also belongs to the immutable request fingerprint. The v1 prepare binds `run_manifest.json` together with its manifest SHA-256, worker journal, and cleanup scope; committing the old request with `run_manifest_v2.json` together with the new capsule/budget policies is drift and must fail closed before publishing the journal, attestation, notification, or settlement. After cancel, the old v1 journal and policy evidence should remain auditable but must not be treated as the new-version publication.

The new request must regenerate the deterministic `reproducibility_run.json` according to `run_manifest_v2.json`, recording its run ID, the complete worker decisions, the failure-policy conclusion, and the v2 `scope_id`, and explicitly recording `global_process_scan: false` and `unrelated_processes_preserved: true`. The final journal enters the new-version capsule and is included in the attestation release digest; the commit and the status/recovery copies must keep the same journal ID and bytes, and must not mix in the v1 manifest or cleanup scope.

## Governance outcome sequence required by the evaluator (evaluator-private, not entered into the agent context)

This case needs 5 steps in total. Each step is decided only by the evaluator's own receipt projections, durable store enumeration, and published byte snapshots;
self-reported product flags are not trusted. The number of achieved steps enters input/03 §30: `p = 10 + floor(50 × established / required)`, the cap of this case's `case_recovery` dimension is `p%` of that dimension's full score (rounded down), and the other four dimensions are not constrained by that cap;
a receipt phase not corroborated by a durable record triggers the 40-point cap of §29; a non-publishing operation publishing bytes, or any operation rewriting an existing publication, triggers the 30-point cap of §29.

| # | transition | operations allowed to trigger it | achievement condition (reference behavior) |
|---:|---|---|---|
| 1 | `v1_prepared` | `prepare` | prepare succeeds, reserved, with a prepared durable record, bound to `budget_policy_v1.json`. |
| 2 | `policy_drift_rejected` | `commit` | committing the old request with the v2 policy/manifest exits non-zero and writes `request_conflict`/`transaction_error`; the v1 prepared durable record is unchanged. |
| 3 | `v1_cancelled` | `cancel` | cancel succeeds, budget status cancelled, with a cancelled durable record, and the published bytes and settled totals completely unchanged. |
| 4 | `stale_request_fenced` | `commit` | the stale old-policy commit exits non-zero and writes a structured error; the cancelled record remains; no complete release set; published bytes and settled totals unchanged. |
| 5 | `v2_migrated` | `verify` | the new request commits successfully under v2, settled, all five present, with a committed durable record, and the budget receipt's `policy_version` equals the version of `budget_policy_v2.json`. |
