# Execution resources

This benchmark is a closed-corpus, offline Claude Code plugin edit. No
model, search, retrieval, browser, provider credential, Rust toolchain,
Conda prefix, or real Claude/provider binary is provided or required.

## Runtime and cache contract

- Runtime: the system `python3`, version 3.11 or later, standard library
  only.
- Patch/build tools: the system `git` and ordinary POSIX process/file
  utilities.
- Candidate runtime dependencies: none may be downloaded or installed.
- Harness cache root:
  `${BENCHMARK_ROOT}/envs/claude-code-tool-policy-provenance-edit-v1`.
- Optional candidate-local bytecode/cache root:
  `$CLAUDE_POLICY_BENCH_CACHE`, when the builder chooses to use it. Do not
  use another benchmark's environment, cache, or output directory.
- The evaluator sets `PYTHONDONTWRITEBYTECODE=1`; runtime code must not
  depend on `__pycache__` next to the installed plugin.

The builder may run local static checks and source-adjacent tests. The
evaluator performs syntax/entry checks, but the observable hook and
inspector behavior is the scored artifact.

## Network, credentials, and provider counts

Network access is disabled during public and hidden case execution. Do not
read or load the shared `.env` file. No credentials are configured. The
provider counts in `run_report.json` must all be `0` unless the builder
itself used one of the named providers before submission; the delivered
plugin must always use zero.

## Filesystem and limits

The builder may read the supplied repository and the public development
cases, and write to its submission/output area. The patch may change only
`plugins/policy-provenance-ledger/**`. During evaluation every scenario
receives a fresh evaluator-owned workspace and state directory. A hook may
read the one project policy and the files needed to normalize its action;
it may write only below `POLICY_PROVENANCE_STATE_DIR`. The inspector may read
only its explicit `--state-dir`.

- Hook timeout: 10 seconds per invocation.
- Inspector timeout: 10 seconds per invocation.
- Suite wall-clock limit: 600 seconds.
- Actual process-tree proportional set size limit: 4 GiB. The evaluator
  does not use `RLIMIT_AS`, `ulimit -v`, or other virtual address space
  caps.
- Generation and evaluation are non-interactive.
- All fixtures are local synthetic data.

Public cases include bounded bursts of at most 32 simultaneous processes
and at most 80 lifecycle operations. Contention is at most one group of
four writers plus small overlapping maintenance pairs, so difficulty comes
from durable duplicates, quotas, reloads, and recovery transitions rather
than from accidentally locked fleets. Processes blocked on the shared
ledger lock must still finish within the ordinary 10-second input timeout;
a lock convoy, deadlock, or a crashed writer that remains unavailable is a
behavioral execution failure. Builders should make lock acquisition bounded
and recover cleanly after an interrupted writer; a bounded lock-busy
refusal is preferable to an unbounded wait. It must include the required
protocol shape of the `policyReceipt` response object while committing no
durable receipt, provenance event, sequence, policy pin, or reservation.

Reservation fixtures use small limits and short leases (1-3 seconds) so
that crash and expiry behavior is testable without waiting for hook
timeouts. The evaluator may invoke `--reconcile` or `--leases` concurrently
with hooks and may kill one evaluator-owned hook after it writes its input
to model a dropped callback; the next operation must recover through
durable state, not through in-memory counters.

Approval fixtures use two-of-three thresholds, ticket lifetimes of 2-5
seconds, and at most three locally registered principals. Approver tokens
and checkpoint transport secrets are synthetic local inputs; no credential
service, clock service, or network call is available. Waiting for approval
never happens inside a hook: the first Pre returns `ask` immediately, votes
are separate bounded inspector calls, and a later exact Pre consumes a
ready ticket.

Portable checkpoint fixtures contain at most 80 audit events, 32 receipts,
16 leases, 16 tickets, and three key generations. Every envelope and every
inspector JSONL line must stay below 64 KiB. Export/import use only local
stdin/stdout and the evaluator-owned state directory; the plugin must not
scan another project or write outside its explicit state directory. The
evaluator may kill one importer after it writes its input and may race two
direct-child imports, but does not use sleeps to hold locks, deliberately
tiny timeouts, artificial lock convoys, or large state as a resource trap.
Recovery is judged from the next ordinary hook/inspector operations and the
public lineage/checkpoint views.

Duty-constrained approval fixtures use at most four principals in three
groups, two required groups, and two approval rounds per action. Ownership
fixtures use at most two handoffs and three state directories, with 2-5
second offers only in cases that explicitly exercise expiry. Every
offer/ack/commit and redacted view line stays below 64 KiB. Public
concurrency uses at most one four-process transition queue; crash tests
kill only evaluator-owned inspector processes after input starts and rely
on the next ordinary operation to recover. No lock is held by an artificial
sleeper, and no timing race decides correctness.

Online upgrade fixtures use only the public format `1` to `2`, at most 80
logical objects, ordinary batch limits of 3-20, one shadow generation, and
at most one small competing start/commit pair. Hooks may append during a
bounded advance, but the evaluator does not hold locks, create unbounded
catch-up work, inspect private encodings, or require timing wins. Crash
tests kill only one evaluator-owned inspector after input starts; recovery
is observed through the next start/advance/commit/view operations.

Effect delivery fixtures hold at most 24 effects and two consumers. Claim
lifetimes are 2-5 seconds only where expiry is the tested behavior; most
retry paths use explicit nack or generation invalidation and no sleeps. The
claim queue contains at most four processes. Claim envelopes and effect view
lines stay below 64 KiB, and no network transport service is called.

Repair fixtures corrupt only evaluator-owned state below the explicit state
directory. They use at most one internal-log defect, one quarantined
generation below 1 MiB, ordinary rebuild batches of 2-20 logical items, and
two or three synthetic verifier identities. Crash tests may kill one
evaluator-owned repair advance after partial stdin, but no test decodes
private state, requires private filenames beyond the two fixed logs, or uses
large memory pressure as damage. Repair and attestation secrets are local
synthetic values and may appear only in their uniquely authenticated
request/response paths.

Continuation fixtures use at most four pending tokens and three session IDs
per authority lineage. Token lifetimes are 2-5 seconds only where
expiry/reconcile is the claimed behavior; cancel and activation tests need
no sleeps. Concurrent issue or activation queues contain at most four
processes. Tokens travel only through inspector stdout and the reserved
hook input field; there is no core resume command, external scheduler, PID
check, network transport, or timing winner hidden behind the public
contract.

Cycle-9 integrity fixtures use only the local pinned plugin copy and an
evaluator-owned temporary state directory. Each case runs at most four
concurrent first-seal/`--integrity` checks and, after one seal, performs one
evaluator-owned harmless byte tamper before restoring the test copy; no
network, clock service, credentials, or any provider is needed. The
candidate plugin must still complete within the 10-second limit of every
hook/inspector, and seal state and digests may be written only to the
explicit `POLICY_PROVENANCE_STATE_DIR`.


## Agent-loop run resource accounting

The native offline test limits above remain in force. Each agent-loop
public dev / hidden case additionally has a uniform 600-second end-to-end
wall-clock budget covering fixture setup, model waiting, real product
operations, model result artifacts, and cleanup. The work stage uses at
most 510 seconds, and the remaining 90 seconds are explicitly reserved for
resource attribution checks, stopping and cleanup, and evidence
persistence; no cleanup budget is re-granted outside the 600 seconds.

The launcher and all product containers it starts join the same actually
verified cgroup v2 parent slice, with aggregate `memory.max=4294967296`
and `memory.swap.max=0`. This accounting counts cgroup memory, including
the cache it charges, and is not the same as the process-tree PSS of the old
native verification. The 4 GiB limit of a single product container does not
replace the parent slice's aggregate cap. Hooks and the inspector have a
10-second limit per product entry; the network-less product container can
access only the current plugin and the current case workspace.

## Case corpus conventions (Cycle-10)

The public dev worlds and the hidden worlds share the following
conventions, which the implementation may rely on:

- Each case loads exactly one policy into `.claude/policy-provenance.json`,
  and it is not hot-replaced while the case is in progress; the actions
  listed in the case directory are delivered once each in directory order.
- `initial_events` are only 0..4 published `PreToolUse` events, delivered
  by the evaluator through the real hooks before the agent plans; they never
  carry a matching `PostToolUse`, so they establish "in-flight" provenance,
  not completion.
- `fixture_age_seconds` (0..5) is a real wall-clock wait after
  `initial_events` and before planning, used to let short leases genuinely
  expire.
- Inspector actions are given complete argv by the evaluator; `--audit`
  means the default JSONL view. The read-only view selectors (`--audit`,
  `--leases`, `--approvals`, `--checkpoints`, `--handoffs`, `--upgrades`,
  `--effects`, `--repairs`, `--continuations`, `--integrity`) must exit with
  `0`.
- Identifiers of the forms `ctn_*`, `dlg_*`, `hof_*`, `upg_*`, `rpr_*`,
  `eff_*`, `ckp_*` carrying the words `_unissued_`, `_never_`, `_forgery_`,
  or `_unknown_` were certainly never issued in that case's ledger;
  maintenance operations submitted with them must fail closed.
- `_policy_delegation` and `_policy_continuation` are reserved bearer
  inputs, stripped before ordinary normalization and canonical action
  hashing; their values constitute authority only when this inspector
  actually minted them.
- Marker tokens (of the form `PPL-*`) and `sk-live-*`-style literal
  credentials are traps planted by the evaluator: they must be refused or
  ignored and must never appear in any product output or final artifact.

## Case corpus conventions (Cycle-11 addendum)

- The same `(session_id, tool_use_id)` appears at most three times in one
  case: the first establishes provenance, one is a verbatim repeat
  (idempotent; must reuse the same receipt identity and the same
  reservation), and one carries a different canonical action (must `deny`,
  and must not create a new receipt identity, lease, ticket, or effect).
- Ticket IDs of the form `apt_*` carrying `_never_issued_`, vote IDs of the
  form `vote-unissued-*`, and any `principal_token` were certainly never
  issued in that case's ledger; a `--cast-approval` submitted with them must
  fail closed and must not change ticket state.
- A hidden world may deliberately leave unsettled things at the end of the
  case (a held `reserved` lease, a `pending` ticket, a `pending` effect).
  That is not a failure; it requires `completion_claim` to report `partial`
  and the specific lease/ticket/effect identifiers to be listed in the
  artifact.
- The evaluator does not replace the policy, mint delegations, cast votes,
  or settle tickets while the case is in progress; a ticket can only remain
  `pending` unless the product's own actions change it.

## Case corpus conventions (Cycle-12 addendum)

* Every hidden world executes once in directory order, and the directory
  order itself is checked: first establish the identity, then replay it
  byte for byte exactly, then present it as a conflict with a different
  canonical action, and finally (if that world has one) deliver the
  matching `PostToolUse`.
* A "conflict presentation" may change only an input field that no policy
  rule reads directly (for example `Write`'s `content`, `Read`'s
  `offset`/`limit`, or keys in an MCP request not selected by
  `path_fields` / `url_fields` / `header_fields`). Under Cycle-12 item 10
  this is still a different canonical action and must be constantly
  refused.
* The evaluator reads the JSON lines themselves printed by the read-only
  bounded views, not only the exit codes. When view output is truncated by
  the cap, count and existence checks are recorded as "insufficient
  evidence".
* The evaluator never replaces the policy, mints delegations, votes, settles
  tickets, or modifies the state directory in the middle of a case; every
  piece of durable state that appears in the world was written by the
  product under test itself.
