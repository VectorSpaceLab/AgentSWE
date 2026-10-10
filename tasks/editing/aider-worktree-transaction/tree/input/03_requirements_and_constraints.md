# Requirements and constraints

## Functional requirements

1. Validate the complete repository set: canonical distinct Git common
   directories, root identity, cardinality, DAG/order/scopes, acyclic
   registered gitlinks, child-before-parent participant order, unique targets,
   and target OIDs before any transaction state, admission, or worktree is
   created.
2. Acquire repository-scoped admission atomically in canonical repository
   order. Block any overlapping live transaction/state root across path
   aliases while admitting disjoint sets, and serve read-only status without
   waiting for admission, leases, or Git ref locks.
3. Durably fence all participant repositories with one monotonic epoch. Allow
   takeover only after expiry, and permanently reject every stale fence's
   command, quarantine/object, Git/ref, checkout, cleanup, and rollback
   effect.
4. Run ready workers in uniquely owned linked worktrees within the plan
   scope; make disjoint work truly concurrent, serialize overlapping scopes
   within one repository, preserve dependency order, and preserve foreign
   registrations.
5. Build one deterministic quarantined candidate per repository. Preserve
   tracked/staged/untracked/binary/rename/delete/executable/symlink/filter and
   gitlink state, and bind each parent `160000` entry to the exact child
   candidate before federation prepare.
6. Keep candidate objects invisible to ordinary repository object lookup
   until the durable commit decision. Persist and verify an independent raw
   Git object closure so that recovery survives worktree/ref/reflog/alternate/
   object pruning without repeating workers, tests, filters, hooks, or
   commits.
7. Prepare one old-OID CAS `update-ref --stdin` transaction per participant,
   then sync one global decision. Before the decision abort everything; after
   it only promote and advance the same participants in declared order.
8. Preserve foreign writers: pre-decision drift publishes nothing; post-
   decision obstruction preserves the committed prefix and the writer, reports
   a stable `decision_obstructed`, and never compensates or overwrites.
9. Recover after cooperative exit or process-group `SIGKILL` at every declared
   worker, quarantine, multi-repository prepare/decision/promotion/ref,
   checkout, cleanup, and rollback boundary. Released Git locks must not wedge
   takeover; every observable effect is preserved exactly once.
10. Rollback uses one durable reverse decision for every participant. Drift
    before it recovers; after it, roll the same reverse prefix forward.
    Restore the exact HEAD/symref, refs, index stages/modes, tracked and
    untracked state, worktree locks, submodule checkouts, and gitlink OIDs.
11. Fail aliased/path-escaping, repository or admission forgery,
    missing/corrupt/extra closure objects, pre-decision object leaks,
    impossible intra-participant partial refs, invalid graphs, stale fences,
    and hook/filter failures while preserving user/foreign/disjoint evidence.
13. Make "exactly once" externally checkable: every declared subtask's
    worker/test command, every hook/filter, every commit, and every ref update
    genuinely happens once across the whole transaction (including crashes,
    takeovers, recovery, and rollback). An exact retry of the same operation
    after a settled response must have no effect.
14. Make ownership boundaries externally checkable: after the transaction
    settles, the admission receipts written, branches created, and linked
    worktrees created by this transaction must not remain in the participant
    repositories, while foreign receipts, foreign locked worktrees, and the
    bytes of disjoint transactions must be preserved byte for byte.
15. Make reports externally checkable: the transaction ID, ledger digest,
    base/candidate/final OIDs, admission receipts, and fence cited by the
    response and the delivery report must be values genuinely produced by this
    run.

12. Add focused tests for schema/CLI, canonical repository identity,
    admission concurrency, non-blocking status, quarantine
    visibility/promotion, nested gitlinks, global prepare/decision recovery,
    object pruning, post-decision obstruction, reverse rollback decisions,
    exact dirty state, hooks/filters, and ordinary Aider/Git/submodule/worktree
    compatibility. The schema, argv environment, crash/signal rules, object
    paths, admission locations, recoverable atomicity model, and delivery
    report in `02_interface_and_delivery.md` are normative.

## Constraints

- Modify only the relevant `aider/**` sources and source-adjacent `tests/**`.
  Do not modify locks, build configuration, benchmark cases, generated
  output, authentication, sandbox, or network policy.
- Use only plain local Git and local commands. No providers, remote workers,
  cloud services, browsers, models, or network calls are used by the cases.
- Do not import evaluator modules from product code. Treat argv, repository
  configuration, hooks, filters, refs, alternates, paths, IDs, ledger/object
  bytes, admission records, and worktree/submodule metadata as untrusted.
- Do not hard-code public/hidden paths, IDs, tokens, content, OIDs,
  repository counts, branch names, timings, or evaluation roots.
- Complete non-interactively within 600 seconds and the documented process-
  tree memory cap. Write only to the declared repositories, their
  Git-common-dir `aider/transactions/` admission areas, `state_dir`, and the
  response path.
