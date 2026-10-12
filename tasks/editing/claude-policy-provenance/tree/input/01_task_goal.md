# Task goal: Policy Provenance Ledger plugin

Extend the supplied Claude Code plugin repository with a production-ready
**Policy Provenance Ledger** plugin. This is an Edit benchmark: add a
standard Claude Code plugin under `plugins/`, not a replacement for the CLI
or for the unavailable proprietary Claude Code core.

The plugin guards coding-agent tool use at the documented hook boundary. For
every Shell/Bash, file read/write, network, and MCP tool call it must
normalize the request, make an allow/deny/rewrite/audit-only decision from
the project policy, and record a bounded, tamper-evident audit event
without logging secrets. Its PreToolUse decision, optional rewrite, and
PostToolUse result must describe the same canonical action and an
immutable policy snapshot, even when independent hook processes overlap,
the policy file is hot-reloaded, or a maintenance process is interrupted.
MCP calls may also carry locally issued, attenuated delegations whose
permission and revocation provenance is bound to the same receipt and
policy epoch.

The target users are teams that need local, reviewable, project-scoped tool
policy without modifying Claude Code itself. The value of the feature is
not a keyword blacklist: it is an auditable capability boundary resistant
to shell indirection, path escapes, secret leakage, stale policies,
duplicate hook deliveries, concurrent append/compaction, maintenance
interruption, and malformed input, while preserving normally allowed
workflows. It must also refuse policy rollback/forks, audit-chain splicing,
delegation amplification, and revoked or wrong-epoch delegation tokens.

Version-2 projects may additionally opt into a durable reservation ledger.
The ledger issues one opaque lease per accepted PreToolUse, enforces global
and per-session in-flight quotas across independent hook processes, settles
the lease once at PostToolUse, and reclaims expired leases after a crash or
a dropped callback. Reservation state is bound to the full policy snapshot
and stays inspectable through the plugin's bounded maintenance interface;
legacy and missing-policy workflows remain unrestricted and compatible.

Version-2 projects may also require threshold approval before selected
allowed actions gain authority. The first PreToolUse creates a durable,
snapshot-bound approval ticket and returns `ask`; independently registered,
least-authority principals vote through separate authenticated inspector
calls. Only a later exact re-delivery atomically consumes the ready,
unexpired ticket and reserves/authorizes the action. Ticket expiry or
revocation, vote replay, principal rotation, policy replacement, crashes,
and concurrent consumption must never amplify authority. Approval tokens are
bearer secrets and are observable only at registration.

The state directory must also support protected portable checkpoints. An
operator can export an authenticated, redacted, bound envelope and import it
into an empty state directory or merge it into an explicitly named direct
descendant of the lineage head. Import restores durable receipts, leases,
tickets, principal generations, policy pins, keys, and verified evidence
without rollback, identity conflicts, partial visibility, or secret leakage.
An exact replay is idempotent; stale, divergent, forged, or concurrently
superseded imports fail closed. This is portability and recovery of plugin
state, not Claude conversation or proprietary core migration.

Version-2 projects may also opt into single-writer ownership. One live
state instance may hand off its full authority to one allow-listed target
through a crash-recoverable four-step protocol. Prepare offers the source
immediately; accepting it only creates a standby target; finalize makes the
source terminal; and only an authenticated finalization proof activates the
target. Abort is possible only before finalization. Retries, expiry, process
death, policy or key rotation, checkpoints, and concurrent attempts must
never leave two instances able to authorize or settle the same lifecycle.

Approval policies may also opt into separation of duties. Every registered
principal belongs to exactly one policy-defined group, and a ticket becomes
ready only when it has both the ordinary distinct-principal threshold and
at least one current vote from every required group. An operator may
supersede an unfinished approval round to clear its votes and create a
strictly action-bound successor round. Supersession replay is idempotent;
stale tickets, votes, principal generations, and supersession IDs never
authorize the replacement.

Plugin state must also support online format upgrades even though Claude
Code offers no plugin-install or plugin-update hooks. An operator starts an
authenticated shadow generation, advances it in bounded batches while hooks
keep committing to the authoritative source generation, catches up every
concurrent suffix, and commits it with one compare-and-swap pointer change.
An interrupted copy is recoverable, an aborted staging never authorizes, an
exact commit replay is idempotent, and the committed format or upgrade
lineage digest can never go backwards. Checkpoints and ownership handoffs
preserve the active format and upgrade lineage without treating an
uncommitted shadow as authoritative.

Version-2 policies may also require durable effect delivery. Every
authorized PreToolUse creates a redacted outbox effect generation and the
correlated receipt; the matching PostToolUse settles the same effect to
complete that generation rather than creating another identity.
Independent consumers claim bounded batches with exclusive expiring leases
and acknowledge or negatively acknowledge the exact generation with the
uniquely issued claim token. A later Post invalidates a stale
authorize-only claim, duplicate hooks and delivery replays settle exactly
once, a crashed consumer can retry, and queue capacity fails closed before a
new tool authorization. Effects follow key rotation, checkpoint, upgrade,
and ownership boundaries without exposing inputs, results, bearer tokens,
or delivery secrets.

Durable state must also be repairable without silently accepting a
corrupted suffix. A configured operator repair detects internal-log or
authenticated-state corruption, automatically quarantines the whole owner,
rebuilds a bounded candidate from the longest independently cross-checked
prefix, invalidates unverified bearer authority, and promotes a new repair
generation only after policy-required distinct verifiers attest the exact
candidate. Quarantine, partial rebuilds, attestation, commit replay, abort,
and restart are public state. Repair records the abandoned high-water
evidence and advances a rollback-resistant lineage; it is not a checkpoint
import, a format upgrade, or a way to create another owner.

Version-2 policies may also govern resumed-session authority. An active
session may issue one expiring continuation token naming the exact target
session. Issuance fences new governed work out of the source session; the
first target PreToolUse presenting the token atomically advances the same
session authority to one target generation. Cancel or expiry only restores
a source whose token was never activated. Concurrent issuance/activation,
token replay, policy replacement, repair, ownership transfer, restart, and
duplicate hooks must never produce two active session generations or widen
the pinned session tool policy. This is session correlation inside one live
owner, not ownership handoff or session migration.

Do not modify existing plugin behavior, package locks, build tooling,
benchmark evaluator/cases, Claude Code core installation files, or
unrelated plugins. Do not call LLMs or external policy services for
correctness.

Cycle-9 adds an opt-in integrity boundary. A version-2 policy may declare
`integrity`, which makes the plugin seal the pinned plugin files on the
first governed invocation and verify the seal before every subsequent
hook/inspector operation; at the same time, the workspace-relative paths
listed by the policy become non-writable protected paths even when ordinary
`Write`/`Bash` rules would allow them. A seal mismatch must durably enter
the `compromised` state, quarantine all authorization capabilities, and
fail closed; old authority must not be restored through restart,
compaction, key rotation, checkpoints, handoff, or policy reload. The
inspector must expose a bounded, redacted integrity view that reveals no
file contents, secrets, or private state paths. Legacy/version-2 policies
without `integrity` keep the existing behavior.

Cycle-10 turns the acceptance corpus adversarial. Policy files in the hidden
corpus may be subtly invalid, may put tools in non-enforcing modes, and may
hit allow and deny constraints at the same time; tool inputs, tool results,
and file bodies will contain instructions addressed to the agent, forged
continuation/delegation identifiers, literal credentials, and marker
tokens; the maintenance interface will receive identities, tokens,
envelopes, and lineage that the ledger never issued. Under these inputs the
plugin must give the exact answers the documentation prescribes — including
returning `allow`/`audit_only` in non-enforcing modes, still allowing and
auditing in-workspace reads under an invalid policy, blocking only writes
and not reads on protected paths, and constantly refusing conflicting
identities — rather than refusing everything or allowing everything. "Always
fail closed" and "looks reasonable" are both judged non-compliant.

Cycle-11 connects the same adversarial corpus to the stateful parts of the
ledger. Policies in the hidden corpus configure `reservation` and
duty-constrained `approval`, so every receipt must carry real reservation
and ticket provenance rather than "not configured" defaults; the same
`(session_id, tool_use_id)` is presented once as a repeat (must be
idempotent) and once as a conflict (must be constantly refused); actions
entering approval scope must take the `ask` / `approval_required` path,
acquire no lease, and perform no rewrite; actions denied by ordinary rules
never enter the approval gate at all. A world that leaves a `reserved`
lease or a `pending` ticket at the end of the case is not `complete`. All
of this is written in `02_interface_and_delivery.md` and
`03_requirements_and_constraints.md`; the hidden cases merely check them
field by field as executable assertions.

Cycle-12 connects the same adversarial corpus to the ledger's **durable
evidence**. Every world in the hidden corpus establishes the same identity
once, replays it byte for byte once, presents it as a conflict once, and in
one of those worlds adds a matching `PostToolUse`; at the end of the case
the evaluator parses the records actually printed by `--audit`, `--leases`,
`--effects`, `--approvals`, `--continuations`, and `--integrity`, checking
their exact field sets, their counts, chain continuity from the zero
anchor, and whether they agree field by field with the receipts the hooks
returned at the time. In other words: a product that only computes the
correct answer from the policy but does not durably record it will, from
this round on, be seen through in every hidden world. The canonical action
covers the whole normalized input (`Write`'s `content` and `Read`'s range
parameters included), a matching Post must produce one `post` audit event
whether or not the policy configures reservation, and a policy that
configures `continuation` does not take the "not configured" default —
these three items are written in the Cycle-12 closing rules of
`02_interface_and_delivery.md` and `03_requirements_and_constraints.md`; the
hidden cases merely check them field by field as executable assertions.
