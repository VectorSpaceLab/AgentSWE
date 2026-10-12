# Reference Baseline

## Upstream Execution

The upstream UI-TARS Desktop implementation was not run. This construction inherited a pinned, read-only source analysis from the assigned original benchmark. Running upstream would require a large Node/Electron build, graphical desktop permissions, operator configuration, and model resources outside this local deterministic construction task. No upstream code, release binary, remote computer, or production browser was invoked.

## Assigned Original Benchmark

The assigned original benchmark was inspected but not executed. Its documents, 3 development requests, 8 hidden requests, and representative static HTML fixture were used to identify the baseline abstraction and shortcomings. Its source tree was SHA-256 fingerprinted before V2 edits. No original file was modified or copied into V2.

The original establishes feasibility for visible-control browser automation, JSON/screenshot artifacts, validation handling, tables, dialogs, scrolling, and basic drag actions. It does not establish the V2 capability increase: stateful server-backed fixtures, runtime CAPTCHA/image state, evaluator-only bridge, application-owned decisive snapshots, multi-page transaction boundaries, virtualized data, or runtime-varying canvas spatial decisions.

## V2 Fixture Baseline

All V2 fixtures use Python's standard-library loopback HTTP server and browser-owned JavaScript application logic. The dedicated validation environment uses Python 3.11 and Playwright 1.61 with Chromium 149. Validation launches every fixture with a unique evaluator token, checks health/readiness, renders at the case viewport, exercises the generated CAPTCHA endpoint and protected bridge, and drives each decisive path in a real headless browser. Detailed commands and outcomes are recorded in `construction_report.md`.

## Limitations

There is no measured upstream or general-agent score on the six hidden cases, no latency/token baseline, and no claim that the reference architecture can solve them. The intended difficulty comes from combined observation, navigation, memory, scope, recovery, and verification. A builder may score fully with any compliant implementation.
