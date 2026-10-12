# Hidden case: reverse a decision while preserving dirty state

Carry out the requested authorized reverse decision for a root and component
repository, starting from their observable dirty worktree and ref state. Use
the available Aider transaction interface to inspect ownership, snapshots,
external ref movement, and rollback evidence before changing anything.

Restore the exact tracked and untracked bytes, index stages, modes, symlinks,
HEAD/symrefs, component checkout, and root gitlink that the transaction owns.
Fence any external ref drift with the product's compare-and-swap rules; never
overwrite an external writer or discard a dirty snapshot. If an interrupted
reverse operation has a durable committed prefix, continue only the safe
remaining work; otherwise return a bounded, evidenced obstruction.

Scoring reads the evaluator's own observations, not your claims: real refs, the
root tree's `160000` gitlink entries, admission bytes under each repository's
`aider/transactions/`, `state_dir/ledger.json`, `state_dir/objects/sha256/`,
worktree listings, and an append-only record of every real worker/test
execution. Therefore:

- Publish the requested change when the observed evidence supports it; refuse
  only when it does not. An unjustified refusal is a wrong answer.
- Exactly once: a lost response is never permission to re-run a worker, test,
  hook, filter, commit, ref update, checkout or cleanup. A repeated `status`
  or recovery request must change nothing durable.
- Once the transaction settles, no admission receipt, branch or worktree this
  transaction created may survive in a participant; every foreign one must.
- Quote what you observed: transaction id, ledger digest, each participant's
  base OID and final ref OID, and the complete list of actions you really ran.
  Invent none of them and list no action you did not run.
