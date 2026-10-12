# Construction Report

## v3 optimization-native update (2026-08-13)

- Runnable initial artifact: OpenClaw Basic Productivity Agent under `starter_harness/`.
- Editable space: `run_harness.py` and `productivity_policy.py` only.
- All 147 source tasks were audited for fixtures, grading, timeout, private integration, live-network stability and authorized capability.
- The stratified eligible split is 20 public development / 40 hidden; `meta/eligibility_audit.json` records selection/exclusion. The earlier compact 3/5 split below is superseded history.

- Source: https://github.com/pinchbench/skill
- Commit: `819384ae830492365b8363fc26bc2602e73f216d`
- Public development cases: 3 (task_files, task_calendar, task_csv_iris_summary)
- Hidden test cases: 5 (task_csv_cities_density, task_email_triage, task_dockerfile_optimization, task_codebase_navigation, task_browser_automation)

## Design decisions

Used three public core tasks and five hidden tasks spanning file creation, calendar simulation, CSV analysis, email triage, Docker, codebase navigation and browser workflow.

## Checks

- One uniform JSONL input/output contract is used for every case.
- Dev and hidden IDs are disjoint and hidden gold stays evaluator-owned.
- No source repository prompts, reference answers, validator internals, or credentials are placed in Builder inputs.
- Rubric weights sum to 100 and scores final artifacts only.
- Every case records its source provenance and pinned commit.
