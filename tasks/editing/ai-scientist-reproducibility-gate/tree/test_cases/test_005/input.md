# Release request: project isolation and authorization

Attestation, notification status, and errors are project-local; an unauthorized participant cannot learn whether peer events exist.

Audit the invalid significance and the high variance. Two projects use the same tenant-level release identifiers and the same durable budget root but different policies, usage statements, and capsule bindings.

Each authorized project must be charged and attested independently. Project status, errors, and archive content must not leak the peer project or the unselected operator note. An unauthorized participant must fail without revealing whether peer state exists.

Alpha and Beta must each validate and bind their own `run_manifest.json` and `run_manifest_beta.json`, project ID, run ID, replay/script/result digests, and per-worker journal; even when the tenant-level identifiers collide, project-local and distinct journal IDs must be generated. Each `reproducibility_run.json` should record its own project's failure-policy decision and cleanup `scope_id`, `global_process_scan: false`, and `unrelated_processes_preserved: true`, and must not reference peer workers/manifests or contain the unselected operator secret.

Every journal must enter its own project's capsule and be included in its own project's attestation release digest, with the durable commit and status/recovery copies byte-consistent. An unauthorized status request must not create or modify journals, attestations, notifications, or budget state, and must not leak whether a peer project exists through error text, digests, events, or archives.

## Governance outcome sequence required by the evaluator (evaluator-private, not entered into the agent context)

This case needs 4 steps in total. Each step is decided only by the evaluator's own receipt projections, durable store enumeration, and published byte snapshots;
self-reported product flags are not trusted. The number of achieved steps enters input/03 §30: `p = 10 + floor(50 × established / required)`, the cap of this case's `case_recovery` dimension is `p%` of that dimension's full score (rounded down), and the other four dimensions are not constrained by that cap;
a receipt phase not corroborated by a durable record triggers the 40-point cap of §29; a non-publishing operation publishing bytes, or any operation rewriting an existing publication, triggers the 30-point cap of §29.

| # | transition | operations allowed to trigger it | achievement condition (reference behavior) |
|---:|---|---|---|
| 1 | `alpha_committed` | `verify` | this project's commit succeeds, settled, all five present, with a committed durable record. |
| 2 | `peer_project_isolated` | `status` | the evaluator's beta probe commits successfully first; alpha's read-only status succeeds and is still committed; both projects have durable records; `project-beta`/`tenant-beta` appear nowhere in alpha's output. |
| 3 | `unauthorized_peer_denied` | `status` | the unauthorized participant exits non-zero and writes `code=unauthorized`; the output does not leak the peer project, tenant, or operator secret. |
| 4 | `notification_retry_reconciled` | `verify` | after recovery by the authorized owner: committed, all five present, a committed durable record, alpha's identity consistent with the baseline, and exactly one event for `project-alpha` in the outbox. |
