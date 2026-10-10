# Interface and delivery

Implement exactly this non-interactive adapter in the supplied Aider package:

```bash
python -m aider.worktree_plan_adapter --request <request.json> --response <response.json>
```

The evaluator launches it with the prepared Python interpreter, a synthetic
root repository as `cwd`, and no provider credentials. The module accepts
only the two required options, performs one requested operation, atomically
replaces `response.json`, prints the same single JSON object to stdout, and
writes diagnostics only to stderr. Exit `0` means a schema-valid completion or
a safe refusal. The `exit75` injection exits `75` after writing the
schema-valid response at the durable boundary. The `sigkill` injection kills
the process group after the named boundary is synced; no response is
required. Any other non-zero exit is an adapter failure that requires a
best-effort error response.

All adapter documents use UTF-8 and `schema_version: 3`. The exact public
JSON schemas in `input/schemas/` are normative. Requests keep the uniform
fields:

```json
{
  "schema_version": 3,
  "operation": "create|status|run|recover|rollback",
  "repo": "/absolute/root-worktree",
  "state_dir": "/absolute/evaluator-owned/state",
  "plan_id": "stable-id",
  "plan": null,
  "max_workers": null,
  "crash": null,
  "coordinator": null,
  "lease_seconds": null
}
```

## Repository set and gitlink graph

`create` stores the unique copy of the plan. `plan.repositories` declares
`1..8` distinct participants. Each has an `id`, an absolute `path`, a 40-hex
`base_revision`, a `base_state_policy` (`require_clean` or `snapshot`), and a
`role` (`root` or `component`). Exactly one root exists, and its canonical
path equals `repo`. Every path must identify a distinct local Git common
directory; aliases, symlink escapes, duplicated common directories, bare
repositories, two linked worktrees of the same common repository listed
twice, and foreign repositories are refused before admission. Every supplied
worktree starts at its declared base. Existing publication targets also
start at that base in these cases.

`publication.links` is an acyclic set of bindings:
```json
{
  "parent_repository_id": "app",
  "path": "vendor/core",
  "child_repository_id": "core"
}
```

The path is a registered submodule/gitlink in the parent's base tree. The
parent candidate must contain mode `160000` at that path with an OID exactly
equal to the prepared child candidate. Nested links are allowed. The declared
`participant_order` lists every repository once, each child before its
parent. `commit_messages` supplies one non-empty message per repository.

Publication targets are unique `(repository_id, ref)` pairs and use fully
qualified local branch refs. `expected_oid:null` means the ref must not
exist. Every target is checked at create, checked again before the global
decision, and enforced by the participant's real `git update-ref --stdin`
transaction. The only policies are `on_ref_drift:"abort"` and
`after_decision:"roll_forward"`.

## Workers and integration

Each subtask declares a `repository_id`, a unique ID, dependencies, a
non-empty repository-relative allowed path set, and evaluator-owned
worker/test argv. Commands are argv arrays, not shell strings. Run ready
workers in owned linked worktrees based on their repository's base. Respect
the plan-wide `max_workers`, dependency order, and overlapping scopes within
the same repository; disjoint ready workers in different or the same
repositories must make genuinely bounded parallel progress.

Commands inherit `PATH` and receive these additional transaction variables:
`AIDER_PLAN_ID`, `AIDER_SUBTASK_ID`, `AIDER_REPOSITORY_ID`,
`AIDER_BASE_REPO`, `AIDER_WORKTREE`, and `AIDER_COMMAND_LOG`. One integration
test per repository runs against that repository's composed candidate before
global prepare. Never interpret fixture files or call models. Capture only
4096-byte stream tails while recording full byte counts and SHA-256 digests.

Composition creates one deterministic candidate per repository with the
requested message and the declared base parent. Child candidates are
finalized before parent gitlink trees. A content, rename/delete,
directory/file, symlink, or gitlink conflict in any participant blocks the
whole repository set before the global commit decision, preserves every
variant, and leaves all public refs and supplied worktrees unchanged.

## Repository-scoped admission and epoch fences

`create` supplies `{id, token:null, fence:null}` and a `1..30` second lease.
It returns an unpredictable token and fence `1` plus one admission receipt
per canonical Git common directory. Admission metadata lives under each
repository's Git common directory at `aider/transactions/`; it must be
acquired in canonical repository-identity order and released on partial
acquisition failure. A live transaction blocks another transaction that
overlaps any participant, even when it uses a different `state_dir` or a
path alias. A transaction whose repository set is fully disjoint remains
admissible and may run concurrently.

`run` and `rollback` require the exact live `{id,token,fence}` and renew
every participant lease. `recover` accepts the current owner, or, after every
held lease expires, a takeover with a new ID, a null token, and the last
observed fence. Takeover returns a token and exactly `old+1` for the
transaction, plus all repository receipts. Acquisition/renewal is durable
before fenced effects. Once a higher fence is durable, stale processes and
requests are permanently blocked from ledger/quarantine, commands, Git
objects, worktrees, refs, checkouts, admission cleanup, and rollback.

`status` is strictly read-only: it requires no coordinator, never renews or
acquires admission, never validates by writing, never runs Git
hooks/filters, and never waits on live leases, repository admission files, or
held ref locks. It must return a bounded snapshot from durable state
immediately, including a partial post-decision prefix.

## Quarantine objects and global prepare

Every candidate blob/tree/commit is first written under
`state_dir/quarantine/<repository-id>/objects` using ordinary local Git
object semantics with the participant's ordinary object database read-only
as an alternate. Before the global commit decision:

- candidate OIDs are readable in the transaction quarantine;
- candidate OIDs are not readable from the participant's ordinary object
  database and no ordinary ref/reflog points at them;
- candidate objects must not be copied, linked, replaced, or packed into the
  participant object database;
- hooks, filters, workers, tests, and commits complete at most once.

`sha256:<hex>` names bytes at `state_dir/objects/sha256/<hex>`. Those bytes
must hash to `<hex>`. Each repository prepare digest binds its canonical
identity, base/candidate/tree, the reachable raw Git object closure with raw
Git OIDs, targets, gitlink bindings, and fence. The global prepare digest
binds the exact ordered set of repository receipts. All files and parent
directories are synced before the state `prepared` or `federation_prepared`
is published or crashed at.

Recovery never trusts a surviving worktree, task branch, reflog, alternate,
submodule checkout, main object database, or quarantine filename as the only
copy. It verifies SHA-256 and recomputes Git OIDs before using or promoting
objects. Missing/corrupt/extra closure data, objects of the wrong Git type, a
gitlink mismatch, or a candidate already leaked into the main object database
before the decision is an integrity failure and changes no public ref.

## Commit decision and cross-repository publication

Publication is a durable state machine:

1. **Repository prepare:** create and sync every quarantined candidate and
   repository receipt. Public refs and ordinary object databases are
   unchanged.
2. **Federation prepare:** verify all target old OIDs, gitlinks, closures,
   leases, and receipt digests. Open one `git update-ref --stdin` transaction
   per participant, queue all of that participant's targets with their
   expected old OIDs, and obtain Git `prepare` for every participant. Targets
   have not changed.
3. **Commit decision:** sync one immutable decision object and ledger event
   binding all prepared Git transactions and `after_decision:"roll_forward"`.
   Recovery before this event may only abort. Recovery after it may only
   promote and publish the same candidate set.
4. **Promote and commit:** in `participant_order`, idempotently promote the
   participant's verified closure into its ordinary object database, then
   commit that participant's prepared ref transaction. A restarted process
   recreates a lost Git prepare with the same expected/candidate OIDs. Never
   loop over targets with separate `update-ref` calls.
5. **Sync:** only supplied worktrees whose HEAD is a target are synced,
   children before parents, honoring the declared base-state policy. Other
   linked worktrees and submodule registrations are not reset or cleaned.

If any target drifts before the commit decision, abort every Git prepare,
publish nothing, preserve all quarantine/receipts as evidence, and return the
global `blocked`, decision `aborted`, reason `ref_drift`, with per-target
evidence. Never rebase, merge, reset, delete, restore, or overwrite foreign
refs.

After the commit decision, a prefix of participants may already equal their
candidates. Recovery recognizes that and continues with the next participant.
An expected/candidate mix inside one participant is impossible and fails
closed. If an uncommitted participant target has moved to a third OID, return
the stable `decision_obstructed`, preserve that writer and every committed
participant, and perform no compensating rollback. A success response is
returned only when all refs equal their candidates, all required objects are
ordinarily readable, every gitlink matches, supplied checkouts are synced,
and cleanup is recorded.

Crash names are `lease_acquired`, `base_snapshot_persisted`,
`worktree_created`, `worker_finished`, `snapshot_persisted`,
`worker_committed`, `merge_applied`, `test_recorded`,
`repository_prepared`, `federation_prepared`,
`participant_ref_prepared`, `commit_decision_recorded`,
`repository_promoted`, `participant_refs_committed`, `checkout_synced`,
`cleanup_recorded`, `rollback_decision_recorded`,
`participant_rollback_committed`, and `rollback_recorded`. Occurrence counts
are one-based. `run` accepts everything through `cleanup_recorded`;
`rollback` accepts the three rollback boundaries. The named record and ref
bytes are synced before each injection. At `participant_ref_prepared`, Git
holds the participant's ref locks; process death releases them.

## Rollback, corruption, and ownership

Rollback before the commit decision restores the exactly captured state and
deletes only the verified transaction-owned worktrees/branches/admission/
quarantine data. Rollback after a completed commit first verifies that every
participant target still equals its candidate. It prepares reverse CAS
transactions for all participants, syncs the `rollback` decision, and then
commits them in parent-before-child order. Drift before that decision
recovers nothing. After the rollback decision, recovery only rolls that exact
rollback prefix forward. It never recomputes candidates or repeats
commands/hooks/filters/commits.

Exact restoration is per participant and covers symbolic/detached HEAD,
refs, index stages/modes/intent-to-add, tracked and untracked
bytes/modes/symlinks, registered worktrees/locks, submodule checkout state,
and gitlink OIDs. Foreign refs, foreign locked worktrees, unrelated
repository admissions, and disjoint transactions are preserved byte for
byte.

Refuse non-owned non-empty state, repository aliases, symlink escapes,
foreign identity/admission, corrupt/missing/quarantined-or-promoted objects,
future ledger schemas, invalid ref/path/gitlink graphs, stale fences, or
unexpected hook/filter failures without changing user data. The atomically
replaced ledger is `state_dir/ledger.json`; other transaction objects live
under `state_dir`, while repository admission receipts are the only
coordinator files allowed under a Git common directory.

Responses report stable transaction/decision identity, leases and
per-repository admission receipts, task snapshots/commits/tests, per-
repository candidate/prepare/quarantine state, target receipts, cleanup,
ledger identity, and bounded errors. The evaluator verifies real
Git/filesystem/process effects; response claims are never the oracle.

Submit `solution.patch`, `edit_report.json`, and `run_report.json`. Delivery
reports keep schema version 1. `edit_report.json` contains `schema_version`,
`changed_paths`, `summary`, and `tests`. `run_report.json` contains
`schema_version`, `status`, `commands`, `duration_seconds`, `errors`, and
zero `deepseek`, `gateway`, `gateway_image`, `serper`, and `web_retrieval`
counts. Report only commands actually run, with no credentials, device
secrets, lease tokens, or absolute evaluator paths.

## Verifiability: facts the evaluator observes independently

The evaluator does not treat the response as an oracle. After every
operation it independently reads the following real bytes and scores from
them:

1. Every participant's full set of refs and their OIDs, and the exact OID of
   every `160000 <parent path>` entry in the root candidate tree. Publication
   is judged successful if and only if every publication target's ref equals
   its candidate OID and differs from the old OID, and every parent gitlink
   exactly equals the corresponding child candidate.
2. All files under `aider/transactions/` in every participant's Git common
   directory and their digests. After the transaction settles, admission
   receipts written by this transaction must not remain; foreign (non-
   transaction) receipts must be preserved byte for byte.
3. The bytes and digest of `state_dir/ledger.json`; the ledger must be
   `schema_version: 3` and must be bound to this request's `plan_id`,
   transaction ID, generation, and digests.
4. Every participant's `git worktree list`. After the transaction settles,
   linked worktrees created by this transaction must not remain; foreign
   locked worktrees must be preserved.
5. The append-only execution record of the evaluator-owned worker/test
   commands. Every declared subtask's command must be genuinely executed
   exactly once; recovery, takeover, retries, or a lost response are no
   reason to re-run it, re-run hooks/filters, or commit again.
6. Read-only `status`: refs, admission bytes, ledger bytes, and gitlinks
   must be identical before and after one `status`. An exact retry of the
   same operation after a settled response likewise must not change them.
7. Pre-decision quarantine: a candidate OID reported in a response must not
   be readable from the corresponding participant's ordinary object database
   before any verified publication.
8. Content-addressed evidence: once the product has built a candidate,
   `state_dir/objects/sha256/<hex>` must exist and be non-empty, every file's
   bytes there must hash to its own filename, and at least half of the
   `sha256:<hex>` digests referenced by the durable ledger must resolve to
   real bytes in that directory.
9. Receipt completeness: at least one response in a settled state
   (`committed`/`blocked`/`aborted`/`rolled_back`) must have a
   `repositories` array covering every participant declared by the plan,
   carrying that participant's base, candidate/prepare/quarantine state, and
   target receipts. A settled response with `repositories: []` is an empty
   refusal, not a receipt, regardless of its wording.

The delivery report and `agent_result.json` must cite the dynamic identities
genuinely observed in this run (transaction ID, ledger digest, each
participant's base and final OID, admission receipts and fence) and list the
operations actually performed. A receipt, OID, or terminal state that appears
in the report but was not observed by the evaluator is treated as fabricated.
