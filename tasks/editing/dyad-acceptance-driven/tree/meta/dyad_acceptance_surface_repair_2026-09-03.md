# Dyad typed Acceptance surface repair — 2026-09-03

## Scope and isolation

This work is confined to sibling `19-edit-dyad-acceptance-driven-agentloop-v1`.
The Candidate product implementation was developed in:

```text
.work/acceptance-adapter-reference/
```

The pinned authoritative source, `0830` staging, old runs, and aggregate documentation were not modified. No formal/provider-backed run was started.

## Baseline blocker

The pinned Dyad baseline had no active `acceptance` ChatMode, no typed `acceptance:*` IPC contracts, no Acceptance handlers/events, and no durable run/session/attestation ledger. The prior headless probe's acceptance-to-`local-agent` fallback therefore violated the product boundary; it has been changed to fail closed.

## Implemented surface

The Candidate patch is recorded at:

```text
solution.patch
```

The patch adds:

- `src/ipc/types/acceptance.ts`: seven typed invoke contracts (`get-run`, `get-latest-run`, `control-run`, `get-control-receipt`, `start-preview`, `get-preview`, `get-attestation`) and three typed events (`run-updated`, `preview-updated`, `attestation-updated`).
- `src/ipc/services/acceptance_store.ts`: main-owned durable `acceptance-ledger.json`, atomic temp-file rename, serialized writes, app/chat-scoped reads, operation-id replay, immutable terminal sessions/attestations, revision and target fingerprints, and cancellation marking.
- `src/ipc/handlers/acceptance_handlers.ts`: ownership and generation checks, safe `e2e-tests/*.spec.*` target validation, typed preview lifecycle, terminal attestation creation, and reuse of `runAppTestsWithIsolation` with `source: "acceptance"`.
- `src/lib/schemas.ts` and related chat-mode plumbing: explicit active/stored `acceptance` mode, local-agent-backed execution with a distinct persisted/requested mode, and no ordinary-chat evidence promotion.
- `src/ipc/ipc_host.ts`, `src/ipc/preload/channels.ts`, and `src/ipc/types/index.ts`: production registration and preload whitelist.
- `src/ipc/handlers/tests_handlers.ts`: Acceptance cancellation is connected to the existing `tests:stop` controller path.
- `src/testing/hybrid_chat_harness.tsx`, `src/hooks/useTestRunEvents.ts`, prompt/token-count plumbing: typed propagation for the new explicit mode.

Sibling runner change:

```text
environment/headless_chat_flow.py
```

now throws when Acceptance is requested but the product does not expose the mode; it never silently configures `local-agent`.

## Low-cost verification

Executed from `.work/acceptance-adapter-reference`:

```bash
npx vitest run src/ipc/handlers/acceptance_handlers.test.ts \
  src/ipc/services/acceptance_store.test.ts --config vitest.config.ts
```

Result: **PASS**, 2 test files and 3 tests passed. Coverage includes typed handler registration, invoke/receive preload allowlists, durable reload, idempotent operation replay, and terminal attestation immutability.

```bash
npx tsgo -p tsconfig.app.json --noEmit --incremental false
```

Result: **non-zero due to pre-existing baseline/type-dependency failures** in unrelated UI and fake-server files. Filtering the output for all Acceptance and directly modified product files produced no errors. The command also avoids the temporary `node_modules/.tmp` permission issue caused by incremental build-info writes.

Executed from the sibling:

```bash
python3 -m py_compile smoke/self_test.py environment/headless_chat_flow.py \
  adapters/*.py controller/*.py evaluator/**/*.py
node --check dev_cases/assets/acceptance-benchmark.js
python3 smoke/self_test.py
```

Result:

```text
SELF_TEST=PASS; lifecycle=2-round; stub-rejection=PASS; hidden-after-freeze=PASS; code-axis=100; broker-calls=0 (static only)
```

No provider calls, hidden formal execution, or expensive Electron/Playwright suite was run.

## Remaining blockers

1. Full product typecheck remains blocked by unrelated pre-existing errors in `ChatList`, `ChatTabs`, generated query typing, and fake-server dependency declarations. Acceptance-specific and directly modified source files are clean under the filtered typecheck.
2. The product-entry smoke validates handler registration/whitelisting and durable ledger behavior, but a real provider-backed chat plus Playwright preview was intentionally not run under the requested low-cost limit. Therefore this is not a formal benchmark result.
3. The patch provides the Candidate-side surface; the normal renderer UI does not yet expose a dedicated Acceptance control panel. The typed IPC entry is available for the benchmark probe and future UI wiring.

Formal status remains: **N/A / not started**.
