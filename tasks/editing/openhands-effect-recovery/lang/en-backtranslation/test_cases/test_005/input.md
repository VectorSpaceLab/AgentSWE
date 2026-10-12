# Hidden scenario: schema migration and scoped compaction

Import a valid unscoped schema-v2 chain into `default-local` once, keeping
the completed, uncertain, event, workspace, terminal, and compaction
evidence. Race two first mutations from the same legacy revision, require
one migration winner, and refuse the stale loser again after restart without
a second migration revision. Keep an independent same-conversation lineage
on another backend unchanged. Force schema-v3 compaction, corrupt the newest
record, inject a future version, and verify the fallback plus live
lease/generation/outbox retention after restart. Then compact the migrated
`default-local` lineage while the same conversation has an independent
foreign-backend v3 lineage. Compaction must be deterministic and exactly
scoped: keep the migrated outbox/authority, every foreign revision, and all
immutable legacy records, including prefix-like encoded names. After the
newest revision is valid, inject one `removeItem` failure. The mutation must
still return committed state, the temporary retention excess must be cleaned
up on a later mutation, and no compaction anchor may claim a revision that
was not removed. Open dispatcher sessions against the migrated and the
foreign lineage. The migrated session resumes at the retained legacy cursor
and suppresses history echoes; after compaction the foreign-scope
replacement obtains only its own durable cursor and fences the late work of
the earlier default-local session from before the mutation. Ledger
migration/compaction must leave the separate exactly scoped durable sync
record namespace unchanged byte for byte; sync terms are not ledger
revisions or compaction candidates. The same byte-exact isolation applies to
the workspace recovery transaction namespace: ledger migration and
compaction cannot enumerate or delete its immutable records.
