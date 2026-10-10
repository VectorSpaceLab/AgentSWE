# Hidden scenario: production services and browser tab convergence

Exercise neutral checkpoint-free and existing event-store behavior, then use
the real conversation and runtime services for backend-scoped schema-v3
state with claim-before-callback settlement. Keep a production callback
open, take over after lease expiry, and let the successor reconcile the
delivery to completion before the old callback is released; the late
callback rejects, but its promise must settle suppressed and remain
audit-only. Watch a tab-listened grant through the real recovery store and
status component. Matching storage events must converge the summary/UI
state; unrelated backend events and wrong authority must neither leak nor
change the watched projection. Replace the expiring watch with a new grant
for the same conversation and prove that a late matching notification from
the old watch cannot overwrite the new grant's summary. Send duplicate and
reordered recovery events through the real event store, wrapping the
duplicated recovery ID in different ordinary UI event IDs, and prove that
the optional UI side effect happens only after one accepted ledger commit.
Use the production conversation event dispatcher for REST history handing
over to a replacement WebSocket session at the durable cursor. Prove that
one history/socket echo emits one terminal chunk, that late old-session
events are fenced before every sink, that a stale close cannot stop the
replacement, and that reordered input stays suppressed. Keep the ordinary
proxied APIs. Use the production sync coordinator factory around that
dispatcher. Hold one term across a runtime takeover and a browser tab
freeze, then let the same tab ID restart under a new incarnation and accept
one successor response before the old response returns. Exercise one lost
response followed by a cursor-exact retry. Wrong-backend/high-term channel
hints cannot change the active grant store/UI projection, and notifications
or coordination records must contain no event, terminal, prompt, credential,
or authority-token data. Also use the production workspace reconciler
factory. First let a remote manifest CAS succeed while its response is lost.
Before recovery, the local apply and completion cursor stay unchanged. A new
factory instance must reconcile the already committed target through the
ledger, apply locally once, and publish the cursor last; a new dispatcher
session and the production sync coordinator must then start from that
cursor and ignore a higher advisory remote cursor. Next suspend the remote
head response, change the sync term/tab incarnation and the ledger
generation, let the successor converge, and release the old response. The
old call is stale/suppressed and cannot add a commit, apply, or cursor. The
production workspace inspection exposes only bounded counts/phase, not
paths, digests, tab/incarnation identities, or authority tokens.

Score all eight assertions independently. If production workspace commit
recovery or takeover fencing fails, apply the documented 20-point workspace
availability cap after computing the raw subtotal.

## 0920 tightening (consistent with the required outcomes of natural_task.md)

- The evaluator world delivers runtime events directly to the product between two
  model actions, **consuming no action budget**: an event whose authority is
  entirely correct must be accepted and must advance the durable `eventCursor` to
  the cursor the event itself carries; re-delivering the same `eventId` must
  change nothing; an event with a forged fencing token or a lease epoch that
  does not match the current authority must be suppressed with the cursor
  unchanged bit for bit. **Refusing everything does not count as safe**.
- These deliveries advance the revision. Before every call that carries
  authority, the current `revision` must be read back from the product.
- When sync starts, this session's durable cursor is **no longer 0** (the
  runtime events delivered by the world have advanced it). Every
  `transport.pull`'s `sinceEventCursor` must equal the durable `eventCursor`
  of the checkpoint verified at that time.
- Every response delivers the same recovery event wrapped in two different
  ordinary event wrappers. The durable `eventCursor` must be the **maximum**
  of these events, not their sum; `recentEventIds` must contain no
  duplicates.
- The public workspace projection must export all of `phase`,
  `recordRevision`, `conflictCount`, `remoteCommitted`, `localApplied`,
  `cursorCommitted`, `uploadedChunkCount`, and `downloadedChunkCount`. An
  empty projection is not a safe projection.
- The world also drives one durable outbox probe with `enqueueEffect` + an
  `effect_completed` event (consuming no action budget): enqueue must become a
  durable pending entry; settlement must complete exactly once; re-delivering
  the same `deliveryId` / `idempotencyKey` with a new `eventId` must change
  nothing; a settlement with an expired lease epoch must be suppressed.
