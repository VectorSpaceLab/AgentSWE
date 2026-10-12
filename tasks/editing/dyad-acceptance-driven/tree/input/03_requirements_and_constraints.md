# Requirements and constraints

## Functional requirements

1. Preserve the stored/active `acceptance` mode, production `chat:stream`,
   test preconditions, the durable ledger, stable Unicode/SHA-256
   requirement identities, behavior tests, mutation sensitivity, bounded
   repair, the focused-fix-before-regression order, integrity detection,
   trace links, honest partial outcomes, retryable durable control,
   generation isolation, and ownership isolation.
2. Register all preview and attestation contracts through Dyad's trusted
   typed IPC system and `registerIpcHandlers`; expose typed clients and
   events.
3. Persist the preview before acknowledgement and perform the same start
   exactly once across concurrent deliveries, lost responses, renderer
   retries, remounts, and database/main re-initialization.
4. Enforce one live preview owner per run generation. Refuse stale,
   conflicting, foreign, and invalid targets and competing starts without
   starting work.
5. Drive the production test isolation/Stop/output path. Do not implement a
   standalone fake runner or a second Playwright process manager.
6. Persist monotonic setup/running/terminal progress. Deduplicate runner
   output, ignore reordered regressions, bound text, and prevent terminal
   overwrite.
7. Compute frozen workspace revisions and target fingerprints from real app
   files. Detect edits, deletions, replacements, skipped-test weakening, and
   dirty-state drift during execution.
8. Atomically create one immutable terminal attestation with a deterministic
   digest. Recover it after restart; never infer it from renderer memory.
9. Treat setup failure, empty/partial/corrupt results, cancellation,
   revision drift, stale generations, and delayed output as non-passing
   evidence.
10. Allow passing evidence only from the current, matching ledger `passed`
    attestation. Keep old facts diagnostically and never revive an
    invalidated attestation when bytes match again later.
11. Enforce app, chat, run, session, and attestation ownership before
    writing or disclosing on verification. Opaque IDs are correlation values,
    not capabilities.
12. Add user-facing run affordances and progress/navigation behavior in the
    preview using the existing Preview/Tests conventions and accessible
    controls.
13. Preserve ordinary test-panel runs, the local agent `run_tests`, headed
    and headless modes, app preview, Build/Ask/Plan/Agent chat,
    consent/read-only guards, existing apps, and default/deprecated chat mode
    migration.
14. Add focused unit/persistence/transaction tests, renderer behavior tests,
    typed IPC ownership/restart tests, and deterministic production
    integration.
15. Expose `currentTarget` in the run snapshot and have the preview session
    preserve the accepted command's `target` field by field;
    `testFingerprint` is the SHA-256 of the target file's raw bytes at the
    moment of admission.
16. Make the two revision fields and the attestation's `finishedRevision`
    genuinely content-addressed: a clean run has all three equal, in-run
    drift makes `finishedRevision` differ from `startedRevision`, and a byte
    restoration recomputes back to the original value.
17. Implement the append-only `deniedRequests` ledger: every denied request
    (stale-generation control, foreign-owner control, unauthorized read)
    appends exactly one record with `classification`, a bounded `reason`,
    `controlSequence`, and `at`; apart from the ledger a denied request
    changes no snapshot field, in particular not `runId`, `generation`, or
    `controlSequence`. Reads themselves must be byte-stable.
18. Denials and unauthorized reads must not disclose the internal state of
    another session/run/attestation, and the ledger must not echo the
    `runId`/`sessionId`/`attestationId` strings carried by the denied
    request.
19. The same `operationId` carrying different command content is refused as
    a conflict: no second test process is started, the existing session
    identity and event sequence are unchanged, and no second attestation is
    produced.
20. At most one attestation per session; late callbacks, duplicate Stops,
    duplicate terminal callbacks, and restart reconciliation all return the
    same `attestationId`, `outcome` agrees with the session's terminal
    state, and `lastEventSequence` never decreases.
21. After a restart, reconcile interrupted `queued`/`setup`/`running` work
    into a terminal state with a bounded reason, keep `runId` unchanged,
    keep `generation` and `controlSequence` monotonically non-decreasing,
    and do not re-run automatically.
22. Ordinary Build/Ask/Agent chat and Tests list reads must not change any
    byte of an Acceptance run snapshot.

15. Expose `currentTarget` in the run snapshot and have the preview session
    preserve the accepted command's `target` field by field;
    `testFingerprint` is the SHA-256 of the target file's raw bytes at the
    moment of admission.
16. Make the two revision fields and the attestation's `finishedRevision`
    genuinely content-addressed: a clean run has all three equal, in-run
    drift makes `finishedRevision` differ from `startedRevision`, and a byte
    restoration recomputes back to the original value.
17. Implement the append-only `deniedRequests` ledger: every record carries
    `classification`, a bounded `reason`, `controlSequence`, and `at`; apart
    from the ledger a denied request changes no snapshot field.
18. Denials and unauthorized reads must not disclose the internal state of
    another session/run/attestation, and the ledger must not echo the
    `runId`/`sessionId`/`attestationId` strings carried by the denied
    request.
19. The same `operationId` carrying different command content is refused as
    a conflict: no second test process is started, the existing session
    identity and event sequence are unchanged, and no second attestation is
    produced.
20. At most one attestation per session; late callbacks, duplicate Stops,
    duplicate terminal callbacks, and restart reconciliation all return the
    same `attestationId`, `outcome` agrees with the session's terminal
    state, and `lastEventSequence` never decreases.
21. After a restart, reconcile interrupted `queued`/`setup`/`running` work
    into a terminal state with a bounded reason, keep `runId` unchanged,
    keep `generation` and `controlSequence` monotonically non-decreasing,
    and do not re-run automatically.
22. Ordinary Build/Ask/Agent chat and Tests list reads must not change any
    byte of an Acceptance run snapshot.

## Implementation constraints

- Follow `AGENTS.md`, `CONTRIBUTING.md`, and the relevant IPC, database,
  state-machine, Git, testing, hybrid-test, strict TypeScript, UI, and error
  rules.
- Use the trusted contract-defined IPC. Do not add direct production
  `ipcMain.handle`, renderer-owned authority, or untyped preload bridges.
- Use asynchronous filesystem APIs in production. SQLite changes require a
  generated drizzle migration, a journal entry, and snapshot consistency.
- Keep operation, session, run, generation, sequence, revision, target, and
  attestation identities stable across async boundaries. Coordinate
  terminal, stop, restart, and workspace-change races transactionally.
- Do not expose internal writer/lease secrets in renderer events or reads.
  Do not persist credentials, absolute app roots, raw environment values, or
  unbounded logs in session, attestation, or acceptance evidence.
- Do not hard-code public/hidden prose, fixture markers, actor counts, event
  schedules, target names, operation IDs, expected digests, or evaluator
  paths.
- Product execution is offline. Do not read hidden/evaluator files or use
  live model/search results as runtime oracles.
- Existing tests and snapshots must not be modified, deleted, skipped, or
  weakened. New tests belong in the established test trees.
- Allowed patch paths are `src/**`, `drizzle/**`, `e2e-tests/**`,
  `testing/fake-llm-server/**`, `package.json`, and `package-lock.json`.
  Forbidden paths include `.git/**`, `.github/**`, `.claude/**`, `AGENTS.md`,
  `rules/**`, licenses, `out/**`, `node_modules/**`, and benchmark files.
- The patch must add integrated production behavior. Prompt-only,
  test-only, schema-only, storage-only, or detached-helper changes cannot
  score highly.
- Use the exact three-file delivery contract. Do not leak secrets, source
  dumps, private reasoning, fabricated command results, or hidden material.
- A passing result returned by the runner, the existence of an event, the
  existence of a field, or a process exit code must not be treated as
  acceptance passing.
- The reasons for denials, conflicts, invalidation, cancellation, and restart
  reconciliation must be bounded factual strings without absolute paths,
  credentials, unbounded logs, or another session's internal state.
- A passing result returned by the runner, the existence of an event, the
  existence of a field, or a process exit code must not be treated as
  acceptance passing.
- The reasons for denials, conflicts, invalidation, cancellation, and restart
  reconciliation must be bounded factual strings without absolute paths,
  credentials, unbounded logs, or another session's internal state.

## Additional requirements (0920)

23. `currentTarget` must equal the run's Acceptance target field by field;
    `grep` is the filter value the product actually uses, and is null only
    when no filtering is actually applied.
24. Two consecutive reads of the same state must return identical bytes: an
    attestation is always immutable, and a terminal preview session must not
    change any field because it was read.
25. `resultDigest` must equal the result of the canonical digest formula
    given in item 6 of "Independently verifiable product contract" in
    `input/02`, recomputable by any caller from the observed terminal-state
    fields.
26. A non-`passed` attestation must not make the run `passed` and must not
    be recorded as passing evidence; a green runner result by itself is
    never acceptance passing.
