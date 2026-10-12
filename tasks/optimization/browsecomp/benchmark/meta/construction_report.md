# Construction Report

## v3 optimization-native update (2026-08-13)

- Runnable initial artifact: Clean Basic Search ReAct under `starter_harness/`.
- Editable space: `run_harness.py` and `agent/**` only.
- Split: Arena Track A seeded validation 50 / test 100 (`20260529`).
- `task_contract.json` is the machine-readable source of truth. The earlier compact 3/5 split below is superseded history.

- Source: https://github.com/openai/simple-evals
- Commit: `652c89d0ca9df547706735883097e9537d40dc47`
- Public development cases: 3 (browsecomp::0, browsecomp::1, browsecomp::2)
- Hidden test cases: 5 (browsecomp::3, browsecomp::4, browsecomp::5, browsecomp::6, browsecomp::7)

## Design decisions

Selected different topics and multi-hop questions by fixed row index. The answer key is evaluator-owned and is not copied into case inputs.

## Checks

- One uniform JSONL input/output contract is used for every case.
- Dev and hidden IDs are disjoint and hidden gold stays evaluator-owned.
- No source repository prompts, reference answers, validator internals, or credentials are placed in Builder inputs.
- Rubric weights sum to 100 and scores final artifacts only.
- Every case records its source provenance and pinned commit.
