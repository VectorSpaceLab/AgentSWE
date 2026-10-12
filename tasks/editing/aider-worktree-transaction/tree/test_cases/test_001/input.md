# Hidden case: safely admit and publish a repository change

Complete the requested bounded change across the registered root repository
and its components. Inspect the current worktrees, refs, transaction records,
and the available Aider transaction interface before acting. Some participants
have disjoint work while another participant overlaps the same scope, and one
participant may have stopped after preparing part of the transaction.

Choose the safe admission and recovery decisions from the observations you
actually receive. Preserve unrelated authors and repository bytes, fence stale
owners, and publish the component changes together with the root only when the
prepared candidates, their receipts and the root gitlinks all agree. Candidate
objects must stay unreadable from the participants' ordinary object databases
until the commit decision is durable.

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
