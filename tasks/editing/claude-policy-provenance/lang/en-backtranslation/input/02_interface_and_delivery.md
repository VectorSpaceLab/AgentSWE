# Interface and delivery

## Repository and submission

The supplied `repository/` is the pinned public Claude Code plugin
repository. Add files only below `plugins/policy-provenance-ledger/`. Submit
these three files next to the repository, not inside it:

- `solution.patch`: a non-empty UTF-8 Git unified patch that applies exactly
  once with `git apply`, whose changed paths all lie under the plugin root.
- `edit_report.json`: an object with the required top-level fields
  `schema_version` (`"1.0"`), `feature_summary` (string), `changed_paths`
  (string array), `commands_run` (string array), `compatibility_notes`
  (string array), and `limitations` (string array).
- `run_report.json`: an object with `status` (`"success"` or `"failure"`),
  `artifact_paths` (string array), `errors` (string array),
  `runtime_seconds` (non-negative number), `peak_memory_mb` (non-negative
  number), and `providers`. `providers` has non-negative integer fields
  `deepseek`, `gateway`, `gateway_image`, `serper`, and `web_retrieval`.

`changed_paths` must exactly match the patch paths. `artifact_paths` must
name the manifest, the hook configuration, the hook executable, the
inspector, the documentation, and any source-adjacent tests added by the
patch. Every artifact entry must be a unique, normalized changed path under
the pinned plugin root; absolute paths, traversal, unchanged paths, and
unrelated paths are invalid.

## Pinned plugin layout

The evaluator recognizes this public layout; private aliases are not
probed:
```text
plugins/policy-provenance-ledger/
|-- .claude-plugin/plugin.json
|-- hooks/hooks.json
|-- hooks/policy_hook.py
|-- bin/policy-ledger-inspect
`-- README.md
```

The manifest is JSON with `name` equal to `policy-provenance-ledger`. The
hook configuration uses the plugin `{"hooks": {...}}` wrapper and registers
command hooks for `PreToolUse` and `PostToolUse` with matcher `*`, using the
exact command:

```text
python3 ${CLAUDE_PLUGIN_ROOT}/hooks/policy_hook.py
```

The hook and inspector must work with the Python 3.11+ standard library
only. The inspector file must be executable and directly launchable as
follows:

```bash
plugins/policy-provenance-ledger/bin/policy-ledger-inspect \
  --state-dir <directory> [--receipt-id <id>] [--session-id <id>] \
  [--after-sequence <n>] [--limit <1..1000>] \
  [--compact | --rotate-key | --mint-delegation | \
   --revoke-delegation <id> | --reconcile | --enroll-approver | \
   --cast-approval | --rotate-approver <id> | \
   --revoke-approval <ticket-id> | --supersede-approval <ticket-id> | \
   --export-checkpoint | --import-checkpoint | --prepare-handoff | \
   --accept-handoff | --finalize-handoff | --activate-handoff | \
   --abort-handoff | --begin-upgrade | --advance-upgrade <id> | \
   --commit-upgrade | --abort-upgrade <id> | --claim-effects | \
   --ack-effect <id> | --nack-effect <id> | --begin-repair | \
   --advance-repair <id> | --verify-repair <id> | --commit-repair | \
   --abort-repair <id> | --issue-continuation | \
   --cancel-continuation <id>] \
  [--leases | --approvals | --checkpoints | --handoffs | --upgrades | \
   --effects | --repairs | --continuations]
```

It writes zero or more redacted audit event JSON objects as JSON lines to
stdout, exits `0` for a readable journal (including a recovered truncated
tail), writes safe diagnostics to stderr, and exits non-zero for invalid
arguments or an unreadable state directory. It must not require the
project, the policy, or a private storage path when `--state-dir` is
supplied.

`--compact` atomically compacts the verified active evidence before
emitting the same filtered JSONL view. `--rotate-key` atomically compacts,
rotates the active ledger and delegation authentication keys, then emits
that view. Rotation does not invalidate old receipts, remove evidence, or
revoke minted delegations. A concurrent hook or mint binds entirely to one
key generation. `--after-sequence` is exclusive, and `--limit` bounds the
ordered verified view; the default limit is `1000`, so inspector memory and
output stay bounded even for a large journal.

`--mint-delegation` reads exactly one JSON object from stdin and writes
exactly one signed delegation token JSON object to stdout. A root claim
contains `delegation_id`, `subject`, `policy_id`, `policy_epoch`, `tools`,
`path_prefixes`, and `hosts`. A child request adds `parent_token` and may
only remove tools or narrow path/host permissions; amplification fails
non-zero without printing either token. `--revoke-delegation <id>`
permanently revokes that ID and all descendants, emits no token, and then
prints the bounded audit view. All maintenance modes are mutually
exclusive. `--leases`, `--approvals`, `--checkpoints`, `--handoffs`,
`--repairs`, and `--continuations` are mutually exclusive view selectors,
not maintenance modes, and cannot be combined with a maintenance mode. They
print reservation, approval ticket, or checkpoint records instead of audit
events. A successful `--mint-delegation` response on stdout is the only
place where a newly minted token may appear, because the caller must be
able to present it on a later hook request. Tokens are opaque bearer
material everywhere else: hook responses, post summaries, audit events,
lease records, diagnostics, rotation or revocation views, and all other
inspector evidence must not copy, hash-in-a-displayed-field, or echo them.
A failed or over-broad mint prints no token.

`--enroll-approver` reads exactly one principal claim object and prints
exactly one approver token object. `--cast-approval` reads one vote
request. A successful cast, `--rotate-approver <id>`, and
`--revoke-approval <ticket-id>` print the ordinary bounded audit view;
failures print no bearer token or vote material. `--export-checkpoint`
reads one transport request and prints exactly one portable checkpoint
object. `--import-checkpoint` reads one import request and prints the
ordinary bounded audit view. The five handoff commands read one handoff
object; prepare, accept, and finalize print their exact envelopes,
activate prints the ordinary bounded audit view, and abort prints the exact
abort certificate. Their exact objects and transitions follow below.

`--begin-upgrade`, `--advance-upgrade`, `--commit-upgrade`, and
`--abort-upgrade` operate the online state format machine below. Begin,
advance, commit, and abort each print one corresponding authenticated JSON
object. `--claim-effects` prints only one claim envelope. A successful
`--ack-effect` and `--nack-effect` print the ordinary bounded audit view. A
failed upgrade or delivery operation prints no manifest, commit, claim
token, raw effect, or bearer material. `--upgrades` and `--effects` are
mutually exclusive view selectors and honor the ordinary `--limit`; the
receipt/session filters apply only to effects, and `--after-sequence`
applies to the effect's creation sequence. The five repair commands and the
two continuation commands are mutually exclusive maintenance modes. The
repair view rejects receipt/session/after-sequence filters. The
continuation view accepts `--session-id`, rejects the receipt filter, and
applies `--after-sequence` to its issued sequence.

## Hook input and output

The evaluator invokes the installed plugin hook once per event, supplies
`CLAUDE_PROJECT_DIR`, `CLAUDE_PLUGIN_ROOT`, and
`POLICY_PROVENANCE_STATE_DIR`, streams one UTF-8 JSON object on stdin, and
expects exactly one UTF-8 JSON object on stdout. The process must exit `0`
within 10 seconds. The input uses the documented Claude hook names plus
`tool_use_id`:

```json
{
  "session_id": "session-1",
  "hook_event_name": "PreToolUse",
  "cwd": "/canonical/workspace",
  "tool_name": "Write",
  "tool_use_id": "tool-1",
  "tool_input": {"file_path": "src/a.py", "content": "print(1)"}
}
```

`PostToolUse` repeats those fields and also contains `tool_response`. The
required output schema is:
```json
{
  "hookSpecificOutput": {
    "hookEventName": "PreToolUse",
    "permissionDecision": "allow",
    "permissionDecisionReason": "safe, redacted text",
    "updatedInput": null
  },
  "policyReceipt": {
    "schema_version": 1,
    "receipt_id": "pol_<opaque-id>",
    "session_id": "session-1",
    "event_id": "tool-1",
    "tool_name": "Write",
    "decision": "allow",
    "policy_id": "project-policy",
    "policy_epoch": 7,
    "policy_revision": "revision-1",
    "policy_snapshot_digest": "sha256:<64 lowercase hex>",
    "canonical_action_digest": "sha256:<64 lowercase hex>",
    "key_id": "key_0123456789abcdef",
    "ledger_sequence": 1,
    "integrity": "hmac-sha256:<64 lowercase hex>",
    "delegation_id": null,
    "delegation_parent_id": null,
    "delegation_key_id": null,
    "delegation_scope_digest": null,
    "reservation_id": "res_0123456789abcdef",
    "reservation_state": "reserved",
    "quota_remaining": 3,
    "approval_ticket_id": "apt_0123456789abcdef",
    "approval_state": "consumed",
    "approval_count": 2,
    "approval_threshold": 2,
    "approval_round": 1,
    "approval_required_groups": ["release", "security"],
    "approval_satisfied_groups": ["release", "security"],
    "owner_instance_id": "inst_0123456789abcdef",
    "owner_generation": 1,
    "ownership_state": "active",
    "handoff_id": null,
    "state_format_version": 2,
    "upgrade_id": "upg_0123456789abcdef",
    "upgrade_state": "stable",
    "effect_id": "eff_0123456789abcdef",
    "effect_generation": 1,
    "effect_state": "pending",
    "repair_id": null,
    "repair_generation": 0,
    "integrity_state": "healthy",
    "session_authority_id": "sca_0123456789abcdef",
    "session_generation": 1,
    "continuation_id": null,
    "continuation_state": "active"
  }
}
```

Every field listed in the receipt is required, and its value when "this
policy does not configure that subsystem" is fixed by this document: a
valid policy without `reservation` uses JSON `null` / `"unlimited"` / JSON
`null`; an action not in approval scope uses JSON `null` / `"not_required"`
/ JSON `null` / JSON `null` / JSON `null` / `[]` / `[]`; a policy without
`ownership` uses JSON `null` / JSON `null` / `"unmanaged"` / JSON `null`; a
policy without `delivery` uses `effect_state="not_configured"`; the format
before any commit is always `1` with `upgrade_state="stable"`. The
evaluator checks these values field by field; "roughly reasonable" does not
qualify.

`permissionDecision` and `decision` are two independent fields: `rewrite`
and `audit_only` are both rulings on `decision`, whose `permissionDecision`
is `allow`; `updatedInput` is the completely rewritten tool input if and
only if `decision` is `rewrite`, and JSON `null` in every other situation
(including the `dry_run` and `audit_only` modes).

The four fields `session_authority_id`, `session_generation`,
`continuation_id`, and `continuation_state` in the receipt are required like
the rest: when the policy has no `continuation` object (including
schema-version-1 and a missing policy) they are respectively JSON `null`,
JSON `null`, JSON `null`, and `"not_configured"`. `repair_generation` is
only required to be a non-negative integer; the evaluator fixes no value
for it.

The receipt shape of the approval path is likewise checked field by field:
when `permissionDecision` = `ask`, `decision` must be `approval_required`,
`approval_state` = `pending`, `approval_count` = `0`, `approval_threshold`
equals the policy threshold, `approval_round` = `1`, for a duty-constrained
policy `approval_required_groups` is the set of groups the policy requires
(sorted) while `approval_satisfied_groups` is `[]`, and because approval
acquires no lease, `reservation_state` = `"rejected"` and `reservation_id`
= JSON `null`. `ask` is not a rewrite: even when the action hits an exact
rewrite rule, `updatedInput` remains JSON `null`.

Cycle-12 makes explicit three things already implicit above, because the
hidden cases check them one by one:

* `canonical_action_digest` commits to **the whole tool input after the
  reserved bearer fields are stripped and after normalization**, not to the
  path or tool name within it. Under the same `(session_id, tool_use_id)`,
  any difference anywhere in the input (`Write`'s `content`, `Read`'s range
  parameters, `Bash`'s command text, fields of an MCP request not selected
  by the policy) constitutes an identity conflict, must `deny`, and must
  not reuse the established provenance; only a whole input that is
  equivalent counts as an "exact repeat".
* One matching `PostToolUse` lands exactly one durable audit event with
  `kind="post"`, **regardless of whether the policy configures
  `reservation` or `delivery`**; an exactly repeated Pre and a conflicting
  Pre land no new event; an orphan Post lands no event. That event agrees
  field by field with the receipt returned at the time on `receipt_id`,
  `decision`, `policy_id`, `policy_epoch`, `policy_revision`,
  `policy_snapshot_digest`, and `key_id`.
* In the receipts returned by an exactly repeated Pre and a matching Post,
  `receipt_id`, `policy_snapshot_digest`, `canonical_action_digest`,
  `key_id`, and `ledger_sequence` are identical to those of the first Pre;
  the evaluator only compares whether these fields are equal within the
  same case and never fixes their opaque values.

When the policy supplies a valid `continuation` object, the receipt's four
fields `session_authority_id`, `session_generation`, `continuation_id`, and
`continuation_state` do not take the "not configured" defaults: when the
session has not yet established a session-authority generation they are
`null` / `null` / `null` / `"not_created"`; when it has, they are a
non-empty `sca_` ID / a positive integer / `null` (when no to-be-activated
token has been issued) / `"active"`. `"not_configured"` belongs only to
schema-version-1, a missing policy, an invalid policy, and version-2
policies without a `continuation` object.

All listed fields are required. `permissionDecision` is `allow`, `deny`, or
`ask`; `decision` is `allow`, `deny`, `rewrite`, `audit_only`, or
`approval_required`. `updatedInput` is `null` except for `rewrite`, which
returns `allow` plus the fully rewritten tool input. PostToolUse returns
`hookEventName` set to `PostToolUse`, authorizes no new action, and repeats
the receipt identity, revision, snapshot digest, decision, canonical
digest, key ID, and the pre-event ledger sequence established by
PreToolUse. Malformed input still yields a safe JSON response and a denied
receipt when an identity can be established. Equivalent or private field
names are not accepted. For a delegated MCP action the four delegation
fields are non-empty strings that identify the verified token without
exposing it; for every other action they are JSON `null`. The three
reservation, seven approval, and four ownership fields are also always
present. For a valid policy without reservation, a schema-version-1 policy,
or a missing policy, the reservation fields are respectively JSON `null`,
`"unlimited"`, and JSON `null`. For an action outside approval scope, the
approval fields are respectively JSON `null`, `"not_required"`, JSON `null`,
JSON `null`, JSON `null`, `[]`, and `[]`. Schema-version-1, missing-policy,
and version-2 policies without `ownership` use owner ID/generation JSON
`null`, `ownership_state="unmanaged"`, and a null handoff ID. A managed
valid receipt names the durable instance and active generation. A managed
checkpoint archive uses `ownership_state="checkpoint_only"` and stays
hook-fenced. A fenced hook still returns the full schema but denies and
claims no new durable receipt or event. The six upgrade/effect fields are
likewise universal. Before any upgrade the format is `1`, the upgrade ID is
null, and the upgrade state is `stable`; while a shadow is building, the
source format stays authoritative with state `building`; after a commit,
the target format and the committed upgrade ID are reported with `stable`.
A policy without delivery uses a null ID/generation and
`effect_state="not_configured"`. A valid delivery policy uses `not_created`
for denials/asks that created no effect, and uses a non-empty effect ID, a
positive generation, and `pending`, `claimed`, or `delivered` for an
authorized lifecycle. A Pre establishes generation 1. Its matching Post
advances the same ID to generation 2, ordinarily returning it to `pending`
and invalidating any generation-1 claim. An exactly repeated response
exposes the current durable state without allocating another effect. The
seven repair/continuation fields are also universal. A healthy state has a
non-negative repair generation, a null repair ID, and
`integrity_state="healthy"`. Detected corruption changes it to
`quarantined`; an active repair reports `rebuilding` or `verifying` and a
non-empty `rpr_` ID. Schema-version-1, missing-policy, and version-2
policies without continuation use a null session authority/generation/
continuation ID and `continuation_state="not_configured"`. A configured
policy that has not yet established authority for a denied action uses
`not_created`; a live session uses `active`; issuance reports `pending`; and
the old source after activation reports `continued`. A cancelled, expired,
or repair-invalidated lineage reports `cancelled`, `expired`, or
`invalidated` in the affected fail-closed response. The continuation bearer
object is stripped before canonical action hashing and never appears in
receipts or durable views.

## Policy and state

The only policy location is
`$CLAUDE_PROJECT_DIR/.claude/policy-provenance.json`. Its exact version-1
and version-2 shapes are documented in `03_requirements_and_constraints.md`.
The evaluator always sets `POLICY_PROVENANCE_STATE_DIR` to a fresh absolute
directory; all keys, journals, locks, and indexes must stay below that
directory. When the override is absent, use
`$CLAUDE_PROJECT_DIR/.claude/policy-provenance-ledger-state`. The active
append journal is exactly `<state-dir>/audit.jsonl`; the durable compacted
generation is exactly `<state-dir>/audit.snapshot.jsonl`. Other private
key/index/lock files may coexist under the state directory but are not
evaluator inputs. Inspector output, not private indexes, is the
authoritative public view of the evidence.

Policy schema version `1` remains supported and is reported as
`policy_id="legacy"` and `policy_epoch=0`. Version `2` adds a stable
`policy_id`, a positive monotonic `policy_epoch`, and
`previous_policy_snapshot_digest`. The first accepted version-2 policy pins
its full bytes. A higher epoch is accepted only when its previous digest
names the currently pinned snapshot. A lower epoch, or different bytes for
an already accepted epoch, is a rollback/fork: the attempt fails the hook
action closed and binds its receipt to the last accepted full snapshot. A
later valid successor remains usable. Snapshot acceptance and receipt
establishment must be one process-safe transaction; racing hooks cannot
move the durable epoch backwards.

Every call is a separate process, and matching hooks may overlap, so
cross-hook identity, sequence allocation, duplicate handling, compaction,
and key rotation must be durable and process-safe. For a `(session_id,
tool_use_id)` identity, the first valid PreToolUse atomically establishes
the receipt. A completely identical Pre delivery returns the same response
and creates no new event. A conflicting Pre with the same identity but a
different canonical action is denied, returns the already established
receipt, and creates no new event. Exactly one matching Post may append; a
duplicate Post returns the same receipt without appending. Repeated runs
never overwrite the policy or project files.

`policy_snapshot_digest` is the SHA-256 of the exact stable policy bytes
used for the decision. For a missing policy it is the SHA-256 of the UTF-8
literal `missing-policy`; for an invalid policy it is the SHA-256 of the
exact bytes read. `ledger_sequence` is the positive sequence of the
receipt's Pre event and is repeated by the Post. Audit events themselves
have a unique positive `sequence`; within one receipt, the Post is later
than the Pre. `key_id` identifies the full authentication key generation
used for that receipt without exposing key material.

Every public audit event also has `previous_event_digest` and
`event_digest` (`sha256:<64 lowercase hex>`). The first event uses the
documented zero anchor `sha256:` followed by 64 zeros; every later event
names the preceding verified event digest across processes, compaction, and
key rotation. The event digest commits to the canonical JSON event with
only `event_digest` excluded. A hook that finds a torn last line may
truncate only that line while holding the state lock, then append from the
last verified anchor. An invalid internal link, a modified event, a missing
prefix, or a foreign continuation is unverified and cannot become a new
append anchor. Snapshot/active overlaps are deduplicated by event digest.

### Reservation and settlement extension

A version-2 policy may contain the optional `reservation` object:

```json
{"max_inflight": 4, "max_session_inflight": 2,
 "lease_seconds": 30, "reclaim_expired": true}
```

When the object is present all four keys are required. The two limits are
positive integers, `lease_seconds` is an integer from 1 to 3600, and
`reclaim_expired` is a boolean. An invalid reservation object makes the
full policy invalid. A policy without this object uses the legacy
unrestricted behavior: that default applies to every response under that
policy, including ordinary denials and same-identity conflict denials, all
of which report `reservation_state="unlimited"` with a null reservation ID;
this holds even when that snapshot is invalid because of other fields (for
example `policy.delivery`). `reservation_state="rejected"` appears only
under a policy that configures a `reservation` object.

For an allowed PreToolUse under a reservation policy, including `rewrite`
and `audit_only`, the hook must atomically reserve one global slot and one
session slot under the same state lock before returning. Its
`policyReceipt` adds `reservation_id` (`res_` plus opaque characters),
`reservation_state` (`reserved`), and an integer `quota_remaining`; the
quota value is the remaining global capacity immediately after that
transaction. Actions denied under a valid reservation policy, and actions
under an invalid reservation object, use `reservation_state="rejected"` and
a null reservation ID. A denial under a valid reservation policy reports
the current non-negative global capacity; an invalid policy may report it
as null. A repeated Pre returns the established reservation and consumes no
other slot. A conflicting identity is denied and cannot replace the winner.
When either quota is full, a new Pre is denied with a safe reason and a
rejected reservation state.

PostToolUse settles the matching reservation exactly once. The Post
response repeats the established receipt identity and
policy/canonical/key fields while reporting `reservation_state="settled"`
and the post-release global capacity; its redacted result is persisted in
the post audit event. A duplicate Post is idempotent. A Post without a
valid Pre, with an expired lease, or with a different identity cannot
settle or create provenance. The receipt `integrity` value may change to
authenticate the new state and quota; `receipt_id`, `reservation_id`,
session/event/tool identity, decision, policy snapshot fields, canonical
digest, key ID, ledger sequence, and delegation provenance stay the same as
the Pre. A known stale Post for an expired lease is refused and reports the
established lease as `expired`. Accepted reservations survive policy
successor reload, compaction, restart, and key rotation but remain bound to
the exact Pre snapshot. When `reclaim_expired` is true, a later Pre or
`--reconcile` atomically marks leases older than `lease_seconds` as
`expired` and releases their slots; the stale Post is refused.
Reconciliation is bounded, non-blocking, and safe to repeat. Active leases
of every accepted epoch count against the reservation limits of new
actions, so a successor cannot forget old in-flight work.

`--reconcile` inspects at most the ordinary `--limit` reservations (default
1000) in reservation-ID order and emits the normal verified audit view
after reclamation. Repeated calls may drain a larger backlog. `--leases`
emits only accepted leases, sorted by reservation ID and bounded by the
same limit. Each redacted JSONL object has exactly `schema_version` (`1`),
`reservation_id`, `session_id`, `event_id`, `receipt_id`, `policy_id`,
`policy_epoch`, `policy_snapshot_digest`, `key_id`, `state` (`reserved`,
`settled`, or `expired`), a positive `created_sequence`, a nullable positive
`settled_sequence`, a non-negative integer `quota_remaining`, a nullable
`session_authority_id`, and a nullable positive `session_generation`.
Rejected attempts are audit events, not lease records. Lease output never
contains tool input, tool responses, delegation tokens, secrets, or private
timing/key material.

With `--leases`, `--receipt-id` and `--session-id` filter the corresponding
lease fields and `--after-sequence` is exclusive on `created_sequence`.
`settled_sequence` is positive only for `settled` records and JSON `null`
for `reserved` or `expired` records. A lease's `quota_remaining` records the
global capacity immediately after that lease's most recent transition.

### Threshold approval extension

A version-2 policy may contain the exact `approval` object documented in
`03_requirements_and_constraints.md`. Policy evaluation and every ordinary
deny constraint run before approval. A policy-allowed action whose exact
tool name is in `approval.tools` is not authorized on its first PreToolUse.
Under the state transaction lock, that delivery establishes the ordinary
receipt plus one ticket, appends one `pre` event, and returns permission
`ask`, decision `approval_required`, `approval_state="pending"`, a
non-empty `apt_` ticket ID, count `0`, and the policy threshold. It creates
no reservation and reports `reservation_state="rejected"`. A completely
identical Pre delivery before the threshold returns the current
pending/ready ticket state and creates no new ticket, receipt, vote, event,
or lease. A conflicting same-identity action is denied and cannot use the
ticket.

An enrollment request contains exactly these fields:

```json
{"principal_id":"release","policy_id":"project-policy","policy_epoch":7,
 "tools":["Bash"],"path_prefixes":["src/"],"hosts":["example.test"]}
```

The principal must be named by the currently accepted approval policy. The
arrays contain unique strings and grant only the listed exact tools,
normalized workspace-relative path prefixes, and case-insensitive exact
hosts; an empty array grants no authority of that kind. A successful
enrollment creates generation 1 and returns exactly:

```json
{"schema_version":1,"principal_id":"release","generation":1,
 "policy_id":"project-policy","policy_epoch":7,"tools":["Bash"],
 "path_prefixes":["src/"],"hosts":["example.test"],
 "issued_key_id":"key_0123456789abcdef","token":"apr_<opaque>"}
```

The complete object is the bearer token. Re-enrolling an existing principal
fails. `--rotate-approver` advances its generation and revokes every
unconsumed vote from the old generation without issuing a replacement
token; the operator then enrolls the same claim to receive a
new-generation token. Ledger `--rotate-key` keeps valid approver tokens and
votes.

A cast request is exactly
`{"ticket_id":"apt_...","vote_id":"vote-opaque","principal_token":{...}}`.
The vote ID is 8..128 URL-safe characters. The token must name the ticket's
policy ID and epoch and cover the exact tool and every canonical action
path and host. Each principal has one valid vote. Repeating the same vote
ID and token is idempotent; another vote ID for that principal, or reuse of
a vote ID for another ticket, fails without changing state. Votes from
distinct valid principal generations count until consumption, ticket
expiry/revocation, or principal rotation. Reaching the threshold
automatically changes `pending` to `ready`.

The next exact Pre delivery for a ready ticket is the only consuming
operation. It re-validates the ticket expiry, revocation, principal
generations, the unchanged action identity, and the exact pinned policy
snapshot; it does not adopt a later policy. It then changes the ticket to
`consumed`, returns permission `allow` with the policy's original decision,
and atomically obtains a lease if the pinned policy has reservation. A
quota failure leaves the ticket `ready` and returns deny/rejected so that
capacity can be retried before expiry. Exactly one concurrent consumer may
append the `approval` audit event and create the lease; later duplicates
return its consumed response. The receipt ID, identity,
policy/canonical/key fields, and the original Pre `ledger_sequence` stay
fixed; the approval/reservation/count/quota/integrity fields may change.
Settlement then follows the normal rules. An expired or revoked ticket
remains denied and cannot be revived; a successor policy cannot adopt it.

`--approvals` emits records sorted by ticket ID and bounded by the ordinary
filters/limit. Each JSONL object has `schema_version` (`1`), `ticket_id`,
`session_id`, `event_id`, `receipt_id`, `tool_name`, `policy_id`,
`policy_epoch`, `policy_snapshot_digest`, `canonical_action_digest`,
`key_id`, `state` (`pending`, `ready`, `consumed`, `expired`, `revoked`, or
`superseded`), a positive `threshold`, a non-negative `approval_count`,
sorted `principal_ids`, a positive `created_sequence`, a nullable positive
`consumed_sequence`, an integer `expires_at`, a positive `approval_round`,
sorted `required_groups`, sorted `satisfied_groups`, a nullable
`predecessor_ticket_id`, a nullable `superseded_by_ticket_id`, a nullable
`supersession_id`, a nullable `session_authority_id`, and a nullable
positive `session_generation`. `--receipt-id` and `--session-id` filter the
corresponding fields; `--after-sequence` is exclusive on
`created_sequence`. Tokens, vote IDs, transport secrets, raw
inputs/results, and private key material are absent.

When the approval object also has `principal_groups` and
`required_groups`, it is a duty-constrained policy. `principal_groups` maps
every enrolled principal to exactly one non-empty group; every required
group must appear in that mapping. Enrollment derives the group from the
policy rather than accepting a caller-chosen group, and the token response
adds the exact string field `group`. A valid vote counts once for its
principal and satisfies only that principal's group. Ready requires the
threshold and every required group. Principal rotation removes that
principal and recounts the count and satisfied groups.

`--supersede-approval <ticket-id>` reads exactly
`{"supersession_id":"sup_<8..128 URL-safe>","expected_approval_round":1}`.
It is valid only for a non-terminal pending or ready duty-constrained
ticket. It atomically marks the old ticket `superseded`, creates a pending
successor with a new ticket ID, the same receipt/action/policy/key identity,
an incremented round, zero votes/groups, and reciprocal
predecessor/successor fields, then prints the ordinary audit view. The same
supersession ID/request is idempotent; reuse with another ticket or round
fails. Old votes never transfer to the successor. Concurrent supersessions
have one winner. A later exact Pre returns the current round, which can be
consumed only after the new round independently reaches the
duty-constrained ready state.

### Fenced portable checkpoints

An export request is exactly
`{"transfer_id":"mig_<8..128 URL-safe characters>","transfer_secret":"<at least 32 UTF-8 characters>"}`.
While holding the bounded state lock, export verifies the full evidence
chain, expires tickets/leases, captures one complete logical state, and
advances the instance's positive export sequence and lineage. It returns
exactly one object with these fields:
```json
{"schema_version":1,"kind":"policy-provenance-checkpoint",
 "transfer_id":"mig_example","origin_instance_id":"inst_<opaque>",
 "export_sequence":1,"base_checkpoint_digest":null,"created_at":1700000000,
 "policy_id":"project-policy","policy_epoch":7,
 "policy_snapshot_digest":"sha256:<64 lowercase hex>",
 "audit_event_count":12,"receipt_count":6,"lease_count":2,
 "approval_ticket_count":1,"handoff_count":0,"effect_count":3,"upgrade_count":1,
 "repair_count":0,"continuation_count":1,"repair_generation":0,
 "repair_lineage_digest":"sha256:<64 lowercase hex>",
 "state_format_version":2,
 "upgrade_lineage_digest":"sha256:<64 lowercase hex>",
 "owner_instance_id":"inst_<opaque>","owner_generation":1,
 "ownership_state":"active","sealed_state":"<opaque authenticated payload>",
 "checkpoint_digest":"sha256:<64 lowercase hex>",
 "integrity":"hmac-sha256:<64 lowercase hex>"}
```

`checkpoint_digest` commits to the canonical JSON excluding
`checkpoint_digest` and `integrity`; `integrity` authenticates the
canonical JSON excluding only `integrity` with the transport secret. The
opaque sealed state contains the verified state needed to restore
historical keys, receipts, leases, tickets, principal generations, policy
pins, checkpoint lineage, and redacted audit evidence. Neither the
checkpoint nor any local file/output contains the transport secret or
plaintext bearer/key material. Export appends one `checkpoint_export` event
only after the envelope and lineage are durably committed. A failed export
emits no checkpoint.

An import request is exactly
`{"checkpoint":{...},"transfer_secret":"...","mode":"replace",
"expected_lineage_digest":null}` or uses mode `merge` and a digest.
`replace` is allowed only for a logically empty state directory and a null
expected lineage. `merge` requires the supplied expected digest to equal
the target lineage at commit and the incoming `base_checkpoint_digest` to
equal it. The checkpoint must authenticate, its public metadata must match
the sealed state, its origin export sequence must exceed the highest
sequence accepted for that origin, its policy must be identical to or a
valid successor of the local pin, and every imported identity/generation
must be identical or non-conflicting. All checks precede mutation. A stale,
divergent, rollback, modified, wrong-secret, identity-conflicting, or
wrong-policy import exits non-zero and leaves the authoritative state
unchanged.

A successful replace restores the captured state and appends a local
`checkpoint_import` continuation. For an ownership-managed checkpoint it
sets the destination to `checkpoint_only`: verified evidence and redacted
views may be inspected, exported, replayed, or merged, but hooks and all
authority maintenance remain fenced. Live ownership transfers only through
the handoff protocol below; a checkpoint import never clones a live owner.
A successful merge keeps local completed evidence and objects, imports the
descendant suffix and live objects, advances the lineage to the incoming
checkpoint digest, and appends one import event committed to the origin
tail/count. An exact replay of the same accepted checkpoint is successfully
idempotent with no new event; a different checkpoint at an already seen
origin sequence is refused. Concurrent imports use the lineage digest as a
compare-and-swap fence, so at most one direct child wins.

Import stages and fsyncs a complete generation before atomically replacing
the durable generation pointer. Death before the pointer replacement exposes
no staged state; death after it exposes all of it. The next bounded
operation discards unreferenced staging or completes cleanup. No hook may
authorize from staging.

`--checkpoints` emits redacted records with exactly `schema_version`,
`checkpoint_digest`, `origin_instance_id`, `origin_export_sequence`, a
nullable `base_checkpoint_digest`, a nullable positive
`imported_at_sequence`, `policy_id`, `policy_epoch`,
`policy_snapshot_digest`, `audit_event_count`, `receipt_count`,
`lease_count`, `approval_ticket_count`, `handoff_count`, `effect_count`,
`upgrade_count`, `repair_count`, `continuation_count`, a non-negative
`repair_generation`, `repair_lineage_digest`, a positive
`state_format_version`, `upgrade_lineage_digest`, a nullable
`owner_instance_id`, a nullable positive `owner_generation`,
`ownership_state` (`unmanaged`, `active`, `prepared`, `accepted`,
`finalized`, or `checkpoint_only`), `state` (`exported` or `imported`), and
`lineage_digest`. Records are sorted by checkpoint digest and bounded by
`--limit`; `--after-sequence` applies to the origin export sequence.
Receipt/session filters are invalid for this view.

### Crash-recoverable ownership handoff

A version-2 policy with `ownership` creates a durable local instance ID and
a positive owner generation on first accepted use. The object is
`{"handoff_seconds":30,"allowed_targets":["build-b","recovery-c"]}`: the
lifetime is an integer `2..3600`, targets are unique non-empty 1..128-byte
URL-safe strings, and both keys are required. Invalid ownership invalidates
the full policy. A managed instance authorizes only in the `active` state.

`--prepare-handoff` reads exactly
`{"handoff_id":"hof_<8..128 URL-safe>","target_instance_id":"build-b",
"handoff_secret":"<at least 32 UTF-8 characters>"}`. Under the state lock
it verifies the chain and live objects, appends `handoff_prepare`, captures
and seals the complete logical state, and atomically changes the source
from `active` to `prepared` before returning this exact offer schema:

```json
{"schema_version":1,"kind":"policy-provenance-handoff-offer",
 "handoff_id":"hof_example","source_instance_id":"inst_<opaque>",
 "target_instance_id":"build-b","source_generation":1,
 "target_generation":2,"created_at":1700000000,"expires_at":1700000030,
 "policy_id":"project-policy","policy_epoch":7,
 "policy_snapshot_digest":"sha256:<64 lowercase hex>",
 "audit_event_count":12,"receipt_count":6,"lease_count":2,
 "approval_ticket_count":1,"effect_count":3,"upgrade_count":1,
 "repair_count":0,"continuation_count":1,"repair_generation":0,
 "repair_lineage_digest":"sha256:<64 lowercase hex>",
 "state_format_version":2,"upgrade_lineage_digest":"sha256:<64 lowercase hex>",
 "sealed_state":"<opaque authenticated payload>",
 "offer_digest":"sha256:<64 lowercase hex>",
 "integrity":"hmac-sha256:<64 lowercase hex>"}
```

The source is fenced immediately: every hook, checkpoint/key/delegation/
approval mutation, and new handoff fails without durable authority once
prepared. An exact prepare replay returns the same offer. A second handoff
fails. Before finalization, `--abort-handoff` on the source reads
`{"handoff_id":"hof_...","handoff_secret":"..."}` and atomically restores
the active state, appends `handoff_abort`, and prints an authenticated abort
certificate. An exact replay returns that certificate. An expired,
unfinalized prepare is equivalently aborted by the next source
`--reconcile` or hook before new authority; an accepted standby never
authorizes from an expired offer.

`--accept-handoff` on a logically empty target reads exactly
`{"offer":{...},"handoff_secret":"..."}`. It authenticates before decoding,
cross-checks all public/sealed metadata and the target allowlist,
stages/fsyncs a complete generation, atomically installs it in the
`accepted` standby state, appends `handoff_accept`, and returns exactly:
```json
{"schema_version":1,"kind":"policy-provenance-handoff-ack",
 "handoff_id":"hof_example","source_instance_id":"inst_<opaque>",
 "target_instance_id":"build-b","source_generation":1,
 "target_generation":2,"offer_digest":"sha256:<64 lowercase hex>",
 "accepted_at":1700000001,"ack_digest":"sha256:<64 lowercase hex>",
 "integrity":"hmac-sha256:<64 lowercase hex>"}
```

An accepted target is fenced and cannot settle imported leases. An exact
accept replay returns the same ack. A killed partial accept exposes no
state; death after its generation pointer exposes the complete standby and
replay recovers it.

`--finalize-handoff` on the prepared source reads
`{"ack":{...},"handoff_secret":"..."}`. It verifies the exact
offer/target/generation identities, atomically marks the source
`finalized` and permanently unable to authorize, appends
`handoff_finalize`, and returns one commit:

```json
{"schema_version":1,"kind":"policy-provenance-handoff-commit",
 "handoff_id":"hof_example","source_instance_id":"inst_<opaque>",
 "target_instance_id":"build-b","source_generation":1,
 "target_generation":2,"offer_digest":"sha256:<64 lowercase hex>",
 "ack_digest":"sha256:<64 lowercase hex>","finalized_at":1700000002,
 "commit_digest":"sha256:<64 lowercase hex>",
 "integrity":"hmac-sha256:<64 lowercase hex>"}
```

A finalize replay returns the same commit; abort fails after finalization.
`--activate-handoff` on the accepted target reads
`{"commit":{...},"handoff_secret":"..."}`, verifies the complete chain of
envelopes, atomically changes it to active at `target_generation`, appends
`handoff_activate`, and prints the bounded audit view. An exact activate
replay is successfully idempotent. Only after activation may imported
lifecycles be authorized or settled. A wrong secret, target, generation, or
policy, a modified envelope, a stale/expired offer, a concurrent competitor,
or an out-of-order transition fails without mutation or secret output.

The abort certificate has exactly `schema_version`, `kind`
(`policy-provenance-handoff-abort`), `handoff_id`, `source_instance_id`,
`target_instance_id`, `source_generation`, `target_generation`,
`offer_digest`, `aborted_at`, `abort_digest`, and `integrity`. Presenting
`{"abort":{...},"handoff_secret":"..."}` to `--abort-handoff` on the
accepted target atomically discards the standby authority and prints the
ordinary bounded audit view; an exact replay succeeds. It can never revoke
a finalized or activated handoff.

`--handoffs` emits exact redacted records sorted by handoff ID:
`schema_version`, `handoff_id`, `source_instance_id`, `target_instance_id`,
a positive `source_generation`, a positive `target_generation`, `state`
(`prepared`, `accepted`, `finalized`, `activated`, or `aborted`),
`offer_digest`, a nullable `ack_digest`, a nullable `commit_digest`, a
positive `created_sequence`, a positive `transition_sequence`, `policy_id`,
`policy_epoch`, `policy_snapshot_digest`, `expires_at`, `effect_count`,
`upgrade_count`, `state_format_version`, `upgrade_lineage_digest`,
`repair_count`, `continuation_count`, `repair_generation`, and
`repair_lineage_digest`. The ordinary limit applies;
receipt/session/after-sequence filters are invalid. Secrets, sealed state,
bearer tokens, inputs, and results are absent. Ledger key rotation keeps
committed envelopes only while active; policy successors are refused while
prepared/accepted/finalized and resume normally after abort or activation.

### Online state format upgrades

Every state starts at the public `state_format_version=1` with the zero
upgrade lineage digest (`sha256:` plus 64 zeros). Supported targets are
positive integers, and this contract requires the implementation to
support the exact forward transition `1` to `2`. Only `current+1` is valid:
a downgrade, a skipped version, reuse of an upgrade ID, or a predecessor
lineage mismatch fails before mutation. The active source remains the sole
authority while the shadow is `building`; hooks continue normally, and every
committed source transition is available for later bounded catch-up. A
shadow is neither a checkpoint nor an owner and can never answer hooks,
settle leases, claim effects, or become an append anchor before commit.

`--begin-upgrade` reads exactly:

```json
{"upgrade_id":"upg_<8..128 URL-safe>","target_format_version":2,
 "expected_upgrade_lineage_digest":"sha256:<64 lowercase hex>",
 "upgrade_secret":"<at least 32 UTF-8 characters>"}
```

Under the state lock it verifies the complete active generation, records a
source digest and base audit sequence, creates recoverable staging below
the state directory, appends `upgrade_begin`, and returns exactly:

```json
{"schema_version":1,"kind":"policy-provenance-upgrade-manifest",
 "upgrade_id":"upg_example","source_format_version":1,
 "target_format_version":2,"base_sequence":12,"cursor_sequence":0,
 "source_state_digest":"sha256:<64 lowercase hex>",
 "expected_upgrade_lineage_digest":"sha256:<64 lowercase hex>",
 "repair_generation":0,"repair_lineage_digest":"sha256:<64 lowercase hex>",
 "continuation_count":1,
 "started_at":1700000000,"upgrade_digest":"sha256:<64 lowercase hex>",
 "integrity":"hmac-sha256:<64 lowercase hex>"}
```

`upgrade_digest` commits to the canonical JSON excluding it and
`integrity`; `integrity` authenticates the object with the upgrade secret,
excluding only itself. The secret is never stored or displayed. An exact
begin replay returns the same manifest. Concurrent different begins have
one winner.

`--advance-upgrade <id>` reads exactly
`{"manifest":{...},"upgrade_secret":"...","expected_cursor_sequence":0}`.
It authenticates the manifest before staging, verifies the expected cursor,
and copies or verifies at most the ordinary `--limit` source items in a
deterministic logical object order. It then atomically publishes the staged
progress and returns exactly:
```json
{"schema_version":1,"kind":"policy-provenance-upgrade-status",
 "upgrade_id":"upg_example","source_format_version":1,
 "target_format_version":2,"base_sequence":12,"cursor_sequence":5,
 "catchup_sequence":14,"remaining_items":7,"ready_to_commit":false,
 "repair_generation":0,"repair_lineage_digest":"sha256:<64 lowercase hex>",
 "continuation_count":1,
 "state":"building","upgrade_digest":"sha256:<64 lowercase hex>",
 "integrity":"hmac-sha256:<64 lowercase hex>"}
```

The cursor is monotonic. An exact advance replay at a committed cursor is
successfully idempotent and returns the current status; a stale or future
cursor fails without moving it. Death before progress publication exposes
no partial batch, and death after publication exposes the whole batch. The
next advance discards unreferenced temporaries and continues. Hooks that
commit while copying extend `catchup_sequence`; repeated bounded advances
must eventually report `remaining_items=0` and `ready_to_commit=true` after
a quiet boundary. The `upgrade_advance` audit event is appended only for
newly published progress.

`--commit-upgrade` reads exactly
`{"status":{...},"upgrade_secret":"..."}`. Under one final state lock it
authenticates the status, requires it to be the current ready state,
verifies the shadow against the complete source, including every effect,
receipt, lease, ticket, key, checkpoint, handoff, policy pin, chain event,
and concurrent suffix, and compares the predecessor lineage. If the source
has advanced since the status, the commit fails non-zero and requires a
later advance/retry. Otherwise it fsyncs the complete target generation,
atomically replaces one active generation pointer, appends
`upgrade_commit` in format 2, and returns exactly:

```json
{"schema_version":1,"kind":"policy-provenance-upgrade-commit",
 "upgrade_id":"upg_example","source_format_version":1,
 "target_format_version":2,
 "source_state_digest":"sha256:<64 lowercase hex>",
 "previous_upgrade_lineage_digest":"sha256:<64 lowercase hex>",
 "repair_generation":0,"repair_lineage_digest":"sha256:<64 lowercase hex>",
 "continuation_count":1,
 "committed_at":1700000002,"commit_sequence":15,
 "upgrade_digest":"sha256:<64 lowercase hex>",
 "upgrade_lineage_digest":"sha256:<64 lowercase hex>",
 "integrity":"hmac-sha256:<64 lowercase hex>"}
```

The lineage digest commits to the previous lineage, the upgrade digest, the
target format, the source state digest, and the commit sequence. An exact
commit replay returns the same commit. Death before the pointer replacement
leaves format 1 authoritative; death after the replacement leaves all
format-2 state authoritative and a replay recovers the commit. No operation
may select staging based on directory presence.

`--abort-upgrade <id>` reads exactly `{"upgrade_secret":"..."}`. It is valid
only while building, atomically makes the staging unreachable, appends
`upgrade_abort`, and returns the exact fields `schema_version`, `kind`
(`policy-provenance-upgrade-abort`), `upgrade_id`, `source_format_version`,
`target_format_version`, `aborted_at`, `abort_digest`, and `integrity`. An
exact replay returns the certificate. Abort fails after commit.

`--upgrades` emits exact records sorted by upgrade ID with
`schema_version`, `upgrade_id`, a positive `source_format_version`, a
positive `target_format_version`, `state` (`building`, `committed`, or
`aborted`), a positive `base_sequence`, a non-negative `cursor_sequence`, a
non-negative `catchup_sequence`, `source_state_digest`,
`previous_upgrade_lineage_digest`, a nullable `upgrade_lineage_digest`, a
positive `created_sequence`, a positive `transition_sequence`, a
non-negative `repair_generation`, `repair_lineage_digest`, and a
non-negative `continuation_count`. It never exposes manifests, secrets,
temporary paths, private formats, keys, or sealed state.
Receipt/session/after-sequence filters are all invalid. Compaction and key
rotation may run while building but must be included in catch-up.
Checkpoint export and ownership prepare may run only when no upgrade is
building. Checkpoint import restores the committed upgrade lineage but
never staging.

### Durable effect delivery

A version-2 policy may include exactly:
```json
{"delivery":{"claim_seconds":3,"max_pending":64,
             "require_completion":true}}
```

`claim_seconds` is an integer `2..3600`, `max_pending` is an integer
`1..4096`, and `require_completion` is a boolean; booleans are never
integers. An invalid delivery object invalidates the full policy under the
same safe-read rules as an invalid reservation/approval/ownership.
Missing-policy, schema-version-1, and version-2 policies without delivery
produce no effects.

After the policy, approval, quota, and ownership checks succeed, an
authorized PreToolUse atomically creates one `eff_` effect at generation 1
and phase `authorized` in the same receipt transaction. Complete redacted
effects count against `max_pending` until their newest generation is
delivered. When capacity is full, a new action is denied before
authorization, creates no receipt effect/lease, and reports
`effect_state="not_created"`. Duplicate and conflicting Pre rules are
unchanged and produce no additional effect.

The matching PostToolUse advances the same effect to generation 2 and phase
`completed`, binds the redacted SHA-256 result digest, and returns the
effect to pending delivery even when generation 1 was claimed or delivered.
Any generation-1 claim becomes stale immediately and cannot acknowledge
generation 2. A duplicate Post is idempotent. A missing/stale Post creates
no completion generation; its authorized generation remains independently
deliverable. When `require_completion=false`, an acknowledgement of
generation 1 may finish the effect unless a Post arrives later. When true,
generation 1 may be acknowledged as observed but remains in the pending
lifecycle until generation 2 is delivered or the bounding lease expires;
reconciliation then advances it to phase `expired` with a positive next
generation, pending delivery. No raw input, result, reason, token, header,
URL credential, or private timing is retained.

`--claim-effects` reads exactly:
```json
{"consumer_id":"consumer-a","claim_id":"clm_<8..128 URL-safe>",
 "max_items":10}
```

The consumer is a non-empty 1..128-byte URL-safe string; the maximum item
count is a positive integer no greater than the ordinary `--limit`. It first
expires claim leases, then atomically selects ordered pending effects and
exclusively claims their current generations for the policy
`claim_seconds`. It returns exactly:

```json
{"schema_version":1,"kind":"policy-provenance-effect-claim",
 "consumer_id":"consumer-a","claim_id":"clm_example",
 "claimed_at":1700000000,"expires_at":1700000003,"items":[{...}],
 "claim_token":"efc_<opaque>","claim_digest":"sha256:<64 lowercase hex>",
 "integrity":"hmac-sha256:<64 lowercase hex>"}
```

Items use the exact redacted effect view schema below. The complete
envelope is the only place where the claim token may appear. An exact
replay of the same claim while live returns the same envelope; a claim ID
cannot be reused after expiry or by another consumer/request. Concurrent
consumers cannot claim one generation. An active owner may claim; a
prepared/finalized source, an accepted target, and a checkpoint-only
archive cannot.

`--ack-effect <effect-id>` reads exactly
`{"claim_id":"clm_...","claim_token":"efc_...",
"expected_effect_generation":2,"delivery_id":"dlv_<8..128 URL-safe>"}`. It
verifies the live claim and the exact generation, atomically sets the
generation `delivered`, records the globally unique delivery ID, appends
`effect_ack`, and prints the bounded audit view. An exact replay is
idempotent. Reusing a delivery ID elsewhere, a stale generation/claim, an
expired claim, a wrong consumer secret, or a wrong owner fails without
mutation.

`--nack-effect <effect-id>` reads the same object with `delivery_id`
replaced by `retry_id` (`rty_<8..128 URL-safe>`). It atomically releases
only the exact generation to pending, increments its attempt count, appends
`effect_nack`, and prints the bounded audit view. An exact retry-ID replay
is idempotent; reuse elsewhere fails. A consumer crash needs no command:
claim expiry makes the same generation claimable again without changing its
identity or generation.

`--effects` emits exact records sorted by effect ID with `schema_version`,
`effect_id`, `receipt_id`, `session_id`, `event_id`, `phase` (`authorized`,
`completed`, or `expired`), `policy_id`, `policy_epoch`,
`policy_snapshot_digest`, `canonical_action_digest`, a nullable
`owner_instance_id`, a nullable positive `owner_generation`, a positive
`effect_generation`, `state` (`pending`, `claimed`, or `delivered`), a
nullable `claim_id`, a nullable `consumer_id`, a non-negative
`attempt_count`, a positive `created_sequence`, a positive
`updated_sequence`, a nullable `delivery_id`, a nullable `outcome_digest`, a
nullable `session_authority_id`, and a nullable positive
`session_generation`. Claim tokens, retry IDs, raw actions/results, secrets,
timestamps, and private storage names are absent. Effects survive restart,
compaction, key rotation, committed upgrades, and checkpoint replace/merge.
Ownership handoff transfers effects exactly once; claims are invalidated at
prepare, and only the activated target may create new claims.

### Quarantined integrity repair

Every state starts at repair generation `0` with the repair lineage digest
`sha256:` followed by 64 zeros. A successful repair is the only operation
that increments that generation or changes that lineage.

A version-2 policy may include exactly:
```json
{"repair":{"max_quarantine_bytes":1048576,"required_verifiers":2,
           "verifiers":["operations","audit"]}}
```

`max_quarantine_bytes` is an integer `65536..16777216`,
`required_verifiers` is an integer `2..8`, and `verifiers` is an array of
`required_verifiers..16` unique non-empty 1..128-byte URL-safe IDs.
Booleans are not integers. A missing or invalid key invalidates the whole
policy. Repair is not configured for missing-policy/schema-version-1 state.
At most one repair may be non-terminal. Quarantined bytes, the candidate,
and all progress must stay below `max_quarantine_bytes`; on reaching the
bound, the owner is quarantined and non-zero is returned without publishing
a candidate.

An internal invalid JSON line, a digest/link break, an authenticated
auxiliary-state failure, a missing committed prefix, or a
generation/pointer mismatch is corruption. A torn final line remains the
ordinary bounded tail recovery. The first hook or inspector operation with
authority atomically detects corruption, records the damage digest when
possible, and enters `quarantined`. Every hook then returns the universal
fail-closed receipt shape without appending, settling, voting, claiming,
advancing policy/format/checkpoint/handoff state, or creating authority.
Read-only bounded views and the repair commands remain available.
Compaction, rotation, checkpoint export/import, upgrade, handoff,
continuation, effect delivery, approval/delegation mutation, and all
maintenance other than repair fail during quarantine.

`--begin-repair` reads exactly:
```json
{"repair_id":"rpr_<8..128 URL-safe>","expected_repair_generation":0,
 "repair_secret":"<at least 32 UTF-8 characters>"}
```

Under the state lock it re-detects the corruption, requires the expected
generation, captures the opaque damaged bytes below the configured bound,
identifies the longest contiguous independently authenticated prefix, and
creates a candidate generation with a zero cursor. A bad event is never
skipped to trust descendants. All bearer tokens, delivery claims, pending
continuations, unconsumed approvals, and unsettled leases that cannot be
cross-checked against the fully established prefix are eventually
invalidated; verified terminal evidence remains. Begin returns exactly:

```json
{"schema_version":1,"kind":"policy-provenance-repair-manifest",
 "repair_id":"rpr_example","repair_generation":1,
 "source_repair_generation":0,"source_state_digest":"sha256:<64 hex>",
 "damage_digest":"sha256:<64 hex>","damaged_high_sequence":24,
 "verified_prefix_sequence":11,"verified_prefix_digest":"sha256:<64 hex>",
 "candidate_cursor":0,"candidate_item_count":18,"started_at":1700000000,
 "repair_digest":"sha256:<64 hex>","integrity":"hmac-sha256:<64 hex>"}
```

The repair digest commits to the canonical JSON excluding it and
`integrity`; integrity authenticates the object with the repair secret,
excluding only itself. An exact begin replay returns the same manifest. ID
reuse, a generation mismatch, a wrong secret, no corruption, a second begin,
or a capacity failure exits non-zero and emits no
manifest/secret/candidate.

`--advance-repair <id>` reads exactly
`{"manifest":{...},"repair_secret":"...","expected_candidate_cursor":0}`.
It authenticates the manifest before staging, verifies the cursor, and
copies, cross-checks, or conservatively terminates at most `--limit`
deterministic logical items. Publication is one atomic progress record. It
returns exactly:

```json
{"schema_version":1,"kind":"policy-provenance-repair-status",
 "repair_id":"rpr_example","repair_generation":1,
 "candidate_cursor":5,"candidate_item_count":18,"remaining_items":13,
 "candidate_state_digest":"sha256:<64 hex>","discarded_event_count":13,
 "invalidated_authority_count":3,"attested_verifiers":[],
 "ready_for_verification":false,"state":"rebuilding",
 "repair_digest":"sha256:<64 hex>","integrity":"hmac-sha256:<64 hex>"}
```

After copying completes, `ready_for_verification=true`,
`remaining_items=0`, and the state is `verifying`. An exact advance replay
at a published cursor returns the current status; a stale/future cursor
fails. Death before publication exposes no batch, death after publication
exposes all of it, and a restart discards unreferenced temporaries. The
candidate never answers hooks or serves as an audit append anchor.

`--verify-repair <id>` reads exactly:

```json
{"status":{...},"repair_secret":"...","verifier_id":"operations",
 "verifier_secret":"<at least 32 UTF-8 characters>"}
```

The verifier ID must be policy-allowed. The operation independently
re-verifies the complete candidate and damage boundary rather than trusting
the status, records one verifier identity for this repair generation, and
returns exactly:
```json
{"schema_version":1,"kind":"policy-provenance-repair-attestation",
 "repair_id":"rpr_example","repair_generation":1,
 "verifier_id":"operations","candidate_state_digest":"sha256:<64 hex>",
 "damage_digest":"sha256:<64 hex>","verified_prefix_sequence":11,
 "attested_at":1700000001,"attestation_digest":"sha256:<64 hex>",
 "integrity":"hmac-sha256:<64 hex>"}
```

Integrity is an HMAC with the verifier secret over the object excluding
`integrity`. A completely identical verifier/request replay returns the
same attestation. Another secret for the same verifier, an unlisted
verifier, a digest mismatch, or a verifier impersonating a second ID fails
without changing coverage or printing either secret. Distinct IDs count
once.

`--commit-repair` reads exactly
`{"status":{...},"repair_secret":"...","attestations":[{...},{...}]}`. It
re-verifies the current ready status, requires the configured number of
distinct current candidate attestations, re-checks the source damage
digest and the repair generation compare-and-swap, fsyncs the candidate,
and replaces one active generation pointer. The commit appends a
`repair_commit` event chained from the verified prefix, records the damaged
high sequence/digest as discarded evidence, increments the repair
generation, invalidates every pending continuation and claim, and returns
exactly:

```json
{"schema_version":1,"kind":"policy-provenance-repair-commit",
 "repair_id":"rpr_example","repair_generation":1,
 "source_state_digest":"sha256:<64 hex>",
 "candidate_state_digest":"sha256:<64 hex>",
 "damage_digest":"sha256:<64 hex>","verified_prefix_sequence":11,
 "discarded_event_count":13,"invalidated_authority_count":3,
 "verifier_ids":["audit","operations"],"committed_at":1700000002,
 "commit_sequence":12,"previous_repair_lineage_digest":"sha256:<64 hex>",
 "repair_lineage_digest":"sha256:<64 hex>",
 "repair_digest":"sha256:<64 hex>","integrity":"hmac-sha256:<64 hex>"}
```

The repair lineage commits to the previous lineage, the
repair/damage/candidate digests, the generation, the prefix, the discarded
high-water evidence, and the commit sequence. An exact commit replay
returns the same commit. Death before the pointer replacement leaves
quarantine authoritative; death after the replacement leaves the complete
healthy generation and a replay recovers the commit. No older repair
generation, lineage, checkpoint, or handoff can roll it back.

`--abort-repair <id>` reads exactly `{"repair_secret":"..."}` and returns an
authenticated object with the exact fields `schema_version`, `kind`
(`policy-provenance-repair-abort`), `repair_id`, `repair_generation`,
`damage_digest`, `aborted_at`, `abort_digest`, and `integrity`. It discards
only in-progress staging and deliberately keeps the owner quarantined; a
new repair ID may begin from the same uncommitted generation. An abort
replay is exact. Abort fails after commit.

`--repairs` emits exact records sorted by repair generation with
`schema_version`, `repair_id`, a positive `repair_generation`,
`source_repair_generation`, `state` (`quarantined`, `rebuilding`,
`verifying`, `committed`, or `aborted`), `source_state_digest`,
`damage_digest`, `damaged_high_sequence`, `verified_prefix_sequence`,
`candidate_cursor`, `candidate_item_count`, `candidate_state_digest`,
`discarded_event_count`, `invalidated_authority_count`, sorted
`verifier_ids`, `created_sequence`, `transition_sequence`,
`previous_repair_lineage_digest`, and a nullable `repair_lineage_digest`.
It exposes no damaged payloads, candidate content, secrets, attestation
integrity, bearer tokens, raw inputs/results, keys, or paths.

### Resumed-session continuation authority

A version-2 policy may include exactly:

```json
{"continuation":{"token_seconds":30,"max_pending":4,
                 "tools":["Bash","Write","mcp__files__inspect"]}}
```

`token_seconds` is an integer `2..3600`, `max_pending` is an integer
`1..64`, and `tools` is a non-empty unique array of exact tool names. An
invalid field invalidates the whole policy. The first pending-approval or
policy-authorized Pre for a configured tool creates an `sca_` session
authority at generation 1 for that session. Ordinary denials create
nothing. Existing receipt/reservation/approval/effect identities stay bound
to the session authority ID and generation.

`--issue-continuation` reads exactly:
```json
{"continuation_id":"ctn_<8..128 URL-safe>",
 "source_session_id":"session-old","target_session_id":"session-new",
 "expected_session_generation":1,
 "continuation_secret":"<at least 32 UTF-8 characters>"}
```

The source must be one active authority generation, and the target must be
distinct and unused in that authority lineage. Under the state lock,
issuance expires expired tokens, checks the global `max_pending` and the
per-authority pending-token capacity, binds the full policy snapshot, owner,
repair/format lineage, and last receipt/sequence, then changes the source
to `pending` before returning exactly:

```json
{"schema_version":1,"kind":"policy-provenance-continuation-token",
 "continuation_id":"ctn_example","session_authority_id":"sca_<opaque>",
 "source_session_id":"session-old","target_session_id":"session-new",
 "source_generation":1,"target_generation":2,"issued_at":1700000000,
 "expires_at":1700000030,"policy_id":"project-policy","policy_epoch":7,
 "policy_snapshot_digest":"sha256:<64 hex>",
 "owner_instance_id":"inst_<opaque>","owner_generation":1,
 "repair_generation":0,"repair_lineage_digest":"sha256:<64 hex>",
 "state_format_version":2,"upgrade_lineage_digest":"sha256:<64 hex>",
 "last_receipt_id":"pol_<opaque>","last_sequence":12,
 "predecessor_continuation_id":null,"token_digest":"sha256:<64 hex>",
 "token":"ctk_<opaque>","integrity":"hmac-sha256:<64 hex>"}
```

The complete object is the only bearer response. The token digest commits
to the canonical public object excluding `token`, `token_digest`, and
`integrity`; integrity authenticates the object excluding only itself with
the continuation secret. An exact issue replay returns the same token.
Another issue, ID reuse, a wrong generation/source/target, capacity
exhaustion, a policy successor while the token is pending, an inactive
owner, a building repair, or quarantine fails without a token or fence
change.

While pending, a new governed source Pre delivery denies without a durable
receipt/effect/lease/ticket; a matching Post and an exact duplicate delivery
of an already established source lifecycle may still settle and update its
bounded effect. The pending token captures those suffixes by identity. The
exact target Pre presents the complete token at
`tool_input._policy_continuation`. Before ordinary action normalization,
the hook verifies and strips it, cross-checks the target session,
generation, policy, owner, repair/format lineage, expiry, and the current
pending identity, then atomically changes the same authority to the target
generation and marks the source `continued`. The target event then runs
ordinary policy, approval, quota, and delivery handling; even an `ask`
receipt binds to the new generation. Exact concurrent target duplicates
activate once and return one receipt. Token reuse by another
event/session, a wrong target, a modified token, an
expired/cancelled/invalidated token, or a stale generation fails and creates
no authority. Later target events omit the token and use the durable
active generation.

`--cancel-continuation <id>` reads exactly
`{"expected_session_generation":1,"continuation_secret":"..."}`. Only a
still-pending source may cancel. It restores the source generation to
active, appends `continuation_cancel`, and prints the ordinary bounded
audit view. An exact cancel replay succeeds. Activation permanently
prevents cancel. Expiry through the next hook or `--reconcile` is
equivalent: it appends a `continuation_expire`, restores only the
never-activated source, and makes the token non-reusable. Repair
quarantine invalidates pending tokens; a successful repair increments the
surviving active session generation before new work, so every pre-repair
token is fenced.

`--continuations` emits exact records sorted by continuation ID with
`schema_version`, `continuation_id`, `session_authority_id`,
`source_session_id`, `target_session_id`, a positive `source_generation`, a
positive `target_generation`, `state` (`pending`, `activated`, `cancelled`,
`expired`, or `invalidated`), `policy_id`, `policy_epoch`,
`policy_snapshot_digest`, a nullable `owner_instance_id`, a nullable
positive `owner_generation`, a non-negative `repair_generation`,
`repair_lineage_digest`, a positive `state_format_version`,
`upgrade_lineage_digest`, `last_receipt_id`, a positive `last_sequence`, a
nullable `predecessor_continuation_id`, `token_digest`, a positive
`issued_sequence`, a positive `transition_sequence`, and `expires_at`. It
never exposes tokens, secrets, inputs, results, claims, approvals, keys, or
private scheduling state.

Continuation is session-level authority inside one owner. Ownership prepare
fences issue/activation and transfers pending records only as inactive
data; activation resumes only after target owner activation. Managed
checkpoint imports keep redacted continuation/repair evidence but remain
`checkpoint_only`: they cannot issue, cancel, activate, verify, or commit,
and never clone a live session or owner. Online upgrade catch-up copies
continuation and repair records; an upgrade cannot commit while a repair is
unhealthy. Key rotation keeps valid tokens, but the token's
policy/owner/repair/format fields stay exact fences. Effects, leases,
approvals, and audit events created after activation include the same
session authority ID/generation.

Checkpoint envelopes/views and handoff offers/views add exact non-negative
`repair_count` and `continuation_count`, a non-negative
`repair_generation`, and `repair_lineage_digest`. Upgrade
manifests/status/commits/views add `repair_generation`,
`repair_lineage_digest`, and `continuation_count`. Lease, approval, and
effect views add a nullable `session_authority_id` and a nullable positive
`session_generation`; their values must match the receipts. Every
format/checkpoint/handoff digest commits to these additions. A pre-repair
checkpoint or handoff cannot roll back the active repair lineage, and a
managed checkpoint import remains `checkpoint_only` regardless of the
included session authority.

## Bounded locking and recovery

Hook and inspector processes may overlap. All receipt, policy snapshot,
reservation, approval, checkpoint, ownership handoff, journal, compaction,
rotation, mint, revocation, and reconciliation transitions must be
serialized through durable state below `--state-dir`; a process must never
hold that transaction lock while waiting for another process, reading an
unbounded stream, or doing unrelated work. Lock acquisition and recovery are
bounded by the ordinary entry timeout. If a bounded acquisition cannot
complete, return the normal protocol-shaped, fail-closed response, including
the required `policyReceipt` object and all its public fields, rather than
hanging. That response object is an ephemeral protocol-only receipt: a
lock-busy attempt must not commit a durable receipt transaction, provenance
event, reservation/lease, sequence allocation, policy pin, or other state
that a later hook or inspector could retrieve or settle. In this lock-busy
response the decision is `deny`, the reservation ID is null, and the
reservation state is `rejected`; the values for the safe schema shape may
derive from stable input and the last readable state without claiming a
committed ledger entry. This distinction does not allow lock-free
authorization or appends. A crashed writer or interrupted maintenance
operation must be recoverable by the next bounded operation without leaving
a lock that blocks recovery.

## Cycle-9 integrity seal and protected paths

A version-2 policy may include exactly the following object:

```json
{"integrity":{"protected_paths":[".claude/policy-provenance.json",
                                     ".claude/policy-provenance-ledger-state/**"],
               "seal_plugin_files":true}}
```

`integrity` is a schema-version-2-only all-or-nothing object. It requires
exactly the two keys `protected_paths` and `seal_plugin_files`; the former
is 1..16 unique non-empty UTF-8 workspace-relative POSIX globs that may not
be absolute or contain `..`, and the latter must be the boolean `true`. A
wrong type, an empty array, duplicates, path traversal, or other fields
invalidate the full policy and follow the existing safe-read/deny behavior.

When a valid `integrity` is present, the pinned plugin file set is
`.claude-plugin/plugin.json`, `hooks/hooks.json`, `hooks/policy_hook.py`,
`bin/policy-ledger-inspect`, and `README.md`. The first hook or inspector
using that policy atomically records the exact byte digests of these files
under the same state lock, forming the seal generation. Every subsequent
hook/inspector call must verify the seal before any authorization, vote,
reservation, effect, maintenance, or view output. An exact repeated
verification is idempotent; concurrent first verifications may form only
one seal.

As soon as any sealed file's bytes change, go missing, are replaced by a
symlink, or become unreadable, the integrity state becomes `compromised`
once and for all, at the same time quarantining all owner, lease, approval,
effect, delegation, handoff, upgrade, repair, and continuation authority.
Afterwards the hook must return a valid safe denial response and cannot
establish new receipts/events/other authority; restart, compaction,
rotation, checkpoint import, handoff, a policy successor, or repeated calls
cannot restore the state to authorizable. `--integrity` still emits the
bounded view with exit 0.

Every normalized action whose selected path matches `protected_paths`, or
whose safely identifiable command target would write, delete, or replace a
matching path, must be refused by the integrity boundary after the ordinary
allow result. The refusal produces no lease, approval, effect, delegation,
or continuation authority; a safe audit receipt may exist but must keep the
existing redaction and chain constraints. Reads of these paths are still
decided by the ordinary policy. Protection matching must handle `./`,
duplicate separators, symlinks, and `..` escapes outside the workspace, and
must not rely on raw string prefixes alone.

The inspector command table gains the mutually exclusive read-only view
selector `--integrity`. It cannot be combined with other views, filters, or
maintenance modes, and outputs only one current integrity JSON object. The
exact fields per line are: `schema_version` (`1`), `state`
(`not_configured`, `sealed`, or `compromised`), a non-negative `generation`,
a non-negative `sealed_file_count`, a non-negative `protected_path_count`,
`manifest_digest` (`sha256:<64 lowercase hex>`), `authority_state`
(`unmanaged`, `active`, or `quarantined`), a non-negative
`created_sequence`, and a non-negative `transition_sequence`.
`not_configured` uses generation/count/sequence all `0`, the all-zero
manifest digest, and authority `unmanaged`; `sealed` must report generation
`1`, file count `5`, and active; `compromised` keeps the same seal
generation, reports quarantined, and monotonically advances the transition
sequence. The view must not output sealed filenames, path patterns, file
contents, keys, tokens, secrets, or private storage paths, and it is not a
reseal/recovery interface.
