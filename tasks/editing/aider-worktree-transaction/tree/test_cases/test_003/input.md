# Hidden case: finish a durable participant-prefix recovery

Recover a repository-set transaction whose durable record contains only a
verified participant prefix. Inspect the actual refs, gitlinks, object closure,
filters, hooks, and transaction evidence, then complete the safe missing work
needed for the user's requested change.

Keep the committed prefix immutable and preserve exact file modes, symlinks,
binary bytes, nested gitlinks, and reference values. Use the product's recovery
and status evidence to distinguish an already committed participant from the
remaining suffix, and roll that suffix forward with the same prepared
candidates. Reject ambiguous, stale, or foreign work without changing unrelated
repository state.

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
