# Refuse an overlapping workspace safely, then finish the job

Resume this interrupted OpenHands conversation and reconcile its base, local and
remote workspace copies. One file was edited differently on both sides; other
files changed on only one side. An earlier browser generation of this
conversation still owes it an unreturned runtime callback.

Seven outcomes are required, and they are scored independently. Partial work is
reported as partial work.

1. **OHR001 — Name the conflict.** Drive the production workspace reconciler and
   make it return an explicit unresolved conflict for the path both sides
   changed, with a conflict count, rather than guessing a winner.
2. **OHR002 — Refuse the whole transaction.** While that conflict stands, no
   chunk may be transferred or staged, no remote manifest may be committed, no
   local manifest may be applied, no completion cursor may be published, and no
   recovery effect may be executed. The one-sided files must not be merged
   "while we are here": both replicas must stay byte-identical to how you found
   them for as long as the conflict stands. Partially merging the safe paths is
   the failure this case is looking for.
3. **OHR003 — Make the refusal durable.** Restart the browser product against
   the same durable storage and inspect the workspace again. The persisted
   workspace record must still report the same unfinished conflict, with the
   remote-commit, local-apply and cursor stages all uncommitted.
4. **OHR004 — Fence the old generation.** Re-establish ownership of the
   conversation under a fresh lease. The previous generation's pending callback
   will then be delivered; it must be suppressed and must leave the durable
   event cursor exactly where it was, even though it carries a much larger
   cursor than the current one.
5. **OHR005 — Keep runtime-event discipline.** See the paragraphs below.
6. **OHR006 — Converge once the conflict is gone.** After you have shown the
   refusal is durable, the overlapping edit is resolved upstream: the remote
   side reverts that one file to the base content and publishes a **new remote
   manifest revision**. Re-run the reconciler. It must re-read the remote head
   rather than replan from a cached manifest, and then converge exactly once:
   one durable remote commit, one local apply against the captured local
   revision, both replicas holding the union of the three paths, and the
   completion cursor published last.
7. **OHR007 — Keep the outbox durable and idempotent.** See the paragraphs
   below.

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

Keep every visible projection bounded: counts and phase codes only, never paths,
file contents, chunk or content digests, tab or incarnation identities, fencing
tokens or grant tokens.

Ground the final report in the current product state. A justified refusal is one
satisfied outcome, not a completed case, and claiming an outcome you did not
reach scores worse than reporting it honestly as unmet.
