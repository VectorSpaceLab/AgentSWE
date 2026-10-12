# Task goal: shared gateway handoff ledger

Extend the supplied OpenClaw Gateway with a durable cross-channel handoff
ledger that remains authoritative when two independent gateway processes
share one `OPENCLAW_STATE_DIR`. A user can safely start work through one
channel and continue, delegate limited access, attach content, schedule
external effects, pause, resume, cancel, inspect, compact, migrate, or
repair it through another. This is an Edit benchmark against the supplied
TypeScript Gateway, not a standalone task service.

The hard boundary is ownership plus recoverable authority. A task ID never
authorizes. Every mutation is bounded by the current channel's grant, the
task's capability, revision, sequence, capability epoch, and durable owner
epoch. A live gateway owner excludes the second gateway; after expiry or
process loss a peer may take over by atomically advancing one integer
epoch. Owner, capability, grant, migration, and compaction epochs come from
stored counters, not wall-clock values, and never decrease after clock
rollback or restart.

Grants form a weakening lineage. A child can only lose permissions or
visibility relative to its parent. Revoking or rotating a parent
invalidates its descendants in the same transaction as the permission
change. Task capability rotation preserves the cycle-1 atomic/replay
contract.

External effects use a transactional outbox. Admission commits the task
event and the pending outbox row together. Dispatch goes only to the
configured loopback idempotent sink with a stable effect key; crash
recovery and owner takeover must produce one externally accepted effect and
one durable completion. Attachments are stored by a server-computed SHA-256
content address and are readable only through the current task/channel
permission. Projections and inspection never reveal principals, paths,
grants, capabilities, outbox payloads, or sink credentials.

User-visible completion delivery uses a separate durable receipt queue. A
producer freezes a bounded result or an internal-control payload against
its immutable source channel. Pulling an item creates a durable, expiring
claim but never deletes the payload. On the pull-consumer path, only an
explicit, replayable acknowledgement may mark it consumed. A lost pull
response, process crash, claim expiry, owner takeover, or restart therefore
redelivers the same item; concurrent consumers cannot hold live claims at
the same time. Full, summary, and internal-control disclosure are
structural fields, not text heuristics, and remain bounded by the current
channel lineage and result authority.

Human-channel egress crosses a separate production boundary. A user-result
delivery freezes whether its source conversation is a direct message, a
group, or a thread. The current source-route participants may be reached
only through the configured loopback channel connector. Connector
acceptance is not delivery: only reconciling the same stable dispatch
identity with a non-empty platform message identity records a verified
send. A lost HTTP response, gateway death, lease takeover, restart, and
ambiguous provider results must be retried or reconciled without posting
twice, reordering in-flight routes, or letting stale lineage, foreign
routes, or internal-control payloads reach a human channel. Binary media
egress is a durable transaction inside that boundary, not a best-effort
attachment added at send time. Before a media-bearing result may reach the
channel connector, the gateway freezes a server-computed immutable manifest
over one authorized content-addressed attachment, including its ordered
chunk digests. It then drives the provider upload through separately
recoverable idempotent init, chunk, and finalize operations. A lost
response at any stage, a restart, or an owner takeover reuses the exact
identities and request bodies; the stored offset may advance only after a
matching provider observation. The final logical text-plus-media message
stays unchanged. Partial, duplicate, or out-of-order media receipts do not
verify it until the exact expected media set agrees with a non-empty
platform message identity. Unfinished upload state, stale lineage, foreign
routes, or missing disclosure permission are rejected before channel
connector I/O.

Inbound button, selection, and callback actions use a separate durable
interaction transaction. They are accepted only on the same immutable
direct/group/thread source route on which the verified outbound was
delivered, and only after the configured loopback interaction adapter
verifies their opaque callback token. The provider event identity, action,
body digest, correlation, route, and verification request are immutable. If
adapter acceptance is ambiguous, a restart or a new owner reconciles the
same prepared verification instead of creating a second interaction or
agent turn. Only one current actor may hold a durable processing claim.
Completing that claim atomically records the processed interaction and
freezes one user-result delivery whose route and reply correlation are
inherited from the source message. Duplicate callbacks, concurrent
gateways, stale owners, retired lineage, and changed bodies cannot create
duplicate turns or replies. Callback tokens, provider event/message/
interaction identities, callback and reply bodies, claims, adapter
credentials, and exact external routes stay out of status, inspection,
compaction proofs, and evaluator evidence. Restart, takeover, migration,
compaction, repair, and wall-clock rollback preserve verified callbacks,
the live/expired claim rules, completions, and correlated replies without
adapter or channel replay.

Schema-1 tasks may be created and then transactionally migrated to schema
2. Schema 2 adds owner leases, grant lineage, outbox state,
content-addressed attachments, and chained integrity records. Compaction
proofs stay chained; verification detects inconsistent derived
projection/proof state, and repair may rebuild derived state only from
durable authorized events without resetting epochs, reviving authority,
duplicating completed effects, acknowledging deliveries, repeating
completed provider uploads or channel sends, or losing unacknowledged
payloads or recoverable upload cursors.

Do not build a UI, a remote database, cloud sync, or contact real channel
accounts. The evaluator-owned loopback channel, media, and interaction
connectors are frozen adapter boundaries. Preserve existing
gateway/channel/session behavior for users who do not use handoff. The
exact evaluator-facing contract is the compiled product Gateway RPC
surface in `02_interface_and_delivery.md`; an isolated library or test
server does not complete the task. Do not add evaluator-only failure hooks,
magic fixtures, or case-specific behavior.
