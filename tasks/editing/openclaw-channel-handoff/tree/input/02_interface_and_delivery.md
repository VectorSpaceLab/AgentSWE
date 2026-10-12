# Frozen interface and delivery contract

Implement exactly the Gateway WebSocket RPC methods below. The evaluator
calls them only through the compiled production CLI:

```bash
node openclaw.mjs gateway call <method> \
  --url ws://127.0.0.1:<port> --token <gateway-token> \
  --params '<json-object>' --json
```

It builds once, starts independent gateway processes with fresh or shared
`OPENCLAW_STATE_DIR` values, disables environment providers/channels, and
controls process termination, clock values, and the local idempotent effect
sink. A standalone module, a test-only server, an alternate binary, or a
candidate adapter is not valid.

## General rules

All exact JSON fields are snake_case. Reject unknown fields. IDs are
non-empty UTF-8 strings of at most 128 bytes. Times are integer Unix
milliseconds. Opaque grants, capabilities, and dispatch claims are 32-256
printable ASCII characters. An actor is:

```json
{
  "principal_id": "principal-a",
  "channel": {"provider": "telegram", "account_id": "default", "peer_id": "peer-1"},
  "grant": "opaque-grant"
}
```

The channel identity is the exact provider/account/peer tuple; display
labels carry no authority. A successful task mutation contains `ok:true`,
`task_id`, `event_id`, `revision`, `idempotency_key`, `state`,
`owner_epoch`, `capability_epoch`, `schema_version`, and `replayed`. A
refusal is a structured gateway error or `{ok:false,error:{code,message}}`
and commits nothing.

The server identifies its own process from the evaluator-set
`OPENCLAW_HANDOFF_GATEWAY_ID`; clients never supply a gateway ID. Its value
is a non-secret stable instance label for one process lifetime. A gateway
missing that variable may generate a fresh label, but it must still
coordinate through the shared database.

## Grants and lineage

`handoff.bind` creates an operator-authorized root grant. Params are
`principal_id`, `channel`, `permissions`, `visibility`, `idempotency_key`,
and `occurred_at_ms`. Permissions are a subset of `start`, `append`,
`attach`, `status`, `control`, `effect`, `result_full`, and
`result_summary`. `visibility` is `full` or `summary`. Returns `grant`,
`grant_id`, `parent_grant_id:null`, `lineage_depth:0`, `grant_epoch:1`, the
canonical scope, and replay metadata. Replaying the key returns the same
secret.

`handoff.grant.delegate` params are `actor`, target `channel`,
`permissions`, `visibility`, `idempotency_key`, and `occurred_at_ms`. The
destination is the same principal. Permissions must be a subset of the
current parent grant's permissions; a `summary` parent visibility cannot
delegate `full`. The maximum lineage depth is 4. Returns the new `grant`,
`grant_id`, the parent grant ID, depth, and the monotonically stored
`grant_epoch`. Duplicate, revoked, rotated, or foreign parents fail.

`handoff.grant.rotate` params are `actor`, `idempotency_key`, and
`occurred_at_ms`. In one transaction it retires the grant and all current
descendants, advances the durable grant epoch, and returns a replacement
with the same parent/scope/attenuation. The replacement starts a new
subtree. Replay returns the same replacement. No successful old-subtree
operation may commit after the rotation response.

`handoff.revoke` params are `grant_id`, `idempotency_key`, and
`occurred_at_ms`. It transactionally revokes the named grant and its
descendants. Replay returns the original response. A concurrent mutation
either commits entirely before or is entirely refused after the revoke.
Rebinding never revives lineage.

## Tasks and shared gateway ownership

`handoff.start` params are `actor`, the evaluator-chosen `task_id`,
`session_key`, `idempotency_key`, `event_id`, `sequence` (1),
`occurred_at_ms`, `lease_ms` (1,000-60,000), and `schema_version` (`1` or
the current `2`). Schema 1 also accepts an optional
`legacy_projection_digest`, a 64-character lowercase hex digest recorded by
a legacy writer. Returns an opaque task `capability`, `capability_epoch:1`,
and the initial `owner_epoch`. Equal task IDs under different principals
stay isolated. Schema 1 accepts only ordinary lifecycle events before
migration; it cannot delegate task-local permissions, store attachments,
enqueue effects, compact, or repair.

Every non-terminal task has a durable owner lease. The initial owner is the
gateway that accepted `start`. The owner epoch is a stored positive integer,
not a timestamp. Leases use the durable logical clock described below.

`handoff.owner.acquire` params are `actor`, `task_id`, `capability`,
`observed_owner_epoch`, `lease_ms` (1,000-60,000), `event_id`,
`idempotency_key`, `expected_revision`, and `occurred_at_ms`. The current
owner may renew idempotently before expiry without changing its epoch. A
different gateway is refused while the lease is valid. At or after expiry it
atomically takes ownership, advances `owner_epoch` and the revision, and
returns `owner_gateway_id` plus `lease_expires_logical_ms`. Two contenders
cannot both acquire one epoch. Every mutating RPC validates the calling
gateway and the supplied `owner_epoch`; status/inspect may be served by a
non-owner.

Each task keeps `logical_time_ms`. On every accepted operation set it to at
least `max(previous_logical_time_ms + 1, occurred_at_ms, Date.now())`
without overflow. A lower wall clock or client time never lowers logical
time, extends an expired lease, decreases an epoch, or makes a stale owner
current. Across restarts all owner, capability, grant, migration, and
compaction epochs stay monotonic.

`handoff.append`, `handoff.pause`, `handoff.resume`, and `handoff.cancel`
keep the cycle-1 envelope. `append` params are `actor`, `task_id`,
`capability`, `event_id`, `idempotency_key`, `sequence`, `expected_revision`,
`owner_epoch`, `occurred_at_ms`, `schema_version`, and `event`. Transitions
omit `event`. Schema-2 event types are `progress`, `attachment.referenced`,
`subagent.started`, `subagent.completed`, `continuation.pending`,
`continuation.completed`, and `result.completed`. Effect completion and
attachment creation are produced only by their dedicated RPCs. Cancel is
terminal and a late completion cannot override it.

The scoped operation key plus the event ID identify one immutable request.
An exact duplicate replays the originally committed response across
clients, gateways, restarts, migration, and compaction. A changed
payload/time or a previously crossed key/event pair conflicts. Different
operations at one revision have one winner. Stale sequences, revisions,
owners, capability epochs, grant lineage, and future schemas fail closed.

`handoff.capability.rotate` keeps the cycle-1 params and behavior. The actor
needs `control`. It retires the old capability, advances the capability
epoch and revision, preserves events, and returns a replayable replacement
in one transaction. The retired capability fails for reads and writes
except replaying that rotation.

## Transactional effect outbox

`handoff.effect.enqueue` params are `actor`, `task_id`, `capability`,
`effect_id`, `kind`, a JSON `payload`, `event_id`, `idempotency_key`,
`sequence`, `expected_revision`, `owner_epoch`, and `occurred_at_ms`. `kind`
is a short identifier and the encoded payload is at most 16,384 bytes. The
actor needs `append` and `effect`. In one SQLite transaction append the
request event and create a pending outbox item keyed by
principal/task/effect ID. Returns `effect_id`, `outbox_state:"pending"`,
and the normal mutation fields.

`handoff.outbox.dispatch` params are `actor`, `task_id`, `capability`,
`effect_id`, `owner_epoch`, `idempotency_key`, and `occurred_at_ms`. It
needs `control` and current gateway ownership. Dispatch only to the exact
loopback HTTP URL in `OPENCLAW_HANDOFF_EFFECT_SINK_URL`; reject absent,
non-loopback, credential-bearing, or redirecting URLs. POST JSON containing
`task_id`, `effect_id`, `kind`, and `payload`, with the HTTP header
`Idempotency-Key: <effect_id>`. The evaluator sink returns
`{"ok":true,"effect_id":"...","receipt_id":"...","duplicate":false|true}`.
Only a valid 2xx response authorizes the transaction that marks the outbox
`delivered` and adds the effect to the projection once. Never hold a SQLite
write transaction during HTTP I/O.

Concurrent dispatchers use a durable claim fenced by the owner epoch. A
crash after sink acceptance but before the completion commit leaves a
recoverable pending or claimed item. A later owner retries with the same
effect key; the idempotent sink returns its original receipt and the ledger
commits one completion. Replaying a completed dispatch returns its stored
receipt and does not call the sink again. Status exposes only effect IDs
and states, never payloads, sink URLs, headers, or claims.

## Content-addressed attachments and results

`handoff.attachment.put` params are `actor`, `task_id`, `capability`,
`content_base64`, `media_type`, `visibility` (`source_channel` or
`principal`), `event_id`, `idempotency_key`, `sequence`, `expected_revision`,
`owner_epoch`, and `occurred_at_ms`. The actor needs `append` and `attach`.
Strictly decode at most 65,536 bytes, compute the size and lowercase SHA-256
server-side, store the content at the address `sha256:<64hex>`, and
transactionally append the authorized task reference. Returns
`attachment_id` equal to that content address plus `sha256`, `size`, and
`media_type`. Reusing identical bytes may reuse storage but does not reuse
the task/channel authorization record.

`handoff.attachment.read` params are `actor`, `task_id`, `capability`, and
`attachment_id`. It needs current `status` and attachment visibility.
Returns the metadata and `content_base64`. A source-channel item is readable
only through the exact current canonical channel lineage that added it. A
principal item is readable through another current grant of the same
principal/task. No item crosses tasks or principals. A revoked/rotated
grant subtree cannot read.

The `attachment.referenced` event contains only `attachment_id`.
Projections and inspection return visible metadata but never
`content_base64`, decoded bodies, or filesystem paths. A result summary
needs `result_summary`; a full result needs `result_full` and a
bound/delegated visibility of `full`. A full label or permission alone is
not sufficient.

## Durable channel delivery receipts

`handoff.delivery.enqueue` params are `actor`, `task_id`, `capability`,
`delivery_id`, `intent` (`user_result` or `internal_control`), `disclosure`
(`summary`, `full`, or `internal`), `visibility` (`source_channel` or
`principal`), `destination_type` (`direct`, `group`, or `thread`),
`content_base64`, `media_type`, `event_id`, `idempotency_key`, `sequence`,
`expected_revision`, `owner_epoch`, and `occurred_at_ms`. Strictly decode at
most 16,384 bytes. It needs schema 2. The actor needs `append`, the current
task capability, and current gateway ownership. `full` also needs
`result_full` plus grant visibility `full`; `summary` needs
`result_summary`. `internal_control` must use `disclosure:"internal"` and
`visibility:"source_channel"`; every other combination with `internal` is
invalid. The immutable source route is the enqueuing actor's exact
canonical provider/account/peer tuple plus `destination_type`. In one
transaction append `delivery.enqueued`, freeze the bytes and metadata, and
create one pending delivery. Returns the normal mutation fields plus
`delivery_id`, `delivery_state:"pending"`, `content_sha256`, and `size`.
Delivery IDs and operation key/event pairs are immutable and replay under
the general rules.

`handoff.delivery.pull` params are `actor`, `task_id`, `capability`,
`max_items` (1-16), `claim_ms` (1,000-60,000), `idempotency_key`, and
`occurred_at_ms`. It needs current `status` and capability authority but
not task ownership. It returns `ok:true`, `task_id`, `replayed`, and
`items`. Each item contains `delivery_id`, `intent`, `disclosure`,
`visibility`, `content_base64`, `media_type`, `content_sha256`, `size`,
`attempt`, an opaque `claim_token`, a positive stored `claim_epoch`, and
`claim_expires_logical_ms`. Source-channel items are pullable only through
a current grant on their exact source tuple. Principal items may cross
channels only under a current grant of the same principal. Summary and full
items need the same independent result permission/visibility as status; an
internal item is visible only to its exact source channel and to no other
channel, even one holding full result permission.

A pull is a durable claim, not a destructive drain. Concurrent pullers may
not both receive one item while a claim is live. An exact replay of the pull
returns the same items and claim tokens. At or after logical claim expiry an
authorized pull may redeliver the same frozen bytes with `attempt` and
`claim_epoch` increased. Clock rollback does not extend a claim. Claim
tokens are authoritative only for the matching delivery and actor lineage,
and are never accepted without current actor, task, and capability
authority.

`handoff.delivery.ack` params are `actor`, `task_id`, `capability`,
`delivery_id`, `claim_token`, `idempotency_key`, and `occurred_at_ms`. It
requires the same current disclosure authority that obtained the claim. In
one transaction it marks exactly the live claim `acked`, keeps the
acknowledgement receipt, and returns `ok:true`, `task_id`, `delivery_id`,
`delivery_state:"acked"`, `attempt`, `claim_epoch`,
`acknowledged_at_logical_ms`, and `replayed`. Replaying the key returns the
same response. Expired, foreign, retired-lineage, replaced-capability,
wrong-delivery, or superseded claims fail without mutation. Ack never
changes the task's terminal state or calls an external sink.

## Verified human-channel dispatch

`handoff.delivery.dispatch` params are `actor`, `task_id`, `capability`,
`delivery_id`, `owner_epoch`, `idempotency_key`, and `occurred_at_ms`. It
needs schema 2, `control`, the current task capability, current gateway
ownership, a current grant on the exact source route, and the delivery's
result disclosure authority. Only `intent:"user_result"` may cross this
boundary; `internal_control` is always refused. An acked or verified
delivery, a live pull claim, retired lineage, a foreign route, or a stale
owner fails without connector I/O. Dispatch only to the exact loopback base
URL `OPENCLAW_HANDOFF_CHANNEL_CONNECTOR_URL` with the bearer credential
`OPENCLAW_HANDOFF_CHANNEL_CONNECTOR_TOKEN`. Reject absent, non-loopback,
credential-bearing, redirecting, or path-confused URLs. POST to
`<base>/v1/messages` with `Authorization: Bearer <token>` and
`Idempotency-Key: <dispatch_id>`. `dispatch_id` is a stable non-secret value
derived from the immutable principal/task/delivery/content/source-route
facts; it never changes across retries, owners, restarts, migration, or
compaction. The exact JSON body is:

```json
{
  "dispatch_id":"stable-id",
  "delivery_id":"delivery-1",
  "route":{"provider":"discord","account_id":"ops","peer_id":"thread-7","destination_type":"thread"},
  "intent":"user_result",
  "disclosure":"full",
  "media_type":"text/plain",
  "content_base64":"...",
  "content_sha256":"64-lowercase-hex"
}
```

The connector returns 2xx JSON containing exactly `ok:true`, the same
`dispatch_id`, a non-empty `provider_receipt_id`, `accepted:true`, and a
`duplicate` boolean. Acceptance authorizes the durable state
`accepted_unverified`, never `acked` or `verified`. It holds an exclusive
durable egress claim and is not returned by `handoff.delivery.pull`. Do not
hold a SQLite write transaction during HTTP I/O. If the connector accepts
but the response is lost, the retry uses the same dispatch ID and body; a
duplicate acceptance commits the same receipt. Concurrent dispatch of one
delivery converges to one connector acceptance. Per exact route, a
later-enqueued user result cannot be submitted while an earlier one is in
flight or accepted but unverified.

`handoff.delivery.reconcile` has the same params as dispatch. It needs the
same current authority and calls `<base>/v1/receipts` with the same two
headers and the JSON `{dispatch_id,delivery_id,provider_receipt_id}`. The
2xx response contains the same identifiers, an `outcome` (`verified`,
`failed`, or `unknown`), and `platform_message_id`. Only `verified` with a
non-empty printable platform message ID marks the delivery `verified`,
stores the bounded receipt metadata, and returns
`delivery_state:"verified"`. `unknown`, a transport failure, mismatched
identities, or an empty platform identity remain non-terminal and must
never imply delivery. `failed` records a bounded failure reason and makes
the item eligible for an explicit new dispatch attempt under the same
immutable delivery; neither failure nor reconciliation changes the task's
terminal state. An exact replay returns the original response without
connector I/O.

For a delivery with staged media, the message body additionally contains a
`media` array in immutable `ordinal` order. Each element has the exact
`media_id`, `manifest_id`, `provider_media_id`, `media_type`, `size`,
`content_sha256`, and `ordinal`. Connector acceptance additionally returns
`media_receipts`, exactly one element per media item, with `media_id`,
`manifest_id`, `provider_media_id`, and a non-empty
`provider_attachment_receipt_id`. The reconcile request repeats that array.
The media reconcile response additionally returns zero or more
`media_receipts` in arbitrary order; each adds `platform_attachment_id`.
`outcome:"verified"` is authoritative only when the platform message
identity is non-empty and the response contains the exact expected set
exactly once, each identity non-empty and matching the accepted immutable
tuple. Partial, duplicate, unknown, or mismatched media receipts remain
non-terminal or fail closed. A plain-text delivery keeps the body and
response shapes above without the added media fields.

Status and inspection expose only bounded delivery metadata: IDs, intent,
disclosure, visibility, destination type, state, attempt, and content
digest/size, plus aggregate
pending/claimed/accepted-unverified/acked/verified/failed counts. They
never expose content bytes, claim tokens, connector credentials, connector
URLs, provider receipt IDs, platform message IDs, dispatch claims, claim
expiry, immutable route details beyond the caller's own canonical channel,
or acknowledgement secrets. The task's terminal state and the delivery
state remain distinct: a completed task with unacknowledged results has
pending or claimed deliveries without implying a successful channel
delivery.

## Durable provider media upload

`handoff.media.stage` params are `actor`, `task_id`, `capability`,
`delivery_id`, `media_id`, `attachment_id`, `chunk_size`, `owner_epoch`,
`idempotency_key`, and `occurred_at_ms`. `chunk_size` is 1,024-16,384
bytes; one delivery holds at most 4 media items and an attachment may
appear in it only once. It needs schema 2, `control`, `attach`, the current
task capability, current gateway ownership, a current grant on the exact
immutable source route, the delivery's result disclosure authority, and
permission to read that attachment. The delivery must be an undispatched
`user_result`; `internal_control`, a live pull claim, an
acked/accepted/verified delivery, a foreign task/principal, a foreign
route, or a principal-visible attachment from retired lineage fails without
provider or channel I/O. In one SQLite transaction stage one immutable
ordered media item and freeze one canonical manifest computed by the server
from the stored attachment bytes. The manifest fields are `media_id`, the
attachment's `content_sha256`, `size`, `media_type`, `chunk_size`,
`chunk_count`, and `chunks`; each ordered chunk has `index`, `offset`,
`size`, and a lowercase SHA-256. `manifest_id` is `sha256:<64hex>` over the
canonical UTF-8 JSON encoding of those fields with sorted object keys and no
insignificant whitespace. `media_id` is chosen by the caller but is scoped
to principal/task/delivery and immutable. Returns
`media_state:"pending_init"`, `manifest_id`, `content_sha256`, `size`,
`chunk_size`, `chunk_count`, `next_offset:0`, `ordinal`, and `replayed`. An
exact replay returns the same response; reuse with any changed delivery,
attachment, chunk size, or body fact conflicts.

`handoff.media.upload` params are `actor`, `task_id`, `capability`,
`delivery_id`, `media_id`, `owner_epoch`, `idempotency_key`, and
`occurred_at_ms`. It needs the same current owner, exact route, lineage,
attachment, and disclosure authority as stage. One call performs at most one
remote operation from the durable current state: init, the chunk starting
at `next_offset`, or finalize. It never holds a SQLite write transaction
during HTTP I/O and uses a durable claim fenced by the owner epoch. Returns
the new `media_state` (`uploading` or `uploaded`), the immutable public
manifest metadata, `next_offset`, `uploaded_chunks`, and `replayed`; it
never returns provider sessions, provider media identities, or credentials.

Upload only to the exact loopback base URL in
`OPENCLAW_HANDOFF_MEDIA_CONNECTOR_URL` with the bearer credential
`OPENCLAW_HANDOFF_MEDIA_CONNECTOR_TOKEN`. Apply the same absent,
non-loopback, credential-bearing, redirecting, and path-confused URL
rejections as the channel connector. The three provider operations are:

- init: POST `<base>/v1/uploads` with `Idempotency-Key: <upload_id>` and the
  exact JSON
  `{upload_id,media_id,manifest_id,content_sha256,size,media_type,chunk_size,chunk_count,chunks}`.
  `upload_id` is stable from the immutable
  principal/task/delivery/media/manifest facts. The 2xx response has the
  same upload/manifest identities, a non-empty `upload_session_id`,
  `next_offset:0`, and a `duplicate` boolean.
- chunk: POST `<base>/v1/upload-chunks` with
  `Idempotency-Key: <upload_id>:chunk:<index>` and the exact JSON
  `{upload_id,upload_session_id,manifest_id,index,offset,size,chunk_sha256,content_base64}`.
  The body is the frozen chunk. The 2xx response repeats the immutable
  identities and has `accepted_offset`, `next_offset`, and `duplicate`.
  Only the expected complete chunk at the exact next boundary is accepted;
  overlaps, gaps, partial advances, regressions, impossible offsets, or
  identity mismatches do not advance durable state.
- finalize: POST `<base>/v1/upload-finalize` with
  `Idempotency-Key: <upload_id>:finalize` and the exact JSON
  `{upload_id,upload_session_id,manifest_id,chunk_count}`. The 2xx response
  repeats the identities and has a non-empty `provider_media_id`,
  `complete:true`, and `duplicate`. Only that response marks `uploaded`.

`handoff.media.reconcile` has the same params as upload. It calls
`<base>/v1/upload-status` with `Idempotency-Key: <upload_id>:status` and the
exact JSON `{upload_id,manifest_id}`. The response repeats both identities
and has `state` (`initialized`, `uploading`, or `uploaded`), `next_offset`,
and the provider session identity; `uploaded` also has the provider media
identity. The gateway validates the observation against the explicit chunk
boundaries and may advance only to an observed boundary or the completed
state. It never rewinds, accepts an unknown session/manifest, or fabricates
completion. This is the recovery path after a lost init, chunk, or finalize
response. An exact replay of a committed upload/reconcile operation performs
no provider I/O.

Provider init, every chunk, finalize, and status retries use byte-stable
bodies and identities across process loss, restart, migration, compaction,
and owner takeover. After every staged item is `uploaded`,
`handoff.delivery.dispatch` sends one immutable logical text-plus-media
body. Before that it refuses without channel connector I/O. Per-route
ordering spans plain-text and media deliveries. A channel receipt failure
never causes the provider upload to restart; a failed provider upload never
allows a plain-text success for a media-bearing logical delivery.

Status and inspection expose only bounded media metadata: media and
manifest IDs, attachment content digest, size, media type, chunk
count/size, ordinal, state, next offset, and uploaded chunk count. They
never expose chunk bodies, attachment bytes, upload
session/provider-media/provider-receipt/platform attachment identities,
connector URLs/tokens, HTTP headers, or durable claims.

## Durable inbound interaction transactions

The inbound interaction methods are part of the compiled Gateway RPC
surface and use the independent evaluator-owned loopback adapter. They
never call a real provider. All interaction mutation fields are strict, and
unknown fields fail closed.

`handoff.interaction.ingest` params are `actor`, `task_id`, `capability`,
`owner_epoch`, `provider_event_id`, `correlation_delivery_id`, `kind`
(`button`, `select`, or `command`), `action_id`, `callback_token`,
`content_base64`, `media_type`, `destination_type`, `event_id`,
`idempotency_key`, and `occurred_at_ms`. The actor needs current `control`
permission, task ownership, and the exact immutable source route of the
correlated verified user-result delivery. Decode at most 8,192 bytes and
compute the body SHA-256 server-side. The `provider_event_id`, action,
body, correlated delivery, route, and callback token form one immutable
request; reusing any identity with changed facts conflicts without mutation
or adapter I/O. A duplicate provider event or exact operation replay
returns the original response. Before outbound adapter I/O, atomically
persist a prepared verification row and a stable `ingest_id` derived from
the principal/task/provider-event/route facts. POST the exact JSON body
`{ingest_id,provider_event_id,correlation_delivery_id,kind,action_id,route,content_base64,content_sha256,media_type,destination_type,callback_token,occurred_at_ms}`
to `<OPENCLAW_HANDOFF_INTERACTION_CONNECTOR_URL>/v1/interactions/verify`
with `Authorization: Bearer <OPENCLAW_HANDOFF_INTERACTION_CONNECTOR_TOKEN>`
and `Idempotency-Key: <ingest_id>`. The adapter returns the same
ingest/event identities, a non-empty provider interaction identity,
`accepted:true`, and a `duplicate` boolean. Acceptance commits
`interaction_state:"verified_pending"` and one durable interaction fact. A
lost response leaves the prepared row for `handoff.interaction.reconcile`;
it never creates another event or agent turn.

`handoff.interaction.reconcile` has the same actor/task/capability/owner
and event identity fields plus `idempotency_key` and `occurred_at_ms`. It
calls `<base>/v1/interactions/status` with the same bearer credential and
`Idempotency-Key: <ingest_id>:status`, JSON `{ingest_id,provider_event_id}`.
Only an accepted status with the same matching provider interaction
identity may complete the verification. Unknown, mismatched, empty, or
future states remain non-terminal and never imply that the callback was
processed.

`handoff.interaction.claim` params are `actor`, `task_id`, `capability`,
`interaction_id`, `claim_ms`, `idempotency_key`, and `occurred_at_ms`. It
needs current status authority on the exact source route. One live claim
exists per interaction; concurrent consumers cannot both receive it.
Pulling the claim returns the immutable interaction ID, kind/action
metadata, `content_base64`, digest/size, the correlated delivery ID,
attempt, an opaque `claim_token`, a positive `claim_epoch`, and the logical
expiry. The body is returned only through this claim path. An exact replay
returns the same claim; after expiry the same bytes are redelivered with a
higher attempt/claim epoch. Retired grants, foreign routes, replicated
capabilities, or stale task authority fail without adapter I/O.

`handoff.interaction.complete` params are `actor`, `task_id`, `capability`,
`owner_epoch`, `interaction_id`, `claim_token`, `outcome` (`handled` or
`rejected`), `reply_delivery_id`, `reply_content_base64`,
`reply_disclosure`, `reply_media_type`, `event_id`, `sequence`,
`expected_revision`, `idempotency_key`, and `occurred_at_ms`. It needs
current ownership, control permission, the live claim, and the exact
source-route lineage. In one SQLite transaction it records the immutable
completion and freezes at most one `user_result` delivery. The reply
inherits the interaction's provider, account, peer, and destination type
and carries the internal correlation metadata
`reply_to:{interaction_id,correlation_delivery_id}`; that metadata is not
caller-editable. An exact replay returns the same completion/reply
response, and a changed body or reply ID conflicts. Old owners, expired or
foreign claims, internal-control intent, or retired lineage cannot
complete.

Status/inspect expose only the interaction ID, provider event digest, kind,
action ID, correlated delivery ID, destination type, state, attempt, and
bounded counts. They never expose callback/reply bytes, callback tokens,
provider interaction/message identities, adapter URLs/tokens, claims, or
exact routes beyond the caller's own canonical channel. Compaction, repair,
migration, restart, takeover, and wall-clock rollback keep prepared,
verified, claimed, and completed interaction facts without adapter or
channel I/O or duplicate reply deliveries.

## Migration, compaction, verification, and repair

`handoff.migrate` params are `actor`, `task_id`, `capability`,
`target_schema_version` (exactly 2), `event_id`, `idempotency_key`,
`expected_revision`, `owner_epoch`, and `occurred_at_ms`. The actor needs
`control` and current ownership. In one transaction upgrade a schema-1 task
while keeping all events, operation replays, state, sequences, epochs, and
results. Initialize the missing schema-2 derived structures and advance
`migration_epoch`. Replay is identical. Termination yields either a valid
schema-1 task or a complete schema-2 task, never a mixed state. Migration
recomputes the business projection. If the supplied legacy digest does not
match, keep all facts but mark integrity invalid with the issue code
`legacy_projection_digest_mismatch`; `handoff.integrity.repair` is the only
operation that seals the rebuilt derived projection and resolves that
issue.

`handoff.compact` keeps the cycle-1 envelope and needs schema 2, `control`,
and current ownership. It uses the production handoff projection invoked
from the existing session compaction lifecycle. Advance
`compaction_generation` and return `compaction_proof` with `generation`,
`projection_digest`, `previous_generation_digest` (null for generation 0),
and `generation_digest`, all digests lowercase SHA-256. Every later proof
links to the exact previous generation digest. Replay does not advance.
Preserve completed effects/subagents, outbox receipts, attachment
references, pending continuations, lineage tombstones, migration epochs,
monotonic counters, and
pending/claimed/accepted-unverified/acked/verified/failed delivery metadata
plus staged media manifest IDs/states/cursors, without copying raw delivery
or chunk content, routes, connector receipts, upload sessions,
provider/platform media identities, platform message IDs, claim tokens, or
secrets into the proof.

`handoff.integrity.verify` params are `actor`, `task_id`, and `capability`.
Returns `valid`, `verified_generation`, `projection_digest`,
`generation_digest`, and a bounded array of stable issue codes. Recompute
the current business projection digest and verify the stored compaction
links; do not merely trust cached digest fields.

`handoff.integrity.repair` uses the compact mutation envelope plus
`repair_components`, a subset of `projection` and `compaction_proof`. It
needs schema 2, `control`, current ownership, and a failed verification
result. In one transaction rebuild only the requested derived state from
durable events, outbox receipts, attachment references, authority
tombstones, prior valid proofs, delivery receipt metadata, and durable media
manifest/upload observations. Returns `before_valid:false`,
`after_valid:true`, the repaired components, the normal mutation fields, and
a new linked compaction proof generation. It must not change the sequence
history, lower/reset any epoch, restore permissions, expose secrets,
dispatch effects, repeat provider init/chunks/finalize, submit channel
messages, acknowledge deliveries, or drop unacknowledged deliveries or
unfinished uploads. Replaying the repair is identical.

`handoff.status` and `handoff.inspect` keep their actor/task/capability
params. Status returns the bounded current projection including revision,
sequence, state, progress, owner/capability/grant/migration epochs, schema,
filtered results, visible attachment metadata, effect/outbox states,
subagent IDs, pending continuation IDs, and bounded delivery
metadata/counts. Inspect adds logical lease metadata, the integrity result,
the compaction proof, and at most 64 event headers. The encoded response is
below 65,536 bytes. Neither returns raw authority secrets,
attachment/outbox payloads, paths, sink data, or another principal's data.

## Discoverability and the agent-loop result contract

`node openclaw.mjs gateway call --help` lists every `handoff.*` method and
its exact required parameter names. The shape of a structured refusal is:

```json
{"ok":false,"error":{"code":"INVALID_REQUEST","message":"...","retryable":false,
 "details":{"missing":["actor","capability"],"invalid":[]}}}
```

`missing` and `invalid` are listed completely at once; reporting missing
fields one at a time is non-compliant. `code` is drawn from a stable set
enumerable in `--help`.

The product documents an incrementally emittable agent-loop result
contract: the driver can write `agent_result.json` right after obtaining
the first product response and refresh the same file after every step. It
contains at least `schema_version`, `case_id`, `decision`, `decision_state`
(`complete` or `partial`), `observed_actions`, `evidence`, and
`uncertainty`; `uncertainty` explicitly lists the parts not attempted or
not verified. `run_report.json` carries the same `case_id`, `status`,
artifact paths, errors, and basic usage statistics. Neither file contains
reasoning traces, grants, capabilities, claim/callback tokens, connector
credentials, body bytes, or provider/platform identities.

## Persistence and submission

Use `$OPENCLAW_STATE_DIR/state/openclaw.sqlite` and the repository
SQLite/Kysely helpers. Adding handoff tables and migrations is allowed; do
not change the shared global schema version or add sidecars. Register the
handlers in the production gateway registry and the validators in
`packages/gateway-protocol`. The methods must be usable without any
provider or channel configured.

Submit exactly:

- `solution.patch`: a UTF-8 unified diff against the supplied commit.
- `edit_report.json`: a `summary` string, an exact `changed_paths` array, a
  `production_seams` string array, and a `known_limits` string array.
- `run_report.json`: `status` (`ok` or `error`), a `commands` array of
  `{command,exit_code,duration_seconds}`, a `tests` string array, and an
  `errors` string array. Report only commands actually run.

The patch may change relevant files under `src/gateway/**`,
`src/channels/**`, `src/sessions/**`, `src/agents/**`, `src/audit/**`,
`src/cli/**`, `src/state/**`, `src/media/**`, and
`packages/gateway-protocol/**`, including co-located tests and the required
generated schema/type counterparts. It may not change lockfiles,
package/build configuration, generated build output, benchmark files,
authentication, sandbox/network policy, or unrelated paths.
`changed_paths` must exactly match the patch.
