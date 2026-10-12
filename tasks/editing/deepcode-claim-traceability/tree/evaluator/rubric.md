# Cycle 003 Behavioral Rubric (100-point scale)

Apply validity rules before behavioral scoring. A patch that cannot be applied,
cannot launch, times out, breaches the resource limit, or fails delivery/build
preflight receives `0` for that case. A valid but incomplete product receives
ordinary partial credit. Reports and source symbols are never evidence.

## Preserved behavior - 12 points

Score the portable capsule, checksums, replay, requested scientific result,
graph/provenance evidence, blocked status where applicable, and ordinary-project
verification. Full credit requires safe checksum coverage and repeatable replay;
partial credit covers an honest domain gap. Do not reward empty or equality-only
artifacts.

## Immutable revision review - 8 points

Score safe immutable registration, semantic parent diff, current-policy role
authorization, review-generation fencing, exact operation retry, and changed
body conflicts. Full credit requires stable reads after caller-source deletion
and meaningful node/edge/mapping changes. Do not require a specific database or
revision identifier format.

## Resumable execution - 8 points

Score exact revision/review binding, one-command checkpointing, ordered evidence,
response-loss idempotency, pause/resume or cancel generation fencing, and an
attestation bound to the immutable digest. A stale worker must not execute or
mutate state after a boundary.

## Cross-surface promotion and recovery - 12 points

Promotion requires current required reviews, exact digest/review/plan generation,
integrity evidence, and one atomic head compare-and-swap. Early, stale,
concurrent, and repeated attempts must fail closed or replay exactly.
Reconciliation must detect corruption or invalidation without executing commands,
preserve readable evidence, and deterministically quarantine or roll back unsafe
state. A no-op is creditable only when the evidence proves no mutation.

## Isolation and compatibility - 3 points

Scope confinement and non-disclosure cover wrong-tenant/project reads, unsafe
relative paths, escaping symlinks, and contamination by sibling canaries or
other-case identifiers. Never award isolation points without an established
object/operation boundary; ordinary projects must remain unchanged.

## Operational surfaces - 57 points

Audit and quarantine are first-class production boundaries, not metadata fields.
Score the append-only tenant/project hash-chain ledger and deterministic,
authorized, exact-retry export (`AUDIT-LEDGER`, `AUDIT-AUTH-REPLAY`, 15 points
each). Score security-only quarantine/restore with digest, policy-version, and
generation fencing, fail-closed plan/promotion behavior, and exact retries
(`QUARANTINE-RECOVERY`, 15 points; `QUARANTINE-SAFETY`, 12 points).

The evaluator scores only executed responses, parsed snapshots, checkpoints,
attestations, digests, and artifact state from the current isolated case.
Reasonable equivalent JSON ordering, identifiers, provenance labels, and local
storage choices do not lose points.

A case-local ceiling of 35 points applies only after observed cross-tenant
disclosure/mutation or successful stale execution/publication after cancellation
or generation invalidation. It leaves ordinary partial credit intact and is not
a synthetic global difficulty cap.
