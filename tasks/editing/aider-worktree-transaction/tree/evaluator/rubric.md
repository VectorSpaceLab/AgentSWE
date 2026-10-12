# Aider repository-set transactions — Result scoring rules (100 points)

This file is the task-local rubric for the Result axis of this task; its
dimensions correspond one-to-one to the caps in
`evaluator/result_dimensions.json` and sum to 100. It replaces the native
assertion table that was previously handed directly to the judge: that table
described assertion ids computed by the native suite itself, and the judge
neither runs the native suite nor can independently decide those ids, so it
could only award points for artifacts that "looked complete".

The judge sees only four kinds of evidence: the case task text, the
`agent_result.json` written by the model itself, the sanitized model/product
trajectory, and the native facts and oracle-comparison summary obtained by
**independent evaluator observation** (`semantic_comparison.checks` and
`independent_product_observations`). The evaluator's observations come from
real Git refs, `160000` gitlink entries, `.git/aider/transactions/` admission
bytes, `state_dir/ledger.json` bytes, the worktree list, and the append-only
execution record of the evaluator-owned worker/test commands. **Claims in
the response are not an oracle.** Any claim that conflicts with the
evaluator's observations is treated as an error.

## Scoring dimensions

### transaction_outcome_correctness (30)

Whether the world of this case was brought to the correct terminal state it
actually supports, confirmed independently by the evaluator.

- When `checks.expected_terminal_class = publication`, only
  `publication_independently_verified = true` counts as complete: every
  publication target's ref equals its candidate OID and differs from the old
  OID, and every `160000 components/<child>` entry in the root candidate tree
  exactly equals that child repository's published commit. A refusal that
  "safely published nothing" is the wrong answer in this class of case, not
  a conservative one.
- When `expected_terminal_class = fail_closed`, only
  `fail_closed_evidenced = true` (blocked/aborted/refused + every
  participant's HEAD still equals base + preserved baseline bytes unchanged +
  a ledger bound to this case + bounded `reason`/`errors`) counts as
  complete, and nothing may be published.
- When `expected_terminal_class = reverse_or_bounded_obstruction`, a
  verifiable reverse decision is required (`rollback` with a bound ledger and
  the dirty state exactly restored) or the fail-closed evidence above.
- `required_action_sequence = false`: the product was never actually driven
  to the necessary boundary of this case; this dimension scores at most 6.

### atomicity_and_ownership_integrity (25)

All or nothing, invisible before the decision, touch only your own things.

- `predecision_quarantine_respected`: before any verified publication, a
  candidate OID reported in a response must not be readable from the
  participant's ordinary object database.
- `foreign_state_preserved`: foreign writer bytes, foreign admission
  receipts, foreign locked worktrees, and the preserved baselines of disjoint
  transactions are unchanged byte for byte.
- `owned_resources_released`: after the transaction settles, no admission
  receipt of this transaction may remain under a participant's Git common
  directory, and no worktree/branch created by this transaction may remain;
  the fixture's own foreign entries must still be present.
- Any conflict, drift, crash, or corruption before the decision must leave
  all participants unpublished; after the decision only the same batch of
  candidates may be rolled forward in declared order.

### exactly_once_and_idempotency (20)

- `commands_executed_at_most_once`: each evaluator-owned worker/test command
  is genuinely executed once per subtask. Recovery, retries, or a lost
  response are no reason to re-run workers, tests, hooks, filters, or
  commits. A missing key means the product never executed any declared
  command; this dimension then scores at most 4.
- `repeat_requests_repeated_no_effect`: an exact retry of the same operation
  after a settled response must not change any ref and must not add command
  execution records.
- `read_only_status_left_no_durable_change`: `status` is strictly read-only
  — it takes no admission, writes no ledger, moves no ref, runs no
  hook/filter, and waits on no lease or ref lock.

### evidence_binding_and_provenance (15)

`agent_result.json` must write the dynamic identities genuinely observed in
this run into the report: the transaction ID, the ledger digest, each
participant's base OID and candidate/published OID, admission receipts and
fence, and the full set of actions it actually performed. Merely repeating
field names, writing only booleans, or omitting actions it performed are
major deductions in this dimension. **A receipt, OID, or terminal state that
appears in the report but was not observed by the evaluator is treated as
fabricated: this dimension scores 0 and it must be written into
`major_errors`.**

### fail_closed_honesty (10)

Whether refusals, partial completion, and unverified facts are reported
truthfully and within bounds, and whether the next step is safe. Calling the
incomplete complete, the unverified verified, or guessing the terminal state
when evidence is insufficient scores 0 in this dimension.

## Evidence-bound caps (bound by the evaluator; not new requirements)

The evaluator may issue `agentswe-result-score-caps/v1` conditions with this
case; each is bound to the byte digest of this scoring input and cites the
requirements published above. A condition judged `violated` caps the sum of
the five dimension scores; distribute points semantically within the
original dimension caps rather than first awarding an uncapped total and
trimming it afterwards. `unavailable` is neither proof of violation nor proof
of compliance. Do not derive unpublished caps on your own.

## General scoring discipline

- Do not score architecture, private symbols, source similarity, code style,
  framework choice, or the candidate's self-written tests.
- Complete fields, many actions, or a long report earn nothing by
  themselves.
- A valid rollout that never drove the product to the case boundary can score
  low or even 0.
- Infrastructure invalidity (provider/credential/broker/mount/Docker/
  evaluator/missing runtime) is reported separately and never silently
  converted into a model score of 0; this step has already been performed by
  the deterministic evaluator and the judge must not overrule it.

## Focus of each hidden case

| Case | World | Terminal class | Main focus |
|---|---|---|---|
| `test_001` | three repositories, repository-scoped admission, bounded DAG, death before decision | publication | admission scope, real concurrency, roll-forward after takeover, gitlink binding, exactly-once effects |
| `test_002` | shutdown after quarantine, loss of worktree/ref/reflog/objects | publication | closure reconstruction, no re-running workers/tests, gitlink publication, entry fidelity |
| `test_003` | durable decision committed only a prefix of participants | publication | immutable prefix, suffix roll-forward, mode/symlink/nested-gitlink fidelity, hooks/filters exactly once |
| `test_004` | component conflict, foreign writer and foreign locked worktree | fail_closed | no foreign work taken, variants preserved, disjoint transaction unaffected, stable conflict report |
| `test_005` | authorized reverse decision on top of a committed publication, dirty state in two repositories | reverse_or_bounded_obstruction | reverse CAS, exact restoration of dirty bytes/modes/symlinks/gitlinks, foreign-drift fence |
| `test_006` | decision corruption, aliases, foreign admission, post-decision obstruction | fail_closed | read-only integrity report, no rebuild and no promotion, fail-closed on identity/generation/digest mismatch |
