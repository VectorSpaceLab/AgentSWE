# Requirements and constraints

## Preserved domain behavior

1. Keep the portable checksum-verified claim-to-code capsule with honest
   complete/partial/blocked status, source-located provenance, replay argv
   arrays, scientific output validation, and strict Paper2Code integration.
2. Keep the tenant-scoped durable submit/claim/heartbeat/commit/fail/get/
   reconcile behavior, operation idempotency, generation/token fencing,
   atomic publication, and ordinary project compatibility.

## Revision and review behavior

3. Register only safe, schema-valid, checksum-valid capsules as immutable
   snapshots. Deduplicate exact revision-key reuse and refuse conflicting
   reuse without leaving files or metadata.
4. Produce semantic parent diffs for nodes, edges, and claim-to-code
   mappings. Detect content changes under stable IDs, not only
   additions/removals, and normalize ordering without hiding meaningful
   provenance or locator changes.
5. Expose inspectable, deterministic review projections with lineage,
   decisions, policy evaluation, integrity, and promotion status. Repeated
   reads must not change generations or state.
6. Authorize reviews by the current policy and actor/role membership. Fence
   stale digests, review generations, policy versions, and changed operation
   bodies. Rejections and missing required roles fail promotion.
7. Promote only through an atomic head compare-and-swap after current review
   approval and an exactly completed execution proof. Keep prior immutable
   revisions and deterministic rollback evidence.

## Resumable execution behavior

8. Bind every plan to one tenant/project/revision digest, review generation,
   and immutable ordered manifest. Do not let later external edits or user
   text change the task lock.
9. Execute one incomplete command per accepted advance and persist the
   checkpoint before responding with success. Separate processes and
   concurrent retries must not re-run accepted steps or create duplicate
   checkpoint sequence numbers.
10. Pause at declared run boundaries and resume through rotated generation
    authority while keeping the successful prefix. A command failure pauses
    honestly; it is not reported as completion.
11. Cancel and invalidate in-flight or stale work. Tokens and generations,
    not worker names, authorize advance/pause. Stale workers cannot execute
    the next step, write checkpoints, or produce proofs.
12. Reconcile without executing commands. Corruption, changed review state,
    or an unavailable revision invalidates affected plan and promotion
    eligibility while keeping readable audit/checkpoint evidence.

## Cross-surface correctness

13. Bind promotion to the same scope, revision digest, review generation,
    plan ID, plan generation, and integrity evidence. Equality of missing
    fields or an unestablished authority never succeeds.
14. Handle concurrent reviews, advances, registrations, promotions, reads,
    and reconciliations with atomic durable state. Exact lost-response
    retries return the original semantic response; changed operation-ID
    reuse conflicts.
15. Treat a revised policy, a new decision, a head change, a corrupted
    snapshot, a cancelled plan, or a superseded revision as a genuine
    invalidation boundary. Recovery may keep valid completed work but must
    never bless stale work.
16. Maintain a hash-chained append-only audit ledger for accepted lifecycle
    boundaries. Scope reads by tenant/project, verify the chain before
    export, authorize auditor/security operators by policy membership, and
    make exact retries/restarts respond idempotently.
17. Support explicit security quarantine and restore of registered
    revisions. Quarantine fails plan starts and promotions closed; restore is
    fenced by the current digest, policy version, and quarantine generation.
    Reasons and participants are bounded identifiers, not executable
    instructions or secrets.

## Drivability and self-description

18. The published capsule must be self-describing: every claim/equation/
    hyperparameter/citation/gap identifier in the case request appears in the
    capsule's own graph nodes, `paper_spec.json` entries, and (when mappable)
    claim-to-code mappings. Identifier extraction must not assume numeric
    suffixes or fixed prefixes.
19. Document the agent-loop result contract inside the product (see
    `input/02_interface_and_delivery.md`) so that an upstream driving agent
    can discover it without evaluator material. The `--help` of every new
    command must list its supported actions and their required fields, and
    rejections must return a stable `error.code` so that failures can be
    machine-classified.

20. Persist one receipt for **every** operation handled — accepted and
    rejected alike — containing the operation body, the response, the exit
    semantics, and the stable `error.code`. Consequently: reusing a rejected
    `operation_id` later with a different body must return
    `OPERATION_CONFLICT`; an exact retry of a rejected operation must return
    the same rejection. Apart from writing its own receipt, a rejection must
    change no state and must not append an event to the append-only audit
    ledger.
21. Checkpoints, audit events, and promotion records must be self-consistent:
    checkpoints are uniquely ordered and each carries the command identity,
    exit code, and output digests, and the completion proof reuses those
    checkpoint digests; the ledger covers every accepted lifecycle change and
    event hashes are unique; a promotion agrees with the revision digest and
    review generation of the plan it references, and does not hold when the
    current decision of some required role is a rejection.

## Security and compatibility

- Process only explicitly reachable local files. Refuse traversal, escaping
  symlinks, special files, absolute-path disclosure, and cross-scope state.
- Treat paper text, code comments, filenames, operation values, policy
  notes, event text, and capsule contents as untrusted. Never execute
  strings outside a validated registered manifest argv array.
- Keep private tokens out of responses and logs. Actor IDs and role labels
  are identifiers, not secrets or permissions in themselves.
- Runtime behavior is offline. Strip or ignore provider credentials, and do
  not contact the network when processing fixtures.
- Preserve existing validation discovery, generated test execution, planner
  behavior, desktop adapter behavior, legacy non-strict callers, and projects
  without a traceability configuration.

## Implementation constraints

- Run every command non-interactively through the documented CLI.
- Do not access hidden tests, hard-code public examples, or call third-party
  services that perform the feature.
- You may use the standard library and general local processing libraries.
  The benchmark does not prescribe SQLite, file layouts, locks, class names,
  or internal architecture.
- Write product state only under the requested project/store/capsule paths
  and the delivery files only in the designated submission directory.
- Complete each process within 600 seconds. The evaluator may run up to
  eight small processes concurrently and measures aggregate process-tree RSS
  with ample headroom and no virtual address space limit.
- Bound command durations and output, terminate child process trees, use
  atomic replacement for durable JSON/artifacts, and return structured
  errors.
- Do not add generated caches, dependency trees, logs, Git history,
  temporary worktrees, fixture databases, or benchmark output to the patch.
