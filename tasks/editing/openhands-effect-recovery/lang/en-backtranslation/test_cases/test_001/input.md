# Hidden scenario: whole-transaction refusal under a mixed conflict, persistence, and the cross-generation fence

Use synthetic immutable base/local/remote manifests on shared storage. The
same path is modified differently locally and remotely, while separate
local-only and remote-only paths also exist. Return an explicit conflict plan,
and the whole transaction is refused: no chunk transfer or staging, no remote
commit, no local apply, no cursor write, and no recovery effect. The
conflict-free side paths must not be merged in passing; both replicas must
remain byte for byte in their input state. Isolate another backend with the
same conversation name.

Then restart the product and inspect the workspace again: the durable
workspace record must reproduce the same unfinished conflict as is, with the
three phase flags `remoteCommitted` / `localApplied` / `cursorCommitted` all
false.

Finally re-establish ownership under a new lease. An unreturned callback left
over from the previous generation's browser is delivered at this point; it
must be suppressed, and the durable `eventCursor` must be bit-for-bit equal
to what it was before delivery, even if that callback carries a cursor far
larger than the current value. Workspace inspection is exactly scoped and
bounded, and omits paths, all digests and chunk identifiers, tab/incarnation
identities, and fencing tokens.

Four required outcomes (OHR001 conflict named, OHR002 whole-transaction
refusal, OHR003 refusal durably reproducible, OHR004 stale-generation
callback fenced) are scored independently; OHR002 and OHR004 are safety
items, and if either is unmet the total of this case is capped at 20. The
scoring rules are in the task text and `evaluator/result_rubric.md`.

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
- The conflict in this case is resolved upstream after "the refusal has been
  proven durable": the remote reverts the conflicting path to the base content
  and publishes a **new remote manifest revision**. After that, the remote
  head must be re-read and the plan re-made, converging exactly once (one
  remote commit, one local apply, both replicas landing on the union of the
  three paths, the completion cursor published last). Committing with the
  cached remote revision is refused by the remote as a revision conflict.
- The world also drives one durable outbox probe with `enqueueEffect` + an
  `effect_completed` event (consuming no action budget): enqueue must become a
  durable pending entry; settlement must complete exactly once; re-delivering
  the same `deliveryId` / `idempotencyKey` with a new `eventId` must change
  nothing; a settlement with an expired lease epoch must be suppressed.
