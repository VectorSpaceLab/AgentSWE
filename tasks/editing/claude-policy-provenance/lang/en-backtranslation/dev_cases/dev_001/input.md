# Public case: workspace and shell policy

Create the Policy Provenance Ledger plugin described in the global contract.

The public fixture in `assets/` configures policy revision
`public-workspace-v1`, which allows writes below `src/`, denies `.env`
reads, allows `pytest`, and denies `rm`. Invoke the pinned hook executable
with Write, Read, and Bash payloads. The expected permissions are `allow`,
`deny`, `allow`, and `deny` respectively. Verify that every response has
the exact public receipt schema, a safe reason, and one inspector-visible
`pre` event per unique tool event. Launch eight independent hook processes
concurrently with the same allowed write. They must all return the same
receipt and leave one Write `pre` event. A same-identity write to a
different path must be denied without replacing that receipt or adding an
event.

Compare two nested shell spellings that send combined output to
`tmp/run.log`; both must be allowed with one canonical action digest. The
same nested redirection into `.env` must be denied. Then launch a bounded
queue (no more than four concurrent writers) of unique writes, duplicate
Pre/Post deliveries, and inspector reads. Keep the queue small enough that a
crashed or slow writer cannot create a lock convoy. Every process must
finish, receipts must persist exactly once, audit sequence/digest links must
be unique and contiguous, and an `--after-sequence`/`--limit` view must
return only the requested ordered window.

Run `python3 dev_cases/run_public_case.py --case dev_001 --plugin-root
plugins/policy-provenance-ledger`. Deliver the patch and reports.

The policy also declares a global reservation limit of 2, a per-session
limit of 1, and 2-second leases. Reserve one allowed Write Pre but do not
post it; the next same-session write must be denied with a rejected
reservation and a non-negative quota count. Post the held Write, verify that
its reservation becomes settled exactly once, then run `--leases` and
`--reconcile` concurrently with four fresh writes. A duplicate Post must
not occupy another slot, and inspector output must show only opaque
reservation IDs and redacted results.

The same policy requires two distinct approvals for `WebFetch`. Submit an
allowed `https://example.test/release` action: its first Pre must return
`ask` with a pending ticket and no lease. Enroll three policy principals,
the third scoped only to another host. The out-of-scope vote must fail; two
in-scope votes (including an exact replay) make the ticket ready. A bounded
concurrent pair of duplicate Pres must consume exactly once, reserve once,
and settle once. The `--approvals` view must expose the consumed ticket and
principal IDs without tokens or vote IDs.

The approval policy assigns release/security groups and requires both.
Prove that two release-group principals do not make a round ready.
Supersede that unconsumed round with one durable supersession ID, replay the
supersession, and show that the successor inherits no votes. Race a second
supersession so that one round wins, then satisfy the current round with
release plus security and consume only that round.

Finally export the complete state with a synthetic transport secret, import
it into an empty recovery state in `replace` mode, and prove that settled
receipts, tickets, leases, and verified audit evidence remain valid. An exact
import replay is idempotent; a modified envelope or wrong secret fails
without changing the recovery lineage. Inspect the bounded `--checkpoints`
view, and never print the transfer secret or sealed bearer material.

With one approved lease live, prepare an ownership handoff to
`public-recovery`. An exact prepare replay returns the same offer and fences
new source hooks and settlements. Accept into an empty target; an exact
replay returns one ack, while the standby target also refuses hooks and
settlements. Finalize at the source, replay the finalization with lost
output, activate once at the target, and settle the imported lease there.
The finalized source stays fenced. A wrong secret or modified envelope
changes neither side, and `--handoffs` exposes only bounded redacted
transition records.

Exercise durable effect delivery before the handoff. An allowed Write Pre
must create generation 1 of one effect. Claim it, then deliver the Post
while that claim is live: the Post must advance the same effect to
generation 2 and make the old claim unable to acknowledge. Claim generation
2, nack once, reclaim it under a new claim ID, and acknowledge exactly once.
Concurrent claimants must never receive the same generation. `--effects`
must correlate the receipt, effect, policy, action digest, owner, phase,
generation, attempt, and delivery ID without exposing claim tokens, inputs,
content, or results.

Then begin an online state upgrade `1` to `2` with the zero predecessor
lineage. Use `--limit 3` so that more than one authenticated advance is
needed. Append one new allowed/settled effect while the shadow builds; hooks
must continue on format 1 and commit must refuse the stale state until
catch-up includes that suffix. Commit once, replay the exact commit, and
prove that subsequent receipts use format 2. A downgrade begin and a
competing upgrade ID fail closed. The `--upgrades`, `--effects`,
checkpoint, handoff, and audit views must agree on the committed upgrade
lineage and contain no upgrade secret or staging data.

Exercise the Cycle-8 continuation before the ownership transfer. Issue an
exact-target token from the live governed session, replay the issuance, and
prove that the source cannot begin new governed work while its established
Post can settle. Race duplicate token-bearing target Pre deliveries: one
session generation activates exactly once, and later target work needs no
bearer. A wrong target and token reuse fail. Issue another token, cancel and
replay it, then show only its never-activated source restored. The bounded
continuation view must correlate authority, source/target generations,
policy, owner, format, and repair lineage without showing tokens or secrets.

Then deliberately corrupt one internal line of the evaluator-owned pinned
audit journal. The next hook must quarantine and claim no authority. Begin a
repair generation, advance it through multiple authenticated batches with
`--limit 3`, and verify the ready candidate independently as the two
configured verifier IDs. A verifier replays exactly; unknown verifiers and
wrong secrets fail. Commit and replay the repaired generation, prove that
pre-repair continuation/claim bearers are invalid, and run new
approved/reserved/effect work on the healthy owner. The repair view exposes
the abandoned high-water boundary, candidate digest, verifier coverage,
generation, and lineage without corrupted payloads or secrets.

## Cycle-11 agent-loop public world (`assets/policy_agentloop_v3.json`)

The fixtures and assertions used by `run_public_case.py` are completely
unchanged. The Cycle-11 **agent-loop public world** builds on Cycle-10 by
adding to the same policy a valid `reservation` object and an ordinary
(non-duty-constrained) `approval` object whose scope contains only
`Write`. The upper agent uses it to deliver, one by one, the action
directory outside this directory, and checks receipts field by field. It
examines the class of difficulty "five different rulings appearing in the
same policy at the same time":

- an ordinary read of `src/app.py` → `permissionDecision=allow` /
  `decision=allow`, receipt `reservation_state=reserved`;
- `src/policy-pin.json`, hit by `integrity.protected_paths`: the **read**
  is still allowed by the ordinary policy, the **write** must be refused by
  the integrity boundary after the ordinary allow result — and because
  ordinary deny constraints precede approval, this `Write` does not enter
  the approval gate even though it is in `approval.tools`; it is a direct
  `deny` with `reservation_state=rejected`;
- a `.env` read → `deny`;
- `pwd` in `commands.audit_only` → `permissionDecision=allow` and
  `decision=audit_only`, likewise occupying one reservation slot;
- `printf raw-status`, hitting the exact rewrite rule →
  `permissionDecision=allow`, `decision=rewrite`, `updatedInput` is the
  completely rewritten tool input;
- a nested interpreter such as `sh -lc 'cat .env'` → `deny`;
- a write to `src/report.txt` that falls in `approval.tools` and is
  allowed by ordinary rules → `permissionDecision=ask`,
  `decision=approval_required`, `approval_state=pending`,
  `approval_count=0`, `approval_threshold=2`, `approval_round=1`,
  `reservation_state=rejected` with a `null` reservation ID;
- a verbatim repeat of the first read must be idempotent (same receipt
  identity, same reservation, no new event); a presentation with the same
  `(session_id, tool_use_id)` but a different action must `deny` and must
  not create a new receipt identity, lease, or ticket;
- this policy still has no `ownership`/`delivery`/`repair`/`continuation`,
  so the fields of those subsystems in the receipt must take the
  "not configured" defaults prescribed by the interface document, checked
  field by field; the four fields `session_authority_id`,
  `session_generation`, `continuation_id`, and `continuation_state` are
  likewise required.

The four bounded views `--audit`, `--leases`, `--approvals`, and
`--integrity` must exit with `0`. A `reserved` lease and one `pending`
ticket are still held at the end of the case, so
`decision.completion_claim` must be `partial`.

## Cycle-12 addendum (public agent-loop world)

`dev_001` adds two more actions so that the Builder already meets, in the
dev round, the two things the hidden worlds check:

* `conflicting_content_same_identity`: the same `(session_id, tool_use_id)`
  as `dev001-approval-write`, differing only in `content`. Under Cycle-12
  item 10 the canonical action covers the whole normalized input, so this is
  an identity conflict, must `deny`, and must not reuse the already
  established approval ticket.
* `settle_audit_command`: the matching `PostToolUse` of
  `dev001-audit-cmd`. It must reuse the established receipt identity and
  `ledger_sequence`, advance the lease to `settled`, and land exactly one
  `kind="post"` event in `audit.jsonl`.

This round's oracle also parses the records actually printed by
`--audit` / `--leases` / `--approvals` / `--integrity`, checking record by
record the "exact record" field sets prescribed by the interface document,
the counts, the event chain, and whether these records agree with the
receipts the hooks returned at the time. `run_public_case.py` and its
original fixtures and assertions remain unchanged.
