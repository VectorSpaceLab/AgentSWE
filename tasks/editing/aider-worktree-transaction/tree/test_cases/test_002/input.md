# Hidden case: recover a quarantined worktree transaction

Restore the requested component change after an interrupted transaction left
worktrees, Git objects, and cleanup state in an uncertain or quarantined
condition. Use the current repository observations and the available Aider
transaction interface to decide whether the candidate closure can be trusted.

Preserve exact tracked and untracked bytes, executable modes, symbolic links,
filters, refs, and component gitlinks. Rehydrate only objects and worktrees
that the durable transaction evidence supports. A corrupt or foreign repository
must remain isolated and must not be promoted. When the verified closure does
support the decision already recorded, finish the publication rather than
stopping at a diagnosis.

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
