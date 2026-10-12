# OpenHands effect recovery Agent-loop migration plan

Status: `PARTIAL` in Stage A. This document describes the migration boundary and
does not claim a successful lower-agent or broker run.

## Authoritative product and six real entry questions

1. **Real edited product entry.** The authoritative source is the OpenHands Agent
   Canvas React/TypeScript client (`@openhands/agent-canvas`), not a generic CLI.
   The production seams are `AgentServerConversationService.sendMessage` and
   conversation creation, `AgentServerRuntimeService.executeCommand`/
   `downloadFile`, `ConversationService` factories, the recovery adapter surface
   required by `input/02_interface_and_delivery.md`, the event store, and the
   conversation recovery UI. The lower launcher therefore executes the patched
   recovery adapter and its product-facing service boundary in a fresh Vitest
   runtime; it never substitutes Codex, Aider, or an external coding agent.
2. **Model connection.** Agent Canvas is a frontend client and does not contain
   the OpenHands Python agent loop. A small evaluator-owned Agent Server shim is
   the existing server boundary: it asks the fixed model to choose among
   case-client actions, then returns the model-authored plan to the patched Canvas
   driver. The candidate sees only `broker-only-placeholder`; only the broker reads
   the evaluator credential. This is an adapter for the real Canvas boundary, not
   a replacement lower product.
3. **Task/action/state/artifact.** A case gives a user-facing recovery task and a
   local action client. Actions exercise effect enqueue/claim/settle/reconcile,
   durable checkpoint inspection, runtime event ingestion, and (where exposed by
   the patch) workspace recovery. Product state is the bounded recovery
   inspection plus action receipts. The lower agent must write a case-bound
   `agent_result.json`; raw tool events and model-authored text are stored
   separately. Dynamic nonce, effect result, manifest, and oracle remain in the
   evaluator service.
4. **Dynamic oracle and visibility.** The evaluator creates fresh case nonce,
   effect IDs, scope IDs, response-loss flags, manifest versions and block
   digests at runtime. The lower process receives only bounded observations from
   the action client, never hidden `case.json`, expected decisions, evaluator
   source, other cases, historical runs, or credential material. The oracle checks
   durable state transitions, callback counts, idempotency, scope/generation
   fencing, and artifact/receipt binding outside the candidate.
5. **Edit effect on behavior.** A correct edit should make a model-driven Canvas
   operator conservative after lost responses and restarts: it should reconcile
   uncertain remote effects exactly once, reject stale owners/events, preserve
   workspace conflict evidence, and report `blocked`/`uncertain` rather than
   claiming completion. The six hidden families cover workspace transfer,
   uncertain remote commit, ABA generation, grant/scope redaction, migration/
   compaction, and dispatcher/sync takeover.
6. **Oracle defense.** Candidate-visible prompts contain schema and legal enum
   values only. Hidden expected values never enter the public package, command
   line, environment, mounted path, or model prompt. The runner verifies that
   output claims are backed by the same rollout's receipts and bounded state;
   fabricated nonces/digests and leaked secrets receive no provenance/safety
   credit.

## Isolation and lifecycle

- Builder package: only `input/` and `dev_cases/` are mounted.
- Candidate build: pristine repository is copied per submission; the patch is
  applied once; generated sources and dependencies use the task-local OpenHands
  cache only.
- Lower case: fresh worktree, fresh browser-like storage, fresh case client,
  fresh rollout directory, no benchmark/evaluator/hidden assets mounted.
- Broker: evaluator-owned process; Candidate endpoint and credential are
  placeholder-only. Broker forcibly records `gpt-5.6-sol`, reasoning `medium`,
  request/failure/token statistics.
- Controller: up to ten distinct accepted Candidates each run `dev_001` and
  `dev_002`; fresh feedback is returned to the same Builder session after every
  acceptance. Builder exit or the round limit freezes the latest accepted
  digest; hidden cases are rejected before freeze and run only after the freeze
  receipt is immutable. A passing dev mean is feedback, not automatic freeze.

## Build/materialize strategy

`adapters/materialize_candidate.py` validates the three delivery files, rejects
absolute/parent/forbidden paths, copies the pristine repository, applies the
patch once, runs `npm run make-i18n`, and performs a strict TypeScript smoke.
The lower launcher then runs an evaluator-owned Vitest driver against that
materialized source. A full app build is a separate optional gate; no formal
Docker/build/API evaluation is started in Stage A.

## Result and Code separation

Agent-loop Result is based on model calls, required product actions, bounded
dynamic facts, state/receipt/provenance binding, final artifact, honest recovery,
and privacy/safety. It is never replaced by the legacy native Vitest score.
Code is an independent eight-dimensional rubric with fixed weights
`15/20/15/15/10/10/10/5`; the two axes are not added or averaged.

## Cost and simple-pilot estimate

Stage A uses zero API calls and zero Docker runs. The planned simple pilot is one
known-good or baseline-compatible Candidate, one public dev case, and one hidden
smoke after a local freeze: approximately 2 successful lower-model Responses
calls per action-oriented case plus retries, 2--5 minutes of broker runtime,
one Node/Vitest process, and about 1--2 GiB peak PSS for dependency-free driver
execution. A realistic candidate build with the pinned dependency tree may use
4--8 GiB peak PSS, 5--15 minutes, and 1--3 GiB disk in the private task cache.
The full up-to-ten-round Builder plus six hidden cases is intentionally not scheduled
here; budget it at 2 Builder calls/round plus 8 lower cases (2 dev x 2 rounds +
6 hidden), with provider latency and npm/build retries as the dominant risk.

## Known risks

- Agent Canvas has no embedded model loop, so the Agent Server shim must remain a
  thin protocol adapter and must not become a generic task solver.
- Candidate patches add recovery modules that the pristine repository lacks;
  baseline TypeScript cannot prove the adapter until a candidate is materialized.
- Browser storage, `window`, and `import.meta.env` require a jsdom/Vitest
  runtime; the launcher must not silently fall back to a Python reimplementation.
- A provider, credential, Docker, mount, or evaluator failure is
  `infrastructure-invalid`/`N/A`, never Candidate zero. A valid lower agent that
  makes zero broker calls is a Candidate/product failure.
- Stage A has no successful broker calls. Therefore the sibling cannot be
  labeled `READY` until simple-pilot evidence records calls > 0 and a hidden
  post-freeze smoke verifies digest/isolation/timing.
