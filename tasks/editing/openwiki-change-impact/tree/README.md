# OpenWiki change-impact executable-docs Edit v2, Cycle 002

> Agent-loop migration note: this sibling adds `agentloop/` for a real patched OpenWiki lower-agent protocol. The copied native runner is retained as read-only provenance/mechanism comparison; it is not the Agent-loop Result.

This Agent Edit benchmark measures whether a coding agent can extend a fixed OpenWiki TypeScript CLI with four combined production features:

- deterministic change-impact documentation repair and executable examples;
- durable request receipts for retries, restarts, and overlapping workers;
- an independent, immutable static wiki publication; and
- generation-consistent full-text search through production CLI commands.

The hard boundary is convergence. A coordinated update may never expose a partially maintained documentation set, an active static site, or an active search index from different generations. Product scenarios are deterministic and offline.

## Builder package

Give the Builder only:

- `input/01_task_goal.md` through `input/04_resources.md`;
- a writable copy of `input/repository/`; and
- the complete `dev_cases/` directory.

Do not expose `test_cases/`, `evaluator/`, or `meta/`. The repository is the product being edited, not a reference implementation. The Builder must return exactly `solution.patch`, `edit_report.json`, and `run_report.json`.

## Public development

Run either public scenario against the delivery:

```bash
python3 dev_cases/run_public.py \
  --candidate /absolute/path/to/delivery \
  --case dev_001 \
  --work-dir /absolute/path/to/scratch
```

Use `dev_002` for expired-owner recovery, publication/index recovery, and conflicting retries. The public runner validates the three-file delivery, applies the patch once to the fixed source, installs pinned dependencies when needed, builds both TypeScript projects, runs focused upstream regressions, calls the compiled update and search entry points, and prints behavioral assertion evidence. `--keep-work` retains the materialized repository for debugging.

## Hidden evaluation

Keep hidden requests, fixed assets, evaluator inventories, and evaluator code isolated. The canonical suite runs six cases in order:

```bash
python3 evaluator/harness/run_hidden.py \
  --candidate /absolute/path/to/candidate \
  --work-dir /absolute/path/to/scratch \
  --output /absolute/path/to/hidden-results.json
```

The suite builds once and continues after ordinary case failures. Malformed delivery or patch/build failure is a Candidate validity failure. A production call that returns valid failure evidence remains case-local behavior.

## Evaluation

For each case, provide its request/assets, materialized repository, response artifacts, immutable publication/search state, complete call records, evaluator-owned inventory, global headings, and canonical assertion evidence, as specified by `evaluator/eval_prompt.md`.

Each hidden rubric totals 100 points. Sum the six final scores with an unweighted arithmetic mean, keeping full precision until the final two-decimal rounding. Public scores are development feedback only.

Dependency installation is setup only. Product update, publication, and search receive no provider credentials and may not use the network.
