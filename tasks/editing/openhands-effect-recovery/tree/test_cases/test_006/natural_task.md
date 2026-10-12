# Bring the live browser back to one consistent state

Resume this OpenHands conversation through the production services: the real
conversation service, the recovery-aware event dispatcher, the persistent
cross-tab sync coordinator and the transactional workspace reconciler. The
transport is unreliable, the history stream replays the same recovery event
under different UI wrappers, a replacement browser owner can take over
mid-flight, and a remote workspace commit response is lost after the remote side
has persisted it.

Seven outcomes are required, and they are scored independently.

1. **OHR501 — Retry a lost response from the same cursor.** The first transport
   pull loses its response. The next pull must ask from exactly the same durable
   cursor, not from the advisory remote cursor and not from a skipped position.
2. **OHR502 — Every pull comes from the verified ledger cursor.** Each transport
   pull's `sinceEventCursor` must equal the durable recovery checkpoint cursor at
   the moment of the pull. Channel notices and advisory remote cursors are hints
   and never authority. Note that this conversation's durable cursor is **not**
   zero by the time you start syncing: runtime traffic has already advanced it,
   so a coordinator that hard-codes zero, or that never reads the checkpoint
   back, is measurably wrong.
3. **OHR503 — Reconcile the uncertain production commit exactly once.** When the
   remote commit response disappears, confirm the already-committed target
   through the reconcile-commit path, apply it locally exactly once against the
   captured local revision, and publish the completion cursor last. The workspace
   must end with one durable remote commit, one local apply and converged
   replicas.
4. **OHR504 — Drive the production surfaces, and apply the replay once.** Start
   the production sync coordinator, run a sync through it, and run the production
   workspace reconciler; calling the bare recovery adapter alone does not
   exercise this case. Each delivered response carries the same recovery event
   twice under two different ordinary-event wrappers. The durable event cursor
   must end as a high-water mark over the events that were delivered — never the
   sum of them — and no event id may be retained twice.
5. **OHR505 — Complete, bounded projection; no duplicate effect.** No side effect
   may be executed twice. The public workspace projection must carry every
   published stage field (`phase`, `recordRevision`, `conflictCount`,
   `remoteCommitted`, `localApplied`, `cursorCommitted`, `uploadedChunkCount`,
   `downloadedChunkCount`) and must expose no path, digest, chunk identifier,
   tab or incarnation identity, or token. An empty projection is not a safe
   projection.
6. **OHR506 — Keep runtime-event discipline.** See the paragraphs below.
7. **OHR507 — Keep the outbox durable and idempotent.** OHR505's "no side
   effect twice" only means something next to this. See the paragraphs below.

**Runtime-event discipline (measured in every case that names it).** Between
your actions this conversation receives real runtime traffic that you did not
send. A delivery that carries the current owner, lease epoch, fencing token,
run generation and expected revision is genuine: accept it and move the durable
`eventCursor` to exactly the cursor it carries. The same `eventId` delivered
again must change nothing. A delivery whose fencing token or lease epoch does
not match the live authority must be suppressed with the durable cursor
bit-identical, however large the cursor it claims. Suppressing everything is not
a safe answer: the genuine delivery must land. Read the current revision back
from the product before every authority-bearing call, because this traffic moves
it.

**The durable outbox (measured by the world, no action of yours).** The
environment enqueues one well-formed effect on this conversation under your live
authority and then settles it. All four steps are read back from your own durable
checkpoint:

* the enqueued effect must become a **durable pending effect** carrying its
  idempotency key — an outbox that only exists in memory fails here;
* its settlement must complete it **exactly once**, moving it out of pending;
* the **same `deliveryId` and idempotency key delivered again under a new
  `eventId`** must change nothing — de-duplicating on the event id alone is not
  enough;
* a settlement carrying a stale lease epoch must change nothing.

Describe the observed result and any remaining block. Calling a production
factory is not a completed recovery, and claiming an outcome the product did not
reach scores worse than reporting it as unmet.
