# Hidden scenario: recoverable workspace transfer and the crash matrix

Build a conflict-free three-way workspace plan with one local-only and one
remote-only file. The first upload response is lost after the chunk reaches
its destination; the remote commit likewise loses its response after durable
publication. A new instance must probe by digest, avoid re-transferring that
chunk, stage the verified remote chunk, and complete normally.

Inject in turn the workspace crash boundaries recorded in
`assets/fixtures.json`: after the plan, after the remote commit, after the
local apply. Each named state must be durably observable before the throw,
and the thrown error message must contain that boundary code verbatim. A new
reconciler using the same storage, replica, transport, and ledger must
continue from the durable record and finally converge the manifest and
cursor with one remote commit and one local apply. After the remote CAS but
before the ledger callback returns, `reconcileCommit` must be used instead of
executing the remote commit again; a replay after the final cursor must be
suppressed.

Then restart the product again and inspect the workspace: the record must
still be `completed`, all three phase flags true, and the ledger cursor
stopped at the published completion event. Reject escaping manifest paths
and downloads whose digest mismatches, and reject them before the remote
commit, the local apply, or the cursor publication. Keep no bytes or raw
error text in the public projection.

Four required outcomes (OHR101 transfer only the missing chunk once, OHR102
recover every crash boundary, OHR103 reconcile the uncertain commit and
converge exactly once, OHR104 completed state persists across restart) are
scored independently; OHR102 and OHR103 are safety items, and if either is
unmet the total of this case is capped at 20.

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
- Completion must survive a browser restart: a reconciler reconstructed after
  the restart must read `phase=completed` from `inspect(scope)` with all
  three phase flags true. A completion that exists only in memory does not
  count as completion (safety item).
