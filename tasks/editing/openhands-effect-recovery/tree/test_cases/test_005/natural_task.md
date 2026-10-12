# Resume a legacy conversation without damaging its neighbours

This conversation still lives in the old unscoped checkpoint format. Bring it
into the current backend-scoped ledger and keep it usable. Other records share
the same storage: a same-named conversation on a different backend, a browser
sync coordination record and a workspace transaction record. Normal runtime
traffic continues during the migration, and the newest record is tampered with
afterwards.

Four outcomes are required, and they are scored independently.

1. **OHR401 — Migrate once, losing nothing.** The legacy lineage must become a
   schema-v3 checkpoint under the recorded default backend, keeping its durable
   event cursor, its completed effect, its uncertain effect and its recent event
   ids. Migration is one-way and must be safe to retry after a restart.
2. **OHR402 — Only one first mutation wins.** Two competitors replay the same
   legacy compare-and-set base. At most one of them may take the lineage
   forward; the other must fail against the current revision rather than publish
   a second migration.
3. **OHR403 — Neighbours are byte-identical afterwards.** The foreign backend's
   record, the sync coordination namespace and the workspace transaction
   namespace must be unchanged, byte for byte, after the migration and after
   compaction. Ledger compaction may only remove revisions it decoded as exactly
   this scope, including prefix-lookalike encoded names.
4. **OHR404 — Compaction is bounded and survives corruption.** The continuing
   runtime traffic will push this lineage past its retention bound, so
   compaction must run and must leave the retained valid revision set within
   that bound. Then the newest record is tampered with and a future-version
   record is injected: restart the product and show that a valid retained
   predecessor still restores the conversation. Compacting down to a single
   revision loses that fallback.

A removal failure after a valid revision is published is non-fatal: the
committed latest state must stay readable, the excess is cleaned up on a later
mutation, and no compaction anchor may claim an undeleted revision.

If corruption or a future-format record blocks safe recovery, identify that
boundary honestly instead of replacing valid history.
