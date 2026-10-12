# Public mastery-tool contract

This document restores the original public interface requirements. The pinned repository exposes the ordinary five-tool baseline; implement and register all 26 tools. The accompanying `mastery_tool_schema.json` is the canonical parameter and result-field index. Its schemas document the interface; they are not an evaluator runtime filter that rejects optional compatibility aliases.

Use ordinary product tool classes, `MASTERY_TOOL_NAMES`, `MASTERY_TOOL_TYPES` and usable `get_definition()` metadata. The native lower agent must receive the same canonical parameter definitions through its normal tool registry. The server injects the active authorized `_mastery_path_id` and `_session_id`; the model cannot override this authorization. IDs, actor names, challenges and catalog text are inert data. Extension refusals are bounded structured errors with `ToolResult.success=false` and no state mutation. A successful empty/busy status is not an issued claim.

## Public parameter index

### `mastery_assess`

Parameters: `knowledge_point_id`: string required, `passed`: boolean required, `feedback`: string optional.

Successful response fields: preserve the original baseline response. See the JSON index for nested structures, required fields and conditional fields.

### `mastery_build`

Parameters: `modules`: array required, `mode`: string optional; values replace, append.

Successful response fields: preserve the original baseline response. See the JSON index for nested structures, required fields and conditional fields.

### `mastery_grade`

Parameters: `answer`: string required.

Successful response fields: preserve the original baseline response. See the JSON index for nested structures, required fields and conditional fields.

### `mastery_learner_snapshot`

Parameters: `as_of`: integer or null optional, `limit`: integer optional; minimum 1, `include_terminal`: boolean optional.

Successful response fields: `schema_version`, `status`, `as_of`, `snapshot_revision`, `high_water_sequence`, `digest`, `mastery`, `reviews`, `handoffs`, `policy`, `lineage`, `attestation`. See the JSON index for nested structures, required fields and conditional fields.

### `mastery_learner_snapshot_attest`

Parameters: `snapshot_revision`: integer required; minimum 0, `snapshot_digest`: string required, `high_water_sequence`: integer required; minimum 0, `policy_version`: integer required; minimum 1, `challenge`: string required, `attestation_id`: string or null optional, `attestation_ref`: string or null optional, `expected_revision`: integer or null optional; minimum 0, `event_id`: string required, `as_of`: integer or null optional.

Successful response fields: `schema_version`, `attestation_id`, `snapshot_revision`, `snapshot_digest`, `high_water_sequence`, `policy_version`, `covered`, `canonical_digest`, `proof`, `replayed`, `state`. See the JSON index for nested structures, required fields and conditional fields.

### `mastery_learner_snapshot_chain_audit`

Parameters: `snapshot_revision`: integer required; minimum 0, `snapshot_digest`: string required, `attestation_digest`: string required, `witness_id`: string required, `witness_sequence`: integer required; minimum 1, `witness_root_digest`: string required, `expected_head_sequence`: integer required; minimum 1, `challenge`: string required, `event_id`: string required, `as_of`: integer or null optional.

Successful response fields: `schema_version`, `state`, `audit_valid`, `head_sequence`, `head_root_digest`, `snapshot_digest`, `attestation_digest`. See the JSON index for nested structures, required fields and conditional fields.

### `mastery_learner_snapshot_checkpoint`

Parameters: `subscriber_id`: string required, `through_revision`: integer required; minimum 0, `expected_checkpoint`: integer required; minimum 0, `event_id`: string required, `as_of`: integer or null optional.

Successful response fields: `status`, `checkpoint`, `replayed`, `snapshot_digest`, `attestation_digest`, `witness_root_digest`, `witness_sequence`. See the JSON index for nested structures, required fields and conditional fields.

Bind the accepted snapshot and attestation digests. When a witness head exists, also return witness_root_digest and witness_sequence. These fields do not merge snapshot and event-feed checkpoint namespaces.

### `mastery_learner_snapshot_restore`

Parameters: `snapshot_revision`: integer required; minimum 0, `snapshot_digest`: string required, `high_water_sequence`: integer required; minimum 0, `policy_version`: integer required; minimum 1, `challenge`: string optional, `attestation_id`: string required, `attestation_ref`: string or null optional, `expected_revision`: integer required; minimum 0, `event_id`: string required, `as_of`: integer or null optional.

Successful response fields: `schema_version`, `status`, `snapshot_revision`, `snapshot_digest`, `replayed`. See the JSON index for nested structures, required fields and conditional fields.

### `mastery_learner_snapshot_verify`

Parameters: `witness_id`: string required, `witness_sequence`: integer required; minimum 1, `snapshot_digest`: string required, `attestation_digest`: string required, `leaf_digest`: string required, `root_digest`: string required, `previous_root_digest`: string required, `inclusion_proof`: array required, `consistency_proof`: array required, `event_id`: string optional, `as_of`: integer or null optional.

Successful response fields: `schema_version`, `state`, `witness_id`, `root_digest`, `leaf_digest`, `inclusion_valid`, `consistency_valid`. See the JSON index for nested structures, required fields and conditional fields.

### `mastery_learner_snapshot_witness`

Parameters: `snapshot_revision`: integer required; minimum 0, `snapshot_digest`: string required, `attestation_id`: string required, `attestation_digest`: string required, `challenge`: string required, `witness_id`: string or null optional, `expected_sequence`: integer required; minimum 0, `previous_root_digest`: string or null optional, `event_id`: string required, `as_of`: integer or null optional.

Successful response fields: `schema_version`, `state`, `witness_id`, `witness_sequence`, `snapshot_digest`, `attestation_digest`, `previous_root_digest`, `leaf_digest`, `root_digest`, `inclusion_proof`, `consistency_proof`, `replayed`. See the JSON index for nested structures, required fields and conditional fields.

### `mastery_learning_checkpoint`

Parameters: `subscriber_id`: string required, `through_sequence`: integer required; minimum 0, `expected_checkpoint`: integer required; minimum 0, `event_id`: string required, `as_of`: integer or null optional.

Successful response fields: `status`, `checkpoint`, `replayed`. See the JSON index for nested structures, required fields and conditional fields.

### `mastery_learning_events`

Parameters: `subscriber_id`: string required, `after_sequence`: integer or null optional; minimum 0, `limit`: integer optional; minimum 1, `event_types`: array or null optional.

Successful response fields: `schema_version`, `status`, `checkpoint`, `high_water_sequence`, `events`. See the JSON index for nested structures, required fields and conditional fields.

### `mastery_policy_publish`

Parameters: `actor_id`: string required, `policy_id`: string required, `policy`: object required, `version`: integer required; minimum 1, `expected_version`: integer required; minimum 1, `effective_at`: integer required, `event_id`: string required, `as_of`: integer or null optional.

Successful response fields: `schema_version`, `policy_id`, `version`, `digest`, `status`, `replayed`. See the JSON index for nested structures, required fields and conditional fields.

### `mastery_policy_status`

Parameters: `actor_id`: string optional, `policy_id`: string optional, `policy`: object or null optional, `version`: integer or null optional; minimum 1, `expected_version`: integer or null optional; minimum 1, `effective_at`: integer or null optional, `event_id`: string optional, `as_of`: integer or null optional.

Successful response fields: `schema_version`, `policy_id`, `version`, `policy`, `digest`, `migration_required`. See the JSON index for nested structures, required fields and conditional fields.

### `mastery_quiz`

Parameters: `knowledge_point_id`: string required, `question`: string required, `expected_answer`: string required, `question_type`: string optional; values choice, short, open, `options`: array optional, `adaptive_context`: object optional.

Successful response fields: preserve the original baseline response. See the JSON index for nested structures, required fields and conditional fields.

### `mastery_remediation_ack`

Parameters: `delivery_id`: string required, `consumer_id`: string required, `generation`: integer required; minimum 1, `event_id`: string required, `outcome`: string optional, `as_of`: integer or null optional.

Successful response fields: `status`, `replayed`. See the JSON index for nested structures, required fields and conditional fields.

Only the current active-path delivery_id/consumer_id/generation may acknowledge. Default outcome is delivered. Exact event replay returns its saved response; expired, stale generation, conflicting event, foreign path or terminal delivery is refused without revival or duplicate review.

### `mastery_remediation_claim`

Parameters: `topic_id`: string or null optional, `consumer_id`: string required, `event_id`: string required, `lease_seconds`: integer optional; minimum 1, `as_of`: integer or null optional.

Successful response fields: `status`, `delivery`, `replayed`. See the JSON index for nested structures, required fields and conditional fields.

A claimed result has a complete delivery. Legitimate empty/busy responses need not expose a delivery and may return null or an empty bounded object; they do not claim a lease was issued.

### `mastery_remediation_reset`

Parameters: `topic_id`: string or null optional, `reason`: string required, `event_id`: string required, `as_of`: integer or null optional.

Successful response fields: `status`, `replayed`. See the JSON index for nested structures, required fields and conditional fields.

### `mastery_remediation_status`

Parameters: `topic_id`: string or null optional, `as_of`: integer or null optional.

Successful response fields: `status`. See the JSON index for nested structures, required fields and conditional fields.

### `mastery_review_plan`

Parameters: `as_of`: integer or null optional, `limit`: integer optional; minimum 1, `include_terminal`: boolean optional.

Successful response fields: `schema_version`, `status`, `as_of`, `due_count`, `next_due_at`, `tasks`. See the JSON index for nested structures, required fields and conditional fields.

### `mastery_review_reschedule`

Parameters: `task_id`: string required, `due_at`: integer required, `expected_revision`: integer required; minimum 1, `reason`: string required, `event_id`: string required, `as_of`: integer or null optional.

Successful response fields: `status`, `replayed`, `task`. See the JSON index for nested structures, required fields and conditional fields.

### `mastery_session_abandon`

Parameters: `actor_id`: string required, `handoff_id`: string required, `expected_revision`: integer required; minimum 1, `event_id`: string required, `include_terminal`: boolean optional, `as_of`: integer or null optional, `reason`: string optional.

Successful response fields: `schema_version`, `handoff_id`, `revision`, `status`, `replayed`. See the JSON index for nested structures, required fields and conditional fields.

### `mastery_session_handoff`

Parameters: `actor_id`: string required, `handoff_id`: string or null optional, `expected_revision`: integer or null optional; minimum 0, `pending_question`: object required, `policy_version`: integer required; minimum 1, `expires_at`: integer required, `event_id`: string required.

Successful response fields: `schema_version`, `handoff_id`, `revision`, `policy_version`, `status`, `pending_question`, `replayed`. See the JSON index for nested structures, required fields and conditional fields.

### `mastery_session_handoffs`

Parameters: `actor_id`: string optional, `handoff_id`: string or null optional, `expected_revision`: integer or null optional; minimum 0, `event_id`: string optional, `include_terminal`: boolean optional, `as_of`: integer or null optional, `reason`: string optional.

Successful response fields: `schema_version`, `status`, `handoffs`. See the JSON index for nested structures, required fields and conditional fields.

### `mastery_session_resume`

Parameters: `actor_id`: string required, `handoff_id`: string required, `expected_revision`: integer required; minimum 1, `event_id`: string required, `include_terminal`: boolean optional, `as_of`: integer or null optional, `reason`: string optional.

Successful response fields: `schema_version`, `handoff_id`, `revision`, `status`, `replayed`. See the JSON index for nested structures, required fields and conditional fields.

### `mastery_status`

Parameters: none.

Successful response fields: preserve the original baseline response. See the JSON index for nested structures, required fields and conditional fields.

## Historical learning evidence

The API uses the same active-path learning records as ordinary mastery. Reconcile already completed, confirmed historical error/diagnosis records idempotently with delivery/review lineage, retaining source identity even when legacy extension state is absent. This is different from creating a new persistent diagnosis from a single uncertain answer. New diagnosis requires corroborating distinct events and declared thresholds; later correct evidence and graduated records remain visible to a conservative recommendation.

Canonical claims use consumer_id/event_id and return delivery_id/consumer_id/generation/attempt. Acknowledgments use that identity, consumer and generation. Review edits use task_id/event_id/reason. Snapshot identity is revision plus digest; no snapshot_id is required. Attestation and witness operations bind the canonical returned revision/digest/high-water/policy fields. Compatibility aliases are optional; the evaluator uses canonical parameters.

## Restored original public semantics

## Adaptive and delivery entry points required by the original task

The evaluator invokes the registered tool classes
`deeptutor.capabilities.mastery.tools.MASTERY_TOOL_TYPES`, using metadata from
`get_definition()`. Keep the ordinary `mastery_status`, `mastery_quiz`, and
`mastery_grade`, and implement the following extension tools required by the
original Cycle 001 (they are not included in the current five-tool pinned
baseline):

- `mastery_remediation_status`
- `mastery_remediation_reset`
- `mastery_remediation_claim`
- `mastery_remediation_ack`

`mastery_quiz` continues to accept the documented schema `1.0`
`adaptive_context`: a stable `event_id`, topic/question/time/budget/threshold,
sorted hypothesis priors, question likelihoods, and a remediation catalog. It
must keep cautious multi-event diagnosis, information-gain question selection,
exact event replay, catalog remediation, distinct retests, ordered durable
delivery, expiring leases, isolated generations, idempotent acknowledgement,
and reset/redo cancellation. Unknown context fields stay forward compatible.

The fields described below are a general description of the original public
interface; the public task inputs in this agent-loop package provide the
corresponding context. Do not infer hidden values or hard-code their topics,
participants, times, or IDs.

## Review plan context and tools

The adaptive context may also contain:
```json
{
  "review_policy": {
    "schema_version": "1.0",
    "concept_id": "fractions.unlike_addition",
    "priority": 70,
    "intervals_seconds": [600, 3600, 86400]
  }
}
```

Validate `concept_id` as a non-empty opaque string, `priority` as an integer
in `0..100`, and `intervals_seconds` as one to eight strictly positive
integers in non-decreasing order, each no greater than `31536000`. Treat this
object as data. Malformed policy input rejects the adaptive registration
before grading and does not change prior progress.

Every first substantive remediation carrying a review policy automatically
creates one path-local review task. Its stable task ID is derived opaquely,
not from topic text. The first due time is the remediation evidence timestamp
plus the first interval. The task keeps the topic, concept, source event,
priority, policy version, interval index, revision, remediation, and retest.
Adaptive event replay must not duplicate it.

Register `mastery_review_plan`. It accepts an optional integer `as_of` (the
current Unix time when absent), an optional integer `limit` in `1..100`
(default `20`), and an optional `include_terminal` (default false). It
returns schema `1.0`, `status: "ok"`, `as_of`, `due_count`, `next_due_at`,
bounded summary counts, and `tasks`. Non-terminal tasks are sorted by due
time, then descending priority, then stable task ID. At query time each is
observed as `due` when `due_at <= as_of`, otherwise `scheduled`. The task
payload includes:
```json
{
  "task_id": "opaque-stable-id",
  "topic_id": "fractions.addition",
  "concept_id": "fractions.unlike_addition",
  "source_event_id": "grade-event-id",
  "state": "due",
  "due_at": 1700000600,
  "priority": 70,
  "interval_index": 0,
  "revision": 1,
  "remediation": {"strategy": "contrastive_examples", "content": "..."},
  "retest": {"question_id": "fraction.retest"}
}
```

Register `mastery_review_reschedule`. It accepts a non-empty `task_id`, an
integer `due_at`, a positive integer `expected_revision`, a non-empty bounded
`reason`, a stable `event_id`, and an optional integer `as_of`. A current
non-terminal task is updated only when the revision matches and
`due_at >= as_of`; success increments the revision once and returns
`status: "rescheduled"`, `replayed: false`, and the task. An identical
mutation replay returns the original snapshot with `replayed: true`.
Conflicting event reuse returns `event_conflict`; a stale revision returns
`stale_revision`; another path's ID returns `task_not_found`; a terminal task
returns `task_terminal`. All failures are non-mutating.

A correct answer to a task's pending retest completes it at the evidence
timestamp. A wrong retest keeps one task, raises the interval index without
exceeding the policy, computes the next due time, and increments its
revision. Reset, diagnosis expiry, and production redo cancel non-terminal
tasks with provenance. Legacy state without review fields loads as an empty
plan.

## Learning event feed and checkpoints

Register `mastery_learning_events`. It accepts a non-empty `subscriber_id`,
an optional non-negative `after_sequence`, an optional integer `limit` in
`1..100` (default `50`), and an optional string array `event_types`. Without
an explicit cursor, reading starts after that subscriber's durable
checkpoint. It is read-only and returns schema `1.0`, `status: "ok"`, the
subscriber checkpoint, the path high-water sequence, and the ordered events.
Filtering changes which events are returned but never rewrites sequence
numbers or advances the checkpoint.

Every accepted adaptive evidence update and every remediation, delivery,
review task, reset/expiry/redo, reschedule, retest, or checkpoint-related
learning transition emits an immutable path-local event in the same durable
commit as its source mutation. Replayed and refused mutations emit nothing.
Events have a strictly increasing path-local integer `sequence` and include
the schema version, a stable event ID, type, topic, source event ID,
occurrence time, aggregate kind/ID/revision, and a bounded data object.
Catalog text must never be executed. The feed must at least distinguish
`adaptive_evidence_recorded`, `remediation_scheduled`, `review_rescheduled`,
`review_retest_failed`, `review_completed`, `review_cancelled`, and
`adaptive_reset` when those transitions occur.

Register `mastery_learning_checkpoint`. It accepts `subscriber_id`, a
non-negative integer `through_sequence`, a non-negative integer
`expected_checkpoint`, a stable `event_id`, and an optional integer `as_of`.
It advances only that subscriber, from the exact expected checkpoint to an
existing sequence at or below the high-water mark. Success returns
`status: "checkpointed"`, the resulting checkpoint, and `replayed: false`. An
exact replay returns the original snapshot with `replayed: true`.
Conflicting event reuse returns `event_conflict`; a stale expectation returns
`checkpoint_conflict`; a regressing or above-high-water value returns
`sequence_out_of_range`. Failures do not change the feed, the plan, or other
subscribers. Legacy state has a high-water mark and a checkpoint of zero.

## Cross-surface guarantees

The review task creation and its `remediation_scheduled` event commit
together with the adaptive remediation and the delivery item, or not at
all. The task revision and its events report the same task ID/revision/due
or terminal state. Malformed adaptive input, conflicting mutation IDs, stale
revisions, invalid checkpoints, and subscriber failures cannot delete or
partially publish valid learning state. Everything is isolated by the
injected `_mastery_path_id`, survives fresh Python processes/tool instances,
stays bounded, and never exposes other paths.

## Learner portfolio snapshot

Register `mastery_learner_snapshot` and
`mastery_learner_snapshot_checkpoint`. The snapshot is read-only and returns
schema `1.0`, `status: "ok"`, `as_of`, `snapshot_revision`,
`high_water_sequence`, a non-empty stable `digest`, bounded `mastery`,
`reviews`, `handoffs`, `policy`, and `lineage`; lineage contains at least
`review_task_ids` and `event_sequences`. For the same path/as_of with no
newly accepted mutation, repeated reads keep the revision/digest unchanged,
and reads advance no cursor. The checkpoint accepts subscriber,
through_revision, expected_checkpoint, event_id, and an optional as_of, and
allows only exact independent cursor advances; an exact replay carries
`replayed: true`, and conflicting/stale/regressing/out-of-range values return
bounded errors without changing the learner, snapshot, event feed, or other
subscribers.

## Session handoff and policy registry

Register `mastery_session_handoff`, `mastery_session_handoffs`,
`mastery_session_resume`, and `mastery_session_abandon`. A handoff response
is schema `1.0` and contains an opaque `handoff_id`, a bounded
pending-question snapshot, `policy_version`, an integer `revision`, and a
lifecycle `status`. Creation is idempotent by path and event ID and commits
before responding; a repeated request after a simulated lost response
returns the original snapshot with `replayed: true`. Resume and abandon
require the current revision and an authorized actor. Stale, expired,
terminal, malformed, and foreign-path requests fail without disclosure or
mutation. Listing after a fresh process proves durability; terminal handoffs
are excluded unless explicitly requested.

Register `mastery_policy_status` and `mastery_policy_publish`. Status
returns schema `1.0`, the active `policy_id`, an integer `version`, the
canonical bounded policy data, a deterministic digest, and whether migration
is required. Publish validates the schema/version/intervals and requires an
authorized policy actor plus the expected current version. An exact event
replay returns the original result; stale versions, unauthorized authors,
future or regressing effective times, malformed policies, and incompatible
policy versions are non-mutating failures. Learner artifacts keep their
policy version so that drift is observable rather than silently changing past
work. Legacy paths without registry data expose the deterministic version 1
default; the first accepted publication therefore uses
`expected_version: 1`.

## Snapshot provenance attestation

Register `mastery_learner_snapshot_attest` and
`mastery_learner_snapshot_restore`. Attest must recompute the canonical
digest of an already returned snapshot and return schema `1.0`, an opaque
`attestation_id`, the snapshot revision/digest, the event-feed high-water
mark, the policy version, the `covered` set, a stable canonical digest, and a
non-empty proof. `covered` must contain exactly `mastery`, `reviews`,
`handoffs`, `policy`, `lineage`, and `event_feed`; it cannot sign only a
partial projection. A retry with the same snapshot, challenge, and event ID
must return the same proof and be marked `replayed: true`.

Restore may accept only an attestation, snapshot digest, parent revision, and
path that still match; success returns `status: "restored"` (or an
equivalent schema-`1.0` verified status) and the same revision/digest. A
tampered digest, mismatched proof, fork, stale attestation, foreign path,
duplicate conflicting event ID, or out-of-range revision must return a
bounded error and must not write to the learner, review, handoff, policy,
feed, or any checkpoint. The attestation and the snapshot checkpoint must be
bound laterally, but the two kinds of checkpoint remain independent; a read
from a fresh process must reproduce the same proof.

## Append-only snapshot witness ledger

Register `mastery_learner_snapshot_witness` and
`mastery_learner_snapshot_verify`. Witness accepts only a snapshot and a
complete attestation that are still valid on the same learner/path, and
returns schema `1.0`, `state: "witnessed"`, an opaque `witness_id`, a
positive integer `witness_sequence`, the snapshot/attestation digests,
`previous_root_digest`, non-empty `leaf_digest`/`root_digest`, bounded
`inclusion_proof`/`consistency_proof`, and `replayed`. Genesis may use an
explicit empty previous root; subsequent appends must be optimistically
fenced with the current head and the expected sequence. A retry with the
same event ID, snapshot, attestation, challenge, and parent head must return
exactly the same leaf, root, proofs, ID, and sequence, marked
`replayed: true`.

Verify, after receiving untrusted witness fields, must re-verify inclusion
and consistency from the durable chain and canonical digests, returning
schema `1.0`, `state: "verified"`, the matching witness/root/leaf, and
`inclusion_valid: true`, `consistency_valid: true`; it is a read-only
operation. A wrong leaf/root, truncation, deletion, reordering, a forged
head, a stale parent, a fork, a foreign path, a wrong attestation, or a
conflicting event ID must return a bounded error and must not repair,
overwrite, or append to the chain. When a witness head already exists, the
snapshot checkpoint must additionally return `witness_root_digest` and
`witness_sequence`, keeping its original subscriber isolation and exact
replay semantics.

## Witness chain audit

Register `mastery_learner_snapshot_chain_audit`. This read-only tool receives
untrusted snapshot, attestation, witness head, and expected sequence values,
re-enumerates the whole bounded chain from the active-path durable chain, and
verifies it. Success returns schema `1.0`, `state: "audited"`,
`audit_valid: true`, `head_sequence`, `head_root_digest`, the matching
snapshot/attestation digests, and a chain digest; it cannot return success
based only on caller-supplied fields. A retry with the same event ID replays
field by field, and after the chain head changes the old audit request must
fail without side effects. A checkpoint may reference an audited head, but
the audit itself must not append to the chain, advance checkpoints, publish
events, or change any learning state.
