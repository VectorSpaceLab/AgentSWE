# OpenHands effect and workspace-recovery Edit benchmark v10

This Edit benchmark measures whether a coding agent can add a backend-scoped transactional effect-delivery ledger to a fixed OpenHands Agent Canvas TypeScript application. Evaluation observes cross-instance lease fencing, persistent outbox stages, recovery at every callback boundary, schema migration, pause/resume/cancel ABA prevention, least-privilege browser-tab checks, and real service/store/UI integration.

Cycle 8 retains ledger, scheduler, and cross-table synchronization invariants while adding a substantially independent workspace-reconnection transaction. A three-way coordinator produces an immutable manifest, restores verified content-addressed blocks, commits the remote target through the effect ledger, applies the exact revision locally, and advances the scheduler/sync cursor last.

## Builder boundary

Provide only `input/`, `dev_cases/`, and a writable copy of `input/repository/`. Do not provide `test_cases/`, `evaluator/`, `meta/`, prior submissions, or evaluation results. The Builder returns exactly `solution.patch`, `edit_report.json`, and `run_report.json`.

The fixed source is the Agent Canvas commit `7fa7b16968eeae6d8f5ea4f8136225ac8e621fe6`, represented by the clean local snapshot commit `c38e8bc72fbadca097620fa847c07e403005d0ed`.

Read the four Builder documents in order. They define a unified patch/report delivery contract and the observable ledger, scheduler, synchronization, and workspace-coordinator APIs. No source-repository or hidden-case access is required.

## Public development

Run the two public cases against a Candidate submission:

```bash
python dev_cases/run_dev_case.py --case dev_001 --submission <submission> --output-dir <output-1>
python dev_cases/run_dev_case.py --case dev_002 --submission <submission> --output-dir <output-2>
```

`dev_001` uses an independent store instance and an injected `after_claim` process boundary, then rebuilds and coordinates an unsafe delivery. It also drops a synchronization response and demonstrates recovery from an unchanged durable cursor. `dev_002` races lease takeover, limits check grants to one tab/backend, commits a recovery event before a UI-store side effect, observes service/store convergence from browser-storage events, and demonstrates a successor reconciling a delayed production callback. It opens, replaces, closes, and recovers injected historical/socket scheduler sessions, including terminal echoes and delayed stale high-cursor events. It then freezes an in-progress production sync leader and has a new incarnation take over the same tab ID before releasing protected stale responses.

The self-contained runner validates delivery/allowed paths, applies the patch once to the original archive, uses task-local pinned dependencies, generates i18n and React Router declarations, injects Builder-visible type probes, selects public integration cases before strict TypeScript, focuses on upstream compatibility tests, and runs public Vitest cases. It writes bounded, edited `run_summary.json` evidence.

## Hidden evaluation

Keep `test_cases/` and `evaluator/` outside each Builder worktree. Run:

```bash
python evaluator/harness/evaluate_suite.py \
  --repository input/repository \
  --cases-root test_cases \
  --submission <submission> \
  --output-dir <output>
```

The suite applies the patch once, builds once, runs focused compatibility gates, and then independently runs all six cases. Behavioral timeouts/failures are local to their case. Missing or error results on the evaluator side reject aggregation and invalidate the suite. Valid case scores are `0..100`; the benchmark score is their arithmetic mean. Stable assertion evidence and all case-local caps are written to `run_summary.json`.

## Environment and memory

The environment requires Node at least `22.12.0` and npm exactly `10.5.0`. Warm it once:

```bash
python evaluator/harness/prepare_environment.py --repository input/repository
```

Commands are monitored through Linux `/proc` using summed process-tree PSS, with RSS fallback only when PSS is unavailable. The cap is 8 GiB (`8589934592` bytes); there is no `RLIMIT_AS`, `ulimit -v`, `--jitless`, or small virtual-address limit. PSS peaks and the cap appear in the runner evidence.

## Evaluator inputs and results

For each case, provide its `input.md` and assets, the patched final product, executable Vitest results, and `evaluator/rubric.md`. Scores use only observable persistent state, callback counts, service/store state, storage-event aggregation, transport call/response counts, cursor movement, and rendered UI. Implementation form and Candidate-written tests are ignored.

Evidence is bounded and redacted. Fixture credentials, prompt tails, claim/fencing/grant tokens, and private paths must never appear in results. The dependency-free evaluator self-test audits the exact `4/2/6` inventory, public isolation, one-patch behavior, assertion inventory, cap accounting, resource parity, Cycle-8 workspace coverage, case isolation, and missing-result validity. Synchronization and workspace modules are intentionally absent from the global type probe, so failures remain behavioral and case-local.

Cycle 10 changes only local caps for workspace-availability cases `test_001`, `test_002`, and `test_006`, from 20 to 30. Public contracts, cases, assertions, weights, 120-second case isolation, and the 8 GiB PSS cap are unchanged.
