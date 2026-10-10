# Construction Report

## Parameters and Assumptions

- Task type: difficult Create-Agent benchmark.
- Slug/output: `desktop-gui-automation-agent-v2`.
- Assigned original: local `desktop-gui-automation-agent`, treated as read-only evidence.
- Counts: exactly 2 public development cases and 6 hidden cases.
- Uniform invocation: `python run_agent.py --input <input.md> --output <output_dir>`.
- Final target: structured workflow result plus initial, decisive-step, and final PNG evidence; application state/trace remains authoritative in a protected harness bridge.
- Runtime: 600 seconds and 4 GiB per case.
- Dedicated prefix: `/opt/agentswe/benchmark/envs/desktop-gui-automation-agent-v2`.
- Network/model/search resources follow the 2026-07-25 canonical contract; all cases are closed-corpus, so search and retrieval should be zero.

The harness-started fixture URL is supplied through `GUI_FIXTURE_URL`. This was chosen so the uniform command remains exact, ports can be allocated safely, and the harness retains the application process and evaluator token after the submitted agent exits.

## Coverage Design

Development demonstrates the contract without revealing hidden solutions. `dev_001` covers five-screen enrollment, stale validation, asynchronous availability, branching, terms scroll, simulated email verification, review, and a server-memory PNG CAPTCHA. `dev_002` covers filters, pagination, stale selection, cross-page multi-selection, scoped bulk review, exact mutation, and preservation.

Hidden cases are structurally distinct:

| Case | New capability combination |
| --- | --- |
| `test_001` | Runtime canvas graph, hazard/eyewash interpretation, ordered coordinate selection, itinerary review |
| `test_002` | Coordinate drag/drop on a calendar, conflict modal, adjusted time, metadata preservation |
| `test_003` | 390 px responsive navigation, interruption modal, stale personal fields, policy scroll, async prerequisites |
| `test_004` | Cross-page catalog/cart/shipping/approval review with exact promo math and mandatory no-commit boundary |
| `test_005` | Virtualized/filterable ledger, derived selections, legal-hold exclusion, prerequisite failure and retry |
| `test_006` | Runtime seat-map pixels, spatial adjacency/front/east-exit constraints, interruption recovery, confirmation |

This includes runtime degradation/recovery paths rather than missing credentials or corrupt assets. Difficulty is produced by realistic capability composition, not undisclosed facts: every requirement is stated in the active case and every runtime fact is visibly observable.

## Fixture and Evidence Design

Each case contains only `input.md` and necessary `assets/index.html` plus `assets/serve.py`. A fixture records state and trace only when rendered client event handlers run. `/__evaluator__/state` requires a fresh token supplied at server launch and never exposed through the client or agent environment. It returns application-owned state, event trace, decisive snapshots, and runtime challenge metadata. Direct mutation/application-function calls and protected bridge access are prohibited and are zero-gate violations.

CAPTCHA digits are created from `SystemRandom` in server memory at launch and rendered into a noisy PNG with a small server-side rasterizer. The answer is absent from input text and static HTML/JavaScript. Canvas route and seat layout variants use server-memory random seeds. Verification codes are local and intentionally visible only in the rendered simulated inbox.

## Provenance

All names, IDs, records, policies, products, schedules, amounts, and applications are synthetic and authored for this benchmark. No personal data, real account, payment, message, copyrighted media, or live production system is used. Upstream identity and license facts come from the assigned original benchmark's pinned public analysis; no upstream implementation or asset was copied.

## Environment and Command Record

- Read the complete benchmark skill, artifact specification, V2 common contract, canonical resource contract, assigned original tree, builder documents, requests, metadata, and representative fixture.
- Fingerprinted all 34 original benchmark files with SHA-256 before V2 construction.
- Created the dedicated Conda prefix with Python 3.11 and pip.
- Installed Playwright 1.61.0. Full Chromium 149 downloaded successfully; the optional duplicate headless-shell download was interrupted after stalling because validation explicitly uses the installed full Chromium executable.
- Fixture validation uses loopback only, unique ephemeral ports, fresh evaluator tokens, browser console/page-error capture, screenshot/image checks, protected-bridge reads, and case-specific decisive interactions.

## Browser Validation Results

The final full validation run completed successfully for all 8 fixtures on 2026-07-25. Every case reached `data-benchmark-ready="true"`, denied an evaluator-bridge request without the token with HTTP 403, accepted the protected request with the correct token, emitted nonempty application-owned state and trace, had no uncaught browser error, and completed its case-specific decisive path. Initial, settled decisive-step, and final viewport PNGs were captured for all cases. All 24 PNG headers match their case viewport dimensions. Both canvas fixtures produced nonblank element screenshots.

Observed UI-owned trace/event counts were 20 (`dev_001`), 8 (`dev_002`), 9 (`test_001`), 8 (`test_002`), 20 (`test_003`), 21 (`test_004`), 15 (`test_005`), and 8 (`test_006`). A second enrollment fixture launch produced a different CAPTCHA PNG SHA-256 hash, confirming launch-time challenge variation. The generated CAPTCHA was solved through the rendered challenge path using evaluator-authorized runtime metadata solely for validation. The procurement final bridge remained reviewed with `commitCount: 0`; ledger recovery recorded the required blocked first apply and successful retry; route and seat canvases recorded runtime-correct selections.

Visual inspection covered the runtime route canvas, 390 x 844 mobile receipt, procurement no-commit review, and runtime seat map. A first seat-map render revealed crowded labels; row spacing was revised and the full eight-case browser validation was rerun successfully. Validation artifacts and the machine-readable report are temporary construction evidence under `orchestration/four-v2-20260725/desktop-gui-v2-validation/`, not benchmark case inputs.

## Consistency and Leakage Checks

The final validation checks exact tree/counts, exactly four builder inputs, 2/6 case counts, one input and necessary assets per case, UTF-8 decoding, relative links, Python compilation, rubric arithmetic (35+20+15+15+10+5=100), consistent 600-second/4 GiB resources, provider names/count fields, no secret values, no unfinished markers, no source-repository mention in builder inputs/cases, runtime CAPTCHA variation across launches, protected-bridge 403 without token, and original fingerprint equality.

## Known Limitations and Risks

- These are browser applications, not native OS windows; host desktop permissions and multi-monitor behavior are intentionally out of scope.
- Client event recording is delivered to the fixture server over loopback. A malicious submission could attempt direct HTTP posts, but that violates an explicit zero gate; harness network logging and trace/screenshot consistency provide detection evidence.
- Static client code necessarily contains UI mechanics and synthetic base records. The agent is prohibited from reading source as a substitute for rendered observation; runtime CAPTCHA answers and spatial variants remain server-memory-only.
- Visual validation uses the installed Chromium build. Minor font rasterization may differ on another compatible browser without affecting state.
- No upstream reference success score is claimed. Intended hidden difficulty is based on combined capabilities rather than an empirical population run.

## Inventory

Expected final inventory: `README.md`; 4 builder inputs; 2 development `input.md` files with 4 fixture assets; 6 hidden `input.md` files with 12 fixture assets; 2 evaluator files; and 3 metadata files. No per-case oracle, rubric, expected output, or evaluator note is present.
