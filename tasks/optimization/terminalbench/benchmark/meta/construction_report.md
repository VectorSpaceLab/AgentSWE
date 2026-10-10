# Construction Report

## v3 optimization-native update (2026-08-13)

- Runnable initial artifact: Clean Live Terminus-2 under `starter_harness/`.
- Editable space: `run_harness.py` and `agent_policy.py` only.
- Split: Arena Track A 10 validation / 40 test (`20260515`); unavailable local `headless-terminal` is replaced by unselected official `hello-world` and recorded in `task_contract.json`.
- All 50 task trees pass required-file structural checks. Full Harbor migration/image/build preflight remains pending in `meta/terminalbench_preflight.json` because Docker registry pulls timed out.
- The earlier compact 3/5 split below is superseded history.

- Source: https://github.com/laude-institute/terminal-bench
- Commit: `d28711d0da2675d0bb1d56de45ae5df6082438a3`
- Public development cases: 3 (accelerate-maximal-square, hello-world, jsonl-aggregator)
- Hidden test cases: 5 (write-compressor, log-summary, csv-to-parquet, count-dataset-tokens, reverse-engineering)

## Design decisions

Selected eight distinct official task IDs with different tool and recovery profiles. `solution.sh` and other reference-only files are excluded from case assets.

## Checks

- One uniform JSONL input/output contract is used for every case.
- Dev and hidden IDs are disjoint and hidden gold stays evaluator-owned.
- No source repository prompts, reference answers, validator internals, or credentials are placed in Builder inputs.
- Rubric weights sum to 100 and scores final artifacts only.
- Every case records its source provenance and pinned commit.
