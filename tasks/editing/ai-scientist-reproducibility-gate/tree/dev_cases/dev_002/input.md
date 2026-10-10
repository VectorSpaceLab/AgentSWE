# Public case 2: prepare, restart, budget status, and commit

Attestation and notification artifacts are commit-only and must not appear during prepare or the read-only status after a restart.

Verify the supplied single-seed stability study and block its unsupported stability claim while keeping the supported numeric comparison. Govern it under project `public-repro`, request `publish-002`, owner `worker-b`, generation `1`, and the supplied policies.

The exact charge is `5500` micro-dollars. `prepare` must create a durable deterministic staged capsule and reserve `5500`, but it must not publish release artifacts, update the external ledger, or settle usage. A separate `status` process must report the prepared generation and project totals without changing state.

After a restart, `commit` runs through `launch_scientist_bfts.py`. It must verify the original staged bytes, publish the scientific files and the capsule, convert one reservation into one settlement, and leave no reserved amount. The final status must expose the linked prepare/commit history and the same policy, capsule, charge, and settlement evidence.

At the same time, verify the replay path/argv/timeout and script digest in `run_manifest.json`, and the per-worker experiment, seed, successful result digest, and `worker_error` failure classification. Because the policy explicitly allows partial and that failure does not belong to the fatal classes, `prepare` should record the successful results together with the failed worker as `partial`, rather than pretending complete success or blocking the release; if `pool_broken` or an integrity-class failure appears, it must fail closed before reservation and publication.

`prepare` must durably stage the deterministic `reproducibility_run.json` but must not copy it into the response directory, the attestation, or the notification store. The journal binds the project, run ID, and manifest SHA-256, recording the successful/failed workers, the policy decision, and cleanup limited to the current run (including the failed worker, `global_process_scan: false`, and `unrelated_processes_preserved: true`). `commit` publishes the same journal, puts it into the capsule, and includes it in the attestation digest; restart, status queries, and repeated recovery must not change the journal bytes or repeat the replay, settlement, attestation, or notification.

`prepare` must also stage the canonical v3 `science_gate` and bind it to the staged transaction receipt and capsule manifest; `commit` may settle only after re-verifying it. The final attestation and notification must reference the same gate digest, and a second science fence must not be generated on restart or response recovery.

```bash
python dev_cases/run_public.py --submission <delivery_dir> --case dev_002 --work-dir <temporary_dir>
```

## Durable session records and publication boundaries (input/03 §29)

The public assertions of this case independently enumerate `--session-store`: a
`prepared`/`committed` phase claimed by a receipt must have a durable record
findable by `request_fingerprint` (internal hierarchy of your choice, identity
may be inside an `identity` object). The assertions also take byte snapshots of
the attestation store, the notification store, the `committed` copies under the
session store, and the budget store before and after every governed operation:
only `commit` and its atomic form `verify` may add published bytes or change
settled totals, and no operation may rewrite or delete already published bytes.
Hidden cases are evaluated by the same semantics and the case score cap is set
accordingly.
