# Source Repository Analysis

## Identity and Provenance

- Assigned original benchmark: `/opt/agentswe/benchmark/desktop-gui-automation-agent` (read-only evidence).
- Upstream reference analyzed by that benchmark: `https://github.com/bytedance/UI-TARS-desktop`.
- Owner: ByteDance (`bytedance`).
- Pinned commit: `c2ad42e3eb9b27830db41a3e6f51ca7179d9b168` on `main`.
- Commit date: 2026-07-01 03:03:17 UTC.
- License: Apache License 2.0.
- Primary upstream language/runtime: TypeScript, React/Vite, Electron 34, Node.js 20+, pnpm 9.

This V2 construction used the original benchmark's pinned public-repository analysis as evidence. It did not clone, execute, or copy upstream source. The original benchmark directory was fingerprinted before edits and preserved.

## Reference System

UI-TARS Desktop combines an Electron UI, GUI-agent SDK, multimodal screenshot/action loop, desktop and browser operators, action parsing, status callbacks, and trace/report surfaces. Its user value is observe-decide-act GUI control: accept a natural-language instruction, capture current screen state, map a decision to mouse/keyboard/scroll/drag actions, re-observe, stop safely, and retain inspectable action/screenshot evidence.

Reference inputs include instructions, screenshots, model/operator configuration, loop/retry policy, and optional browser/search settings. Outputs are live GUI effects, action/status streams, screenshot-bearing conversations, final/error status, logs, and share/report data. The reference does not provide one stable portable final artifact for every task.

## Capabilities Retained and Increased

The V2 benchmark retains goal interpretation, repeated observation, visible control grounding, pointer coordinates, typing, selection, scrolling, dialogs, drag, retries, stopping, and trace evidence. It substantially increases the operational surface beyond the assigned original benchmark's static single-page forms:

- Stateful fixture servers and protected evaluator-owned state/trace inspection.
- Multi-screen enrollment with async username validation, local verification, terms scrolling, branch choices, and server-memory runtime image CAPTCHA.
- Pagination and virtualization with cross-page selection, derived row qualification, scoped bulk review, hidden prerequisites, and unrelated-record preservation.
- Runtime canvas route and seat-map interpretation where decisive facts are rendered pixels.
- Coordinate-sensitive drag/drop with conflict adjustment and preserved event metadata.
- A narrow responsive flow with stale draft data, hidden prerequisites, modal interruption, scrolling, validation, and review.
- A catalog-to-cart-to-shipping-to-approval transaction with an observable review/no-commit boundary.
- Three screenshot phases including decisive-step evidence rather than before/after alone.

## Boundaries and Exclusions

The benchmark excludes native OS control, real accounts, credentials entered into UIs, payments, external messages, uploads/downloads, production effects, arbitrary sites, remote browser/computer services, model-specific prompts/action syntax, Electron packaging, and upstream architecture. Fixtures are synthetic, loopback-only, and independently authored for this benchmark.

The evaluator can measure exact application state, mutation scope, recovery traces, review and commit counts, visual evidence timing, and artifact validity. It cannot establish performance on arbitrary native applications, OS permissions, multi-monitor/DPR behavior, anti-bot services, or live third-party pages.

## Security Observations

General desktop automation has high host-side risk. This abstraction confines interaction to a harness-started loopback fixture and blocks direct application-internal mutation. The state bridge requires a fresh harness-only token. Case text and UI content are untrusted and cannot expand network/filesystem authority. The runtime CAPTCHA answer and spatial seeds live only in server memory; static case text/client source does not contain the runtime answer.

## Difference from the Assigned Original

The original benchmark had 3 development and 8 hidden cases, each centered on a static `mock_ui.html`, a browser-readable JavaScript bridge, and mostly bounded single-page controls. V2 has exactly 2 development and 6 hidden cases, each a runnable application server with protected application-owned evidence, multi-page/stateful workflows, runtime visual challenges, decisive-step screenshots, 600 seconds, and 4 GiB. V2 tests capability rather than imitation and does not require the upstream repository's code, model, prompt, or file layout.
