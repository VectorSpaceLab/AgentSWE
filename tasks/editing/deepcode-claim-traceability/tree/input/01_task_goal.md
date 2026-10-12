# Task goal: reviewable and recoverable claim-to-code revisions

You are extending the supplied DeepCode repository. Preserve its required
Paper2Code claim-to-code capsule and durable-run behavior, then add two
production capabilities for teams that iterate on a reproduction over time.

First, add an immutable revision review surface. Researchers must be able
to register checksum-valid capsule revisions, diff their claim/code graphs
against a parent, inspect stable human-review projections, record
role-authorized decisions, and promote only reviewed revisions. A revision
snapshot never changes after registration. Added, removed, and changed
claims, code mappings, and evidence must be distinguishable without
treating renamed or missing items as unchanged.

Second, add a resumable execution-plan surface. Operators must be able to
bind an execution plan to the exact digest of a registered revision,
advance its manifest one command per checkpoint in separate processes,
pause at quota/dependency/operator boundaries, resume without re-running
successful steps, and cancel or supersede a generation so that stale
workers cannot continue. The plan is a task lock: its revision, command
order, and completed evidence cannot silently drift after a failure or
restart.

The two surfaces must compose. Promotion requires the current review policy
and a complete execution proof for the same tenant, project, revision
digest, and live plan generation. New review decisions, revised policies,
changed heads, corrupted snapshots, cancelled plans, or stale generations
must prevent publication until they are consistent. Exact operation retries
remain idempotent even when the original response was lost.

The target users are researchers reviewing scientific fidelity, maintainers
reviewing implementation mappings, and operators recovering interrupted
local Paper2Code work. They need clear diff and status evidence, not a
database or a benchmark-prescribed state-machine implementation.

The end goal is one non-empty patch against the supplied repository plus
the three required delivery files. All fixture behavior must work offline,
across independent CLI processes, and without affecting projects that do
not request traceability.

Third, add an append-only audit/export surface. Every accepted
registration, review, quarantine, restore, promotion, and reconciliation
appends one tenant/project-scoped hash-chained event. An auditor-authorized
`audit` operation returns deterministic events and the chain head;
unauthorized callers, altered operation bodies, and tampered chains fail
closed. An exact retry after a lost response returns the original export
without another event.

Fourth, add a security quarantine/restore surface motivated by upstream
reports of prompt injection, shell injection, and fail-unsafe recovery. An
authorized security operator quarantines a revision for a bounded reason;
new execution plans and promotions are refused during quarantine. Restore
is a separate, digest- and generation-fenced operation that is safe across
restarts and policy changes. Ordinary projects without traceability remain
unchanged.

Non-goals:

- Do not build a web application, remote queue, hosted collaboration
  service, or live paper search system.
- Do not require LLMs, the network, container daemons, GPUs, or wall-clock
  services when running fixture operations.
- Do not hard-code case names, participant counts, claim IDs, revision
  digests, operation plans, filenames, or expected output.
- Do not replace DeepCode's existing Paper2Code orchestrator, validation
  discovery, durable-run contract, or ordinary compatibility behavior.
- Do not accept report claims, symbol presence, or schema shapes as
  substitutes for executed product behavior.
