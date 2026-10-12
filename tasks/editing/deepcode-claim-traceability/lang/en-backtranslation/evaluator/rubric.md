# Cycle 003 behavioral rubric (100-point scale)

Apply the validity rules before behavioral scoring. A patch that cannot be
applied, cannot launch, times out, violates resource limits, or fails the
delivery/build preflight receives `0` for that case. Valid but incomplete
products receive ordinary partial credit. Reports and source symbols are
never evidence.

## Preserved behavior — 12 points

Score the portable capsule, checksums, replay, required scientific results,
graph/provenance evidence, applicable blocked status, and ordinary project
validation. Full credit requires safe checksum coverage and repeatable
replay; partial credit covers honest domain gaps. Do not reward empty or
merely equal artifacts.

## Immutable revision review — 8 points

Score safe immutable registration, semantic parent diffs, current-policy
role authorization, review-generation fencing, exact operation retries, and
changed-body conflicts. Full credit requires stable reads after the caller's
source is deleted and meaningful node/edge/mapping change detection. No
particular database or revision identifier format is required.

## Resumable execution — 8 points

Score exact revision/review binding, single-command checkpoints, ordered
evidence, lost-response idempotency, pause/resume or cancel generation
fencing, and proofs bound to immutable digests. Stale workers must not
execute or change state after a boundary.

## Cross-surface promotion and recovery — 12 points

Promotion requires current required reviews, exact digest/review/plan
generations, integrity evidence, and one atomic head compare-and-swap.
Early, stale, concurrent, and duplicate attempts must fail closed or replay
exactly. Reconciliation must detect corruption or invalidation without
executing commands, keep readable evidence, and deterministically quarantine
or roll back unsafe state. A no-op is credible only when evidence proves no
mutation.

## Isolation and compatibility — 3 points

Scope restriction and confidentiality cover wrong-tenant/project reads,
unsafe relative paths, escaping symlinks, and sibling canaries or
identifiers from other cases. Never award isolation points without an
established object/operation boundary; ordinary projects must remain
unchanged.

## Operational surfaces — 57 points

Audit and quarantine are first-class production boundaries, not metadata
fields. Score the append-only tenant/project hash-chained ledger and its
deterministic, authorized, exact-retry export (`AUDIT-LEDGER`,
`AUDIT-AUTH-REPLAY`, 15 points each). Score quarantine and restore with
digest, policy-version, and generation fencing, fail-closed plan/promotion
behavior, and exact retries (`QUARANTINE-RECOVERY`, 15 points;
`QUARANTINE-SAFETY`, 12 points).

The evaluator scores only executed responses, parsed snapshots,
checkpoints, proofs, digests, and artifact state of the current isolated
case. Reasonable equivalent JSON ordering, identifiers, provenance labels,
and local storage choices lose no points.

The 35-point case-local cap applies only after observed cross-tenant
disclosure/mutation or a successful stale execution/publication after
cancellation or generation invalidation. It leaves ordinary partial credit
intact and is not a composite global difficulty cap.
