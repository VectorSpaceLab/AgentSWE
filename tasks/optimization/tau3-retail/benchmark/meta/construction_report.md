# Construction Report

## v3 optimization-native update (2026-08-13)

- Runnable initial artifact: Policy-aware ReAct under `starter_harness/`.
- Editable space: `run_harness.py` and `policy_react.py` only.
- Split: first 20 ordered tasks from official retail `train`, plus all 40 tasks from official retail `test`; all 60 upstream task IDs are unique.
- Simulator, policy, tools, DB, user simulator, model/provider, budgets and reward are fixed. The earlier compact 3/5 split below is superseded history.

- Source: https://github.com/sierra-research/tau2-bench
- Commit: `668d3bcd135c02aa3438f987ef45735b7c163ee3`
- Public development cases: 20 official retail train tasks.
- Hidden test cases: 40 official retail test tasks.

## Design decisions

Selected a deterministic subset of 20 official retail train tasks for development and retained the complete official 40-task retail test split for hidden evaluation. This avoids duplicated tasks and keeps one domain, one policy/tool surface, and one official evaluator protocol across both roles.

## Checks

- One uniform JSONL input/output contract is used for every case.
- Dev and hidden IDs are disjoint and hidden gold stays evaluator-owned.
- No source repository prompts, reference answers, validator internals, or credentials are placed in Builder inputs.
- Rubric weights sum to 100 and scores final artifacts only.
- Every case records its source provenance and pinned commit.
